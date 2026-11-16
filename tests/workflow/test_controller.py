"""Durable admission/race tests; process enforcement is covered in test_worker."""
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from uuid import uuid4

import pytest

from src.config import get_settings
from src.contracts.v1 import CreateJobRequest
from src.runtime.policy import load_demo_policy
from src.workflow.auth import Principal
from src.workflow.controller import WorkflowService
from src.workflow.store import SQLiteStore, WorkflowError
from src.workflow.worker import ExecutionOutcome

ACTOR = Principal('test-operator')


def request(incident='INC-000123'):
    policy = load_demo_policy(get_settings().project_root / 'config/demo-policy.json')
    return CreateJobRequest(schema_version='1.0', incident_id=incident, window=policy.window)


class FakeWorker:
    def __init__(self, delay=0.01):
        self.is_ready, self.calls, self.delay = False, 0, delay
        self.return_gate = None
    def start(self, timeout_seconds=180):
        self.is_ready = True
        return {'ready': True, 'code': 'READY'}
    def execute(self, incident_id, engine='template', timeout_seconds=180, cancel_event=None):
        self.calls += 1
        if self.return_gate is not None:
            assert self.return_gate.wait(10), 'Test did not release the worker'
        elif cancel_event.wait(self.delay):
            self.is_ready = False
            return ExecutionOutcome('cancelled')
        return ExecutionOutcome('result', result={
            'schema_version': '1.0', 'status': 'refusal', 'incident_id': incident_id,
            'generated_at': datetime.now(timezone.utc).isoformat(), 'data_completeness': 'unassessed',
            'reason_code': 'POLICY_NOT_APPROVED', 'reason': 'Synthetic control', 'recommended_action': 'manual_review'})
    def close(self):
        self.is_ready = False


@pytest.fixture
def setup():
    path = get_settings().project_root / 'var/workflow' / ('test-controller-' + uuid4().hex) / 'state.sqlite3'
    store = SQLiteStore(path)
    worker = FakeWorker()
    with WorkflowService(store, worker=worker) as service:
        yield store, worker, service


def until(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('CONTROL_TIMEOUT')


def ready(service):
    service.prepare(timeout_seconds=2)
    until(lambda: service.readiness()['ready'])


def terminal(store, job_id):
    return until(lambda: (job if (job := store.get_job(job_id, ACTOR))['status'] not in ('queued', 'running') else None))


def test_admission_requires_readiness_and_accepted_request_is_replayed_without_rerun(setup):
    store, worker, service = setup
    with pytest.raises(WorkflowError, match='NOT_READY'):
        service.submit(request(), ACTOR, 'first')
    assert store.list_jobs(ACTOR) == []
    ready(service)
    accepted = service.submit(request(), ACTOR, 'first')
    assert accepted['status'] == 'queued'
    assert terminal(store, accepted['job_id'])['status'] == 'refused'
    until(lambda: service.readiness()['ready'])
    worker.is_ready = False
    assert service.submit(request(), ACTOR, 'first') == accepted
    assert worker.calls == 1


def test_capacity_rejects_new_work_but_allows_idempotent_replay(setup):
    store, worker, service = setup
    worker.delay = .3
    ready(service)
    accepted = service.submit(request(), ACTOR, 'one')
    assert service.submit(request(), ACTOR, 'one') == accepted
    with pytest.raises(WorkflowError, match='NOT_READY'):
        service.submit(request('INC-000250'), ACTOR, 'two')
    with pytest.raises(WorkflowError, match='IDEMPOTENCY_CONFLICT'):
        service.submit(request(), ACTOR, 'one', engine='local-model')
    terminal(store, accepted['job_id'])
    assert len(store.list_jobs(ACTOR)) == 1


def test_timer_persists_timeout_before_ignoring_worker_returns_and_late_result_cannot_replace_it(setup):
    store, worker, service = setup
    worker.return_gate = threading.Event()
    ready(service)
    accepted = service.submit(request(), ACTOR, 'deadline', timeout_seconds=1)
    try:
        until(lambda: worker.calls == 1)
        result = terminal(store, accepted['job_id'])
        assert result['status'] == 'timed_out' and 'result' not in result
        assert service.readiness()['state'] == 'busy'
    finally:
        worker.return_gate.set()
    until(lambda: service.readiness()['state'] != 'busy')
    assert store.get_job(accepted['job_id'], ACTOR) == result


def test_cancel_is_durable_before_worker_stops_and_cross_user_is_forbidden(setup):
    store, worker, service = setup
    worker.delay = 2
    ready(service)
    job = service.submit(request(), ACTOR, 'start')
    with pytest.raises(WorkflowError, match='FORBIDDEN'):
        service.cancel(job['job_id'], Principal('stranger'), 'bad-cancel')
    result = service.cancel(job['job_id'], ACTOR, 'cancel')
    assert result['status'] == 'cancelled'
    assert service.cancel(job['job_id'], ACTOR, 'cancel') == result
    until(lambda: service.readiness()['state'] != 'busy')
    assert store.get_job(job['job_id'], ACTOR) == result


def test_exclusive_controller_lease_prevents_false_recovery_of_live_jobs(setup):
    store, worker, service = setup
    worker.delay = .3
    ready(service)
    job = service.submit(request(), ACTOR, 'alive')
    with pytest.raises(WorkflowError, match='NOT_READY'):
        WorkflowService(SQLiteStore(store.path), worker=FakeWorker())
    assert store.get_job(job['job_id'], ACTOR)['status'] == 'running'
    terminal(store, job['job_id'])


def test_restart_reconciles_orphaned_active_job_and_retains_idempotency(setup):
    store, worker, service = setup
    service.close()
    accepted = store.create_job(request(), ACTOR, 'interrupted')
    with WorkflowService(SQLiteStore(store.path), worker=FakeWorker()) as restarted:
        assert store.get_job(accepted['job_id'], ACTOR)['status'] == 'failed'
        assert restarted.submit(request(), ACTOR, 'interrupted') == accepted


def test_close_cancels_active_job_and_releases_lease(setup):
    store, worker, service = setup
    worker.delay = 2
    ready(service)
    job = service.submit(request(), ACTOR, 'closing')
    service.close()
    assert store.get_job(job['job_id'], ACTOR)['status'] == 'cancelled'
    with WorkflowService(SQLiteStore(store.path), worker=FakeWorker()) as restarted:
        assert restarted.readiness()['state'] == 'not_ready'


def test_cancellation_from_another_store_connection_stops_controller_worker(setup):
    store, worker, service = setup
    worker.delay = 5
    ready(service)
    job = service.submit(request(), ACTOR, 'other-connection')
    other = SQLiteStore(store.path)
    cancelled = other.cancel_job(job['job_id'], ACTOR, idempotency_key='external-cancel')
    until(lambda: service.readiness()['state'] != 'busy', timeout=2)
    assert cancelled['status'] == 'cancelled' and not worker.is_ready
