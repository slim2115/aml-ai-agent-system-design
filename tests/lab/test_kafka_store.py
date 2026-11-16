"""Projection transaction contracts; live PostgreSQL is qualified separately.

The small transactional DB double models state/rollback and asserts read/write
roles. These tests exercise redelivery and crashes without a broker or server.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from src.lab.database import LabDatabaseError, MARKER
from src.lab.kafka.contracts import Record, ReviewEvent
from src.lab.kafka.store import EventConflict, KafkaStore, SCHEMA_MARKER, UnknownIncident


NOW = datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc)
EVENT_ID = UUID('00000000-0000-4000-8000-000000000001')
SECOND_ID = UUID('00000000-0000-4000-8000-000000000002')


def event(identifier=EVENT_ID, incident='INC-000123'):
    return ReviewEvent(event_id=identifier, incident_id=incident, occurred_at=NOW)


def record(offset=1):
    return Record(topic='aml.review.requests.v1', partition=0, offset=offset,
                  key=b'INC-000123', value=b'{}')


class Rows:
    def __init__(self, rows=()):
        self.rows = rows

    def fetchone(self):
        return deepcopy(self.rows[0]) if self.rows else None

    def fetchall(self):
        return deepcopy(list(self.rows))


class TransactionDatabase:
    """In-memory SQL boundary with atomic commit, not a PostgreSQL substitute."""
    def __init__(self):
        self.events = {}
        self.deliveries = {}
        self.schema = {'marker': SCHEMA_MARKER, 'owner': 'aml_lab_admin'}
        self.relations = [
            {'name': name, 'kind': 'r', 'owner': 'aml_lab_admin'}
            for name in ('processed_events', 'deliveries')
        ]
        self.incidents = {'INC-000123': {'incident_id': 'INC-000123', 'client_id': 'C001'}}
        self.totals = [{'currency': 'RUB', 'transaction_count': 2,
                        'total_amount': Decimal('100.50')}]
        self.roles = []
        self.commands = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_commit = False
        self.fail_delivery = False
        self.database_marker = MARKER

    @contextmanager
    def connect(self, role):
        self.roles.append(role)
        snapshot = deepcopy((self.events, self.deliveries, self.schema))
        try:
            yield self
            if self.fail_commit:
                raise LabDatabaseError('UNAVAILABLE')
        except BaseException:
            self.events, self.deliveries, self.schema = snapshot
            self.rollbacks += 1
            raise
        else:
            self.commits += 1

    def execute(self, query, params=()):
        self.commands.append((query, params))
        compact = ' '.join(query.split())
        if compact.startswith('SELECT current_database()'):
            return Rows([{'name': 'aml_lab', 'marker': self.database_marker}])
        if compact.startswith("SELECT obj_description(oid, 'pg_namespace') AS marker,"):
            return Rows([self.schema] if self.schema else [])
        if compact.startswith("SELECT obj_description(oid, 'pg_namespace') AS marker "):
            assert params == ('reference',)
            return Rows([{'marker': MARKER}])
        if compact.startswith('SELECT c.relname AS name'):
            return Rows(self.relations)
        if compact.startswith('SELECT pg_advisory_xact_lock'):
            return Rows()
        if compact.startswith(('REVOKE ', 'GRANT ')):
            assert self.roles[-1] == 'admin'
            return Rows()
        if 'CREATE SCHEMA kafka_lab AUTHORIZATION aml_lab_admin;' in compact:
            assert self.roles[-1] == 'admin' and self.schema is None
            self.schema = {'marker': SCHEMA_MARKER, 'owner': 'aml_lab_admin'}
            return Rows()
        if compact.startswith('SELECT incident_id, client_id FROM reference.incidents'):
            return Rows([self.incidents[params[0]]] if params[0] in self.incidents else [])
        if compact.startswith('SELECT currency, count(*) AS transaction_count'):
            return Rows(self.totals)
        if compact.startswith('INSERT INTO kafka_lab.processed_events'):
            assert self.roles[-1] == 'api'
            identifier, _, _, incident, occurred, digest, result, topic, partition, offset = params
            assert identifier not in self.events
            self.events[identifier] = {
                'event_id': identifier, 'incident_id': incident, 'occurred_at': occurred,
                'processed_at': NOW, 'canonical_sha256': digest, 'result_json': result.obj,
                'topic': topic, 'partition': partition, 'offset_value': offset,
            }
            return Rows([self.events[identifier]])
        if compact.startswith('INSERT INTO kafka_lab.deliveries'):
            if self.fail_delivery:
                raise LabDatabaseError('UNAVAILABLE')
            assert self.roles[-1] == 'api'
            topic, partition, offset, identifier, digest, duplicate = params
            coordinate = (topic, partition, offset)
            if coordinate in self.deliveries:
                return Rows()
            assert identifier in self.events
            self.deliveries[coordinate] = {
                'event_id': identifier, 'canonical_sha256': digest, 'duplicate': duplicate,
            }
            return Rows([{'event_id': identifier}])
        if compact.startswith('SELECT event_id, canonical_sha256 FROM kafka_lab.deliveries'):
            row = self.deliveries.get(tuple(params))
            return Rows([row] if row else [])
        if compact.startswith('SELECT event_id, incident_id, occurred_at, processed_at,'):
            if 'WHERE event_id = %s' in compact:
                row = self.events.get(params[0])
                return Rows([row] if row else [])
            assert 'ORDER BY processed_at DESC, event_id LIMIT %s' in compact
            return Rows(sorted(self.events.values(), key=lambda row: str(row['event_id']))[:params[0]])
        if compact.startswith('SELECT (SELECT count(*) FROM kafka_lab.processed_events)'):
            return Rows([{'processed_events': len(self.events), 'deliveries': len(self.deliveries)}])
        raise AssertionError(f'Unexpected SQL: {query}')


def test_projection_commits_both_rows_and_serializes_exact_amounts():
    db = TransactionDatabase()
    result = KafkaStore(db).process(event(), record())
    assert db.commits == 1 and db.roles == ['api']
    assert len(db.events) == len(db.deliveries) == 1
    assert result['status'] == 'processed'
    assert result['event_id'] == str(EVENT_ID)
    assert result['occurred_at'] == '2026-10-01T10:30:00Z'
    assert result['result']['transaction_count'] == 2
    assert result['result']['transaction_totals_by_currency'][0]['total_amount'] == '100.50'
    assert result['result']['kind'] == 'snapshot_bookkeeping'
    assert not any('practice.' in query for query, _ in db.commands)


def test_same_event_new_coordinate_is_logged_as_duplicate_without_reprojection():
    db = TransactionDatabase()
    store = KafkaStore(db)
    first = store.process(event(), record())
    db.totals = []  # A duplicate must retain the original committed projection.
    duplicate = store.process(event(), record(2))
    assert len(db.events) == 1 and len(db.deliveries) == 2
    assert duplicate == {**first, 'status': 'duplicate'}
    assert db.deliveries[(record().topic, 0, 2)]['duplicate'] is True


def test_redelivery_after_db_commit_does_not_add_a_second_coordinate():
    db = TransactionDatabase()
    store = KafkaStore(db)
    store.process(event(), record())
    assert store.process(event(), record())['status'] == 'duplicate'
    assert len(db.events) == len(db.deliveries) == 1
    assert db.commits == 2


def test_reusing_event_id_with_changed_payload_rolls_back():
    db = TransactionDatabase()
    store = KafkaStore(db)
    store.process(event(), record())
    before = deepcopy((db.events, db.deliveries))
    with pytest.raises(EventConflict, match='^KAFKA_EVENT_CONFLICT$'):
        store.process(event(incident='INC-000124'), record(2))
    assert (db.events, db.deliveries) == before
    assert db.rollbacks == 1


def test_reusing_coordinate_for_another_event_rolls_back_new_projection():
    db = TransactionDatabase()
    store = KafkaStore(db)
    store.process(event(), record())
    with pytest.raises(EventConflict):
        store.process(event(SECOND_ID), record())
    assert set(db.events) == {EVENT_ID}
    assert len(db.deliveries) == 1 and db.rollbacks == 1


def test_unknown_incident_never_creates_projection_or_delivery():
    db = TransactionDatabase()
    with pytest.raises(UnknownIncident, match='^KAFKA_UNKNOWN_INCIDENT$'):
        KafkaStore(db).process(event(incident='INC-999999'), record())
    assert not db.events and not db.deliveries
    assert db.rollbacks == 1


@pytest.mark.parametrize('failure', ['fail_delivery', 'fail_commit'])
def test_no_success_or_partial_projection_if_delivery_insert_or_commit_fails(failure):
    db = TransactionDatabase()
    setattr(db, failure, True)
    with pytest.raises(LabDatabaseError, match='^UNAVAILABLE$'):
        KafkaStore(db).process(event(), record())
    assert not db.events and not db.deliveries and db.commits == 0
    setattr(db, failure, False)
    assert KafkaStore(db).process(event(), record())['status'] == 'processed'


def test_initialize_creates_only_absent_namespace_and_preserves_existing_events():
    db = TransactionDatabase()
    db.schema = None
    store = KafkaStore(db)
    assert store.initialize()['created'] is True
    store.process(event(), record())
    before = deepcopy((db.events, db.deliveries))
    assert store.initialize()['created'] is False
    assert (db.events, db.deliveries) == before
    assert db.roles == ['admin', 'api', 'admin']
    assert sum('CREATE SCHEMA' in query for query, _ in db.commands) == 1


@pytest.mark.parametrize('bad_schema', [None, {'marker': 'unrelated', 'owner': 'aml_lab_admin'},
                                       {'marker': SCHEMA_MARKER, 'owner': 'another_owner'}])
def test_processing_rejects_missing_or_foreign_namespace(bad_schema):
    db = TransactionDatabase()
    db.schema = bad_schema
    with pytest.raises(LabDatabaseError, match='^INVALID_STATE$'):
        KafkaStore(db).process(event(), record())
    assert not db.events and not db.deliveries


@pytest.mark.parametrize('alteration', ['foreign_marker', 'foreign_owner', 'foreign_table_owner',
                                     'view_instead_of_table', 'missing_table', 'foreign_database'])
def test_initialize_never_grants_or_overwrites_foreign_or_incomplete_objects(alteration):
    db = TransactionDatabase()
    if alteration == 'foreign_marker':
        db.schema['marker'] = 'unrelated'
    elif alteration == 'foreign_owner':
        db.schema['owner'] = 'another_owner'
    elif alteration == 'foreign_table_owner':
        db.relations[0]['owner'] = 'another_owner'
    elif alteration == 'view_instead_of_table':
        db.relations[0]['kind'] = 'v'
    elif alteration == 'missing_table':
        db.relations.pop()
    else:
        db.database_marker = 'unrelated'
    with pytest.raises(LabDatabaseError, match='^INVALID_STATE$'):
        KafkaStore(db).initialize()
    assert not any(query.startswith(('GRANT', 'REVOKE')) or 'CREATE SCHEMA' in query
                   for query, _ in db.commands)


def test_read_results_use_reader_and_have_consistent_wire_format():
    db = TransactionDatabase()
    store = KafkaStore(db)
    processed = store.process(event(), record())
    expected = {key: value for key, value in processed.items() if key != 'status'}
    assert store.get(str(EVENT_ID)) == expected
    assert store.list_results() == [expected]
    assert store.get(str(SECOND_ID)) is None
    assert store.status()['processed_events'] == 1
    assert db.roles == ['api', 'reader', 'reader', 'reader', 'reader']


@pytest.mark.parametrize('bad_limit', [0, 101, -1, True, '20', 1.0, None])
def test_invalid_list_limit_rejected_before_connect(bad_limit):
    db = TransactionDatabase()
    with pytest.raises(ValueError, match='^INVALID_LIMIT$'):
        KafkaStore(db).list_results(bad_limit)
    assert not db.roles


@pytest.mark.parametrize('bad_id', ['', 'invalid', None, 123])
def test_invalid_event_id_rejected_before_connect(bad_id):
    db = TransactionDatabase()
    with pytest.raises(ValueError, match='^INVALID_EVENT_ID$'):
        KafkaStore(db).get(bad_id)
    assert not db.roles
