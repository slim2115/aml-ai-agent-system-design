"""The owned runner must preserve incomplete or failed evidence on error paths."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from qa.requirements import runner
from qa.requirements.catalog import canonical_digest, load_catalog, validate_catalog


POSITIVE = 'tests/dark_factory/test_sample.py::test_positive'
NEGATIVE = 'tests/dark_factory/test_sample.py::test_negative'


@pytest.fixture
def project(tmp_path, monkeypatch):
    policy = {'purpose': 'synthetic runner controls'}
    document = {
        'schema_version': '1.0', 'catalog_id': 'dark-factory-requirements-v1', 'version': '1.0',
        'title': 'Synthetic runner requirements', 'scope': 'Runner lifecycle controls only.',
        'policy': {'path': 'qa/strategy/quality-policy.json', 'canonical_sha256': canonical_digest(policy)},
        'requirements': [{
            'id': 'DFR-001', 'version': '1.0', 'title': 'Expected approval boundary',
            'statement': 'An approved version can be exported; an unapproved version cannot.',
            'risk': 'Unapproved export.', 'priority': 'P0', 'owner': 'qa_automation',
            'scope': 'Synthetic lifecycle observations.', 'preconditions': ['Synthetic report exists.'],
            'allowed_outcomes': ['allowed', 'blocked'],
            'expectation_source': {'path': 'qa/strategy/quality-policy.json', 'reference': 'purpose'},
            'limitations': ['Does not exercise the real export service.'],
            'scenarios': [
                {'id': 'DFR-001-POS', 'kind': 'positive', 'given': 'Approved version.',
                 'action': 'Export.', 'expected_outcome': 'allowed', 'test_selectors': [POSITIVE]},
                {'id': 'DFR-001-NEG', 'kind': 'negative', 'given': 'Unapproved version.',
                 'action': 'Export.', 'expected_outcome': 'blocked', 'test_selectors': [NEGATIVE]},
            ],
        }],
    }
    files = {
        'qa/requirements/catalog-v1.json': json.dumps(document),
        'qa/strategy/quality-policy.json': json.dumps(policy),
        'pyproject.toml': '[tool.pytest.ini_options]\ntestpaths = ["tests/dark_factory"]\n',
        'tests/dark_factory/test_sample.py': 'def test_positive():\n    pass\ndef test_negative():\n    pass\n',
    }
    for name in ('tests/conftest.py', 'qa/requirements/__init__.py', 'qa/requirements/catalog.py',
                 'qa/requirements/execution.py', 'qa/requirements/runner.py',
                 'qa/tools/check_requirements.py', 'qa/reporting.py', 'src/qa_profile.py'):
        files[name] = '# Synthetic snapshot source; never imported.\n'
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    original_output = runner.output_path
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'output_path', lambda value: original_output(value, root=tmp_path))
    monkeypatch.setattr(runner, 'load_catalog', lambda: load_catalog(tmp_path))
    monkeypatch.setattr(runner, 'validate_catalog', lambda value: validate_catalog(value, tmp_path))
    calls = []

    def successful_pytest(arguments, plugins):
        calls.append(arguments)
        recorder = plugins[0]
        recorder.pytest_collection_finish(SimpleNamespace(
            items=[SimpleNamespace(nodeid=node) for node in (POSITIVE, NEGATIVE)]))
        for node in (POSITIVE, NEGATIVE):
            for phase in ('setup', 'call', 'teardown'):
                recorder.pytest_runtest_logreport(SimpleNamespace(nodeid=node, when=phase, outcome='passed'))
        return 0

    monkeypatch.setattr(pytest, 'main', successful_pytest)
    return SimpleNamespace(root=tmp_path, calls=calls, execute=successful_pytest,
                           output='var/qa/requirements/check-01')


def result(project):
    return json.loads((project.root / project.output / 'result.json').read_text('utf-8'))


def test_validate_writes_incomplete_execution_without_calling_pytest(project):
    assert runner.run('validate', project.output) == 0
    report = result(project)
    assert project.calls == []
    assert report['catalog_validation'] == 'PASS'
    assert report['execution_status'] == 'NOT_RUN'
    assert report['requirements_status'] == report['product_acceptance'] == 'INCOMPLETE'
    assert not (project.root / project.output / 'observations.json').exists()


def test_owned_success_preserves_mechanical_scope_and_new_output(project):
    assert runner.run('run', project.output) == 0
    report = result(project)
    assert len(project.calls) == 1
    assert report['execution_status'] == report['requirements_status'] == 'PASS'
    assert report['product_acceptance'] == 'INCOMPLETE'
    assert report['candidate_admission'] == 'NOT_EVALUATED'
    assert report['confirmed_requirements'] == 1
    original = (project.root / project.output / 'result.json').read_bytes()
    with pytest.raises(ValueError):
        runner.run('run', project.output)
    assert len(project.calls) == 1
    assert (project.root / project.output / 'result.json').read_bytes() == original


def test_context_exit_error_after_passing_pytest_cannot_publish_success(project, monkeypatch):
    original_environment = runner.pytest_environment

    @contextmanager
    def failing_exit(directory):
        with original_environment(directory):
            yield
        raise RuntimeError('Synthetic context finalization failure')

    monkeypatch.setattr(runner, 'pytest_environment', failing_exit)
    assert runner.run('run', project.output) == 2
    report = result(project)
    assert len(project.calls) == 1
    assert report['runner_error_type'] == 'RuntimeError'
    assert report['execution_status'] == 'ERROR'
    assert report['requirements_status'] == 'INCOMPLETE'
    observations = json.loads((project.root / project.output / 'observations.json').read_text('utf-8'))
    assert observations['exit_code'] == 3


@pytest.mark.parametrize('changed_source', ['test', 'catalogue'])
def test_change_after_validation_is_rejected_before_execution(project, monkeypatch, changed_source):
    def validate_then_change(document):
        checked = validate_catalog(document, project.root)
        if changed_source == 'test':
            path = project.root / POSITIVE.split('::')[0]
            path.write_text(path.read_text('utf-8') + '\n# Changed after validation.\n', encoding='utf-8')
        else:
            changed = deepcopy(document)
            changed['title'] = 'Changed after validation'
            (project.root / 'qa/requirements/catalog-v1.json').write_text(json.dumps(changed), encoding='utf-8')
        return checked

    monkeypatch.setattr(runner, 'validate_catalog', validate_then_change)
    assert runner.run('run', project.output) == 2
    assert project.calls == []
    report = result(project)
    assert report['requirements_status'] == 'INCOMPLETE'
    assert report['execution_status'] != 'PASS'
    assert 'error_code' in report


def test_changed_source_during_successful_execution_invalidates_result(project, monkeypatch):
    def execute_and_change(arguments, plugins):
        code = project.execute(arguments, plugins)
        (project.root / 'tests/conftest.py').write_text('# Changed during execution.\n', encoding='utf-8')
        return code

    monkeypatch.setattr(pytest, 'main', execute_and_change)
    assert runner.run('run', project.output) == 2
    report = result(project)
    assert report['execution_status'] == 'ERROR'
    assert report['requirements_status'] == 'INCOMPLETE'


def test_test_failure_is_retained_as_failure(project, monkeypatch):
    def failed_pytest(arguments, plugins):
        project.execute(arguments, plugins)
        recorder = plugins[0]
        next(row for row in recorder.reports if row['nodeid'] == NEGATIVE and row['when'] == 'call')['outcome'] = 'failed'
        return 1

    monkeypatch.setattr(pytest, 'main', failed_pytest)
    assert runner.run('run', project.output) == 1
    report = result(project)
    assert report['execution_status'] == report['requirements_status'] == 'FAIL'
    assert report['product_acceptance'] == 'INCOMPLETE'


@pytest.mark.parametrize('output', [
    '../outside', 'var/qa/other/run', 'var/qa/requirements',
    'var/qa/requirements/run/nested', 'var/qa/requirements/../run', 'var/qa/requirements/bad name',
])
def test_output_cannot_escape_unique_requirement_run_scope(tmp_path, output):
    with pytest.raises(ValueError):
        runner.output_path(output, root=tmp_path)


def test_pytest_environment_clears_filters_and_restores_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setenv('PYTEST_ADDOPTS', '-k only_easy_cases')
    monkeypatch.setenv('PYTEST_PLUGINS', 'unexpected_plugin')
    monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '0')
    before = {key: os.environ.get(key) for key in (
        'PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD', 'TEMP', 'TMP')}
    cwd = Path.cwd()
    directory = tmp_path / 'run'
    directory.mkdir()
    with pytest.raises(RuntimeError):
        with runner.pytest_environment(directory):
            assert Path.cwd() == tmp_path
            assert 'PYTEST_ADDOPTS' not in os.environ
            assert 'PYTEST_PLUGINS' not in os.environ
            assert os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] == '1'
            assert Path(os.environ['TEMP']) == directory / 'tmp'
            raise RuntimeError('Synthetic interruption')
    assert Path.cwd() == cwd
    assert {key: os.environ.get(key) for key in before} == before
