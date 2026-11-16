"""Qualification completeness, comparison and owned evidence boundaries."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from qa.qualification import runner


def test_real_catalog_and_observations_qualify_with_all_faults_detected():
    catalog = runner.load_catalog()
    rows = [{'id': identity, 'observation': runner.controls.observe(identity)}
            for identity in runner.controls.CONTROL_IDS]
    baseline = runner.assess(catalog, rows)
    assert baseline['status'] == 'PASS'
    assert baseline['metrics']['matched'] == baseline['metrics']['planned'] == 29
    injected = runner.fault_injections(catalog, rows, baseline)
    assert injected['status'] == 'PASS'
    assert injected['detected'] == injected['planned'] == 6
    assert runner.assess(catalog, rows) == baseline


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(runner.controls, 'CONTROL_IDS', ('P-01', 'N-01', 'I-01'))
    return {'schema_version': '1.0', 'catalog_id': 'evaluator-qualification-v1',
            'evaluators': [{'id': 'fixture', 'purpose': 'Owned test', 'limitations': ['Synthetic only'],
                            'requirements': ['DFR-010']}],
            'controls': [
                {'id': 'P-01', 'evaluator_id': 'fixture', 'kind': 'positive', 'description': 'Matching measurement',
                 'expected': {'decision': 'PASS', 'metric': {'numerator': 1, 'denominator': 2, 'value': 0.5}}},
                {'id': 'N-01', 'evaluator_id': 'fixture', 'kind': 'negative', 'description': 'Known defect',
                 'expected': {'decision': 'FAIL', 'codes': ['KNOWN_DEFECT']}},
                {'id': 'I-01', 'evaluator_id': 'fixture', 'kind': 'incomplete', 'description': 'No measurement',
                 'expected': {'decision': 'INCOMPLETE', 'metric': {'numerator': 0, 'denominator': 0, 'value': None}}},
            ]}


def observations(catalog):
    return [{'id': row['id'], 'observation': deepcopy(row['expected'])} for row in catalog['controls']]


def test_matching_pass_fail_and_incomplete_observations_all_qualify(catalog):
    result = runner.assess(catalog, observations(catalog))
    assert result['status'] == 'PASS'
    assert result['metrics']['matched_rate'] == {'numerator': 3, 'denominator': 3, 'value': 1.0}
    assert result['metrics']['decision_false_approval_rate']['denominator'] == 2
    assert result['metrics']['decision_false_alarm_rate']['denominator'] == 1


def test_missing_record_preserves_the_planned_denominator(catalog):
    result = runner.assess(catalog, observations(catalog)[:-1])
    assert result['status'] == 'INCOMPLETE'
    assert result['metrics']['planned'] == 3
    assert result['metrics']['missing'] == 1
    assert result['metrics']['matched_rate']['value'] == 2 / 3
    assert result['metrics']['decision_false_approval_rate']['denominator'] == 2


@pytest.mark.parametrize('change,code', [('duplicate', 'DUPLICATE_OBSERVATION'),
                                      ('foreign', 'UNREGISTERED_OBSERVATION'),
                                      ('malformed', 'MALFORMED_OBSERVATION_RECORD')])
def test_extra_records_never_become_more_successful_measurements(catalog, change, code):
    rows = observations(catalog)
    if change == 'duplicate':
        rows.append(deepcopy(rows[0]))
    elif change == 'foreign':
        rows.append({'id': 'OTHER', 'observation': {'decision': 'PASS'}})
    else:
        rows.append('invalid')
    result = runner.assess(catalog, rows)
    assert result['status'] == 'ERROR'
    assert result['metrics']['planned'] == 3
    assert code in result['integrity_issues'] + [code for row in result['matrix'] for code in row['issues']]


@pytest.mark.parametrize('actual', [True, 1.0, '1'])
def test_matching_decision_cannot_hide_a_different_metric_type(catalog, actual):
    rows = observations(catalog)
    rows[0]['observation']['metric']['numerator'] = actual
    result = runner.assess(catalog, rows)
    assert result['status'] == 'FAIL'
    assert result['matrix'][0]['status'] == 'FAIL'


def test_missing_metric_is_failure_even_when_decision_matches(catalog):
    rows = observations(catalog)
    rows[0]['observation'].pop('metric')
    assert runner.assess(catalog, rows)['status'] == 'FAIL'


def test_false_approval_and_false_alarm_are_separate_from_control_match(catalog):
    rows = observations(catalog)
    rows[0]['observation']['decision'] = 'FAIL'
    rows[1]['observation']['decision'] = 'PASS'
    result = runner.assess(catalog, rows)
    assert result['metrics']['decision_false_approval_rate'] == {'numerator': 1, 'denominator': 2, 'value': 0.5}
    assert result['metrics']['decision_false_alarm_rate'] == {'numerator': 1, 'denominator': 1, 'value': 1.0}
    assert result['metrics']['mismatched'] == 2


def test_no_positive_controls_means_undefined_false_alarm_rate(catalog):
    catalog['controls'][0]['expected']['decision'] = 'FAIL'
    result = runner.assess(catalog, observations(catalog))
    assert result['status'] == 'PASS'
    assert result['metrics']['decision_false_alarm_rate'] == {'numerator': 0, 'denominator': 0, 'value': None}


@pytest.mark.parametrize('actual', [float('nan'), float('inf'), {'nested': object()}])
def test_invalid_projection_is_error_without_denominator_loss(catalog, actual):
    rows = observations(catalog)
    rows[1]['observation']['unexpected'] = actual
    result = runner.assess(catalog, rows)
    assert result['status'] == 'ERROR'
    assert result['metrics']['planned'] == 3
    assert result['matrix'][1]['issues'] == ['INVALID_OBSERVATION_PROJECTION']
    assert 'observed' not in result['matrix'][1]


def test_exception_record_is_not_an_expected_rejection(catalog):
    catalog['controls'][1]['expected'] = {'decision': 'REJECTED', 'code': 'KNOWN_REJECTION'}
    rows = observations(catalog)
    rows[1] = {'id': 'N-01', 'error_code': 'CONTROL_EXECUTION_ERROR'}
    result = runner.assess(catalog, rows)
    assert result['status'] == 'ERROR'
    assert result['metrics']['errors'] == 1
    assert result['metrics']['matched'] == 2


def test_six_fault_injections_are_detected_and_do_not_mutate_baseline(catalog):
    rows = observations(catalog)
    saved = deepcopy(rows)
    baseline = runner.assess(catalog, rows)
    injected = runner.fault_injections(catalog, rows, baseline)
    assert injected['status'] == 'PASS'
    assert injected['planned'] == injected['detected'] == 6
    assert {row['id'] for row in injected['rows']} == set(runner.INJECTIONS)
    assert all(row['observed_status'] != 'PASS' for row in injected['rows'])
    assert rows == saved
    assert baseline['status'] == 'PASS'


def test_missing_numeric_challenge_is_incomplete_not_qualified(catalog):
    for row in catalog['controls']:
        row['expected'] = {'decision': row['expected']['decision']}
    rows = observations(catalog)
    injected = runner.fault_injections(catalog, rows, runner.assess(catalog, rows))
    assert injected['status'] == 'INCOMPLETE'
    assert injected['detected'] == 5


def test_fault_injections_do_not_claim_detection_on_failing_baseline(catalog):
    rows = observations(catalog)
    rows[0]['observation']['decision'] = 'FAIL'
    injected = runner.fault_injections(catalog, rows, runner.assess(catalog, rows))
    assert injected['status'] == 'INCOMPLETE'
    assert injected['detected'] == 0
    assert all(row['status'] == 'NOT_RUN' for row in injected['rows'])


@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'unknown', 'extra_field', 'wrong_evaluator',
                                  'unknown_kind', 'empty_limits', 'nonfinite', 'bad_requirement'])
def test_invalid_catalog_cannot_reduce_or_redefine_execution(catalog, defect):
    if defect == 'missing':
        catalog['controls'].pop()
    elif defect == 'duplicate':
        catalog['controls'].append(deepcopy(catalog['controls'][0]))
    elif defect == 'unknown':
        catalog['controls'][0]['id'] = 'UNKNOWN-01'
    elif defect == 'extra_field':
        catalog['controls'][0]['command'] = 'untrusted'
    elif defect == 'wrong_evaluator':
        catalog['controls'][0]['evaluator_id'] = 'missing'
    elif defect == 'unknown_kind':
        catalog['controls'][0]['kind'] = 'optional'
    elif defect == 'empty_limits':
        catalog['evaluators'][0]['limitations'] = []
    elif defect == 'nonfinite':
        catalog['controls'][0]['expected']['metric']['value'] = float('inf')
    else:
        catalog['evaluators'][0]['requirements'] = ['DFR-999']
    with pytest.raises(ValueError):
        runner.validate_catalog(catalog)


@pytest.fixture
def project(tmp_path, monkeypatch, catalog):
    source = {'digest': 'a' * 64}
    monkeypatch.setattr(runner, 'snapshot', lambda root: deepcopy(source))
    monkeypatch.setattr(runner, 'compare_snapshots', lambda a, b: {
        'status': 'PASS' if a == b else 'ERROR', 'added': [], 'removed': [], 'changed': []})
    monkeypatch.setattr(runner, 'environment', lambda: {'digest': 'b' * 64})
    monkeypatch.setattr(runner, 'load_catalog', lambda root: deepcopy(catalog))
    saved = {row['id']: deepcopy(row['expected']) for row in catalog['controls']}
    monkeypatch.setattr(runner.controls, 'observe', lambda identity: deepcopy(saved[identity]))
    return tmp_path, 'var/qa/evaluator-qualification/unit'


def result(project):
    root, output = project
    return json.loads((root / output / 'result.json').read_text('utf-8'))


def test_run_pins_own_evidence_and_separates_programmatic_qualification(project):
    root, output = project
    assert runner.run(output, root=root) == 0
    report = result(project)
    assert report['qualification_status'] == report['fault_injection_status'] == report['version_binding'] == 'PASS'
    assert report['product_acceptance'] == 'INCOMPLETE'
    assert report['human_calibration'] == report['model_judge'] == 'NOT_MEASURED'
    assert report['historical_judge'] == 'NOT_REASSESSED'
    evidence = json.loads((root / output / 'evidence.json').read_text('utf-8'))
    assert runner.verify_files(evidence['files'], root)['status'] == 'PASS'
    assert (root / output / 'report.md').is_file()


def test_unexpected_exception_records_all_planned_controls_without_leaking(project, monkeypatch):
    original = runner.controls.observe
    calls = []
    def observe(identity):
        calls.append(identity)
        if identity == 'N-01':
            raise RuntimeError('secret-should-not-be-printed')
        return original(identity)
    monkeypatch.setattr(runner.controls, 'observe', observe)
    root, output = project
    assert runner.run(output, root=root) == 2
    report = result(project)
    assert calls == list(runner.controls.CONTROL_IDS)
    assert report['baseline']['metrics']['planned'] == 3
    assert report['baseline']['metrics']['errors'] == 1
    assert 'secret-should-not-be-printed' not in (root / output / 'result.json').read_text('utf-8')
    assert report['fault_injection_status'] == 'INCOMPLETE'


def test_source_change_before_controls_stops_execution(project, monkeypatch):
    versions = iter([{'digest': 'a'}, {'digest': 'changed'}, {'digest': 'changed'}])
    monkeypatch.setattr(runner, 'snapshot', lambda root: next(versions))
    monkeypatch.setattr(runner.controls, 'observe', lambda identity: pytest.fail('must not execute'))
    root, output = project
    assert runner.run(output, root=root) == 2
    assert result(project)['error_code'] == 'QUALIFICATION_SOURCE_CHANGED'


def test_environment_drift_invalidates_otherwise_successful_qualification(project, monkeypatch):
    versions = iter([{'digest': 'before'}, {'digest': 'after'}])
    monkeypatch.setattr(runner, 'environment', lambda: next(versions))
    root, output = project
    assert runner.run(output, root=root) == 2
    report = result(project)
    assert report['qualification_status'] == 'PASS'
    assert report['version_binding'] == 'ERROR'


def test_replaced_artifact_is_not_accepted_as_current_evidence(project, monkeypatch):
    root, output = project
    calls = 0
    def environment():
        nonlocal calls
        calls += 1
        if calls == 2:
            (root / output / 'baseline.json').write_text('{}', encoding='utf-8')
        return {'digest': 'b'}
    monkeypatch.setattr(runner, 'environment', environment)
    assert runner.run(output, root=root) == 2
    report = result(project)
    assert report['qualification_status'] == 'PASS'
    assert report['evidence_integrity']['status'] == 'ERROR'
    assert report['evidence_integrity']['changed_or_missing'] == [output + '/baseline.json']


def test_owned_results_cannot_be_overwritten(project):
    root, output = project
    assert runner.run(output, root=root) == 0
    saved = (root / output / 'result.json').read_bytes()
    with pytest.raises(runner.QualificationError, match='QUALIFICATION_OUTPUT_EXISTS'):
        runner.run(output, root=root)
    assert (root / output / 'result.json').read_bytes() == saved


@pytest.mark.parametrize('output', ['var/other/one', 'var/qa/evaluator-qualification',
                                  'var/qa/evaluator-qualification/one/two', '../outside',
                                  'var/qa/evaluator-qualification/' + 'x' * 65])
def test_outputs_stay_in_qualification_namespace(tmp_path, output):
    with pytest.raises(ValueError):
        runner.output_path(output, tmp_path)


def test_catalog_duplicate_json_keys_and_hardlinks_are_rejected(tmp_path, catalog):
    path = tmp_path / runner.CATALOG
    path.parent.mkdir(parents=True)
    path.write_text('{"schema_version":"1.0","schema_version":"1.0"}', encoding='utf-8')
    with pytest.raises(ValueError, match='DUPLICATE_JSON_KEY'):
        runner.load_catalog(tmp_path)
    path.write_text(json.dumps(catalog), encoding='utf-8')
    linked = tmp_path / 'linked.json'
    linked.hardlink_to(path)
    with pytest.raises(ValueError, match='INVALID_QA_PATH'):
        runner.load_catalog(tmp_path)
