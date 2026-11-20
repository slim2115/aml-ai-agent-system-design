"""Streamlit reruns with real isolated artifact files, no workflow or model."""
from pathlib import Path
import json

import pytest
from streamlit.testing.v1 import AppTest

from qa.analyst import reader
from tests.dark_factory.test_analyst_reader import make_run, save
from tests.dark_factory.test_execution_provenance import project, write

APP = Path(__file__).resolve().parents[2] / 'qa/analyst/app.py'


@pytest.fixture
def page(project, monkeypatch):
    original_list, original_load = reader.list_runs, reader.load_run
    monkeypatch.setattr(reader, 'list_runs', lambda kind: original_list(kind, project))
    monkeypatch.setattr(reader, 'load_run', lambda kind, run_id, **kw: original_load(kind, run_id, root=project, **kw))
    return AppTest.from_file(str(APP), default_timeout=20)


def texts(page):
    return '\n'.join(item.value for item in page.text)


def button(page, label):
    return next(item for item in page.button if item.label == label)


def test_empty_state_without_services(page):
    page.run()
    assert not page.exception
    assert 'Сохранённых запусков' in page.warning[0].value
    assert [item.label for item in page.button] == ['Обновить список']


def test_pending_profiles_remain_visible_and_no_release_actions(page, project):
    make_run(project)
    page.run()
    assert not page.exception
    assert 'Записанный итог: INCOMPLETE' in texts(page)
    assert 'Невыполненные профили: llm' in texts(page)
    assert set(item.label for item in page.button) == {'Обновить список', 'Проверить файлы и текущий код'}
    assert 'INCOMPLETE' in page.info[0].value


def test_explicit_check_then_rerun_does_not_reuse_stale_green(page, project):
    make_run(project)
    page.run()
    button(page, 'Проверить файлы и текущий код').click().run()
    assert not page.exception and page.success
    assert 'исходниками: MATCH' in texts(page)
    write(project, 'src/service.py', 'changed')
    button(page, 'Обновить список').click().run()
    assert not page.success and 'исходниками: NOT_CHECKED' in texts(page)
    button(page, 'Проверить файлы и текущий код').click().run()
    assert not page.exception and not page.success
    assert any('Код изменился' in row.value for row in page.warning)


def test_refresh_reloads_result_and_preserves_failure(page, project):
    directory = make_run(project)
    page.run()
    result = json.loads((directory / 'result.json').read_text('utf-8'))
    result['status'] = 'FAIL'
    save(directory / 'result.json', result)
    button(page, 'Обновить список').click().run()
    assert not page.exception and 'Записанный итог: FAIL' in texts(page)


def test_invalid_and_unfinished_runs_have_distinct_messages(page, project):
    directory = make_run(project)
    (directory / 'result.json').write_text('{', encoding='utf-8')
    page.run()
    assert not page.exception and page.error
    (directory / 'result.json').unlink()
    button(page, 'Обновить список').click().run()
    assert not page.exception and not page.error
    assert any('result.json' in row.value and 'процесс' in row.value for row in page.warning)


def test_external_html_is_plain_text_and_not_an_action(page, project):
    make_run(project, result={'error_code': '<script>alert(1)</script>'})
    page.run()
    assert not page.exception
    assert '<script>alert(1)</script>' in texts(page)
    assert not any('<script>' in str(row.value) for row in page.markdown)


def test_type_switch_drops_previous_report(page, project):
    make_run(project)
    page.run()
    page.selectbox[0].set_value('autonomy').run()
    assert not page.exception
    assert 'Записанный итог:' not in texts(page)
    assert any('Сохранённых запусков' in row.value for row in page.warning)
