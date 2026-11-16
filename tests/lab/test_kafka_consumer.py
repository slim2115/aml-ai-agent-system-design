"""Delivery guarantees without a broker or a database server.

These tests assert observable transaction/offset ordering and restart behavior;
actual PostgreSQL idempotency and Kafka acknowledgements need integration checks.
"""
from collections import deque
import base64
from dataclasses import replace
import json

import pytest

from src.lab.database import LabDatabaseError
from src.lab.kafka.consumer import (
    DLQ_KEY_PREVIEW_BYTES, DLQ_VALUE_PREVIEW_BYTES, MAX_EVENT_BYTES,
    WorkerFailure, process_one, run_worker,
)
from src.lab.kafka.contracts import DLQ_TOPIC, TOPIC, Record
from src.lab.kafka.store import EventConflict, UnknownIncident


EVENT_ID = '2287c75b-a9a6-494f-a620-72e87d71a165'
PAYLOAD = {
    'event_id': EVENT_ID,
    'occurred_at': '2026-10-01T09:00:00Z',
    'incident_id': 'INC-000123',
    'schema_version': 1,
    'event_type': 'incident.review.requested.v1',
    'synthetic': True,
}
SECRET = 'postgresql://private-user:private-password@internal-host/private-db'


def record(**changes):
    source = Record(topic=TOPIC, partition=1, offset=7, key=b'INC-000123',
                    value=json.dumps(PAYLOAD).encode('utf-8'))
    return replace(source, **changes)


class Store:
    """A committed result survives consumer restart in the same fake database."""
    def __init__(self, trace, *, failure=None, result=None):
        self.trace = trace
        self.failure = failure
        self.result = result
        self.committed = set()
        self.calls = 0

    def process(self, event, source):
        self.calls += 1
        self.trace.append('database.begin')
        if self.failure is not None:
            self.trace.append('database.rollback')
            raise self.failure
        duplicate = str(event.event_id) in self.committed
        self.committed.add(str(event.event_id))
        self.trace.append('database.commit')
        return self.result if self.result is not None else {
            'status': 'duplicate' if duplicate else 'processed',
        }


class Broker:
    def __init__(self, trace, records=(), *, publish_failure=None,
                 commit_failure=None, poll_failure=None, close_failure=None):
        self.trace = trace
        self.records = deque(records)
        self.publish_failure = publish_failure
        self.commit_failure = commit_failure
        self.poll_failure = poll_failure
        self.close_failure = close_failure
        self.published = []
        self.committed = []
        self.closed = False
        self.polls = 0

    def publish(self, topic, *, key, value):
        self.trace.append('dlq.publish')
        self.published.append((topic, key, value))
        if self.publish_failure is not None:
            raise self.publish_failure
        self.trace.append('dlq.ack')
        return object()

    def commit(self, source):
        self.trace.append('offset.commit_attempt')
        if self.commit_failure is not None:
            raise self.commit_failure
        self.committed.append(source)
        self.trace.append('offset.committed')

    def poll(self, timeout):
        assert timeout == 1
        self.polls += 1
        if self.poll_failure is not None:
            raise self.poll_failure
        return self.records.popleft() if self.records else None

    def close(self):
        self.closed = True
        if self.close_failure is not None:
            raise self.close_failure


def test_database_commit_precedes_offset_commit():
    trace = []
    store, broker = Store(trace), Broker(trace)
    result = process_one(record(), store, broker)
    assert result == {'status': 'processed', 'event_id': EVENT_ID,
                      'topic': TOPIC, 'partition': 1, 'offset': 7}
    assert trace == ['database.begin', 'database.commit',
                     'offset.commit_attempt', 'offset.committed']
    assert len(broker.committed) == 1
    assert broker.published == []


def test_duplicate_delivery_is_committed_after_database_returns():
    trace = []
    store, broker = Store(trace), Broker(trace)
    process_one(record(), store, broker)
    trace.clear()
    result = process_one(record(offset=8), store, broker)
    assert result['status'] == 'duplicate'
    assert len(store.committed) == 1
    assert trace == ['database.begin', 'database.commit',
                     'offset.commit_attempt', 'offset.committed']
    assert [item.offset for item in broker.committed] == [7, 8]


@pytest.mark.parametrize(('failure', 'code'), [
    (LabDatabaseError('UNAVAILABLE'), 'DATABASE_UNAVAILABLE'),
    (RuntimeError(SECRET), 'DATABASE_PROCESS_FAILED'),
])
def test_database_failure_never_advances_offset_or_sends_to_dlq(failure, code):
    trace = []
    store, broker = Store(trace, failure=failure), Broker(trace)
    with pytest.raises(WorkerFailure, match=f'^{code}$'):
        process_one(record(), store, broker)
    assert trace == ['database.begin', 'database.rollback']
    assert broker.committed == broker.published == []


@pytest.mark.parametrize(('source', 'reason'), [
    (record(value=None), 'TOMBSTONE'),
    (record(value=b'x' * (MAX_EVENT_BYTES + 1)), 'EVENT_TOO_LARGE'),
    (record(value=b'{'), 'INVALID_EVENT'),
    (record(value=b'\xff'), 'INVALID_EVENT'),
    (record(value=b'{}'), 'INVALID_EVENT'),
    (record(value=json.dumps({**PAYLOAD, 'synthetic': False}).encode()), 'INVALID_EVENT'),
    (record(key=None), 'INVALID_KEY'),
    (record(key=b'INC-999999'), 'INVALID_KEY'),
])
def test_bad_messages_are_acknowledged_in_dlq_before_input_commit(source, reason):
    trace = []
    store, broker = Store(trace), Broker(trace)
    result = process_one(source, store, broker)
    assert result['status'] == 'dead_lettered'
    assert result['reason_code'] == reason
    assert store.calls == 0
    assert trace == ['dlq.publish', 'dlq.ack', 'offset.commit_attempt', 'offset.committed']
    topic, key, value = broker.published[0]
    envelope = json.loads(value)
    assert topic == DLQ_TOPIC
    assert key == f'{TOPIC}:1:7'.encode()
    assert envelope['source'] == {'topic': TOPIC, 'partition': 1, 'offset': 7}
    assert envelope['reason_code'] == reason
    assert envelope['value_is_null'] is (source.value is None)
    assert envelope['key_is_null'] is (source.key is None)


def test_dlq_diagnostic_copy_is_bounded_for_oversized_message():
    trace = []
    source = record(value=b'x' * (MAX_EVENT_BYTES * 2), key=b'y' * 1000)
    broker = Broker(trace)
    process_one(source, Store(trace), broker)
    envelope = json.loads(broker.published[0][2])
    assert len(base64.b64decode(envelope['value_base64'])) == DLQ_VALUE_PREVIEW_BYTES
    assert len(base64.b64decode(envelope['key_base64'])) == DLQ_KEY_PREVIEW_BYTES
    assert envelope['value_truncated'] is envelope['key_truncated'] is True
    assert envelope['value_bytes'] == len(source.value)
    assert envelope['key_bytes'] == len(source.key)
    assert len(broker.published[0][2]) < MAX_EVENT_BYTES


@pytest.mark.parametrize(('failure', 'reason'), [
    (EventConflict(), 'EVENT_CONFLICT'),
    (UnknownIncident(), 'UNKNOWN_INCIDENT'),
])
def test_permanent_domain_failure_is_dead_lettered_after_rollback(failure, reason):
    trace = []
    broker = Broker(trace)
    result = process_one(record(), Store(trace, failure=failure), broker)
    assert result['reason_code'] == reason
    assert trace == ['database.begin', 'database.rollback', 'dlq.publish', 'dlq.ack',
                     'offset.commit_attempt', 'offset.committed']


def test_dlq_publish_failure_leaves_input_offset_uncommitted():
    trace = []
    broker = Broker(trace, publish_failure=RuntimeError(SECRET))
    with pytest.raises(WorkerFailure, match='^DLQ_PUBLISH_FAILED$'):
        process_one(record(value=b'{}'), Store(trace), broker)
    assert trace == ['dlq.publish']
    assert broker.committed == []


def test_unexpected_topic_is_a_fatal_failure_without_side_effects():
    trace = []
    broker = Broker(trace)
    store = Store(trace)
    with pytest.raises(WorkerFailure, match='^UNEXPECTED_TOPIC$'):
        process_one(record(topic=DLQ_TOPIC), store, broker)
    assert trace == []
    assert store.calls == 0


@pytest.mark.parametrize('result', [{}, {'status': 'failed'}, {'status': 'pending'}])
def test_invalid_database_result_never_commits_input(result):
    trace = []
    broker = Broker(trace)
    with pytest.raises(WorkerFailure, match='^INVALID_PROCESS_RESULT$'):
        process_one(record(), Store(trace, result=result), broker)
    assert broker.committed == broker.published == []


def test_commit_failure_stops_worker_and_restart_can_redeliver():
    trace = []
    store = Store(trace)
    failed = Broker(trace, [record(), record(offset=8)], commit_failure=RuntimeError(SECRET))
    messages = []
    assert run_worker(store, failed, emit=messages.append) == 1
    assert failed.closed
    assert failed.polls == 1
    assert failed.committed == []
    assert len(failed.records) == 1  # Never advance to a later offset after failure.
    assert messages[-1] == {'status': 'failed', 'code': 'OFFSET_COMMIT_FAILED'}
    restarted = Broker(trace, [record()])
    assert run_worker(store, restarted, stop=lambda: not restarted.records,
                      emit=messages.append) == 0
    assert messages[-2]['status'] == 'duplicate'
    assert len(store.committed) == 1
    assert restarted.committed == [record()]
    assert restarted.closed
    assert SECRET not in json.dumps(messages)


def test_database_outage_stops_worker_before_next_record():
    trace = []
    broker = Broker(trace, [record(), record(offset=8)])
    store = Store(trace, failure=LabDatabaseError('UNAVAILABLE'))
    messages = []
    assert run_worker(store, broker, emit=messages.append) == 1
    assert broker.polls == 1
    assert broker.closed
    assert broker.committed == broker.published == []
    assert messages[-1] == {'status': 'failed', 'code': 'DATABASE_UNAVAILABLE'}


def test_commit_failure_after_dlq_ack_may_redeliver_same_dlq_key():
    trace = []
    source = record(value=b'{}')
    failed = Broker(trace, [source], commit_failure=RuntimeError(SECRET))
    assert run_worker(Store(trace), failed, emit=lambda message: None) == 1
    assert failed.committed == []
    restarted = Broker(trace)
    process_one(source, Store(trace), restarted)
    assert restarted.published[0] == failed.published[0]
    assert restarted.committed == [source]


def test_poll_failure_closes_broker_and_reports_safe_code(capsys):
    trace = []
    broker = Broker(trace, poll_failure=RuntimeError(SECRET))
    assert run_worker(Store(trace), broker) == 1
    assert broker.closed
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert messages[-1] == {'status': 'failed', 'code': 'BROKER_POLL_FAILED'}
    assert SECRET not in json.dumps(messages)


def test_idle_poll_then_clean_stop_never_commits():
    trace = []
    broker = Broker(trace, [None])
    messages = []
    assert run_worker(Store(trace), broker, stop=lambda: not broker.records,
                      emit=messages.append) == 0
    assert broker.polls == 1
    assert broker.closed
    assert broker.committed == []
    assert messages[-1] == {'status': 'stopped'}


def test_keyboard_interrupt_closes_broker_without_committing():
    trace = []
    broker = Broker(trace, poll_failure=KeyboardInterrupt())
    messages = []
    assert run_worker(Store(trace), broker, emit=messages.append) == 0
    assert broker.closed
    assert broker.committed == []
    assert messages[-1] == {'status': 'stopped'}


def test_close_failure_returns_failure_without_claiming_clean_stop():
    trace = []
    broker = Broker(trace, close_failure=RuntimeError(SECRET))
    messages = []
    assert run_worker(Store(trace), broker, stop=lambda: True, emit=messages.append) == 1
    assert messages[-1] == {'status': 'failed', 'code': 'BROKER_CLOSE_FAILED'}
    assert {'status': 'stopped'} not in messages
