"""Integrated page shares API history while execution stays in the API process."""
from uuid import uuid4

import pytest

from src.contracts.v1 import CreateJobRequest
from src.runtime.policy import load_demo_policy
from src.workflow.auth import trusted_local_principal
from src.workflow.remote import RemoteWorkflowService
from src.workflow.store import SQLiteStore, WorkflowError, utc_now
from tests.workflow.test_ui_guided import SyntheticService, open_job
from tests.workflow.test_ui_readiness import APP, PREPARE, stage, widget


class API:
    def __init__(self):
        self.online = True
        self.prepared = False
        self.prepare_calls = 0
        self.items = []

    def readiness(self):
        if not self.online:
            raise WorkflowError('NOT_READY')
        return {'ready': self.prepared, 'code': 'READY' if self.prepared else 'NOT_READY',
                'state': 'ready' if self.prepared else 'not_ready'}

    def prepare(self):
        self.prepare_calls += 1
        self.prepared = True
        return self.readiness()

    def analysis_requests(self):
        return {'items': self.items, 'limit': 20}


@pytest.fixture
def page(monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    import src.workflow.controller as controller
    import src.workflow.remote as remote

    store = SQLiteStore(APP.parents[2] / 'var/workflow/ui-integrated-tests' / uuid4().hex / 'api-workflow.sqlite3')
    principal = trusted_local_principal()
    policy = load_demo_policy(APP.parents[2] / 'config/demo-policy.json')
    request = CreateJobRequest(schema_version='1.0', incident_id='INC-000123', window=policy.window)
    job = SyntheticService(store).finish(store.create_job(request, principal, 'fixture'))
    active = store.create_job(request, principal, 'active')
    client = API()
    remote_service = RemoteWorkflowService(client=client, store=SQLiteStore(store.path))
    monkeypatch.setenv('AML_WORKFLOW_BACKEND', 'api')
    monkeypatch.setattr(remote, 'RemoteWorkflowService', lambda: remote_service)
    def forbidden(*args, **kwargs):
        raise AssertionError('The integrated UI must not create a local controller')
    monkeypatch.setattr(controller, 'WorkflowService', forbidden)
    monkeypatch.setattr(SQLiteStore, 'recover_active_jobs', forbidden)
    st.cache_resource.clear()
    ui = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not ui.exception
    yield ui, client, store, job, active, principal
    st.cache_resource.clear()


def test_integrated_page_reads_shared_report_and_never_recovers_active_job(page):
    ui, client, store, job, active, principal = page
    assert any('общая история с API и Kafka' in item.value for item in ui.info)
    open_job(ui, job)
    assert ui.dataframe
    assert store.get_job(active['job_id'], principal)['status'] == 'queued'
    assert client.prepare_calls == 0


def test_history_and_human_decision_remain_available_with_api_offline(page):
    ui, client, store, job, active, principal = page
    client.online = False
    ui.run()
    assert not ui.exception
    assert any('API сейчас недоступен' in item.value for item in ui.warning)
    open_job(ui, job)
    stage(ui, 4)
    widget(ui, 'selectbox', 'Решение').set_value('request_changes').run()
    widget(ui, 'text_area', 'Обоснование решения').set_value('Нужна ручная сверка учебного источника.').run()
    widget(ui, 'button', 'Сохранить учебное решение').click().run()
    assert not ui.exception
    latest = store.latest_decision(job['result']['report_id'], 1, principal)
    assert latest['action'] == 'request_changes'
    assert store.get_job(active['job_id'], principal)['status'] == 'queued'


def test_kafka_panel_opens_linked_report_without_model_preparation(page):
    ui, client, store, job, active, principal = page
    client.items = [{'event_id': str(uuid4()), 'incident_id': job['incident_id'], 'status': 'succeeded',
                     'job_id': job['job_id'], 'last_error_code': None}]
    ui.run()
    assert not ui.exception
    assert any('Черновик готов к проверке' in item.value for item in ui.text)
    widget(ui, 'button', 'Открыть задание из Kafka').click().run()
    assert not ui.exception
    assert ui.session_state['active_job'] == job['job_id']
    assert widget(ui, 'radio', 'Этап работы').value == '3. Проверка отчёта'
    assert client.prepare_calls == 0


def test_prepare_is_explicit_and_delegated_to_api(page):
    ui, client, *_ = page
    assert client.prepare_calls == 0
    widget(ui, 'button', PREPARE).click().run()
    assert not ui.exception
    assert client.prepare_calls == 1
    assert any('Поиск готов' in item.value for item in ui.success)


@pytest.mark.parametrize('status', ['refused', 'failed'])
def test_integrated_terminal_failure_cannot_be_reviewed_or_exported(page, status):
    ui, client, store, job, active, principal = page
    running = store.transition_job(active['job_id'], 0, dict(active, status='running', revision=1))
    if status == 'refused':
        result = {'schema_version': '1.0', 'status': 'refusal', 'incident_id': active['incident_id'],
            'generated_at': utc_now(), 'data_completeness': 'unassessed',
            'reason_code': 'INSUFFICIENT_DATA', 'reason': 'Недостаточно учебных данных.',
            'recommended_action': 'manual_review'}
        expected = 'Анализ завершён отказом'
    else:
        result = {'schema_version': '1.0', 'status': 'error', 'generated_at': utc_now(),
            'correlation_id': 'test-failure', 'code': 'INTERNAL_ERROR',
            'message': 'Ошибка учебной обработки.', 'retryable': False}
        expected = 'Анализ не завершён'
    terminal = store.transition_job(active['job_id'], 1,
        dict(running, status=status, revision=2, completed_at=utc_now(), result=result))
    client.items = [{'event_id': str(uuid4()), 'incident_id': active['incident_id'],
                    'status': status, 'job_id': active['job_id'], 'last_error_code': None}]
    ui.run()
    assert any(expected in item.value for item in ui.text)
    widget(ui, 'button', 'Открыть задание из Kafka').click().run()
    assert not ui.exception
    assert any('Готового отчёта нет' in item.value for item in ui.warning)
    stage(ui, 4)
    assert not any(item.label == 'Решение' for item in ui.selectbox)
    assert not any(item.label == 'Сохранить учебное решение' for item in ui.button)
    stage(ui, 5)
    assert not any(item.label == 'Экспортировать в локальный симулятор' for item in ui.button)
    assert widget(ui, 'button', 'Зарегистрировать в локальной учебной очереди')
    assert store.get_job(terminal['job_id'], principal) == terminal
    assert client.prepare_calls == 0
