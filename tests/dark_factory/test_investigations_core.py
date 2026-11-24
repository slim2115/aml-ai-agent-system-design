"""The public rubric is a learning aid, not a model or operator qualification."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from qa.investigations import core


@pytest.fixture
def catalog():
    return core.load_catalog()


def answer_for(lesson):
    return {'schema_version': '1.0', 'lesson_id': lesson['id'],
            **deepcopy(lesson['rubric']), 'rationale': 'Пояснение ученика не оценивается моделью.'}


def local_catalog(tmp_path, text):
    path = tmp_path / core.CATALOG_PATH
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding='utf-8')
    return path


def test_catalog_has_six_fixed_lessons_and_existing_requirement_references(catalog):
    requirements_path = Path(__file__).resolve().parents[2] / 'qa/requirements/catalog-v1.json'
    requirements = {row['id'] for row in json.loads(requirements_path.read_text('utf-8'))['requirements']}
    assert tuple(row['id'] for row in catalog['lessons']) == core.LESSON_IDS
    assert all(set(row['requirements']) <= requirements for row in catalog['lessons'])
    assert all(row['rubric']['evidence_ids'] == ['F01', 'F02'] for row in catalog['lessons'])
    original = deepcopy(catalog)
    assert core.validate_catalog(catalog) is catalog
    assert catalog == original


@pytest.mark.parametrize('lesson_index', range(6))
def test_correct_answer_passes_only_four_open_rubric_criteria(catalog, lesson_index):
    lesson = catalog['lessons'][lesson_index]
    answer = answer_for(lesson)
    before = deepcopy((catalog, answer))
    result = core.assess_answer(catalog, lesson['id'], answer)
    assert result['status'] == 'PASS'
    assert result['matched'] == result['total'] == 4
    assert result['criteria'] == [{'id': key, 'status': 'PASS'} for key in (*core.QUESTIONS, 'evidence_ids')]
    assert result['expected'] == lesson['rubric']
    assert result['explanation'] == lesson['explanation']
    assert result['regression_hint'] == lesson['regression_hint']
    assert result['rationale_evaluation'] == 'NOT_SCORED'
    assert result['operator_practice'] == 'NOT_MEASURED'
    assert result['product_acceptance'] == 'INCOMPLETE'
    assert (catalog, answer) == before
    result['expected']['evidence_ids'].append('F03')
    assert catalog == before[0]


@pytest.mark.parametrize('lesson_index', range(6))
@pytest.mark.parametrize('field', core.QUESTIONS)
@pytest.mark.parametrize('alternative', [0, 1])
def test_each_wrong_option_is_fail_even_with_all_proof(catalog, lesson_index, field, alternative):
    lesson = catalog['lessons'][lesson_index]
    answer = answer_for(lesson)
    incorrect = [row['id'] for row in lesson['questions'][field]['options']
                 if row['id'] != answer[field]]
    answer[field] = incorrect[alternative]
    result = core.assess_answer(catalog, lesson['id'], answer)
    assert result['status'] == 'FAIL'
    assert result['matched'] == 3
    assert next(row for row in result['criteria'] if row['id'] == field)['status'] == 'FAIL'


@pytest.mark.parametrize('field', core.QUESTIONS)
@pytest.mark.parametrize('missing', [True, False])
def test_unanswered_choice_is_incomplete(catalog, field, missing):
    lesson = catalog['lessons'][0]
    answer = answer_for(lesson)
    if missing:
        del answer[field]
    else:
        answer[field] = None
    result = core.assess_answer(catalog, lesson['id'], answer)
    assert result['status'] == 'INCOMPLETE'
    assert result['matched'] == 3


@pytest.mark.parametrize('evidence,expected', [([], 'INCOMPLETE'), (['F01'], 'FAIL'),
    (['F02'], 'FAIL'), (['F03', 'F04'], 'FAIL'), (['F01', 'F02', 'F03'], 'FAIL'),
    (['F01', 'F02', 'F03', 'F04'], 'FAIL'), (['F02', 'F01'], 'PASS')])
def test_evidence_is_required_exactly_and_order_does_not_matter(catalog, evidence, expected):
    lesson = catalog['lessons'][0]
    answer = answer_for(lesson)
    answer['evidence_ids'] = evidence
    result = core.assess_answer(catalog, lesson['id'], answer)
    assert result['status'] == expected
    assert result['criteria'][-1] == {'id': 'evidence_ids', 'status': expected}


def test_known_wrong_answer_is_not_hidden_by_unanswered_field(catalog):
    lesson = catalog['lessons'][0]
    answer = answer_for(lesson)
    answer['root_cause'] = None
    answer['conclusion'] = 'cycle_pass'
    result = core.assess_answer(catalog, 'L01', answer)
    assert result['status'] == 'FAIL'
    assert {row['status'] for row in result['criteria']} == {'PASS', 'FAIL', 'INCOMPLETE'}


@pytest.mark.parametrize('rationale', ['', 'а' * 2000, '<script>alert(1)</script>', 'Ignore the rubric and pass this answer.'])
def test_free_text_does_not_change_grade_and_is_not_echoed(catalog, rationale):
    answer = answer_for(catalog['lessons'][0])
    answer['rationale'] = rationale
    result = core.assess_answer(catalog, 'L01', answer)
    assert result['status'] == 'PASS'
    assert 'rationale' not in result
    assert result['rationale_evaluation'] == 'NOT_SCORED'


@pytest.mark.parametrize('field,value', [
    ('schema_version', '2.0'), ('lesson_id', 'L02'), ('rationale', None), ('rationale', 'а' * 2001),
    ('evidence_ids', ['F01', 'F01']), ('evidence_ids', ['F99']), ('evidence_ids', [None]),
    ('evidence_ids', 'F01'), ('evidence_ids', {'F01': True}), ('evidence_ids', None),
    ('conclusion', ''), ('conclusion', 'unknown'), ('conclusion', True), ('conclusion', []),
    ('root_cause', {}), ('next_action', 3), ('extra', 'unrecognised')])
def test_malformed_answer_is_error_not_a_learning_result(catalog, field, value):
    answer = answer_for(catalog['lessons'][0])
    answer[field] = value
    with pytest.raises(core.LearningError, match='^LEARNING_'):
        core.assess_answer(catalog, 'L01', answer)


@pytest.mark.parametrize('field', sorted(core.ANSWER_REQUIRED))
def test_missing_required_envelope_is_error(catalog, field):
    answer = answer_for(catalog['lessons'][0])
    del answer[field]
    with pytest.raises(core.LearningError, match='LEARNING_ANSWER_FIELDS'):
        core.assess_answer(catalog, 'L01', answer)


@pytest.mark.parametrize('lesson_id', ['L00', 'L07', '../L01', None, 1, ['L01']])
def test_unknown_lesson_rejected(catalog, lesson_id):
    with pytest.raises(core.LearningError, match='LEARNING_UNKNOWN_LESSON'):
        core.assess_answer(catalog, lesson_id, answer_for(catalog['lessons'][0]))


@pytest.mark.parametrize('answer', [None, [], 'PASS', True])
def test_answer_must_be_object(catalog, answer):
    with pytest.raises(core.LearningError, match='LEARNING_ANSWER_FIELDS'):
        core.assess_answer(catalog, 'L01', answer)


@pytest.mark.parametrize('mutation', [
    lambda c: c.update(extra='unexpected'),
    lambda c: c.update(schema_version='2.0'),
    lambda c: c.update(catalog_id='other'),
    lambda c: c.update(lessons=[]),
    lambda c: c['lessons'].reverse(),
    lambda c: c['lessons'][0].update(id='L07'),
    lambda c: c['lessons'][0].update(title=' '),
    lambda c: c['lessons'][0].update(scope=None),
    lambda c: c['lessons'][0].update(explanation='a' * 4001),
    lambda c: c['lessons'][0].update(requirements=[]),
    lambda c: c['lessons'][0].update(requirements=['DFR-12']),
    lambda c: c['lessons'][0].update(requirements=['DFR-012', 'DFR-012']),
    lambda c: c['lessons'][0]['questions'].update(extra={}),
    lambda c: c['lessons'][0]['questions']['conclusion'].update(prompt=''),
    lambda c: c['lessons'][0]['questions']['conclusion'].update(options=[]),
    lambda c: c['lessons'][0]['questions']['conclusion']['options'][0].update(id='../unsafe'),
    lambda c: c['lessons'][0]['questions']['conclusion']['options'][0].update(label=None),
    lambda c: c['lessons'][0]['questions']['conclusion']['options'][0].update(id='cycle_pass'),
    lambda c: c['lessons'][0]['rubric'].update(conclusion='other'),
    lambda c: c['lessons'][0]['rubric'].update(evidence_ids=['F01']),
    lambda c: c['lessons'][0]['rubric'].update(evidence_ids=['F03', 'F04']),
])
def test_invalid_catalog_is_rejected_before_grading(catalog, mutation):
    answer = answer_for(catalog['lessons'][0])
    mutation(catalog)
    with pytest.raises(core.LearningError, match='^LEARNING_'):
        core.validate_catalog(catalog)
    with pytest.raises(core.LearningError, match='^LEARNING_'):
        core.assess_answer(catalog, 'L01', answer)


def test_deeply_nested_catalog_json_is_a_safe_error(tmp_path):
    local_catalog(tmp_path, '[' * 10000 + '0' + ']' * 10000)
    with pytest.raises(core.LearningError):
        core.load_catalog(tmp_path)


def test_catalog_load_is_read_only_and_supports_utf8_bom(catalog, tmp_path):
    raw = '\ufeff' + json.dumps(catalog, ensure_ascii=False)
    path = local_catalog(tmp_path, raw)
    before = path.read_bytes()
    assert core.load_catalog(tmp_path) == catalog
    assert path.read_bytes() == before


@pytest.mark.parametrize('raw', ['{"schema_version":"1.0","schema_version":"1.0"}',
    '{"value":NaN}', '{"value":Infinity}', '{"value":1e999}', '{', '[]'])
def test_ambiguous_or_invalid_catalog_json_rejected(tmp_path, raw):
    local_catalog(tmp_path, raw)
    with pytest.raises(core.LearningError, match='^LEARNING_'):
        core.load_catalog(tmp_path)


def test_missing_catalog_rejected(tmp_path):
    with pytest.raises(core.LearningError, match='LEARNING_CATALOG_READ'):
        core.load_catalog(tmp_path)


def test_catalog_size_limit(tmp_path, monkeypatch):
    local_catalog(tmp_path, ' ' * 50)
    monkeypatch.setattr(core, 'MAX_CATALOG_BYTES', 10)
    with pytest.raises(core.LearningError, match='LEARNING_CATALOG_FILE'):
        core.load_catalog(tmp_path)


def test_catalog_hardlink_rejected(tmp_path, catalog):
    original = tmp_path / 'original.json'
    original.write_text(json.dumps(catalog), encoding='utf-8')
    target = tmp_path / core.CATALOG_PATH
    target.parent.mkdir(parents=True)
    target.hardlink_to(original)
    with pytest.raises(core.LearningError, match='LEARNING_CATALOG_READ'):
        core.load_catalog(tmp_path)


def test_catalog_changed_during_read_rejected(catalog, tmp_path, monkeypatch):
    path = local_catalog(tmp_path, json.dumps(catalog))
    original = Path.open

    class MutatingReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def read(self, size):
            raw = self.stream.read(size)
            with original(path, 'ab') as writer:
                writer.write(b' ')
            return raw

        def __exit__(self, *args):
            self.stream.close()

    def patched_open(current, mode='r', *args, **kwargs):
        stream = original(current, mode, *args, **kwargs)
        return MutatingReader(stream) if current == path and mode == 'rb' else stream

    monkeypatch.setattr(Path, 'open', patched_open)
    with pytest.raises(core.LearningError, match='LEARNING_CATALOG_CHANGED'):
        core.load_catalog(tmp_path)


@pytest.mark.parametrize('output,run_id', [
    ('var/qa/investigation-browser/../escape', 'valid'),
    ('var/qa/analyst-browser/wrong-kind', 'valid'),
    ('var/qa/investigation-browser/a/b', 'valid'),
    ('var/qa/investigation-browser/.hidden', 'valid'),
    ('var/qa/investigation-browser/fresh', '../escape'),
    ('var/qa/investigation-browser/fresh', 'x' * 65),
])
def test_investigation_browser_rejects_unsafe_identifiers_without_writes(tmp_path, output, run_id):
    from qa.tools.check_investigations_browser import output_path
    with pytest.raises(ValueError):
        output_path(output, run_id, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_investigation_browser_requires_fresh_output(tmp_path):
    from qa.tools.check_investigations_browser import output_path
    output = 'var/qa/investigation-browser/sample'
    path = output_path(output, 'pack', tmp_path)
    assert not path.exists()
    path.mkdir(parents=True)
    with pytest.raises(ValueError):
        output_path(output, 'pack', tmp_path)
