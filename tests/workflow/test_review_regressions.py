"""Independent review regressions for close and persistence races."""
import threading
import time
from uuid import uuid4

from src.config import get_settings
from src.workflow.controller import WorkflowService
from src.workflow.store import SQLiteStore, WorkflowError
from src.workflow.worker import ManagedWorker
from tests.workflow.test_controller import ACTOR, FakeWorker, ready, request, until
from tests.workflow.test_worker import boot, handler


def test_close_before_spawn_cannot_be_undone_by_preparation(monkeypatch):
    """Close wins even before the startup thread publishes its child handle."""
    worker = ManagedWorker(bootstrap=boot, handler=handler)
    before_spawn, proceed = threading.Event(), threading.Event()
    original_stop = worker._stop
    result = []

    def pause_startup_boundary():
        elapsed = original_stop()
        if threading.current_thread().name == 'review-startup':
            before_spawn.set()
            assert proceed.wait(5)
        return elapsed

    monkeypatch.setattr(worker, '_stop', pause_startup_boundary)
    starter = threading.Thread(target=lambda: result.append(worker.start(timeout_seconds=15)),
                               name='review-startup')
    starter.start()
    try:
        assert before_spawn.wait(5)
        worker.close()
        proceed.set()
        starter.join(18)
        assert not starter.is_alive()
        assert result and not result[0]['ready']
        assert worker.process_id is None
    finally:
        proceed.set()
        starter.join(18)
        worker.close()


def test_failed_terminal_writes_do_not_admit_while_prior_job_is_orphaned(monkeypatch):
    """A transient database fault cannot silently orphan a running job."""
    path = get_settings().project_root / 'var/workflow' / ('review-' + uuid4().hex) / 'state.sqlite3'
    store = SQLiteStore(path)
    worker = FakeWorker(delay=.08)
    attempts = []
    original_transition = store.transition_job

    def fail_first_terminal_writes(job_id, revision, following):
        if following['status'] not in ('queued', 'running') and len(attempts) < 2:
            attempts.append(following['status'])
            raise WorkflowError('NOT_READY')
        return original_transition(job_id, revision, following)

    monkeypatch.setattr(store, 'transition_job', fail_first_terminal_writes)
    with WorkflowService(store, worker=worker) as service:
        ready(service)
        accepted = service.submit(request(), ACTOR, 'review-storage', timeout_seconds=1)
        until(lambda: len(attempts) == 2)
        # Permit retries/reconciliation to recover; otherwise admission must stay
        # closed. The original implementation immediately returned READY while
        # keeping this durable row running and cancelling its deadline timer.
        end = time.monotonic() + .3
        while time.monotonic() < end:
            current = store.get_job(accepted['job_id'], ACTOR)
            if current['status'] not in ('queued', 'running'):
                break
            assert not service.readiness()['ready']
            time.sleep(.01)
        until(lambda: store.get_job(accepted['job_id'], ACTOR)['status'] not in ('queued', 'running'), timeout=4)
