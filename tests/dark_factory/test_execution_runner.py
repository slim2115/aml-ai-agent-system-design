"""Orchestration boundaries; owned real processes are qualified separately."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qa.execution import runner
from qa.requirements.catalog import canonical_digest


def declaration():
    return {'schema_version': '1.0', 'changes': [{'status': 'M', 'path': 'src/ui/app.py'}], 'model_changes': []}


@pytest.mark.parametrize('path', [
    'qa/execution/runner.py',
    'tests/dark_factory/test_execution_adapters.py',
    'tests/dark_factory/test_execution_processes.py',
    'tests/dark_factory/test_execution_provenance.py',
    'tests/dark_factory/test_execution_runner.py',
])
def test_executor_changes_preserve_model_and_live_profiles_in_real_map(path):
    changes = {'schema_version': '1.0', 'changes': [{'status': 'M', 'path': path}], 'model_changes': []}
    plan, catalog, _, validation = runner.prepare_plan(changes, runner.ROOT)
    assert validation['status'] == 'PASS'
    assert plan['planning_status'] == 'READY'
    assert plan['unknown_paths'] == []
    assert {row['id'] for row in plan['requirements']} == {row['id'] for row in catalog['requirements']}
    assert {'full_regression', 'requirements', 'ui_cli', 'browser', 'retrieval', 'llm', 'judge',
            'live_integration', 'mapping_review'} <= {row['id'] for row in plan['profiles']}
    assert plan['execution_status'] == 'NOT_RUN'


@pytest.fixture
def project(tmp_path, monkeypatch):
    source = {'schema_version': '1.0', 'files': {'src/example.py': 'a' * 64}, 'scope': 'test'}
    source['digest'] = canonical_digest(source)
    plan = {'planning_status': 'READY', 'profiles': [{'id': name} for name in runner.BASE_PROFILES],
            'catalog_sha256': 'c' * 64, 'map_sha256': 'd' * 64, 'change_set_sha256': 'e' * 64}
    specs = [{'id': name, 'output_dir': 'var/qa/execution/unit/' + name,
              'argv_tail': ['-I', '-B', 'scripts/' + name + '.py']} for name in runner.BASE_PROFILES]
    monkeypatch.setattr(runner, 'snapshot', lambda root: source)
    monkeypatch.setattr(runner, 'environment', lambda: {'digest': 'b' * 64})
    monkeypatch.setattr(runner, 'compare_snapshots', lambda left, right: {
        'status': 'PASS' if left == right else 'ERROR', 'added': [], 'removed': [], 'changed': []})
    monkeypatch.setattr(runner, 'prepare_plan', lambda changes, root: (plan, {}, {}, {'status': 'PASS'}))
    monkeypatch.setattr(runner, 'specs', lambda root, run_id: specs)
    calls = []

    def process(argv, root, environment, log_path, timeout):
        calls.append(argv)
        log_path.write_text('owned output\n', encoding='utf-8')
        return {'returncode': 0, 'timed_out': False, 'interrupted': False, 'cleanup_status': 'PASS', 'elapsed_seconds': 0.01}

    monkeypatch.setattr(runner, 'run_owned', process)
    monkeypatch.setattr(runner, 'assess_adapter', lambda spec, root, code: {
        'id': spec['id'], 'status': 'PASS', 'issues': [], 'metrics': {}, 'files': []})
    return SimpleNamespace(root=tmp_path, output='var/qa/execution/unit', plan=plan, calls=calls, source=source, specs=specs)


def result(project):
    return json.loads((project.root / project.output / 'result.json').read_text('utf-8'))


def test_all_owned_deterministic_profiles_run_and_bind_their_results(project):
    assert runner.run(declaration(), project.output, root=project.root) == 0
    report = result(project)
    assert len(project.calls) == 4
    assert report['deterministic_status'] == report['version_binding'] == report['plan_fulfillment'] == 'PASS'
    assert all(row['source_digest'] == report['source_digest'] for row in report['profiles'])
    assert report['product_acceptance'] == 'INCOMPLETE'
    assert report['candidate_admission'] == 'NOT_EVALUATED'
    assert report['model_identity'] == 'NOT_MEASURED'
    assert report['historical_judge'] == 'NOT_REASSESSED'
    assert (project.root / project.output / 'evidence.json').is_file()


@pytest.mark.parametrize('profile', ['llm', 'judge', 'retrieval', 'browser', 'live_integration', 'documentation', 'mapping_review'])
def test_missing_measurements_do_not_inherit_success_from_deterministic_profiles(project, profile):
    project.plan['profiles'].append({'id': profile})
    assert runner.run(declaration(), project.output, root=project.root) == 3
    report = result(project)
    assert report['deterministic_status'] == 'PASS'
    assert report['plan_fulfillment'] == 'INCOMPLETE'
    assert report['pending_profiles'] == [profile]
    assert next(row for row in report['profiles'] if row['id'] == profile)['status'] == 'NOT_RUN'


def test_incomplete_plan_never_becomes_fulfilled(project):
    project.plan['planning_status'] = 'INCOMPLETE'
    assert runner.run(declaration(), project.output, root=project.root) == 3
    assert result(project)['plan_fulfillment'] == 'INCOMPLETE'


def test_existing_child_output_blocks_all_execution_and_is_not_read_as_evidence(project):
    project.specs[-1]['output_dir'] = 'var/qa/requirements/already-owned'
    destination = project.root / project.specs[-1]['output_dir']
    destination.mkdir(parents=True)
    (destination / 'result.json').write_text('{"status":"PASS"}', encoding='utf-8')
    assert runner.run(declaration(), project.output, root=project.root) == 2
    assert not project.calls
    assert result(project)['error_code'] == 'EXECUTION_CHILD_OUTPUT_EXISTS'
    assert (destination / 'result.json').read_text('utf-8') == '{"status":"PASS"}'


def test_existing_run_cannot_be_overwritten(project):
    assert runner.run(declaration(), project.output, root=project.root) == 0
    saved = (project.root / project.output / 'result.json').read_bytes()
    with pytest.raises(runner.ExecutionError, match='EXECUTION_OUTPUT_EXISTS'):
        runner.run(declaration(), project.output, root=project.root)
    assert (project.root / project.output / 'result.json').read_bytes() == saved


@pytest.mark.parametrize('path', ['var/checks/new', 'var/qa/execution', 'var/qa/execution/one/two',
                                 '../outside', 'var/qa/execution/bad:name', 'var/qa/execution/' + 'x' * 65])
def test_output_cannot_escape_owned_namespace(project, path):
    with pytest.raises(ValueError):
        runner.output_path(path, project.root)


@pytest.mark.parametrize('timeout', [0, -1, 7201, True, 1.5])
def test_timeout_is_bounded_before_creating_evidence(project, timeout):
    with pytest.raises(runner.ExecutionError):
        runner.run(declaration(), project.output, root=project.root, timeout_seconds=timeout)
    assert not (project.root / project.output).exists()


def test_test_failure_remains_visible_when_other_profiles_are_unmeasured(project, monkeypatch):
    project.plan['profiles'].append({'id': 'llm'})
    monkeypatch.setattr(runner, 'assess_adapter', lambda spec, root, code: {
        'id': spec['id'], 'status': 'FAIL' if spec['id'] == 'requirements' else 'PASS',
        'issues': [], 'metrics': {}, 'files': []})
    assert runner.run(declaration(), project.output, root=project.root) == 1
    report = result(project)
    assert report['observed_failed_profiles'] == ['requirements']
    assert report['status'] == report['plan_fulfillment'] == 'FAIL'
    assert report['pending_profiles'] == ['llm']


@pytest.mark.parametrize('failure', ['timed_out', 'interrupted', 'cleanup_status'])
def test_process_boundary_failure_stops_remaining_children(project, monkeypatch, failure):
    def process(argv, root, environment, log_path, timeout):
        project.calls.append(argv)
        log_path.write_text('child stopped', encoding='utf-8')
        values = {'returncode': 0, 'timed_out': False, 'interrupted': False, 'cleanup_status': 'PASS', 'elapsed_seconds': 0.01}
        values[failure] = 'ERROR' if failure == 'cleanup_status' else True
        return values
    monkeypatch.setattr(runner, 'run_owned', process)
    assert runner.run(declaration(), project.output, root=project.root) == 2
    assert len(project.calls) == 1
    report = result(project)
    assert report['profiles'][0]['status'] == 'ERROR'
    assert report['profiles'][0]['observed_status'] == 'PASS'
    assert report['profiles'][1]['status'] == 'NOT_RUN'


def test_mutation_after_capture_prevents_first_child(project, monkeypatch):
    count = 0
    def snapshot(root):
        nonlocal count
        count += 1
        return project.source if count == 1 else dict(project.source, digest='changed')
    monkeypatch.setattr(runner, 'snapshot', snapshot)
    assert runner.run(declaration(), project.output, root=project.root) == 2
    assert not project.calls
    assert result(project)['source_stability']['status'] == 'ERROR'


def test_environment_mutation_invalidates_binding_after_successful_tests(project, monkeypatch):
    values = iter([{'digest': 'b' * 64}, {'digest': 'f' * 64}])
    monkeypatch.setattr(runner, 'environment', lambda: next(values))
    assert runner.run(declaration(), project.output, root=project.root) == 2
    report = result(project)
    assert report['deterministic_status'] == 'PASS'
    assert report['version_binding'] == 'ERROR'


def test_saved_log_mutation_is_detected_before_final_status(project, monkeypatch):
    calls = 0
    def assess(spec, root, code):
        nonlocal calls
        calls += 1
        if calls == 4:
            (root / project.output / 'full_regression.log').write_text('replaced', encoding='utf-8')
        return {'id': spec['id'], 'status': 'PASS', 'issues': [], 'metrics': {}, 'files': []}
    monkeypatch.setattr(runner, 'assess_adapter', assess)
    assert runner.run(declaration(), project.output, root=project.root) == 2
    assert result(project)['evidence_integrity']['changed_or_missing'] == [project.output + '/full_regression.log']


def test_missing_catalogue_reference_saves_incomplete_plan_without_running(project, monkeypatch):
    monkeypatch.setattr(runner, 'prepare_plan', lambda changes, root: (project.plan, {}, {}, {'status': 'INCOMPLETE'}))
    assert runner.run(declaration(), project.output, root=project.root) == 2
    assert not project.calls
    assert result(project)['error_code'] == 'EXECUTION_CATALOG_INCOMPLETE'


def test_interruption_still_saves_unfinished_status(project, monkeypatch):
    monkeypatch.setattr(runner, 'run_owned', lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert runner.run(declaration(), project.output, root=project.root) == 2
    assert result(project)['error_code'] == 'INTERRUPTED'


@pytest.mark.parametrize('stale_field', ['catalog_sha256', 'map_sha256', 'change_set_sha256'])
def test_stale_plan_is_rejected_instead_of_silent_reuse(project, stale_field):
    directory = project.root / 'var/qa/impact/old'
    directory.mkdir(parents=True)
    (directory / 'changes.json').write_text(json.dumps(declaration()), encoding='utf-8')
    saved = dict(project.plan)
    saved[stale_field] = '0' * 64
    (directory / 'result.json').write_text(json.dumps(saved), encoding='utf-8')
    with pytest.raises(runner.ExecutionError, match='STALE_IMPACT_PLAN'):
        runner.saved_declaration('var/qa/impact/old', project.root)


def test_saved_commands_are_never_trusted(project):
    directory = project.root / 'var/qa/impact/old'
    directory.mkdir(parents=True)
    (directory / 'changes.json').write_text(json.dumps(declaration()), encoding='utf-8')
    saved = dict(project.plan, profiles=[{'id': 'full_regression', 'entrypoint': 'delete_everything.exe'}])
    (directory / 'result.json').write_text(json.dumps(saved), encoding='utf-8')
    assert runner.saved_declaration('var/qa/impact/old', project.root) == declaration()


def test_evidence_recheck_rejects_replaced_or_missing_file(tmp_path):
    path = tmp_path / 'owned.log'
    path.write_text('original', encoding='utf-8')
    rows = [runner.pin_file(path, tmp_path)]
    assert runner.verify_files(rows, tmp_path)['status'] == 'PASS'
    path.write_text('different', encoding='utf-8')
    assert runner.verify_files(rows, tmp_path)['status'] == 'ERROR'
    path.unlink()
    assert runner.verify_files(rows, tmp_path)['status'] == 'ERROR'
