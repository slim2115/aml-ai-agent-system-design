"""Bounded local PostgreSQL access and guarded synthetic-dataset bootstrap.

No database URL or caller-selected SQL identifier is accepted. Reset is explicit;
ordinary reads, API startup and bootstrap never discard practice work.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from src.qa_profile import checked_path, get_qa_profile
from src.runtime.io_budget import BudgetExpired, check_budget, current_budget

ROOT = Path(__file__).resolve().parents[2]
MARKER = 'aml-ai-agent-system-rebuild:sql-api-lab:v1:synthetic-only'
SNAPSHOT_ID = 'demo-de73d2a245ca4ba7cd221975'
MANIFEST_SHA256 = '8e479353ad648bd18864f9afae20e3798592795fb72a7e9a965124ea89324723'
TABLES = ('clients', 'cards', 'incidents', 'transactions', 'rules', 'case_notes')
ROLE_NAMES = {'admin': 'aml_lab_admin', 'reader': 'aml_reader',
              'student': 'aml_student', 'api': 'aml_api'}
SEED_COLUMNS = {
    'clients': ('client_id', 'risk_category', 'kyc_status', 'client_since'),
    'cards': ('card_id', 'client_id', 'card_status'),
    'incidents': ('incident_id', 'case_id', 'client_id', 'alert_type', 'created_at'),
    'transactions': ('tx_id', 'client_id', 'card_id', 'amount', 'currency',
                     'counterparty', 'purpose', 'channel', 'status', 'timestamp'),
    'rules': ('rule_id', 'version', 'title', 'regulation_ref', 'category', 'text', 'effective_from'),
}


class LabDatabaseError(RuntimeError):
    """Safe public code; original driver text is never included in the message."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _BudgetConnection(psycopg.Connection):
    """Pinned psycopg 3.3.6 wait timeout covers guard SQL, result I/O and commit.

    Closing on any interrupted wait avoids a new cancellation/rollback request
    with its own timeout. An interrupted commit may already have committed;
    callers reconcile by the original event/job identity on the next tick.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._io_budget = current_budget()

    def wait(self, gen, interval=0.1, timeout=None):
        budget = self._io_budget
        if budget is None:
            return super().wait(gen, interval=interval, timeout=timeout)
        try:
            remaining = budget.remaining()
            result = psycopg.waiting.wait(gen, self.pgconn.socket, interval=interval,
                timeout=remaining if timeout is None else min(remaining, timeout))
            budget.check()
            return result
        except psycopg.errors._WaitTimeout:
            self.close()
            raise BudgetExpired() from None
        except BaseException:
            self.close()
            raise


def _identity() -> tuple[str, str, dict[str, str]]:
    profile = get_qa_profile()
    return ((profile.db_name, profile.marker, profile.role_names) if profile else
            ('aml_lab', MARKER, ROLE_NAMES))


def _profile_sql(command: str) -> str:
    """Adapt only fixed, source-owned identifier tokens; quote QA identifiers.

    Callers supply trusted SQL templates, never user SQL or arbitrary strings.
    Keeping the default template unchanged also preserves the ordinary stand.
    """
    profile = get_qa_profile()
    if profile is None:
        return command
    identifiers = {ROLE_NAMES[key]: name for key, name in profile.role_names.items()}
    identifiers['aml_lab'] = profile.db_name
    pattern = r'\b(' + '|'.join(map(re.escape, identifiers)) + r')\b'
    return re.sub(pattern, lambda match: sql.Identifier(identifiers[match.group()]).as_string(), command)


def _safe_code(exc: psycopg.Error) -> str:
    state = exc.sqlstate or ''
    if state == '23505':
        return 'CONFLICT'
    if state.startswith('23') or state.startswith('22'):
        return 'CONSTRAINT_VIOLATION'
    if state.startswith('08') or state in ('57014', '57P01', '57P02', '57P03', '55P03'):
        return 'UNAVAILABLE'
    if state in ('42501', '42P01', '3F000'):
        return 'INVALID_STATE'
    if isinstance(exc, psycopg.OperationalError):
        return 'UNAVAILABLE'
    return 'INTERNAL_ERROR'


def _settings(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        from src.lab.config import load_lab_settings
        value = load_lab_settings()
    profile = get_qa_profile()
    database, marker, role_names = _identity()
    if (not isinstance(value, dict) or value.get('host') != '127.0.0.1'
            or type(value.get('port')) is not int or value['port'] != (profile.pg_port if profile else 55432)
            or value.get('database') != database):
        raise LabDatabaseError('INVALID_STATE')
    if profile and (value.get('run_id') != profile.run_id or value.get('marker') != marker):
        raise LabDatabaseError('INVALID_STATE')
    roles = value.get('roles')
    if not isinstance(roles, dict) or (profile and set(roles) != set(role_names)):
        raise LabDatabaseError('INVALID_STATE')
    for key, expected in role_names.items():
        role = roles.get(key)
        if (not isinstance(role, dict) or role.get('user') != expected
                or not isinstance(role.get('password'), str)
                or not 16 <= len(role['password']) <= 256
                or '\x00' in role['password']):
            raise LabDatabaseError('INVALID_STATE')
    # Keep a private copy so callers cannot redirect a live database object.
    result = {'host': value['host'], 'port': value['port'], 'database': value['database'],
              'roles': {key: dict(roles[key]) for key in role_names}}
    if profile:
        result.update(run_id=profile.run_id, marker=marker)
    return result


class LabDatabase:
    def __init__(self, settings: dict[str, Any] | None = None):
        self._settings = _settings(settings)

    def _open(self, role: str, *, maintenance: bool = False,
              autocommit: bool = False) -> psycopg.Connection:
        if role not in ROLE_NAMES or (maintenance and role != 'admin'):
            raise LabDatabaseError('INVALID_STATE')
        # Revalidate against the active profile before any socket is opened.
        cfg = _settings(self._settings)
        creds = cfg['roles'][role]
        budget = current_budget()
        connect_timeout = 3
        if budget is not None:
            # libpq clamps nonzero values below two seconds to two seconds.
            # Never silently expand a sub-two-second remainder.
            remaining = budget.remaining()
            if remaining < 2:
                raise BudgetExpired()
            connect_timeout = math.floor(min(3, remaining))
        connector = _BudgetConnection.connect if budget is not None else psycopg.connect
        connection = connector(
            host=cfg['host'], hostaddr='127.0.0.1', port=cfg['port'],
            dbname='postgres' if maintenance else cfg['database'],
            user=creds['user'], password=creds['password'],
            connect_timeout=connect_timeout, sslmode='disable', application_name='aml-local-lab',
            options='-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=2000 '
                    '-c idle_in_transaction_session_timeout=30000 -c search_path=pg_catalog',
            row_factory=dict_row, autocommit=autocommit,
        )
        try:
            check_budget()
        except BudgetExpired:
            connection.close()
            raise
        return connection

    @contextmanager
    def connect(self, role: str = 'api') -> Iterator[psycopg.Connection]:
        """Commit successful work; rollback failures; always close the connection."""
        try:
            if current_budget() is None:
                with self._open(role) as connection:
                    yield connection
            else:
                connection = self._open(role)
                try:
                    yield connection
                    check_budget()
                    connection.commit()
                    check_budget()
                finally:
                    # No rollback round trip after an expired/failed operation.
                    # A close releases an open transaction without waiting.
                    connection.close()
        except psycopg.Error as exc:
            check_budget()
            raise LabDatabaseError(_safe_code(exc)) from None

    def status(self) -> dict[str, Any]:
        with self.connect('reader') as conn:
            _assert_marker(conn)
            result = {'database': self._settings['database'], 'purpose': 'synthetic_learning_only',
                      'snapshot_id': SNAPSHOT_ID, 'schemas': {}}
            for schema in ('reference', 'practice'):
                _assert_schema_marker(conn, schema)
                result['schemas'][schema] = {
                    table: conn.execute(sql.SQL('SELECT count(*) AS n FROM {}.{}').format(
                        sql.Identifier(schema), sql.Identifier(table))).fetchone()['n']
                    for table in TABLES
                }
            return result


def _assert_marker(conn: psycopg.Connection) -> None:
    row = conn.execute("SELECT current_database() AS name, "
                       "shobj_description(oid, 'pg_database') AS marker "
                       "FROM pg_database WHERE datname = current_database()").fetchone()
    database, marker, _roles = _identity()
    if not row or row['name'] != database or row['marker'] != marker:
        raise LabDatabaseError('INVALID_STATE')


def _assert_schema_marker(conn: psycopg.Connection, schema: str) -> None:
    row = conn.execute("SELECT obj_description(oid, 'pg_namespace') AS marker "
                       "FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone()
    if not row or row['marker'] != _identity()[1]:
        raise LabDatabaseError('INVALID_STATE')


def verified_seed() -> dict[str, list[dict[str, Any]]]:
    """Pin the manifest itself and each source before parsing any fixture data."""
    profile = get_qa_profile()
    source = profile.seed_dir if profile else ROOT / 'data' / 'demo-v1'
    manifest_path = source / 'manifest.json'
    if profile:
        checked_path(manifest_path, profile.root)
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != MANIFEST_SHA256:
        raise LabDatabaseError('INVALID_STATE')
    manifest = json.loads(manifest_bytes)
    if manifest['snapshot_id'] != SNAPSHOT_ID or manifest['purpose'] != 'synthetic_learning_only':
        raise LabDatabaseError('INVALID_STATE')
    expected = {item['name']: item for item in manifest['files']}
    output = {}
    for table in SEED_COLUMNS:
        filename = 'cases.json' if table == 'incidents' else table + '.json'
        source_path = source / filename
        if profile:
            checked_path(source_path, profile.root)
        raw = source_path.read_bytes()
        entry = expected[filename]
        if hashlib.sha256(raw).hexdigest() != entry['content_sha256']:
            raise LabDatabaseError('INVALID_STATE')
        rows = json.loads(raw, parse_float=Decimal)
        if not isinstance(rows, list) or len(rows) != entry['row_count']:
            raise LabDatabaseError('INVALID_STATE')
        for row in rows:
            if set(row) != set(SEED_COLUMNS[table]):
                raise LabDatabaseError('INVALID_STATE')
            if table == 'transactions':
                if not isinstance(row['amount'], str):
                    raise LabDatabaseError('INVALID_STATE')
                row['amount'] = Decimal(row['amount'])
        output[table] = rows
    return output


def _create_schema(conn: psycopg.Connection, schema: str,
                   seed: dict[str, list[dict[str, Any]]]) -> None:
    if schema not in ('reference', 'practice'):
        raise LabDatabaseError('INVALID_STATE')
    conn.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION {}').format(
        sql.Identifier(schema), sql.Identifier(_identity()[2]['admin'])))
    conn.execute(sql.SQL('COMMENT ON SCHEMA {} IS {}').format(sql.Identifier(schema), sql.Literal(_identity()[1])))
    ddl = (ROOT / 'lab' / 'sql' / 'schema.sql').read_text(encoding='utf-8')
    conn.execute(ddl.replace('__SCHEMA__', schema))
    for table, columns in SEED_COLUMNS.items():
        command = sql.SQL('INSERT INTO {}.{} ({}) VALUES ({})').format(
            sql.Identifier(schema), sql.Identifier(table),
            sql.SQL(', ').join(map(sql.Identifier, columns)),
            sql.SQL(', ').join(sql.Placeholder() for _ in columns))
        with conn.cursor() as cursor:
            cursor.executemany(command, [tuple(row[col] for col in columns) for row in seed[table]])
    _grants(conn, schema)


def _grants(conn: psycopg.Connection, schema: str) -> None:
    conn.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM PUBLIC').format(sql.Identifier(schema)))
    conn.execute(sql.SQL(_profile_sql('GRANT USAGE ON SCHEMA {} TO aml_reader, aml_student, aml_api')).format(sql.Identifier(schema)))
    conn.execute(sql.SQL(_profile_sql('GRANT SELECT ON ALL TABLES IN SCHEMA {} TO aml_reader, aml_student, aml_api')).format(sql.Identifier(schema)))
    if schema == 'practice':
        conn.execute(_profile_sql('GRANT CREATE ON SCHEMA practice TO aml_student'))
        for table in TABLES:
            conn.execute(sql.SQL(_profile_sql('ALTER TABLE practice.{} OWNER TO aml_student')).format(sql.Identifier(table)))
        conn.execute(_profile_sql('GRANT INSERT, UPDATE, DELETE ON practice.case_notes TO aml_api'))
        conn.execute(_profile_sql('ALTER DEFAULT PRIVILEGES FOR ROLE aml_student IN SCHEMA practice '
                                 'GRANT SELECT ON TABLES TO aml_reader, aml_api'))


def bootstrap(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create only this owned database, named roles and missing owned schemas."""
    seed = verified_seed()  # No server mutation before source verification.
    db = LabDatabase(settings)
    cfg = db._settings
    database, marker, _roles = _identity()
    try:
        with db._open('admin', maintenance=True, autocommit=True) as conn:
            # Serialize bootstrap attempts in this dedicated cluster.
            conn.execute('SELECT pg_advisory_lock(731221504)')
            existing = conn.execute("SELECT shobj_description(oid, 'pg_database') AS marker "
                                    "FROM pg_database WHERE datname = %s", (database,)).fetchone()
            if existing and existing['marker'] != marker:
                raise LabDatabaseError('INVALID_STATE')
            for role in ('reader', 'student', 'api'):
                creds = cfg['roles'][role]
                present = conn.execute("SELECT shobj_description(oid, 'pg_authid') AS marker "
                                       "FROM pg_roles WHERE rolname = %s", (creds['user'],)).fetchone()
                if present and present['marker'] != marker:
                    raise LabDatabaseError('INVALID_STATE')
                if not present:
                    conn.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                                         'NOREPLICATION NOBYPASSRLS PASSWORD {}').format(
                        sql.Identifier(creds['user']), sql.Literal(creds['password'])))
                    conn.execute(sql.SQL('COMMENT ON ROLE {} IS {}').format(
                        sql.Identifier(creds['user']), sql.Literal(marker)))
                # Repeated bootstrap does not rotate credentials or erase work.
            if not existing:
                conn.execute(_profile_sql('CREATE DATABASE aml_lab OWNER aml_lab_admin TEMPLATE template0 ENCODING \'UTF8\''))
                conn.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
                    sql.Identifier(database), sql.Literal(marker)))
        with db.connect('admin') as conn:
            _assert_marker(conn)
            conn.execute('SELECT pg_advisory_xact_lock(731221505)')
            conn.execute(_profile_sql('REVOKE ALL ON DATABASE aml_lab FROM PUBLIC'))
            conn.execute(_profile_sql('GRANT CONNECT ON DATABASE aml_lab TO aml_reader, aml_student, aml_api'))
            conn.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC')
            for schema in ('reference', 'practice'):
                exists = conn.execute('SELECT 1 FROM pg_namespace WHERE nspname = %s', (schema,)).fetchone()
                if exists:
                    _assert_schema_marker(conn, schema)
                else:
                    _create_schema(conn, schema, seed)
        return db.status()
    except psycopg.Error as exc:
        raise LabDatabaseError(_safe_code(exc)) from None


def reset_practice(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Explicit, atomic reset confined to the previously marked practice schema."""
    seed = verified_seed()
    db = LabDatabase(settings)
    with db.connect('admin') as conn:
        _assert_marker(conn)
        _assert_schema_marker(conn, 'practice')
        _assert_schema_marker(conn, 'reference')
        conn.execute('SELECT pg_advisory_xact_lock(731221505)')
        conn.execute('DROP SCHEMA practice CASCADE')
        _create_schema(conn, 'practice', seed)
    return db.status()
