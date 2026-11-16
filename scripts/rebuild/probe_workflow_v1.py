"""Finite managed-workflow integration with real local models and spawn controls."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / 'verification/block-05'
from src.contracts.v1 import CreateJobRequest, DecisionRequest, DeliveryRequest
from src.runtime.policy import load_demo_policy
from src.workflow.auth import trusted_local_principal
from src.workflow.controller import WorkflowService
from src.workflow.delivery import DeliveryService
from src.workflow.sources import resolve_report_sources
from src.workflow.store import SQLiteStore, WorkflowError
from src.workflow.worker import BootstrapResult, ManagedWorker


def boot_control():
    return BootstrapResult(None, {'fixture': 'lifecycle_only_no_model'})


def stalled_boot_control():
    time.sleep(60)
    return boot_control()


def stalled_handler_control(state, incident_id, engine):
    time.sleep(60)
    raise RuntimeError('UNREACHABLE_AFTER_TERMINATION')


def inputs():
    paths = ['src/ui/app.py', 'scripts/run_workflow.py', 'scripts/run_investigation.py',
             'config/demo-policy.json', 'data/demo-v1/manifest.json', 'data/demo-v1/rules.json']
    for directory in ('src/workflow', 'src/runtime', 'src/contracts'):
        paths += [str(path.relative_to(ROOT)).replace('\\', '/') for path in (ROOT / directory).glob('*.py')]
    return {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in sorted(paths)}


def wait_for(predicate, timeout=190):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.04)
    raise TimeoutError('PROBE_BOUND_EXCEEDED')


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    started, before, checks, results = time.monotonic(), inputs(), [], []
    def save():
        report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'RUNNING',
                  'elapsed_seconds': time.monotonic() - started, 'checks': checks, 'input_hashes': before}
        (OUTPUT / 'workflow-probe-progress.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    def check(name, passed, **detail):
        checks.append({'name': name, 'passed': bool(passed), **detail})
        print(name + ': ' + ('PASS' if passed else 'FAIL'), flush=True)
        save()
        if not passed:
            raise AssertionError(name)
    def cli(store, *args):
        proc = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'scripts/run_workflow.py'),
                               '--database', str(store.path), *args], capture_output=True, timeout=30)
        return proc.returncode, json.loads(proc.stdout.decode('utf-8'))
    actor = trusted_local_principal()
    store = SQLiteStore(ROOT / 'var/workflow' / ('probe-' + uuid4().hex) / 'workflow.sqlite3')
    policy = load_demo_policy(ROOT / 'config/demo-policy.json')
    request = CreateJobRequest(schema_version='1.0', incident_id='INC-000123', window=policy.window)
    try:
        # These real spawned children deliberately stall, without invoking Ollama.
        with ManagedWorker(bootstrap=stalled_boot_control, handler=stalled_handler_control) as worker:
            mark = time.monotonic()
            state = worker.start(timeout_seconds=.3)
            check('readiness_stall_is_bounded', not state['ready'] and worker.process_id is None,
                  elapsed_seconds=time.monotonic()-mark, result=state)
        for operation in ('deadline', 'cancel'):
            with ManagedWorker(bootstrap=boot_control, handler=stalled_handler_control) as worker:
                check(operation + '_control_ready', worker.start(timeout_seconds=15)['ready'])
                child = worker._process  # Verification only: retain OS process handle after retirement.
                cancel = threading.Event()
                timer = threading.Timer(.2, cancel.set) if operation == 'cancel' else None
                if timer:
                    timer.start()
                try:
                    outcome = worker.execute('INC-000123', timeout_seconds=.35 if operation == 'deadline' else 10,
                                             cancel_event=cancel)
                finally:
                    if timer:
                        timer.cancel()
                check(operation + '_child_terminated', outcome.status == ('timed_out' if operation == 'deadline' else 'cancelled')
                      and not child.is_alive() and not worker.is_ready, elapsed_seconds=outcome.elapsed_seconds,
                      cleanup_seconds=outcome.cleanup_seconds, child_exit_code=child.exitcode)
        with WorkflowService(store) as service:
            try:
                service.submit(request, actor, 'before-ready')
                blocked = False
            except WorkflowError as exc:
                blocked = exc.code == 'NOT_READY'
            check('not_ready_admission_creates_no_job', blocked and not store.list_jobs(actor))
            mark = time.monotonic()
            service.prepare(timeout_seconds=180)
            wait_for(lambda: service.readiness()['state'] != 'preparing')
            check('real_embedding_ready', service.readiness()['ready'], elapsed_seconds=time.monotonic()-mark,
                  state=service.readiness(), provenance=service._ready_result.get('provenance'))
            worker_pid = service.worker.process_id
            cases = [('INC-000123', 'template', 'succeeded', None),
                     ('INC-000999', 'template', 'refused', 'INSUFFICIENT_DATA'),
                     ('INC-000123', 'local-model', 'succeeded', None)]
            for incident, engine, expected, reason in cases:
                wait_for(lambda: service.readiness()['ready'], timeout=5)
                mark = time.monotonic()
                accepted = service.submit(CreateJobRequest(schema_version='1.0', incident_id=incident, window=policy.window),
                                          actor, 'probe-' + engine + incident, engine=engine)
                def terminal():
                    job = store.get_job(accepted['job_id'], actor)
                    return job if job['status'] not in ('queued', 'running') else None
                job = wait_for(terminal)
                results.append(job)
                check('managed_' + engine + '_' + incident, job['status'] == expected and
                      (reason is None or job.get('result', {}).get('reason_code') == reason),
                      elapsed_seconds=time.monotonic()-mark, status=job['status'], job_id=job['job_id'],
                      same_worker=service.worker.process_id == worker_pid)
                events = store.list_runtime_events(job['job_id'], actor)
                saved_events = SQLiteStore(store.path).list_runtime_events(job['job_id'], actor)
                check('durable_runtime_events_' + engine + '_' + incident, events == saved_events and bool(events)
                      and events[0]['event']['stage'] == 'input' and events[-1]['event']['stage'] == 'complete'
                      and [item['sequence'] for item in events] == list(range(1, len(events) + 1)),
                      event_count=len(events), generation_attempts=[item['event']['attempt'] for item in events
                          if item['event']['stage'] == 'generation' and item['event']['code'] == 'STARTED'])
                code, value = cli(store, 'events', job['job_id'])
                check('cli_runtime_events_' + engine + '_' + incident, code == 0 and value == events)
                if expected == 'succeeded':
                    report = job['result']
                    resolved = resolve_report_sources(report)
                    check('exact_sources_' + engine, len(resolved['facts']) == len(report['found_facts'])
                          and len(resolved['rules']) == len(report['applicable_rules'])
                          and all(item['edition_text'][item['char_start']:item['char_end']] == item['selected_text']
                                  for item in resolved['rules']))
                    reference = store.report_ref(report['report_id'], report['report_version'], actor)
                    decision = store.create_decision(reference.report_id,
                        DecisionRequest(schema_version='1.0', report=reference, expected_revision=0,
                                        action='approve', reason='Synthetic integration verification; not SME acceptance.'),
                        actor, 'approve-' + job['job_id'])
                    body = DeliveryRequest(schema_version='1.0', job_id=job['job_id'], decision_id=decision['decision_id'],
                                           report=reference, destination='simulator-export', operation='export')
                else:
                    body = DeliveryRequest(schema_version='1.0', job_id=job['job_id'], decision_id=None,
                                           report=None, destination='simulator-manual-queue', operation='manual_queue')
                deliveries = DeliveryService(store)
                pending = deliveries.create(body, actor, 'deliver-' + job['job_id'])
                confirmed = deliveries.dispatch(pending.delivery_id, actor, pending.revision)
                reopened = DeliveryService(SQLiteStore(store.path))
                persisted = reopened.get(confirmed.delivery_id, actor)
                check('persisted_receipt_' + engine + '_' + incident,
                      persisted == confirmed and persisted.status == 'delivered' and persisted.mode == 'local_simulator',
                      receipt_id=persisted.receipt.receipt_id)
                if expected == 'succeeded':
                    raw = reopened.export_artifact(confirmed.delivery_id, actor)
                    check('exact_export_' + engine, sha256(raw).hexdigest() == reference.content_sha256 and json.loads(raw) == report)
                code, value = cli(store, 'get', job['job_id'])
                check('cli_persisted_' + engine + '_' + incident, code == 0 and value == job)
            check('same_ready_worker_reused', service.worker.process_id == worker_pid)
        check('controller_close_stops_worker', service.worker.process_id is None)
        with WorkflowService(SQLiteStore(store.path)) as restarted:
            check('restart_retains_terminal_jobs', restarted.store.list_jobs(actor) == store.list_jobs(actor)
                  and len(store.list_jobs(actor)) == 3 and not restarted.readiness()['ready'])
        code, value = cli(store, 'run', 'INC-000123')
        check('cli_production_refuses_unapproved_policy', code == 2 and value.get('reason_code') == 'POLICY_NOT_APPROVED')
        code, value = cli(store, 'run', 'INC-000123<script>', '--demo')
        check('cli_rejects_invalid_id_before_model', code == 3 and value.get('code') == 'INVALID_INPUT_FORMAT')
    except Exception as exc:
        checks.append({'name': 'probe_completed', 'passed': False, 'safe_exception_type': type(exc).__name__})
        print('probe_completed: FAIL (' + type(exc).__name__ + ')', flush=True)
    unchanged = before == inputs()
    checks.append({'name': 'runtime_inputs_unchanged', 'passed': unchanged})
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'PASS' if checks and all(c['passed'] for c in checks) else 'FAIL',
              'elapsed_seconds': time.monotonic()-started, 'checks': checks, 'input_hashes': before,
              'inputs_unchanged_during_probe': unchanged, 'database': str(store.path),
              'scope': 'Synthetic local workflow, actual embedding and Ollama, spawn lifecycle controls. No SME, legal, external delivery, OS isolation or population latency acceptance.'}
    (OUTPUT / 'workflow-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (OUTPUT / 'workflow-probe.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
