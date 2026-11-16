"""Failure accounting and destination guards do not require browser tooling."""
from pathlib import Path
from types import SimpleNamespace
import pytest

from qa.tools.browser_boundaries import browser_target_allowed
from qa.tools.run_browser import browser_run_lease, evidence_path, junit_result, selected_test
from src.qa_profile import QAProfile, ROOT


@pytest.mark.parametrize('url,websocket,allowed', [
    ('http://127.0.0.1:18501/', False, True),
    ('http://127.0.0.1:18765/docs', False, True),
    ('ws://127.0.0.1:18501/_stcore/stream', True, True),
    ('https://127.0.0.1:18501/', False, False),
    ('http://127.0.0.1:8501/', False, False),
    ('http://127.0.0.1:8765/', False, False),
    ('http://localhost:18501/', False, False),
    ('http://127.0.0.1.evil.test:18501/', False, False),
    ('http://name:password@127.0.0.1:18501/', False, False),
    ('http://127.0.0.1:bad/', False, False),
    ('ws://127.0.0.1:18501/', False, False),
    ('wss://127.0.0.1:18501/', True, False),
    ('file:///D:/work/secret.txt', False, False),
])
def test_browser_requests_only_owned_loopback_ports(url, websocket, allowed):
    assert browser_target_allowed(url, SimpleNamespace(ui_port=18501, api_port=18765), websocket=websocket) is allowed


@pytest.mark.parametrize('body,code,status', [
    ('<testsuites/>', 0, 'FAIL'),
    ('<testsuites><testcase/></testsuites>', 0, 'PASS'),
    ('<testsuites><testcase><skipped/></testcase></testsuites>', 0, 'FAIL'),
    ('<testsuites><testcase><error/></testcase></testsuites>', 1, 'FAIL'),
    ('<testsuites><testcase><failure/></testcase></testsuites>', 1, 'FAIL'),
    ('<testsuites><testcase/></testsuites>', 5, 'ERROR'),
    ('<testsuites><testcase/></testsuites>', 1, 'FAIL'),
])
def test_result_requires_nonempty_pass_with_no_hidden_skips(tmp_path, body, code, status):
    path = tmp_path / 'result.xml'
    path.write_text(body)
    assert junit_result(path, code)['status'] == status


def test_missing_junit_cannot_pass(tmp_path):
    assert junit_result(tmp_path / 'absent.xml', 0)['status'] == 'FAIL'


def test_real_model_has_separate_explicit_file():
    assert selected_test('real-model').endswith('test_real_model.py')
    assert selected_test('deterministic').endswith('test_operator.py')


def test_workshop_uses_a_fixed_separate_file_and_exact_function():
    assert selected_test('deterministic', suite='workshop').endswith('test_workshop.py')
    name = 'test_incident_field_defaults_and_keeps_edited_value'
    assert selected_test('deterministic', name, suite='workshop').endswith('test_workshop.py::' + name)
    assert selected_test('deterministic', suite='operator') == selected_test('deterministic')


@pytest.mark.parametrize('mode,name,defect,suite', [
    ('real-model', None, None, 'workshop'),
    ('deterministic', 'test_template_job_finishes_and_shows_exact_sources', 'corrupt_total', 'workshop'),
    ('deterministic', '../test_operator.py', None, 'workshop'),
    ('deterministic', 'test_a or test_b', None, 'workshop'),
    ('deterministic', None, None, '../outside'),
    ('deterministic', None, None, 'test_operator.py'),
])
def test_workshop_cannot_expand_selector_or_use_model_and_fault_profiles(mode, name, defect, suite):
    with pytest.raises(ValueError):
        selected_test(mode, name, defect, suite)


@pytest.mark.parametrize('mode,name,defect', [
    ('deterministic', '../outside', None),
    ('deterministic', 'test_a or test_b', None),
    ('deterministic', None, 'corrupt_total'),
    ('deterministic', 'test_swagger_authorization_executes_protected_readiness', 'corrupt_total'),
    ('real-model', 'test_template_job_finishes_and_shows_exact_sources', 'corrupt_total'),
])
def test_unbounded_selector_or_ambiguous_fault_is_rejected(mode, name, defect):
    with pytest.raises(ValueError):
        selected_test(mode, name, defect)


def test_intentional_fault_uses_the_ordinary_source_assertion():
    name = 'test_template_job_finishes_and_shows_exact_sources'
    assert selected_test('deterministic', name, 'corrupt_total').endswith('test_operator.py::' + name)


@pytest.mark.parametrize('path', ['README.md', 'src/new-result', 'var/qa/runs/qa-bbbbbbbbbbbb/new-result', '../escape', 'verification/../src/new-result'])
def test_evidence_must_stay_in_owned_run_or_verification(path):
    with pytest.raises((ValueError, FileExistsError)):
        evidence_path(QAProfile('qa-aaaaaaaaaaaa'), path)


def test_concurrent_browser_run_is_rejected_and_lease_released(tmp_path, monkeypatch):
    import src.workflow.lease as leases
    monkeypatch.setattr(leases, 'PROJECT_ROOT', tmp_path)
    profile = SimpleNamespace(workflow_dir=tmp_path / 'var/workflow/qa/run')
    with pytest.raises(RuntimeError, match='test run interrupted'):
        with browser_run_lease(profile):
            with pytest.raises(ValueError, match='QA_BROWSER_RUN_ALREADY_ACTIVE'):
                with browser_run_lease(profile):
                    pytest.fail('A second runner must not enter the shared stand')
            raise RuntimeError('test run interrupted')
    with browser_run_lease(profile):
        pass
