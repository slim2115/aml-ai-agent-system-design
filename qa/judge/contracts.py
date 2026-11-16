"""Strict syntax/reference checks; these do not establish semantic correctness."""
from __future__ import annotations

from src.runtime.transport import strict_json

RUBRIC_VERSION = '1.0'
CRITERIA = ('grounding', 'completeness', 'ordering', 'clarity', 'relevance')
VERDICTS = ('pass', 'fail', 'abstain')
PREFERENCES = ('not_applicable', 'A', 'B', 'tie', 'abstain')


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError('QA_JUDGE_' + code)


def _keys(value, expected: set[str], code: str) -> None:
    _require(type(value) is dict and set(value) == expected, code)


def _text(value) -> bool:
    return type(value) is str and bool(value.strip())


def validate_payload(payload: dict) -> dict:
    """Validate blind task shape and globally unique quote-reference identifiers."""
    _keys(payload, {'case_id', 'criterion', 'mode', 'sources', 'candidates'}, 'PAYLOAD_FIELDS')
    _require(_text(payload['case_id']), 'CASE_ID')
    _require(type(payload['criterion']) is str and payload['criterion'] in CRITERIA, 'CRITERION')
    _require(type(payload['mode']) is str and payload['mode'] in ('single', 'pair'), 'MODE')
    _require(type(payload['sources']) is list and bool(payload['sources']), 'SOURCES')
    _require(type(payload['candidates']) is list, 'CANDIDATES')
    expected = {'A'} if payload['mode'] == 'single' else {'A', 'B'}
    _require(len(payload['candidates']) == len(expected), 'CANDIDATES')
    labels, ids = set(), set()

    def fragment(item):
        _keys(item, {'ref_id', 'text'}, 'FRAGMENT_FIELDS')
        _require(_text(item['ref_id']) and _text(item['text']), 'FRAGMENT_TEXT')
        _require(item['ref_id'] not in ids, 'DUPLICATE_REF_ID')
        ids.add(item['ref_id'])

    for item in payload['sources']:
        fragment(item)
    for candidate in payload['candidates']:
        _keys(candidate, {'label', 'report', 'fragments'}, 'CANDIDATE_FIELDS')
        _require(type(candidate['label']) is str and candidate['label'] in expected
                 and candidate['label'] not in labels, 'CANDIDATE_LABEL')
        labels.add(candidate['label'])
        _require(type(candidate['report']) is dict, 'CANDIDATE_REPORT')
        _require(type(candidate['fragments']) is list and bool(candidate['fragments']), 'FRAGMENTS')
        for item in candidate['fragments']:
            fragment(item)
    return payload


def response_schema() -> dict:
    """JSON schema supplied to Ollama; contextual rules are checked separately."""
    properties = {
        'schema_version': {'type': 'string', 'const': '1.0'},
        'rubric_version': {'type': 'string', 'const': RUBRIC_VERSION},
        'case_id': {'type': 'string', 'minLength': 1},
        'criterion': {'type': 'string', 'enum': list(CRITERIA)},
        'verdict': {'type': 'string', 'enum': list(VERDICTS)},
        'references': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['ref_id', 'quote'], 'properties': {
                'ref_id': {'type': 'string', 'minLength': 1},
                'quote': {'type': 'string', 'minLength': 1},
            }}},
        'rationale': {'type': 'string', 'minLength': 1, 'maxLength': 500},
        'preference': {'type': 'string', 'enum': list(PREFERENCES)},
    }
    return {'type': 'object', 'additionalProperties': False,
            'required': list(properties), 'properties': properties}


def validate_response(text: str, payload: dict) -> dict:
    """Accept only strict JSON and exact quotations from this blinded payload.

    A real quotation can still support an incorrect judgment: semantic accuracy
    must be measured independently against the withheld calibration oracle.
    """
    validate_payload(payload)
    _require(type(text) is str, 'RESPONSE_TEXT')
    try:
        value = strict_json(text)
    except (ValueError, TypeError, RecursionError):
        raise ValueError('QA_JUDGE_RESPONSE_JSON') from None
    _keys(value, set(response_schema()['required']), 'RESPONSE_FIELDS')
    _require(type(value['schema_version']) is str and value['schema_version'] == '1.0', 'SCHEMA_VERSION')
    _require(type(value['rubric_version']) is str and value['rubric_version'] == RUBRIC_VERSION, 'RUBRIC_VERSION')
    _require(type(value['case_id']) is str and value['case_id'] == payload['case_id'], 'CASE_ID')
    _require(type(value['criterion']) is str and value['criterion'] == payload['criterion'], 'CRITERION')
    _require(type(value['verdict']) is str and value['verdict'] in VERDICTS, 'VERDICT')
    _require(type(value['preference']) is str and value['preference'] in PREFERENCES, 'PREFERENCE')
    _require(_text(value['rationale']) and len(value['rationale']) <= 500, 'RATIONALE')
    _require(type(value['references']) is list, 'REFERENCES')
    fragments = payload['sources'] + [item for candidate in payload['candidates']
                                     for item in candidate['fragments']]
    lookup = {item['ref_id']: item['text'] for item in fragments}
    seen = set()
    for reference in value['references']:
        _keys(reference, {'ref_id', 'quote'}, 'REFERENCE_FIELDS')
        ref_id, quote = reference['ref_id'], reference['quote']
        _require(_text(ref_id) and ref_id in lookup and ref_id not in seen, 'REFERENCE_ID')
        _require(_text(quote) and quote in lookup[ref_id], 'REFERENCE_QUOTE')
        seen.add(ref_id)
    _require(value['verdict'] == 'abstain' or bool(seen), 'REFERENCE_REQUIRED')
    if payload['mode'] == 'single':
        _require(value['preference'] == 'not_applicable', 'SINGLE_PREFERENCE')
    else:
        _require((value['verdict'] == 'pass' and value['preference'] in ('A', 'B', 'tie'))
                 or (value['verdict'] == 'abstain' and value['preference'] == 'abstain'),
                 'PAIR_PREFERENCE')
    return value
