"""Real Chromium/UI/API/SQLite with the explicit deterministic QA backend.

The Kafka panel uses a labelled backend fixture row; this suite is not evidence
of a broker delivery or of real model quality. Each case has a fresh browser
context and creates its own jobs in one serial, owned QA run.
"""
import hashlib
import json
import re
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from qa.e2e.pages import OperatorPage, assert_base_display, assert_base_report, frozen_scenario, set_checkbox


def ready(page, qa_api, browser_control, **settings):
    browser_control(**settings)
    ui = OperatorPage(page, qa_api).open()
    ui.prepare()
    return ui


def successful(page, qa_api, browser_control):
    ui = ready(page, qa_api, browser_control)
    job_id = ui.create()
    job = ui.terminal(job_id, 'Отчёт готов к проверке')
    assert job['status'] == 'succeeded'
    return ui, job


def test_preparation_is_explicit_and_becomes_ready(page, qa_api, browser_control, qa_profile):
    browser_control()
    ui = OperatorPage(page, qa_api).open()
    expect(page.get_by_text('QA-стенд · ' + qa_profile.run_id + ' · отдельные задания, решения и данные проверки.', exact=True)).to_be_visible()
    expect(page.get_by_text('Обработчик: Поиск не подготовлен', exact=True)).to_be_visible()
    assert qa_api('GET', '/v1/readiness')['ready'] is False
    ui.prepare()
    expect(page.get_by_role('button', name='Подготовить поиск', exact=True)).to_be_disabled()
    assert qa_api('GET', '/v1/readiness') == {'ready': True, 'code': 'READY', 'state': 'ready'}


def test_failed_preparation_explains_retry_and_blocks_job(page, qa_api, browser_control):
    browser_control(prepare_failure=True)
    ui = OperatorPage(page, qa_api).open()
    page.get_by_role('button', name='Подготовить поиск', exact=True).click()
    expect(page.get_by_text(re.compile(r'Подготовка не завершилась\. Проверьте сообщение'))).to_be_visible()
    expect(page.get_by_text('Обработчик: Поиск не подготовлен', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='Подготовить поиск', exact=True)).to_be_enabled()
    ui.stage(2)
    set_checkbox(page, 'Использовать учебную политику и синтетический снимок')
    expect(page.get_by_role('button', name='Создать задание', exact=True)).to_be_disabled()
    assert qa_api('GET', '/v1/readiness')['ready'] is False


def test_incident_format_and_synthetic_consent_gate_submission(page, qa_api, browser_control):
    ui = ready(page, qa_api, browser_control)
    before = {job['job_id'] for job in qa_api('GET', '/v1/jobs')}
    ui.stage(2)
    create = page.get_by_role('button', name='Создать задание', exact=True)
    expect(create).to_be_disabled()
    incident = page.get_by_role('textbox', name='Идентификатор инцидента', exact=True)
    incident.fill(frozen_scenario('QA-S08').case.incident_id)
    incident.press('Tab')
    set_checkbox(page, 'Использовать учебную политику и синтетический снимок')
    expect(page.get_by_text('Введите идентификатор в формате INC-000123: INC- и шесть цифр.', exact=True)).to_be_visible()
    expect(create).to_be_disabled()
    incident.fill('INC-000123')
    incident.press('Tab')
    expect(create).to_be_enabled()
    set_checkbox(page, 'Использовать учебную политику и синтетический снимок', checked=False)
    expect(create).to_be_disabled()
    assert {job['job_id'] for job in qa_api('GET', '/v1/jobs')} == before


def test_template_job_finishes_and_shows_exact_sources(page, qa_api, browser_control):
    ui, job = successful(page, qa_api, browser_control)
    assert_base_report(job['result'])
    assert_base_display(page)
    expect(page.get_by_role('heading', name='Факты и исходные значения', exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile(r'Уверенность не измерена\.'))).to_be_visible()
    transaction_number = next(index for index, fact in enumerate(job['result']['found_facts'], 1)
                              if any(item.get('tx_id') == 'TX-7700' for item in fact['evidence']))
    page.get_by_text(f'Сверить источники факта {transaction_number}', exact=True).click()
    page.get_by_text(f'Технические сведения факта {transaction_number}', exact=True).click()
    # Aggregate evidence also includes TX-7700. Scope to the opened technical
    # expander instead of matching hidden JSON trees from other facts.
    technical = page.get_by_test_id('stExpander').filter(
        has=page.get_by_text(f'Технические сведения факта {transaction_number}', exact=True)).last
    source_json = technical.get_by_test_id('stJson')
    expect(source_json).to_be_visible()
    for value in ('95000.00', 'RUB', 'completed', 'transactions.json', '/0/amount'):
        expect(source_json).to_contain_text(value)
    expect(page.get_by_text(re.compile(r'^R-115-002 · редакция '))).to_be_visible()
    page.get_by_text('Полная редакция правила 1', exact=True).click()
    expect(page.get_by_role('button', name='Перейти к решению', exact=True)).to_be_enabled()
    assert qa_api('GET', '/v1/jobs/' + job['job_id'])['result'] == job['result']


def test_refusal_exposes_reason_and_cannot_be_approved_or_exported(page, qa_api, browser_control):
    ui = ready(page, qa_api, browser_control)
    scenario = frozen_scenario('QA-S05')
    job_id = ui.create(scenario.case.incident_id)
    job = ui.terminal(job_id, 'Отчёт не сформирован')
    assert job['status'] == 'refused'
    assert job['result']['reason_code'] == scenario.case.expected_reason_code
    # A Streamlit rerun can briefly retain the previous reason caption. Read
    # the settled review for this incident, including its operator-facing reason.
    review = page.locator('[data-testid="stApp"][data-test-script-state="notRunning"]').get_by_test_id(
        'stMain').filter(has=page.get_by_role(
            'heading', name=job['incident_id'] + ' · Отчёт не сформирован', exact=True))
    expect(review.get_by_text(job['result']['reason'], exact=True)).to_be_visible()
    code = review.get_by_test_id('stCaptionContainer').get_by_text(
        'Код: ' + scenario.case.expected_reason_code, exact=True)
    expect(code).to_be_visible()
    ui.stage(4)
    expect(page.get_by_text('Решение недоступно: готового отчёта нет.', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='Сохранить учебное решение', exact=True)).to_have_count(0)
    ui.stage(5)
    expect(page.get_by_role('button', name='Экспортировать в локальный симулятор', exact=True)).to_have_count(0)
    expect(page.get_by_role('button', name='Зарегистрировать в локальной учебной очереди', exact=True)).to_be_enabled()


def test_running_job_can_be_cancelled_without_report(page, qa_api, browser_control):
    ui = ready(page, qa_api, browser_control, execution='hold')
    job_id = ui.create()
    expect(page.get_by_role('button', name='Отменить задание', exact=True)).to_be_enabled()
    page.get_by_role('button', name='Отменить задание', exact=True).click()
    job = ui.terminal(job_id, 'Задание отменено')
    assert job['status'] == 'cancelled'
    assert job.get('result') is None
    expect(page.get_by_role('button', name='Перейти к решению', exact=True)).to_have_count(0)
    expect(page.get_by_text('Обработчик: Обработчик занят заданием', exact=True)).to_have_count(0)


def test_deadline_ends_job_with_visible_timeout_and_no_export(page, qa_api, browser_control):
    ui = ready(page, qa_api, browser_control, execution='deadline')
    job_id = ui.create()
    job = ui.terminal(job_id, 'Время обработки истекло')
    assert job['status'] == 'timed_out'
    assert job.get('result') is None
    ui.stage(5)
    expect(page.get_by_role('button', name='Экспортировать в локальный симулятор', exact=True)).to_have_count(0)
    expect(page.get_by_text('Обработчик: Обработчик занят заданием', exact=True)).to_have_count(0)


def test_reload_reopens_the_same_durable_report_from_history(page, qa_api, browser_control):
    ui, job = successful(page, qa_api, browser_control)
    page.reload()
    ui.history(job['job_id'])
    restored = ui.terminal(job['job_id'], 'Отчёт готов к проверке')
    expect(page.get_by_text(re.compile('Отчёт: ' + re.escape(job['result']['report_id']) + ' · версия 1'))).to_be_visible()
    assert restored['result'] == job['result']
    assert restored['revision'] == job['revision']


def test_concurrent_decision_preserves_draft_until_revision_acknowledged(page, qa_api, browser_control):
    ui, job = successful(page, qa_api, browser_control)
    reason = 'Нужна дополнительная ручная сверка учебных исходных значений.'
    ui.decision_draft('Запросить изменения', reason)
    report = job['result']
    ref = qa_api('GET', f"/v1/reports/{report['report_id']}/reference?version={report['report_version']}")
    competing = qa_api('POST', f"/v1/reports/{report['report_id']}/decisions", expected=201,
        headers={'Idempotency-Key': 'qa-other-decision-' + uuid4().hex},
        json={'schema_version': '1.0', 'report': ref, 'expected_revision': 0,
              'action': 'reject', 'reason': 'Параллельное учебное решение другого окна.'})
    assert competing['revision'] == 1
    page.get_by_role('button', name='Обновить состояние', exact=True).click()
    expect(page.get_by_text(re.compile(r'Сохранённое решение изменилось\.'))).to_be_visible()
    expect(page.get_by_role('textbox', name='Обоснование решения', exact=True)).to_have_value(reason)
    expect(page.get_by_role('button', name='Сохранить учебное решение', exact=True)).to_be_disabled()
    page.get_by_role('button', name='Перечитать сохранённое решение', exact=True).click()
    expect(page.get_by_role('button', name='Сохранить учебное решение', exact=True)).to_be_enabled()
    page.get_by_role('button', name='Сохранить учебное решение', exact=True).click()
    expect(page.get_by_text('Сохранено: Запросить изменения · ревизия 2', exact=True)).to_be_visible()
    expect(page.get_by_text(reason, exact=True)).to_be_visible()
    assert qa_api('GET', '/v1/decisions/' + competing['decision_id']) == competing


def test_export_requires_approval_confirmation_and_download_matches_report(page, qa_api, browser_control, qa_artifacts):
    ui, job = successful(page, qa_api, browser_control)
    ui.stage(5)
    expect(page.get_by_text('Решение пока не сохранено. Для экспорта сначала проверьте отчёт и сохраните одобрение.', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='Экспортировать в локальный симулятор', exact=True)).to_have_count(0)
    expect(page.get_by_role('button', name='Скачать сохранённый учебный отчёт', exact=True)).to_have_count(0)
    ui.decision_draft('Одобрить учебный отчёт', 'Сверены синтетические факты, период, сумма и редакция учебного правила.')
    page.get_by_role('button', name='Сохранить учебное решение', exact=True).click()
    expect(page.get_by_text('Сохранено: Одобрить учебный отчёт · ревизия 1', exact=True)).to_be_visible()
    export = page.get_by_role('button', name='Экспортировать в локальный симулятор', exact=True)
    expect(export).to_be_disabled()
    set_checkbox(page, 'Понимаю: после принятия экспорта решение по этой версии изменить нельзя')
    expect(export).to_be_enabled()
    export.click()
    download_button = page.get_by_role('button', name='Скачать сохранённый учебный отчёт', exact=True)
    expect(download_button).to_be_visible()
    with page.expect_download() as pending:
        download_button.click()
    download = pending.value
    assert download.failure() is None
    assert re.fullmatch(r'DEL-[0-9a-f]{32}\.json', download.suggested_filename)
    path = qa_artifacts / download.suggested_filename
    download.save_as(path)
    raw = path.read_bytes()
    assert json.loads(raw) == job['result']
    report = job['result']
    ref = qa_api('GET', f"/v1/reports/{report['report_id']}/reference?version={report['report_version']}")
    assert hashlib.sha256(raw).hexdigest() == ref['content_sha256']
    ui.stage(4)
    expect(page.get_by_role('combobox', name='Решение', exact=True)).to_be_disabled()
    expect(page.get_by_role('textbox', name='Обоснование решения', exact=True)).to_be_disabled()
    expect(page.get_by_role('button', name='Сохранить учебное решение', exact=True)).to_be_disabled()


def test_kafka_fixture_panel_opens_its_linked_job(page, qa_api, browser_control):
    ui, job = successful(page, qa_api, browser_control)
    linked = qa_api('POST', '/qa/browser/kafka-link', json={'job_id': job['job_id']})
    page.reload()
    page.get_by_text('Запросы AML из Kafka', exact=True).click()
    expect(page.get_by_text('Событие: ' + linked['event_id'], exact=True)).to_be_visible()
    expect(page.get_by_text('Задание: ' + job['job_id'], exact=True)).to_be_visible()
    # Previous runs may retain other Kafka fixture links. Open this exact
    # event's card, never whichever button happens to appear first.
    card = page.get_by_test_id('stVerticalBlock').filter(
        has=page.get_by_text('Событие: ' + linked['event_id'], exact=True)).last
    card.get_by_role('button', name='Открыть задание из Kafka', exact=True).click()
    restored = ui.terminal(job['job_id'], 'Отчёт готов к проверке')
    assert restored['result'] == job['result']
    expect(page.get_by_role('button', name='Перейти к решению', exact=True)).to_be_enabled()


@pytest.mark.swagger_key
def test_swagger_authorization_executes_protected_readiness(page, qa_api, browser_control, qa_profile, qa_token):
    browser_control()
    page.goto(qa_profile.api_url + '/docs')
    page.get_by_role('button', name='Authorize', exact=True).click()
    modal = page.locator('.dialog-ux')
    expect(modal).to_be_visible()
    # Tracing/screenshots are disabled for this marked test: it handles an
    # ephemeral local QA key, which must never be retained in exported traces.
    try:
        modal.get_by_role('textbox').fill(qa_token)
        modal.get_by_role('button', name='Apply credentials', exact=True).click()
        modal.get_by_role('button', name='Close', exact=True).click()
        operation = page.locator('.opblock-get').filter(has=page.get_by_text('/v1/readiness', exact=True))
        operation.get_by_role('button', name=re.compile(r'^GET\b')).click()
        operation.get_by_role('button', name='Try it out', exact=True).click()
        with page.expect_response(lambda response: response.url == qa_profile.api_url + '/v1/readiness' and response.request.method == 'GET') as pending:
            operation.get_by_role('button', name='Execute', exact=True).click()
        response = pending.value
        assert response.status == 200
        assert response.json() == {'ready': False, 'code': 'NOT_READY', 'state': 'not_ready'}
        expect(operation.locator('.responses-inner .response-col_status').filter(has_text='200').first).to_be_visible()
    finally:
        # The isolated context is then destroyed by pytest-playwright. Visiting
        # a blank page also removes the key-bearing Swagger UI from the screen.
        page.goto('about:blank')

