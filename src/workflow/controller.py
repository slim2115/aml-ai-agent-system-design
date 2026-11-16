"""Admission, durable state and a parent timer independent of worker cleanup.

One local controller owns a database lease. Readiness is explicit and occurs
before acceptance; accepted jobs include dispatch/IPC time in their deadline.
"""
from datetime import datetime, timezone
import queue
import threading
import time
from uuid import uuid4

from src.config import get_demo_paths, get_settings
from src.contracts.v1 import CreateJobRequest, ErrorResult, instant
from src.runtime.policy import load_demo_policy
from .lease import ControllerLease
from .store import ACTIVE, MAX_RUNTIME_EVENTS, SQLiteStore, WorkflowError, utc_now
from .worker import ManagedWorker


class WorkflowService:
    def __init__(self, store=None, *, worker=None):
        self.store = store if store is not None else SQLiteStore()
        try:
            self._lease = ControllerLease(self.store.path)
        except (OSError, ValueError):
            raise WorkflowError('NOT_READY') from None
        self.worker = worker if worker is not None else ManagedWorker()
        self._lock = threading.RLock()
        self._closed = False
        self._preparing = None
        self._active = None
        self._ready_result = {'ready': False, 'code': 'NOT_READY'}
        self.last_cleanup_seconds = None
        try:
            self.store.recover_active_jobs()
        except Exception:
            self._lease.close()
            raise

    def readiness(self):
        with self._lock:
            if self._closed:
                return {'ready': False, 'code': 'NOT_READY', 'state': 'closed'}
            preparing = self._preparing is not None and self._preparing.is_alive()
            busy = self._active is not None
            ready = self.worker.is_ready and not preparing and not busy
            last_code = self._ready_result.get('code', 'NOT_READY')
            code = 'READY' if ready else ('BUSY' if busy else (last_code if last_code in {'NOT_READY', 'DEADLINE_EXCEEDED'} else 'NOT_READY'))
            return {'ready': ready, 'code': code,
                    'state': 'preparing' if preparing else ('busy' if busy else ('ready' if ready else 'not_ready'))}

    def prepare(self, *, timeout_seconds=180):
        """Start readiness asynchronously; returning is not readiness success."""
        with self._lock:
            if self._closed or self._active is not None:
                raise WorkflowError('NOT_READY')
            if self.worker.is_ready or (self._preparing is not None and self._preparing.is_alive()):
                return self.readiness()
            def run():
                try:
                    result = self.worker.start(timeout_seconds=timeout_seconds)
                except Exception:
                    result = {'ready': False, 'code': 'NOT_READY'}
                with self._lock:
                    self._ready_result = result
            self._ready_result = {'ready': False, 'code': 'NOT_READY'}
            self._preparing = threading.Thread(target=run, name='aml-readiness', daemon=True)
            self._preparing.start()
            return self.readiness()

    def submit(self, request, principal, idempotency_key, *, engine='template', timeout_seconds=None):
        try:
            request = CreateJobRequest.model_validate(request.model_dump(mode='json') if hasattr(request, 'model_dump') else request)
        except (TypeError, ValueError):
            raise WorkflowError('INVALID_INPUT_FORMAT') from None
        budget = get_settings().performance.generation_timeout_sec if timeout_seconds is None else timeout_seconds
        with self._lock:
            if self._closed:
                raise WorkflowError('NOT_READY')
            replay = self.store.lookup_job_replay(request, principal, idempotency_key, timeout_seconds=budget, engine=engine)
            if replay is not None:
                return replay
            if not self.readiness()['ready']:
                raise WorkflowError('NOT_READY')
            policy = load_demo_policy(get_demo_paths(get_settings().project_root)[0])
            if request.window != policy.window:
                raise WorkflowError('INVALID_INPUT_FORMAT')
            job = self.store.create_job(request, principal, idempotency_key, timeout_seconds=budget, engine=engine)
            remaining = (instant(job['deadline_at']) - datetime.now(timezone.utc)).total_seconds()
            control = {'job_id': job['job_id'], 'cancel': threading.Event(), 'principal': principal,
                       'watch_stop': threading.Event(), 'monotonic_deadline': time.monotonic() + max(0, remaining)}
            self._active = control
            control['timer'] = threading.Timer(max(0, remaining), self._expire, args=(control,))
            control['timer'].daemon = True
            control['thread'] = threading.Thread(target=self._execute, args=(control, engine), name='aml-job', daemon=True)
            control['watcher'] = threading.Thread(target=self._watch_durable_state, args=(control,), name='aml-job-observer', daemon=True)
            try:
                running = dict(job, status='running', revision=job['revision'] + 1)
                self.store.transition_job(job['job_id'], job['revision'], running)
                control['timer'].start()
                control['watcher'].start()
                control['thread'].start()
            except Exception:
                control['timer'].cancel()
                control['watch_stop'].set()
                self._finish_failure(control, 'INTERNAL_ERROR')
                self._active = None
                raise WorkflowError('INTERNAL_ERROR') from None
            # The persisted acceptance response stays stable under idempotency.
            return job

    def _watch_durable_state(self, control):
        # A CLI/API caller in another process can cancel in the same database.
        # Observe that durable decision, not only this controller's local event.
        while not control['watch_stop'].wait(.1):
            try:
                job = self.store.get_job_internal(control['job_id'])
                if job['status'] not in ACTIVE:
                    control['cancel'].set()
                    return
            except WorkflowError:
                continue

    def _terminal(self, control, *, result=None, status=None):
        # Closing the controller waits for an in-flight publication and then
        # prevents every later worker/timer write before releasing the OS lease.
        with self._lock:
            if self._closed:
                raise WorkflowError('NOT_READY')
            return self._publish_terminal(control, result=result, status=status)

    def _publish_terminal(self, control, *, result=None, status=None):
        for _ in range(3):
            job = self.store.get_job_internal(control['job_id'])
            if job['status'] not in ACTIVE:
                return job
            now = utc_now()
            late = time.monotonic() >= control['monotonic_deadline'] or instant(now) >= instant(job['deadline_at'])
            if late:
                status, result = 'timed_out', None
                # Monotonic time controls cancellation. Wall time is clamped to
                # the already persisted deadline if the system clock moved back.
                if instant(now) < instant(job['deadline_at']):
                    now = job['deadline_at']
            following = dict(job, status=status, revision=job['revision'] + 1, completed_at=now)
            if result is not None:
                following['result'] = result
            try:
                return self.store.transition_job(job['job_id'], job['revision'], following)
            except WorkflowError as exc:
                if exc.code == 'VERSION_CONFLICT':
                    continue
                raise
        raise WorkflowError('VERSION_CONFLICT')

    def _reconcile_terminal(self, control):
        """Keep admission closed until a terminal state is durably confirmed.

        A transient disk/SQLite failure must not orphan an accepted running job.
        The existing deadline timer stays armed; restart recovery covers process
        termination, and close prevents writes after the lease is handed back.
        """
        while not self._closed:
            try:
                job = self.store.get_job_internal(control['job_id'])
                if job['status'] not in ACTIVE:
                    return
                self._finish_failure(control, 'DEPENDENCY_UNAVAILABLE')
            except WorkflowError:
                time.sleep(.05)

    def _expire(self, control):
        try:
            self._terminal(control, status='timed_out')
        except WorkflowError:
            # The worker completion path and startup recovery also reconcile the
            # job. Storage failure is never reported as a successful investigation.
            pass
        finally:
            control['cancel'].set()

    def _finish_failure(self, control, code):
        error = ErrorResult(schema_version='1.0', status='error', generated_at=utc_now(),
                            correlation_id='workflow-' + uuid4().hex, code=code,
                            message='Локальная обработка не завершена. Состояние задания сохранено.', retryable=True)
        return self._terminal(control, result=error.model_dump(mode='json'), status='failed')

    def _start_runtime_writer(self, control):
        control['runtime_events'] = queue.Queue(maxsize=MAX_RUNTIME_EVENTS)
        control['runtime_events_done'] = threading.Event()
        control['runtime_events_failed'] = threading.Event()

        def persist():
            while not self._closed:
                try:
                    sequence, event = control['runtime_events'].get(timeout=.05)
                except queue.Empty:
                    if control['runtime_events_done'].is_set():
                        return
                    continue
                # Keep this item until committed; a transient SQLite failure
                # cannot turn a successful result into an unrecorded trace.
                while not self._closed:
                    try:
                        with self._lock:
                            if self._closed:
                                return
                            self.store.append_runtime_event(control['job_id'], sequence, event)
                        break
                    except WorkflowError as exc:
                        if exc.code == 'NOT_READY':
                            time.sleep(.05)
                            continue
                        control['runtime_events_failed'].set()
                        control['cancel'].set()
                        return
                    except Exception:
                        control['runtime_events_failed'].set()
                        control['cancel'].set()
                        return

        control['runtime_writer'] = threading.Thread(target=persist, name='aml-runtime-events', daemon=True)
        control['runtime_writer'].start()
        return lambda sequence, event: control['runtime_events'].put_nowait((sequence, event))

    def _drain_runtime_events(self, control):
        if 'runtime_writer' not in control:
            return
        control['runtime_events_done'].set()
        # The deadline timer and worker monitor remain independent of this
        # writer. Publication still observes the original accepted-job budget.
        while control['runtime_writer'].is_alive() and not self._closed:
            control['runtime_writer'].join(.05)

    def _execute(self, control, engine):
        try:
            remaining = control['monotonic_deadline'] - time.monotonic()
            if remaining <= 0:
                self._expire(control)
                return
            job = self.store.get_job_internal(control['job_id'])
            options = {}
            if getattr(self.worker, 'supports_runtime_events', False):
                options['event_sink'] = self._start_runtime_writer(control)
            outcome = self.worker.execute(job['incident_id'], engine=engine, timeout_seconds=remaining,
                                          cancel_event=control['cancel'], **options)
            self.last_cleanup_seconds = getattr(outcome, 'cleanup_seconds', None)
            self._drain_runtime_events(control)
            if control.get('runtime_events_failed') is not None and control['runtime_events_failed'].is_set():
                self._finish_failure(control, 'INTERNAL_ERROR')
                return
            if outcome.status == 'result':
                status = {'draft': 'succeeded', 'refusal': 'refused', 'error': 'failed'}[outcome.result['status']]
                self._terminal(control, result=outcome.result, status=status)
            elif outcome.status == 'timed_out':
                if time.monotonic() >= control['monotonic_deadline']:
                    self._expire(control)
                else:
                    self._finish_failure(control, 'DEADLINE_EXCEEDED')
            elif outcome.status == 'cancelled':
                # User cancellation/expiry persists before signalling the worker.
                job = self.store.get_job_internal(control['job_id'])
                if job['status'] in ACTIVE:
                    self._terminal(control, status='cancelled')
            else:
                self._finish_failure(control, 'DEPENDENCY_UNAVAILABLE')
        except Exception:
            try:
                self._finish_failure(control, 'INTERNAL_ERROR')
            except WorkflowError:
                pass
        finally:
            self._drain_runtime_events(control)
            self._reconcile_terminal(control)
            control['watch_stop'].set()
            control['timer'].cancel()
            with self._lock:
                if self._active is control:
                    self._active = None

    def cancel(self, job_id, principal, idempotency_key):
        result = self.store.cancel_job(job_id, principal, idempotency_key=idempotency_key)
        with self._lock:
            if self._active is not None and self._active['job_id'] == job_id:
                self._active['timer'].cancel()
                self._active['cancel'].set()
        return result

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            control = self._active
        if control is not None:
            try:
                self.store.cancel_job(control['job_id'], control['principal'])
            except WorkflowError:
                pass
            control['cancel'].set()
            control['watch_stop'].set()
            control['timer'].cancel()
        self.worker.close()
        if control is not None:
            control['thread'].join(timeout=1)
        preparing = self._preparing
        if preparing is not None and preparing is not threading.current_thread():
            preparing.join(timeout=1)
        self._lease.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
