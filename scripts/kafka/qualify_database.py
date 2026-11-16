"""Qualify the real local PostgreSQL projection without a running Kafka broker.

Creates the absent, owned kafka_lab schema and a few uniquely named fixtures.
Only those fixtures are removed. Reference, practice and pre-existing Kafka
rows are fingerprinted before/after. Run on an idle learning stand so concurrent
operator writes are not mistaken for preservation failures. Never resets data.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import psycopg
from psycopg import sql

from src.lab.database import LabDatabase, LabDatabaseError
from src.lab.kafka.contracts import Record, ReviewEvent
from src.lab.kafka.store import EventConflict, KafkaStore, UnknownIncident


def check(result, name, condition, detail=None):
    item = {'name': name, 'status': 'PASS' if condition else 'FAIL'}
    if detail is not None:
        item['detail'] = detail
    result['checks'].append(item)
    if not condition:
        raise AssertionError(name)


def error(result, stage, exc):
    # Exception text may contain driver diagnostics or credentials. Keep codes.
    item = {'stage': stage, 'type': type(exc).__name__}
    if isinstance(exc, LabDatabaseError):
        item['code'] = exc.code
    elif isinstance(exc, psycopg.Error):
        item['sqlstate'] = exc.sqlstate
    result['errors'].append(item)


def fingerprints(db):
    output = {}
    with db.connect('admin') as conn:
        for namespace in ('reference', 'practice', 'kafka_lab'):
            tables = conn.execute(
                'SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
                "WHERE n.nspname=%s AND c.relkind IN ('r','p') ORDER BY c.relname",
                (namespace,),
            ).fetchall()
            output[namespace] = {}
            for table in tables:
                # PostgreSQL JSON text preserves numeric precision. The evidence
                # contains only counts and hashes, never the operator's rows.
                rows = conn.execute(sql.SQL('SELECT to_jsonb(t)::text AS body FROM {}.{} t').format(
                    sql.Identifier(namespace), sql.Identifier(table['relname']),
                )).fetchall()
                bodies = sorted(row['body'] for row in rows)
                digest = hashlib.sha256(json.dumps(bodies, ensure_ascii=True,
                    separators=(',', ':')).encode('utf-8')).hexdigest()
                output[namespace][table['relname']] = {'rows': len(rows), 'sha256': digest}
    return output


def fixture_counts(db, identifiers, topic):
    with db.connect('reader') as conn:
        return conn.execute(
            'SELECT (SELECT count(*) FROM kafka_lab.processed_events WHERE event_id=ANY(%s)) '
            'AS events, (SELECT count(*) FROM kafka_lab.deliveries WHERE event_id=ANY(%s)) '
            'AS deliveries, (SELECT count(*) FROM kafka_lab.deliveries WHERE topic=%s) '
            'AS topic_deliveries', (identifiers, identifiers, topic),
        ).fetchone()


def cleanup(db, identifiers, topic):
    """Both the fresh UUIDs and the private qualification topic must match."""
    with db.connect('admin') as conn:
        deliveries = conn.execute(
            'DELETE FROM kafka_lab.deliveries WHERE event_id=ANY(%s) AND topic=%s',
            (identifiers, topic),
        ).rowcount
        events = conn.execute(
            'DELETE FROM kafka_lab.processed_events WHERE event_id=ANY(%s) AND topic=%s',
            (identifiers, topic),
        ).rowcount
    return {'deliveries': deliveries, 'processed_events': events}


def qualify(db, result):
    baseline = None
    empty_initialized = None
    cleanup_authorized = False
    ids = [uuid4(), uuid4(), uuid4()]
    topic = 'qualification.' + uuid4().hex
    result['fixture'] = {'event_ids': [str(value) for value in ids], 'topic': topic}
    store = KafkaStore(db)
    try:
        baseline = fingerprints(db)
        result['baseline'] = baseline
        initialized = store.initialize()
        result['initialization'] = initialized
        after_initialize = fingerprints(db)
        check(result, 'initialization_preserves_reference_and_practice', all(
            baseline[name] == after_initialize[name] for name in ('reference', 'practice')))
        if initialized['created']:
            check(result, 'new_namespace_has_only_empty_projection_tables',
                  set(after_initialize['kafka_lab']) == {'processed_events', 'deliveries'}
                  and all(value['rows'] == 0 for value in after_initialize['kafka_lab'].values()))
            empty_initialized = after_initialize['kafka_lab']
        else:
            check(result, 'initialization_preserves_existing_kafka_rows',
                  baseline['kafka_lab'] == after_initialize['kafka_lab'])

        # Establish that these exact IDs and this private topic were absent
        # before authorizing the finally cleanup, even on a failed probe.
        counts = fixture_counts(db, ids, topic)
        with db.connect('reader') as conn:
            topic_events = conn.execute(
                'SELECT count(*) AS n FROM kafka_lab.processed_events WHERE topic=%s',
                (topic,),
            ).fetchone()['n']
            known = conn.execute('SELECT incident_id FROM reference.incidents '
                                 'ORDER BY incident_id LIMIT 1').fetchone()
            unknown = conn.execute("SELECT 1 FROM reference.incidents WHERE incident_id='INC-999999'").fetchone()
        check(result, 'fixture_ids_and_topic_absent_before_probe',
              counts == {'events': 0, 'deliveries': 0, 'topic_deliveries': 0} and topic_events == 0)
        cleanup_authorized = True
        check(result, 'known_and_unknown_incident_fixtures_valid', known is not None and unknown is None)

        for role in ('reader', 'student', 'api'):
            with db.connect(role) as conn:
                schema = conn.execute(
                    "SELECT has_schema_privilege(current_user,'kafka_lab','USAGE') AS usage, "
                    "has_schema_privilege(current_user,'kafka_lab','CREATE') AS create"
                ).fetchone()
                check(result, role + '_schema_usage_without_create', schema == {'usage': True, 'create': False})
                for table in ('processed_events', 'deliveries'):
                    grants = {}
                    for privilege in ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'):
                        grants[privilege] = conn.execute(
                            'SELECT has_table_privilege(current_user,%s,%s) AS allowed',
                            ('kafka_lab.' + table, privilege),
                        ).fetchone()['allowed']
                    check(result, role + '_' + table + '_effective_permissions', grants == {
                        privilege: privilege == 'SELECT' or (privilege == 'INSERT' and role == 'api')
                        for privilege in grants}, grants)

        event = ReviewEvent(event_id=ids[0], incident_id=known['incident_id'],
                            occurred_at=datetime.now(timezone.utc))

        def delivery(offset):
            return Record(topic=topic, partition=0, offset=offset,
                          key=event.incident_id.encode('utf-8'), value=event.canonical_bytes())

        first = store.process(event, delivery(0))
        check(result, 'first_event_committed_as_processed', first['status'] == 'processed')
        expected = {key: value for key, value in first.items() if key != 'status'}
        check(result, 'get_reads_committed_result', store.get(str(ids[0])) == expected)
        listed = store.list_results(100)
        check(result, 'list_returns_bounded_readable_results', 1 <= len(listed) <= 100
              and all(set(item) == set(expected) for item in listed))
        second = store.process(event, delivery(1))
        replay = store.process(event, delivery(1))
        check(result, 'duplicate_and_redelivery_reuse_committed_projection',
              second == {**first, 'status': 'duplicate'} and replay == second)
        check(result, 'two_coordinates_one_projection', fixture_counts(db, ids, topic) == {
            'events': 1, 'deliveries': 2, 'topic_deliveries': 2})
        with db.connect('reader') as conn:
            flags = conn.execute('SELECT offset_value, duplicate FROM kafka_lab.deliveries '
                                 'WHERE event_id=%s AND topic=%s ORDER BY offset_value',
                                 (ids[0], topic)).fetchall()
        check(result, 'delivery_flags_distinguish_duplicate_publication', flags == [
            {'offset_value': 0, 'duplicate': False}, {'offset_value': 1, 'duplicate': True}])

        conflict = False
        try:
            altered = event.model_copy(update={'occurred_at': event.occurred_at + timedelta(seconds=1)})
            store.process(altered, delivery(2))
        except EventConflict:
            conflict = True
        check(result, 'changed_payload_same_uuid_rejected', conflict)
        coordinate_conflict = False
        try:
            store.process(event.model_copy(update={'event_id': ids[1]}), delivery(0))
        except EventConflict:
            coordinate_conflict = True
        check(result, 'coordinate_conflict_rolls_back_second_projection', coordinate_conflict
              and store.get(str(ids[1])) is None)
        rejected_unknown = False
        try:
            store.process(event.model_copy(update={'event_id': ids[2], 'incident_id': 'INC-999999'}), delivery(3))
        except UnknownIncident:
            rejected_unknown = True
        check(result, 'unknown_incident_leaves_no_projection', rejected_unknown and store.get(str(ids[2])) is None)
        check(result, 'all_rejected_events_leave_delivery_counts_unchanged', fixture_counts(db, ids, topic) == {
            'events': 1, 'deliveries': 2, 'topic_deliveries': 2})
        before_reinitialize = fingerprints(db)
        check(result, 'second_initialize_reports_existing_schema', store.initialize()['created'] is False)
        check(result, 'second_initialize_preserves_all_rows', fingerprints(db) == before_reinitialize)
    except Exception as exc:
        error(result, 'qualification', exc)
    finally:
        if cleanup_authorized:
            try:
                result['cleanup'] = cleanup(db, ids, topic)
                check(result, 'owned_fixtures_removed', fixture_counts(db, ids, topic) == {
                    'events': 0, 'deliveries': 0, 'topic_deliveries': 0})
            except Exception as exc:
                error(result, 'fixture_cleanup', exc)
        if baseline is not None:
            try:
                after = fingerprints(db)
                result['after_cleanup'] = after
                for namespace in ('reference', 'practice'):
                    check(result, namespace + '_all_table_hashes_preserved',
                          after[namespace] == baseline[namespace])
                expected_kafka = empty_initialized if empty_initialized is not None else baseline['kafka_lab']
                check(result, 'preexisting_kafka_rows_preserved', after['kafka_lab'] == expected_kafka)
            except Exception as exc:
                error(result, 'preservation_verification', exc)


def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory = ROOT / 'var' / 'checks' / 'kafka-db' / (stamp + '-' + uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    result = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'scope': 'Real local PostgreSQL only; no Kafka broker; synthetic fixtures; no practice reset',
              'checks': [], 'errors': []}
    try:
        qualify(LabDatabase(), result)
    except Exception as exc:
        error(result, 'configuration_or_unexpected_failure', exc)
    result['status'] = 'PASS' if not result['errors'] and result['checks'] and all(
        item['status'] == 'PASS' for item in result['checks']) else 'FAIL'
    result['passed_checks'] = sum(item['status'] == 'PASS' for item in result['checks'])
    output = directory / 'database-qualification.json'
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'passed_checks': result['passed_checks'],
                      'evidence': str(output), 'errors': result['errors']}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
