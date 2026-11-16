"""Finite checks against this stand; ordinary probes rollback their changes.

--verify-reset is opt-in and refuses a modified practice dataset. It performs
one reset of freshly seeded practice after checking bootstrap preservation.
Never use this command while another learner or API qualifier is writing.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import psycopg
from psycopg import sql
from src.lab import database as module
from src.lab.database import LabDatabase, LabDatabaseError, TABLES, ROLE_NAMES

CHECKS = []


def check(name, condition, detail=None):
    item = {'name': name, 'status': 'PASS' if condition else 'FAIL'}
    if detail is not None:
        item['detail'] = detail
    CHECKS.append(item)
    if not condition:
        raise AssertionError(name)


def normalized(value):
    if isinstance(value, Decimal):
        return format(value, '.2f')
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return str(value) if hasattr(value, 'hex') else value


def rows(conn, schema, table):
    result = conn.execute(sql.SQL('SELECT * FROM {}.{}').format(
        sql.Identifier(schema), sql.Identifier(table))).fetchall()
    return sorted(normalized(result), key=lambda row: json.dumps(row, sort_keys=True))


def fingerprint(conn, schema):
    content = {table: rows(conn, schema, table) for table in TABLES}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


@contextmanager
def rollback_connection(db, role):
    conn = db._open(role)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def rejected(conn, name, command, expected, params=None):
    conn.execute('SAVEPOINT qualification_probe')
    state = None
    try:
        conn.execute(command, params)
    except psycopg.Error as exc:
        state = exc.sqlstate
    finally:
        conn.execute('ROLLBACK TO SAVEPOINT qualification_probe')
        conn.execute('RELEASE SAVEPOINT qualification_probe')
    check(name, state == expected, {'expected_sqlstate': expected, 'actual_sqlstate': state})


def qualify(db, verify_reset):
    seed = module.verified_seed()
    with rollback_connection(db, 'reader') as conn:
        check('server_version_postgresql_17_11', conn.execute('SHOW server_version_num').fetchone()['server_version_num'] == '170011')
        check('server_listens_only_on_loopback', conn.execute('SHOW listen_addresses').fetchone()['listen_addresses'] == '127.0.0.1')
        check('session_timezone_utc', conn.execute('SHOW timezone').fetchone()['TimeZone'] == 'UTC')
        check('session_search_path_catalog_only', conn.execute('SHOW search_path').fetchone()['search_path'] == 'pg_catalog')
        check('session_statement_timeout_10_seconds', conn.execute('SHOW statement_timeout').fetchone()['statement_timeout'] == '10s')
        for schema in ('reference', 'practice'):
            for table in TABLES:
                expected = sorted(normalized(seed.get(table, [])), key=lambda row: json.dumps(row, sort_keys=True))
                actual = rows(conn, schema, table)
                check(f'{schema}_{table}_exact_seed', actual == expected, {'rows': len(actual)})
        reference_before = fingerprint(conn, 'reference')
        practice_before = fingerprint(conn, 'practice')
        check('no_extra_practice_relations_before_reset', not conn.execute(
            "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='practice' AND c.relkind IN ('r','v','m','f','p','S') "
            "AND c.relname <> ALL(%s)", (list(TABLES),)).fetchall())
        amount = conn.execute("SELECT amount FROM practice.transactions WHERE tx_id='TX-7700'").fetchone()['amount']
        check('numeric_uses_decimal_and_two_digit_scale', isinstance(amount, Decimal) and amount == Decimal('95000.00') and amount.as_tuple().exponent == -2)
        sums = conn.execute('SELECT currency, sum(amount) AS total FROM practice.transactions GROUP BY currency ORDER BY currency').fetchall()
        check('currency_totals_remain_separate_and_exact', sums == [{'currency': 'RUB', 'total': Decimal('1790000.00')}, {'currency': 'USD', 'total': Decimal('1500000.00')}])
        timestamp = conn.execute('SELECT min(timestamp) AS t FROM practice.transactions').fetchone()['t']
        check('timestamp_is_aware_utc', timestamp.utcoffset().total_seconds() == 0)
        roles = conn.execute('SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = ANY(%s)', ([ROLE_NAMES[r] for r in ('reader', 'student', 'api')],)).fetchall()
        check('three_learning_roles_without_admin_capabilities', len(roles) == 3 and all(not any(value for key, value in row.items() if key != 'rolname') for row in roles))

    for role in ('reader', 'student', 'api'):
        with rollback_connection(db, role) as conn:
            for table in TABLES:
                column = 'id' if table == 'case_notes' else next(iter(seed[table][0]))
                rejected(conn, f'{role}_cannot_update_reference_{table}', sql.SQL('UPDATE reference.{} SET {}={} WHERE false').format(sql.Identifier(table), sql.Identifier(column), sql.Identifier(column)), '42501')
            rejected(conn, f'{role}_cannot_create_reference_objects', 'CREATE TABLE reference.qualifier_forbidden(id integer)', '42501')
            rejected(conn, f'{role}_cannot_create_public_objects', 'CREATE TABLE public.qualifier_forbidden(id integer)', '42501')
            rejected(conn, f'{role}_cannot_drop_reference_clients', 'DROP TABLE reference.clients', '42501')
            rejected(conn, f'{role}_cannot_become_admin', 'SET ROLE aml_lab_admin', '42501')
            check(f'{role}_can_read_both_schemas', conn.execute('SELECT (SELECT count(*) FROM reference.clients) AS a, (SELECT count(*) FROM practice.clients) AS b').fetchone() == {'a': 5, 'b': 5})
            if role != 'student':
                rejected(conn, f'{role}_cannot_create_practice_objects', 'CREATE TABLE practice.qualifier_forbidden(id integer)', '42501')
                rejected(conn, f'{role}_cannot_update_practice_clients', 'UPDATE practice.clients SET risk_category=risk_category WHERE false', '42501')
                rejected(conn, f'{role}_cannot_drop_practice_clients', 'DROP TABLE practice.clients', '42501')
            if role == 'reader':
                rejected(conn, 'reader_cannot_add_note', "INSERT INTO practice.case_notes(id,incident_id,body) VALUES (%s,'INC-000123','Qualification only')", '42501', (uuid4(),))

    with rollback_connection(db, 'student') as conn:
        conn.execute('CREATE TABLE practice.qualifier_rollback_table (id integer PRIMARY KEY)')
        conn.execute('INSERT INTO practice.qualifier_rollback_table VALUES (1)')
        check('student_can_create_and_write_practice_tables', conn.execute('SELECT id FROM practice.qualifier_rollback_table').fetchone()['id'] == 1)
        check('student_can_edit_seed_practice_row', conn.execute("UPDATE practice.clients SET risk_category='low' WHERE client_id='C-00045'").rowcount == 1)
        rejected(conn, 'duplicate_client_pk_rejected', "INSERT INTO practice.clients VALUES ('C-00045','low','pending','2026-01-01')", '23505')
        rejected(conn, 'unknown_note_incident_fk_rejected', "INSERT INTO practice.case_notes(id,incident_id,body) VALUES (%s,'INC-999999','Qualification only')", '23503', (uuid4(),))
        rejected(conn, 'blank_note_check_rejected', "INSERT INTO practice.case_notes(id,incident_id,body) VALUES (%s,'INC-000123','   ')", '23514', (uuid4(),))
        rejected(conn, 'oversized_note_check_rejected', "INSERT INTO practice.case_notes(id,incident_id,body) VALUES (%s,'INC-000123',%s)", '23514', (uuid4(), 'x' * 4001))
        rejected(conn, 'mismatched_card_client_fk_rejected', "UPDATE practice.transactions SET card_id='CARD-1002' WHERE tx_id='TX-7700'", '23503')
        rejected(conn, 'negative_amount_check_rejected', "UPDATE practice.transactions SET amount=-1 WHERE tx_id='TX-7700'", '23514')
        rejected(conn, 'nan_amount_check_rejected', "UPDATE practice.transactions SET amount='NaN' WHERE tx_id='TX-7700'", '23514')
        exact = Decimal('123456789012345678901234567890123456.78')
        conn.execute("UPDATE practice.transactions SET amount=%s WHERE tx_id='TX-7700'", (exact,))
        check('numeric_38_2_preserves_large_value_exactly', conn.execute("SELECT amount FROM practice.transactions WHERE tx_id='TX-7700'").fetchone()['amount'] == exact)
        conn.execute("UPDATE practice.transactions SET timestamp='2026-07-11T03:00:00+03' WHERE tx_id='TX-7700'")
        check('timestamptz_normalizes_equivalent_offset', conn.execute("SELECT timestamp FROM practice.transactions WHERE tx_id='TX-7700'").fetchone()['timestamp'] == datetime(2026, 7, 11, tzinfo=timezone.utc))

    with rollback_connection(db, 'api') as conn:
        note_id = uuid4()
        conn.execute("INSERT INTO practice.case_notes(id,incident_id,body) VALUES (%s,'INC-000123','Qualification only')", (note_id,))
        check('api_can_create_own_practice_note', conn.execute('SELECT revision FROM practice.case_notes WHERE id=%s', (note_id,)).fetchone()['revision'] == 1)
        check('api_note_revision_cas_update', conn.execute('UPDATE practice.case_notes SET revision=revision+1,updated_at=clock_timestamp() WHERE id=%s AND revision=1', (note_id,)).rowcount == 1)
        check('stale_note_revision_updates_nothing', conn.execute('UPDATE practice.case_notes SET revision=revision+1 WHERE id=%s AND revision=1', (note_id,)).rowcount == 0)
        check('api_can_delete_own_practice_note', conn.execute('DELETE FROM practice.case_notes WHERE id=%s', (note_id,)).rowcount == 1)

    # Run the actual educational answers, including their own BEGIN/ROLLBACK.
    with db._open('student', autocommit=True) as conn:
        cursor = conn.execute((ROOT / 'lab/sql/solutions.sql').read_text(encoding='utf-8'))
        result_sets = []
        while True:
            if cursor.description:
                result_sets.append(cursor.fetchall())
            if not cursor.nextset():
                break
        check('all_12_solution_sections_execute', len(re.findall(r'^-- (?:0[1-9]|1[0-2]):', (ROOT / 'lab/sql/solutions.sql').read_text(encoding='utf-8'), re.M)) == 12 and len(result_sets) == 13)
        check('solution_01_three_largest_rub', [row['tx_id'] for row in result_sets[0]] == ['TX-8900', 'TX-7700', 'TX-7701'])
        check('solution_03_left_join_keeps_zero_transaction_client', [row['transaction_count'] for row in result_sets[2]] == [12, 1, 1, 1, 0])
        check('solution_06_half_open_utc_window', len(result_sets[5]) == 4)
        check('solution_07_running_amount', result_sets[6][-1]['running_amount'] == Decimal('1140000.00'))
        check('solution_08_last_transaction_keeps_missing_value', result_sets[7][-1]['client_id'] == 'C-00099' and result_sets[7][-1]['tx_id'] is None)
        check('solution_09_missing_transactions_incident', result_sets[8] == [{'incident_id': 'INC-000999', 'client_id': 'C-00099'}])
        check('solution_11_cas_and_rollback', result_sets[10][0]['revision'] == 2 and result_sets[11] == [] and result_sets[12] == [{'remaining_exercise_notes': 0}])

    # Exercise the reset guards against real transactional PostgreSQL comments.
    # Inject only the connection lifetime, so these metadata probes also rollback.
    for target in ('database', 'practice', 'reference'):
        with rollback_connection(db, 'admin') as conn:
            statement = 'COMMENT ON DATABASE aml_lab IS NULL' if target == 'database' else f'COMMENT ON SCHEMA {target} IS NULL'
            conn.execute(statement)

            class TransactionDatabase:
                @contextmanager
                def connect(self, role='api'):
                    yield conn

            code = None
            with patch.object(module, 'LabDatabase', return_value=TransactionDatabase()):
                try:
                    module.reset_practice()
                except LabDatabaseError as exc:
                    code = exc.code
            check(f'reset_refuses_unmarked_{target}', code == 'INVALID_STATE')
            check(f'marker_guard_{target}_preserves_practice_rows', fingerprint(conn, 'practice') == practice_before)

    with rollback_connection(db, 'reader') as conn:
        check('all_rollback_probes_preserve_reference', fingerprint(conn, 'reference') == reference_before)
        check('all_rollback_probes_preserve_practice', fingerprint(conn, 'practice') == practice_before)

    if verify_reset:
        note_id = uuid4()
        created = False
        try:
            with db.connect('student') as conn:
                conn.execute('CREATE TABLE practice.qualifier_reset_marker (id integer PRIMARY KEY)')
                conn.execute('INSERT INTO practice.qualifier_reset_marker VALUES (1)')
                conn.execute("INSERT INTO practice.case_notes(id,incident_id,body) VALUES (%s,'INC-000123','Qualifier reset marker')", (note_id,))
                created = True
            module.bootstrap()
            with rollback_connection(db, 'reader') as conn:
                check('bootstrap_preserves_existing_practice_note', conn.execute('SELECT 1 FROM practice.case_notes WHERE id=%s', (note_id,)).fetchone() is not None)
                check('bootstrap_preserves_student_created_table', conn.execute('SELECT id FROM practice.qualifier_reset_marker').fetchone()['id'] == 1)
            module.reset_practice()
            created = False
            with rollback_connection(db, 'reader') as conn:
                check('explicit_reset_restores_exact_practice_seed', fingerprint(conn, 'practice') == practice_before)
                check('explicit_reset_preserves_reference_exactly', fingerprint(conn, 'reference') == reference_before)
                check('explicit_reset_removes_student_training_table', conn.execute("SELECT to_regclass('practice.qualifier_reset_marker') AS t").fetchone()['t'] is None)
        finally:
            if created:
                # Narrow cleanup of this probe only; no fallback reset on failure.
                with db.connect('student') as conn:
                    conn.execute('DELETE FROM practice.case_notes WHERE id=%s', (note_id,))
                    conn.execute('DROP TABLE IF EXISTS practice.qualifier_reset_marker')
    else:
        CHECKS.append({'name': 'explicit_reset', 'status': 'NOT_RUN', 'detail': 'Use --verify-reset only on a fresh, idle stand.'})
    return db.status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-reset', action='store_true')
    parser.add_argument('--output-dir', type=Path, default=Path('var/checks/lab'),
                        help='Evidence directory inside rebuild; default var/checks/lab')
    args = parser.parse_args()
    directory = (ROOT / args.output_dir).resolve()
    if not directory.is_relative_to(ROOT.resolve()):
        parser.error('--output-dir must remain inside the rebuild directory')
    output = directory / 'sql-qualification.json'
    result = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'scope': 'Real PostgreSQL 17.11 local stand; synthetic fixture; role probes rollback; explicit reset requires a fresh practice baseline', 'explicit_reset_requested': args.verify_reset, 'checks': CHECKS}
    try:
        result['database'] = qualify(LabDatabase(), args.verify_reset)
        result['status'] = 'PASS' if all(item['status'] in ('PASS', 'NOT_RUN') for item in CHECKS) else 'FAIL'
    except Exception as exc:
        result['status'] = 'FAIL'
        result['error_type'] = type(exc).__name__
        # Driver text and DSNs must never enter evidence/logs.
        if isinstance(exc, LabDatabaseError):
            result['error_code'] = exc.code
        elif isinstance(exc, psycopg.Error):
            result['sqlstate'] = exc.sqlstate
    result['passed_checks'] = sum(item['status'] == 'PASS' for item in CHECKS)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'passed_checks': result['passed_checks'], 'evidence': str(output), 'last_check': CHECKS[-1] if CHECKS else None}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
