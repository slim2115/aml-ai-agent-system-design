"""Durable inbox and read projection for Kafka-triggered AML workflow jobs.

Kafka offsets may advance after enqueue commits. HTTP scheduling and terminal
reconciliation happen separately and never run inside a database transaction.
SQLite remains the authority for reports, decisions and delivery controls.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from src.contracts.semantics import canonical_digest, validate_job
from src.lab.database import LabDatabase, LabDatabaseError, _profile_sql
from src.qa_profile import get_qa_profile
from .analysis_contracts import AnalysisEvent, TOPIC
from .contracts import Record
from .store import EventConflict, INITIALIZE_LOCK, _schema_owner, _assert_base, _assert_owned, _wire

ROOT = Path(__file__).resolve().parents[3]
ANALYSIS_MARKER = 'aml-ai-agent-system-rebuild:kafka-analysis:v1:synthetic-only'
TABLES = ('analysis_requests', 'analysis_deliveries')
TERMINAL = frozenset(('succeeded', 'refused', 'failed', 'cancelled', 'timed_out'))
FIELDS = ('event_id', 'incident_id', 'engine', 'canonical_sha256', 'event_json', 'status',
          'job_id', 'job_revision', 'job_json', 'report_id', 'report_version', 'content_sha256',
          'last_error_code', 'created_at', 'updated_at')
SELECT = 'SELECT ' + ', '.join(FIELDS) + ' FROM kafka_lab.analysis_requests '


def _analysis_marker() -> str:
    profile = get_qa_profile()
    return profile.marker + ':analysis' if profile else ANALYSIS_MARKER


def _analysis_ddl() -> str:
    command = (ROOT / 'lab/kafka/analysis-schema.sql').read_text(encoding='utf-8')
    profile = get_qa_profile()
    if profile is None:
        return command
    marker = sql.Literal(ANALYSIS_MARKER).as_string()
    topic = sql.Literal('aml.lab.analysis-requested.v1').as_string()
    if command.count(marker) != 2 or command.count(topic) != 1:
        raise LabDatabaseError('INVALID_STATE')
    return command.replace(marker, sql.Literal(_analysis_marker()).as_string()).replace(
        topic, sql.Literal(profile.analysis_topic).as_string())


def _identifier(value: str | UUID) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('INVALID_EVENT_ID') from None


def _limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError('INVALID_LIMIT')


def _relations(conn: Any) -> list[dict]:
    return conn.execute(
        "SELECT c.relname AS name, c.relkind AS kind, pg_get_userbyid(c.relowner) AS owner, "
        "obj_description(c.oid, 'pg_class') AS marker FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname = 'kafka_lab' "
        "AND c.relname IN ('analysis_requests', 'analysis_deliveries')"
    ).fetchall()


def _check_relations(rows: list[dict]) -> None:
    if ({row['name'] for row in rows} != set(TABLES)
            or any(row['kind'] != 'r' or row['owner'] != _schema_owner()
                   or row['marker'] != _analysis_marker() for row in rows)):
        raise LabDatabaseError('INVALID_STATE')


def _guard(conn: Any) -> None:
    _assert_base(conn)
    _assert_owned(conn)
    _check_relations(_relations(conn))


def _existing(conn: Any, identifier: UUID, *, lock=False) -> dict | None:
    return conn.execute(SELECT + 'WHERE event_id = %s' + (' FOR UPDATE' if lock else ''),
                        (identifier,)).fetchone()


def _result(row: dict) -> dict:
    """Reject corrupted identity/result projections instead of exposing them."""
    try:
        event = AnalysisEvent.model_validate(row['event_json'])
        if (str(event.event_id) != str(row['event_id']) or event.incident_id != row['incident_id']
                or event.engine != row['engine']
                or hashlib.sha256(event.canonical_bytes()).hexdigest() != row['canonical_sha256']):
            raise ValueError
        if row['job_json'] is None:
            if row['status'] not in ('queued', 'rejected') or any(
                    row[key] is not None for key in ('job_id', 'job_revision', 'report_id',
                                                    'report_version', 'content_sha256')):
                raise ValueError
        else:
            job = validate_job(row['job_json']).model_dump(mode='json')
            expected_status = 'submitted' if job['status'] in ('queued', 'running') else job['status']
            if (job['job_id'] != row['job_id'] or job['revision'] != row['job_revision']
                    or job['incident_id'] != event.incident_id or row['status'] != expected_status):
                raise ValueError
            expected_report = ((job['result']['report_id'], job['result']['report_version'],
                                canonical_digest(job['result'])) if job['status'] == 'succeeded'
                               else (None, None, None))
            if tuple(row[key] for key in ('report_id', 'report_version', 'content_sha256')) != expected_report:
                raise ValueError
        return _wire({field: row[field] for field in FIELDS if field != 'canonical_sha256'})
    except (ValueError, TypeError, KeyError, AttributeError):
        raise LabDatabaseError('INVALID_STATE') from None


class AnalysisStore:
    def __init__(self, database: LabDatabase):
        self.database = database

    def initialize(self) -> dict:
        with self.database.connect('admin') as conn:
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (INITIALIZE_LOCK,))
            _assert_base(conn)
            _assert_owned(conn)
            rows = _relations(conn)
            created = not rows
            if rows:
                _check_relations(rows)
            else:
                conn.execute(_analysis_ddl())
                _check_relations(_relations(conn))
            conn.execute(_profile_sql('REVOKE ALL ON TABLE kafka_lab.analysis_requests, kafka_lab.analysis_deliveries '
                                     'FROM PUBLIC, aml_reader, aml_student, aml_api'))
            conn.execute(_profile_sql('GRANT SELECT ON TABLE kafka_lab.analysis_requests, kafka_lab.analysis_deliveries '
                                     'TO aml_reader, aml_student, aml_api'))
            conn.execute(_profile_sql('GRANT INSERT, UPDATE ON TABLE kafka_lab.analysis_requests TO aml_api'))
            conn.execute(_profile_sql('GRANT INSERT ON TABLE kafka_lab.analysis_deliveries TO aml_api'))
        return {'schema': 'kafka_lab', 'tables': list(TABLES), 'schema_version': 1,
                'created': created, 'purpose': 'synthetic_learning_only'}

    def enqueue(self, event: AnalysisEvent, record: Record) -> dict:
        event = AnalysisEvent.model_validate(event.model_dump(mode='json'))
        if (record.topic != TOPIC or type(record.partition) is not int or record.partition < 0
                or type(record.offset) is not int or record.offset < 0
                or record.key != event.incident_id.encode('utf-8')):
            raise ValueError('INVALID_ANALYSIS_RECORD')
        digest = hashlib.sha256(event.canonical_bytes()).hexdigest()
        event_lock = int.from_bytes(hashlib.sha256(b'analysis:' + event.event_id.bytes).digest()[:8],
                                    byteorder='big', signed=True)
        with self.database.connect('api') as conn:
            _guard(conn)
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (event_lock,))
            existing = _existing(conn, event.event_id)
            duplicate = existing is not None
            if existing:
                _result(existing)
                if existing['canonical_sha256'] != digest:
                    raise EventConflict()
            else:
                conn.execute('INSERT INTO kafka_lab.analysis_requests '
                             '(event_id, incident_id, engine, canonical_sha256, event_json) '
                             'VALUES (%s, %s, %s, %s, %s)',
                             (event.event_id, event.incident_id, event.engine, digest,
                              Jsonb(event.model_dump(mode='json'))))
            delivered = conn.execute('INSERT INTO kafka_lab.analysis_deliveries '
                '(topic, partition, offset_value, event_id, canonical_sha256, duplicate) '
                'VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (topic, partition, offset_value) DO NOTHING '
                'RETURNING event_id',
                (record.topic, record.partition, record.offset, event.event_id, digest, duplicate)).fetchone()
            if not delivered:
                previous = conn.execute('SELECT event_id, canonical_sha256 FROM kafka_lab.analysis_deliveries '
                    'WHERE topic = %s AND partition = %s AND offset_value = %s',
                    (record.topic, record.partition, record.offset)).fetchone()
                if (not previous or str(previous['event_id']) != str(event.event_id)
                        or previous['canonical_sha256'] != digest):
                    raise EventConflict()
        return {'status': 'duplicate' if duplicate else 'queued', 'event_id': str(event.event_id)}

    def get(self, event_id: str) -> dict | None:
        identifier = _identifier(event_id)
        with self.database.connect('reader') as conn:
            _guard(conn)
            row = _existing(conn, identifier)
            return _result(row) if row else None

    def list_results(self, limit=20) -> list[dict]:
        _limit(limit)
        with self.database.connect('reader') as conn:
            _guard(conn)
            return [_result(row) for row in conn.execute(
                SELECT + 'ORDER BY created_at DESC, event_id LIMIT %s', (limit,)).fetchall()]

    def _work(self, status: str, limit: int) -> list[dict]:
        _limit(limit)
        with self.database.connect('reader') as conn:
            _guard(conn)
            return [_result(row) for row in conn.execute(
                SELECT + 'WHERE status = %s ORDER BY updated_at, event_id LIMIT %s',
                (status, limit)).fetchall()]

    def pending(self, limit=1) -> list[dict]:
        return self._work('queued', limit)

    def submitted(self, limit=1) -> list[dict]:
        return self._work('submitted', limit)

    def record_job(self, event_id: str, jobdict: dict) -> dict:
        identifier = _identifier(event_id)
        try:
            job = validate_job(jobdict).model_dump(mode='json')
        except (ValueError, TypeError):
            raise ValueError('INVALID_ANALYSIS_JOB') from None
        with self.database.connect('api') as conn:
            _guard(conn)
            row = _existing(conn, identifier, lock=True)
            if row is None:
                raise ValueError('ANALYSIS_EVENT_NOT_FOUND')
            _result(row)
            if job['incident_id'] != row['incident_id'] or row['status'] == 'rejected':
                raise ValueError('ANALYSIS_JOB_CONFLICT')
            previous = row['job_json']
            if previous:
                immutable = ('job_id', 'incident_id', 'accepted_at', 'deadline_at', 'schema_version')
                if any(previous[field] != job[field] for field in immutable):
                    raise ValueError('ANALYSIS_JOB_CONFLICT')
                if job['revision'] < previous['revision']:
                    # Independent observers may receive an earlier HTTP reply
                    # after another observer has committed a newer revision.
                    # Preserve the committed result without stopping the worker.
                    return _result(row)
                if job['revision'] == previous['revision'] and job != previous:
                    raise ValueError('ANALYSIS_JOB_CONFLICT')
                if row['status'] in TERMINAL:
                    if job != previous:
                        raise ValueError('ANALYSIS_JOB_CONFLICT')
                    return _result(row)
                if previous['status'] == 'running' and job['status'] == 'queued':
                    raise ValueError('ANALYSIS_JOB_CONFLICT')
            status = 'submitted' if job['status'] in ('queued', 'running') else job['status']
            report = job.get('result') if status == 'succeeded' else None
            changed = conn.execute('UPDATE kafka_lab.analysis_requests SET status = %s, job_id = %s, '
                'job_revision = %s, job_json = %s, report_id = %s, report_version = %s, '
                'content_sha256 = %s, last_error_code = NULL, updated_at = clock_timestamp() '
                'WHERE event_id = %s RETURNING ' + ', '.join(FIELDS),
                (status, job['job_id'], job['revision'], Jsonb(job),
                 report['report_id'] if report else None, report['report_version'] if report else None,
                 canonical_digest(report) if report else None, identifier)).fetchone()
            return _result(changed)

    def _error(self, event_id: str, code: str, *, reject: bool) -> dict:
        identifier = _identifier(event_id)
        if not isinstance(code, str) or re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', code) is None:
            raise ValueError('INVALID_ANALYSIS_ERROR_CODE')
        with self.database.connect('api') as conn:
            _guard(conn)
            row = _existing(conn, identifier, lock=True)
            if row is None:
                raise ValueError('ANALYSIS_EVENT_NOT_FOUND')
            _result(row)
            # A stale scheduler response must never overwrite a committed job or
            # terminal outcome. Only requests not yet submitted may be rejected.
            if row['status'] not in ('queued', 'submitted') or (reject and row['status'] != 'queued'):
                return _result(row)
            status = 'rejected' if reject else row['status']
            changed = conn.execute('UPDATE kafka_lab.analysis_requests SET status = %s, '
                'last_error_code = %s, updated_at = clock_timestamp() WHERE event_id = %s RETURNING '
                + ', '.join(FIELDS), (status, code, identifier)).fetchone()
            return _result(changed)

    def defer(self, event_id: str, code: str) -> dict:
        return self._error(event_id, code, reject=False)

    def reject(self, event_id: str, code: str) -> dict:
        return self._error(event_id, code, reject=True)
