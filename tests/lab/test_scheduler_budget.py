"""Shared scheduler deadline, durable retries and pinned driver wait boundary."""
from copy import deepcopy
from io import BytesIO
import inspect
import json
from types import SimpleNamespace

import psycopg
import pytest

from src.lab import database
from src.lab.kafka.analysis_consumer import run_worker, scheduler_tick
from src.runtime.io_budget import BudgetExpired, Deadline, current_budget, use_budget
from src.workflow.remote import WorkflowClient
from src.workflow.store import WorkflowError
from tests.lab.test_kafka_analysis_consumer import (
    ACTOR, EVENT_ID, SECOND_ID, Broker, Client, Queue, event, record,
)
from tests.lab.test_qa_database_profile import settings


class Clock:
    value = 0.0

    def __call__(self):
        return self.value

    def spend(self, seconds):
        self.value += seconds

    def io(self, seconds):
        """A cooperative fake of an adapter bounded by the shared remainder."""
        self.spend(min(seconds, current_budget().remaining()))
        current_budget().check()


@pytest.mark.parametrize('seconds', [0, -1, True, None, float('nan'), float('inf')])
def test_invalid_budget_rejected(seconds):
    with pytest.raises(ValueError, match='INVALID_IO_BUDGET'):
        Deadline.after(seconds)


def test_budget_is_monotonic_and_context_restores_after_failure():
    clock = Clock()
    budget = Deadline.after(10, clock=clock)
    with pytest.raises(BudgetExpired), use_budget(budget):
        assert current_budget() is budget
        clock.spend(7)
        assert budget.remaining() == 3
        clock.spend(3)
        budget.check()
    assert current_budget() is None


def test_two_stages_share_one_budget_and_keep_ambiguous_admission_key():
    clock, observed = Clock(), []

    class TimedQueue(Queue):
        def submitted(self, limit=1):
            observed.append(('submitted', current_budget().remaining()))
            clock.io(1)
            return super().submitted(limit)

        def pending(self, limit=1):
            observed.append(('pending', current_budget().remaining()))
            clock.io(1)
            return super().pending(limit)

        def record_job(self, identifier, job):
            observed.append(('projection', current_budget().remaining()))
            clock.io(1)
            return super().record_job(identifier, job)

    queue, client = TimedQueue(), Client()
    queue.enqueue(event(), record())
    first = client.submit(event().job_request(), ACTOR, event().idempotency_key, engine='template')
    Queue.record_job(queue, EVENT_ID, first)
    queue.enqueue(event(event_id=SECOND_ID), record(offset=8))
    client.calls.clear()
    normal_get, normal_submit = client.get_job, client.submit

    def get(identifier):
        observed.append(('get', current_budget().remaining()))
        clock.io(3)
        return normal_get(identifier)

    def submit(*args, **kwargs):
        # Remote accepts before the acknowledgement exhausts the shared budget.
        observed.append(('submit', current_budget().remaining()))
        result = normal_submit(*args, **kwargs)
        clock.io(4)
        return result

    client.get_job, client.submit = get, submit
    result = scheduler_tick(queue, client, ACTOR, clock=clock)
    assert clock.value == 10
    assert observed == [('submitted', 10), ('get', 9), ('projection', 6),
                        ('pending', 5), ('submit', 4)]
    assert result == [{'status': 'projected', 'event_id': EVENT_ID},
                      {'status': 'budget_exhausted', 'code': 'SCHEDULER_BUDGET_EXHAUSTED'}]
    assert queue.rows[SECOND_ID]['status'] == 'queued'
    assert queue.rows[SECOND_ID]['job_id'] is None
    assert queue.rows[SECOND_ID]['last_error_code'] is None
    key = event(event_id=SECOND_ID).idempotency_key
    assert len(client.jobs) == 2 and client.calls[-1][3] == key
    client.get_job, client.submit = normal_get, normal_submit
    scheduler_tick(queue, client, ACTOR, clock=clock)
    assert queue.rows[SECOND_ID]['status'] == 'submitted'
    assert len(client.jobs) == 2 and client.calls[-1][3] == key
    assert current_budget() is None


def test_no_next_stage_or_defer_after_expiry_before_projection():
    clock = Clock()

    class TimedClient(Client):
        def submit(self, *args, **kwargs):
            result = super().submit(*args, **kwargs)
            clock.spend(10)
            return result

    queue, client = Queue(), TimedClient()
    queue.enqueue(event(), record())
    before = deepcopy(queue.rows)
    result = scheduler_tick(queue, client, ACTOR, clock=clock)
    assert result[-1]['status'] == 'budget_exhausted'
    assert queue.rows == before and len(client.jobs) == 1
    assert 'db.job.commit' not in queue.trace


def test_budget_expiry_before_first_queue_read_performs_no_io():
    values = iter([0, 10])
    result = scheduler_tick(Queue(), Client(), ACTOR, clock=lambda: next(values))
    assert result == [{'status': 'budget_exhausted', 'code': 'SCHEDULER_BUDGET_EXHAUSTED'}]


@pytest.mark.parametrize('submitted', [False, True])
def test_protocol_error_stops_worker_once_and_retains_queued_or_submitted_identity(submitted):
    queue, client = Queue(), Client()
    queue.enqueue(event(), record())
    if submitted:
        scheduler_tick(queue, client, ACTOR)
    before = deepcopy(queue.rows[EVENT_ID])
    client.failure = WorkflowError('PROTOCOL_ERROR')
    client.calls.clear()
    broker, messages = Broker(), []
    assert run_worker(queue, broker, client, principal=ACTOR, emit=messages.append) == 1
    assert broker.polls == 1 and broker.closed
    assert len(client.calls) == 1
    assert messages[-1] == {'status': 'failed', 'code': 'WORKFLOW_PROTOCOL_ERROR'}
    after = queue.rows[EVENT_ID]
    assert (after['status'], after['job_id'], after['event_json']) == (
        before['status'], before['job_id'], before['event_json'])
    assert after['last_error_code'] == 'PROTOCOL_ERROR'
    # A deliberate restart after repair reconciles the original job/key.
    client.failure = None
    scheduler_tick(queue, client, ACTOR)
    assert len(client.jobs) == 1


def test_no_new_pg_connection_when_remainder_below_libpq_minimum(monkeypatch):
    monkeypatch.setattr(database._BudgetConnection, 'connect',
                        lambda **kwargs: pytest.fail('new connection must not start'))
    with use_budget(Deadline.after(1.9)), pytest.raises(BudgetExpired):
        database.LabDatabase(settings())._open('api')


def test_pg_connect_timeout_floors_remaining_and_close_on_expiry(monkeypatch):
    clock, calls = Clock(), []
    connection = SimpleNamespace(close=lambda: calls.append('close'))

    def connect(**kwargs):
        calls.append(kwargs)
        clock.spend(2.8)
        return connection

    monkeypatch.setattr(database._BudgetConnection, 'connect', connect)
    with use_budget(Deadline.after(2.7, clock=clock)), pytest.raises(BudgetExpired):
        database.LabDatabase(settings())._open('api')
    assert calls[0]['connect_timeout'] == 2
    assert calls[0]['hostaddr'] == '127.0.0.1'
    assert calls[1:] == ['close']


def test_pinned_psycopg_wait_supports_timeout():
    assert psycopg.__version__ == '3.3.6'
    assert 'timeout' in inspect.signature(psycopg.Connection.wait).parameters
    assert 'timeout' in inspect.signature(psycopg.waiting.wait).parameters


def test_pg_each_guard_and_commit_wait_uses_decreasing_remainder(monkeypatch):
    clock, timeouts, closed = Clock(), [], []
    budget = Deadline.after(10, clock=clock)
    connection = SimpleNamespace(_io_budget=budget, pgconn=SimpleNamespace(socket=123),
                                 close=lambda: closed.append(True))

    def wait(gen, fd, *, interval, timeout):
        assert fd == 123
        timeouts.append(timeout)
        clock.spend(2)
        return gen

    monkeypatch.setattr(psycopg.waiting, 'wait', wait)
    for operation in ('guard', 'row_lock', 'fetch', 'write', 'commit'):
        if operation == 'commit':
            with pytest.raises(BudgetExpired):
                database._BudgetConnection.wait(connection, operation)
        else:
            assert database._BudgetConnection.wait(connection, operation) == operation
    assert timeouts == [10, 8, 6, 4, 2] and closed == [True]


def test_pg_driver_timeout_closes_without_cancellation_or_rollback(monkeypatch):
    actions = []
    connection = SimpleNamespace(_io_budget=Deadline.after(10),
        pgconn=SimpleNamespace(socket=123), close=lambda: actions.append('close'))

    def wait(*args, **kwargs):
        raise psycopg.errors._WaitTimeout('private driver details')

    monkeypatch.setattr(psycopg.waiting, 'wait', wait)
    with pytest.raises(BudgetExpired, match='^SCHEDULER_BUDGET_EXHAUSTED$'):
        database._BudgetConnection.wait(connection, iter(()))
    assert actions == ['close']


@pytest.mark.parametrize('failure', ['body', 'commit', 'expired_body'])
def test_transaction_failure_never_starts_fresh_rollback(monkeypatch, failure):
    clock, actions = Clock(), []

    def commit():
        actions.append('commit')
        if failure == 'commit':
            clock.spend(10)
            current_budget().check()

    connection = SimpleNamespace(commit=commit, close=lambda: actions.append('close'))
    db = database.LabDatabase(settings())
    monkeypatch.setattr(db, '_open', lambda role: connection)
    with use_budget(Deadline.after(10, clock=clock)):
        with pytest.raises(BudgetExpired if failure != 'body' else ValueError):
            with db.connect():
                actions.append('body')
                if failure == 'body':
                    raise ValueError('test')
                if failure == 'expired_body':
                    clock.spend(10)
    assert actions == (['body', 'commit', 'close'] if failure == 'commit' else ['body', 'close'])


def test_scheduler_budget_does_not_silently_use_unbounded_transport():
    transport = SimpleNamespace(open=lambda *args, **kwargs: pytest.fail('unbounded open'))
    client = WorkflowClient({'api_token': 'a' * 64}, transport=transport)
    with use_budget(Deadline.after(10)), pytest.raises(WorkflowError, match='^PROTOCOL_ERROR$'):
        client.readiness()


def test_http_response_read_and_semantic_validation_share_budget():
    clock = Clock()

    class Response(BytesIO):
        status, headers = 200, {}

        def read(self, size):
            clock.spend(10)
            return super().read(size)

    class Transport:
        def open_budgeted(self, request, *, timeout, deadline):
            assert deadline is current_budget() and timeout == 5
            return Response(json.dumps({'ready': True, 'code': 'READY', 'state': 'ready'}).encode())

    client = WorkflowClient({'api_token': 'a' * 64}, transport=Transport())
    with use_budget(Deadline.after(10, clock=clock)), pytest.raises(BudgetExpired):
        client.readiness()
