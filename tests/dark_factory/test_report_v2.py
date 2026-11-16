"""Standalone tests for evidence separation, with no local models or archives."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from qa.dark_factory import report_v2 as report
from qa.dark_editions.probe import cases as edition_cases


def base():
    return {'schema_version': '1.0', 'historical_judge': 'FAIL',
            **{field: 'PASS' for field in report.BASE_MEASUREMENTS},
            'human_acceptance': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE',
            'hidden_holdout': 'NOT_ESTABLISHED', 'deterministic_regression': 'PASS'}


def test_base_summary_is_not_requalified_and_preserves_failure():
    row = report.base_report(base())
    assert row['historical_judge'] == 'FAIL'
    assert row['evidence_status'] == 'PASS'
    assert 'not requalified' in row['scope']
    invalid = dict(base(), human_acceptance='PASS')
    row = report.base_report(invalid)
    assert row['evidence_status'] == 'INCOMPLETE'
    assert row['historical_judge'] == 'FAIL'


@pytest.mark.parametrize('regression', ['PASS', 'FAIL', 'INCOMPLETE'])
def test_base_accepts_only_known_regression_states(regression):
    row = report.base_report(dict(base(), deterministic_regression=regression))
    assert row['evidence_status'] == 'PASS'
    assert row['deterministic_regression'] == regression


@pytest.mark.parametrize('regression', [None, 'MAYBE', 1, [], {}])
def test_base_invalid_or_missing_regression_cannot_appear_complete(regression):
    row = report.base_report(dict(base(), deterministic_regression=regression))
    assert row['evidence_status'] == 'INCOMPLETE'
    assert row['deterministic_regression'] == 'INCOMPLETE'
    assert row['historical_judge'] == 'FAIL'


def test_base_keeps_regression_failure_when_other_fields_are_inconsistent():
    row = report.base_report(dict(base(), deterministic_regression='FAIL', human_acceptance='PASS'))
    assert row['evidence_status'] == 'INCOMPLETE'
    assert row['deterministic_regression'] == 'FAIL'
    assert report.evidence_exit_code({'components': {'base': row}}) == 1


def test_base_missing_regression_is_incomplete():
    doc = base()
    del doc['deterministic_regression']
    row = report.base_report(doc)
    assert row['evidence_status'] == 'INCOMPLETE'
    assert row['deterministic_regression'] == 'INCOMPLETE'


@pytest.mark.parametrize('field', report.BASE_MEASUREMENTS)
@pytest.mark.parametrize('status,exit_code', [('PASS', 2), ('FAIL', 1), ('INCOMPLETE', 2)])
def test_base_all_measurement_states_are_recorded_and_gated(field, status, exit_code):
    doc = dict(base(), historical_judge='INCOMPLETE')
    doc[field] = status
    row = report.base_report(doc)
    assert row['evidence_status'] == 'PASS'
    assert row[field] == status
    assert report.evidence_exit_code({'components': {'base': row}}) == exit_code


@pytest.mark.parametrize('field', report.BASE_MEASUREMENTS)
@pytest.mark.parametrize('bad', [None, 'UNKNOWN', [], {}])
def test_base_missing_or_unknown_measurement_is_incomplete(field, bad):
    doc = dict(base(), historical_judge='INCOMPLETE')
    doc[field] = bad
    row = report.base_report(doc)
    assert row['evidence_status'] == 'INCOMPLETE'
    assert row[field] == 'INCOMPLETE'
    assert report.evidence_exit_code({'components': {'base': row}}) == 2


@pytest.mark.parametrize('field', report.BASE_MEASUREMENTS)
def test_base_retains_measurement_failure_despite_bad_metadata(field):
    doc = dict(base(), historical_judge='INCOMPLETE', human_acceptance='PASS')
    doc[field] = 'FAIL'
    row = report.base_report(doc)
    assert row['evidence_status'] == 'INCOMPLETE'
    assert row[field] == 'FAIL'
    assert report.evidence_exit_code({'components': {'base': row}}) == 1


@pytest.mark.parametrize('field', report.BASE_MEASUREMENTS)
def test_base_build_preserves_fail_when_partial_document_is_invalid(tmp_path, monkeypatch, field):
    directory = tmp_path / 'var/qa/dark-factory'
    directory.mkdir(parents=True)
    doc = {'schema_version': 'bad', field: 'FAIL'}
    (directory / 'partial.json').write_text(json.dumps(doc), encoding='utf-8')
    monkeypatch.setattr(report, 'ROOT', tmp_path)
    result = report.build({'base': 'var/qa/dark-factory/partial.json'})
    assert result['components']['base'][field] == 'FAIL'
    assert result['components']['base']['evidence_status'] == 'INCOMPLETE'
    assert report.evidence_exit_code(result) == 1


@pytest.mark.parametrize('field', report.BASE_MEASUREMENTS)
def test_base_build_preserves_fail_if_local_processing_raises(tmp_path, monkeypatch, field):
    directory = tmp_path / 'var/qa/dark-factory'
    directory.mkdir(parents=True)
    (directory / 'partial.json').write_text(json.dumps({field: 'FAIL'}), encoding='utf-8')
    monkeypatch.setattr(report, 'ROOT', tmp_path)
    def broken(_doc):
        raise ValueError('synthetic processing failure')
    monkeypatch.setattr(report, 'base_report', broken)
    result = report.build({'base': 'var/qa/dark-factory/partial.json'})
    assert result['components']['base'][field] == 'FAIL'
    assert result['components']['base']['evidence_status'] == 'INCOMPLETE'
    assert report.evidence_exit_code(result) == 1


def test_base_pass_only_still_does_not_confirm_human_acceptance():
    row = report.base_report(dict(base(), historical_judge='INCOMPLETE'))
    assert all(row[field] == 'PASS' for field in report.BASE_MEASUREMENTS)
    assert 'not requalified' in row['scope']


def test_gate_requires_recomputed_exact_evidence():
    value = {'status': 'PASS', 'historical_judge_status': 'FAIL',
             'counts': {'gate_fail_judge_pass_rate': {'numerator': 2, 'denominator': 12},
                        'planned_single_cases': 18, 'planned_pair_observations': 2}}
    assert report.gate_report(value, deepcopy(value))['evidence_status'] == 'PASS'
    changed = deepcopy(value)
    changed['counts']['gate_fail_judge_pass_rate']['numerator'] = 0
    result = report.gate_report(value, changed)
    assert result['evidence_status'] == 'INCOMPLETE'
    assert result['historical_judge'] == 'FAIL'


def editions():
    planned = edition_cases()
    doc = {'status': 'PASS', 'planned': 4, 'embedding': 'FAKE_EQUAL_VECTORS', 'llm': 'NOT_RUN',
           'downstream_selection': 'NOT_OBSERVED', 'legal_applicability': 'NOT_ESTABLISHED',
           'human_acceptance': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE',
           'hidden_holdout': 'NOT_ESTABLISHED', 'cases': [
               {'case_id': case['id'], 'status': 'PASS', 'expected': deepcopy(case['expected']),
                'observed': deepcopy(case['expected'])} for case in planned]}
    return doc, planned


def test_editions_checks_each_expected_result_not_just_green_status():
    doc, planned = editions()
    assert report.editions_report(doc, planned)['observed_status'] == 'PASS'
    doc['cases'][0]['observed']['target_present'] = True
    assert report.editions_report(doc, planned)['evidence_status'] == 'INCOMPLETE'


def test_editions_retains_recorded_failure_when_rows_missing():
    assert report.editions_report({'status': 'FAIL'}, edition_cases())['observed_status'] == 'FAIL'


def v3():
    source = {'probe.py': 'runner', 'cases.json': 'catalog'}
    ids = ('DF3-NO-WINDOW', 'DF3-CURRENCIES', 'DF3-FOREIGN-CARD')
    snapshots = {id: {'fixture': id} for id in ids}
    doc = {'status': 'PASS', 'embedding': 'FAKE_EQUAL_VECTORS', 'engine': 'template',
           'transport': 'FAKE_NOT_CALLED', 'real_llm': 'NOT_RUN', 'sme_acceptance': 'INCOMPLETE',
           'human_acceptance': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE',
           'hidden_holdout': 'NOT_ESTABLISHED', 'catalog_sha256': 'catalog',
           'source_sha256_before': source, 'source_sha256_after': source,
           'cases': [{'id': id, 'status': 'PASS', 'issues': [], 'snapshot_sha256': snapshots[id]}
                     for id in ids]}
    return doc, source, snapshots


def test_v3_rejects_source_or_fixture_change():
    doc, sources, snapshots = v3()
    assert report.v3_report(doc, sources, snapshots)['observed_status'] == 'PASS'
    changed = deepcopy(snapshots)
    changed['DF3-CURRENCIES']['fixture'] = 'changed'
    assert report.v3_report(doc, sources, changed)['evidence_status'] == 'INCOMPLETE'
    assert report.v3_report(doc, dict(sources, **{'probe.py': 'changed'}), snapshots)['evidence_status'] == 'INCOMPLETE'


def test_v3_does_not_discard_observed_failure():
    doc, sources, snapshots = v3()
    doc['status'] = 'FAIL'
    doc['cases'][1].update(status='FAIL', issues=['WRONG_SUM'])
    row = report.v3_report(doc, sources, snapshots)
    assert row['observed_status'] == 'FAIL'
    assert row['evidence_status'] == 'PASS'


def test_downstream_measurement_pass_is_not_policy_safety_pass():
    summary = {'status': 'PASS', 'policy_safety': 'FAIL_FOR_CONTROLLED_MUTATION',
               'semantic_relevance': 'NOT_ESTABLISHED', 'product_quality': 'INCOMPLETE',
               'human_acceptance': 'INCOMPLETE', 'hidden_holdout': 'NOT_ESTABLISHED'}
    child = {'policy_safety': 'FAIL_FOR_CONTROLLED_MUTATION'}
    row = report.downstream_report(summary, child, {'x': 'y'}, {'x': 'y'}, lambda *_: 'PASS')
    assert row['measurement_status'] == 'PASS'
    assert row['policy_safety'] == 'FAIL_FOR_CONTROLLED_MUTATION'
    row = report.downstream_report(summary, child, {'x': 'y'}, {'x': 'changed'}, lambda *_: 'PASS')
    assert row['evidence_status'] == 'INCOMPLETE'
    assert row['recorded_policy_safety'] == 'FAIL_FOR_CONTROLLED_MUTATION'


def test_injection_invalid_response_stays_incomplete():
    rows = [{'variant': 'attack', 'delivery': 'COMPLETE', 'contract': 'INVALID',
             'verdict': None, 'judge_against_known_defect': 'INCOMPLETE'}]
    summary = {'measurement_status': 'INCOMPLETE', 'historical_judge_status': 'FAIL',
               'human_calibration': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE',
               'hidden_holdout': 'NOT_ESTABLISHED', 'cases': rows}
    row = report.injection_report(summary, 'INCOMPLETE', rows)
    assert row['evidence_status'] == 'PASS'
    assert row['measurement_status'] == 'INCOMPLETE'
    assert row['cases'][0]['judge_against_known_defect'] == 'INCOMPLETE'
    assert report.injection_report(summary, 'PASS', rows)['evidence_status'] == 'INCOMPLETE'


def test_missing_evidence_is_incomplete_without_model_calls():
    result = report.build({})
    assert result['product_acceptance'] == 'INCOMPLETE'
    assert all(row['evidence_status'] == 'INCOMPLETE' for row in result['components'].values())
    assert report.evidence_exit_code(result) == 2


def test_exit_code_keeps_failures_even_when_other_evidence_incomplete():
    result = {'components': {'known': {'evidence_status': 'PASS', 'historical_judge': 'FAIL'},
                             'missing': {'evidence_status': 'INCOMPLETE'}}}
    assert report.evidence_exit_code(result) == 1
    result['components']['known'] = {'evidence_status': 'PASS',
                                     'policy_safety': 'FAIL_FOR_CONTROLLED_MUTATION'}
    assert report.evidence_exit_code(result) == 1
    result['components']['known'] = {'evidence_status': 'PASS', 'measurement_status': 'PASS'}
    assert report.evidence_exit_code(result) == 2


@pytest.mark.parametrize('field', ['observed_status', 'deterministic_regression',
                                   'historical_judge', 'measurement_status'])
@pytest.mark.parametrize('status,expected', [('FAIL', 1), ('INCOMPLETE', 2), ('UNKNOWN', 2), ('PASS', 0)])
def test_exit_code_tracks_each_measurement_status(field, status, expected):
    assert report.evidence_exit_code({'components': {'case': {'evidence_status': 'PASS', field: status}}}) == expected


def test_valid_abstention_does_not_count_as_a_successful_known_defect_judgment():
    result = {'components': {'judge': {'evidence_status': 'PASS', 'measurement_status': 'PASS',
             'cases': [{'contract': 'VALID', 'verdict': 'abstain', 'judge_against_known_defect': 'INCOMPLETE'}]}}}
    assert report.evidence_exit_code(result) == 2
    result['components']['judge']['cases'][0]['judge_against_known_defect'] = 'FAIL'
    assert report.evidence_exit_code(result) == 1
    result['components']['judge']['cases'][0]['judge_against_known_defect'] = 'PASS'
    assert report.evidence_exit_code(result) == 0


def test_malformed_cases_or_no_components_cannot_count_as_complete():
    assert report.evidence_exit_code({'components': {}}) == 2
    result = {'components': {'case': {'evidence_status': 'PASS', 'cases': [None]}}}
    assert report.evidence_exit_code(result) == 2
    result['components']['case']['deterministic_regression'] = 'FAIL'
    assert report.evidence_exit_code(result) == 1


def test_v3_incomplete_observation_cannot_return_zero():
    doc, sources, snapshots = v3()
    doc['status'] = 'INCOMPLETE'
    doc['cases'][1].update(status='INCOMPLETE', issues=['NOT_OBSERVED'])
    row = report.v3_report(doc, sources, snapshots)
    assert row['evidence_status'] == 'PASS'
    assert row['observed_status'] == 'INCOMPLETE'
    assert report.evidence_exit_code({'components': {'v3': row}}) == 2


@pytest.mark.parametrize('path', ['../outside', 'var/qa/dark-factory/../secret.json',
                                  'var/qa/dark-factory/x.json/extra', 'C:/secret.json'])
def test_unsafe_path_rejected(path):
    with pytest.raises(ValueError):
        report.build({'base': path})


def test_missing_child_keeps_explicit_recorded_failure(tmp_path, monkeypatch):
    root = tmp_path
    directory = root / 'var/qa/dark-downstream/test'
    directory.mkdir(parents=True)
    (directory / 'summary.json').write_text(json.dumps({'policy_safety': 'FAIL_FOR_CONTROLLED_MUTATION'}))
    monkeypatch.setattr(report, 'ROOT', root)
    row = report.build({'downstream': 'var/qa/dark-downstream/test'})['components']['downstream']
    assert row['evidence_status'] == 'INCOMPLETE'
    assert row['recorded_policy_safety'] == 'FAIL_FOR_CONTROLLED_MUTATION'


def test_present_judge_failure_survives_missing_dependencies(tmp_path, monkeypatch):
    path = tmp_path / 'var/qa/dark-judge-injection/test'
    path.mkdir(parents=True)
    (path / 'summary.json').write_text(json.dumps({'historical_judge_status': 'FAIL'}))
    monkeypatch.setattr(report, 'ROOT', tmp_path)
    row = report.build({'judge_injection': 'var/qa/dark-judge-injection/test'})['components']['judge_injection']
    assert row['evidence_status'] == 'INCOMPLETE'
    assert row['historical_judge'] == 'FAIL'


def _judge_saved_fixture(tmp_path, monkeypatch, *, version):
    from qa.dark_judge_injection import live, live_v2
    directory = tmp_path / 'var/qa/dark-judge-injection/demo'
    directory.mkdir(parents=True)
    marker = {'status': 'STARTED', 'source_sha256_before': {'synthetic': 'hash'}}
    rows = [{'variant': 'neutral', 'delivery': 'COMPLETE', 'contract': 'VALID',
             'verdict': 'fail', 'judge_against_known_defect': 'PASS'},
            {'variant': 'attack', 'delivery': 'COMPLETE', 'contract': 'INVALID',
             'verdict': None, 'judge_against_known_defect': 'INCOMPLETE'}]
    summary = {'measurement_status': 'INCOMPLETE', 'historical_judge_status': 'FAIL',
               'human_calibration': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE',
               'hidden_holdout': 'NOT_ESTABLISHED', 'cases': rows}
    def write(name, data):
        (directory / name).write_text(json.dumps(data), encoding='utf-8')
    write('summary.json', summary)
    write('attempt.json', marker)
    for name in ('child-result.json', 'neutral-start.json', 'neutral-result.json',
                 'attack-start.json', 'attack-result.json'):
        write(name, {})
    if version == '2':
        write('neutral-raw.json', {'integrity': 'synthetic'})
        write('attack-raw.json', {'integrity': 'synthetic'})
    runner = live_v2 if version == '2' else live
    monkeypatch.setattr(report, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'source_hashes', lambda: {'synthetic': 'hash'})
    def validate(directory_path, before, after):
        assert before == after == {'synthetic': 'hash'}
        if version == '2':
            for variant in ('neutral', 'attack'):
                if json.loads((directory_path / f'{variant}-raw.json').read_text()) != {'integrity': 'synthetic'}:
                    raise ValueError('SYNTHETIC_WIRE_CHANGED')
        return 'INCOMPLETE', rows
    monkeypatch.setattr(runner, 'validate_saved', validate)
    return directory, runner


@pytest.mark.parametrize('version', ['1', '2'])
def test_saved_judge_runner_version_revalidates_first_attempts(tmp_path, monkeypatch, version):
    _judge_saved_fixture(tmp_path, monkeypatch, version=version)
    paths = {'judge_injection': 'var/qa/dark-judge-injection/demo'}
    if version == '2':
        paths['judge_injection_version'] = '2'
    row = report.build(paths)['components']['judge_injection']
    assert row['evidence_status'] == 'PASS'
    assert row['runner_version'] == version
    assert row['measurement_status'] == 'INCOMPLETE'
    assert row['historical_judge'] == 'FAIL'


@pytest.mark.parametrize('problem', ['missing_raw', 'changed_raw', 'changed_sources'])
def test_v2_missing_or_modified_wire_and_sources_are_incomplete(tmp_path, monkeypatch, problem):
    directory, runner = _judge_saved_fixture(tmp_path, monkeypatch, version='2')
    if problem == 'missing_raw':
        (directory / 'attack-raw.json').unlink()
    elif problem == 'changed_raw':
        (directory / 'attack-raw.json').write_text(json.dumps({'integrity': 'changed'}))
    else:
        monkeypatch.setattr(runner, 'source_hashes', lambda: {'synthetic': 'changed'})
    row = report.build({'judge_injection': 'var/qa/dark-judge-injection/demo',
                        'judge_injection_version': '2'})['components']['judge_injection']
    assert row['evidence_status'] == 'INCOMPLETE'
    assert row['historical_judge'] == 'FAIL'


def test_unknown_judge_runner_version_is_rejected():
    with pytest.raises(ValueError, match='INVALID_JUDGE_INJECTION_VERSION'):
        report.build({'judge_injection_version': 'unknown'})


def test_inconsistent_v3_summary_cannot_hide_failed_case():
    doc, sources, snapshots = v3()
    doc['cases'][1].update(status='FAIL', issues=['WRONG_SUM'])
    row = report.v3_report(doc, sources, snapshots)
    assert row['evidence_status'] == 'INCOMPLETE'
    assert report.evidence_exit_code({'components': {'v3': row}}) == 1


def test_inconsistent_editions_summary_cannot_hide_failed_case():
    doc, cases = editions()
    doc['cases'][0].update(status='FAIL')
    row = report.editions_report(doc, cases)
    assert row['evidence_status'] == 'INCOMPLETE'
    assert report.evidence_exit_code({'components': {'editions': row}}) == 1


def test_downstream_failed_measurement_survives_missing_child():
    row = report.downstream_report({'status': 'FAIL'}, None, {}, {}, lambda *_: 'PASS')
    assert row['evidence_status'] == 'INCOMPLETE'
    assert report.evidence_exit_code({'components': {'downstream': row}}) == 1


def test_build_retains_failed_case_when_snapshot_is_missing(tmp_path, monkeypatch):
    directory = tmp_path / 'var/qa/dark-factory-v3/partial'
    directory.mkdir(parents=True)
    (directory / 'result.json').write_text(json.dumps({
        'status': 'PASS', 'cases': [{'id': 'DF3-CURRENCIES', 'status': 'FAIL'}]}))
    monkeypatch.setattr(report, 'ROOT', tmp_path)
    result = report.build({'v3': 'var/qa/dark-factory-v3/partial'})
    assert result['components']['v3']['evidence_status'] == 'INCOMPLETE'
    assert report.evidence_exit_code(result) == 1
