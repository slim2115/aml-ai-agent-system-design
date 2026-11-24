"""Open-rubric learning checks; no model calls or independent skill assessment."""
from copy import deepcopy
from pathlib import Path
import re
import stat

from qa.reporting import strict_json
from qa.requirements.catalog import ROOT
from src.qa_profile import checked_path

CATALOG_PATH = 'qa/investigations/catalog-v1.json'
LESSON_IDS = tuple(f'L{number:02d}' for number in range(1, 7))
FACT_IDS = ('F01', 'F02', 'F03', 'F04')
QUESTIONS = ('conclusion', 'root_cause', 'next_action')
CATALOG_KEYS = {'schema_version', 'catalog_id', 'lessons'}
LESSON_KEYS = {'id', 'title', 'claim', 'requirements', 'scope', 'questions', 'rubric',
               'explanation', 'regression_hint'}
ANSWER_REQUIRED = {'schema_version', 'lesson_id', 'evidence_ids', 'rationale'}
ANSWER_KEYS = ANSWER_REQUIRED | set(QUESTIONS)
MAX_CATALOG_BYTES = 1024 * 1024


class LearningError(ValueError):
    """A fixed diagnostic code for malformed learning inputs."""


def _require(condition, code):
    if not condition:
        raise LearningError(code)


def _shape(value, keys, code):
    _require(type(value) is dict and set(value) == keys, code)


def _text(value, limit=4000):
    return type(value) is str and bool(value.strip()) and len(value) <= limit


def _identifier(value):
    return type(value) is str and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', value) is not None


def validate_catalog(value):
    """Validate exact versioned shapes and references, returning the unchanged value."""
    _shape(value, CATALOG_KEYS, 'LEARNING_CATALOG_FIELDS')
    _require(value['schema_version'] == '1.0' and value['catalog_id'] == 'investigations-v1',
             'LEARNING_CATALOG_VERSION')
    lessons = value['lessons']
    _require(type(lessons) is list and len(lessons) == len(LESSON_IDS), 'LEARNING_LESSONS')
    for index, lesson in enumerate(lessons):
        _shape(lesson, LESSON_KEYS, 'LEARNING_LESSON_FIELDS')
        _require(lesson['id'] == LESSON_IDS[index], 'LEARNING_LESSON_ID')
        _require(all(_text(lesson[key]) for key in
                     ('title', 'claim', 'scope', 'explanation', 'regression_hint')),
                 'LEARNING_LESSON_TEXT')
        requirements = lesson['requirements']
        _require(type(requirements) is list and 0 < len(requirements) <= 12 and
                 all(type(item) is str and re.fullmatch(r'DFR-[0-9]{3}', item)
                     for item in requirements), 'LEARNING_REQUIREMENTS')
        _require(len(requirements) == len(set(requirements)), 'LEARNING_REQUIREMENTS')
        _shape(lesson['questions'], set(QUESTIONS), 'LEARNING_QUESTIONS')
        _shape(lesson['rubric'], set(QUESTIONS) | {'evidence_ids'}, 'LEARNING_RUBRIC')
        for question_id in QUESTIONS:
            question = lesson['questions'][question_id]
            _shape(question, {'prompt', 'options'}, 'LEARNING_QUESTION_FIELDS')
            _require(_text(question['prompt']), 'LEARNING_QUESTION_PROMPT')
            options = question['options']
            _require(type(options) is list and len(options) == 3, 'LEARNING_OPTIONS')
            for option in options:
                _shape(option, {'id', 'label'}, 'LEARNING_OPTION_FIELDS')
                _require(_identifier(option['id']) and _text(option['label']), 'LEARNING_OPTION')
            ids = [option['id'] for option in options]
            _require(len(set(ids)) == 3, 'LEARNING_OPTION_DUPLICATE')
            expected = lesson['rubric'][question_id]
            _require(type(expected) is str and expected in ids, 'LEARNING_RUBRIC_CHOICE')
        _require(lesson['rubric']['evidence_ids'] == ['F01', 'F02'], 'LEARNING_RUBRIC_EVIDENCE')
    return value


def load_catalog(root=ROOT):
    """Read only the owned, bounded regular catalog and reject ambiguous JSON."""
    try:
        root = Path(root).absolute()
        path = checked_path(root / CATALOG_PATH, root)
        before = path.stat()
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                 and before.st_size <= MAX_CATALOG_BYTES, 'LEARNING_CATALOG_FILE')
        with path.open('rb') as stream:
            raw = stream.read(MAX_CATALOG_BYTES + 1)
        after = path.stat()
        identity = lambda record: (record.st_dev, record.st_ino, record.st_size,
                                   record.st_mtime_ns, record.st_ctime_ns, record.st_nlink)
        _require(identity(before) == identity(after) and len(raw) == before.st_size,
                 'LEARNING_CATALOG_CHANGED')
        _require(len(raw) <= MAX_CATALOG_BYTES, 'LEARNING_CATALOG_FILE')
        return validate_catalog(strict_json(raw.decode('utf-8-sig')))
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, LearningError):
            raise
        raise LearningError('LEARNING_CATALOG_READ') from None


def assess_answer(catalog, lesson_id, answer):
    """Compare four explicit selections with the public rubric, never free prose."""
    validate_catalog(catalog)
    _require(type(lesson_id) is str and lesson_id in LESSON_IDS, 'LEARNING_UNKNOWN_LESSON')
    lesson = catalog['lessons'][LESSON_IDS.index(lesson_id)]
    _require(type(answer) is dict and ANSWER_REQUIRED <= set(answer) <= ANSWER_KEYS,
             'LEARNING_ANSWER_FIELDS')
    _require(answer['schema_version'] == '1.0' and answer['lesson_id'] == lesson_id,
             'LEARNING_ANSWER_BINDING')
    _require(type(answer['rationale']) is str and len(answer['rationale']) <= 2000,
             'LEARNING_RATIONALE')
    evidence = answer['evidence_ids']
    _require(type(evidence) is list and len(evidence) <= len(FACT_IDS) and
             all(type(item) is str and item in FACT_IDS for item in evidence),
             'LEARNING_EVIDENCE_IDS')
    _require(len(set(evidence)) == len(evidence), 'LEARNING_EVIDENCE_IDS')
    criteria = []
    for question_id in QUESTIONS:
        selected = answer.get(question_id)
        options = {option['id'] for option in lesson['questions'][question_id]['options']}
        _require(selected is None or (type(selected) is str and selected in options),
                 'LEARNING_UNKNOWN_CHOICE')
        status = ('INCOMPLETE' if selected is None else
                  'PASS' if selected == lesson['rubric'][question_id] else 'FAIL')
        criteria.append({'id': question_id, 'status': status})
    evidence_status = ('INCOMPLETE' if not evidence else
                       'PASS' if set(evidence) == set(lesson['rubric']['evidence_ids']) else 'FAIL')
    criteria.append({'id': 'evidence_ids', 'status': evidence_status})
    states = {row['status'] for row in criteria}
    status = 'FAIL' if 'FAIL' in states else 'INCOMPLETE' if 'INCOMPLETE' in states else 'PASS'
    return {'status': status, 'criteria': criteria,
            'matched': sum(row['status'] == 'PASS' for row in criteria), 'total': len(criteria),
            'expected': deepcopy(lesson['rubric']), 'explanation': lesson['explanation'],
            'regression_hint': lesson['regression_hint'], 'rationale_evaluation': 'NOT_SCORED',
            'operator_practice': 'NOT_MEASURED', 'product_acceptance': 'INCOMPLETE'}
