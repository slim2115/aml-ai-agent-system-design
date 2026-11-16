"""Model reports cannot turn incomplete or contradictory measurements green."""
import json

import pytest

from qa import reporting


def report_for(root, kind, result):
    name = 'verification/model-gates/result.json'
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result), encoding='utf-8')
    index = {'schema_version': '1.0', 'scope': 'historical', 'profiles': [
        {'id': 'model', 'kind': kind, 'files': {'result': reporting.pin(root, name)}}]}
    return reporting.build_report(index, root=root)


def llm_result(control_count=8):
    # Same field contract as run_llm / the step-07 pilot, without loading it.
    return {'status': 'PASS', 'model_identity_stable': True,
            'full_planned_matrix': True, 'collection_complete': True,
            'source_files_unchanged_during_run': True, 'template_failures': [],
            'repeats_per_draft_case': 3, 'draft_observation_count': 15,
            'control_observation_count': control_count,
            'controls': [{'case_id': f'QA-S{i + 5:02d}', 'status': 'PASS', 'issues': []}
                         for i in range(control_count)],
            'summary': {'status': 'PASS', 'planned_repeat_count': 15,
                        'observed_repeat_count': 15, 'missing_repeat_count': 0,
                        'fact_violation_count': 0, 'observed_independence_family_count': 3}}


def judge_result():
    return {'status': 'PASS', 'model_identity_stable': True,
            'full_planned_matrix': True, 'source_files_unchanged_during_run': True,
            'summary': {'execution_status': 'PASS', 'known_challenge_status': 'PASS',
                        'human_calibration': {'status': 'EVIDENCE_COMPLETE',
                                              'supplied_reviews': 20,
                                              'paired_valid_observations': 20},
                        'overall': {'planned': 20, 'valid_responses': 20,
                                    'missing': 0, 'error': 0, 'timeout': 0,
                                    'invalid_response': 0,
                                    'all_planned_accuracy': {'numerator': 20,
                                                             'denominator': 20, 'rate': 1.0}}}}


@pytest.mark.parametrize('count', [1, 8])
def test_llm_complete_controls_do_not_require_a_hardcoded_catalog_size(tmp_path, count):
    report = report_for(tmp_path, 'llm', llm_result(count))
    assert report['status'] == 'PASS'
    assert report['profiles'][0]['metrics']['control_observation_count'] == count
    assert report['product_acceptance'] == 'INCOMPLETE'


@pytest.mark.parametrize('field,value,expected', [
    ('model_identity_stable', False, 'FAIL'),
    ('model_identity_stable', None, 'INCOMPLETE'),
    ('model_identity_stable', 'true', 'INCOMPLETE'),
    ('full_planned_matrix', False, 'INCOMPLETE'),
    ('full_planned_matrix', None, 'INCOMPLETE'),
    ('collection_complete', False, 'INCOMPLETE'),
    ('collection_complete', None, 'INCOMPLETE'),
    ('template_failures', [{'case_id': 'QA-S01', 'issues': ['FACT_MISMATCH']}], 'FAIL'),
    ('template_failures', None, 'INCOMPLETE'),
    ('controls', [], 'INCOMPLETE'),
    ('control_observation_count', 7, 'INCOMPLETE'),
    ('control_observation_count', True, 'INCOMPLETE'),
    ('control_observation_count', '8', 'INCOMPLETE'),
    ('draft_observation_count', 14, 'INCOMPLETE'),
    ('repeats_per_draft_case', 2, 'INCOMPLETE'),
    ('repeats_per_draft_case', True, 'INCOMPLETE'),
])
def test_llm_claimed_pass_cannot_hide_failed_or_missing_gates(tmp_path, field, value, expected):
    result = llm_result()
    result[field] = value
    report = report_for(tmp_path, 'llm', result)
    assert report['status'] == expected
    assert 'claimed_pass_not_supported' in report['profiles'][0]['issues']


@pytest.mark.parametrize('field', [
    'model_identity_stable', 'full_planned_matrix', 'collection_complete',
    'template_failures', 'controls', 'control_observation_count',
    'draft_observation_count', 'repeats_per_draft_case',
])
def test_llm_missing_gate_is_incomplete(tmp_path, field):
    result = llm_result()
    del result[field]
    assert report_for(tmp_path, 'llm', result)['status'] == 'INCOMPLETE'


@pytest.mark.parametrize('status,expected', [
    ('FAIL', 'FAIL'), ('ERROR', 'ERROR'), ('INCOMPLETE', 'INCOMPLETE'),
    ('unexpected', 'INCOMPLETE'), (None, 'INCOMPLETE'),
])
def test_llm_control_result_is_not_hidden_by_passing_drafts(tmp_path, status, expected):
    result = llm_result()
    result['controls'][0]['status'] = status
    assert report_for(tmp_path, 'llm', result)['status'] == expected


def test_llm_control_issue_cannot_be_hidden_by_its_own_claimed_pass(tmp_path):
    result = llm_result()
    result['controls'][0]['issues'] = ['REFUSAL_REASON_MISMATCH']
    assert report_for(tmp_path, 'llm', result)['status'] == 'FAIL'


def test_judge_complete_exercise_keeps_suitability_unestablished(tmp_path):
    report = report_for(tmp_path, 'judge', judge_result())
    assert report['status'] == 'PASS'
    assert report['profiles'][0]['metrics']['suitability'] == 'NOT_ESTABLISHED'
    assert report['product_acceptance'] == 'INCOMPLETE'


@pytest.mark.parametrize('value,expected', [
    (None, 'INCOMPLETE'), (False, 'FAIL'), ('true', 'ERROR'), (1, 'ERROR'),
])
def test_judge_requires_a_strictly_true_model_identity(tmp_path, value, expected):
    result = judge_result()
    result['model_identity_stable'] = value
    assert report_for(tmp_path, 'judge', result)['status'] == expected


@pytest.mark.parametrize('value', [None, True, 'no independent ratings', '20', 20.0, -1, {}])
def test_judge_human_count_is_a_nonnegative_integer_and_never_public_prose(tmp_path, value):
    result = judge_result()
    result['summary']['human_calibration']['supplied_reviews'] = value
    report = report_for(tmp_path, 'judge', result)
    assert report['status'] == 'ERROR'
    assert 'invalid_human_review_count' in report['profiles'][0]['issues']
    assert 'no independent ratings' not in json.dumps(report)


@pytest.mark.parametrize('status,count', [('INCOMPLETE', 1), ('INCOMPLETE', 0), ('EVIDENCE_COMPLETE', 1),
                                         (None, 20), ('PASS', 20)])
def test_judge_human_count_alone_does_not_establish_complete_calibration(tmp_path, status, count):
    result = judge_result()
    result['summary']['human_calibration'].update(status=status, supplied_reviews=count,
                                                 paired_valid_observations=count)
    assert report_for(tmp_path, 'judge', result)['status'] == 'INCOMPLETE'


def test_judge_dropped_human_status_is_incomplete(tmp_path):
    result = judge_result()
    del result['summary']['human_calibration']['status']
    assert report_for(tmp_path, 'judge', result)['status'] == 'INCOMPLETE'


@pytest.mark.parametrize('paired', [None, True, 1])
def test_judge_complete_label_cannot_hide_unpaired_ratings(tmp_path, paired):
    result = judge_result()
    result['summary']['human_calibration']['paired_valid_observations'] = paired
    assert report_for(tmp_path, 'judge', result)['status'] == 'INCOMPLETE'


@pytest.mark.parametrize('field', ['model_identity_stable', 'full_planned_matrix'])
def test_judge_missing_required_measurement_is_incomplete(tmp_path, field):
    result = judge_result()
    del result[field]
    assert report_for(tmp_path, 'judge', result)['status'] == 'INCOMPLETE'


def test_judge_partial_matrix_cannot_pass(tmp_path):
    result = judge_result()
    result['full_planned_matrix'] = False
    assert report_for(tmp_path, 'judge', result)['status'] == 'INCOMPLETE'
