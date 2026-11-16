from copy import deepcopy

import pytest

from qa.dark_compare.comparison import ComparisonError, compare

H = lambda c: c * 64


def inputs():
    spec = {'catalog_sha256': H('a'), 'cases': [
        {'case_id': 'C1', 'family': 'F1', 'case_sha256': H('b')},
        {'case_id': 'C2', 'family': 'F1', 'case_sha256': H('c')}]}
    def run(identifier):
        return {'run_id': identifier, 'catalog_sha256': H('a'), 'code_sha256': H('d'),
                'model_sha256': H('e'), 'prompt_sha256': H('f'), 'attempts': [
                    {**case, 'attempt': 1, 'status': 'PASS'} for case in spec['cases']]}
    return spec, run('baseline'), run('candidate')


def test_failure_remains_visible_when_second_attempt_passes():
    spec, old, new = inputs()
    new['attempts'][0]['status'] = 'FAIL'
    new['attempts'].append({**spec['cases'][0], 'attempt': 2, 'status': 'PASS'})
    report = compare(spec, old, new)
    assert report['comparison_status'] == 'MEASURED'
    assert report['transitions']['regression'] == 1
    assert report['candidate_attempts'] == 3 and report['planned_cases'] == 2
    assert report['distinct_families_descriptive'] == 1 and report['independent_cases'] is None
    assert report['human_acceptance'] == 'INCOMPLETE'


def test_only_retry_without_first_attempt_is_incomplete():
    spec, old, new = inputs()
    new['attempts'][0]['attempt'] = 2
    report = compare(spec, old, new)
    assert report['comparison_status'] == 'INCOMPLETE'
    assert report['incomplete_pairs'] == 1
    assert report['cases'][0]['candidate_first'] is None


@pytest.mark.parametrize('field,value,error', [
    ('catalog_sha256', H('1'), 'CATALOG_MISMATCH'),
    ('run_id', 'baseline', 'RUN_IDS_MUST_DIFFER'),
    ('model_sha256', 'secret material', 'INVALID_RUN'),
])
def test_run_identity_is_strict(field, value, error):
    spec, old, new = inputs()
    new[field] = value
    with pytest.raises(ComparisonError, match=error):
        compare(spec, old, new)


@pytest.mark.parametrize('field,value', [('family', 'F2'), ('case_sha256', H('9')), ('case_id', 'other')])
def test_case_identity_mismatch_is_rejected(field, value):
    spec, old, new = inputs()
    new['attempts'][0][field] = value
    with pytest.raises(ComparisonError, match='CASE_IDENTITY_MISMATCH'):
        compare(spec, old, new)


def test_duplicate_attempt_and_extra_payload_rejected():
    spec, old, new = inputs()
    new['attempts'].append(deepcopy(new['attempts'][0]))
    with pytest.raises(ComparisonError, match='DUPLICATE_ATTEMPT'):
        compare(spec, old, new)
    new['attempts'].pop()
    new['attempts'][0]['raw_prompt'] = 'must not be copied into report'
    with pytest.raises(ComparisonError, match='INVALID_ATTEMPT'):
        compare(spec, old, new)


def test_non_scalar_status_is_rejected_explicitly():
    spec, old, new = inputs()
    new['attempts'][0]['status'] = ['PASS']
    with pytest.raises(ComparisonError, match='INVALID_ATTEMPT'):
        compare(spec, old, new)


def test_candidate_may_change_model_or_prompt_but_not_dataset():
    spec, old, new = inputs()
    new['model_sha256'] = H('1')
    new['prompt_sha256'] = H('2')
    result = compare(spec, old, new)
    assert result['comparison_status'] == 'MEASURED'
    assert result['baseline_metadata']['model_sha256'] != result['candidate_metadata']['model_sha256']
