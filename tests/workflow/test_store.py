"""Durable local controls; no production IAM, bank or SME acceptance claim."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import time
import uuid

import pytest

from src.contracts.semantics import canonical_digest
from src.workflow.auth import Principal, trusted_local_principal
from src.workflow.store import SQLiteStore, WORKFLOW_ROOT, WorkflowError, utc_now
from tests.evaluator.fixtures import bundle


OWNER = Principal('local-owner')
OTHER = Principal('local-other')
SUPERVISOR = Principal('local-supervisor', 'supervisor')


@pytest.fixture
def store():
    return SQLiteStore(WORKFLOW_ROOT / 'test-store' / uuid.uuid4().hex / 'workflow.sqlite3')


def request():
    return {'schema_version': '1.0', 'incident_id': 'INC-000001', 'window':
            {'start_inclusive': '2026-01-01T00:00:00Z', 'end_exclusive': '2026-02-01T00:00:00Z',
             'as_of': '2026-02-01T00:00:00Z'}}


def draft_job(store, key='job-key', report_id=None):
    queued = store.create_job(request(), OWNER, key)
    running = store.transition_job(queued['job_id'], 0, dict(queued, status='running', revision=1))
    report = bundle(1)['outcomes'][0]['result']
    report.update(report_id=report_id or 'report-' + uuid.uuid4().hex, generated_at=utc_now(), confidence=None)
    return store.transition_job(running['job_id'], 1, dict(running, status='succeeded', revision=2,
                                                         completed_at=utc_now(), result=report))


def decision_request(store, job, revision=0, action='approve'):
    report = job['result']
    return {'schema_version': '1.0', 'report': store.report_ref(report['report_id'], 1, OWNER).model_dump(mode='json'),
            'expected_revision': revision, 'action': action, 'reason': 'Synthetic human control reason'}


def assert_error(code, operation):
    with pytest.raises(WorkflowError) as caught:
        operation()
    assert caught.value.code == code
    assert str(caught.value) == code


def test_local_identity_is_stable_and_role_not_read_from_request():
    assert trusted_local_principal() == trusted_local_principal()
    assert trusted_local_principal().role == 'operator'
    assert trusted_local_principal().subject_id.startswith('local-')
    with pytest.raises(ValueError):
        Principal('local-a', 'administrator')


def test_acceptance_restart_ownership_request_and_engine(store):
    job = store.create_job(request(), OWNER, 'accept', engine='local-model')
    restarted = SQLiteStore(store.path)
    assert restarted.get_job(job['job_id'], OWNER) == job
    assert restarted.get_request_internal(job['job_id']) == request()
    assert restarted.get_engine_internal(job['job_id']) == 'local-model'
    assert len(restarted.list_active_jobs()) == 1
    assert restarted.list_jobs(OTHER) == []
    assert restarted.list_jobs(SUPERVISOR) == [job]
    assert_error('FORBIDDEN', lambda: restarted.get_job(job['job_id'], OTHER))
    assert_error('NOT_FOUND', lambda: restarted.get_job('job-absent', OWNER))


def test_replay_is_original_response_without_new_work(store):
    job = store.create_job(request(), OWNER, 'same-key')
    running = store.transition_job(job['job_id'], 0, dict(job, revision=1, status='running'))
    assert store.create_job(request(), OWNER, 'same-key') == job
    assert store.lookup_job_replay(request(), OWNER, 'same-key') == job
    assert store.get_job(job['job_id'], OWNER) == running
    assert store.lookup_job_replay(request(), OWNER, 'unused') is None
    assert len(store.list_jobs(OWNER)) == 1


@pytest.mark.parametrize('change', ['incident', 'engine', 'timeout', 'window'])
def test_same_key_changed_request_rejected(store, change):
    payload = request()
    store.create_job(payload, OWNER, 'same-key')
    kwargs = {}
    if change == 'incident':
        payload['incident_id'] = 'INC-000002'
    elif change == 'engine':
        kwargs['engine'] = 'local-model'
    elif change == 'timeout':
        kwargs['timeout_seconds'] = 120
    else:
        payload['window']['start_inclusive'] = '2026-01-02T00:00:00Z'
    assert_error('IDEMPOTENCY_CONFLICT', lambda: store.create_job(payload, OWNER, 'same-key', **kwargs))


def test_idempotency_scoped_by_principal(store):
    a = store.create_job(request(), OWNER, 'same-key')
    b = store.create_job(request(), OTHER, 'same-key')
    assert a['job_id'] != b['job_id']


@pytest.mark.parametrize('timeout', [0, -1, 181, True, '180', float('inf'), float('nan')])
def test_invalid_timeout_refused_without_insertion(store, timeout):
    assert_error('INVALID_INPUT_FORMAT', lambda: store.create_job(request(), OWNER, 'key', timeout))
    assert store.list_jobs(OWNER) == []


@pytest.mark.parametrize('key', ['', ' ', 'bad\nkey', 'x' * 129, None])
def test_invalid_idempotency_key_rejected(store, key):
    assert_error('INVALID_INPUT_FORMAT', lambda: store.create_job(request(), OWNER, key))


def test_concurrent_job_replay_has_one_job_and_one_event(store):
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = list(pool.map(lambda _: store.create_job(request(), OWNER, 'concurrent-key'), range(6)))
    assert all(job == jobs[0] for job in jobs)
    with store.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM audit_events').fetchone()[0] == 1


def test_transition_compare_and_swap_and_terminal_immutability(store):
    job = store.create_job(request(), OWNER, 'key')
    next_job = dict(job, revision=1, status='running')
    with ThreadPoolExecutor(max_workers=2) as pool:
        def transition(_):
            try:
                return store.transition_job(job['job_id'], 0, next_job)['status']
            except WorkflowError as error:
                return error.code
        results = list(pool.map(transition, range(2)))
    assert sorted(results) == ['VERSION_CONFLICT', 'running']
    cancelled = store.cancel_job(job['job_id'], OWNER, expected_revision=1, idempotency_key='cancel')
    reactivated = dict(cancelled, status='running', revision=3)
    reactivated.pop('completed_at')
    assert_error('INVALID_STATE', lambda: store.transition_job(job['job_id'], 2, reactivated))


def test_transition_identity_and_incident_cannot_change(store):
    job = store.create_job(request(), OWNER, 'key')
    assert_error('INVALID_STATE', lambda: store.transition_job(job['job_id'], 0,
                 dict(job, job_id='job-forged', revision=1, status='running')))
    assert store.get_job(job['job_id'], OWNER) == job


def test_cancellation_authorization_and_replay(store):
    job = store.create_job(request(), OWNER, 'key')
    assert_error('FORBIDDEN', lambda: store.cancel_job(job['job_id'], OTHER, idempotency_key='cancel'))
    cancelled = store.cancel_job(job['job_id'], SUPERVISOR, idempotency_key='cancel')
    assert cancelled['status'] == 'cancelled'
    assert store.cancel_job(job['job_id'], SUPERVISOR, idempotency_key='cancel') == cancelled
    assert_error('IDEMPOTENCY_CONFLICT', lambda: store.cancel_job(job['job_id'], SUPERVISOR, 1, 'cancel'))


def test_report_persisted_atomically_and_digest_verified(store):
    job = draft_job(store)
    report = job['result']
    restarted = SQLiteStore(store.path)
    assert restarted.get_report(report['report_id'], 1, OWNER) == report
    assert restarted.report_ref(report['report_id'], 1, OWNER).content_sha256 == canonical_digest(report)
    assert_error('FORBIDDEN', lambda: restarted.get_report(report['report_id'], 1, OTHER))
    assert restarted.get_report(report['report_id'], 1, SUPERVISOR) == report
    with restarted.transaction() as connection:
        damaged = deepcopy(report)
        damaged['found_facts'][0]['fact'] = 'Tampered'
        connection.execute('UPDATE reports SET payload_json=?', (json.dumps(damaged),))
    assert_error('INTERNAL_ERROR', lambda: restarted.get_report(report['report_id'], 1, OWNER))
    assert_error('INTERNAL_ERROR', lambda: restarted.get_job(job['job_id'], OWNER))


@pytest.mark.parametrize('corruption', ['revision', 'incident', 'completion'])
def test_corrupt_job_read_fails_safe(store, corruption):
    job = draft_job(store)
    damaged = deepcopy(job)
    if corruption == 'revision':
        damaged['revision'] = 300
    elif corruption == 'incident':
        damaged['incident_id'] = 'INC-999999'
        damaged['result']['incident_id'] = 'INC-999999'
    else:
        damaged['completed_at'] = job['deadline_at']
    with store.transaction() as connection:
        connection.execute('UPDATE jobs SET payload_json=? WHERE job_id=?', (json.dumps(damaged), job['job_id']))
    assert_error('INTERNAL_ERROR', lambda: store.get_job(job['job_id'], OWNER))
    assert_error('INTERNAL_ERROR', lambda: store.list_jobs(OWNER))


def test_duplicate_report_identity_rolls_back_job_transition(store):
    first = draft_job(store, 'first', report_id='report-bound')
    second = store.create_job(request(), OWNER, 'second')
    running = store.transition_job(second['job_id'], 0, dict(second, status='running', revision=1))
    duplicate = deepcopy(first['result'])
    duplicate['generated_at'] = utc_now()
    assert_error('INVALID_STATE', lambda: store.transition_job(running['job_id'], 1,
        dict(running, status='succeeded', revision=2, completed_at=utc_now(), result=duplicate)))
    assert store.get_job(running['job_id'], OWNER) == running


def test_decision_persists_server_actor_time_event_and_replay(store):
    job = draft_job(store)
    payload = decision_request(store, job)
    record = store.create_decision(job['result']['report_id'], payload, OWNER, 'decision-key')
    restarted = SQLiteStore(store.path)
    assert restarted.get_decision(record['decision_id'], OWNER) == record
    assert restarted.create_decision(job['result']['report_id'], payload, OWNER, 'decision-key') == record
    assert record['actor'] == OWNER.actor()
    assert record['previous_revision'] == 0 and record['revision'] == 1
    assert restarted.decision_revision(job['result']['report_id'], 1, OWNER) == 1
    with store.transaction() as connection:
        event = connection.execute('SELECT * FROM audit_events WHERE event_id=?', (record['audit_event_id'],)).fetchone()
        assert event['event_code'] == 'DECISION_RECORDED'
        assert event['actor_subject'] == OWNER.subject_id
        assert payload['reason'] not in str(tuple(event))


@pytest.mark.parametrize('principal', [OTHER, SUPERVISOR])
def test_decision_cannot_be_made_by_other_owner_even_supervisor(store, principal):
    job = draft_job(store)
    assert_error('FORBIDDEN', lambda: store.create_decision(job['result']['report_id'], decision_request(store, job), principal, 'key'))


@pytest.mark.parametrize('field,value', [('actor', {'subject_id': 'forged', 'role': 'supervisor'}),
                                       ('decided_at', '2026-01-01T00:00:00Z'), ('decision_id', 'forged')])
def test_decision_request_cannot_supply_server_fields(store, field, value):
    job = draft_job(store)
    payload = decision_request(store, job)
    payload[field] = value
    assert_error('INVALID_INPUT_FORMAT', lambda: store.create_decision(job['result']['report_id'], payload, OWNER, 'key'))


def test_forged_report_hash_rejected(store):
    job = draft_job(store)
    payload = decision_request(store, job)
    payload['report']['content_sha256'] = '0' * 64
    assert_error('VERSION_CONFLICT', lambda: store.create_decision(job['result']['report_id'], payload, OWNER, 'key'))


def test_decision_race_one_revision_only_and_changed_replay_conflicts(store):
    job = draft_job(store)
    report_id = job['result']['report_id']
    payload = decision_request(store, job)
    def decide(number):
        try:
            return store.create_decision(report_id, payload, OWNER, 'key-' + str(number))['revision']
        except WorkflowError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, [1, 2]))
    assert results.count(1) == 1 and results.count('VERSION_CONFLICT') == 1
    winner = results.index(1) + 1
    changed = dict(payload, action='reject')
    assert_error('IDEMPOTENCY_CONFLICT', lambda: store.create_decision(report_id, changed, OWNER, f'key-{winner}'))
    second = store.create_decision(report_id, decision_request(store, job, 1, 'request_changes'), OWNER, 'changes')
    assert second['revision'] == 2


def test_accepted_export_freezes_decisions_but_allows_original_replay(store):
    job = draft_job(store)
    report_id = job['result']['report_id']
    payload = decision_request(store, job)
    original = store.create_decision(report_id, payload, OWNER, 'original')
    with store.transaction() as connection:
        connection.execute('INSERT INTO delivery_export_locks VALUES (?,?,?)', (report_id, 1, 'delivery-synthetic'))
    assert_error('INVALID_STATE', lambda: store.create_decision(report_id, decision_request(store, job, 1, 'reject'), OWNER, 'revoke'))
    assert store.create_decision(report_id, payload, OWNER, 'original') == original


def test_recovery_explicit_only_and_terminal_unchanged(store):
    active = store.create_job(request(), OWNER, 'active')
    terminal = draft_job(store, 'terminal')
    restarted = SQLiteStore(store.path)
    assert restarted.get_job(active['job_id'], OWNER)['status'] == 'queued'
    recovered = restarted.recover_active_jobs()
    assert len(recovered) == 1 and recovered[0]['status'] == 'failed'
    assert recovered[0]['result']['code'] == 'DEPENDENCY_UNAVAILABLE'
    assert restarted.get_job(terminal['job_id'], OWNER) == terminal
    assert restarted.recover_active_jobs() == []


def test_overdue_recovery_and_forged_old_completion_rejected(store):
    job = store.create_job(request(), OWNER, 'short', .01)
    running = store.transition_job(job['job_id'], 0, dict(job, status='running', revision=1))
    report = bundle(1)['outcomes'][0]['result']
    report['generated_at'] = job['accepted_at']
    time.sleep(.02)
    assert_error('DEADLINE_EXCEEDED', lambda: store.transition_job(job['job_id'], 1,
        dict(running, revision=2, status='succeeded', completed_at=job['accepted_at'], result=report)))
    assert store.recover_active_jobs()[0]['status'] == 'timed_out'


def test_transaction_rollback_and_foreign_keys(store):
    with pytest.raises(RuntimeError):
        with store.transaction() as connection:
            connection.execute("INSERT INTO idempotency VALUES ('owner','op','key','digest','{}')")
            raise RuntimeError('synthetic rollback')
    with store.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM idempotency').fetchone()[0] == 0
        assert connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert connection.execute('PRAGMA busy_timeout').fetchone()[0] == 2000


def test_database_lock_failure_bounded_and_safe(store):
    locking = sqlite3.connect(str(store.path), isolation_level=None)
    try:
        locking.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        assert_error('NOT_READY', lambda: store.create_job(request(), OWNER, 'locked'))
        assert time.monotonic() - started < 4
    finally:
        locking.rollback()
        locking.close()


def test_path_escape_rejected_before_writing():
    assert_error('INVALID_INPUT_FORMAT', lambda: SQLiteStore(WORKFLOW_ROOT.parent.parent.parent / 'original.sqlite3'))
    assert_error('INVALID_INPUT_FORMAT', lambda: SQLiteStore(WORKFLOW_ROOT / '..' / 'escape.sqlite3'))
