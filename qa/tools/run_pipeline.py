"""Explicit real Kafka/SQL/API/AML QA profile, isolated from ordinary data."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.qa_profile import QAProfile, checked_path
from qa.tools.environment import child_environment
from qa.tools.run_browser import browser_run_lease, evidence_path, junit_result, require_live

CHAIN_CASES = frozenset({
    'test_01_real_chain_opens_exact_kafka_job_in_operator_ui',
    'test_02_identical_republication_has_two_deliveries_and_one_job',
    'test_03_conflicting_uuid_goes_to_dlq_without_changing_original',
    'test_04_malformed_json_has_exact_dlq_source_and_no_inbox_entry',
    'test_05_broker_ack_while_worker_stopped_then_resumes_single_job',
})
FAULT_CASES = frozenset({
    'test_api_outage_retains_queue_then_uses_same_event',
    'test_postgresql_outage_does_not_ack_unstored_event',
    'test_broker_outage_retries_original_payload_without_double_effect',
    'test_lost_http_acceptance_response_reuses_same_job',
    'test_lost_kafka_commit_ack_preserves_intake_and_recovers[before-commit]',
    'test_lost_kafka_commit_ack_preserves_intake_and_recovers[after-commit]',
    'test_live_postgresql_query_respects_shared_budget',
})


def expected_cases(suite, selection=None):
    cases = {'chain': CHAIN_CASES, 'faults': FAULT_CASES, 'all': CHAIN_CASES | FAULT_CASES}[suite]
    return {name for name in cases if selection is None or name.split('[')[0] == selection}


def collection_complete(junit, suite, selection=None):
    expected = expected_cases(suite, selection)
    names = [row.get('name') for row in ET.parse(junit).iter('testcase')] if junit.exists() else []
    return bool(expected) and len(names) == len(expected) and set(names) == expected

def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def hashes():
    paths = [path for directory in ('src', 'qa') for path in (ROOT / directory).rglob('*.py')]
    paths += [ROOT / 'qa/pytest.ini', ROOT / 'qa/requirements-browser.lock',
              ROOT / 'qa/datasets/aml-v1/manifest.json',
              ROOT / 'var/models/paraphrase-multilingual-MiniLM-L12-v2.manifest.json']
    return {path.relative_to(ROOT).as_posix(): sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def prepare_search(output, enabled):
    from src.workflow.remote import WorkflowClient
    client = WorkflowClient(timeout=5)
    started = time.monotonic()
    before = client.readiness()
    if before['state'] == 'busy':
        raise RuntimeError('QA_PIPELINE_HAS_ACTIVE_JOB')
    if not before['ready'] and not enabled:
        raise RuntimeError('QA_SEARCH_NOT_READY_USE_PREPARE_MODEL')
    if not before['ready'] and before['state'] != 'preparing':
        client.prepare()
    deadline = time.monotonic() + 180
    observations = [before]
    while True:
        current = client.readiness()
        if current != observations[-1]:
            observations.append(current)
        if current['ready']:
            break
        if time.monotonic() >= deadline or current['state'] != 'preparing':
            write(output / 'preparation.json', dict(status='FAIL', observations=observations,
                  elapsed_seconds=time.monotonic() - started))
            raise RuntimeError('QA_REAL_SEARCH_PREPARATION_FAILED')
        time.sleep(.25)
    write(output / 'preparation.json', dict(status='PASS', observations=observations,
          elapsed_seconds=time.monotonic() - started,
          mode='real_local_embedding_and_index; template reports; no Ollama generation'))


def select_tests(suite, name):
    if name and re.fullmatch(r'test_[a-z0-9_]+', name) is None:
        raise ValueError('QA_SINGLE_TEST_FUNCTION_REQUIRED')
    if name and suite == 'all':
        raise ValueError('QA_SINGLE_TEST_REQUIRES_SUITE')
    filenames = ['test_chain.py', 'test_faults.py'] if suite == 'all' else ['test_' + suite + '.py']
    return [str(ROOT / 'qa/pipeline' / filename) + ('::' + name if name else '') for filename in filenames]


def run(args):
    profile = QAProfile(args.run_id).validate()
    with browser_run_lease(profile):
        require_live(profile, 'real-model')
        selectors = select_tests(args.suite, args.test)
        output = evidence_path(profile, args.output_dir)
        cfg = json.loads(profile.config_path.read_text(encoding='utf-8'))
        private_values = [cfg['api_token'], *(role['password'] for role in cfg['roles'].values())]
        environment = child_environment(profile)
        for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'AML_QA_BROWSER_DEFECT', 'AML_QA_BROWSER_MODE'):
            environment.pop(key, None)
        environment.update(AML_QA_PIPELINE_OUTPUT=str(output), AML_QA_PROBE_ID=secrets.token_hex(16))
        os.environ.clear()
        os.environ.update(environment)
        from qa.tools.browser_tooling import activate
        versions = activate()
        output.mkdir(parents=True, exist_ok=False)
        started = datetime.now(timezone.utc).isoformat()
        before = hashes()
        write(output / 'source-hashes.json', before)
        write(output / 'run.json', dict(status='INCOMPLETE', started_at_utc=started,
            run_id=profile.run_id, probe_id=environment['AML_QA_PROBE_ID'], suite=args.suite,
            selection=args.test or 'full_suite'))
        log = io.StringIO()
        error_type = error_code = None
        code = 3
        try:
            with redirect_stdout(log), redirect_stderr(log):
                from qa.tools import stand, broker
                if not stand.pg_running(profile) or broker.status(profile)['status'] != 'ready':
                    raise RuntimeError('QA_PIPELINE_DEPENDENCIES_NOT_READY')
                if not all(stand.service_ready(profile, name) for name in ('worker', 'review-worker')):
                    raise RuntimeError('QA_PIPELINE_WORKERS_NOT_READY')
                prepare_search(output, args.prepare_model)
                import pytest
                code = int(pytest.main(['-c', str(ROOT / 'qa/pytest.ini'), '--confcutdir=' + str(ROOT / 'qa'),
                    *selectors, '-q', '--junitxml=' + str(output / 'tests.xml')]))
        except Exception as exc:
            error_type = type(exc).__name__
            error_code = str(exc) if str(exc).startswith('QA_') else 'QA_PIPELINE_SETUP_FAILED'
        safe_log = log.getvalue()
        for value in private_values:
            safe_log = safe_log.replace(value, 'REDACTED_QA_SECRET')
        (output / 'pytest.log').write_text(safe_log, encoding='utf-8')
        junit = output / 'tests.xml'
        if junit.exists():
            text = junit.read_text(encoding='utf-8')
            for value in private_values:
                text = text.replace(value, 'REDACTED_QA_SECRET')
            junit.write_text(text, encoding='utf-8')
        try:
            measured = junit_result(junit, code)
            complete = collection_complete(junit, args.suite, args.test)
        except ET.ParseError:
            measured = dict(status='ERROR', testcases=0, failure=0, error=1, skipped=0)
            complete = False
        unchanged = hashes() == before
        if not unchanged or not complete:
            measured['status'] = 'ERROR'
        result = dict(schema_version='1.0', started_at_utc=started,
            finished_at_utc=datetime.now(timezone.utc).isoformat(), run_id=profile.run_id,
            probe_id=environment['AML_QA_PROBE_ID'], suite=args.suite, selection=args.test or 'full_suite',
            pytest_exit_code=code, runner_error_type=error_type, runner_error_code=error_code,
            expected_testcases=len(expected_cases(args.suite, args.test)), collection_complete=complete,
            source_files_unchanged_during_run=unchanged, packages=versions, **measured,
            scope='Real QA API, Kafka, PostgreSQL, SQLite, local embedding and template reports. '
                  'UI via Chromium in chain suite; explicitly labelled boundary fault injection in faults suite. '
                  'No Ollama quality benchmark, Judge or expert acceptance.',
            product_acceptance='INCOMPLETE')
        write(output / 'result.json', result)
        (output / 'summary.md').write_text(
            f"# SQL/API/Kafka: {result['status']}\n\n"
            f"Run `{profile.run_id}`, suite `{args.suite}`, selection `{result['selection']}`.\n\n"
            f"Тестов: {result['testcases']}; провалов: {result['failure']}; ошибок: {result['error']}; пропусков: {result['skipped']}.\n\n"
            f"{result['scope']}\n\n[Результат](result.json), [JUnit](tests.xml), [журнал](pytest.log).\n",
            encoding='utf-8')
        print(safe_log)
        print(json.dumps({key: result[key] for key in ('status', 'suite', 'testcases', 'failure', 'error', 'skipped', 'runner_error_code')}))
        return 0 if result['status'] == 'PASS' else 1


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--suite', choices=('chain', 'faults', 'all'), default='chain')
    parser.add_argument('--test')
    parser.add_argument('--prepare-model', action='store_true')
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as exc:
        print(json.dumps(dict(status='ERROR', error_type=type(exc).__name__,
            code=str(exc) if str(exc).startswith('QA_') else 'QA_PIPELINE_SETUP_FAILED')))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
