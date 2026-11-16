"""Run the explicit browser profile and retain new, scoped, redacted evidence."""
import argparse
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.qa_profile import QAProfile, checked_path
from qa.tools.environment import child_environment


def evidence_path(profile, value):
    path = checked_path(ROOT / value)
    if not (path.is_relative_to(profile.base) or path.is_relative_to(ROOT / 'verification')):
        raise ValueError('INVALID_QA_EVIDENCE_PATH')
    if path.exists():
        raise FileExistsError('QA_EVIDENCE_ALREADY_EXISTS')
    return path


def selected_test(mode, name=None, defect=None, suite='operator'):
    if suite not in {'operator', 'workshop'}:
        raise ValueError('QA_BROWSER_SUITE_INVALID')
    if suite == 'workshop' and mode != 'deterministic':
        raise ValueError('QA_WORKSHOP_REQUIRES_DETERMINISTIC_PROFILE')
    filename = ('test_workshop.py' if suite == 'workshop' else
                'test_operator.py' if mode == 'deterministic' else 'test_real_model.py')
    if name and re.fullmatch(r'test_[a-z0-9_]+', name) is None:
        raise ValueError('QA_SINGLE_TEST_FUNCTION_REQUIRED')
    if defect and (suite != 'operator' or mode != 'deterministic' or name != 'test_template_job_finishes_and_shows_exact_sources'):
        raise ValueError('QA_DEFECT_REQUIRES_DETERMINISTIC_SOURCE_TEST')
    return str(ROOT / 'qa/e2e' / filename) + ('::' + name if name else '')


def require_live(profile, mode):
    from qa.tools import stand
    from qa.tools.browser_stand import require_browser
    marker = checked_path(profile.base / 'browser-profile.json')
    if mode == 'deterministic':
        require_browser(profile)
    elif marker.exists():
        raise ValueError('QA_REAL_MODEL_REQUIRES_FULL_STAND')
    active = checked_path(ROOT / 'var/qa/active.json')
    if not active.exists() or json.loads(active.read_text(encoding='utf-8')) != profile.owner_record():
        raise ValueError('QA_ACTIVE_OWNER_MISMATCH')
    if not all(stand.service_ready(profile, service) for service in ('api', 'ui')):
        raise RuntimeError('QA_API_AND_UI_MUST_BE_STARTED_FIRST')
    state = stand.read_state(profile, 'api')
    is_browser = state.get('mode') == 'browser-deterministic'
    if is_browser != (mode == 'deterministic'):
        raise ValueError('QA_BROWSER_SERVER_MODE_MISMATCH')


def junit_result(path, code):
    rows = list(ET.parse(path).iter('testcase')) if path.exists() else []
    counts = {kind: sum(any(child.tag == kind for child in row) for row in rows)
              for kind in ('failure', 'error', 'skipped')}
    status = 'PASS' if code == 0 and rows and not any(counts.values()) else 'FAIL'
    if int(code) not in (0, 1):
        status = 'ERROR'
    return {'status': status, 'testcases': len(rows), **counts}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def source_hashes():
    paths = [path for folder in ('src', 'qa') for path in (ROOT / folder).rglob('*.py')]
    paths += [ROOT / 'qa/e2e/pytest.ini', ROOT / 'qa/requirements-browser.lock']
    return {path.relative_to(ROOT).as_posix(): sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


@contextmanager
def browser_run_lease(profile):
    from src.workflow.lease import ControllerLease
    try:
        lease = ControllerLease(profile.workflow_dir / 'browser-checks.sqlite3')
    except ValueError as exc:
        if str(exc) == 'CONTROLLER_ALREADY_ACTIVE':
            raise ValueError('QA_BROWSER_RUN_ALREADY_ACTIVE') from None
        raise
    try:
        yield
    finally:
        lease.close()


def run(args):
    profile = QAProfile(args.run_id).validate()
    with browser_run_lease(profile):
        return run_owned(args, profile)


def run_owned(args, profile):
    suite = getattr(args, 'suite', 'operator')
    selector = selected_test(args.profile, args.test, args.inject_defect, suite)
    output = evidence_path(profile, args.output_dir)
    require_live(profile, args.profile)
    token = json.loads(profile.config_path.read_text(encoding='utf-8'))['api_token']
    if not isinstance(token, str) or not token:
        raise ValueError('QA_KEY_REQUIRED')
    env = child_environment(profile)
    for name in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'AML_QA_BROWSER_DEFECT'):
        env.pop(name, None)
    env.update(AML_QA_BROWSER_MODE=args.profile, AML_QA_BROWSER_OUTPUT=str(output))
    if args.inject_defect:
        env['AML_QA_BROWSER_DEFECT'] = args.inject_defect
    os.environ.clear()
    os.environ.update(env)
    from qa.tools.browser_tooling import activate
    versions = activate()
    import pytest
    output.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    inputs_before = source_hashes()
    write_json(output / 'source-hashes.json', inputs_before)
    log = io.StringIO()
    error_type = None
    arguments = ['-c', str(ROOT / 'qa/e2e/pytest.ini'), '--confcutdir=' + str(ROOT / 'qa'), selector,
                 '-p', 'pytest_playwright.pytest_playwright', '--browser', 'chromium',
                 '--tracing=off', '--screenshot=off', '--video=off',
                 '--output=' + str(output / 'plugin-output'),
                 '--junitxml=' + str(output / 'tests.xml'), '-q']
    if args.headed:
        arguments.append('--headed')
    try:
        with redirect_stdout(log), redirect_stderr(log):
            code = int(pytest.main(arguments))
    except Exception as exc:
        code, error_type = 3, type(exc).__name__
    safe_log = log.getvalue().replace(token, 'REDACTED_QA_KEY')
    (output / 'pytest.log').write_text(safe_log, encoding='utf-8')
    junit = output / 'tests.xml'
    if junit.exists():
        junit.write_text(junit.read_text(encoding='utf-8').replace(token, 'REDACTED_QA_KEY'), encoding='utf-8')
    try:
        measured = junit_result(junit, code)
    except ET.ParseError:
        measured = {'status': 'ERROR', 'testcases': 0, 'failure': 0, 'error': 1, 'skipped': 0}
    inputs_unchanged = source_hashes() == inputs_before
    if not inputs_unchanged:
        measured['status'] = 'ERROR'
    result = {
        'schema_version': '1.0', 'started_at_utc': started,
        'finished_at_utc': datetime.now(timezone.utc).isoformat(),
        'run_id': profile.run_id, 'profile': args.profile, 'suite': suite, 'headed': args.headed,
        'selection': args.test or 'full_profile', 'injected_defect': args.inject_defect,
        'pytest_exit_code': code, 'runner_error_type': error_type, **measured,
        'packages': versions,
        'source_files_unchanged_during_run': inputs_unchanged,
        'dataset_manifest_sha256': sha256((ROOT / 'qa/datasets/aml-v1/manifest.json').read_bytes()).hexdigest(),
        'scope': ('Real Chromium, operator UI, HTTP API and SQLite. Deterministic model doubles; '
                  'Kafka panel fixture only. No PostgreSQL, Kafka transport or model quality qualification.'
                  if args.profile == 'deterministic' else
                  'Opt-in browser smoke against real local retrieval and generation; a single smoke is not model quality acceptance.'),
        'product_acceptance': 'INCOMPLETE',
        'trace_policy': 'Failure-only traces and screenshots; both disabled for Swagger key entry.',
        'artifacts': sorted(path.relative_to(output).as_posix() for path in output.rglob('*') if path.is_file()),
    }
    write_json(output / 'result.json', result)
    (output / 'summary.md').write_text(
        f"# Playwright: {result['status']}\n\n"
        f"Профиль: `{args.profile}`. Набор: `{suite}`. Run: `{profile.run_id}`. Выбор: `{result['selection']}`.\n\n"
        f"Тестов: {result['testcases']}; провалов: {result['failure']}; ошибок: {result['error']}; пропусков: {result['skipped']}.\n\n"
        f"{result['scope']}\n\n"
        "[Машинный результат](result.json), [журнал](pytest.log), [JUnit](tests.xml). "
        "При сбое откройте `artifacts/<id>/trace.zip` и `failure.png`. Для Swagger они отключены, чтобы не сохранять ключ.\n",
        encoding='utf-8')
    print(safe_log)
    print(json.dumps({key: result[key] for key in ('status', 'profile', 'suite', 'selection', 'testcases', 'failure', 'error', 'skipped')}, ensure_ascii=False))
    return 0 if result['status'] == 'PASS' else 1


def main():
    # Windows terminals may default to a code page without Playwright's Unicode
    # diagnostics. Console rendering must not replace an already saved verdict.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--profile', choices=('deterministic', 'real-model'), default='deterministic')
    parser.add_argument('--suite', choices=('operator', 'workshop'), default='operator')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--test')
    parser.add_argument('--inject-defect', choices=('corrupt_total',))
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as exc:
        print(json.dumps({'status': 'ERROR', 'error_type': type(exc).__name__,
                          'code': str(exc) if str(exc).startswith('QA_') else 'BROWSER_RUNNER_SETUP_FAILED'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
