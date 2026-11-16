"""Operator review state follows durable acceptance, not delivery display status."""
import uuid

import pytest

from src.workflow.auth import Principal
from src.workflow.delivery import DeliveryService
from src.workflow.store import SQLiteStore, WORKFLOW_ROOT, WorkflowError, utc_now
from tests.evaluator.fixtures import bundle


OWNER = Principal('review-owner')


@pytest.fixture
def store():
    return SQLiteStore(WORKFLOW_ROOT / 'review-state-tests' / uuid.uuid4().hex / 'state.sqlite3')


def draft(store, *, report_id=None, version=1):
    control = bundle(1)
    request = {'schema_version': '1.0', 'incident_id': 'INC-000001',
               'window': control['cases'][0]['context']['window']}
    job = store.create_job(request, OWNER, 'job-' + uuid.uuid4().hex)
    job = store.transition_job(job['job_id'], 0, dict(job, status='running', revision=1))
    report = control['outcomes'][0]['result']
    report.update(report_id=report_id or 'report-' + uuid.uuid4().hex,
                  report_version=version, generated_at=job['accepted_at'])
    return store.transition_job(job['job_id'], 1, dict(
        job, status='succeeded', revision=2, completed_at=utc_now(), result=report))


def review_state(store, job, principal=OWNER):
    report = job['result']
    return store.report_review_state(report['report_id'], report['report_version'], principal)


def decide(store, job, action='approve'):
    report = job['result']
    reference = store.report_ref(report['report_id'], report['report_version'], OWNER)
    return store.create_decision(reference.report_id, {
        'schema_version': '1.0', 'report': reference.model_dump(mode='json'),
        'expected_revision': store.decision_revision(reference.report_id, reference.report_version, OWNER),
        'action': action, 'reason': 'Synthetic decision for review-state verification.'
    }, OWNER, 'decision-' + uuid.uuid4().hex)


def delivery(service, job, decision=None, *, operation='export'):
    report = job['result']
    return service.create({
        'schema_version': '1.0', 'job_id': job['job_id'],
        'report': service.store.report_ref(report['report_id'], report['report_version'], OWNER).model_dump(mode='json'),
        'decision_id': decision['decision_id'] if decision else None,
        'destination': 'simulator-export' if operation == 'export' else 'simulator-manual-queue',
        'operation': operation,
    }, OWNER, 'delivery-' + uuid.uuid4().hex)


def assert_error(code, operation):
    with pytest.raises(WorkflowError) as caught:
        operation()
    assert caught.value.code == code


def test_review_without_decision_and_latest_revision_survive_reopen(store):
    job = draft(store)
    assert review_state(store, job) == {'latest_decision': None, 'decision_locked': False}
    decide(store, job, 'request_changes')
    current = decide(store, job, 'reject')
    reopened = SQLiteStore(store.path)
    assert review_state(reopened, job) == {'latest_decision': current, 'decision_locked': False}
    assert current['revision'] == 2


def test_only_owner_or_supervisor_can_read_review_state(store):
    job = draft(store)
    current = decide(store, job)
    assert_error('FORBIDDEN', lambda: review_state(store, job, Principal('unrelated-operator')))
    assert review_state(store, job, Principal('review-supervisor', 'supervisor')) == {
        'latest_decision': current, 'decision_locked': False}
    assert_error('NOT_FOUND', lambda: store.report_review_state('missing-report', 1, OWNER))


def test_created_pending_export_does_not_freeze_decision(store):
    job = draft(store)
    service = DeliveryService(store)
    first = decide(store, job)
    pending = delivery(service, job, first)
    assert pending.status == 'pending'
    assert review_state(store, job) == {'latest_decision': first, 'decision_locked': False}
    changed = decide(store, job, 'request_changes')
    assert review_state(store, job)['latest_decision'] == changed


def test_accepted_unpublished_export_is_locked_even_while_pending(store, monkeypatch):
    job = draft(store)
    service = DeliveryService(store)
    current = decide(store, job)
    pending = delivery(service, job, current)

    def interrupt_publication(*args):
        raise RuntimeError('Synthetic interruption after durable acceptance.')

    monkeypatch.setattr(service, '_publish', interrupt_publication)
    with pytest.raises(RuntimeError):
        service.dispatch(pending.delivery_id, OWNER, 0)
    assert service.get(pending.delivery_id, OWNER).status == 'pending'
    assert review_state(SQLiteStore(store.path), job) == {
        'latest_decision': current, 'decision_locked': True}
    assert_error('INVALID_STATE', lambda: decide(store, job, 'reject'))


def test_failed_partial_acceptance_rolls_back_lock_then_retry_freezes(store, monkeypatch):
    job = draft(store)
    service = DeliveryService(store)
    current = decide(store, job)
    pending = delivery(service, job, current)
    accept = service._accept

    def fail_after_partial_accept(*args):
        accept(*args)
        raise RuntimeError('Synthetic adapter failure before acceptance commit.')

    monkeypatch.setattr(service, '_accept', fail_after_partial_accept)
    failed = service.dispatch(pending.delivery_id, OWNER, 0)
    assert failed.status == 'failed'
    assert review_state(store, job) == {'latest_decision': current, 'decision_locked': False}
    monkeypatch.setattr(service, '_accept', accept)
    delivered = service.dispatch(pending.delivery_id, OWNER, failed.revision)
    assert delivered.status == 'delivered'
    assert review_state(store, job) == {'latest_decision': current, 'decision_locked': True}


def test_other_failed_delivery_does_not_hide_existing_export_lock(store, monkeypatch):
    job = draft(store)
    service = DeliveryService(store)
    current = decide(store, job)
    first = delivery(service, job, current)
    service.dispatch(first.delivery_id, OWNER, 0)
    later = delivery(service, job, current)

    def unavailable(*args):
        raise RuntimeError('Synthetic later delivery failure.')

    monkeypatch.setattr(service, '_accept', unavailable)
    assert service.dispatch(later.delivery_id, OWNER, 0).status == 'failed'
    assert review_state(store, job) == {'latest_decision': current, 'decision_locked': True}
    assert_error('INVALID_STATE', lambda: decide(store, job, 'reject'))


def test_manual_queue_does_not_freeze_decision(store):
    job = draft(store)
    service = DeliveryService(store)
    pending = delivery(service, job, operation='manual_queue')
    service.dispatch(pending.delivery_id, OWNER, 0)
    assert review_state(store, job) == {'latest_decision': None, 'decision_locked': False}
    current = decide(store, job)
    assert review_state(store, job)['latest_decision'] == current


def test_lock_and_latest_decision_are_scoped_to_exact_report_version(store):
    first = draft(store, report_id='report-versioned', version=1)
    second = draft(store, report_id='report-versioned', version=2)
    first_decision = decide(store, first)
    second_decision = decide(store, second, 'request_changes')
    service = DeliveryService(store)
    pending = delivery(service, first, first_decision)
    service.dispatch(pending.delivery_id, OWNER, 0)
    assert review_state(store, first) == {'latest_decision': first_decision, 'decision_locked': True}
    assert review_state(store, second) == {'latest_decision': second_decision, 'decision_locked': False}
    assert_error('NOT_FOUND', lambda: store.report_review_state('report-versioned', 3, OWNER))
