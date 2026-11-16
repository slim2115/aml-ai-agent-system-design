"""Exercise controls and explicit failure paths; no real model is called."""
from copy import deepcopy

import pytest

from qa.dark_editions.probe import assess, cases, observe, run


@pytest.mark.parametrize('case', cases(), ids=lambda c: c['id'])
def test_real_index_matches_manually_fixed_expectations(case):
    assert assess(case, observe(case))['status'] == 'PASS'


def test_missing_and_expired_are_distinct_and_do_not_assert_refusal():
    result = run()
    missing, expired = result['cases'][:2]
    assert missing['observed']['citations'] == [['SYN-OTHER', 'v1']]
    assert expired['observed']['citations'] == []
    assert result['downstream_selection'] == 'NOT_OBSERVED'
    assert result['embedding'] == 'FAKE_EQUAL_VECTORS'


def test_conflict_is_reported_and_is_not_masked_as_empty_search():
    case = cases()[2]
    assert observe(case) == {'kind': 'ERROR', 'code': 'AMBIGUOUS_RULE'}
    assert assess(case, {'kind': 'RESULT', 'active_count': 0, 'citations': [],
                         'target_present': False})['status'] == 'FAIL'


def test_missing_result_and_embedding_failure_are_incomplete():
    case = cases()[0]
    assert assess(case, None)['status'] == 'INCOMPLETE'
    assert assess(case, {'kind': 'ERROR', 'code': 'EMBEDDING_NOT_READY'})['status'] == 'INCOMPLETE'


@pytest.mark.parametrize('change', ['wrong_edition', 'expired_included', 'wrong_count', 'wrong_target'])
def test_tampering_cannot_pass_assessment(change):
    case = cases()[3]
    observation = deepcopy(case['expected'])
    if change == 'wrong_edition':
        observation['citations'][0][1] = 'old'
    elif change == 'expired_included':
        observation['citations'].append(['SYN-TARGET', 'old'])
    elif change == 'wrong_count':
        observation['active_count'] = 2
    else:
        observation['target_present'] = False
    assert assess(case, observation)['status'] == 'FAIL'


def test_fresh_scenarios_cannot_mutate_one_another():
    a, b = cases(), cases()
    a[0]['records'][0]['version'] = 'changed'
    assert b[0]['records'][0]['version'] == 'v1'
