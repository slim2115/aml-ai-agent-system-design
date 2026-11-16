"""At-least-once lab consumer: PostgreSQL commit precedes Kafka offset commit.

A dependency failure ends the worker. Continuing a partition after a failed
record could commit past that record, so restart is the deliberate retry path.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Callable

from pydantic import ValidationError

from src.lab.database import LabDatabaseError
from .contracts import DLQ_TOPIC, GROUP, TOPIC, Record, ReviewEvent
from .store import EventConflict, UnknownIncident


MAX_EVENT_BYTES = 64 * 1024
DLQ_VALUE_PREVIEW_BYTES = 32 * 1024
DLQ_KEY_PREVIEW_BYTES = 256


class WorkerFailure(RuntimeError):
    """Safe, constant public code; dependency exceptions are never logged."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _source(record: Record) -> dict[str, Any]:
    return {
        'topic': record.topic,
        'partition': record.partition,
        'offset': record.offset,
    }


def _commit(record: Record, broker: Any) -> None:
    try:
        broker.commit(record)
    except Exception:
        raise WorkerFailure('OFFSET_COMMIT_FAILED') from None


def _dead_letter(record: Record, broker: Any, reason: str) -> dict[str, Any]:
    """A bounded diagnostic copy, never an exception string or a log payload."""
    value = record.value or b''
    key = record.key or b''
    envelope = {
        'schema_version': 1,
        'synthetic': True,
        'reason_code': reason,
        'source': _source(record),
        'value_base64': base64.b64encode(value[:DLQ_VALUE_PREVIEW_BYTES]).decode('ascii'),
        'value_bytes': len(value),
        'value_truncated': len(value) > DLQ_VALUE_PREVIEW_BYTES,
        'value_is_null': record.value is None,
        'key_base64': base64.b64encode(key[:DLQ_KEY_PREVIEW_BYTES]).decode('ascii'),
        'key_bytes': len(key),
        'key_truncated': len(key) > DLQ_KEY_PREVIEW_BYTES,
        'key_is_null': record.key is None,
    }
    encoded = json.dumps(envelope, sort_keys=True, separators=(',', ':')).encode('utf-8')
    # One DLQ record identifies the input position. A crash after its publish but
    # before the offset commit may produce another DLQ record with the same key.
    source_key = f'{record.topic}:{record.partition}:{record.offset}'.encode('ascii')
    try:
        broker.publish(DLQ_TOPIC, key=source_key, value=encoded)
    except Exception:
        raise WorkerFailure('DLQ_PUBLISH_FAILED') from None
    _commit(record, broker)
    return {'status': 'dead_lettered', 'reason_code': reason, **_source(record)}


def process_one(record: Record, store: Any, broker: Any) -> dict[str, Any]:
    """Process exactly one input; an exception forbids processing later inputs.

    ``store.process`` returns only after its PostgreSQL transaction commits.
    ``broker.publish`` returns only after the broker acknowledges the DLQ write.
    No offset is committed for a dependency failure or an ambiguous DB outcome.
    """
    if record.topic != TOPIC:
        raise WorkerFailure('UNEXPECTED_TOPIC')
    if record.value is None:
        return _dead_letter(record, broker, 'TOMBSTONE')
    if len(record.value) > MAX_EVENT_BYTES:
        return _dead_letter(record, broker, 'EVENT_TOO_LARGE')
    try:
        event = ReviewEvent.model_validate_json(record.value)
    except (ValidationError, ValueError, TypeError):
        return _dead_letter(record, broker, 'INVALID_EVENT')
    if record.key != event.incident_id.encode('utf-8'):
        return _dead_letter(record, broker, 'INVALID_KEY')
    try:
        result = store.process(event, record)
    except EventConflict:
        return _dead_letter(record, broker, 'EVENT_CONFLICT')
    except UnknownIncident:
        return _dead_letter(record, broker, 'UNKNOWN_INCIDENT')
    except LabDatabaseError:
        raise WorkerFailure('DATABASE_UNAVAILABLE') from None
    except Exception:
        raise WorkerFailure('DATABASE_PROCESS_FAILED') from None
    if not isinstance(result, dict) or result.get('status') not in ('processed', 'duplicate'):
        raise WorkerFailure('INVALID_PROCESS_RESULT')
    _commit(record, broker)
    return {
        'status': result['status'],
        'event_id': str(event.event_id),
        **_source(record),
    }


def _print_progress(message: dict[str, Any]) -> None:
    print(json.dumps(message, sort_keys=True, ensure_ascii=True), flush=True)


def run_worker(
    store: Any,
    broker: Any,
    *,
    stop: Callable[[], bool] = lambda: False,
    emit: Callable[[dict[str, Any]], None] = _print_progress,
) -> int:
    """Run in the foreground; Ctrl-C closes Kafka without committing new offsets.

    Returns 0 for an orderly stop and 1 for a dependency/internal failure. The
    caller may restart the process; Kafka then redelivers uncommitted positions.
    """
    exit_code = 0
    try:
        emit({'status': 'started', 'group': GROUP, 'topic': TOPIC})
        while not stop():
            try:
                record = broker.poll(timeout=1)
            except Exception:
                raise WorkerFailure('BROKER_POLL_FAILED') from None
            if record is not None:
                emit(process_one(record, store, broker))
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
