"""CI contracts and owned browser lifecycle; no services, browser or network."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from qa.tools import run_ci_browser as runner
from qa.tools import browser_stand, environment


@pytest.fixture
def browser_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    python = tmp_path / '.venv/Scripts/python.exe'
    python.parent.mkdir(parents=True)
    python.write_bytes(b'fake executable; never invoked')
    profile = SimpleNamespace(run_id='qa-123456789abc', base=tmp_path / 'var/qa/runs/qa-123456789abc')
    profile.validate = lambda: profile
    profile.base.mkdir(parents=True)
    monkeypatch.setattr(runner, 'QAProfile', lambda *args: profile)
    events = []
    monkeypatch.setattr(browser_stand, 'prepare', lambda: events.append('prepare') or {'run_id': profile.run_id})
    monkeypatch.setattr(browser_stand, 'start', lambda item: events.append('start'))
    monkeypatch.setattr(browser_stand, 'stop', lambda item: events.append('stop') or {'status': 'stopped'})
    monkeypatch.setattr(environment, 'child_environment', lambda item: {'SYNTHETIC': 'yes'})
    measured = {'status': 'PASS', 'run_id': profile.run_id, 'profile': 'deterministic',
                'selection': runner.TEST, 'headed': False, 'injected_defect': None,
                'source_files_unchanged_during_run': True, 'testcases': 1, 'failure': 0,
                'error': 0, 'skipped': 0, 'pytest_exit_code': 0}
    calls = []

    def execute(args, **kwargs):
        events.append('test')
        calls.append((args, kwargs))
        directory = profile.base / 'ci-browser'
        directory.mkdir()
        (directory / 'result.json').write_text(json.dumps(measured), encoding='utf-8')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, 'run', execute)
    return SimpleNamespace(root=tmp_path, events=events, measured=measured, calls=calls,
                           result=lambda: json.loads((tmp_path / 'var/qa/ci-browser/result.json').read_text()))


def test_smoke_uses_owned_environment_single_test_and_cleanup(browser_fixture):
    item = browser_fixture
    assert runner.run('var/qa/ci-browser') == 0
    assert item.events == ['prepare', 'start', 'test', 'stop']
    args, kwargs = item.calls[0]
    assert args[0] == str(item.root / '.venv/Scripts/python.exe')
    assert args[args.index('--test') + 1] == runner.TEST
    assert args[args.index('--profile') + 1] == 'deterministic'
    assert kwargs['timeout'] == 300
    assert kwargs['stdout'] == subprocess.DEVNULL and kwargs['stderr'] == subprocess.DEVNULL
    result = item.result()
    assert result['status'] == result['cleanup_status'] == 'PASS'
    assert result['browser_result_path'] == 'var/qa/runs/qa-123456789abc/ci-browser/result.json'


def test_start_failure_still_stops_owned_stand_and_omits_error_text(browser_fixture, monkeypatch):
    def fail(profile):
        raise RuntimeError('my-private-api-token')
    monkeypatch.setattr(browser_stand, 'start', fail)
    assert runner.run('var/qa/ci-browser') == 1
    result = browser_fixture.result()
    assert result['cleanup_status'] == 'PASS'
    assert browser_fixture.events == ['prepare', 'stop']
    assert result['runner_error_type'] == 'RuntimeError'
    assert 'my-private-api-token' not in json.dumps(result)


def test_timeout_still_stops_owned_stand(browser_fixture, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired('private-command', 300)
    monkeypatch.setattr(runner.subprocess, 'run', fail)
    assert runner.run('var/qa/ci-browser') == 1
    assert browser_fixture.result()['runner_error_type'] == 'TimeoutExpired'
    assert browser_fixture.result()['cleanup_status'] == 'PASS'


@pytest.mark.parametrize('cleanup', ['raise', 'unconfirmed'])
def test_cleanup_failure_cannot_be_green(browser_fixture, monkeypatch, cleanup):
    def stop(profile):
        if cleanup == 'raise':
            raise RuntimeError('secret-connection')
        return {'status': 'unknown'}
    monkeypatch.setattr(browser_stand, 'stop', stop)
    assert runner.run('var/qa/ci-browser') == 1
    assert browser_fixture.result()['cleanup_status'] == 'FAIL'


@pytest.mark.parametrize('field,value', [
    ('testcases', 0), ('testcases', 2), ('testcases', True), ('failure', 1), ('error', 1),
    ('skipped', 1), ('pytest_exit_code', 1), ('status', 'ERROR'), ('profile', 'real-model'),
    ('selection', 'other_test'), ('headed', True), ('injected_defect', 'corrupt_total'),
    ('source_files_unchanged_during_run', False), ('run_id', 'qa-000000000000'),
])
def test_incorrect_or_missing_measurement_is_not_pass(browser_fixture, field, value):
    browser_fixture.measured[field] = value
    assert runner.run('var/qa/ci-browser') == 1
    assert browser_fixture.result()['status'] == 'FAIL'
    assert browser_fixture.result()['cleanup_status'] == 'PASS'


def test_missing_child_result_fails_but_cleans(browser_fixture, monkeypatch):
    monkeypatch.setattr(runner.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=2))
    assert runner.run('var/qa/ci-browser') == 1
    assert browser_fixture.result()['status'] == 'FAIL'
    assert browser_fixture.result()['cleanup_status'] == 'PASS'


def test_existing_results_never_overwritten(browser_fixture):
    runner.run('var/qa/ci-browser')
    before = browser_fixture.result()
    with pytest.raises(FileExistsError):
        runner.run('var/qa/ci-browser')
    assert browser_fixture.result() == before
    assert browser_fixture.events == ['prepare', 'start', 'test', 'stop']


@pytest.mark.parametrize('path', ['src/new', 'var/qa', '../outside', 'verification/old/new'])
def test_output_stays_in_new_qa_area(browser_fixture, path):
    with pytest.raises(ValueError):
        runner.output_path(path)


def test_ci_has_independent_hermetic_and_browser_jobs():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / '.github/workflows/ci.yml').read_text(encoding='utf-8'))
    assert set(workflow['on']) == {'push', 'pull_request', 'workflow_dispatch'}
    assert workflow['permissions'] == {'contents': 'read'}
    assert set(workflow['jobs']) == {'hermetic-windows', 'browser-smoke-windows'}
    for name, job in workflow['jobs'].items():
        assert job['runs-on'] == 'windows-latest'
        assert 'needs' not in job and 'services' not in job
        steps = job['steps']
        report = next(step for step in steps if 'qa/tools/report_ci.py' in step.get('run', ''))
        upload = next(step for step in steps if step.get('uses', '').startswith('actions/upload-artifact@'))
        summary = next(step for step in steps if 'GITHUB_STEP_SUMMARY' in step.get('run', ''))
        assert report['if'] == upload['if'] == summary['if'] == 'always()'
        assert steps.index(report) < steps.index(summary) < steps.index(upload)
        expected = 'var/checks/ci-report/' if name == 'hermetic-windows' else 'var/checks/browser-report/'
        assert upload['with']['path'] == expected
        assert upload['with']['retention-days'] == 14
        assert not any(step.get('continue-on-error') for step in steps)
    browser = workflow['jobs']['browser-smoke-windows']['steps']
    commands = '\n'.join(step.get('run', '') for step in browser)
    assert 'python -m venv .venv\n' in commands
    assert '--require-hashes --only-binary=:all:' in commands
    assert 'qa/tools/browser_tooling.py install' in commands
    assert 'qa/tools/run_ci_browser.py' in commands
    assert 'run_llm' not in commands and 'docker' not in commands and 'ollama pull' not in commands
    hermetic = '\n'.join(step.get('run', '') for step in workflow['jobs']['hermetic-windows']['steps'])
    for entrypoint in ('check_ci_inputs.py', 'run_checks.py', 'qualify_ci_ui.py'):
        assert entrypoint in hermetic
