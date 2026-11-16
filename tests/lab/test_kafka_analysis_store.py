"""Inbox atomicity, replay and monotonic result projection without network I/O."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import UUID

import pytest

from src.contracts.semantics import canonical_digest
from src.lab.database import LabDatabaseError
from src.lab.kafka.analysis_contracts import AnalysisEvent, TOPIC
from src.lab.kafka.analysis_store import AnalysisStore, ANALYSIS_MARKER, TABLES
from src.lab.kafka.contracts import Record
from src.lab.kafka.store import EventConflict
from tests.evaluator.fixtures import bundle
from tests.lab.test_kafka_store import Rows, TransactionDatabase

NOW = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)
EVENT_ID = UUID('00000000-0000-4000-8000-000000000111')
SECOND_ID = UUID('00000000-0000-4000-8000-000000000222')
WINDOW = {'start_inclusive': '2026-01-01T00:00:00Z',
          'end_exclusive': '2026-02-01T00:00:00Z', 'as_of': '2026-02-01T00:00:00Z'}


def event(identifier=EVENT_ID, **changes):
    return AnalysisEvent(event_id=identifier, incident_id='INC-000001', occurred_at=NOW,
                         window=WINDOW, **changes)


def record(offset=1, value=None):
    value = value or event()
    return Record(TOPIC, 0, offset, value.incident_id.encode(), value.canonical_bytes())


def timestamp(seconds=0):
    return (NOW + timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')


def job(status='queued', revision=0):
    result = {'schema_version': '1.0', 'job_id': 'job-fixture', 'incident_id': 'INC-000001',
              'revision': revision, 'status': status, 'accepted_at': timestamp(),
              'deadline_at': timestamp(180)}
    if status not in ('queued', 'running'):
        result['completed_at'] = timestamp(180 if status == 'timed_out' else 3)
    if status == 'succeeded':
        report = deepcopy(bundle(1)['outcomes'][0]['result'])
        report.update(generated_at=timestamp(2), confidence=None)
        result['result'] = report
    elif status == 'failed':
        result['result'] = {'schema_version': '1.0', 'status': 'error', 'generated_at': timestamp(2),
                            'correlation_id': 'run-fixture', 'code': 'DEPENDENCY_UNAVAILABLE',
                            'message': 'Synthetic failure.', 'retryable': True}
    elif status == 'refused':
        result['result'] = {'schema_version': '1.0', 'status': 'refusal', 'incident_id': 'INC-000001',
            'generated_at': timestamp(2), 'data_completeness': 'insufficient',
            'reason_code': 'INSUFFICIENT_DATA', 'reason': 'Synthetic refusal.',
            'recommended_action': 'manual_review'}
    return result


class AnalysisDatabase(TransactionDatabase):
    """Extend the existing transaction double with an independent inbox namespace."""
    def __init__(self):
        super().__init__()
        self.requests, self.analysis_deliveries = {}, {}
        self.analysis_relations = [dict(name=name, kind='r', owner='aml_lab_admin',
                                       marker=ANALYSIS_MARKER) for name in TABLES]
        self.ticks = 0

    def tick(self):
        self.ticks += 1
        return NOW + timedelta(seconds=self.ticks)

    @contextmanager
    def connect(self, role):
        snapshot = deepcopy((self.requests, self.analysis_deliveries, self.analysis_relations))
        try:
            with super().connect(role):
                yield self
        except BaseException:
            self.requests, self.analysis_deliveries, self.analysis_relations = snapshot
            raise

    def execute(self, query, params=()):
        compact = ' '.join(query.split())
        if "c.relname IN ('analysis_requests', 'analysis_deliveries')" in compact:
            return Rows(self.analysis_relations)
        if 'CREATE TABLE kafka_lab.analysis_requests (' in compact:
            assert self.roles[-1] == 'admin' and not self.analysis_relations
            self.analysis_relations = [dict(name=name, kind='r', owner='aml_lab_admin',
                                           marker=ANALYSIS_MARKER) for name in TABLES]
            self.commands.append((query, params))
            return Rows()
        if compact.startswith('INSERT INTO kafka_lab.analysis_requests'):
            assert self.roles[-1] == 'api'
            identifier, incident, engine, digest, frozen = params
            assert identifier not in self.requests
            now = self.tick()
            self.requests[identifier] = dict(event_id=identifier, incident_id=incident,
                engine=engine, canonical_sha256=digest, event_json=deepcopy(frozen.obj), status='queued',
                job_id=None, job_revision=None, job_json=None, report_id=None, report_version=None,
                content_sha256=None, last_error_code=None, created_at=now, updated_at=now)
            return Rows()
        if compact.startswith('INSERT INTO kafka_lab.analysis_deliveries'):
            assert self.roles[-1] == 'api'
            if self.fail_delivery:
                raise LabDatabaseError('UNAVAILABLE')
            topic, partition, offset, identifier, digest, duplicate = params
            coordinate = (topic, partition, offset)
            if coordinate in self.analysis_deliveries:
                return Rows()
            self.analysis_deliveries[coordinate] = dict(event_id=identifier,
                canonical_sha256=digest, duplicate=duplicate)
            return Rows([dict(event_id=identifier)])
        if compact.startswith('SELECT event_id, canonical_sha256 FROM kafka_lab.analysis_deliveries'):
            row = self.analysis_deliveries.get(tuple(params))
            return Rows([row] if row else [])
        if compact.startswith('SELECT event_id, incident_id, engine, canonical_sha256, event_json, status,'):
            if 'WHERE event_id = %s' in compact:
                row = self.requests.get(params[0])
                return Rows([row] if row else [])
            if 'WHERE status = %s' in compact:
                rows = [row for row in self.requests.values() if row['status'] == params[0]]
                return Rows(sorted(rows, key=lambda row: (row['updated_at'], str(row['event_id'])))[:params[1]])
            return Rows(sorted(self.requests.values(), key=lambda row: -row['created_at'].timestamp())[:params[0]])
        if compact.startswith('UPDATE kafka_lab.analysis_requests SET status = %s, job_id = %s,'):
            assert self.roles[-1] == 'api' and len(params) == 8
            status, job_id, revision, job_json, report_id, report_version, digest, identifier = params
            row = self.requests[identifier]
            row.update(status=status, job_id=job_id, job_revision=revision, job_json=deepcopy(job_json.obj),
                       report_id=report_id, report_version=report_version, content_sha256=digest,
                       last_error_code=None, updated_at=self.tick())
            return Rows([row])
        if compact.startswith('UPDATE kafka_lab.analysis_requests SET status = %s, last_error_code = %s,'):
            assert self.roles[-1] == 'api'
            status, code, identifier = params
            row = self.requests[identifier]
            row.update(status=status, last_error_code=code, updated_at=self.tick())
            return Rows([row])
        return super().execute(query, params)


def seeded():
    db = AnalysisDatabase()
    store = AnalysisStore(db)
    store.enqueue(event(), record())
    return db, store


def test_event_freezes_explicit_window_and_stable_key():
    value = event()
    assert value.job_request().model_dump(mode='json') == {
        'schema_version': '1.0', 'incident_id': 'INC-000001', 'window': WINDOW}
    assert value.idempotency_key == 'kafka-aml:' + str(EVENT_ID)
    assert AnalysisEvent.model_validate_json(value.canonical_bytes()) == value
    assert b'aml.analysis.requested.v1' in value.canonical_bytes()


@pytest.mark.parametrize('change', [{'window': None}, {'event_type': 'incident.review.requested.v1'},
                                   {'synthetic': False}, {'engine': 'remote'}, {'unknown': 1}])
def test_event_rejects_ambiguous_contracts(change):
    payload = event().model_dump(mode='json') | change
    with pytest.raises(ValueError):
        AnalysisEvent.model_validate(payload)


def test_enqueue_commits_full_frozen_request_and_delivery_before_success():
    db, store = seeded()
    assert db.commits == 1 and db.roles == ['api']
    assert len(db.requests) == len(db.analysis_deliveries) == 1
    row = store.get(str(EVENT_ID))
    assert row['status'] == 'queued' and row['event_json'] == event().model_dump(mode='json')
    assert row['job_id'] is None and row['job_json'] is None
    assert not db.events and not db.deliveries  # Existing bookkeeping is independent.
    assert store.pending() == [row] and store.submitted() == []


def test_redelivery_and_process_restart_keep_one_request_and_record_new_coordinates():
    db, store = seeded()
    restarted = AnalysisStore(db)
    assert restarted.enqueue(event(), record())['status'] == 'duplicate'
    assert restarted.enqueue(event(), record(2))['status'] == 'duplicate'
    assert len(db.requests) == 1 and len(db.analysis_deliveries) == 2
    assert db.analysis_deliveries[(TOPIC, 0, 2)]['duplicate'] is True
    assert store.get(str(EVENT_ID)) == restarted.get(str(EVENT_ID))


@pytest.mark.parametrize('change', [{'engine': 'local-model'}, {'occurred_at': timestamp(1)},
    {'window': dict(WINDOW, start_inclusive='2026-01-02T00:00:00Z')}, {'incident_id': 'INC-000002'}])
def test_changed_uuid_body_is_rejected_before_any_submission(change):
    db, store = seeded()
    before = deepcopy((db.requests, db.analysis_deliveries))
    altered = AnalysisEvent.model_validate(event().model_dump(mode='json') | change)
    with pytest.raises(EventConflict):
        store.enqueue(altered, record(2, altered))
    assert (db.requests, db.analysis_deliveries) == before


def test_coordinate_conflict_rolls_back_new_inbox_entry():
    db, store = seeded()
    with pytest.raises(EventConflict):
        store.enqueue(event(SECOND_ID), record(value=event(SECOND_ID)))
    assert set(db.requests) == {EVENT_ID} and len(db.analysis_deliveries) == 1


@pytest.mark.parametrize('failure', ['fail_delivery', 'fail_commit'])
def test_storage_failure_never_reports_success_or_partial_enqueue(failure):
    db = AnalysisDatabase()
    setattr(db, failure, True)
    with pytest.raises(LabDatabaseError, match='UNAVAILABLE'):
        AnalysisStore(db).enqueue(event(), record())
    assert not db.requests and not db.analysis_deliveries and db.commits == 0


def test_defer_rotates_queue_fairly_and_submission_moves_between_pending_sets():
    db, store = seeded()
    store.enqueue(event(SECOND_ID), record(2, event(SECOND_ID)))
    first = store.pending()[0]
    assert first['event_id'] == str(EVENT_ID)
    deferred = store.defer(str(EVENT_ID), 'NOT_READY')
    assert deferred['last_error_code'] == 'NOT_READY'
    assert deferred['updated_at'] > first['updated_at']
    assert store.pending()[0]['event_id'] == str(SECOND_ID)
    submitted = store.record_job(str(EVENT_ID), job())
    assert submitted['status'] == 'submitted' and submitted['last_error_code'] is None
    assert submitted['job_json'] == job() and submitted['job_revision'] == 0
    assert store.submitted() == [submitted]
    assert store.pending()[0]['event_id'] == str(SECOND_ID)


def test_submitted_rotation_happens_even_when_remote_revision_is_unchanged():
    _, store = seeded()
    first = store.record_job(str(EVENT_ID), job())
    second = store.record_job(str(EVENT_ID), job())
    assert first['job_json'] == second['job_json']
    assert first['updated_at'] < second['updated_at']


@pytest.mark.parametrize('status, revision', [('running', 1), ('succeeded', 2)])
def test_delayed_acceptance_from_one_observer_cannot_regress_another_observers_result(status, revision):
    db, store = seeded()
    delayed_observer = AnalysisStore(db)
    captured, release = Event(), Event()

    def delayed_acceptance():
        # Simulate an acceptance reply already fetched outside the transaction,
        # while a second observer reconciles the same job before it is persisted.
        response = job()
        captured.set()
        assert release.wait(5), 'The second observer never released the delayed reply'
        return delayed_observer.record_job(str(EVENT_ID), response)

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(delayed_acceptance)
        try:
            assert captured.wait(5), 'The first observer never captured its reply'
            current = store.record_job(str(EVENT_ID), job(status, revision))
            ticks = db.ticks
        finally:
            release.set()
        assert pending.result(timeout=5) == current

    assert store.get(str(EVENT_ID)) == current
    assert db.ticks == ticks  # A stale reply does not even rotate updated_at.
    assert len(db.requests) == len(db.analysis_deliveries) == 1


@pytest.mark.parametrize('field, value', [
    ('job_id', 'job-another'), ('incident_id', 'INC-000002'),
    ('accepted_at', timestamp(1)), ('deadline_at', timestamp(170)),
])
def test_stale_revision_must_still_match_the_accepted_immutable_identity(field, value):
    _, store = seeded()
    current = store.record_job(str(EVENT_ID), job('succeeded', 2))
    stale = job() | {field: value}
    with pytest.raises(ValueError, match='ANALYSIS_JOB_CONFLICT'):
        store.record_job(str(EVENT_ID), stale)
    assert store.get(str(EVENT_ID)) == current


@pytest.mark.parametrize('status', ['succeeded', 'refused', 'failed', 'cancelled', 'timed_out'])
def test_terminal_projection_can_skip_unobserved_running_revision_and_is_immutable(status):
    _, store = seeded()
    store.record_job(str(EVENT_ID), job())
    terminal_job = job(status, 2)
    final = store.record_job(str(EVENT_ID), terminal_job)
    assert final['status'] == status and final['job_json'] == terminal_job
    assert store.pending() == store.submitted() == []
    assert store.record_job(str(EVENT_ID), terminal_job) == final
    assert store.defer(str(EVENT_ID), 'NOT_READY') == final
    assert store.reject(str(EVENT_ID), 'INVALID_INPUT_FORMAT') == final
    if status == 'succeeded':
        assert final['content_sha256'] == canonical_digest(terminal_job['result'])
        assert final['report_id'] == terminal_job['result']['report_id']
        assert final['report_version'] == terminal_job['result']['report_version']
    else:
        assert final['content_sha256'] is final['report_id'] is final['report_version'] is None
    assert store.record_job(str(EVENT_ID), job('running', 1)) == final
    with pytest.raises(ValueError, match='ANALYSIS_JOB_CONFLICT'):
        store.record_job(str(EVENT_ID), dict(terminal_job, revision=3))


@pytest.mark.parametrize('alteration', ['same_revision_change', 'job_id',
                                     'incident', 'deadline', 'status_regression'])
def test_accepted_identity_and_monotonic_revision_are_preserved(alteration):
    db, store = seeded()
    previous = store.record_job(str(EVENT_ID), job('running', 1))
    bad = job('running', 2)
    if alteration == 'same_revision_change':
        bad = job('queued', 1)
    elif alteration == 'job_id':
        bad['job_id'] = 'job-another'
    elif alteration == 'incident':
        bad['incident_id'] = 'INC-000002'
    elif alteration == 'deadline':
        bad['deadline_at'] = timestamp(170)
    else:
        bad['status'] = 'queued'
    with pytest.raises(ValueError, match='ANALYSIS_JOB_CONFLICT'):
        store.record_job(str(EVENT_ID), bad)
    assert store.get(str(EVENT_ID)) == previous


def test_rejection_only_applies_before_acceptance_and_is_final():
    _, store = seeded()
    rejected = store.reject(str(EVENT_ID), 'INVALID_INPUT_FORMAT')
    assert rejected['status'] == 'rejected' and rejected['job_id'] is None
    assert store.defer(str(EVENT_ID), 'NOT_READY') == rejected
    with pytest.raises(ValueError, match='ANALYSIS_JOB_CONFLICT'):
        store.record_job(str(EVENT_ID), job())
    _, other = seeded()
    submitted = other.record_job(str(EVENT_ID), job())
    assert other.reject(str(EVENT_ID), 'INVALID_INPUT_FORMAT') == submitted


def test_bad_error_strings_and_invalid_jobs_are_rejected_without_database_access():
    db, store = seeded()
    roles = list(db.roles)
    for code in ('HTTP 500: secret', '', 'x' * 100, None):
        with pytest.raises(ValueError, match='INVALID_ANALYSIS_ERROR_CODE'):
            store.defer(str(EVENT_ID), code)
    with pytest.raises(ValueError, match='INVALID_ANALYSIS_JOB'):
        store.record_job(str(EVENT_ID), {})
    assert db.roles == roles


def test_initialize_is_additive_idempotent_and_validates_before_grants():
    db = AnalysisDatabase()
    db.analysis_relations = []
    db.events['existing'] = {'synthetic': True}
    store = AnalysisStore(db)
    assert store.initialize()['created'] is True
    store.enqueue(event(), record())
    frozen = deepcopy((db.requests, db.analysis_deliveries, db.events, db.deliveries))
    assert store.initialize()['created'] is False
    assert (db.requests, db.analysis_deliveries, db.events, db.deliveries) == frozen
    writes = [sql for sql, _ in db.commands if sql.startswith(('GRANT ', 'REVOKE '))]
    assert all('analysis_' in sql for sql in writes)
    assert not any('DELETE' in sql or 'TO aml_student' in sql for sql in writes)
    assert any('TO aml_reader, aml_student, aml_api' in sql for sql in writes)


@pytest.mark.parametrize('alteration', ['marker', 'owner', 'view', 'missing', 'base_marker', 'base_missing'])
def test_foreign_or_partial_namespace_is_never_overwritten_or_granted(alteration):
    db = AnalysisDatabase()
    if alteration == 'marker':
        db.analysis_relations[0]['marker'] = 'another-app'
    elif alteration == 'owner':
        db.analysis_relations[0]['owner'] = 'another_owner'
    elif alteration == 'view':
        db.analysis_relations[0]['kind'] = 'v'
    elif alteration == 'missing':
        db.analysis_relations.pop()
    elif alteration == 'base_marker':
        db.schema['marker'] = 'another-app'
    else:
        db.relations.pop()
    with pytest.raises(LabDatabaseError, match='INVALID_STATE'):
        AnalysisStore(db).initialize()
    assert not any(sql.startswith(('GRANT ', 'REVOKE ')) or 'CREATE TABLE' in sql
                   for sql, _ in db.commands)


@pytest.mark.parametrize('alteration', ['event_identity', 'event_hash', 'job_revision', 'report_hash'])
def test_corrupted_projection_cannot_be_returned_as_a_valid_result(alteration):
    db, store = seeded()
    store.record_job(str(EVENT_ID), job('succeeded', 2))
    row = db.requests[EVENT_ID]
    if alteration == 'event_identity':
        row['incident_id'] = 'INC-000002'
    elif alteration == 'event_hash':
        row['event_json']['occurred_at'] = timestamp(1)
    elif alteration == 'job_revision':
        row['job_revision'] = 99
    else:
        row['content_sha256'] = '0' * 64
    with pytest.raises(LabDatabaseError, match='INVALID_STATE'):
        store.get(str(EVENT_ID))


@pytest.mark.parametrize('limit', [0, 101, True, None, '2'])
def test_invalid_limits_rejected_before_database_access(limit):
    db = AnalysisDatabase()
    for method in (AnalysisStore(db).pending, AnalysisStore(db).submitted, AnalysisStore(db).list_results):
        with pytest.raises(ValueError, match='INVALID_LIMIT'):
            method(limit)
    assert not db.roles


def test_read_methods_use_reader_role_and_missing_event_is_not_a_success():
    db, store = seeded()
    assert store.list_results() == [store.get(str(EVENT_ID))]
    assert store.get(str(SECOND_ID)) is None
    assert db.roles[1:] == ['reader'] * 3
    for call in (lambda: store.record_job(str(SECOND_ID), job()),
                 lambda: store.defer(str(SECOND_ID), 'NOT_READY')):
        with pytest.raises(ValueError, match='ANALYSIS_EVENT_NOT_FOUND'):
            call()
