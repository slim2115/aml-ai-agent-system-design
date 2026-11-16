"""Durable Kafka intake and bounded scheduling of the shared AML workflow.

Kafka acknowledges PostgreSQL intake, not completion of an AML investigation.
No HTTP call or model execution runs inside the intake transaction. The durable
queue is revisited even when Kafka is idle; a stable event UUID also identifies
the workflow admission, including retries after an ambiguous HTTP response.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Callable

from pydantic import ValidationError

from src.lab.database import LabDatabaseError
from src.runtime.io_budget import BudgetExpired, Deadline, check_budget, use_budget
from src.workflow.auth import trusted_local_principal
from src.workflow.store import WorkflowError
from .analysis_contracts import DLQ_TOPIC, GROUP, TOPIC, AnalysisEvent
from .consumer import (
    DLQ_KEY_PREVIEW_BYTES, DLQ_VALUE_PREVIEW_BYTES, MAX_EVENT_BYTES,
    WorkerFailure, _commit, _print_progress, _source,
)
from .contracts import Record
from .store import EventConflict, UnknownIncident


MAX_KEY_BYTES = 1024
SCHEDULE_INTERVAL_SECONDS = 1.0
# A local scheduling limit, not an AML SLA or a Kafka poll-interval guarantee.
SCHEDULER_BUDGET_SECONDS = 10.0
TRANSIENT_CODES = frozenset({
    'NOT_READY', 'BUSY', 'DEPENDENCY_UNAVAILABLE', 'DEADLINE_EXCEEDED', 'INTERNAL_ERROR',
})
PERMANENT_CODES = frozenset({
    'MISSING_INPUT', 'INVALID_INPUT_FORMAT', 'NOT_FOUND',
    'VERSION_CONFLICT', 'IDEMPOTENCY_CONFLICT', 'INVALID_STATE',
})


def _dead_letter(record: Record, broker: Any, reason: str) -> dict[str, Any]:
    value, key = record.value or b'', record.key or b''
    envelope = {
        'schema_version': 1, 'synthetic': True, 'reason_code': reason,
        'source': _source(record),
        'value_base64': base64.b64encode(value[:DLQ_VALUE_PREVIEW_BYTES]).decode('ascii'),
        'value_bytes': len(value), 'value_truncated': len(value) > DLQ_VALUE_PREVIEW_BYTES,
        'value_is_null': record.value is None,
        'key_base64': base64.b64encode(key[:DLQ_KEY_PREVIEW_BYTES]).decode('ascii'),
        'key_bytes': len(key), 'key_truncated': len(key) > DLQ_KEY_PREVIEW_BYTES,
        'key_is_null': record.key is None,
    }
    encoded = json.dumps(envelope, sort_keys=True, separators=(',', ':')).encode('utf-8')
    source_key = f'{record.topic}:{record.partition}:{record.offset}'.encode('ascii')
    try:
        broker.publish(DLQ_TOPIC, key=source_key, value=encoded)
    except Exception:
        raise WorkerFailure('DLQ_PUBLISH_FAILED') from None
    _commit(record, broker)
    return {'status': 'dead_lettered', 'reason_code': reason, **_source(record)}


def process_one(record: Record, store: Any, broker: Any) -> dict[str, Any]:
    """Commit only after durable intake or an acknowledged bounded DLQ copy."""
    if record.topic != TOPIC:
        raise WorkerFailure('UNEXPECTED_TOPIC')
    if record.value is None:
        return _dead_letter(record, broker, 'TOMBSTONE')
    if len(record.value) > MAX_EVENT_BYTES:
        return _dead_letter(record, broker, 'EVENT_TOO_LARGE')
    if record.key is None or len(record.key) > MAX_KEY_BYTES:
        return _dead_letter(record, broker, 'INVALID_KEY')
    try:
        event = AnalysisEvent.model_validate_json(record.value)
    except (ValidationError, ValueError, TypeError):
        return _dead_letter(record, broker, 'INVALID_EVENT')
    if record.key != event.incident_id.encode('utf-8'):
        return _dead_letter(record, broker, 'INVALID_KEY')
    try:
        result = store.enqueue(event, record)
    except EventConflict:
        return _dead_letter(record, broker, 'EVENT_CONFLICT')
    except UnknownIncident:
        return _dead_letter(record, broker, 'UNKNOWN_INCIDENT')
    except LabDatabaseError:
        raise WorkerFailure('DATABASE_UNAVAILABLE') from None
    except Exception:
        raise WorkerFailure('DATABASE_PROCESS_FAILED') from None
    if not isinstance(result, dict) or result.get('status') not in ('queued', 'duplicate'):
        raise WorkerFailure('INVALID_PROCESS_RESULT')
    _commit(record, broker)
    return {'status': result['status'], 'event_id': str(event.event_id), **_source(record)}


def _database(method: Callable, *args, **kwargs):
    check_budget()
    try:
        result = method(*args, **kwargs)
        check_budget()
        return result
    except BudgetExpired:
        raise
    except LabDatabaseError:
        check_budget()
        raise WorkerFailure('DATABASE_UNAVAILABLE') from None
    except Exception:
        check_budget()
        raise WorkerFailure('DATABASE_PROCESS_FAILED') from None


def _failure(store: Any, event_id: str, error: WorkflowError, *, submitted: bool) -> dict[str, Any]:
    # A credential failure is shared configuration, not a bad event. Stop once
    # and leave durable work intact for the operator to repair the connection.
    if error.code in ('UNAUTHORIZED', 'FORBIDDEN'):
        raise WorkerFailure('WORKFLOW_' + error.code) from None
    if error.code == 'PROTOCOL_ERROR':
        _database(store.defer, event_id, error.code)
        raise WorkerFailure('WORKFLOW_PROTOCOL_ERROR') from None
    if error.code in TRANSIENT_CODES:
        _database(store.defer, event_id, error.code)
        return {'status': 'deferred', 'event_id': event_id, 'code': error.code}
    if error.code in PERMANENT_CODES:
        if submitted:
            # Keep the accepted job reference. Missing/inaccessible workflow
            # state requires repair, never resubmission as a new investigation.
            _database(store.defer, event_id, error.code)
            raise WorkerFailure('WORKFLOW_JOB_UNAVAILABLE') from None
        _database(store.reject, event_id, error.code)
        return {'status': 'rejected', 'event_id': event_id, 'code': error.code}
    raise WorkerFailure('WORKFLOW_PROTOCOL_ERROR') from None


def scheduler_tick(store: Any, client: Any, principal=None, *,
                   budget_seconds: float = SCHEDULER_BUDGET_SECONDS,
                   clock: Callable[[], float] = time.monotonic) -> list[dict[str, Any]]:
    """Project one submitted job, then admit one queued request; never prepare.

    A shared monotonic budget bounds production HTTP/PG waits. Expiry preserves
    the durable row and original admission key; the next tick reconciles them.
    Intake, offset/DLQ acknowledgement and poll itself are outside this budget,
    so this is explicitly not a max.poll.interval guarantee or an AML SLA.
    """
    progress = []
    try:
        with use_budget(Deadline.after(budget_seconds, clock=clock)):
            _scheduler_tick(store, client, principal, progress)
    except BudgetExpired:
        # No fresh defer/rollback/HTTP operation after expiry. The last commit
        # or admission may be ambiguous; idempotent reconciliation is required.
        progress.append({'status': 'budget_exhausted', 'code': 'SCHEDULER_BUDGET_EXHAUSTED'})
    return progress


def _scheduler_tick(store: Any, client: Any, principal, progress: list) -> None:
    principal = principal if principal is not None else trusted_local_principal()
    for stage, method in (('submitted', store.submitted), ('queued', store.pending)):
        rows = _database(method, limit=1)
        if not isinstance(rows, list) or len(rows) > 1:
            raise WorkerFailure('INVALID_QUEUE_RESULT')
        if not rows:
            continue
        row = rows[0]
        try:
            event = AnalysisEvent.model_validate(row['event_json'])
            event_id = str(event.event_id)
            if str(row['event_id']) != event_id:
                raise ValueError('MISMATCH')
            if stage == 'submitted' and not isinstance(row['job_id'], str):
                raise ValueError('MISSING_JOB')
        except (KeyError, TypeError, ValueError):
            raise WorkerFailure('INVALID_QUEUE_RECORD') from None
        check_budget()
        try:
            if stage == 'submitted':
                job = client.get_job(row['job_id'])
            else:
                job = client.submit(event.job_request(), principal, event.idempotency_key,
                                    engine=event.engine)
            check_budget()
        except BudgetExpired:
            raise
        except WorkflowError as exc:
            check_budget()
            progress.append(_failure(store, event_id, exc, submitted=stage == 'submitted'))
            continue
        except Exception:
            check_budget()
            # Only the adapter can distinguish an ambiguous transport result.
            # Unexpected programming/protocol failures must not silently spin.
            raise WorkerFailure('WORKFLOW_CLIENT_FAILED') from None
        _database(store.record_job, event_id, job)
        # Do not echo HTTP bodies, model output or credentials into logs.
        progress.append({'status': 'projected' if stage == 'submitted' else 'submitted',
                         'event_id': event_id})


def run_worker(
    store: Any, broker: Any, client: Any, *, principal=None,
    stop: Callable[[], bool] = lambda: False,
    emit: Callable[[dict[str, Any]], None] = _print_progress,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run foreground intake and periodic scheduling; Ctrl-C preserves the queue."""
    principal = principal if principal is not None else trusted_local_principal()
    exit_code, next_tick = 0, 0.0
    try:
        emit({'status': 'started', 'group': GROUP, 'topic': TOPIC})
        while not stop():
            try:
                record = broker.poll(timeout=1)
            except Exception:
                raise WorkerFailure('BROKER_POLL_FAILED') from None
            if record is not None:
                emit(process_one(record, store, broker))
            if clock() >= next_tick:
                for message in scheduler_tick(store, client, principal):
                    emit(message)
                next_tick = clock() + SCHEDULE_INTERVAL_SECONDS
    except KeyboardInterrupt:
        pass
    except WorkerFailure as exc:
        exit_code = 1
        emit({'status': 'failed', 'code': exc.code})
    except Exception:
        exit_code = 1
        emit({'status': 'failed', 'code': 'WORKER_INTERNAL_ERROR'})
    finally:
        try:
            broker.close()
        except Exception:
            exit_code = 1
            emit({'status': 'failed', 'code': 'BROKER_CLOSE_FAILED'})
    if exit_code == 0:
        emit({'status': 'stopped'})
    return exit_code
