"""Guided operator flow against real durable decisions and local delivery.

The investigation boundary is synchronous and synthetic. Sources are resolved
from the shipped demo, SQLite is isolated, and conftest forbids networking.
AppTest exercises reruns and widget cleanup, not browser timers or model speed.
"""
from copy import deepcopy
from hashlib import sha256
import json
from uuid import uuid4

import pytest

from src.contracts.v1 import CreateJobRequest, DeliveryRequest
from src.runtime.policy import load_demo_policy
from src.workflow.auth import trusted_local_principal
from src.workflow.delivery import DeliveryService
from src.workflow.store import SQLiteStore, WorkflowError, utc_now
from tests.workflow.test_sources import demo_report
from tests.workflow.test_ui_readiness import APP, DEMO, PREPARE, SUBMIT, stage, widget


SAVE = 'Сохранить учебное решение'
EXPORT = 'Экспортировать в локальный симулятор'
CONFIRM = 'Понимаю: после принятия экспорта решение по этой версии изменить нельзя'
MANUAL = 'Зарегистрировать в локальной учебной очереди'
REASON = 'Обоснование решения'


class SyntheticService:
    def __init__(self, store):
        self.store = store
        self.state = {'ready': False, 'code': 'NOT_READY', 'state': 'not_ready'}
        self.prepare_calls = self.submit_calls = self.constructions = 0

    def readiness(self):
        return dict(self.state)

    def prepare(self):
        self.prepare_calls += 1
        self.state = {'ready': True, 'code': 'READY', 'state': 'ready'}
        return self.readiness()

    def submit(self, request, principal, idempotency_key, *, engine='template'):
        self.submit_calls += 1
        if not self.state['ready']:
            raise WorkflowError('NOT_READY')
        job = self.store.create_job(request, principal, idempotency_key, engine=engine)
        self.finish(job)
        return job

    def finish(self, job):
        if job['status'] == 'queued':
            job = self.store.transition_job(job['job_id'], job['revision'],
                                            dict(job, status='running', revision=job['revision'] + 1))
        report = demo_report()
        report.update(report_id='guided-' + uuid4().hex, incident_id=job['incident_id'],
                      generated_at=utc_now(), confidence=None)
        return self.store.transition_job(job['job_id'], job['revision'],
            dict(job, status='succeeded', revision=job['revision'] + 1,
                 completed_at=utc_now(), result=report))

    def close(self):
        pass


@pytest.fixture
def page(monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    import src.workflow.controller as controller

    store = SQLiteStore(APP.parents[2] / 'var/workflow/ui-guided-tests' / uuid4().hex / 'workflow.sqlite3')
    service = SyntheticService(store)
    principal = trusted_local_principal()
    policy = load_demo_policy(APP.parents[2] / 'config/demo-policy.json')

    def create_job(*, completed=True):
        request = CreateJobRequest(schema_version='1.0', incident_id='INC-000123', window=policy.window)
        job = store.create_job(request, principal, 'fixture-' + uuid4().hex)
        return service.finish(job) if completed else job

    job = create_job()
    st.cache_resource.clear()

    def construct():
        service.constructions += 1
        return service

    monkeypatch.setattr(controller, 'WorkflowService', construct)
    ui = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not ui.exception
    yield ui, service, principal, job, create_job
    st.cache_resource.clear()


def open_job(ui, job):
    widget(ui, 'selectbox', 'Сохранённые задания').set_value(job['job_id']).run()
    widget(ui, 'button', 'Открыть сохранённое задание').click().run()
    assert not ui.exception
    assert ui.session_state['active_job'] == job['job_id']


def decide(store, principal, job, action='approve', reason='Synthetic reviewed local decision.'):
    report = job['result']
    ref = store.report_ref(report['report_id'], report['report_version'], principal)
    return store.create_decision(ref.report_id, {'schema_version': '1.0',
        'report': ref.model_dump(mode='json'),
        'expected_revision': store.decision_revision(ref.report_id, ref.report_version, principal),
        'action': action, 'reason': reason}, principal, 'decision-' + uuid4().hex)


def fill_review(ui, action='approve', reason='Reviewed the synthetic source values.'):
    widget(ui, 'selectbox', 'Решение').set_value(action).run()
    widget(ui, 'text_area', REASON).set_value(reason).run()


def latest(service, principal, job):
    return service.store.latest_decision(job['result']['report_id'], job['result']['report_version'], principal)


def test_history_opens_sources_without_preparing_or_submitting(page):
    ui, service, principal, job, _ = page
    open_job(ui, job)
    assert widget(ui, 'radio', 'Этап работы').value == '3. Проверка отчёта'
    assert service.prepare_calls == service.submit_calls == 0
    assert service.constructions == 1
    assert not service.readiness()['ready']
    assert ui.dataframe  # Human-readable source values accompany the report.
    assert any('95000' in frame.value.astype(str).to_string() for frame in ui.dataframe)
    assert not any(item.proto.allow_html for item in ui.markdown)
    assert latest(service, principal, job) is None


def test_decision_requires_explicit_choice_and_nonblank_reason(page):
    ui, service, principal, job, _ = page
    open_job(ui, job)
    stage(ui, 4)
    assert widget(ui, 'selectbox', 'Решение').value is None
    assert widget(ui, 'text_area', REASON).value == ''
    assert widget(ui, 'button', SAVE).disabled
    fill_review(ui, reason='   \n ')
    assert widget(ui, 'button', SAVE).disabled
    widget(ui, 'text_area', REASON).set_value('Reviewed source values and period.').run()
    assert not widget(ui, 'button', SAVE).disabled
    widget(ui, 'button', SAVE).click().run()
    assert not ui.exception
    saved = latest(service, principal, job)
    assert saved['action'] == 'approve'
    assert saved['reason'] == 'Reviewed source values and period.'
    assert saved['revision'] == 1


def test_unsaved_review_survives_stage_widget_cleanup_and_is_report_specific(page):
    ui, service, principal, first, create_job = page
    second = create_job()
    ui.run()
    open_job(ui, first)
    stage(ui, 4)
    fill_review(ui, action='request_changes', reason='First report needs source clarification.')
    stage(ui, 3)
    stage(ui, 1)
    stage(ui, 4)
    assert widget(ui, 'selectbox', 'Решение').value == 'request_changes'
    assert widget(ui, 'text_area', REASON).value == 'First report needs source clarification.'
    open_job(ui, second)
    stage(ui, 4)
    assert widget(ui, 'selectbox', 'Решение').value is None
    assert widget(ui, 'text_area', REASON).value == ''
    fill_review(ui, action='reject', reason='Second report has a separate draft.')
    open_job(ui, first)
    stage(ui, 4)
    assert widget(ui, 'selectbox', 'Решение').value == 'request_changes'
    assert widget(ui, 'text_area', REASON).value == 'First report needs source clarification.'
    assert latest(service, principal, first) is latest(service, principal, second) is None


def test_changed_revision_requires_explicit_reread_and_keeps_draft(page):
    ui, service, principal, job, _ = page
    open_job(ui, job)
    stage(ui, 4)
    fill_review(ui, reason='My unsaved justification remains available.')
    competing = decide(service.store, principal, job, 'reject')
    ui.run()
    assert widget(ui, 'button', SAVE).disabled
    widget(ui, 'button', 'Обновить состояние').click().run()
    assert widget(ui, 'button', SAVE).disabled
    assert latest(service, principal, job) == competing
    widget(ui, 'button', 'Перечитать сохранённое решение').click().run()
    assert not ui.exception
    assert widget(ui, 'text_area', REASON).value == 'My unsaved justification remains available.'
    assert not widget(ui, 'button', SAVE).disabled
    widget(ui, 'button', SAVE).click().run()
    saved = latest(service, principal, job)
    assert saved['revision'] == 2 and saved['previous_revision'] == 1
    assert saved['action'] == 'approve'


def test_decision_commit_race_is_rejected_without_overwriting_other_operator(page, monkeypatch):
    ui, service, principal, job, _ = page
    open_job(ui, job)
    stage(ui, 4)
    fill_review(ui, reason='Preserve my explanation after a commit race.')
    original = service.store.create_decision
    competing = None

    def racing(report_id, request, actor, idempotency_key):
        nonlocal competing
        reference = service.store.report_ref(job['result']['report_id'], 1, actor)
        competing = original(report_id, {'schema_version': '1.0',
            'report': reference.model_dump(mode='json'), 'expected_revision': 0,
            'action': 'reject', 'reason': 'Synthetic other operator wins the race.'}, actor, 'race-winner')
        return original(report_id, request, actor, idempotency_key)

    monkeypatch.setattr(service.store, 'create_decision', racing)
    widget(ui, 'button', SAVE).click().run()
    assert not ui.exception
    assert competing is not None and latest(service, principal, job) == competing
    assert any('VERSION_CONFLICT' in item.value for item in ui.error)
    assert widget(ui, 'text_area', REASON).value == 'Preserve my explanation after a commit race.'


@pytest.mark.parametrize('action', ['reject', 'request_changes'])
def test_nonapproval_cannot_export_but_can_use_local_queue(page, action):
    ui, service, principal, job, _ = page
    open_job(ui, job)
    stage(ui, 4)
    fill_review(ui, action=action)
    widget(ui, 'button', SAVE).click().run()
    assert latest(service, principal, job)['action'] == action
    stage(ui, 5)
    assert all(item.disabled for item in ui.button if item.label == EXPORT)
    assert not widget(ui, 'button', MANUAL).disabled


@pytest.mark.parametrize(('status', 'explanation'), [
    ('refused', 'Synthetic incomplete source.'),
    ('failed', 'Synthetic dependency failure.'),
    ('timed_out', 'Время обработки истекло'),
    ('cancelled', 'Задание отменено'),
])
def test_terminal_job_without_report_explains_outcome_and_allows_only_manual_queue(
        page, status, explanation):
    from tests.workflow.test_delivery import make_job

    ui, service, principal, _, _ = page
    job = make_job(service.store, principal, status)
    ui.run()
    open_job(ui, job)
    assert any(explanation == item.value for item in ui.text)
    assert not any(item.label == 'Перейти к решению' for item in ui.button)
    stage(ui, 4)
    assert not any(item.label == 'Решение' for item in ui.selectbox)
    assert not any(item.label == REASON for item in ui.text_area)
    assert not any(item.label in (SAVE, EXPORT) for item in ui.button)
    assert any('готового отчёта нет' in item.value for item in ui.info)
    stage(ui, 5)
    assert not any(item.label in (SAVE, EXPORT) for item in ui.button)
    assert not any(item.label == CONFIRM for item in ui.checkbox)
    assert not widget(ui, 'button', MANUAL).disabled
    widget(ui, 'button', MANUAL).click().run()
    assert not ui.exception
    record, = DeliveryService(service.store).list_for_job(job['job_id'], principal)
    assert record.operation == 'manual_queue' and record.status == 'delivered'
    assert record.report is None and record.decision_id is None
    assert service.store.get_job(job['job_id'], principal) == job
    assert service.prepare_calls == service.submit_calls == 0


def test_export_requires_confirmation_and_returns_exact_saved_report(page):
    ui, service, principal, job, _ = page
    saved = decide(service.store, principal, job)
    open_job(ui, job)
    stage(ui, 4)
    fill_review(ui, action='reject', reason='Unsaved alternative must not appear as the exported decision.')
    stage(ui, 5)
    assert widget(ui, 'checkbox', CONFIRM).value is False
    assert widget(ui, 'button', EXPORT).disabled
    widget(ui, 'checkbox', CONFIRM).set_value(True).run()
    assert not widget(ui, 'button', EXPORT).disabled
    widget(ui, 'button', EXPORT).click().run()
    assert not ui.exception
    deliveries = DeliveryService(service.store)
    records = deliveries.list_for_job(job['job_id'], principal)
    assert len(records) == 1 and records[0].status == 'delivered'
    payload = deliveries.export_artifact(records[0].delivery_id, principal)
    assert json.loads(payload) == job['result']
    assert sha256(payload).hexdigest() == records[0].report.content_sha256
    assert len(ui.get('download_button')) == 1
    stage(ui, 4)
    assert widget(ui, 'selectbox', 'Решение').disabled
    assert widget(ui, 'selectbox', 'Решение').value == saved['action']
    assert widget(ui, 'text_area', REASON).disabled
    assert widget(ui, 'text_area', REASON).value == saved['reason']
    assert widget(ui, 'button', SAVE).disabled
    reference = records[0].report
    draft = ui.session_state['review_drafts'][
        (reference.report_id, reference.report_version, reference.content_sha256)]
    assert draft['action'] == 'reject'
    assert draft['reason'] == 'Unsaved alternative must not appear as the exported decision.'


def test_receipt_acceptance_locks_ui_before_publication_and_retry_uses_same_delivery(page, monkeypatch):
    ui, service, principal, job, _ = page
    saved = decide(service.store, principal, job)
    reference = service.store.report_ref(job['result']['report_id'], 1, principal)
    deliveries = DeliveryService(service.store)
    pending = deliveries.create(DeliveryRequest(schema_version='1.0', job_id=job['job_id'], report=reference,
        decision_id=saved['decision_id'], destination='simulator-export', operation='export'), principal, 'pending-export')

    def interrupted(*args):
        raise RuntimeError('synthetic interruption after durable acceptance')

    with monkeypatch.context() as patch:
        patch.setattr(deliveries, '_publish', interrupted)
        with pytest.raises(RuntimeError):
            deliveries.dispatch(pending.delivery_id, principal, pending.revision)
    assert deliveries.get(pending.delivery_id, principal).status == 'pending'
    open_job(ui, job)
    stage(ui, 4)
    assert widget(ui, 'selectbox', 'Решение').disabled
    assert widget(ui, 'text_area', REASON).disabled
    assert widget(ui, 'button', SAVE).disabled
    stage(ui, 5)
    widget(ui, 'button', 'Повторить локальную доставку').click().run()
    assert not ui.exception
    records = deliveries.list_for_job(job['job_id'], principal)
    assert len(records) == 1
    assert records[0].delivery_id == pending.delivery_id
    assert records[0].status == 'delivered' and records[0].attempt_count == 1


def test_failed_simulator_acceptance_shows_warning_and_retry_recovers_same_delivery(page, monkeypatch):
    ui, service, principal, job, _ = page
    saved = decide(service.store, principal, job)
    deliveries = DeliveryService(service.store)
    open_job(ui, job)
    stage(ui, 5)
    widget(ui, 'checkbox', CONFIRM).set_value(True).run()

    def unavailable(*args):
        raise OSError('Synthetic simulator unavailable before acceptance.')

    with monkeypatch.context() as patch:
        patch.setattr(DeliveryService, '_accept', unavailable)
        widget(ui, 'button', EXPORT).click().run()
        assert not ui.exception
        assert not ui.success
        assert any('Доставка не подтверждена' in item.value for item in ui.warning)
        failed, = deliveries.list_for_job(job['job_id'], principal)
        assert failed.status == 'failed' and failed.attempt_count == 1
        assert failed.error.code == 'DEPENDENCY_UNAVAILABLE'
        assert not service.store.report_review_state(
            job['result']['report_id'], 1, principal)['decision_locked']
        assert not ui.get('download_button')
        widget(ui, 'button', 'Повторить локальную доставку').click().run()
        assert not ui.exception
        assert not ui.success
        assert any('Доставка по-прежнему не подтверждена' in item.value for item in ui.warning)
        retried, = deliveries.list_for_job(job['job_id'], principal)
        assert retried.delivery_id == failed.delivery_id
        assert retried.status == 'failed' and retried.attempt_count == 2

    widget(ui, 'button', 'Повторить локальную доставку').click().run()
    assert not ui.exception
    delivered, = deliveries.list_for_job(job['job_id'], principal)
    assert delivered.delivery_id == failed.delivery_id
    assert delivered.status == 'delivered' and delivered.attempt_count == 3
    assert json.loads(deliveries.export_artifact(delivered.delivery_id, principal)) == job['result']
    assert len(ui.get('download_button')) == 1
    assert service.store.report_review_state(job['result']['report_id'], 1, principal) == {
        'latest_decision': saved, 'decision_locked': True}


@pytest.mark.parametrize('previous_status', ['pending', 'failed'])
def test_new_approval_can_export_while_retry_of_unaccepted_old_decision_is_disabled(
        page, monkeypatch, previous_status):
    ui, service, principal, job, _ = page
    first = decide(service.store, principal, job)
    reference = service.store.report_ref(job['result']['report_id'], 1, principal)
    deliveries = DeliveryService(service.store)
    previous = deliveries.create(DeliveryRequest(schema_version='1.0', job_id=job['job_id'], report=reference,
        decision_id=first['decision_id'], destination='simulator-export', operation='export'),
        principal, 'old-decision-export')
    if previous_status == 'failed':
        def unavailable(*args):
            raise OSError('Synthetic export has not been accepted.')

        with monkeypatch.context() as patch:
            patch.setattr(deliveries, '_accept', unavailable)
            previous = deliveries.dispatch(previous.delivery_id, principal, previous.revision)
    assert previous.status == previous_status
    assert not service.store.report_review_state(reference.report_id, 1, principal)['decision_locked']
    open_job(ui, job)
    stage(ui, 4)
    fill_review(ui, reason='Rechecked the sources; saving a new approval revision.')
    widget(ui, 'button', SAVE).click().run()
    assert not ui.exception
    second = latest(service, principal, job)
    assert second['revision'] == 2 and second['action'] == 'approve'
    assert second['decision_id'] != first['decision_id']
    assert widget(ui, 'button', 'Повторить локальную доставку').disabled
    assert widget(ui, 'checkbox', CONFIRM).value is False
    assert widget(ui, 'button', EXPORT).disabled
    widget(ui, 'checkbox', CONFIRM).set_value(True).run()
    assert not widget(ui, 'button', EXPORT).disabled
    widget(ui, 'button', EXPORT).click().run()
    assert not ui.exception
    unchanged, delivered = deliveries.list_for_job(job['job_id'], principal)
    assert unchanged == previous
    assert delivered.delivery_id != previous.delivery_id
    assert delivered.decision_id == second['decision_id']
    assert delivered.status == 'delivered' and delivered.attempt_count == 1
    assert widget(ui, 'button', 'Повторить локальную доставку').disabled
    assert service.store.report_review_state(reference.report_id, 1, principal) == {
        'latest_decision': second, 'decision_locked': True}


def test_unavailable_sources_block_review_and_export_but_allow_local_queue(page, monkeypatch):
    import src.workflow.sources as source_module
    ui, service, principal, job, _ = page
    decide(service.store, principal, job)

    def unavailable(_report):
        raise WorkflowError('NOT_READY')

    monkeypatch.setattr(source_module, 'resolve_report_sources', unavailable)
    open_job(ui, job)
    stage(ui, 4)
    fill_review(ui)
    assert widget(ui, 'button', SAVE).disabled
    stage(ui, 5)
    widget(ui, 'checkbox', CONFIRM).set_value(True).run()
    assert widget(ui, 'button', EXPORT).disabled
    assert not widget(ui, 'button', MANUAL).disabled
    widget(ui, 'button', MANUAL).click().run()
    records = DeliveryService(service.store).list_for_job(job['job_id'], principal)
    assert not ui.exception
    assert len(records) == 1 and records[0].operation == 'manual_queue'
    assert records[0].status == 'delivered'


def test_unavailable_sources_also_block_retry_of_unaccepted_export(page, monkeypatch):
    import src.workflow.sources as source_module
    ui, service, principal, job, _ = page
    saved = decide(service.store, principal, job)
    reference = service.store.report_ref(job['result']['report_id'], 1, principal)
    deliveries = DeliveryService(service.store)
    pending = deliveries.create(DeliveryRequest(schema_version='1.0', job_id=job['job_id'], report=reference,
        decision_id=saved['decision_id'], destination='simulator-export', operation='export'), principal, 'source-retry')

    def unavailable(_report):
        raise WorkflowError('NOT_READY')

    monkeypatch.setattr(source_module, 'resolve_report_sources', unavailable)
    open_job(ui, job)
    stage(ui, 5)
    assert widget(ui, 'button', 'Повторить локальную доставку').disabled
    current = deliveries.get(pending.delivery_id, principal)
    assert current.status == 'pending' and current.attempt_count == 0
    assert not widget(ui, 'button', MANUAL).disabled


@pytest.mark.parametrize('changed', ['incident', 'engine', 'consent'])
def test_changed_investigation_inputs_hide_previous_report_actions(page, changed):
    ui, service, principal, job, _ = page
    open_job(ui, job)
    stage(ui, 4)
    fill_review(ui)
    stage(ui, 2)
    if changed == 'incident':
        widget(ui, 'text_input', 'Идентификатор инцидента').set_value('INC-000250').run()
    elif changed == 'engine':
        widget(ui, 'selectbox', 'Формирование').set_value('local-model').run()
    else:
        widget(ui, 'checkbox', DEMO).set_value(False).run()
    assert 'active_job' not in ui.session_state
    stage(ui, 4)
    assert not any(item.label == SAVE for item in ui.button)
    assert latest(service, principal, job) is None


def test_fabricated_report_html_is_plain_text_and_cannot_authorize(page, monkeypatch):
    ui, service, principal, job, _ = page
    altered = deepcopy(job)
    attack = '<script>alert("synthetic-ui-control")</script>'
    altered['result']['found_facts'][0]['fact'] = attack
    original = service.store.get_job
    monkeypatch.setattr(service.store, 'get_job',
                        lambda job_id, actor: altered if job_id == job['job_id'] else original(job_id, actor))
    open_job(ui, job)
    assert any(attack in item.value for item in ui.text)
    assert not any(item.proto.allow_html for item in ui.markdown)
    stage(ui, 4)
    fill_review(ui)
    assert widget(ui, 'button', SAVE).disabled
    assert latest(service, principal, job) is None


def test_real_submit_persists_new_job_once_after_preparation_and_consent(page):
    ui, service, principal, old_job, _ = page
    widget(ui, 'button', PREPARE).click().run()
    stage(ui, 2)
    widget(ui, 'checkbox', DEMO).set_value(True).run()
    widget(ui, 'button', SUBMIT).click().run()
    assert not ui.exception
    assert service.submit_calls == service.prepare_calls == 1
    current_id = ui.session_state['active_job']
    assert current_id != old_job['job_id']
    assert service.store.get_job(current_id, principal)['status'] == 'succeeded'
    assert len(service.store.list_jobs(principal)) == 2
    ui.run()
    assert service.submit_calls == 1


def test_job_completion_refreshes_while_worker_readiness_stays_busy(page, monkeypatch):
    ui, service, principal, _, create_job = page
    queued = create_job(completed=False)
    service.state = {'ready': False, 'code': 'BUSY', 'state': 'busy'}
    ui.run()
    open_job(ui, queued)
    original = service.store.get_job
    reads = 0

    def completing(job_id, actor):
        nonlocal reads
        if job_id == queued['job_id']:
            reads += 1
            if reads == 2:
                service.finish(original(job_id, actor))
        return original(job_id, actor)

    monkeypatch.setattr(service.store, 'get_job', completing)
    ui.run()
    assert not ui.exception
    assert service.readiness()['state'] == 'busy'
    assert reads >= 3  # First render, changed watcher state, then refreshed view.
    assert ui.dataframe
    assert service.store.get_job(queued['job_id'], principal)['status'] == 'succeeded'
    assert service.prepare_calls == service.submit_calls == 0
