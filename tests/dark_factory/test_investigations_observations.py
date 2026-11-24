"""Actual synthetic controls, sensitivity and owned fixture boundaries."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import socket
import subprocess

import pytest

from qa.investigations import observations
from src.workflow import store as workflow_store


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def generated(tmp_path_factory):
    root = tmp_path_factory.mktemp('investigation-controls')
    shutil.copytree(ROOT / 'qa/datasets/aml-v1', root / 'qa/datasets/aml-v1')
    (root / 'qa/environments').mkdir(parents=True)
    shutil.copyfile(ROOT / 'qa/environments/catalog-v1.json', root / 'qa/environments/catalog-v1.json')
    directory = root / 'var/qa/investigations/controls'
    directory.mkdir(parents=True)
    workspace = root / 'var/workflow/investigations/controls/http-ack-replay'
    workspace.mkdir(parents=True)
    before = {path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
              for path in (root / 'qa').rglob('*') if path.is_file()}

    def forbidden(*args, **kwargs):
        raise AssertionError('LIVE_DEPENDENCY_FORBIDDEN')

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(workflow_store, 'WORKFLOW_ROOT', root / 'var/workflow')
        monkeypatch.setattr(socket, 'create_connection', forbidden)
        monkeypatch.setattr(socket.socket, 'connect', forbidden)
        monkeypatch.setattr(subprocess, 'Popen', forbidden)
        monkeypatch.setattr('psycopg.connect', forbidden)
        result = observations.generate(root, directory, workspace)
    after = {path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
             for path in (root / 'qa').rglob('*') if path.is_file()}
    assert before == after
    return root, directory, workspace, result


def test_actual_controls_pass_without_live_dependencies_or_frozen_input_changes(generated):
    _root, _directory, workspace, result = generated
    qualification = observations.qualify(result)
    assert qualification['status'] == 'PASS', qualification
    assert len(qualification['checks']) == 11
    assert all(row['passed'] is True for row in qualification['checks'])
    assert result['autonomy']['new_model_calls'] == 0
    assert result['autonomy']['product_acceptance'] == 'INCOMPLETE'
    assert (workspace / 'workflow.sqlite3').is_file()


def test_actual_checksum_detects_only_the_new_owned_copy(generated):
    root, directory, _workspace, result = generated
    evidence = result['integrity']
    path = directory / 'fixtures/checksum-control.json'
    assert json.loads(path.read_text('utf-8')) == {'synthetic_training_copy': True, 'value': 2}
    assert evidence['after_pin']['sha256'] == sha256(path.read_bytes()).hexdigest()
    assert evidence['before_pin']['sha256'] != evidence['after_pin']['sha256']
    assert evidence['before_verification'] == {'status': 'PASS', 'changed_or_missing': []}
    assert evidence['after_verification'] == {'status': 'ERROR', 'changed_or_missing': [path.relative_to(root).as_posix()]}


def test_six_fixed_fact_views_are_copies_and_do_not_contain_reports(generated):
    result = generated[-1]
    before = deepcopy(result)
    views = observations.project(result)
    assert [row['id'] for row in views] == ['L01', 'L02', 'L03', 'L04', 'L05', 'L06']
    for row in views:
        assert [fact['id'] for fact in row['facts']] == ['F01', 'F02', 'F03', 'F04']
        for fact in row['facts']:
            assert set(fact) == {'id', 'label', 'value', 'source'}
            assert '.json#' in fact['source']
            assert not any(word in json.dumps(fact['value']) for word in ('fact_order', 'rule_order', 'report_id', 'correlation_id'))
    views[1]['facts'][3]['value']['max_tokens'] = 999
    assert result == before


def test_all_expected_failures_are_preserved_as_learning_facts(generated):
    facts = {row['id']: {fact['id']: fact['value'] for fact in row['facts']}
             for row in observations.project(generated[-1])}
    assert facts['L01']['F01'] == 'FAIL'
    assert facts['L01']['F02'] == {'final_candidate_status': 'PASS', 'cycle_status': 'FAIL'}
    assert facts['L02']['F01'] == {'unknown_usage_attempts': 1, 'total_tokens': None}
    assert facts['L02']['F02'] == {'status': 'INCOMPLETE', 'termination_reason': 'UNKNOWN_USAGE'}
    assert facts['L03']['F01'] == {'planned_slots': 12, 'observed_slots': 11}
    assert facts['L03']['F02'] == {'missing_slots': 1, 'status': 'INCOMPLETE'}
    assert facts['L04']['F01'] == {'cases': 3, 'families': 2, 'repeat_slots': 6}
    assert facts['L04']['F02'] == {'distinct_semantic_reports': 1, 'distinct_ordered_reports': 2,
                                'population_inference': 'NOT_ESTABLISHED'}
    assert facts['L06']['F01']['job_count'] == 1
    assert facts['L06']['F02'] == {'job_count': 1, 'submit_calls': 2, 'key_count': 1}


@pytest.mark.parametrize(('path', 'replacement'), [
    (('autonomy', 'new_model_calls'), True),
    (('autonomy', 'status'), 'FAIL'),
    (('autonomy', 'controls', 1, 'cycle', 'cases', 0, 'first_attempt_status'), 'PASS'),
    (('autonomy', 'controls', 1, 'cycle', 'status'), 'PASS'),
    (('autonomy', 'controls', 1, 'cycle', 'cases', 0, 'final_candidate_status'), 'FAIL'),
    (('autonomy', 'controls', 1, 'cycle', 'provider_calls'), -1),
    (('autonomy', 'controls', 1, 'cycle', 'provider_calls'), 3),
    (('autonomy', 'controls', 1, 'cycle', 'provider_calls'), '2'),
    (('autonomy', 'controls', 1, 'cycle', 'provider_calls'), {'raw': 'payload'}),
    (('autonomy', 'controls', 7, 'cycle', 'ledger', 'total_tokens'), 0),
    (('autonomy', 'controls', 7, 'cycle', 'ledger', 'unknown_usage_attempts'), True),
    (('autonomy', 'controls', 7, 'cycle', 'termination_reason'), 'ALL_CASES_FINISHED'),
    (('autonomy', 'controls', 7, 'cycle', 'ledger', 'known_tokens'), -1),
    (('autonomy', 'controls', 7, 'cycle', 'ledger', 'known_tokens'), 1),
    (('autonomy', 'controls', 7, 'cycle', 'ledger', 'known_tokens'), False),
    (('autonomy', 'controls', 7, 'cycle', 'ledger', 'known_tokens'), '0'),
    (('autonomy', 'controls', 7, 'cycle', 'ledger', 'known_tokens'), {'raw': 'payload'}),
    (('autonomy', 'controls', 7, 'plan', 'limits'), {'max_attempts': 3, 'max_tokens': 30, 'wall_seconds': 5.0, 'raw': 'payload'}),
    (('autonomy', 'controls', 7, 'plan', 'limits', 'max_attempts'), True),
    (('autonomy', 'controls', 7, 'plan', 'limits', 'max_tokens'), -30),
    (('autonomy', 'controls', 7, 'plan', 'limits', 'max_tokens'), 31),
    (('autonomy', 'controls', 7, 'plan', 'limits', 'wall_seconds'), '5.0'),
    (('stability', 'negative_statistics', 'missing_repeat', 'status'), 'PASS'),
    (('stability', 'negative_statistics', 'missing_repeat', 'observed_slots'), 12),
    (('stability', 'negative_statistics', 'missing_repeat', 'not_run_slots'), True),
    (('stability', 'negative_statistics', 'missing_repeat', 'population_inference'), 'ESTABLISHED'),
    (('stability', 'statistics', 'arms', 'candidate', 'planned_family_count'), 6),
    (('stability', 'statistics', 'arms', 'candidate', 'cases', 0, 'variability', 'distinct_semantic_reports'), 2),
    (('stability', 'statistics', 'arms', 'candidate', 'interval_status'), 'UNCONDITIONAL'),
    (('stability', 'statistics', 'evidence_kind'), 'real_model'),
    (('stability', 'statistics', 'population_inference'), 'ESTABLISHED'),
    (('integrity', 'before_pin', 'path'), '../old-evidence/result.json'),
    (('integrity', 'after_pin', 'sha256'), 'not-a-hash'),
    (('integrity', 'after_pin', 'size'), True),
    (('integrity', 'after_pin', 'size'), 1),
    (('integrity', 'before_verification', 'status'), 'ERROR'),
    (('integrity', 'after_verification', 'status'), 'PASS'),
    (('integrity', 'after_verification', 'changed_or_missing'), []),
    (('environment', 'observation', 'steps', 1, 'job_count'), 0),
    (('environment', 'observation', 'steps', 4, 'job_count'), 2),
    (('environment', 'observation', 'steps', 4, 'submit_calls'), 1),
    (('environment', 'observation', 'steps', 4, 'key_count'), 2),
    (('environment', 'observation', 'steps', 5, 'queue_status'), 'approved'),
    (('environment', 'observation', 'steps', 5, 'job_status'), 'approved'),
    (('environment', 'observation', 'trace'), []),
    (('environment', 'observation', 'invariants', 1, 'passed'), False),
    (('environment', 'observation', 'dependencies', 'modeled'), []),
    (('environment', 'expected_steps_match'), 1),
    (('environment', 'assessment', 'consistent'), False),
])
def test_control_qualification_rejects_changed_facts_and_trace(generated, path, replacement):
    result = deepcopy(generated[-1])
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    assert observations.qualify(result)['status'] == 'FAIL'


def test_self_consistent_statistics_cannot_relabel_synthetic_data_as_model_measurements(generated):
    from qa.stability.statistics import summarize
    result = deepcopy(generated[-1])
    saved = result['stability']
    saved['plan']['evidence_kind'] = 'historical_llm'
    saved['statistics'] = summarize(saved['plan'], saved['observations'])
    saved['negative_statistics']['missing_repeat'] = summarize(
        saved['plan'], saved['negative_observations']['missing_repeat'])
    qualification = observations.qualify(result)
    checks = {row['id']: row['passed'] for row in qualification['checks']}
    assert checks['statistics_recomputed'] is True
    assert checks['context_facts'] is False
    assert qualification['status'] == 'FAIL'


@pytest.mark.parametrize('value', [None, [], {}, {'autonomy': {}}, {'autonomy': {'controls': []}}])
def test_malformed_saved_observations_fail_closed(value):
    assert observations.qualify(value)['status'] == 'FAIL'
    with pytest.raises(ValueError, match='INVESTIGATION_OBSERVATIONS_INVALID'):
        observations.project(value)


def test_existing_workspace_is_never_reused_or_changed(generated):
    root, directory, workspace, _result = generated
    fixture = directory / 'fixtures/checksum-control.json'
    before = fixture.read_bytes()
    with pytest.raises(ValueError, match='INVESTIGATION_WORKSPACE_SCOPE'):
        observations.generate(root, directory, workspace)
    assert fixture.read_bytes() == before


def test_unrelated_folder_is_rejected_before_any_control_runs(tmp_path):
    directory = tmp_path / 'unrelated'
    directory.mkdir()
    workspace = tmp_path / 'another'
    workspace.mkdir()
    with pytest.raises(ValueError, match='INVESTIGATION_WORKSPACE_SCOPE'):
        observations.generate(tmp_path, directory, workspace)
    assert list(directory.iterdir()) == list(workspace.iterdir()) == []


def test_existing_fixture_is_preserved_with_a_fresh_empty_workspace(tmp_path):
    directory = tmp_path / 'var/qa/investigations/existing'
    fixture = directory / 'fixtures/checksum-control.json'
    fixture.parent.mkdir(parents=True)
    fixture.write_text('preserve me', encoding='utf-8')
    workspace = tmp_path / 'var/workflow/investigations/existing/http-ack-replay'
    workspace.mkdir(parents=True)
    with pytest.raises(ValueError, match='INVESTIGATION_FIXTURE_EXISTS'):
        observations.generate(tmp_path, directory, workspace)
    assert fixture.read_text('utf-8') == 'preserve me'
    assert list(workspace.iterdir()) == []
