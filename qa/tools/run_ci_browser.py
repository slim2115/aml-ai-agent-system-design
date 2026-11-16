"""One deterministic Chromium smoke with an owned stand and finally cleanup.

Only the small result envelope is intended for the CI report collector. The
owned stand, connection secrets, traces and process logs stay in var/qa/runs.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.qa_profile import QAProfile, checked_path

TEST = 'test_template_job_finishes_and_shows_exact_sources'
TIMEOUT_SECONDS = 300


def output_path(value):
    path = checked_path(ROOT / value, ROOT)
    relative = path.relative_to(ROOT)
    allowed = (path.is_relative_to(ROOT / 'var/qa') and path != ROOT / 'var/qa') or (
        len(relative.parts) > 2 and relative.parts[0] == 'verification'
        and relative.parts[1].startswith('qa-step-09-'))
    if not allowed:
        raise ValueError('QA_CI_OUTPUT_PATH_REQUIRED')
    if path.exists():
        raise FileExistsError('QA_CI_OUTPUT_ALREADY_EXISTS')
    return path


def measured_result(value, code):
    counts = {key: value[key] for key in ('testcases', 'failure', 'error', 'skipped')}
    if any(type(number) is not int or number < 0 for number in counts.values()):
        raise ValueError('QA_CI_INVALID_BROWSER_COUNTS')
    if (value.get('profile') != 'deterministic' or value.get('selection') != TEST
            or value.get('headed') is not False or value.get('injected_defect') is not None
            or value.get('source_files_unchanged_during_run') is not True):
        raise ValueError('QA_CI_BROWSER_PROFILE_MISMATCH')
    passed = (code == 0 and value.get('pytest_exit_code') == 0 and value.get('status') == 'PASS'
              and counts == {'testcases': 1, 'failure': 0, 'error': 0, 'skipped': 0})
    return dict(counts, status='PASS' if passed else 'FAIL', pytest_exit_code=value.get('pytest_exit_code'))


def run(value):
    from qa.tools import browser_stand
    from qa.tools.environment import child_environment

    output = output_path(value)
    output.mkdir(parents=True, exist_ok=False)
    result = {
        'schema_version': '1.0', 'profile': 'browser', 'status': 'INCOMPLETE',
        'started_at_utc': datetime.now(timezone.utc).isoformat(),
        'run_id': None, 'selected_test': TEST, 'testcases': 0,
        'failure': 0, 'error': 0, 'skipped': 0, 'pytest_exit_code': None,
        'cleanup_status': 'NOT_STARTED', 'runner_error_type': None, 'cleanup_error_type': None,
        'browser_result_path': None,
        'scope': 'One real Chromium smoke: UI, HTTP API and SQLite with deterministic model doubles. '
                 'No PostgreSQL, Kafka transport, embedding or LLM acceptance.',
    }
    profile = None
    try:
        python = checked_path(ROOT / '.venv/Scripts/python.exe', ROOT)
        if not python.is_file():
            raise ValueError('QA_CI_APPLICATION_VENV_REQUIRED')
        prepared = browser_stand.prepare()
        profile = QAProfile(prepared['run_id'], ROOT).validate()
        result['run_id'] = profile.run_id
        browser_output = profile.base / 'ci-browser'
        result['browser_result_path'] = (browser_output / 'result.json').relative_to(ROOT).as_posix()
        browser_stand.start(profile)
        # A separate process contains run_browser's environment/tooling changes.
        # Discard console output: the browser runner retains redacted local logs.
        completed = subprocess.run(
            [str(python), '-I', '-B', str(ROOT / 'qa/tools/run_browser.py'),
             '--run-id', profile.run_id, '--profile', 'deterministic', '--test', TEST,
             '--output-dir', browser_output.relative_to(ROOT).as_posix()],
            cwd=ROOT, env=child_environment(profile), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=TIMEOUT_SECONDS, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        saved = checked_path(browser_output / 'result.json', ROOT)
        if saved.stat().st_size > 1024 * 1024:
            raise ValueError('QA_CI_BROWSER_RESULT_TOO_LARGE')
        measured = json.loads(saved.read_text(encoding='utf-8'))
        if measured.get('run_id') != profile.run_id:
            raise ValueError('QA_CI_BROWSER_RUN_MISMATCH')
        result.update(measured_result(measured, completed.returncode))
    except Exception as exc:
        # Exception messages and subprocess output can contain connection data.
        result.update(status='FAIL', runner_error_type=type(exc).__name__, error=max(1, result['error']))
    finally:
        if profile is not None:
            try:
                stopped = browser_stand.stop(profile)
                if stopped.get('status') != 'stopped':
                    raise RuntimeError('QA_CI_CLEANUP_UNCONFIRMED')
                result['cleanup_status'] = 'PASS'
            except Exception as exc:
                result.update(status='FAIL', cleanup_status='FAIL', cleanup_error_type=type(exc).__name__)
        result['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        with (output / 'result.json').open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    print(json.dumps({key: result[key] for key in ('status', 'testcases', 'failure', 'error', 'skipped', 'cleanup_status')}))
    return 0 if result['status'] == 'PASS' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', default='var/qa/ci-browser')
    args = parser.parse_args()
    try:
        return run(args.output_dir)
    except Exception as exc:
        print(json.dumps({'status': 'FAIL', 'runner_error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
