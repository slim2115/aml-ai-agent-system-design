"""Mechanically labelled local outbox controls, not AML/production acceptance."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from src.contracts.v1 import DeliveryRequest
from src.workflow.auth import Principal
from src.workflow.delivery import DeliveryService
from src.workflow.store import SQLiteStore, WorkflowError, utc_now
from tests.evaluator.fixtures import bundle


@pytest.fixture
def setup_delivery():
    # Retain isolated SQLite evidence under the new project's var only.
    root = Path(__file__).resolve().parents[2]
    store = SQLiteStore(root / 'var' / 'workflow' / 'delivery-tests' / uuid.uuid4().hex / 'state.sqlite3')
    service = DeliveryService(store)
    owner = Principal('test-owner')
    return store, service, owner


def make_job(store, owner, status='succeeded'):
    control = bundle(1)
    request = {'schema_version': '1.0', 'incident_id': 'INC-000001',
               'window': control['cases'][0]['context']['window']}
    job = store.create_job(request, owner, 'job-' + uuid.uuid4().hex)
    if status == 'queued':
        return job
    running = dict(job, status='running', revision=1)
    job = store.transition_job(job['job_id'], 0, running)
    if status == 'running':
        return job
    end = dict(job, status=status, revision=2, completed_at=utc_now())
    if status == 'succeeded':
        report = control['outcomes'][0]['result']
        report.update(report_id='report-' + uuid.uuid4().hex, generated_at=job['accepted_at'])
        end['result'] = report
    elif status == 'refused':
        end['result'] = {'schema_version': '1.0', 'status': 'refusal', 'incident_id': job['incident_id'],
                         'generated_at': job['accepted_at'], 'data_completeness': 'insufficient',
                         'reason_code': 'INSUFFICIENT_DATA', 'reason': 'Synthetic incomplete source.',
                         'recommended_action': 'manual_review'}
    elif status == 'failed':
        end['result'] = {'schema_version': '1.0', 'status': 'error', 'generated_at': job['accepted_at'],
                         'correlation_id': 'synthetic-error', 'code': 'DEPENDENCY_UNAVAILABLE',
                         'message': 'Synthetic dependency failure.', 'retryable': True}
    elif status == 'timed_out':
        end['completed_at'] = job['deadline_at']
    return store.transition_job(job['job_id'], 1, end)


def decide(store, owner, report, action='approve'):
    reference = store.report_ref(report['report_id'], report['report_version'], owner)
    revision = store.decision_revision(reference.report_id, reference.report_version, owner)
    return store.create_decision(reference.report_id, {
        'schema_version': '1.0', 'report': reference.model_dump(mode='json'),
        'expected_revision': revision, 'action': action, 'reason': 'Synthetic local decision.'
    }, owner, 'decision-' + uuid.uuid4().hex)


def export_request(store, owner, job, decision):
    return DeliveryRequest(schema_version='1.0', job_id=job['job_id'],
                           decision_id=decision['decision_id'],
                           report=store.report_ref(job['result']['report_id'], 1, owner),
                           destination='simulator-export', operation='export')


def approved_export(setup_delivery):
    store, service, owner = setup_delivery
    job = make_job(store, owner)
    decision = decide(store, owner, job['result'])
    request = export_request(store, owner, job, decision)
    pending = service.create(request, owner, 'export-1')
    return store, service, owner, job, request, pending


def assert_code(code, operation):
    with pytest.raises(WorkflowError) as caught:
        operation()
    assert caught.value.code == code
    assert str(caught.value) == code


def test_pending_is_durable_before_send_and_exact_report_is_accepted(setup_delivery):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    restarted = DeliveryService(SQLiteStore(store.path))
    assert restarted.get(pending.delivery_id, owner) == pending
    with store.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM delivery_simulator_receipts').fetchone()[0] == 0
    delivered = restarted.dispatch(pending.delivery_id, owner, 0)
    assert delivered.status == 'delivered'
    assert delivered.mode == delivered.receipt.mode == 'local_simulator'
    assert delivered.revision == 2 and delivered.attempt_count == 1
    assert delivered.receipt.report == request.report
    with store.transaction() as connection:
        row = connection.execute('SELECT * FROM delivery_simulator_receipts').fetchone()
        assert hashlib.sha256(row['report_payload']).hexdigest() == request.report.content_sha256
        assert json.loads(row['report_payload']) == job['result']
    assert restarted.list_for_job(job['job_id'], owner) == [delivered]
    artifact = restarted.export_artifact(pending.delivery_id, owner)
    assert json.loads(artifact) == job['result']
    assert hashlib.sha256(artifact).hexdigest() == request.report.content_sha256
    assert_code('FORBIDDEN', lambda: restarted.export_artifact(pending.delivery_id, Principal('other')))


def test_same_key_same_request_is_stable_different_payload_conflicts(setup_delivery):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    assert service.create(request, owner, 'export-1') == pending
    changed = request.model_dump(mode='json')
    changed['decision_id'] = 'different-decision'
    assert_code('IDEMPOTENCY_CONFLICT', lambda: service.create(changed, owner, 'export-1'))
    delivered = service.dispatch(pending.delivery_id, owner, 0)
    assert service.dispatch(pending.delivery_id, owner, 0) == delivered
    assert service.create(request, owner, 'export-1') == pending  # original response, get() gives current
    with store.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM delivery_simulator_receipts').fetchone()[0] == 1


def test_new_key_is_a_new_deliberate_export_of_same_frozen_report(setup_delivery):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    first = service.dispatch(pending.delivery_id, owner, 0)
    second_pending = service.create(request, owner, 'export-2')
    second = service.dispatch(second_pending.delivery_id, owner, 0)
    assert second.receipt.receipt_id != first.receipt.receipt_id
    assert second.receipt.report == first.receipt.report


@pytest.mark.parametrize('actor', [Principal('other'), Principal('supervisor', 'supervisor')])
def test_only_owner_can_create_or_dispatch_even_supervisor_cannot_mutate(setup_delivery, actor):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    assert_code('FORBIDDEN', lambda: service.create(request, actor, 'foreign-create'))
    assert_code('FORBIDDEN', lambda: service.dispatch(pending.delivery_id, actor, 0))
    if actor.role == 'supervisor':
        assert service.get(pending.delivery_id, actor) == pending
    else:
        assert_code('FORBIDDEN', lambda: service.get(pending.delivery_id, actor))
        assert_code('FORBIDDEN', lambda: service.list_for_job(job['job_id'], actor))


@pytest.mark.parametrize('revision,code', [(1, 'VERSION_CONFLICT'), (-1, 'INVALID_INPUT_FORMAT'),
                                         (True, 'INVALID_INPUT_FORMAT'), ('0', 'INVALID_INPUT_FORMAT')])
def test_dispatch_requires_valid_current_revision(setup_delivery, revision, code):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    assert_code(code, lambda: service.dispatch(pending.delivery_id, owner, revision))
    assert service.get(pending.delivery_id, owner).attempt_count == 0


@pytest.mark.parametrize('action', ['reject', 'request_changes', 'approve'])
def test_any_new_decision_invalidates_pending_export_binding(setup_delivery, action):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    decide(store, owner, job['result'], action)
    assert_code('INVALID_STATE', lambda: service.dispatch(pending.delivery_id, owner, 0))
    assert service.get(pending.delivery_id, owner).attempt_count == 0
    with store.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM delivery_simulator_receipts').fetchone()[0] == 0


@pytest.mark.parametrize('action', ['reject', 'request_changes'])
def test_export_requires_approval_not_just_any_decision(setup_delivery, action):
    store, service, owner = setup_delivery
    job = make_job(store, owner)
    decision = decide(store, owner, job['result'], action)
    assert_code('INVALID_STATE', lambda: service.create(export_request(store, owner, job, decision), owner, 'export'))


def test_receipt_acceptance_freezes_decisions(setup_delivery):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    service.dispatch(pending.delivery_id, owner, 0)
    assert_code('INVALID_STATE', lambda: decide(store, owner, job['result'], 'reject'))


def test_crash_after_accept_before_publish_recovers_exact_receipt(setup_delivery, monkeypatch):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    def interrupted(*args):
        raise RuntimeError('synthetic process interruption after durable acceptance')
    monkeypatch.setattr(service, '_publish', interrupted)
    with pytest.raises(RuntimeError):
        service.dispatch(pending.delivery_id, owner, 0)
    assert service.get(pending.delivery_id, owner).status == 'pending'
    assert_code('INVALID_STATE', lambda: decide(store, owner, job['result'], 'reject'))
    with store.transaction() as connection:
        before = dict(connection.execute('SELECT * FROM delivery_simulator_receipts').fetchone())
    restarted = DeliveryService(SQLiteStore(store.path))
    delivered = restarted.dispatch(pending.delivery_id, owner, 0)  # stale pre-crash revision is safe replay
    assert delivered.receipt.receipt_id == before['receipt_id']
    assert delivered.attempt_count == 1
    with store.transaction() as connection:
        assert dict(connection.execute('SELECT * FROM delivery_simulator_receipts').fetchone()) == before


def test_partial_failed_accept_rolls_back_receipt_lock_and_retries(setup_delivery, monkeypatch):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    original = service._accept
    def partially_failed(*args):
        original(*args)
        raise RuntimeError('do not expose source or raw adapter errors')
    monkeypatch.setattr(service, '_accept', partially_failed)
    failed = service.dispatch(pending.delivery_id, owner, 0)
    assert failed.status == 'failed' and failed.revision == failed.attempt_count == 1
    assert failed.error.code == 'DEPENDENCY_UNAVAILABLE'
    assert 'raw adapter' not in failed.error.message
    with store.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM delivery_simulator_receipts').fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM delivery_export_locks').fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM audit_events WHERE event_code = 'SIMULATOR_ACCEPTED'").fetchone()[0] == 0
    monkeypatch.setattr(service, '_accept', original)
    delivered = service.dispatch(pending.delivery_id, owner, 1)
    assert delivered.status == 'delivered' and delivered.attempt_count == 2


def test_concurrent_dispatchers_share_one_receipt(setup_delivery):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.dispatch(pending.delivery_id, owner, 0), range(2)))
    assert results[0] == results[1]
    assert results[0].attempt_count == 1


@pytest.mark.parametrize('status', ['refused', 'failed', 'timed_out', 'cancelled'])
def test_manual_queue_accepts_terminal_without_report_or_decision(setup_delivery, status):
    store, service, owner = setup_delivery
    job = make_job(store, owner, status)
    request = {'schema_version': '1.0', 'job_id': job['job_id'], 'report': None, 'decision_id': None,
               'destination': 'simulator-manual-queue', 'operation': 'manual_queue'}
    pending = service.create(request, owner, 'manual')
    delivered = service.dispatch(pending.delivery_id, owner, 0)
    assert delivered.receipt.report is None and delivered.receipt.job_id == job['job_id']
    with store.transaction() as connection:
        row = connection.execute('SELECT * FROM delivery_simulator_receipts').fetchone()
        assert row['report_payload'] is None and row['report_sha256'] is None


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_active_jobs_cannot_be_delivered(setup_delivery, status):
    store, service, owner = setup_delivery
    job = make_job(store, owner, status)
    request = {'schema_version': '1.0', 'job_id': job['job_id'], 'report': None, 'decision_id': None,
               'destination': 'simulator-manual-queue', 'operation': 'manual_queue'}
    assert_code('INVALID_STATE', lambda: service.create(request, owner, 'manual'))


def test_succeeded_manual_queue_requires_exact_report_but_no_approval(setup_delivery):
    store, service, owner = setup_delivery
    job = make_job(store, owner)
    request = {'schema_version': '1.0', 'job_id': job['job_id'], 'report': None, 'decision_id': None,
               'destination': 'simulator-manual-queue', 'operation': 'manual_queue'}
    assert_code('INVALID_STATE', lambda: service.create(request, owner, 'manual'))
    request['report'] = store.report_ref(job['result']['report_id'], 1, owner).model_dump(mode='json')
    pending = service.create(request, owner, 'manual')
    service.dispatch(pending.delivery_id, owner, 0)
    assert decide(store, owner, job['result'])['action'] == 'approve'  # manual queue does not freeze decisions


@pytest.mark.parametrize('field', ['job_id', 'hash', 'version', 'decision_id'])
def test_mismatched_report_job_hash_version_or_decision_rejected(setup_delivery, field):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    data = request.model_dump(mode='json')
    if field == 'job_id':
        data['job_id'] = make_job(store, owner)['job_id']
    elif field == 'hash':
        data['report']['content_sha256'] = '0' * 64
    elif field == 'version':
        data['report']['report_version'] = 2
    else:
        data['decision_id'] = 'unknown-decision'
    assert_code('NOT_FOUND' if field == 'version' else 'INVALID_STATE',
                lambda: service.create(data, owner, 'mismatch'))


@pytest.mark.parametrize('destination', ['external-export', 'signed-export', 'case-closure', 'http://localhost'])
def test_external_signature_and_closure_are_not_ready(setup_delivery, destination):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    data = request.model_dump(mode='json')
    data['destination'] = destination
    expected = 'INVALID_INPUT_FORMAT' if destination.startswith('http:') else 'NOT_READY'
    assert_code(expected, lambda: service.create(data, owner, 'external'))


@pytest.mark.parametrize('extra', [{'receipt': {'outcome': 'accepted'}}, {'mode': 'external'}, {'accepted': True}])
def test_client_cannot_assert_acceptance_or_inject_receipt(setup_delivery, extra):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    assert_code('INVALID_INPUT_FORMAT', lambda: service.create({**request.model_dump(mode='json'), **extra}, owner, 'forged'))


@pytest.mark.parametrize('field,value', [('mode', 'external'), ('job_id', 'job-other'),
                                       ('destination', 'external'), ('operation', 'manual_queue'),
                                       ('receipt_id', 'forged-receipt')])
def test_corrupted_receipt_is_never_confirmed(setup_delivery, field, value):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    service.dispatch(pending.delivery_id, owner, 0)
    with store.transaction() as connection:
        receipt = json.loads(connection.execute('SELECT receipt_json FROM delivery_simulator_receipts').fetchone()[0])
        receipt[field] = value
        connection.execute('UPDATE delivery_simulator_receipts SET receipt_json = ?', (json.dumps(receipt),))
    assert_code('INTERNAL_ERROR', lambda: service.get(pending.delivery_id, owner))
    assert_code('INTERNAL_ERROR', lambda: service.dispatch(pending.delivery_id, owner, 0))


def test_corrupted_export_bytes_are_detected(setup_delivery):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    service.dispatch(pending.delivery_id, owner, 0)
    with store.transaction() as connection:
        connection.execute('UPDATE delivery_simulator_receipts SET report_payload = ?', (b'altered',))
    assert_code('INTERNAL_ERROR', lambda: service.get(pending.delivery_id, owner))
    assert_code('INTERNAL_ERROR', lambda: service.list_for_job(job['job_id'], owner))
    assert_code('INTERNAL_ERROR', lambda: service.export_artifact(pending.delivery_id, owner))


def test_export_artifact_requires_confirmed_export(setup_delivery):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    assert_code('INVALID_STATE', lambda: service.export_artifact(pending.delivery_id, owner))
    manual_job = make_job(store, owner, 'failed')
    manual = service.create({'schema_version': '1.0', 'job_id': manual_job['job_id'],
                             'decision_id': None, 'report': None, 'destination': 'simulator-manual-queue',
                             'operation': 'manual_queue'}, owner, 'manual-artifact')
    service.dispatch(manual.delivery_id, owner, 0)
    assert_code('INVALID_STATE', lambda: service.export_artifact(manual.delivery_id, owner))


def test_not_found_is_safe_and_ids_are_bound_sql_parameters(setup_delivery):
    store, service, owner, job, request, pending = approved_export(setup_delivery)
    assert_code('NOT_FOUND', lambda: service.get("' OR 1=1 --", owner))
    assert service.get(pending.delivery_id, owner) == pending
