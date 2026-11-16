"""Real Windows-spawn lifecycle controls with no model/network dependencies."""
from datetime import datetime, timezone
import json
import multiprocessing
import os
import queue
import threading
import time

import pytest

from src.workflow.worker import (BootstrapResult, ManagedWorker, MAX_RESPONSE_BYTES,
                                 WorkerNotReady)


def boot():
    return BootstrapResult({'counter': 0}, {'fixture': 'no-model-no-network'})


def slow_boot():
    time.sleep(60)
    return boot()


def bad_boot():
    raise RuntimeError('SECRET PAN 4111111111111111')


def result_for(incident_id):
    return {'schema_version': '1.0', 'status': 'refusal', 'incident_id': incident_id,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'data_completeness': 'unassessed', 'reason_code': 'POLICY_NOT_APPROVED',
            'reason': 'Synthetic lifecycle control only.', 'recommended_action': 'manual_review'}


def handler(state, incident_id, engine):
    if incident_id == 'INC-000001':
        time.sleep(60)
    if incident_id == 'INC-000002':
        os._exit(23)
    if incident_id == 'INC-000003':
        raise RuntimeError('SECRET PAN 4111111111111111')
    if incident_id == 'INC-000004':
        raise WorkerNotReady()
    if incident_id == 'INC-000005':
        return {'status': 'draft', 'secret': '4111111111111111'}
    state['counter'] += 1
    result = result_for(incident_id)
    result['reason'] = str(state['counter'])
    return result


def wrong_correlation_child(connection, _bootstrap, _handler):
    connection.send_bytes(b'{"kind":"ready","provenance":{}}')
    request = json.loads(connection.recv_bytes())
    connection.send_bytes(json.dumps({'kind': 'result', 'request_id': 'wrong',
                                     'result': result_for(request['incident_id'])}).encode())
    time.sleep(60)


def wrong_incident_child(connection, _bootstrap, _handler):
    connection.send_bytes(b'{"kind":"ready","provenance":{}}')
    request = json.loads(connection.recv_bytes())
    connection.send_bytes(json.dumps({'kind': 'result', 'request_id': request['request_id'],
                                     'result': result_for('INC-999999')}).encode())
    time.sleep(60)


def oversized_child(connection, _bootstrap, _handler):
    connection.send_bytes(b'{"kind":"ready","provenance":{}}')
    connection.recv_bytes()
    connection.send_bytes(b'x' * (MAX_RESPONSE_BYTES + 1))
    time.sleep(60)


def delayed_child(connection, _bootstrap, _handler):
    connection.send_bytes(b'{"kind":"ready","provenance":{}}')
    request = json.loads(connection.recv_bytes())
    time.sleep(0.4)
    connection.send_bytes(json.dumps({'kind': 'result', 'request_id': request['request_id'],
                                     'result': result_for(request['incident_id'])}).encode())
    time.sleep(60)


@pytest.fixture
def worker():
    with ManagedWorker(bootstrap=boot, handler=handler) as value:
        ready = value.start(timeout_seconds=15)
        assert ready['ready'], ready
        yield value
    assert not value.is_ready and value.process_id is None


def test_readiness_required_and_constructor_is_lazy():
    with ManagedWorker(bootstrap=boot, handler=handler) as value:
        assert value.process_id is None and not value.is_ready
        outcome = value.execute('INC-000123')
        assert outcome.status == 'failed' and outcome.code == 'NOT_READY'


def test_single_bootstrap_persistent_child_reused(worker):
    first_pid = worker.process_id
    readiness = worker.start(timeout_seconds=1)
    assert readiness['ready'] and readiness['process_id'] == first_pid
    first = worker.execute('INC-000123', timeout_seconds=10)
    second = worker.execute('INC-000123', timeout_seconds=10)
    assert first.status == second.status == 'result'
    assert first.result['reason'] == '1' and second.result['reason'] == '2'
    assert worker.process_id == first_pid and worker.is_ready


def test_stalled_readiness_is_terminated():
    with ManagedWorker(bootstrap=slow_boot, handler=handler) as value:
        began = time.monotonic()
        result = value.start(timeout_seconds=0.25)
        assert result['code'] == 'DEADLINE_EXCEEDED' and not result['ready']
        assert value.process_id is None and not value.is_ready
        assert time.monotonic() - began < 3  # Scheduling/cleanup allowance, not exact SLA.


def test_boot_failure_does_not_leak_payload():
    with ManagedWorker(bootstrap=bad_boot, handler=handler) as value:
        result = value.start(timeout_seconds=15)
        assert not result['ready'] and result['code'] == 'NOT_READY'
        assert 'SECRET' not in json.dumps(result) and '4111111111111111' not in json.dumps(result)
        assert value.process_id is None


def test_stalled_execution_deadline_kills_and_invalidates(worker):
    result = worker.execute('INC-000001', timeout_seconds=0.2)
    assert result.status == 'timed_out' and result.code == 'DEADLINE_EXCEEDED'
    assert result.result is None and result.elapsed_seconds < 3
    assert 0 <= result.cleanup_seconds <= result.elapsed_seconds
    assert worker.process_id is None and not worker.is_ready
    assert worker.execute('INC-000123').code == 'NOT_READY'
    assert worker.start(timeout_seconds=15)['ready']
    assert worker.execute('INC-000123', timeout_seconds=10).status == 'result'


def test_active_cancel_kills_without_late_result(worker):
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    try:
        result = worker.execute('INC-000001', timeout_seconds=10, cancel_event=cancel)
    finally:
        timer.cancel()
    assert result.status == 'cancelled' and result.result is None
    assert result.elapsed_seconds < 3 and worker.process_id is None


def test_already_cancelled_request_cannot_publish(worker):
    cancel = threading.Event()
    cancel.set()
    result = worker.execute('INC-000123', timeout_seconds=10, cancel_event=cancel)
    assert result.status == 'cancelled' and result.result is None and not worker.is_ready


@pytest.mark.parametrize('incident,code', [('INC-000002', 'INTERNAL_ERROR'),
    ('INC-000003', 'INTERNAL_ERROR'), ('INC-000004', 'NOT_READY'), ('INC-000005', 'INTERNAL_ERROR')])
def test_death_exception_stale_provenance_or_bad_result_retire_worker(worker, incident, code):
    result = worker.execute(incident, timeout_seconds=10)
    assert result.status == 'failed' and result.code == code and result.result is None
    assert 'SECRET' not in repr(result) and '4111111111111111' not in repr(result)
    assert worker.process_id is None and not worker.is_ready


def test_concurrent_execute_and_start_rejected(worker):
    cancel = threading.Event()
    inbox = queue.Queue()
    thread = threading.Thread(target=lambda: inbox.put(worker.execute('INC-000001',
                                timeout_seconds=10, cancel_event=cancel)))
    thread.start()
    deadline = time.monotonic() + 3
    while not worker._lock.locked() and time.monotonic() < deadline:
        time.sleep(0.005)
    try:
        assert worker.execute('INC-000123').code == 'BUSY'
        assert worker.start()['code'] == 'BUSY'
    finally:
        cancel.set()
        thread.join(3)
    assert not thread.is_alive() and inbox.get_nowait().status == 'cancelled'


def test_close_during_execution_does_not_wait_for_execution_lock(worker):
    inbox = queue.Queue()
    thread = threading.Thread(target=lambda: inbox.put(worker.execute('INC-000001', timeout_seconds=10)))
    thread.start()
    deadline = time.monotonic() + 3
    while not worker._lock.locked() and time.monotonic() < deadline:
        time.sleep(0.005)
    began = time.monotonic()
    worker.close()
    thread.join(3)
    assert time.monotonic() - began < 3
    assert not thread.is_alive() and inbox.get_nowait().status == 'cancelled'
    worker.close()  # Idempotent.


def test_close_racing_initial_readiness_check_cancels_admitted_call(worker, monkeypatch):
    """Force close between execution-lock admission and the readiness read.

    If admission does not own the lifecycle lock, let close finish before the
    read; otherwise let close contend on that lock. Neither schedule needs a
    timing sleep, and both must cancel the already admitted call.
    """
    checking = threading.Event()
    release_check = threading.Event()
    close_attempted = threading.Event()
    close_done = threading.Event()
    acquired_without_waiting = []
    outcomes = queue.Queue()
    lifecycle_lock = worker._lifecycle_lock
    readiness = ManagedWorker.is_ready

    class ObservedLifecycleLock:
        def __enter__(self):
            if threading.current_thread() is closer:
                acquired = lifecycle_lock.acquire(blocking=False)
                acquired_without_waiting.append(acquired)
                close_attempted.set()
                if not acquired:
                    lifecycle_lock.acquire()
            else:
                lifecycle_lock.acquire()
            return self

        def __exit__(self, *_):
            lifecycle_lock.release()

    def paused_readiness(value):
        if value is worker and threading.current_thread() is executor:
            checking.set()
            assert release_check.wait(5), 'readiness barrier was not released'
        return readiness.fget(value)

    def close_worker():
        try:
            worker.close()
        finally:
            close_done.set()

    executor = threading.Thread(target=lambda: outcomes.put(
        worker.execute('INC-000001', timeout_seconds=10)))
    closer = threading.Thread(target=close_worker)
    monkeypatch.setattr(worker, '_lifecycle_lock', ObservedLifecycleLock())
    monkeypatch.setattr(ManagedWorker, 'is_ready', property(paused_readiness))
    executor.start()
    try:
        assert checking.wait(5), 'execution never reached readiness'
        assert worker._lock.locked()
        closer.start()
        assert close_attempted.wait(5), 'close never attempted lifecycle admission'
        if acquired_without_waiting[0]:
            assert close_done.wait(5), 'uncontended close did not complete'
    finally:
        release_check.set()
        executor.join(5)
        if closer.ident is not None:
            closer.join(5)
    assert not executor.is_alive() and not closer.is_alive()
    result = outcomes.get_nowait()
    assert result.status == 'cancelled' and result.code == 'CANCELLED'
    assert result.result is None and worker.process_id is None


def test_execute_after_close_is_not_admitted(worker):
    worker.close()
    result = worker.execute('INC-000123')
    assert result.status == 'failed' and result.code == 'NOT_READY'
    assert not worker.start()['ready'] and worker.process_id is None


def test_close_during_readiness_terminates():
    with ManagedWorker(bootstrap=slow_boot, handler=handler) as value:
        inbox = queue.Queue()
        thread = threading.Thread(target=lambda: inbox.put(value.start(timeout_seconds=15)))
        thread.start()
        deadline = time.monotonic() + 5
        while value.process_id is None and time.monotonic() < deadline:
            time.sleep(0.005)
        value.close()
        thread.join(3)
        assert not thread.is_alive() and not inbox.get_nowait()['ready']
        assert value.process_id is None


@pytest.mark.parametrize('child,timeout,expected', [
    (wrong_correlation_child, 10, 'failed'), (wrong_incident_child, 10, 'failed'),
    (oversized_child, 10, 'failed'), (delayed_child, 0.1, 'timed_out'),
])
def test_protocol_mismatch_oversize_and_late_frames_not_published(monkeypatch, child, timeout, expected):
    monkeypatch.setattr('src.workflow.worker._child_main', child)
    with ManagedWorker(bootstrap=boot, handler=handler) as value:
        assert value.start(timeout_seconds=15)['ready']
        result = value.execute('INC-000123', timeout_seconds=timeout)
        assert result.status == expected and result.result is None
        assert value.process_id is None


def test_unsupported_engine_and_giant_input_rejected_without_retiring(worker):
    assert worker.execute('INC-000123', engine='remote').code == 'INVALID_INPUT_FORMAT'
    assert worker.execute('X' * 5000).code == 'INVALID_INPUT_FORMAT'
    assert worker.is_ready


@pytest.mark.parametrize('timeout', [0, -1, 181, True, float('nan'), float('inf'), '1'])
def test_invalid_deadlines_rejected(timeout):
    with ManagedWorker(bootstrap=boot, handler=handler) as value:
        with pytest.raises(ValueError):
            value.start(timeout_seconds=timeout)
        with pytest.raises(ValueError):
            value.execute('INC-000123', timeout_seconds=timeout)


def test_no_spawn_process_survives_context_manager():
    with ManagedWorker(bootstrap=boot, handler=handler) as value:
        assert value.start(timeout_seconds=15)['ready']
        pid = value.process_id
    assert pid not in [process.pid for process in multiprocessing.active_children()]
