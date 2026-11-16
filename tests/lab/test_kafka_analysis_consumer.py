"""Durable intake ordering, ambiguous admission and bounded AML scheduling."""
from collections import deque
from copy import deepcopy
from dataclasses import replace
import base64
from io import BytesIO
import json
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from src.lab.database import LabDatabaseError
from src.lab.kafka.analysis_contracts import AnalysisEvent, DLQ_TOPIC, TOPIC
from src.lab.kafka.analysis_consumer import (
    MAX_EVENT_BYTES, WorkerFailure, process_one, run_worker, scheduler_tick,
)
from src.lab.kafka.contracts import Record
from src.lab.kafka.store import EventConflict, UnknownIncident
from src.workflow.auth import Principal
from src.workflow.remote import WorkflowClient
from src.workflow.store import SQLiteStore, WorkflowError, WORKFLOW_ROOT, utc_now
from tests.evaluator.fixtures import bundle


EVENT_ID = '5884e14d-67f3-4f29-8243-a2666203e2ad'
SECOND_ID = '5884e14d-67f3-4f29-8243-a2666203e2ae'
SECRET = 'private-password-and-token'
ACTOR = Principal('test-kafka-operator')
PAYLOAD = {
    'event_id': EVENT_ID, 'occurred_at': '2026-10-04T12:00:00Z',
    'incident_id': 'INC-000123', 'schema_version': 1,
    'event_type': 'aml.analysis.requested.v1', 'synthetic': True, 'engine': 'template',
    'window': {'start_inclusive': '2026-07-01T00:00:00Z',
               'end_exclusive': '2026-07-20T00:00:00Z', 'as_of': '2026-07-20T00:00:00Z'},
}


def event(**changes):
    return AnalysisEvent.model_validate({**PAYLOAD, **changes})


def record(**changes):
    return replace(Record(TOPIC, 0, 7, b'INC-000123', event().canonical_bytes()), **changes)


class Queue:
    """Committed queue state survives a new worker/client wrapper in tests."""
    def __init__(self, trace=None, failure=None):
        self.trace = trace if trace is not None else []
        self.rows = {}
        self.failure = failure
        self.clock = 0

    def _touch(self, row):
        self.clock += 1
        row['order'] = self.clock

    def enqueue(self, item, source):
        self.trace.append('db.begin')
        if self.failure:
            self.trace.append('db.rollback')
            raise self.failure
        identifier = str(item.event_id)
        previous = self.rows.get(identifier)
        if previous and previous['event_json'] != item.model_dump(mode='json'):
            self.trace.append('db.rollback')
            raise EventConflict()
        if not previous:
            row = {'event_id': identifier, 'event_json': item.model_dump(mode='json'),
                   'status': 'queued', 'job_id': None, 'job_json': None, 'last_error_code': None}
            self._touch(row)
            self.rows[identifier] = row
        self.trace.append('db.commit')
        return {'status': 'duplicate' if previous else 'queued', 'event_id': identifier}

    def _work(self, status, limit):
        return deepcopy(sorted((row for row in self.rows.values() if row['status'] == status),
                               key=lambda row: row['order'])[:limit])

    def pending(self, limit=1):
        return self._work('queued', limit)

    def submitted(self, limit=1):
        return self._work('submitted', limit)

    def record_job(self, identifier, job):
        row = self.rows[identifier]
        row.update(job_id=job['job_id'], job_json=deepcopy(job), last_error_code=None,
                   status='submitted' if job['status'] in ('queued', 'running') else job['status'])
        self._touch(row)
        self.trace.append('db.job.commit')
        return deepcopy(row)

    def defer(self, identifier, code):
        row = self.rows[identifier]
        row['last_error_code'] = code
        self._touch(row)
        return deepcopy(row)

    def reject(self, identifier, code):
        row = self.rows[identifier]
        assert row['status'] == 'queued'
        row.update(status='rejected', last_error_code=code)
        self._touch(row)
        return deepcopy(row)


class Broker:
    def __init__(self, trace=None, records=(), **failures):
        self.trace = trace if trace is not None else []
        self.records, self.failures = deque(records), failures
        self.published, self.committed = [], []
        self.closed, self.polls = False, 0

    def publish(self, topic, *, key, value):
        self.trace.append('dlq.publish')
        if self.failures.get('publish'):
            raise self.failures['publish']
        self.published.append((topic, key, value))
        self.trace.append('dlq.ack')

    def commit(self, source):
        self.trace.append('offset.attempt')
        if self.failures.get('commit'):
            raise self.failures['commit']
        self.committed.append(source)
        self.trace.append('offset.committed')

    def poll(self, timeout):
        assert timeout == 1
        self.polls += 1
        if self.failures.get('poll'):
            raise self.failures['poll']
        return self.records.popleft() if self.records else None

    def close(self):
        self.closed = True
        if self.failures.get('close'):
            raise self.failures['close']


class Client:
    def __init__(self, trace=None, *, failure=None, lost_ack=False):
        self.trace = trace if trace is not None else []
        self.failure, self.lost_ack = failure, lost_ack
        self.calls, self.jobs = [], {}

    def submit(self, request, principal, key, *, engine):
        self.calls.append(('submit', request, principal, key, engine))
        self.trace.append('http.submit')
        if self.failure:
            raise self.failure
        job = self.jobs.setdefault(key, {'job_id': 'job-one', 'status': 'queued', 'revision': 0})
        if self.lost_ack:
            self.lost_ack = False
            raise WorkflowError('NOT_READY')
        return deepcopy(job)

    def get_job(self, identifier):
        self.calls.append(('get', identifier))
        self.trace.append('http.get')
        if self.failure:
            raise self.failure
        return deepcopy(next(job for job in self.jobs.values() if job['job_id'] == identifier))


def test_intake_commits_durable_queue_before_offset_without_http():
    trace = []
    queue, broker = Queue(trace), Broker(trace)
    assert process_one(record(), queue, broker)['status'] == 'queued'
    assert trace == ['db.begin', 'db.commit', 'offset.attempt', 'offset.committed']
    assert queue.rows[EVENT_ID]['job_id'] is None
    assert process_one(record(offset=8), queue, broker)['status'] == 'duplicate'
    assert len(queue.rows) == 1


@pytest.mark.parametrize(('source', 'reason'), [
    (record(value=None), 'TOMBSTONE'),
    (record(value=b'x' * (MAX_EVENT_BYTES + 1)), 'EVENT_TOO_LARGE'),
    (record(value=b'{'), 'INVALID_EVENT'),
    (record(value=b'\xff'), 'INVALID_EVENT'),
    (record(value=json.dumps({**PAYLOAD, 'synthetic': False}).encode()), 'INVALID_EVENT'),
    (record(value=json.dumps({**PAYLOAD, 'event_type': 'incident.review.requested.v1'}).encode()), 'INVALID_EVENT'),
    (record(value=json.dumps({**PAYLOAD, 'engine': 'remote-provider'}).encode()), 'INVALID_EVENT'),
    (record(value=json.dumps({**PAYLOAD, 'window': {}}).encode()), 'INVALID_EVENT'),
    (record(key=None), 'INVALID_KEY'),
    (record(key=b'x' * 1025), 'INVALID_KEY'),
    (record(key=b'INC-000124'), 'INVALID_KEY'),
])
def test_invalid_event_dlq_ack_precedes_input_commit(source, reason):
    trace = []
    queue, broker = Queue(trace), Broker(trace)
    result = process_one(source, queue, broker)
    assert result['reason_code'] == reason
    assert queue.rows == {}
    assert trace == ['dlq.publish', 'dlq.ack', 'offset.attempt', 'offset.committed']
    topic, key, encoded = broker.published[0]
    assert topic == DLQ_TOPIC and key == f'{TOPIC}:0:7'.encode()
    assert json.loads(encoded)['source'] == {'topic': TOPIC, 'partition': 0, 'offset': 7}


def test_dlq_is_bounded_even_for_huge_key_and_value():
    broker = Broker()
    source = record(value=b'x' * 200000, key=b'y' * 200000)
    process_one(source, Queue(), broker)
    encoded = broker.published[0][2]
    envelope = json.loads(encoded)
    assert len(encoded) < MAX_EVENT_BYTES
    assert len(base64.b64decode(envelope['value_base64'])) == 32768
    assert len(base64.b64decode(envelope['key_base64'])) == 256
    assert envelope['key_truncated'] and envelope['value_truncated']


@pytest.mark.parametrize(('error', 'code'), [
    (LabDatabaseError('INVALID_STATE'), 'DATABASE_UNAVAILABLE'),
    (RuntimeError(SECRET), 'DATABASE_PROCESS_FAILED'),
])
def test_database_failure_stops_before_later_offset_and_never_uses_http(error, code):
    queue, broker, client = Queue(failure=error), Broker(records=[record(), record(offset=8)]), Client()
    messages = []
    assert run_worker(queue, broker, client, emit=messages.append) == 1
    assert broker.closed and broker.polls == 1 and not broker.committed and not client.calls
    assert messages[-1] == {'status': 'failed', 'code': code}
    assert SECRET not in json.dumps(messages)


def test_uuid_changed_payload_is_dead_lettered_without_replacing_queued_request():
    queue, broker = Queue(), Broker()
    process_one(record(), queue, broker)
    changed = record(value=event(engine='local-model').canonical_bytes(), offset=8)
    assert process_one(changed, queue, broker)['reason_code'] == 'EVENT_CONFLICT'
    assert queue.rows[EVENT_ID]['event_json']['engine'] == 'template'


def test_unknown_incident_is_dead_lettered():
    queue, broker = Queue(failure=UnknownIncident()), Broker()
    assert process_one(record(), queue, broker)['reason_code'] == 'UNKNOWN_INCIDENT'


def test_dlq_failure_does_not_commit_input():
    broker = Broker(publish=RuntimeError(SECRET))
    with pytest.raises(WorkerFailure, match='^DLQ_PUBLISH_FAILED$'):
        process_one(record(value=b'{'), Queue(), broker)
    assert not broker.committed


def test_foreign_topic_fails_without_side_effects():
    trace = []
    with pytest.raises(WorkerFailure, match='^UNEXPECTED_TOPIC$'):
        process_one(record(topic=DLQ_TOPIC), Queue(trace), Broker(trace))
    assert trace == []


def test_offset_failure_leaves_committed_work_replayable_after_restart():
    queue, failed, client = Queue(), Broker(records=[record(), record(offset=8)], commit=RuntimeError(SECRET)), Client()
    assert run_worker(queue, failed, client, emit=lambda _: None) == 1
    assert not client.calls and queue.rows[EVENT_ID]['status'] == 'queued' and failed.polls == 1
    restarted = Broker(records=[record()])
    assert run_worker(queue, restarted, client, stop=lambda: restarted.polls == 1, emit=lambda _: None) == 0
    assert restarted.committed == [record()] and len(queue.rows) == len(client.jobs) == 1


def test_queued_work_is_submitted_when_kafka_is_idle_without_prepare():
    trace = []
    queue, broker, client = Queue(trace), Broker(trace), Client(trace)
    process_one(record(), queue, broker)
    trace.clear()
    assert run_worker(queue, broker, client, principal=ACTOR, stop=lambda: broker.polls == 1,
                      emit=lambda _: None) == 0
    assert trace == ['http.submit', 'db.job.commit']
    call = client.calls[0]
    assert call[2:] == (ACTOR, 'kafka-aml:' + EVENT_ID, 'template')
    assert call[1] == event().job_request()
    assert queue.rows[EVENT_ID]['status'] == 'submitted'


@pytest.mark.parametrize('code', ['NOT_READY', 'BUSY', 'DEPENDENCY_UNAVAILABLE', 'INTERNAL_ERROR'])
def test_temporary_workflow_failure_keeps_queue_and_rotates_fairly(code):
    queue, client = Queue(), Client(failure=WorkflowError(code))
    queue.enqueue(event(), record())
    queue.enqueue(event(event_id=SECOND_ID), record(offset=8))
    assert scheduler_tick(queue, client, ACTOR) == [{'status': 'deferred', 'event_id': EVENT_ID, 'code': code}]
    assert queue.pending()[0]['event_id'] == SECOND_ID
    assert queue.rows[EVENT_ID]['status'] == 'queued'


def test_lost_http_ack_retries_identical_uuid_and_never_creates_second_job():
    queue, client = Queue(), Client(lost_ack=True)
    queue.enqueue(event(), record())
    scheduler_tick(queue, client, ACTOR)
    assert queue.rows[EVENT_ID]['status'] == 'queued' and len(client.jobs) == 1
    scheduler_tick(queue, client, ACTOR)
    assert queue.rows[EVENT_ID]['status'] == 'submitted' and len(client.jobs) == 1
    assert client.calls[0] == client.calls[1]
    client.jobs[event().idempotency_key].update(status='succeeded', revision=2)
    scheduler_tick(queue, client, ACTOR)
    assert queue.rows[EVENT_ID]['status'] == 'succeeded'
    count = len(client.calls)
    assert scheduler_tick(queue, client, ACTOR) == [] and len(client.calls) == count


class SQLiteTransport:
    """Controlled HTTP boundary; admission/replay/report persistence use real SQLite."""
    def __init__(self, path, *, lost_ack=False):
        self.path, self.lost_ack = path, lost_ack
        self.requests = []

    def open_budgeted(self, request, *, timeout, deadline):
        # Explicit bounded capability for this no-network SQLite test double.
        deadline.check()
        response = self.open(request, timeout=timeout)
        deadline.check()
        return response

    def open(self, request, *, timeout):
        assert timeout <= 10
        self.requests.append((request.method, request.full_url, request.data,
                              request.get_header('Idempotency-key')))
        assert request.get_header('X-lab-key') == 'a' * 64
        # Reopen the file at every call to avoid depending on process memory.
        store = SQLiteStore(self.path)
        path = urlsplit(request.full_url).path
        if request.method == 'POST':
            assert path == '/v1/jobs'
            assert urlsplit(request.full_url).query == 'demo=true&engine=template'
            payload = store.create_job(json.loads(request.data), ACTOR,
                                       request.get_header('Idempotency-key'), engine='template')
            status = 202
            if self.lost_ack:
                self.lost_ack = False
                raise TimeoutError(SECRET)
        else:
            assert request.method == 'GET' and path.startswith('/v1/jobs/')
            payload = store.get_job(path.rsplit('/', 1)[1], ACTOR)
            status = 200
        response = BytesIO(json.dumps(payload).encode('utf-8'))
        response.status, response.headers = status, {}
        return response


@pytest.mark.parametrize('failure_boundary', ['http_ack', 'projection_commit'])
def test_real_sqlite_admission_survives_ambiguous_ack_and_projects_without_new_kafka(failure_boundary):
    """Queue/HTTP are controlled; durable jobs/replays/report refs are production SQLite."""
    class ProjectionQueue(Queue):
        fail_once = failure_boundary == 'projection_commit'

        def record_job(self, identifier, job):
            if self.fail_once:
                self.fail_once = False
                raise LabDatabaseError('INVALID_STATE')
            return super().record_job(identifier, job)

    path = WORKFLOW_ROOT / 'kafka-analysis-tests' / uuid4().hex / 'workflow.sqlite3'
    queue, initial_broker, messages = ProjectionQueue(), Broker(records=[record()]), []
    first_transport = SQLiteTransport(path, lost_ack=failure_boundary == 'http_ack')
    client = WorkflowClient({'api_token': 'a' * 64}, transport=first_transport)
    result = run_worker(queue, initial_broker, client, principal=ACTOR,
                        stop=lambda: initial_broker.polls == 1, emit=messages.append)
    assert result == (0 if failure_boundary == 'http_ack' else 1)
    assert initial_broker.committed == [record()] and initial_broker.closed
    assert queue.rows[EVENT_ID]['status'] == 'queued' and queue.rows[EVENT_ID]['job_id'] is None
    assert SECRET not in json.dumps(messages)

    store = SQLiteStore(path)
    accepted, = store.list_jobs(ACTOR)
    assert accepted['status'] == 'queued'
    running = store.transition_job(accepted['job_id'], 0, dict(accepted, revision=1, status='running'))
    report = bundle(1)['outcomes'][0]['result']
    report.update(incident_id=event().incident_id, report_id='report-' + uuid4().hex,
                  generated_at=utc_now(), confidence=None)
    terminal = store.transition_job(accepted['job_id'], 1, dict(running, revision=2,
        status='succeeded', completed_at=utc_now(), result=report))

    # Restart the worker/HTTP adapter, with no new Kafka events. An idempotent
    # replay returns the original accepted job even though it has completed.
    next_transport = SQLiteTransport(path)
    restarted_client = WorkflowClient({'api_token': 'a' * 64}, transport=next_transport)
    for expected in ('submitted', 'succeeded'):
        idle_broker = Broker()
        assert run_worker(queue, idle_broker, restarted_client, principal=ACTOR,
                          stop=lambda: idle_broker.polls == 1, emit=messages.append) == 0
        assert idle_broker.committed == idle_broker.published == [] and idle_broker.closed
        assert queue.rows[EVENT_ID]['status'] == expected
    assert first_transport.requests[0] == next_transport.requests[0]
    assert [request[0] for request in next_transport.requests] == ['POST', 'GET']
    assert queue.rows[EVENT_ID]['job_json'] == terminal
    reopened = SQLiteStore(path)
    assert reopened.list_jobs(ACTOR) == [terminal]
    assert reopened.get_report(report['report_id'], 1, ACTOR) == report
    assert reopened.report_ref(report['report_id'], 1, ACTOR).report_id == report['report_id']
    with reopened.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM reports').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM decisions').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM delivery_export_locks').fetchone()[0] == 0
    assert scheduler_tick(queue, restarted_client, ACTOR) == []
    assert len(next_transport.requests) == 2


@pytest.mark.parametrize('status', ['failed', 'refused', 'cancelled', 'timed_out'])
def test_terminal_job_outcomes_are_projected_without_regeneration(status):
    queue, client = Queue(), Client()
    queue.enqueue(event(), record())
    scheduler_tick(queue, client, ACTOR)
    client.jobs[event().idempotency_key].update(status=status, revision=2)
    scheduler_tick(queue, client, ACTOR)
    assert queue.rows[EVENT_ID]['status'] == status
    assert scheduler_tick(queue, client, ACTOR) == []
    assert [call[0] for call in client.calls] == ['submit', 'get']


@pytest.mark.parametrize('code', ['INVALID_INPUT_FORMAT', 'IDEMPOTENCY_CONFLICT'])
def test_permanent_admission_error_is_recorded_as_rejected(code):
    queue, client = Queue(), Client(failure=WorkflowError(code))
    queue.enqueue(event(), record())
    assert scheduler_tick(queue, client, ACTOR)[0]['status'] == 'rejected'
    assert queue.rows[EVENT_ID]['status'] == 'rejected'
    assert scheduler_tick(queue, client, ACTOR) == []


@pytest.mark.parametrize('code', ['UNAUTHORIZED', 'FORBIDDEN'])
@pytest.mark.parametrize('submitted', [False, True])
def test_auth_configuration_error_stops_and_preserves_queue(code, submitted):
    queue, client = Queue(), Client()
    queue.enqueue(event(), record())
    if submitted:
        scheduler_tick(queue, client, ACTOR)
    before = deepcopy(queue.rows[EVENT_ID])
    client.failure = WorkflowError(code)
    with pytest.raises(WorkerFailure, match='^WORKFLOW_' + code + '$'):
        scheduler_tick(queue, client, ACTOR)
    assert queue.rows[EVENT_ID] == before


@pytest.mark.parametrize('code', ['UNAUTHORIZED', 'FORBIDDEN'])
def test_auth_failure_stops_run_without_consuming_following_record(code):
    queue, broker = Queue(), Broker(records=[record(), record(offset=8)])
    messages = []
    assert run_worker(queue, broker, Client(failure=WorkflowError(code)),
                      principal=ACTOR, emit=messages.append) == 1
    assert broker.closed and broker.polls == 1 and broker.committed == [record()]
    assert queue.rows[EVENT_ID]['status'] == 'queued'
    assert messages[-1] == {'status': 'failed', 'code': 'WORKFLOW_' + code}


def test_submitted_job_missing_never_rejects_or_regenerates_an_accepted_request():
    queue, client = Queue(), Client()
    queue.enqueue(event(), record())
    scheduler_tick(queue, client, ACTOR)
    client.failure = WorkflowError('NOT_FOUND')
    with pytest.raises(WorkerFailure, match='^WORKFLOW_JOB_UNAVAILABLE$'):
        scheduler_tick(queue, client, ACTOR)
    assert queue.rows[EVENT_ID]['job_id'] == 'job-one'
    assert queue.rows[EVENT_ID]['status'] == 'submitted'
    assert queue.rows[EVENT_ID]['last_error_code'] == 'NOT_FOUND'


def test_tick_reconciles_then_submits_at_most_two_requests():
    queue, client = Queue(), Client()
    queue.enqueue(event(), record())
    scheduler_tick(queue, client, ACTOR)
    queue.enqueue(event(event_id=SECOND_ID), record(offset=8))
    client.calls.clear()
    scheduler_tick(queue, client, ACTOR)
    assert [call[0] for call in client.calls] == ['get', 'submit']


def test_busy_kafka_intake_does_not_spin_workflow_scheduler():
    queue = Queue()
    broker = Broker(records=[record(offset=number) for number in range(20)])
    client = Client(failure=WorkflowError('NOT_READY'))
    assert run_worker(queue, broker, client, stop=lambda: not broker.records,
                      emit=lambda _: None, clock=lambda: 10.0) == 0
    assert len(broker.committed) == 20 and len(client.calls) == 1


@pytest.mark.parametrize(('failures', 'code'), [
    ({'poll': RuntimeError(SECRET)}, 'BROKER_POLL_FAILED'),
    ({'close': RuntimeError(SECRET)}, 'BROKER_CLOSE_FAILED'),
])
def test_run_failures_close_broker_and_only_emit_safe_code(failures, code):
    broker, messages = Broker(**failures), []
    assert run_worker(Queue(), broker, Client(), stop=lambda: broker.polls == 1,
                      emit=messages.append) == 1
    assert broker.closed and messages[-1] == {'status': 'failed', 'code': code}
    assert SECRET not in json.dumps(messages)


def test_keyboard_interrupt_closes_broker_without_committing():
    broker = Broker(poll=KeyboardInterrupt())
    assert run_worker(Queue(), broker, Client(), emit=lambda _: None) == 0
    assert broker.closed and broker.committed == []


@pytest.mark.parametrize('failure', [WorkflowError(SECRET), RuntimeError(SECRET)])
def test_unexpected_client_failure_never_leaks_details_or_discards_queue(failure):
    queue, broker, messages = Queue(), Broker(), []
    queue.enqueue(event(), record())
    assert run_worker(queue, broker, Client(failure=failure), emit=messages.append) == 1
    assert queue.rows[EVENT_ID]['status'] == 'queued'
    assert SECRET not in json.dumps(messages)
