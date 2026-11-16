"""Editable practice cases, selected explicitly; frozen operator cases stay intact.

Uses the same owned deterministic browser stand, fixtures and evidence capture.
These checks do not call real models, Kafka or PostgreSQL.
"""
from playwright.sync_api import expect

from qa.e2e.pages import OperatorPage


def test_incident_field_defaults_and_keeps_edited_value(page, qa_api, browser_control):
    browser_control()
    ui = OperatorPage(page, qa_api).open()
    ui.stage(2)
    incident = page.get_by_role('textbox', name='Идентификатор инцидента', exact=True)
    expect(incident).to_have_value('INC-000123')
    incident.fill('INC-000250')
    incident.press('Tab')
    expect(incident).to_have_value('INC-000250')
    ui.stage(1)
    ui.stage(2)
    expect(incident).to_have_value('INC-000250')


def test_decision_requires_nonblank_reason(page, qa_api, browser_control):
    browser_control()
    ui = OperatorPage(page, qa_api).open()
    ui.prepare()
    job_id = ui.create()
    job = ui.terminal(job_id, 'Отчёт готов к проверке')
    assert job['status'] == 'succeeded'
    ui.stage(4)
    reason = page.get_by_role('textbox', name='Обоснование решения', exact=True)
    save = page.get_by_role('button', name='Сохранить учебное решение', exact=True)
    expect(reason).to_have_value('')
    expect(save).to_be_disabled()
    ui.select('Решение', 'Запросить изменения')
    expect(save).to_be_disabled()
    reason.fill('   ')
    reason.press('Tab')
    expect(save).to_be_disabled()
    text = 'Учебная проверка: требуется уточнить источник факта.'
    reason.fill(text)
    reason.press('Tab')
    expect(reason).to_have_value(text)
    expect(save).to_be_enabled()
