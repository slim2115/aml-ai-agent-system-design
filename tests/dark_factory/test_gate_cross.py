"""Independent controls for aggregation; no historical artifacts needed."""
from copy import deepcopy

import pytest

from qa.dark_factory.gate_cross import cross


def case(case_id='C1', gate='FAIL', mode='single'):
    candidate = lambda label: {'label': label, 'report': {},
        'fragments': [{'ref_id': f'candidate/{label}', 'text': 'report'}]}
    value = {'id': case_id, 'split': 'open', 'family': 'one',
             'expected_kind': 'synthetic_rule', 'expected_verdict': 'pass',
             'expected_preference': 'not_applicable',
             'programmatic_gate': {'status': gate, 'issues': []},
             'payload': {'case_id': case_id, 'criterion': 'grounding', 'mode': mode,
                         'sources': [{'ref_id': 'source', 'text': 'fact'}],
                         'candidates': [candidate('A')]}}
    if mode == 'pair':
        value.update({'pair_id': 'pair', 'candidate_mapping': {'A': 'first', 'B': 'second'},
                      'expected_preference': 'A'})
        value['payload']['candidates'].append(candidate('B'))
    return value


def observation(c, verdict='pass', status='completed'):
    response = {'schema_version': '1.0', 'rubric_version': '1.0', 'case_id': c['id'],
                'criterion': 'grounding', 'verdict': verdict,
                'references': [{'ref_id': 'source', 'quote': 'fact'}], 'rationale': 'test',
                'preference': ('A' if c['payload']['mode'] == 'pair' else 'not_applicable')}
    if verdict == 'abstain' and c['payload']['mode'] == 'pair':
        response['preference'] = 'abstain'
    return {'case_id': c['id'], 'status': status, 'response': response,
            'programmatic_gate': deepcopy(c['programmatic_gate'])}


def test_failing_gate_with_pass_is_counted_without_overriding_gate():
    c = case()
    result = cross([c], [observation(c)])
    assert result['gate_judge_counts']['FAIL']['pass'] == 1
    assert result['gate_fail_judge_pass_case_ids'] == ['C1']
    assert result['gate_fail_judge_pass_rate'] == {'numerator': 1, 'denominator': 1, 'rate': 1.0}


@pytest.mark.parametrize('status', ['error', 'timeout', 'invalid_response'])
def test_noncompleted_never_counts_as_approval(status):
    c = case()
    result = cross([c], [observation(c, status=status)])
    assert result['gate_judge_counts']['FAIL'][status] == 1
    assert not result['gate_fail_judge_pass_case_ids']


def test_invalid_quote_is_invalid_response_not_pass():
    c = case()
    obs = observation(c)
    obs['response']['references'][0]['quote'] = 'invented'
    result = cross([c], [obs])
    assert result['gate_judge_counts']['FAIL']['invalid_response'] == 1
    assert not result['gate_fail_judge_pass_case_ids']


def test_missing_and_abstain_remain_in_denominator():
    a, b = case('C1'), case('C2')
    result = cross([a, b], [observation(a, verdict='abstain')])
    assert result['gate_judge_counts']['FAIL']['abstain'] == 1
    assert result['gate_judge_counts']['FAIL']['missing'] == 1
    assert result['gate_fail_judge_pass_rate']['denominator'] == 2


def test_pair_is_not_counted_as_report_approval():
    c = case('P1', mode='pair')
    result = cross([c], [observation(c)])
    assert result['planned_single_cases'] == 0
    assert result['planned_pair_observations'] == 1
    assert result['pair_response_counts']['pass'] == 1
    assert result['gate_fail_judge_pass_rate']['rate'] is None


def test_gate_disagreement_and_duplicate_are_rejected():
    c = case()
    obs = observation(c)
    obs['programmatic_gate']['status'] = 'PASS'
    with pytest.raises(ValueError, match='GATE_RECORD_MISMATCH'):
        cross([c], [obs])
    obs = observation(c)
    with pytest.raises(ValueError):
        cross([c], [obs, obs])


def test_incomplete_gate_never_contributes_to_failed_gate_rate():
    c = case(gate='INCOMPLETE')
    result = cross([c], [observation(c)])
    assert result['gate_judge_counts']['INCOMPLETE']['pass'] == 1
    assert result['gate_fail_judge_pass_rate']['denominator'] == 0
