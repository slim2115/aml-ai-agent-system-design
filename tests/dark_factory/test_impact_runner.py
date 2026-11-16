"""The CLI saves a declared plan without running tests or accepting a product."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from qa.impact import runner
from qa.requirements.catalog import CATALOG_PATH, canonical_digest, load_catalog, validate_catalog


ROOT = Path(__file__).resolve().parents[2]


def declaration(path='src/runtime/retrieval.py'):
    return {'schema_version': '1.0', 'changes': [{'status': 'M', 'path': path}], 'model_changes': []}


@pytest.fixture
def project(tmp_path):
    catalog = load_catalog(ROOT)
    validation = validate_catalog(catalog, ROOT)
    names = set(validation['source_hashes']) | {CATALOG_PATH, runner.MAP_PATH}
    # Real catalogue validation reads these files but must never import the
    # copied tests or their dependencies. Runtime service files are not copied.
    for name in names:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    return SimpleNamespace(root=tmp_path, output='var/qa/impact/check-01', catalog=catalog)


def report(project):
    return json.loads((project.root / project.output / 'result.json').read_text('utf-8'))


def wire_main(project, monkeypatch, arguments):
    original_run, original_read = runner.run, runner.read_json
    monkeypatch.setattr(runner, 'run', lambda changes, output: original_run(changes, output, root=project.root))
    monkeypatch.setattr(runner, 'read_json', lambda name, root=project.root: original_read(name, root=root))
    monkeypatch.setattr(sys, 'argv', ['plan_changes.py', *arguments, '--output-dir', project.output])


def assert_not_executed(result):
    assert result['execution_status'] == 'NOT_RUN'
    assert result['requirements_status'] == result['product_acceptance'] == 'INCOMPLETE'
    assert result['candidate_admission'] == 'NOT_EVALUATED'
    assert all(profile['status'] == 'NOT_RUN' for profile in result.get('profiles', []))


def test_owned_ready_plan_saves_inputs_and_never_starts_checks_or_processes(project, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Impact CLI must not start tests, models or Git')

    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(pytest, 'main', forbidden)
    assert runner.run(declaration(), project.output, root=project.root) == 0
    result = report(project)
    assert result['planning_status'] == 'READY'
    assert_not_executed(result)
    directory = project.root / project.output
    assert {path.name for path in directory.iterdir()} == {
        'started.json', 'changes.json', 'catalog.json', 'map.json',
        'catalog-validation.json', 'result.json', 'plan.md',
    }
    assert json.loads((directory / 'changes.json').read_text('utf-8')) == declaration()
    assert 'NOT_RUN' in (directory / 'plan.md').read_text('utf-8')


def test_unknown_input_is_saved_as_incomplete_with_full_fallback(project):
    assert runner.run(declaration('src/unknown_component/new.py'), project.output, root=project.root) == 2
    result = report(project)
    assert result['planning_status'] == 'INCOMPLETE'
    assert result['unknown_paths'] == ['src/unknown_component/new.py']
    assert {row['id'] for row in result['requirements']} == {row['id'] for row in project.catalog['requirements']}
    assert result['mandatory_full_regression'] is True
    assert_not_executed(result)


def test_catalogue_pin_mismatch_is_an_error_not_a_ready_plan(project):
    path = project.root / runner.MAP_PATH
    mapping = json.loads(path.read_text('utf-8'))
    mapping['catalog_sha256'] = '0' * 64
    path.write_text(json.dumps(mapping), encoding='utf-8')
    assert runner.run(declaration(), project.output, root=project.root) == 2
    result = report(project)
    assert result['planning_status'] == 'ERROR'
    assert result['error_code'] == 'IMPACT_CATALOG_CHANGED'
    assert_not_executed(result)


def test_malformed_catalogue_with_updated_map_pin_is_saved_as_error(project):
    document = project.catalog
    del document['requirements'][0]['id']
    (project.root / CATALOG_PATH).write_text(json.dumps(document), encoding='utf-8')
    mapping_path = project.root / runner.MAP_PATH
    mapping = json.loads(mapping_path.read_text('utf-8'))
    mapping['catalog_sha256'] = canonical_digest(document)
    mapping_path.write_text(json.dumps(mapping), encoding='utf-8')
    assert runner.run(declaration(), project.output, root=project.root) == 2
    result = report(project)
    assert result['planning_status'] == 'ERROR'
    assert_not_executed(result)


@pytest.mark.parametrize('raw', [
    '{"schema_version":"1.0","schema_version":"1.0","changes":[],"model_changes":[]}',
    '{"schema_version":"1.0","changes":[],"model_changes":[],"value":NaN}',
])
def test_ambiguous_json_file_is_rejected_before_planning(project, raw):
    source = project.root / 'changes.json'
    source.write_text(raw, encoding='utf-8')
    with pytest.raises(ValueError):
        runner.read_json('changes.json', root=project.root)
    assert not (project.root / project.output).exists()


@pytest.mark.parametrize('path', ['../outside.json', 'qa/../changes.json'])
def test_json_input_must_stay_inside_project(project, path):
    with pytest.raises(ValueError):
        runner.read_json(path, root=project.root)


@pytest.mark.parametrize('path', ['var/qa/other/run', 'var/qa/impact', 'var/qa/impact/run/nested', '../outside'])
def test_output_is_restricted_to_one_new_impact_directory(project, path):
    with pytest.raises(ValueError):
        runner.output_path(path, root=project.root)


def test_second_run_cannot_overwrite_the_original_plan(project):
    assert runner.run(declaration(), project.output, root=project.root) == 0
    directory = project.root / project.output
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    with pytest.raises(ValueError, match='IMPACT_OUTPUT_EXISTS'):
        runner.run(declaration('src/unknown_component/new.py'), project.output, root=project.root)
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_file_declaration_preserves_both_sides_of_rename(project):
    document = {'schema_version': '1.0', 'changes': [{
        'status': 'R', 'path': 'src/workflow/remote.py', 'previous_path': 'src/workflow/delivery.py',
    }], 'model_changes': []}
    source = project.root / 'rename.json'
    source.write_text(json.dumps(document), encoding='utf-8')
    assert runner.run(runner.read_json('rename.json', root=project.root), project.output, root=project.root) == 0
    recorded = json.loads((project.root / project.output / 'changes.json').read_text('utf-8'))
    assert recorded == document
    assert 'DFR-007' in {row['id'] for row in report(project)['requirements']}
    assert_not_executed(report(project))


def assert_catalogue_source_fallback(result, project, error):
    assert result['planning_status'] == 'INCOMPLETE'
    assert result['catalog_validation'] == 'INCOMPLETE'
    assert result['catalog_validation_error'] == error
    assert {row['id'] for row in result['requirements']} == {row['id'] for row in project.catalog['requirements']}
    assert result['mandatory_full_regression'] is True
    assert {row['id'] for row in result['profiles']} == {
        'full_regression', 'requirements', 'ui_cli', 'browser', 'retrieval',
        'llm', 'judge', 'live_integration', 'documentation', 'mapping_review',
    }
    assert_not_executed(result)


@pytest.mark.parametrize('path', [
    'tests/workflow/test_controller.py', 'docs/rebuild/retrieval-v1.md',
])
@pytest.mark.parametrize('status', ['D', 'R'])
def test_physically_removed_catalogue_source_keeps_its_declared_impact(project, path, status):
    source = project.root / path
    assert source.resolve().is_relative_to(project.root.resolve())
    if status == 'R':
        destination_name = 'unmapped-change/' + source.name
        destination = project.root / destination_name
        assert destination.resolve().is_relative_to(project.root.resolve())
        destination.parent.mkdir()
        source.rename(destination)
        change = {'status': 'R', 'path': destination_name, 'previous_path': path}
    else:
        source.unlink()
        change = {'status': 'D', 'path': path}
    assert not source.exists()
    document = {'schema_version': '1.0', 'changes': [change], 'model_changes': []}
    assert runner.run(document, project.output, root=project.root) == 2
    result = report(project)
    assert_catalogue_source_fallback(result, project, 'SOURCE_NOT_FOUND')
    assert any(row['input'] == path and row['catalog_requirements'] for row in result['path_impacts'])
    assert any(reason['input'] == path for row in result['requirements'] for reason in row['reasons'])
    if status == 'R':
        assert destination_name in result['unknown_paths']
    recorded = json.loads((project.root / project.output / 'changes.json').read_text('utf-8'))
    assert recorded == document


@pytest.mark.parametrize('path', [
    'qa/strategy/quality-policy.json', 'docs/rebuild/retrieval-v1.md',
])
def test_missing_catalogue_source_cannot_be_hidden_by_an_unrelated_declared_change(project, path):
    source = project.root / path
    assert source.resolve().is_relative_to(project.root.resolve())
    source.unlink()
    assert runner.run(declaration('src/ui/app.py'), project.output, root=project.root) == 2
    assert_catalogue_source_fallback(report(project), project, 'SOURCE_NOT_FOUND')


def test_removed_mapped_test_definition_preserves_plan_as_incomplete(project):
    source = project.root / 'tests/workflow/test_controller.py'
    text = source.read_text('utf-8')
    name = 'test_admission_requires_readiness_and_accepted_request_is_replayed_without_rerun'
    assert 'def ' + name + '(' in text
    source.write_text(text.replace('def ' + name + '(', 'def removed_definition_for_test('), encoding='utf-8')
    assert runner.run(declaration('tests/workflow/test_controller.py'), project.output, root=project.root) == 2
    assert_catalogue_source_fallback(report(project), project, 'TEST_DEFINITION_NOT_FOUND')


def test_changed_policy_identity_remains_an_error_not_missing_source_fallback(project):
    source = project.root / 'qa/strategy/quality-policy.json'
    policy = json.loads(source.read_text('utf-8'))
    policy['scope'] = 'A changed policy requiring explicit reconciliation.'
    source.write_text(json.dumps(policy), encoding='utf-8')
    assert runner.run(declaration('qa/strategy/quality-policy.json'), project.output, root=project.root) == 2
    result = report(project)
    assert result['planning_status'] == 'ERROR'
    assert_not_executed(result)


def test_main_accepts_explicit_model_change_without_changed_files(project, monkeypatch):
    wire_main(project, monkeypatch, ['--model-changed', 'llm'])
    assert runner.main() == 0
    result = report(project)
    assert result['planning_status'] == 'READY'
    assert 'llm' in {row['id'] for row in result['profiles']}
    assert_not_executed(result)


def test_main_rejects_duplicate_json_keys_without_creating_a_run(project, monkeypatch, capsys):
    (project.root / 'bad.json').write_text('{"changes":[],"changes":[]}', encoding='utf-8')
    wire_main(project, monkeypatch, ['--changes-file', 'bad.json'])
    assert runner.main() == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed['planning_status'] == 'ERROR'
    assert printed['execution_status'] == 'NOT_RUN'
    assert not (project.root / project.output).exists()


def test_write_failure_after_building_a_ready_plan_cannot_return_success(project, monkeypatch):
    original = runner.write_json

    def fail_copy(directory, name, value, root):
        if name == 'map.json':
            raise OSError('Synthetic write failure')
        return original(directory, name, value, root)

    monkeypatch.setattr(runner, 'write_json', fail_copy)
    assert runner.run(declaration(), project.output, root=project.root) == 2
    result = report(project)
    assert result['planning_status'] == 'ERROR'
    assert result['error_code'] == 'OSError'
    assert_not_executed(result)


def test_failed_final_json_write_returns_cli_error_and_leaves_no_finished_result(project, monkeypatch, capsys):
    original = runner.write_json

    def fail_result(directory, name, value, root):
        if name == 'result.json':
            raise OSError('Synthetic final result write failure')
        return original(directory, name, value, root)

    monkeypatch.setattr(runner, 'write_json', fail_result)
    wire_main(project, monkeypatch, ['--changed', 'src/runtime/retrieval.py'])
    assert runner.main() == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed['planning_status'] == 'ERROR'
    assert not (project.root / project.output / 'result.json').exists()
    started = json.loads((project.root / project.output / 'started.json').read_text('utf-8'))
    assert started['status'] == 'INCOMPLETE'
