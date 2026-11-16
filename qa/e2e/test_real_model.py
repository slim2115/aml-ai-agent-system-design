"""Opt-in browser smoke for an already started owned QA stand and real models.

Not selected by the deterministic profile. Requires the installed retrieval
model and a manually running local Ollama with the configured generation model.
One successful smoke is not a quality benchmark or statistical model evidence.
"""
import pytest
from playwright.sync_api import expect

from qa.e2e.pages import OperatorPage, assert_base_display, assert_base_report


@pytest.mark.real_model
def test_real_local_model_job_keeps_all_facts_and_human_review_gate(page, qa_api):
    ui = OperatorPage(page, qa_api).open()
    state = qa_api('GET', '/v1/readiness')
    assert state['state'] in ('ready', 'not_ready'), 'The explicitly selected real-model stand must be idle.'
    if not state['ready']:
        ui.prepare(timeout=210_000)
    job_id = ui.create(engine='local-model')
    job = ui.terminal(job_id, 'Отчёт готов к проверке', timeout=210_000)
    assert job['status'] == 'succeeded'
    assert_base_report(job['result'])
    assert_base_display(page)
    expect(page.get_by_role('heading', name='Факты и исходные значения', exact=True)).to_be_visible()
    ui.stage(5)
    expect(page.get_by_text('Решение пока не сохранено. Для экспорта сначала проверьте отчёт и сохраните одобрение.', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='Экспортировать в локальный симулятор', exact=True)).to_have_count(0)
