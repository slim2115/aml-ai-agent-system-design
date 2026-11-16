"""Parent-controlled, single-flight spawn worker for the local v1 runtime.

Readiness loads the verified embedding/index before accepting investigation work.
The parent never waits in a pipe read: bounded reader/sender threads isolate IPC
from the monotonic deadline monitor. This is a process boundary, not an OS
firewall or a hard-real-time scheduler guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import inspect
import json
import math
import multiprocessing
import queue
import threading
import time
from typing import Any, Callable, Literal
from uuid import uuid4

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 4096
MAX_EVENT_BYTES = 1024
_POLL_SECONDS = 0.02
_JOIN_SECONDS = 0.2


@dataclass(frozen=True)
class ExecutionOutcome:
    status: Literal['result', 'timed_out', 'cancelled', 'failed']
    result: dict | None = None
    code: str = ''
    elapsed_seconds: float = 0.0
    cleanup_seconds: float = 0.0
    events: tuple[dict, ...] = ()


@dataclass(frozen=True)
class BootstrapResult:
    """Trusted bootstrap injection used by hermetic spawn tests only."""
    state: Any
    provenance: dict


class WorkerNotReady(Exception):
    pass


def _json_bytes(value: Any, maximum: int) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > maximum:
        raise ValueError('IPC_SIZE_LIMIT')
    return raw


def _json_object(raw: bytes) -> dict:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('DUPLICATE_KEY')
            value[key] = item
        return value
    def constant(_):
        raise ValueError('NONFINITE_VALUE')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    if type(value) is not dict:
        raise ValueError('INVALID_MESSAGE')
    return value


def _source_provenance(settings) -> dict:
    """Hash the complete local model artifact, not just its manifest label."""
    from pathlib import Path
    from src.config import get_demo_paths
    from src.retrieval.embedding import validate_embedding_artifact
    validate_embedding_artifact(settings)
    root = settings.project_root
    policy_path, snapshot_dir = get_demo_paths(root)
    paths = {
        'rule_corpus_sha256': snapshot_dir / 'rules.json',
        'snapshot_manifest_sha256': snapshot_dir / 'manifest.json',
        'policy_sha256': policy_path,
        'embedding_manifest_sha256': Path(settings.llm.embedding_manifest_path),
    }
    values = {}
    for key, path in paths.items():
        path.resolve(strict=True).relative_to(root.resolve())
        if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
            raise WorkerNotReady()
        values[key] = sha256(path.read_bytes()).hexdigest()
    return values


def _bootstrap() -> BootstrapResult:
    from src.config import get_demo_paths, get_settings
    from src.runtime.policy import load_demo_policy
    from src.runtime.retrieval import RuleIndex
    settings = get_settings()
    before = _source_provenance(settings)
    policy_path, snapshot_dir = get_demo_paths(settings.project_root)
    load_demo_policy(policy_path)
    index = RuleIndex.from_file(snapshot_dir / 'rules.json', settings=settings)
    if (_source_provenance(settings) != before
            or index.provenance['source_sha256'] != before['rule_corpus_sha256']
            or index.provenance['embedding_manifest_sha256'] != before['embedding_manifest_sha256']):
        raise WorkerNotReady()
    return BootstrapResult((settings, index, before), {
        **before, 'index_fingerprint': index.fingerprint,
        'mode': 'synthetic_learning_only', 'embedding_ready': True,
        'ollama_checked': False,
    })


def _handler(state, incident_id, engine: str, *, event_sink=None) -> dict:
    from src.runtime.service import AMLAgent
    settings, index, expected = state
    if _source_provenance(settings) != expected:
        raise WorkerNotReady()
    result = AMLAgent(mode='demo', engine=engine, settings=settings,
                      index_factory=lambda _: index, event_sink=event_sink).run(incident_id)
    # A replacement during execution must not publish a result as current.
    if _source_provenance(settings) != expected:
        raise WorkerNotReady()
    return result


def _child_main(connection, bootstrap, handler):
    """Only JSON crosses the pipe; callable injection is trusted local code."""
    try:
        prepared = bootstrap()
        if not isinstance(prepared, BootstrapResult) or type(prepared.provenance) is not dict:
            raise WorkerNotReady()
        connection.send_bytes(_json_bytes({'kind': 'ready', 'provenance': prepared.provenance}, MAX_RESPONSE_BYTES))
        while True:
            request = _json_object(connection.recv_bytes(MAX_REQUEST_BYTES))
            if (set(request) != {'kind', 'request_id', 'incident_id', 'engine'} or request['kind'] != 'execute'
                    or type(request['request_id']) is not str or len(request['request_id']) != 32
                    or request['engine'] not in {'template', 'local-model'}):
                raise ValueError('INVALID_REQUEST')
            try:
                from src.contracts.v1 import ADAPTERS
                from src.workflow.store import MAX_RUNTIME_EVENTS, validate_runtime_event
                event_count, event_correlation = 0, None

                def emit(event):
                    nonlocal event_count, event_correlation
                    event = validate_runtime_event(event)
                    if event_count >= MAX_RUNTIME_EVENTS or (event_correlation is not None
                            and event['correlation_id'] != event_correlation):
                        raise ValueError('INVALID_EVENT_STREAM')
                    event_count += 1
                    event_correlation = event['correlation_id']
                    connection.send_bytes(_json_bytes({'kind': 'event', 'request_id': request['request_id'],
                        'sequence': event_count, 'event': event}, MAX_EVENT_BYTES))

                # Old trusted three-argument test handlers remain supported.
                arguments = {'event_sink': emit} if 'event_sink' in inspect.signature(handler).parameters else {}
                result = ADAPTERS['InvestigationResult'].validate_python(
                    handler(prepared.state, request['incident_id'], request['engine'], **arguments)).model_dump(mode='json')
                message = {'kind': 'result', 'request_id': request['request_id'], 'result': result}
            except WorkerNotReady:
                message = {'kind': 'failure', 'request_id': request['request_id'], 'code': 'NOT_READY'}
            except Exception:
                message = {'kind': 'failure', 'request_id': request['request_id'], 'code': 'INTERNAL_ERROR'}
            connection.send_bytes(_json_bytes(message, MAX_RESPONSE_BYTES))
            if message['kind'] == 'failure' or result.get('code') == 'NOT_READY':
                break
    except (EOFError, BrokenPipeError, OSError):
        pass
    except Exception:
        try:
            connection.send_bytes(_json_bytes({'kind': 'startup_failure', 'code': 'NOT_READY'}, MAX_RESPONSE_BYTES))
        except Exception:
            pass
    finally:
        connection.close()


def _receive(connection, inbox):
    """A partial/broken frame cannot trap the deadline-monitoring thread."""
    try:
        while True:
            message = _json_object(connection.recv_bytes(MAX_RESPONSE_BYTES))
            inbox.put_nowait(message)
    except Exception:
        try:
            inbox.put_nowait({'kind': 'ipc_failure'})
        except queue.Full:
            pass


def _send(connection, raw: bytes, inbox):
    try:
        connection.send_bytes(raw)
    except Exception:
        try:
            inbox.put_nowait({'kind': 'ipc_failure'})
        except queue.Full:
            pass


def _timeout(value) -> float:
    if isinstance(value, bool) or type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= 180:
        raise ValueError('timeout_seconds must be finite and in (0, 180]')
    return float(value)


class ManagedWorker:
    """One persistent child, one active call; callers own durable job state.

    ``bootstrap``/``handler`` are internal dependency injection, never user input.
    A timeout/cancellation/death invalidates readiness and requires explicit start.
    ``close`` is permanent and safe during execution or startup. A retired child
    can be restarted after timeout/cancellation, but an explicitly closed owner
    cannot be reopened by an already running preparation thread.
    """
    supports_runtime_events = True
    def __init__(self, *, bootstrap: Callable | None = None, handler: Callable | None = None):
        self._bootstrap = bootstrap or _bootstrap
        self._handler = handler or _handler
        self._context = multiprocessing.get_context('spawn')
        self._lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._abort = threading.Event()
        self._process = None
        self._connection = None
        self._inbox = None
        self._reader = None
        self._ready = False
        self._provenance = {}

    @property
    def process_id(self) -> int | None:
        return self._process.pid if self._process is not None and self._process.is_alive() else None

    @property
    def is_ready(self) -> bool:
        return not self._closed and self._ready and self.process_id is not None and not self._abort.is_set()

    def _stop(self) -> float:
        began = time.monotonic()
        with self._stop_lock:
            self._ready = False
            process = self._process
            if process is not None:
                if process.is_alive():
                    process.terminate()
                process.join(_JOIN_SECONDS)
                if process.is_alive():
                    process.kill()
                    process.join(_JOIN_SECONDS)
            if self._connection is not None:
                self._connection.close()
            # Reader threads are daemonized and hold only a bounded frame. Killing
            # the sole peer closes its endpoint and unblocks recv even on Windows.
        return time.monotonic() - began

    def close(self):
        # Serialize only child creation/publication, never bootstrap/IPC waits or
        # the execution lock. Once closed, start cannot clear this cancellation.
        with self._lifecycle_lock:
            self._closed = True
            self._abort.set()
        self._stop()

    def _wait(self, deadline, cancel_event=None):
        while True:
            if self._abort.is_set() or (cancel_event is not None and cancel_event.is_set()):
                return 'cancelled', None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 'timed_out', None
            try:
                message = self._inbox.get(timeout=min(_POLL_SECONDS, remaining))
            except queue.Empty:
                if self._process is None or not self._process.is_alive():
                    return 'failed', None
                continue
            # Receiving a frame, including decoding, consumes the same budget.
            if time.monotonic() >= deadline:
                return 'timed_out', None
            if self._abort.is_set() or (cancel_event is not None and cancel_event.is_set()):
                return 'cancelled', None
            return 'message', message

    def start(self, timeout_seconds=180) -> dict:
        began = time.monotonic()
        timeout = _timeout(timeout_seconds)
        if not self._lock.acquire(blocking=False):
            return {'ready': False, 'code': 'BUSY', 'process_id': self.process_id, 'provenance': {}}
        try:
            if self._closed:
                return {'ready': False, 'code': 'NOT_READY', 'process_id': None, 'provenance': {}}
            if self.is_ready:
                return {'ready': True, 'code': 'READY', 'process_id': self.process_id,
                        'provenance': dict(self._provenance)}
            self._stop()
            with self._lifecycle_lock:
                # close may have won while cleanup was in progress. The check,
                # abort reset and publication of the started child are atomic
                # relative to close; warmup happens outside this short section.
                if self._closed:
                    return {'ready': False, 'code': 'NOT_READY', 'process_id': None, 'provenance': {}}
                self._abort.clear()
                from src.workflow.store import MAX_RUNTIME_EVENTS
                # Enough room for one bounded stream and its result/failure.
                # Old frames cannot grow without bound or block the reader.
                self._inbox = queue.Queue(maxsize=MAX_RUNTIME_EVENTS + 2)
                parent, child = self._context.Pipe(duplex=True)
                self._connection = parent
                self._process = self._context.Process(target=_child_main,
                    args=(child, self._bootstrap, self._handler), daemon=True, name='aml-v1-worker')
                try:
                    self._process.start()
                except Exception:
                    child.close()
                    parent.close()
                    self._process = None
                    return {'ready': False, 'code': 'NOT_READY', 'process_id': None, 'provenance': {}}
                child.close()
            self._reader = threading.Thread(target=_receive, args=(parent, self._inbox), daemon=True,
                                            name='aml-worker-reader')
            self._reader.start()
            status, message = self._wait(began + timeout)
            with self._lifecycle_lock:
                if (not self._closed and not self._abort.is_set()
                        and status == 'message' and set(message) == {'kind', 'provenance'}
                        and message['kind'] == 'ready' and type(message['provenance']) is dict
                        and self.process_id is not None):
                    self._ready = True
                    self._provenance = message['provenance']
                    return {'ready': True, 'code': 'READY', 'process_id': self.process_id,
                            'provenance': dict(self._provenance)}
            self._stop()
            return {'ready': False, 'code': 'DEADLINE_EXCEEDED' if status == 'timed_out' else 'NOT_READY',
                    'process_id': None, 'provenance': {}}
        finally:
            self._lock.release()

    def execute(self, incident_id, engine='template', timeout_seconds=180,
                cancel_event: threading.Event | None = None, *, event_sink=None) -> ExecutionOutcome:
        """Stream strict events to a trusted nonblocking sink, retaining a prefix.

        The controller sink only enqueues; SQLite runs on its own writer thread.
        Timeout/cancellation may truncate the trace; no completion is invented.
        """
        began = time.monotonic()
        timeout = _timeout(timeout_seconds)
        # Admission is atomic relative to close. Otherwise close can win between
        # acquiring the execution lock and checking readiness, misreporting an
        # admitted call's cancellation as NOT_READY. Do not hold this short lock
        # while executing or waiting for IPC.
        with self._lifecycle_lock:
            if not self._lock.acquire(blocking=False):
                return ExecutionOutcome('failed', code='BUSY', elapsed_seconds=time.monotonic() - began)
            ready = self.is_ready
        try:
            if not ready:
                return ExecutionOutcome('failed', code='NOT_READY', elapsed_seconds=time.monotonic() - began)
            if engine not in {'template', 'local-model'}:
                return ExecutionOutcome('failed', code='INVALID_INPUT_FORMAT', elapsed_seconds=time.monotonic() - began)
            request_id = uuid4().hex
            try:
                raw = _json_bytes({'kind': 'execute', 'request_id': request_id,
                                  'incident_id': incident_id, 'engine': engine}, MAX_REQUEST_BYTES)
            except Exception:
                return ExecutionOutcome('failed', code='INVALID_INPUT_FORMAT', elapsed_seconds=time.monotonic() - began)
            threading.Thread(target=_send, args=(self._connection, raw, self._inbox), daemon=True,
                             name='aml-worker-sender').start()
            events = []
            from src.workflow.store import MAX_RUNTIME_EVENTS, validate_runtime_event
            while True:
                status, message = self._wait(began + timeout, cancel_event)
                if status != 'message' or message.get('kind') != 'event':
                    break
                try:
                    if (set(message) != {'kind', 'request_id', 'sequence', 'event'}
                            or message['request_id'] != request_id
                            or type(message['sequence']) is not int
                            or message['sequence'] != len(events) + 1
                            or len(events) >= MAX_RUNTIME_EVENTS):
                        raise ValueError('INVALID_EVENT_STREAM')
                    observed = validate_runtime_event(message['event'])
                    if events and observed['correlation_id'] != events[0]['event']['correlation_id']:
                        raise ValueError('EVENT_CORRELATION_MISMATCH')
                    record = {'sequence': message['sequence'], 'event': observed}
                    events.append(record)
                    if event_sink is not None:
                        event_sink(record['sequence'], dict(observed))
                except Exception:
                    status, message = 'failed', None
                    break
            result, code = None, 'INTERNAL_ERROR'
            if status == 'message':
                if message.get('request_id') != request_id:
                    status, code = 'failed', 'INTERNAL_ERROR'
                elif (set(message) == {'kind', 'request_id', 'result'} and message['kind'] == 'result'
                      and type(message['result']) is dict):
                    try:
                        from src.contracts.v1 import ADAPTERS
                        result = ADAPTERS['InvestigationResult'].validate_python(message['result']).model_dump(mode='json')
                        if result.get('incident_id', incident_id) != incident_id:
                            raise ValueError('INCIDENT_MISMATCH')
                        if (events and result['status'] == 'error'
                                and result['correlation_id'] != events[0]['event']['correlation_id']):
                            raise ValueError('RESULT_EVENT_CORRELATION_MISMATCH')
                        status, code = 'result', ''
                    except Exception:
                        result, status = None, 'failed'
                elif (set(message) == {'kind', 'request_id', 'code'} and message['kind'] == 'failure'
                      and message['code'] in {'NOT_READY', 'INTERNAL_ERROR'}):
                    status, code = 'failed', message['code']
                else:
                    status = 'failed'
            # Final validation is charged too. Cancellation wins over a ready frame.
            if self._abort.is_set() or (cancel_event is not None and cancel_event.is_set()):
                status, result, code = 'cancelled', None, 'CANCELLED'
            elif time.monotonic() >= began + timeout:
                status, result, code = 'timed_out', None, 'DEADLINE_EXCEEDED'
            elif status == 'timed_out':
                code = 'DEADLINE_EXCEEDED'
            elif status == 'cancelled':
                code = 'CANCELLED'
            cleanup = self._stop() if status != 'result' or result.get('code') == 'NOT_READY' else 0.0
            return ExecutionOutcome(status, result, code, time.monotonic() - began, cleanup, tuple(events))
        finally:
            self._lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
