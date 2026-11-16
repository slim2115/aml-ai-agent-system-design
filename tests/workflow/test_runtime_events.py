"""Bounded runtime telemetry controls; no model or network calls."""
from copy import deepcopy
import json
import threading
import time
from uuid import uuid4

import pytest

from src.contracts.v1 import instant
from src.runtime.service import AMLAgent
from src.workflow.auth import Principal
from src.workflow.controller import WorkflowService
from src.workflow.store import MAX_RUNTIME_EVENTS, SQLiteStore, WorkflowError, WORKFLOW_ROOT
from src.workflow.worker import ManagedWorker
from tests.runtime.test_service import FakeIndex, FakeTransport
from tests.workflow.test_controller import ACTOR, request, until
from tests.workflow.test_worker import boot, result_for


def observation(stage='input', code='STARTED', attempt=0, correlation=None):
    return {'correlation_id': correlation or 'run-' + 'a' * 32,
            'stage': stage, 'code': code, 'attempt': attempt}


def streaming_handler(state, incident_id, engine, *, event_sink):
    correlation = 'run-' + uuid4().hex
    event_sink(observation(correlation=correlation))
    if incident_id == 'INC-000001':
        time.sleep(60)
    if incident_id == 'INC-000002':
        raise RuntimeError('SECRET PAN 4111111111111111')
    if incident_id == 'INC-000003':
        event_sink(dict(observation(correlation=correlation), payload='SECRET PAN 4111111111111111'))
    if incident_id == 'INC-000004':
        for _ in range(MAX_RUNTIME_EVENTS):
            event_sink(observation(correlation=correlation))
    event_sink(observation('complete', 'POLICY_NOT_APPROVED', correlation=correlation))
    return result_for(incident_id)


def bad_event_child(connection, _bootstrap, _handler):
    connection.send_bytes(b'{"kind":"ready","provenance":{}}')
    payload = json.loads(connection.recv_bytes())
    connection.send_bytes(json.dumps({'kind': 'event', 'request_id': payload['request_id'],
        'sequence': 1, 'event': dict(observation(), payload='4111111111111111')}).encode())
    time.sleep(60)


@pytest.fixture
def store():
    return SQLiteStore(WORKFLOW_ROOT / ('runtime-events-' + uuid4().hex) / 'state.sqlite3')


@pytest.fixture
def event_worker():
    with ManagedWorker(bootstrap=boot, handler=streaming_handler) as worker:
        assert worker.start(timeout_seconds=15)['ready']
        yield worker


def test_runtime_emits_strict_stages_and_attempts_without_generated_text():
    seen = []
    runtime = AMLAgent(mode='demo', engine='local-model', index_factory=FakeIndex,
                       transport=FakeTransport(invalid_attempts=1), event_sink=seen.append)
    result = runtime.run('INC-000123')
    assert result['status'] == 'draft'
    assert seen == [event.to_dict() for event in runtime.last_events]
    assert {item['stage'] for item in seen} == {'input', 'policy', 'context', 'retrieval', 'generation', 'validation', 'complete'}
    assert any(item['code'] == 'RETRY' and item['attempt'] == 1 for item in seen)
    assert any(item['stage'] == 'generation' and item['attempt'] == 2 for item in seen)
    assert seen[-1]['stage'] == 'complete' and seen[-1]['code'] == 'DRAFT'
    assert len(seen) <= MAX_RUNTIME_EVENTS
    assert all(set(item) == {'correlation_id', 'stage', 'code', 'attempt'} for item in seen)
    assert 'fabricated' not in json.dumps(seen)


def test_runtime_records_safe_stage_failure_before_terminal():
    seen = []
    def fail(_):
        raise RuntimeError('SECRET PAN 4111111111111111')
    result = AMLAgent(mode='demo', index_factory=fail, event_sink=seen.append).run('INC-000123')
    assert result['status'] == 'error'
    assert seen[-2]['stage'] == 'retrieval' and seen[-2]['code'] == 'INTERNAL_ERROR'
    assert seen[-1]['stage'] == 'complete'
    assert result['correlation_id'] == seen[-1]['correlation_id']
    assert 'SECRET' not in json.dumps(seen) and '4111111111111111' not in json.dumps(seen)


def test_store_events_are_durable_ordered_idempotent_and_authorized(store):
    job = store.create_job(request(), ACTOR, 'events')
    first = store.append_runtime_event(job['job_id'], 1, observation())
    second = store.append_runtime_event(job['job_id'], 2, observation('input', 'PASSED'))
    assert store.append_runtime_event(job['job_id'], 1, observation()) == first
    reopened = SQLiteStore(store.path)
    records = reopened.list_runtime_events(job['job_id'], ACTOR)
    assert records == [first, second]
    assert instant(job['accepted_at']) <= instant(first['received_at']) <= instant(second['received_at'])
    assert reopened.list_runtime_events(job['job_id'], Principal('supervisor', 'supervisor')) == records
    with pytest.raises(WorkflowError, match='FORBIDDEN'):
        reopened.list_runtime_events(job['job_id'], Principal('other'))
    with pytest.raises(WorkflowError, match='VERSION_CONFLICT'):
        store.append_runtime_event(job['job_id'], 1, observation('context', 'PASSED'))
    with pytest.raises(WorkflowError, match='VERSION_CONFLICT'):
        store.append_runtime_event(job['job_id'], 4, observation())
    with pytest.raises(WorkflowError, match='INVALID_STATE'):
        store.append_runtime_event(job['job_id'], 3, observation(correlation='run-' + 'b' * 32))


@pytest.mark.parametrize('change', [
    {'payload': 'SECRET PAN 4111111111111111'}, {'correlation_id': '4111111111111111'},
    {'stage': 'raw payload'}, {'code': 'SECRET PAN 4111111111111111'}, {'attempt': True}, {'attempt': 3},
])
def test_untrusted_event_fields_never_enter_database(store, change):
    job = store.create_job(request(), ACTOR, 'unsafe-events')
    with pytest.raises(WorkflowError, match='INVALID_INPUT_FORMAT'):
        store.append_runtime_event(job['job_id'], 1, dict(observation(), **change))
    assert store.list_runtime_events(job['job_id'], ACTOR) == []


@pytest.mark.parametrize('sequence', [0, -1, True, '1', MAX_RUNTIME_EVENTS + 1])
def test_store_bounds_sequence_and_total_event_count(store, sequence):
    job = store.create_job(request(), ACTOR, 'bounded-events')
    with pytest.raises(WorkflowError, match='INVALID_INPUT_FORMAT'):
        store.append_runtime_event(job['job_id'], sequence, observation())
    assert store.list_runtime_events(job['job_id'], ACTOR) == []


def test_corrupt_stored_event_is_not_exposed(store):
    job = store.create_job(request(), ACTOR, 'damaged-events')
    store.append_runtime_event(job['job_id'], 1, observation())
    with store.transaction() as connection:
        connection.execute("UPDATE runtime_events SET event_code='SECRET PAN 4111111111111111'")
    with pytest.raises(WorkflowError, match='INTERNAL_ERROR') as caught:
        store.list_runtime_events(job['job_id'], ACTOR)
    assert 'SECRET' not in str(caught.value)


def test_worker_streams_before_result_and_preserves_prefix_on_timeout(event_worker):
    events = []
    result = event_worker.execute('INC-000123', timeout_seconds=10,
                                  event_sink=lambda sequence, event: events.append((sequence, event)))
    assert result.status == 'result'
    assert [sequence for sequence, _ in events] == [1, 2]
    assert [event for _, event in events] == [row['event'] for row in result.events]
    events.clear()
    timeout = event_worker.execute('INC-000001', timeout_seconds=.3,
                                   event_sink=lambda sequence, event: events.append((sequence, event)))
    assert timeout.status == 'timed_out' and event_worker.process_id is None
    assert len(events) == len(timeout.events) == 1
    assert events[0][1]['stage'] == 'input'


@pytest.mark.parametrize('incident', ['INC-000002', 'INC-000003', 'INC-000004'])
def test_stream_failure_and_overflow_keep_safe_prefix_only(event_worker, incident):
    result = event_worker.execute(incident, timeout_seconds=10)
    assert result.status == 'failed' and result.result is None and event_worker.process_id is None
    assert 1 <= len(result.events) <= MAX_RUNTIME_EVENTS
    assert 'SECRET' not in repr(result) and '4111111111111111' not in repr(result)


def test_parent_rejects_unvalidated_child_event(monkeypatch):
    monkeypatch.setattr('src.workflow.worker._child_main', bad_event_child)
    with ManagedWorker(bootstrap=boot, handler=streaming_handler) as worker:
        assert worker.start(timeout_seconds=15)['ready']
        result = worker.execute('INC-000123', timeout_seconds=10)
        assert result.status == 'failed' and result.events == () and worker.process_id is None


def test_controller_flushes_received_events_before_success_and_retains_after_restart(store):
    with ManagedWorker(bootstrap=boot, handler=streaming_handler) as worker:
        with WorkflowService(store, worker=worker) as service:
            service.prepare(timeout_seconds=15)
            until(lambda: service.readiness()['ready'], timeout=20)
            job = service.submit(request(), ACTOR, 'streamed-job', timeout_seconds=5)
            final = until(lambda: (row if (row := store.get_job(job['job_id'], ACTOR))['status'] not in ('queued', 'running') else None))
            assert final['status'] == 'refused'
            events = store.list_runtime_events(job['job_id'], ACTOR)
            assert len(events) == 2 and events[-1]['event']['stage'] == 'complete'
    assert SQLiteStore(store.path).list_runtime_events(job['job_id'], ACTOR) == events


def test_controller_stream_is_durable_before_timeout(store):
    with ManagedWorker(bootstrap=boot, handler=streaming_handler) as worker:
        with WorkflowService(store, worker=worker) as service:
            service.prepare(timeout_seconds=15)
            until(lambda: service.readiness()['ready'], timeout=20)
            job = service.submit(request('INC-000001'), ACTOR, 'stream-timeout', timeout_seconds=2)
            events = until(lambda: store.list_runtime_events(job['job_id'], ACTOR))
            assert store.get_job(job['job_id'], ACTOR)['status'] == 'running'
            final = until(lambda: (row if (row := store.get_job(job['job_id'], ACTOR))['status'] not in ('queued', 'running') else None))
            assert final['status'] == 'timed_out'
            until(lambda: service.readiness()['state'] != 'busy')
            assert store.list_runtime_events(job['job_id'], ACTOR) == events


def test_transient_event_storage_failure_is_retried_without_losing_sequence(store, monkeypatch):
    attempts = []
    original = store.append_runtime_event
    def transient(job_id, sequence, event):
        attempts.append(sequence)
        if len(attempts) == 1:
            raise WorkflowError('NOT_READY')
        return original(job_id, sequence, event)
    monkeypatch.setattr(store, 'append_runtime_event', transient)
    with ManagedWorker(bootstrap=boot, handler=streaming_handler) as worker:
        with WorkflowService(store, worker=worker) as service:
            service.prepare(timeout_seconds=15)
            until(lambda: service.readiness()['ready'], timeout=20)
            job = service.submit(request(), ACTOR, 'stream-retry', timeout_seconds=5)
            final = until(lambda: (row if (row := store.get_job(job['job_id'], ACTOR))['status'] not in ('queued', 'running') else None))
            assert final['status'] == 'refused'
            assert attempts == [1, 1, 2]
            assert [row['sequence'] for row in store.list_runtime_events(job['job_id'], ACTOR)] == [1, 2]
