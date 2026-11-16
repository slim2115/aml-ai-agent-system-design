"""Small operator actions shared by the deterministic and opt-in model profiles.

These helpers drive the real Streamlit page. The API is used only to identify
the job created by the browser and inspect its stored result, never to replace
an operator action that the scenario is meant to exercise.
"""
from functools import lru_cache
import re

from playwright.sync_api import expect

from qa.scenarios import load_catalog


STAGES = (
    '1. Подготовка', '2. Инцидент', '3. Проверка отчёта',
    '4. Решение', '5. Сохранение и экспорт',
)


def set_checkbox(page, label, *, checked=True):
    control = page.get_by_role('checkbox', name=label, exact=True)
    if control.is_checked() != checked:
        page.get_by_text(label, exact=True).click()
    if checked:
        expect(control).to_be_checked()
    else:
        expect(control).not_to_be_checked()


class OperatorPage:
    def __init__(self, page, qa_api):
        self.page, self.api = page, qa_api

    def open(self):
        self.page.goto('/')
        expect(self.page.get_by_role('heading', name='AML — пошаговая проверка', exact=True)).to_be_visible()
        return self

    def stage(self, number):
        label = STAGES[number - 1]
        # Streamlit hides the native input under its custom visual radio.
        # Use the same visible label an operator clicks, without force.
        self.page.get_by_role('radiogroup').get_by_text(label, exact=True).click()
        expect(self.page.get_by_role('radio', name=label, exact=True)).to_be_checked()

    def prepare(self, timeout=20_000):
        self.stage(1)
        self.page.get_by_role('button', name='Подготовить поиск', exact=True).click()
        expect(self.page.get_by_text('Обработчик: Можно создать задание', exact=True)).to_be_visible(timeout=timeout)
        expect(self.page.get_by_text('Поиск готов. Перейдите к выбору инцидента.', exact=True)).to_be_visible()

    def incident(self, incident_id='INC-000123', *, engine='template'):
        self.stage(2)
        field = self.page.get_by_role('textbox', name='Идентификатор инцидента', exact=True)
        field.fill(incident_id)
        field.press('Tab')  # Streamlit commits text on blur, not each keystroke.
        set_checkbox(self.page, 'Использовать учебную политику и синтетический снимок')
        if engine == 'local-model':
            self.page.get_by_text('Способ формирования', exact=True).click()
            self.select('Формирование', 'Локальная модель: порядок фактов')
        expect(self.page.get_by_role('button', name='Создать задание', exact=True)).to_be_enabled()

    def create(self, incident_id='INC-000123', *, engine='template'):
        self.incident(incident_id, engine=engine)
        previous = {job['job_id'] for job in self.api('GET', '/v1/jobs')}
        self.page.get_by_role('button', name='Создать задание', exact=True).click()
        expect(self.page.get_by_role('heading', name=re.compile('^' + re.escape(incident_id) + ' · '))).to_be_visible()
        created = [job for job in self.api('GET', '/v1/jobs') if job['job_id'] not in previous]
        assert len(created) == 1, 'The browser action must create exactly one owned QA job.'
        assert created[0]['incident_id'] == incident_id
        return created[0]['job_id']

    def terminal(self, job_id, label, *, timeout=20_000):
        job = self.api('GET', '/v1/jobs/' + job_id)
        expect(self.page.get_by_role('heading', name=job['incident_id'] + ' · ' + label, exact=True)).to_be_visible(timeout=timeout)
        return self.api('GET', '/v1/jobs/' + job_id)

    def select(self, label, option):
        self.page.get_by_role('combobox', name=label, exact=True).click()
        self.page.get_by_role('option', name=option, exact=True).click()

    def decision_draft(self, action, reason):
        self.stage(4)
        self.select('Решение', action)
        field = self.page.get_by_role('textbox', name='Обоснование решения', exact=True)
        field.fill(reason)
        field.press('Tab')
        expect(self.page.get_by_role('button', name='Сохранить учебное решение', exact=True)).to_be_enabled()

    def history(self, job_id):
        self.page.get_by_role('combobox', name='Сохранённые задания', exact=True).click()
        self.page.get_by_role('option', name=re.compile(re.escape(job_id) + '$')).click()
        self.page.get_by_role('button', name='Открыть сохранённое задание', exact=True).click()


def assert_base_report(report):
    """Independent QA-S01 oracle, shared values but not its renamed snapshot ID.

    Loading the sealed catalogue verifies its source manifest. The browser
    profile uses its own copy of the original synthetic seed, so claiming a
    complete frozen-case replay (including snapshot identity) would be wrong.
    """
    scenario = frozen_scenario('QA-S01')
    golden = scenario.golden
    assert report['status'] == 'draft'
    assert report['incident_id'] == scenario.case.incident_id
    assert len(report['found_facts']) == golden.fact_count
    evidence = [item for fact in report['found_facts'] for item in fact['evidence']]
    assert {item['tx_id'] for item in evidence if item['kind'] == 'transaction'} == set(golden.transaction_ids)
    assert [(item['currency'], item['value']) for item in evidence if item['kind'] == 'sum'] == [
        (item.currency, item.value) for item in golden.sums]
    assert [item['value'] for item in evidence if item['kind'] == 'count'] == [len(golden.transaction_ids)]
    assert report['applicable_rules'] == [rule.model_dump(mode='json') for rule in golden.rules]


@lru_cache(maxsize=1)
def _frozen_catalog():
    return load_catalog()


def frozen_scenario(case_id):
    return next(scenario for scenario in _frozen_catalog().cases if scenario.case.id == case_id)


def assert_base_display(page):
    """Assert what the operator reads, independently of typed API evidence.

    In particular, corrupt_total changes the rendered sentence while retaining
    correct typed evidence. Checking the latter alone cannot catch that defect.
    """
    scenario = frozen_scenario('QA-S01')
    total = scenario.golden.sums[0]
    window = scenario.case.context.window
    window_label = f'[{window.start_inclusive}, {window.end_exclusive})'
    expected_sum = (f'Сумма всех транзакций в контексте в валюте {total.currency} за окно {window_label}, '
                    f'включая все статусы: {total.value} {total.currency}.')
    expected_count = (f'Количество всех транзакций в контексте за окно {window_label}, включая все статусы: '
                      f'{len(scenario.golden.transaction_ids)}.')
    expect(page.get_by_text(expected_sum, exact=True)).to_be_visible()
    expect(page.get_by_text(expected_count, exact=True)).to_be_visible()

