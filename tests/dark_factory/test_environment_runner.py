"""Full-denominator assessment and owned evidence; fixtures do not claim live QA."""
from copy import deepcopy
import json

import pytest

from qa.environments import runner


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(runner.models, 'SCENARIO_IDS', ('one', 'two'))
    projection = {'action': 'receive', 'outcome': ['queued'], 'queue_status': 'queued',
                  'queue_error': None, 'job_status': None, 'job_count': 0,
                  'submit_calls': 0, 'key_count': 0, 'offset_acks': 1, 'dlq_acks': 0}
    second = {**projection, 'action': 'tick', 'outcome': ['submitted'],
              'queue_status': 'submitted', 'job_status': 'queued', 'job_count': 1,
              'submit_calls': 1, 'key_count': 1}
    return {'schema_version': '1.0', 'catalog_id': 'environment-scenarios-v1',
            'scenarios': [{'id': identity, 'title': 'Owned fixture', 'purpose': 'Test assessment',
                           'limitations': ['No infrastructure'], 'requirements': ['DFR-010'],
                           'steps': [{'action': 'receive'}, {'action': 'tick'}],
                           'expected': [deepcopy(projection), deepcopy(second)]}
                          for identity in ('one', 'two')]}


def observations(catalog):
    return [{'id': row['id'], 'observation': {
        'steps': deepcopy(row['expected']),
        'invariants': [{'id': name, 'passed': True} for name in runner.models.REQUIRED_INVARIANTS],
        'trace': [{'step': number, 'call': call} for number, calls in enumerate((
            ('action.receive', 'db.enqueue', 'db.receipt', 'offset.attempt', 'offset.ack'),
            ('action.tick', 'db.read.submitted', 'db.read.queued', 'http.submit',
             'sqlite.admission.receipt', 'db.project', 'db.projection.receipt'),
        )) for call in calls],
        'dependencies': {'real': list(runner.models.REAL_DEPENDENCIES),
                         'modeled': list(runner.models.MODELED_DEPENDENCIES)}}}
        for row in catalog['scenarios']]


def test_all_expected_steps_and_registered_invariants_are_required(catalog):
    result = runner.assess(catalog, observations(catalog))
    assert result['status'] == 'PASS'
    assert result['metrics']['scenario_match_rate'] == {'numerator': 2, 'denominator': 2, 'value': 1.0}
    assert result['metrics']['step_match_rate'] == {'numerator': 4, 'denominator': 4, 'value': 1.0}
    assert len(result['matrix']) == 2
    assert all(len(row['steps']) == 2 for row in result['matrix'])


@pytest.mark.parametrize('change,matched', [('scenario', 2), ('step', 3), ('all', 0)])
def test_missing_results_preserve_full_planned_denominator(catalog, change, matched):
    rows = observations(catalog)
    if change == 'scenario':
        rows.pop()
    elif change == 'step':
        rows[0]['observation']['steps'].pop()
    else:
        rows = []
    result = runner.assess(catalog, rows)
    assert result['status'] == 'INCOMPLETE'
    assert result['metrics']['planned_scenarios'] == 2
    assert result['metrics']['step_match_rate'] == {'numerator': matched, 'denominator': 4, 'value': matched / 4}
    assert sum(len(row['steps']) for row in result['matrix']) == 4


@pytest.mark.parametrize('change', ['duplicate', 'foreign', 'malformed', 'extra_step', 'error_record'])
def test_unregistered_duplicate_and_failed_records_cannot_pass(catalog, change):
    rows = observations(catalog)
    if change == 'duplicate':
        rows.append(deepcopy(rows[0]))
    elif change == 'foreign':
        rows.append({'id': 'foreign', 'observation': {}})
    elif change == 'malformed':
        rows.append(None)
    elif change == 'extra_step':
        rows[0]['observation']['steps'].append(deepcopy(rows[0]['observation']['steps'][0]))
    else:
        rows[0] = {'id': 'one', 'error_code': 'SCENARIO_EXECUTION_ERROR'}
    result = runner.assess(catalog, rows)
    assert result['status'] == 'ERROR'
    assert result['metrics']['planned_steps'] == 4


@pytest.mark.parametrize('actual', [True, 1.0, '1', None, 2])
def test_matching_status_does_not_hide_wrong_typed_state(catalog, actual):
    rows = observations(catalog)
    rows[0]['observation']['steps'][1]['job_count'] = actual
    result = runner.assess(catalog, rows)
    assert result['status'] == 'FAIL'
    assert result['matrix'][0]['steps'][1]['expected']['job_count'] == 1
    assert result['metrics']['matched_steps'] == 3


@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'unknown', 'wrong_type'])
def test_invariant_contract_is_complete_and_typed(catalog, defect):
    rows = observations(catalog)
    invariants = rows[0]['observation']['invariants']
    if defect == 'missing':
        invariants.pop()
    elif defect == 'duplicate':
        invariants[1] = deepcopy(invariants[0])
    elif defect == 'unknown':
        invariants[0]['id'] = 'unregistered'
    else:
        invariants[0]['passed'] = 1
    assert runner.assess(catalog, rows)['status'] == 'ERROR'


def test_invariant_failure_rejects_otherwise_matching_projection(catalog):
    rows = observations(catalog)
    rows[0]['observation']['invariants'][0]['passed'] = False
    result = runner.assess(catalog, rows)
    assert result['status'] == 'FAIL'
    assert result['metrics']['matched_steps'] == 4
    assert result['matrix'][0]['issues'] == ['INVARIANT_VIOLATION']


@pytest.mark.parametrize('defect', ['dependencies', 'empty_trace', 'trace_bool', 'trace_range',
                                  'nonfinite', 'unexpected_field', 'secret_object'])
def test_malformed_observation_is_error_without_including_raw_values(catalog, defect):
    rows = observations(catalog)
    value = rows[0]['observation']
    if defect == 'dependencies':
        value['dependencies']['real'] = ['claimed live Kafka']
    elif defect == 'empty_trace':
        value['trace'] = []
    elif defect == 'trace_bool':
        value['trace'][0]['step'] = True
    elif defect == 'trace_range':
        value['trace'][0]['step'] = 2
    elif defect == 'nonfinite':
        value['steps'][0]['job_count'] = float('nan')
    elif defect == 'unexpected_field':
        value['secret'] = 'secret-that-must-not-appear'
    else:
        value['steps'] = object()
    result = runner.assess(catalog, rows)
    assert result['status'] == 'ERROR'
    assert result['matrix'][0]['issues'] == ['INVALID_SCENARIO_OBSERVATION']
    assert 'secret-that-must-not-appear' not in json.dumps(result)
    assert result['metrics']['planned_steps'] == 4


@pytest.mark.parametrize('defect', ['unknown_call', 'wrong_action', 'reordered_steps',
                                  'missing_marker', 'duplicate_marker', 'missing_step', 'unexpected_fault'])
def test_trace_vocabulary_actions_and_step_order_are_closed(catalog, defect):
    rows = observations(catalog)
    trace = rows[0]['observation']['trace']
    if defect == 'unknown_call':
        trace[1]['call'] = 'unregistered.operation'
    elif defect == 'wrong_action':
        trace[1]['call'] = 'http.submit'
    elif defect == 'reordered_steps':
        trace[:] = trace[5:] + trace[:5]
    elif defect == 'missing_marker':
        trace.pop(0)
    elif defect == 'duplicate_marker':
        trace.insert(1, deepcopy(trace[0]))
    elif defect == 'missing_step':
        trace[:] = [row for row in trace if row['step'] == 0]
    else:
        trace.insert(2, {'step': 0, 'call': 'fault.db_unavailable'})
    result = runner.assess(catalog, rows)
    assert result['status'] == 'ERROR'
    assert result['matrix'][0]['issues'] == ['INVALID_SCENARIO_OBSERVATION']
    assert result['metrics']['planned_steps'] == 4


@pytest.mark.parametrize('defect', ['offset_before_receipt', 'ack_before_attempt',
                                  'missing_ack', 'missing_admission', 'admission_before_submit',
                                  'duplicate_http', 'missing_project', 'missing_projection_receipt',
                                  'missing_projection_pair', 'project_before_admission'])
def test_trace_contradiction_cannot_hide_behind_matching_steps_and_true_flags(catalog, defect):
    rows = observations(catalog)
    trace = rows[0]['observation']['trace']
    index = {row['call']: number for number, row in enumerate(trace)}
    swap = {'offset_before_receipt': ('offset.attempt', 'db.receipt'),
            'ack_before_attempt': ('offset.ack', 'offset.attempt'),
            'admission_before_submit': ('sqlite.admission.receipt', 'http.submit'),
            'project_before_admission': ('sqlite.admission.receipt', 'db.project')}
    if defect in swap:
        first, second = (index[name] for name in swap[defect])
        trace[first], trace[second] = trace[second], trace[first]
    elif defect == 'missing_ack':
        trace.pop(index['offset.ack'])
    elif defect == 'missing_admission':
        trace.pop(index['sqlite.admission.receipt'])
    elif defect == 'missing_project':
        trace.pop(index['db.project'])
    elif defect == 'missing_projection_receipt':
        trace.pop(index['db.projection.receipt'])
    elif defect == 'missing_projection_pair':
        trace[:] = [row for row in trace if row['call'] not in {'db.project', 'db.projection.receipt'}]
    else:
        trace.insert(index['http.submit'], deepcopy(trace[index['http.submit']]))
    result = runner.assess(catalog, rows)
    assert result['status'] == 'FAIL'
    assert result['metrics']['matched_steps'] == result['metrics']['planned_steps'] == 4
    assert all(row['passed'] for row in result['matrix'][0]['invariants'])
    assert result['matrix'][0]['issues'] == ['TRACE_CONTRADICTION']
    if defect == 'offset_before_receipt':
        assert result['matrix'][0]['trace_invariants'][0] == {
            'id': 'durable_receipt_before_offset', 'passed': False}


def test_actual_catalog_trace_and_expired_budget_are_checked_independently(tmp_path, monkeypatch):
    catalog = runner.load_catalog()
    monkeypatch.setattr(runner.workflow_store, 'WORKFLOW_ROOT', tmp_path / 'workflow')
    rows = []
    for definition in catalog['scenarios']:
        workspace = tmp_path / 'workflow' / 'environment-models' / definition['id']
        workspace.mkdir(parents=True)
        rows.append({'id': definition['id'], 'observation': runner.models.execute(definition, workspace)})
    baseline = runner.assess(catalog, rows)
    assert baseline['status'] == 'PASS'
    assert baseline['metrics']['matched_scenarios'] == 9
    altered = deepcopy(rows)
    budget = next(row['observation'] for row in altered if row['id'] == 'budget-reconcile')
    trace = budget['trace']
    expired = next(index for index, row in enumerate(trace) if row['call'] == 'clock.expired')
    trace.insert(expired + 1, {'step': trace[expired]['step'], 'call': 'db.defer'})
    result = runner.assess(catalog, altered)
    assert result['status'] == 'FAIL'
    broken = next(row for row in result['matrix'] if row['id'] == 'budget-reconcile')
    assert all(row['passed'] for row in broken['invariants'])
    assert broken['trace_invariants'][2] == {'id': 'no_admission_after_budget', 'passed': False}
    assert broken['issues'] == ['TRACE_CONTRADICTION']
    assert result['metrics']['matched_steps'] == baseline['metrics']['planned_steps']
    # The declared fault itself must remain visible, even when other counters match.
    budget['trace'] = [row for row in trace if row['call'] != 'fault.budget_after_admission']
    assert runner.assess(catalog, altered)['status'] == 'ERROR'


def test_three_canaries_detect_observation_corruption_without_mutating_baseline(catalog):
    rows = observations(catalog)
    saved = deepcopy(rows)
    result = runner.canaries(catalog, rows, runner.assess(catalog, rows))
    assert result['status'] == 'PASS'
    assert result['planned'] == result['detected'] == 3
    assert {row['id'] for row in result['rows']} == set(runner.CANARIES)
    assert all(row['observed_status'] != 'PASS' for row in result['rows'])
    assert rows == saved
    assert 'not live infrastructure' in result['scope']


def test_bad_baseline_does_not_claim_canary_detection(catalog):
    rows = observations(catalog)[:-1]
    result = runner.canaries(catalog, rows, runner.assess(catalog, rows))
    assert result['status'] == 'INCOMPLETE'
    assert result['detected'] == 0
    assert all(row['status'] == 'NOT_RUN' for row in result['rows'])


@pytest.fixture
def project(tmp_path, monkeypatch, catalog):
    monkeypatch.setattr(runner, 'snapshot', lambda root: {'digest': 'a' * 64})
    monkeypatch.setattr(runner, 'compare_snapshots', lambda a, b: {
        'status': 'PASS' if a == b else 'ERROR', 'added': [], 'removed': [], 'changed': []})
    monkeypatch.setattr(runner, 'environment', lambda: {'digest': 'b' * 64})
    monkeypatch.setattr(runner, 'get_qa_profile', lambda: None)
    monkeypatch.setattr(runner, 'load_catalog', lambda root: deepcopy(catalog))
    monkeypatch.setattr(runner.workflow_store, 'WORKFLOW_ROOT', tmp_path / 'var/workflow')
    saved = {row['id']: row['observation'] for row in observations(catalog)}
    def execute(scenario, workspace):
        assert workspace.is_dir() and list(workspace.iterdir()) == []
        # Evidence boundary fixture, intentionally not an actual SQLite database.
        (workspace / 'fixture.txt').write_text('closed fixture', encoding='utf-8')
        return deepcopy(saved[scenario['id']])
    monkeypatch.setattr(runner.models, 'execute', execute)
    return tmp_path, 'var/qa/environments/unit'


def read_result(project):
    root, output = project
    return json.loads((root / output / 'result.json').read_text('utf-8'))


def test_owned_run_pins_evidence_and_keeps_acceptance_separate(project):
    root, output = project
    assert runner.run(output, root=root) == 0
    result = read_result(project)
    assert result['status'] == result['execution_status'] == result['scenario_status'] == 'PASS'
    assert result['version_binding'] == result['canary_status'] == 'PASS'
    assert result['model_quality'] == result['human_calibration'] == 'NOT_MEASURED'
    assert result['live_integration'] == result['remote_ci'] == 'NOT_RUN'
    assert result['product_acceptance'] == 'INCOMPLETE'
    assert result['candidate_admission'] == 'NOT_EVALUATED'
    evidence = json.loads((root / output / 'evidence.json').read_text('utf-8'))
    assert runner.verify_files(evidence['files'], root)['status'] == 'PASS'
    workspace_pins = [row for row in evidence['files'] if row['path'].startswith(result['workspace'])]
    assert len(workspace_pins) == 2
    report = (root / output / 'report.md').read_text('utf-8')
    assert 'No infrastructure' in report and 'job_count' in report


def test_unexpected_exception_preserves_denominator_and_continues_without_secrets(project, monkeypatch):
    original, calls = runner.models.execute, []
    def execute(scenario, workspace):
        calls.append(scenario['id'])
        if scenario['id'] == 'one':
            raise RuntimeError('do-not-log-secret')
        return original(scenario, workspace)
    monkeypatch.setattr(runner.models, 'execute', execute)
    root, output = project
    assert runner.run(output, root=root) == 2
    result = read_result(project)
    assert calls == ['one', 'two']
    assert result['assessment']['metrics']['planned_steps'] == 4
    assert result['assessment']['metrics']['error_scenarios'] == 1
    assert result['execution_status'] == result['canary_status'] == 'INCOMPLETE'
    assert all('do-not-log-secret' not in path.read_text('utf-8') for path in (root / output).iterdir())


def test_missing_steps_cannot_be_reported_as_complete_execution(project, monkeypatch):
    original = runner.models.execute
    def execute(scenario, workspace):
        value = original(scenario, workspace)
        value['steps'].pop()
        return value
    monkeypatch.setattr(runner.models, 'execute', execute)
    root, output = project
    assert runner.run(output, root=root) == 3
    assert read_result(project)['execution_status'] == 'INCOMPLETE'


def test_observed_mismatch_fails_without_claiming_incomplete_execution(project, monkeypatch):
    original = runner.models.execute
    def execute(scenario, workspace):
        value = original(scenario, workspace)
        value['steps'][0]['job_count'] += 1
        return value
    monkeypatch.setattr(runner.models, 'execute', execute)
    root, output = project
    assert runner.run(output, root=root) == 1
    assert read_result(project)['execution_status'] == 'PASS'


def test_source_change_prevents_execution(project, monkeypatch):
    versions = iter([{'digest': 'before'}, {'digest': 'after'}, {'digest': 'after'}])
    monkeypatch.setattr(runner, 'snapshot', lambda root: next(versions))
    monkeypatch.setattr(runner.models, 'execute', lambda *args: pytest.fail('must not execute'))
    root, output = project
    assert runner.run(output, root=root) == 2
    assert read_result(project)['error_code'] == 'ENVIRONMENT_SOURCE_CHANGED'
    assert not (root / 'var/workflow/environment-models/unit').exists()


def test_environment_drift_invalidates_version_binding(project, monkeypatch):
    versions = iter([{'digest': 'before'}, {'digest': 'after'}])
    monkeypatch.setattr(runner, 'environment', lambda: next(versions))
    root, output = project
    assert runner.run(output, root=root) == 2
    result = read_result(project)
    assert result['scenario_status'] == 'PASS'
    assert result['version_binding'] == 'ERROR'


@pytest.mark.parametrize('target', ['assessment.json', '../../workflow/environment-models/unit/one/fixture.txt'])
def test_changed_result_or_workspace_is_not_current_evidence(project, monkeypatch, target):
    root, output = project
    calls = 0
    def environment():
        nonlocal calls
        calls += 1
        if calls == 2:
            destination = root / output / target if target == 'assessment.json' else root / 'var/workflow/environment-models/unit/one/fixture.txt'
            destination.write_text('altered', encoding='utf-8')
        return {'digest': 'b' * 64}
    monkeypatch.setattr(runner, 'environment', environment)
    assert runner.run(output, root=root) == 2
    assert read_result(project)['evidence_integrity']['status'] == 'ERROR'


def test_existing_workspace_is_rejected_before_creating_output(project):
    root, output = project
    workspace = root / 'var/workflow/environment-models/unit'
    workspace.mkdir(parents=True)
    sentinel = workspace / 'keep.txt'
    sentinel.write_text('owned by earlier run', encoding='utf-8')
    with pytest.raises(runner.EnvironmentRunError, match='ENVIRONMENT_WORKSPACE_EXISTS'):
        runner.run(output, root=root)
    assert not (root / output).exists()
    assert sentinel.read_text('utf-8') == 'owned by earlier run'


def test_existing_outputs_and_normal_database_are_not_modified(project):
    root, output = project
    normal = root / 'var/workflow/workflow.sqlite3'
    normal.parent.mkdir(parents=True)
    normal.write_bytes(b'user database marker')
    assert runner.run(output, root=root) == 0
    saved = (root / output / 'result.json').read_bytes()
    with pytest.raises(runner.EnvironmentRunError, match='ENVIRONMENT_OUTPUT_EXISTS'):
        runner.run(output, root=root)
    assert (root / output / 'result.json').read_bytes() == saved
    assert normal.read_bytes() == b'user database marker'


@pytest.mark.parametrize('output', ['var/other/unit', 'var/qa/environments',
                                  'var/qa/environments/unit/nested', '../outside',
                                  'var/qa/environments/' + 'a' * 65])
def test_output_scope_is_fixed_and_exclusive(tmp_path, output):
    with pytest.raises(ValueError):
        runner.output_path(output, tmp_path)


def test_foreign_workflow_root_is_rejected_before_output(project, monkeypatch):
    root, output = project
    monkeypatch.setattr(runner.workflow_store, 'WORKFLOW_ROOT', root / 'other')
    with pytest.raises(runner.EnvironmentRunError, match='ENVIRONMENT_WORKFLOW_ROOT'):
        runner.run(output, root=root)
    assert not (root / output).exists()


def test_nested_profile_is_not_allowed_to_redirect_workspace(project, monkeypatch):
    root, output = project
    monkeypatch.setattr(runner, 'get_qa_profile', lambda: object())
    with pytest.raises(runner.EnvironmentRunError, match='ENVIRONMENT_NESTED_PROFILE_UNSUPPORTED'):
        runner.run(output, root=root)
    assert not (root / output).exists()


def test_catalog_duplicate_keys_and_hardlinks_are_rejected(tmp_path, catalog):
    path = tmp_path / runner.CATALOG
    path.parent.mkdir(parents=True)
    path.write_text('{"schema_version":"1.0","schema_version":"1.0"}', encoding='utf-8')
    with pytest.raises(ValueError, match='DUPLICATE_JSON_KEY'):
        runner.load_catalog(tmp_path)
    path.write_text(json.dumps(catalog), encoding='utf-8')
    (tmp_path / 'linked.json').hardlink_to(path)
    with pytest.raises(ValueError, match='INVALID_QA_PATH'):
        runner.load_catalog(tmp_path)


def test_workspace_hardlink_is_not_pinned_as_owned_evidence(tmp_path):
    workspace = tmp_path / 'var/workflow/environment-models/test/one'
    workspace.mkdir(parents=True)
    source = tmp_path / 'user.db'
    source.write_bytes(b'user data')
    (workspace / 'model.sqlite3').hardlink_to(source)
    with pytest.raises(ValueError, match='INVALID_QA_PATH'):
        runner.workspace_files(workspace, tmp_path)
    assert source.read_bytes() == b'user data'


def test_empty_workspace_cannot_claim_evidence(tmp_path):
    with pytest.raises(runner.EnvironmentRunError, match='ENVIRONMENT_WORKSPACE_EMPTY'):
        runner.workspace_files(tmp_path, tmp_path)


def test_cli_masks_unexpected_error(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['run_environments.py', '--output-dir', 'var/qa/environments/unit'])
    def failed(*args):
        raise RuntimeError('raw secret must not be printed')
    monkeypatch.setattr(runner, 'run', failed)
    assert runner.main() == 2
    response = json.loads(capsys.readouterr().out)
    assert response['error_code'] == 'ENVIRONMENT_OUTPUT_ERROR'
