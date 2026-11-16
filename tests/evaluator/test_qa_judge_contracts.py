"""Adversarial contract checks without a model or network."""
from copy import deepcopy
import json

import pytest

from qa.judge.contracts import response_schema, validate_payload, validate_response


def payload(mode='single'):
    candidates = [{'label': 'A', 'report': {'amount': '12.30'},
                   'fragments': [{'ref_id': 'A:amount', 'text': 'Amount: 12.30 USD'}]}]
    if mode == 'pair':
        candidates.append({'label': 'B', 'report': {'amount': '120.30'},
                           'fragments': [{'ref_id': 'B:amount', 'text': 'Amount: 120.30 USD'}]})
    return {'case_id': 'blind-001', 'criterion': 'grounding', 'mode': mode,
            'sources': [{'ref_id': 'source:amount', 'text': 'The amount is 12.30 USD.'}],
            'candidates': candidates}


def reply(**updates):
    value = {'schema_version': '1.0', 'rubric_version': '1.0', 'case_id': 'blind-001',
             'criterion': 'grounding', 'verdict': 'pass',
             'references': [{'ref_id': 'source:amount', 'quote': '12.30 USD'}],
             'rationale': 'The amount matches the cited source.', 'preference': 'not_applicable'}
    value.update(updates)
    return value


def test_valid_single_quote_is_structurally_accepted_not_a_semantic_verdict():
    # The checker deliberately cannot infer whether this rationale is correct.
    value = reply(rationale='The report contains no amount.')
    assert validate_response(json.dumps(value), payload()) == value
    assert response_schema()['additionalProperties'] is False


@pytest.mark.parametrize('field,value', [
    ('schema_version', 1), ('rubric_version', 1.0), ('case_id', 1),
    ('criterion', 'clarity'), ('case_id', 'different'), ('verdict', True),
    ('rationale', 5), ('rationale', ' '), ('rationale', 'a' * 501),
    ('references', {}), ('references', []), ('preference', 'A'),
])
def test_no_coercion_wrong_context_or_missing_required_evidence(field, value):
    with pytest.raises(ValueError, match='QA_JUDGE_'):
        validate_response(json.dumps(reply(**{field: value})), payload())


@pytest.mark.parametrize('reference', [
    {'ref_id': 'unknown', 'quote': '12.30 USD'},
    {'ref_id': 'source:amount', 'quote': '120.30 USD'},
    {'ref_id': 'source:amount', 'quote': ''},
    {'ref_id': 'source:amount', 'quote': ' '},
    {'ref_id': 'source:amount', 'quote': 12.30},
    {'ref_id': 'source:amount', 'quote': '12.30 USD', 'extra': True},
])
def test_nonexistent_invented_blank_or_untyped_quotes_rejected(reference):
    with pytest.raises(ValueError, match='QA_JUDGE_'):
        validate_response(json.dumps(reply(references=[reference])), payload())


def test_duplicate_response_refs_and_extra_fields_rejected():
    reference = reply()['references'][0]
    for value in (reply(references=[reference, reference]), reply(confidence=1.0)):
        with pytest.raises(ValueError, match='QA_JUDGE_'):
            validate_response(json.dumps(value), payload())


@pytest.mark.parametrize('text', [
    '{"schema_version":"1.0","schema_version":"1.0"}',
    '{"value":NaN}', '```json\n{}\n```', '[]', 'null', '{} {}',
])
def test_only_one_strict_json_object_is_accepted(text):
    with pytest.raises(ValueError, match='QA_JUDGE_'):
        validate_response(text, payload())


def test_abstention_can_have_no_refs_but_cannot_have_fabricated_refs():
    value = reply(verdict='abstain', references=[])
    assert validate_response(json.dumps(value), payload())['verdict'] == 'abstain'
    value['references'] = [{'ref_id': 'invented', 'quote': 'unknown'}]
    with pytest.raises(ValueError, match='REFERENCE_ID'):
        validate_response(json.dumps(value), payload())


@pytest.mark.parametrize('verdict,preference,valid', [
    ('pass', 'A', True), ('pass', 'B', True), ('pass', 'tie', True),
    ('abstain', 'abstain', True), ('fail', 'A', False), ('pass', 'abstain', False),
    ('abstain', 'A', False), ('pass', 'not_applicable', False),
])
def test_pairwise_outcomes_are_comparisons_not_single_candidate_failure(verdict, preference, valid):
    text = json.dumps(reply(verdict=verdict, preference=preference))
    if valid:
        assert validate_response(text, payload('pair'))['preference'] == preference
    else:
        with pytest.raises(ValueError, match='PAIR_PREFERENCE'):
            validate_response(text, payload('pair'))


@pytest.mark.parametrize('mutation', ['oracle', 'duplicate_ref', 'duplicate_label', 'empty_fragment',
                                     'untyped_report', 'no_sources', 'wrong_mode'])
def test_blinded_payload_requires_exact_shape_and_unique_reference_namespace(mutation):
    value = deepcopy(payload('pair'))
    if mutation == 'oracle':
        value['oracle'] = {'verdict': 'pass'}
    elif mutation == 'duplicate_ref':
        value['candidates'][0]['fragments'][0]['ref_id'] = 'source:amount'
    elif mutation == 'duplicate_label':
        value['candidates'][1]['label'] = 'A'
    elif mutation == 'empty_fragment':
        value['candidates'][1]['fragments'] = []
    elif mutation == 'untyped_report':
        value['candidates'][0]['report'] = '{}'
    elif mutation == 'no_sources':
        value['sources'] = []
    else:
        value['mode'] = 'single'
    with pytest.raises(ValueError, match='QA_JUDGE_'):
        validate_payload(value)
