"""Transactional, idempotent synthetic Kafka projection in its own namespace.

The caller commits Kafka offsets only after process() returns. PostgreSQL and
Kafka are not one transaction: redelivery is expected and deduplicated here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from src.lab.database import (
    LabDatabase, LabDatabaseError, SNAPSHOT_ID, _assert_marker, _assert_schema_marker,
    _identity, _profile_sql,
)
from src.qa_profile import get_qa_profile
from src.lab.kafka.contracts import Record, ReviewEvent

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_MARKER = 'aml-ai-agent-system-rebuild:kafka-lab:v1:synthetic-only'
SCHEMA_OWNER = 'aml_lab_admin'
TABLES = ('deliveries', 'processed_events')
INITIALIZE_LOCK = 731221506


def _schema_marker() -> str:
    profile = get_qa_profile()
    return profile.marker + ':kafka' if profile else SCHEMA_MARKER


def _schema_owner() -> str:
    return _identity()[2]['admin']


def _schema_ddl() -> str:
    command = (ROOT / 'lab/kafka/schema.sql').read_text(encoding='utf-8')
    if get_qa_profile() is None:
        return command
    marker = sql.Literal(SCHEMA_MARKER).as_string()
    if command.count(marker) != 1:
        raise LabDatabaseError('INVALID_STATE')
    return _profile_sql(command).replace(marker, sql.Literal(_schema_marker()).as_string())


class EventConflict(RuntimeError):
    """An event UUID or Kafka coordinate was reused for a different event."""
    def __init__(self):
        super().__init__('KAFKA_EVENT_CONFLICT')


class UnknownIncident(RuntimeError):
    """The synthetic immutable snapshot has no matching incident."""
    def __init__(self):
        super().__init__('KAFKA_UNKNOWN_INCIDENT')


def _schema(conn: Any) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT obj_description(oid, 'pg_namespace') AS marker, "
        "pg_get_userbyid(nspowner) AS owner FROM pg_namespace WHERE nspname = 'kafka_lab'"
    ).fetchone()


def _assert_owned(conn: Any) -> None:
    row = _schema(conn)
    owner = _schema_owner()
    if not row or row['marker'] != _schema_marker() or row['owner'] != owner:
        raise LabDatabaseError('INVALID_STATE')
    rows = conn.execute(
        "SELECT c.relname AS name, c.relkind AS kind, pg_get_userbyid(c.relowner) AS owner "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'kafka_lab' AND c.relname IN ('deliveries', 'processed_events')"
    ).fetchall()
    if ({row['name'] for row in rows} != set(TABLES)
            or any(row['kind'] != 'r' or row['owner'] != owner for row in rows)):
        raise LabDatabaseError('INVALID_STATE')


def _assert_base(conn: Any) -> None:
    _assert_marker(conn)
    _assert_schema_marker(conn, 'reference')


def _wire(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_wire(item) for item in value]
    return value


def _result(row: dict[str, Any]) -> dict[str, Any]:
    return _wire({
        'event_id': row['event_id'], 'incident_id': row['incident_id'],
        'occurred_at': row['occurred_at'], 'processed_at': row['processed_at'],
        'topic': row['topic'], 'partition': row['partition'], 'offset': row['offset_value'],
        'result': row['result_json'],
    })


def _existing(conn: Any, event_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        'SELECT event_id, incident_id, occurred_at, processed_at, canonical_sha256, '
        'topic, partition, offset_value, result_json FROM kafka_lab.processed_events '
        'WHERE event_id = %s', (event_id,),
    ).fetchone()


def _grants(conn: Any) -> None:
    conn.execute(_profile_sql('REVOKE ALL ON SCHEMA kafka_lab FROM PUBLIC, aml_reader, aml_student, aml_api'))
    conn.execute(_profile_sql('GRANT USAGE ON SCHEMA kafka_lab TO aml_reader, aml_student, aml_api'))
    conn.execute(_profile_sql('REVOKE ALL ON TABLE kafka_lab.processed_events, kafka_lab.deliveries '
                            'FROM PUBLIC, aml_reader, aml_student, aml_api'))
    conn.execute(_profile_sql('GRANT SELECT ON TABLE kafka_lab.processed_events, kafka_lab.deliveries '
                            'TO aml_reader, aml_student, aml_api'))
    conn.execute(_profile_sql('GRANT INSERT ON TABLE kafka_lab.processed_events, kafka_lab.deliveries TO aml_api'))


class KafkaStore:
    def __init__(self, database: LabDatabase):
        self.database = database

    def initialize(self) -> dict[str, Any]:
        """Create an absent namespace, or verify it and preserve all event rows."""
        with self.database.connect('admin') as conn:
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (INITIALIZE_LOCK,))
            _assert_base(conn)
            existed = _schema(conn) is not None
            if not existed:
                conn.execute(_schema_ddl())
            _assert_owned(conn)
            _grants(conn)
        return {'schema': 'kafka_lab', 'schema_version': 1, 'created': not existed,
                'purpose': 'synthetic_learning_only'}

    def status(self) -> dict[str, Any]:
        with self.database.connect('reader') as conn:
            _assert_base(conn)
            _assert_owned(conn)
            counts = conn.execute(
                'SELECT (SELECT count(*) FROM kafka_lab.processed_events) AS processed_events, '
                '(SELECT count(*) FROM kafka_lab.deliveries) AS deliveries'
            ).fetchone()
        return {'schema': 'kafka_lab', 'schema_version': 1,
                'purpose': 'synthetic_learning_only', **counts}

    def process(self, event: ReviewEvent, record: Record) -> dict[str, Any]:
        """Durably project once per canonical event; each Kafka coordinate is unique."""
        digest = hashlib.sha256(event.canonical_bytes()).hexdigest()
        # Hash collisions merely serialize unrelated events; the UUID remains the PK.
        event_lock = int.from_bytes(hashlib.sha256(event.event_id.bytes).digest()[:8],
                                    byteorder='big', signed=True)
        with self.database.connect('api') as conn:
            _assert_base(conn)
            _assert_owned(conn)
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (event_lock,))
            existing = _existing(conn, event.event_id)
            duplicate = existing is not None
            if existing and existing['canonical_sha256'] != digest:
                raise EventConflict()
            if not existing:
                incident = conn.execute(
                    'SELECT incident_id, client_id FROM reference.incidents WHERE incident_id = %s',
                    (event.incident_id,),
                ).fetchone()
                if not incident:
                    raise UnknownIncident()
                totals = conn.execute(
                    'SELECT currency, count(*) AS transaction_count, sum(amount) AS total_amount '
                    'FROM reference.transactions WHERE client_id = %s GROUP BY currency ORDER BY currency',
                    (incident['client_id'],),
                ).fetchall()
                projection = {
                    'synthetic': True, 'kind': 'snapshot_bookkeeping', 'snapshot_id': SNAPSHOT_ID,
                    'incident_id': event.incident_id, 'client_id': incident['client_id'],
                    'transaction_scope': 'all_client_transactions_in_reference_snapshot_all_statuses',
                    'transaction_totals_by_currency': _wire(totals),
                    'transaction_count': sum(row['transaction_count'] for row in totals),
                }
                existing = conn.execute(
                    'INSERT INTO kafka_lab.processed_events '
                    '(event_id, schema_version, event_type, incident_id, occurred_at, canonical_sha256, '
                    'result_json, topic, partition, offset_value) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) '
                    'RETURNING event_id, incident_id, occurred_at, processed_at, canonical_sha256, '
                    'topic, partition, offset_value, result_json',
                    (event.event_id, event.schema_version, event.event_type, event.incident_id,
                     event.occurred_at, digest, Jsonb(projection), record.topic, record.partition, record.offset),
                ).fetchone()
            delivered = conn.execute(
                'INSERT INTO kafka_lab.deliveries '
                '(topic, partition, offset_value, event_id, canonical_sha256, duplicate) '
                'VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (topic, partition, offset_value) DO NOTHING '
                'RETURNING event_id',
                (record.topic, record.partition, record.offset, event.event_id, digest, duplicate),
            ).fetchone()
            if not delivered:
                previous = conn.execute(
                    'SELECT event_id, canonical_sha256 FROM kafka_lab.deliveries '
                    'WHERE topic = %s AND partition = %s AND offset_value = %s',
                    (record.topic, record.partition, record.offset),
                ).fetchone()
                if (not previous or str(previous['event_id']) != str(event.event_id)
                        or previous['canonical_sha256'] != digest):
                    raise EventConflict()
            result = {**_result(existing), 'status': 'duplicate' if duplicate else 'processed'}
        # The context manager committed both rows before this success is visible.
        return result

    def get(self, event_id: str) -> dict[str, Any] | None:
        try:
            identifier = UUID(event_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError('INVALID_EVENT_ID') from None
        with self.database.connect('reader') as conn:
            _assert_base(conn)
            _assert_owned(conn)
            row = _existing(conn, identifier)
        return _result(row) if row else None

    def list_results(self, limit: int = 20) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('INVALID_LIMIT')
        with self.database.connect('reader') as conn:
            _assert_base(conn)
            _assert_owned(conn)
            rows = conn.execute(
                'SELECT event_id, incident_id, occurred_at, processed_at, canonical_sha256, '
                'topic, partition, offset_value, result_json FROM kafka_lab.processed_events '
                'ORDER BY processed_at DESC, event_id LIMIT %s', (limit,),
            ).fetchall()
        return [_result(row) for row in rows]
