"""Evidence status, missing inputs, and non-overwriting capture boundaries."""
from __future__ import annotations

import json

import pytest

from qa.dark_factory import baseline


@pytest.fixture
def source(tmp_path):
    # Synthetic stand-ins keep this test independent of unpublished local runs.
    documents = {
        'policy': {'schema_version': '1.0'},
        'progress': {'judge_revision': {'judge_status': 'FAIL'}},
        'regression': {'status': 'PASS', 'tests': 2491, 'failures': 0, 'errors': 0,
                       'skipped': 0, 'pytest_exit_code': 0, 'collected_tests': 1846, 'additional_subtests': 645},
        'llm': {'status': 'PASS', 'full_planned_matrix': True, 'collection_complete': True,
                'source_files_unchanged_during_run': True, 'model_identity_stable': True,
                'template_failures': [], 'repeats_per_draft_case': 3,
                'draft_observation_count': 15, 'control_observation_count': 8,
                'controls': [{'case_id': f'QA-S{n:02}', 'status': 'PASS', 'issues': []}
                             for n in range(5, 13)],
                'raw_attempt_summary': {'observations': 15, 'first_attempt_valid_count': 15,
                    'repair_attempted_count': 0, 'total_http_attempts': 15,
                    'rows': [{'case_id': case, 'repeat_index': repeat, 'http_attempts': 1,
                              'first_attempt_valid': True, 'repair_attempted': False, 'final_status': 'draft'}
                             for case in ('QA-S01', 'QA-S02', 'QA-S03', 'QA-S04', 'QA-S14')
                             for repeat in range(1, 4)]},
                'summary': {'status': 'PASS', 'planned_repeat_count': 15,
                'observed_repeat_count': 15, 'missing_repeat_count': 0,
                'planned_case_count': 5, 'observed_case_count': 5,
                'observed_independence_family_count': 3,
                'fact_violation_count': 0,
                'repeat_checks_pass_rate': {'numerator': 15, 'denominator': 15, 'value': 1.0},
                'case_checks_pass_rate': {'numerator': 5, 'denominator': 5, 'value': 1.0},
                'cases': [{'case_id': case, 'independence_id': family, 'status': 'PASS',
                           'planned_repeats': 3, 'observed_repeats': 3, 'missing_repeat_indices': [],
                           'fact_violation_count': 0, 'status_counts': {'PASS': 3, 'FAIL': 0, 'INCOMPLETE': 0}}
                          for case, family in [('QA-S01', 'F1'), ('QA-S02', 'F1'),
                              ('QA-S03', 'F2'), ('QA-S04', 'F3'), ('QA-S14', 'F2')]]}},
        'retrieval': {'quality_acceptance': 'NOT_ESTABLISHED', 'primary_query_count': 5},
        'judge': {'status': 'FAIL', 'summary': {'overall': {'planned': 20,
                  'valid_responses': 19, 'all_planned_accuracy': {'numerator': 16},
                  'confusion': {'fn': 3}}}},
        'judge_qualification': {'status': 'PASS'},
        'aml_manifest': {'schema_version': '1.0'},
        'retrieval_manifest': {'schema_version': '1.0'},
        'judge_manifest': {'schema_version': '1.0'},
        'judge_rubric': {'schema_version': '1.0'},
    }
    for name, relative in baseline.SOURCES.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == 'capture_program':
            target.write_text('# Synthetic capture program identity\n', encoding='utf-8')
        else:
            target.write_text(json.dumps(documents[name]), encoding='utf-8')
    return tmp_path


def test_original_observations_keep_failure_and_human_incomplete(source):
    report = baseline.capture(source)
    assert report['capture_status'] == 'PASS'
    assert report['source_count_captured'] == report['source_count_expected']
    assert len(report['sources']['capture_program']['sha256']) == 64
    historical = report['historical_observations']
    assert historical['judge']['status'] == 'FAIL'
    assert historical['judge']['false_passes'] == 3
    assert historical['judge']['planned'] == 20
    assert historical['human_calibration']['status'] == 'INCOMPLETE'
    assert report['current_product_status'] == 'INCOMPLETE'


def test_missing_evidence_cannot_become_success(source):
    (source / baseline.SOURCES['judge']).unlink()
    report = baseline.capture(source)
    assert report['capture_status'] == 'INCOMPLETE'
    assert report['historical_observations']['judge']['status'] == 'INCOMPLETE'
    assert report['historical_observations']['human_calibration']['status'] == 'INCOMPLETE'
    assert {'source': 'judge', 'status': 'INCOMPLETE'} in report['issues']


def test_ambiguous_json_is_not_accepted(source):
    (source / baseline.SOURCES['progress']).write_text('{"x": 1, "x": 2}', encoding='utf-8')
    report = baseline.capture(source)
    assert report['capture_status'] == 'INCOMPLETE'
    assert {'source': 'progress', 'status': 'INCOMPLETE'} in report['issues']


def test_historical_judge_pass_never_implies_qualified_judge(source):
    judge_path, progress_path = source / baseline.SOURCES['judge'], source / baseline.SOURCES['progress']
    judge, progress = json.loads(judge_path.read_bytes()), json.loads(progress_path.read_bytes())
    judge['status'], progress['judge_revision']['judge_status'] = 'PASS', 'PASS'
    judge_path.write_text(json.dumps(judge), encoding='utf-8')
    progress_path.write_text(json.dumps(progress), encoding='utf-8')
    report = baseline.capture(source)
    assert report['historical_observations']['judge']['status'] == 'INCOMPLETE'
    assert report['historical_observations']['judge']['observed_historical_status'] == 'PASS'
    assert report['historical_observations']['human_calibration']['status'] == 'INCOMPLETE'


def test_inconsistent_observations_never_promote_a_baseline(source):
    path = source / baseline.SOURCES['regression']
    value = json.loads(path.read_bytes())
    value['status'], value['failures'] = 'PASS', 1
    path.write_text(json.dumps(value), encoding='utf-8')
    report = baseline.capture(source)
    assert report['capture_status'] == 'INCOMPLETE'
    assert report['historical_observations']['deterministic_historical']['status'] == 'INCOMPLETE'
    assert report['historical_observations']['judge']['status'] == 'FAIL'


@pytest.mark.parametrize('field,value', [('pytest_exit_code', 2), ('tests', 2490),
                                        ('collected_tests', 0), ('additional_subtests', -1)])
def test_regression_pass_rejects_conflicting_counts_or_exit_code(source, field, value):
    path = source / baseline.SOURCES['regression']
    data = json.loads(path.read_bytes())
    data[field] = value
    path.write_text(json.dumps(data), encoding='utf-8')
    report = baseline.capture(source)
    assert report['capture_status'] == 'INCOMPLETE'
    assert report['historical_observations']['deterministic_historical']['status'] == 'INCOMPLETE'
    assert report['historical_observations']['judge']['status'] == 'FAIL'


@pytest.mark.parametrize('field,value', [('observed_repeat_count', 14), ('missing_repeat_count', 1),
                                        ('observed_case_count', 4), ('observed_independence_family_count', 0),
                                        ('status', 'INCOMPLETE')])
def test_llm_pass_requires_complete_consistent_summary(source, field, value):
    path = source / baseline.SOURCES['llm']
    data = json.loads(path.read_bytes())
    data['summary'][field] = value
    path.write_text(json.dumps(data), encoding='utf-8')
    report = baseline.capture(source)
    assert report['capture_status'] == 'INCOMPLETE'
    assert report['historical_observations']['live_llm_historical']['status'] == 'INCOMPLETE'
    assert report['historical_observations']['judge']['status'] == 'FAIL'


@pytest.mark.parametrize('change', ['missing_control', 'case_failure', 'inconsistent_family',
                                    'partial_run', 'source_changed', 'model_changed'])
def test_llm_pass_rejects_incomplete_matrix_or_failed_controls(source, change):
    path = source / baseline.SOURCES['llm']
    data = json.loads(path.read_bytes())
    if change == 'missing_control':
        data['controls'].pop()
        data['control_observation_count'] -= 1
    elif change == 'case_failure':
        data['summary']['cases'][0]['status'] = 'FAIL'
    elif change == 'inconsistent_family':
        data['summary']['observed_independence_family_count'] = 5
    elif change == 'partial_run':
        data['full_planned_matrix'] = False
    elif change == 'source_changed':
        data['source_files_unchanged_during_run'] = False
    else:
        data['model_identity_stable'] = False
    path.write_text(json.dumps(data), encoding='utf-8')
    report = baseline.capture(source)
    assert report['capture_status'] == 'INCOMPLETE'
    assert report['historical_observations']['judge']['status'] == 'FAIL'


@pytest.mark.parametrize('change', ['summary_fact_violation', 'case_fact_violation', 'case_count_failure',
                                    'wrong_pass_rate', 'missing_attempt', 'duplicate_attempt', 'wrong_attempt_count'])
def test_llm_pass_rejects_hidden_failure_and_incomplete_attempts(source, change):
    path = source / baseline.SOURCES['llm']
    data = json.loads(path.read_bytes())
    if change == 'summary_fact_violation':
        data['summary']['fact_violation_count'] = 1
    elif change == 'case_fact_violation':
        data['summary']['cases'][0]['fact_violation_count'] = 1
    elif change == 'case_count_failure':
        data['summary']['cases'][0]['status_counts'].update(PASS=2, FAIL=1)
    elif change == 'wrong_pass_rate':
        data['summary']['repeat_checks_pass_rate']['numerator'] = 14
    elif change == 'missing_attempt':
        data['raw_attempt_summary']['rows'].pop()
    elif change == 'duplicate_attempt':
        data['raw_attempt_summary']['rows'][0] = dict(data['raw_attempt_summary']['rows'][1])
    else:
        data['raw_attempt_summary']['total_http_attempts'] = 16
    path.write_text(json.dumps(data), encoding='utf-8')
    report = baseline.capture(source)
    assert report['capture_status'] == 'INCOMPLETE'
    assert report['historical_observations']['live_llm_historical']['status'] == 'INCOMPLETE'
    assert report['historical_observations']['judge']['status'] == 'FAIL'


@pytest.mark.parametrize('relative', ['../outside.json', 'var/qa/dark-factory/../escape.json',
                                     'var/qa/dark-factory/run/other.json', 'qa/result.json'])
def test_output_must_stay_in_owned_folder(source, relative):
    with pytest.raises(ValueError):
        baseline.write_new({'a': 1}, relative, source)


def test_saved_file_is_immutable_by_repeated_capture(source):
    relative = 'var/qa/dark-factory/baseline.json'
    output = baseline.write_new({'status': 'one'}, relative, source)
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        baseline.write_new({'status': 'two'}, relative, source)
    assert output.read_bytes() == original
