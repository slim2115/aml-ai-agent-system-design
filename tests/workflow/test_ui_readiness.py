"""UI preparation regressions; no model, SQLite data or network is needed.

AppTest executes the real page and its immediate fragment call. Browser timer
scheduling and actual Windows model startup are separate integration checks.
"""
from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


APP = Path(__file__).resolve().parents[2] / 'src/ui/app.py'
PREPARE = 'Подготовить поиск'
SUBMIT = 'Создать задание'
DEMO = 'Использовать учебную политику и синтетический снимок'
STAGES = ('1. Подготовка', '2. Инцидент', '3. Проверка отчёта',
          '4. Решение', '5. Сохранение и экспорт')


def widget(ui, kind, label):
    matches = [item for item in ui.get(kind) if item.label == label]
    assert len(matches) == 1, (kind, label, len(matches))
    return matches[0]


def stage(ui, number):
    widget(ui, 'radio', 'Этап работы').set_value(STAGES[number - 1]).run()
    assert not ui.exception


def incident_inputs(ui):
    stage(ui, 2)
    widget(ui, 'text_input', 'Идентификатор инцидента').set_value('INC-000222').run()
    widget(ui, 'checkbox', DEMO).set_value(True).run()
    widget(ui, 'selectbox', 'Формирование').set_value('local-model').run()


def assert_incident_inputs(ui):
    stage(ui, 2)
    assert widget(ui, 'text_input', 'Идентификатор инцидента').value == 'INC-000222'
    assert widget(ui, 'checkbox', DEMO).value is True
    assert widget(ui, 'selectbox', 'Формирование').value == 'local-model'
    assert ui.session_state['incident'] == 'INC-000222'
    assert ui.session_state['demo'] is True
    assert ui.session_state['engine'] == 'local-model'


class ReadinessService:
    """Deterministic preparation boundary, including instant success/failure."""

    def __init__(self):
        self.state = {'ready': False, 'code': 'NOT_READY', 'state': 'not_ready'}
        self.outcome = 'READY'
        self.prepare_calls = 0
        self.controller_constructions = 0
        self.store = SimpleNamespace(list_jobs=lambda principal: [])

    def readiness(self):
        return dict(self.state)

    def prepare(self):
        self.prepare_calls += 1
        if self.outcome == 'READY':
            self.state = {'ready': True, 'code': 'READY', 'state': 'ready'}
        elif self.outcome == 'PREPARING':
            self.state = {'ready': False, 'code': 'NOT_READY', 'state': 'preparing'}
        else:
            self.state = {'ready': False, 'code': self.outcome, 'state': 'not_ready'}
        return self.readiness()

    def close(self):
        pass


@pytest.fixture
def page(monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    import src.workflow.controller as controller
    import src.workflow.delivery as delivery

    service = ReadinessService()
    st.cache_resource.clear()
    def construct():
        service.controller_constructions += 1
        return service
    monkeypatch.setattr(controller, 'WorkflowService', construct)
    # No job is opened by these tests; delivery storage is outside this UI
    # boundary and must not create tables or access the operator database.
    monkeypatch.setattr(delivery, 'DeliveryService', lambda store: None)
    ui = AppTest.from_file(str(APP), default_timeout=10).run()
    assert not ui.exception
    yield ui, service
    st.cache_resource.clear()


def test_spawn_main_import_neither_renders_nor_acquires_controller(monkeypatch):
    """Windows spawn must reach its worker target without executing the page."""
    import streamlit as st
    import src.workflow.controller as controller

    constructor = Mock(side_effect=AssertionError('SPAWN_MUST_NOT_OPEN_CONTROLLER'))
    render = Mock(side_effect=AssertionError('SPAWN_MUST_NOT_RENDER_PAGE'))
    monkeypatch.setattr(controller, 'WorkflowService', constructor)
    for name in ('set_page_config', 'title', 'warning', 'caption', 'text', 'button'):
        monkeypatch.setattr(st, name, render)

    runpy.run_path(str(APP), run_name='__mp_main__')

    constructor.assert_not_called()
    render.assert_not_called()


def test_initial_not_ready_is_not_reported_as_failed_preparation(page):
    ui, service = page
    assert not ui.error
    assert service.prepare_calls == 0
    assert not widget(ui, 'button', PREPARE).disabled
    stage(ui, 2)
    assert widget(ui, 'button', SUBMIT).disabled


@pytest.mark.parametrize('code', ['NOT_READY', 'DEADLINE_EXCEEDED'])
def test_preparation_failure_displays_safe_code_and_allows_retry(page, code):
    ui, service = page
    service.outcome = code
    widget(ui, 'button', PREPARE).click().run()
    # A completed background failure is observed on the next state read. Timer
    # ticks themselves are deliberately outside AppTest's test boundary.
    ui.run()

    assert not ui.exception
    assert service.prepare_calls == 1
    assert any(code in item.value for item in ui.error)
    assert not widget(ui, 'button', PREPARE).disabled
    assert not any('Traceback' in item.value for item in ui.error)
    stage(ui, 2)
    assert widget(ui, 'button', SUBMIT).disabled


def test_successful_preparation_reruns_after_inputs_and_preserves_consent(page):
    ui, service = page
    incident_inputs(ui)
    stage(ui, 1)

    # There is intentionally no manual refresh or additional ui.run() here.
    # The page's end-of-render watcher must detect readiness and rerun safely.
    widget(ui, 'button', PREPARE).click().run()

    assert not ui.exception
    assert service.prepare_calls == 1
    assert service.controller_constructions == 1
    assert widget(ui, 'button', PREPARE).disabled
    assert_incident_inputs(ui)
    assert not widget(ui, 'button', SUBMIT).disabled
    assert not ui.error


@pytest.mark.parametrize('active_state', ['preparing', 'busy'])
def test_watch_detects_completed_transition_and_preserves_inputs(page, active_state):
    ui, service = page
    incident_inputs(ui)
    reads = 0

    def readiness_transition():
        nonlocal reads
        reads += 1
        if reads == 1:
            return {'ready': False, 'code': 'BUSY' if active_state == 'busy' else 'NOT_READY',
                    'state': active_state}
        return {'ready': True, 'code': 'READY', 'state': 'ready'}

    service.readiness = readiness_transition
    # A single page run observes active work; its fragment sees completion and
    # triggers the follow-up run. An early rerun would discard these widgets.
    ui.run()

    assert not ui.exception
    assert reads >= 3  # Render, watcher transition, then refreshed render.
    assert_incident_inputs(ui)
    assert not widget(ui, 'button', SUBMIT).disabled
    assert service.controller_constructions == 1


def test_preparing_disables_duplicate_preparation_without_losing_consent(page):
    ui, service = page
    service.outcome = 'PREPARING'
    stage(ui, 2)
    widget(ui, 'checkbox', DEMO).set_value(True).run()
    stage(ui, 1)
    widget(ui, 'button', PREPARE).click().run()

    assert not ui.exception
    assert service.prepare_calls == 1
    assert widget(ui, 'button', PREPARE).disabled
    assert ui.session_state['demo'] is True
    assert not ui.error
    stage(ui, 2)
    assert widget(ui, 'button', SUBMIT).disabled
    assert widget(ui, 'checkbox', DEMO).value is True


@pytest.mark.parametrize('code', ['NOT_READY', 'DEADLINE_EXCEEDED'])
def test_retry_after_failure_can_reach_ready_and_clears_failure(page, code):
    ui, service = page
    service.outcome = code
    stage(ui, 2)
    widget(ui, 'checkbox', DEMO).set_value(True).run()
    stage(ui, 1)
    widget(ui, 'button', PREPARE).click().run()
    ui.run()
    assert any(code in item.value for item in ui.error)

    service.outcome = 'READY'
    widget(ui, 'button', PREPARE).click().run()

    assert not ui.exception
    assert service.prepare_calls == 2
    assert not ui.error
    assert ui.session_state['demo'] is True
    stage(ui, 2)
    assert not widget(ui, 'button', SUBMIT).disabled


@pytest.mark.parametrize('confirmed_state', ['ready', 'busy'])
def test_later_worker_loss_is_not_mislabelled_preparation_failure(page, confirmed_state):
    ui, service = page
    service.outcome = 'PREPARING'
    widget(ui, 'button', PREPARE).click().run()
    service.state = {'ready': confirmed_state == 'ready',
                     'code': 'READY' if confirmed_state == 'ready' else 'BUSY',
                     'state': confirmed_state}
    ui.run()
    service.state = {'ready': False, 'code': 'NOT_READY', 'state': 'not_ready'}
    ui.run()
    assert not ui.exception
    assert not ui.error
    assert not widget(ui, 'button', PREPARE).disabled
