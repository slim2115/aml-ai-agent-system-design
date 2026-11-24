"""UI exercise state and assessment boundaries without a browser service."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from qa.investigations import storage
from tests.dark_factory.test_investigations_build import built, project

APP = Path(__file__).resolve().parents[2] / 'qa/analyst/app.py'


@pytest.fixture
def page(built, monkeypatch):
    original_list, original_load = storage.list_packs, storage.load_pack
    monkeypatch.setattr(storage, 'list_packs', lambda: original_list(built))
    monkeypatch.setattr(storage, 'load_pack', lambda run_id: original_load(run_id, built))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.radio[0].set_value('Учебные расследования').run()
    assert not app.exception
    return app


def texts(page):
    return '\n'.join(item.value for item in page.text)


def submit(page):
    next(item for item in page.button if item.label == 'Проверить разбор').click().run()
    assert not page.exception


def select_correct(page, built):
    lesson = storage.load_pack('sample')['catalog']['lessons'][0]
    for name in ('conclusion', 'root_cause', 'next_action'):
        next(item for item in page.selectbox if item.label == lesson['questions'][name]['prompt']).set_value(lesson['rubric'][name])
    page.multiselect[0].set_value(['F01', 'F02'])
    return lesson


def test_incomplete_then_correct_answer_and_free_rationale_not_scored(page, built):
    submit(page)
    assert 'Результат разбора: INCOMPLETE' in texts(page)
    select_correct(page, built)
    page.text_area[0].set_value('Любое самостоятельное обоснование')
    submit(page)
    assert 'Результат разбора: PASS' in texts(page)
    assert any('NOT_SCORED' in item.value for item in page.caption)


def test_wrong_answer_is_fail_and_switch_does_not_reuse_feedback(page, built):
    lesson = select_correct(page, built)
    wrong = next(item['id'] for item in lesson['questions']['conclusion']['options']
                 if item['id'] != lesson['rubric']['conclusion'])
    next(item for item in page.selectbox if item.label == lesson['questions']['conclusion']['prompt']).set_value(wrong)
    submit(page)
    assert 'Результат разбора: FAIL' in texts(page)
    next(item for item in page.selectbox if item.label == 'Расследование').set_value('L02').run()
    assert not page.exception and 'Результат разбора:' not in texts(page)
    assert page.multiselect[0].value == []


def test_changed_source_cannot_be_assessed_by_old_form(page, built):
    select_correct(page, built)
    (built / 'src/service.py').write_text('changed', encoding='utf-8')
    submit(page)
    assert 'Результат разбора: PASS' not in texts(page)
    assert any('Исходники изменились' in item.value for item in page.warning)
    assert not any(item.label == 'Проверить разбор' for item in page.button)


def test_tampered_pack_has_no_assessment_controls(page, built):
    (storage.pack_path('sample', built) / 'bundle.json').write_text('{}', encoding='utf-8')
    page.run()
    assert not page.exception and page.error
    assert not any(item.label == 'Проверить разбор' for item in page.button)
