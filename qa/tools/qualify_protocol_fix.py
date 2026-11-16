"""QA-022: one protocol attempt stops a worker; explicit repair reuses its job.

Real WorkflowClient, scheduler and worker loop; bounded in-memory HTTP/broker/
queue doubles. Historical baseline fixtures are reused without modifying them.
No PostgreSQL, Kafka broker, models or external network are exercised here.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from qa.tools.qualify_protocol import ACTOR, EVENT_ID, HEALTHY_ID, Queue, event, job
from src.lab.kafka.analysis_consumer import run_worker
from src.qa_profile import checked_path
from src.workflow.remote import WorkflowClient


class Transport:
    """Keep server admissions across explicit worker restarts, including lost replies."""

    def __init__(self, stage, fault):
        self.fault = fault
        self.fault_enabled = True
        self.calls = []
        self.jobs = {EVENT_ID: job('job-protocol-original')} if stage == 'submitted' else {}
        self.admissions = []

    def open(self, request, *, timeout):
        raise AssertionError('SCHEDULER_MUST_USE_SHARED_IO_BUDGET')

    def open_budgeted(self, request, *, timeout, deadline):
        deadline.check()
        key = request.get_header('Idempotency-key')
        poison = key == event().idempotency_key or request.full_url.endswith('/job-protocol-original')
        identifier = EVENT_ID if poison else HEALTHY_ID
        self.calls.append({'method': request.method, 'event_id': identifier,
            'idempotency_key': key, 'timeout_seconds': timeout,
            'job_id': request.full_url.rsplit('/', 1)[1] if request.method == 'GET' else None,
            'phase': 'fault' if self.fault_enabled else 'repaired'})
        if request.method == 'POST' and identifier not in self.jobs:
            self.jobs[identifier] = job('job-protocol-original' if poison else 'job-protocol-healthy')
            self.admissions.append({'event_id': identifier, 'job_id': self.jobs[identifier]['job_id'],
                                    'idempotency_key': key})
        value = deepcopy(self.jobs[identifier])
        if request.method == 'GET':
            value.update(status='cancelled', revision=1, completed_at='2026-10-17T10:00:01Z')
        if poison and self.fault_enabled:
            raw = b'{' if self.fault == 'malformed_json' else b'{"job_id":"unexpected","status":"running"}'
        else:
            raw = json.dumps(value).encode('utf-8')
        response = BytesIO(raw)
        response.status = 202 if request.method == 'POST' else 200
        response.headers = {}
        deadline.check()
        return response


class IdleBroker:
    """No event replay: exercise only the durable scheduler's existing two rows."""

    def __init__(self):
        self.polls = 0
        self.closed = False

    def poll(self, *, timeout):
        self.polls += 1
        return None

    def close(self):
        self.closed = True


def worker_cycle(queue, transport, *, max_ticks, stop_when_recovered=False):
    broker = IdleBroker()
    messages = []
    client = WorkflowClient({'api_token': 'a' * 64}, transport=transport)

    def stop():
        recovered = all(row['status'] == 'cancelled' for row in queue.rows.values())
        return broker.polls >= max_ticks or (stop_when_recovered and recovered)

    # Only the loop's scheduling clock is synthetic. HTTP still receives the
    # real monotonic Deadline from scheduler_tick and explicitly checks it.
    code = run_worker(queue, broker, client, principal=ACTOR, stop=stop,
                      emit=messages.append, clock=lambda: broker.polls * 2.0)
    return {'exit_code': code, 'messages': messages, 'polls': broker.polls,
            'broker_closed': broker.closed}


def scenario(stage, fault):
    queue = Queue()
    queue.add(event())
    if stage == 'submitted':
        queue.record_job(EVENT_ID, job('job-protocol-original'))
    queue.add(event(HEALTHY_ID))
    original = deepcopy(queue.rows[EVENT_ID])
    transport = Transport(stage, fault)

    # Exactly one bounded worker run. A broken retry policy cannot turn this
    # qualification into a loop: without the expected stop it returns FAIL.
    stopped = worker_cycle(queue, transport, max_ticks=1)
    retained = deepcopy(queue.rows[EVENT_ID])
    fault_calls = deepcopy(transport.calls)
    checks = {
        'worker_failed_once_with_protocol_code': stopped['exit_code'] == 1
            and [row for row in stopped['messages'] if row['status'] == 'failed'] == [
                {'status': 'failed', 'code': 'WORKFLOW_PROTOCOL_ERROR'}],
        'exactly_one_protocol_attempt_before_stop': len(fault_calls) == 1
            and fault_calls[0]['event_id'] == EVENT_ID,
        'one_worker_tick_and_closed_broker': stopped['polls'] == 1 and stopped['broker_closed'],
        'reason_recorded_as_protocol_error': retained['last_error_code'] == 'PROTOCOL_ERROR',
        'original_queue_state_preserved': retained['status'] == original['status'],
        'original_event_identity_and_payload_preserved': retained['event_id'] == original['event_id']
            and retained['event_json'] == original['event_json'],
        'original_job_reference_preserved': retained['job_id'] == original['job_id']
            and retained['job_json'] == original['job_json'],
        'healthy_row_not_processed_after_shared_protocol_failure': queue.rows[HEALTHY_ID]['status'] == 'queued'
            and queue.rows[HEALTHY_ID]['job_id'] is None,
    }

    # The operator has repaired the simulated HTTP service. A fresh worker
    # retries only persisted rows; neither queue nor server admissions reset.
    transport.fault_enabled = False
    restarted = worker_cycle(queue, transport, max_ticks=6, stop_when_recovered=True)
    restored = deepcopy(queue.rows[EVENT_ID])
    repaired_calls = transport.calls[len(fault_calls):]
    poison_calls = [call for call in transport.calls if call['event_id'] == EVENT_ID]
    poison_admissions = [row for row in transport.admissions if row['event_id'] == EVENT_ID]
    post_calls = [call for call in poison_calls if call['method'] == 'POST']
    checks.update({
        'explicit_restart_succeeded': restarted['exit_code'] == 0 and restarted['broker_closed']
            and restarted['messages'][-1] == {'status': 'stopped'},
        'original_and_healthy_rows_recovered': all(row['status'] == 'cancelled' for row in queue.rows.values()),
        'recovered_original_event_payload_unchanged': restored['event_id'] == original['event_id']
            and restored['event_json'] == original['event_json'],
        'recovered_original_server_job': restored['job_id'] == 'job-protocol-original',
        'exactly_two_original_rows': set(queue.rows) == {EVENT_ID, HEALTHY_ID},
        'reason_cleared_only_after_valid_response': restored['last_error_code'] is None,
        'same_key_for_ambiguous_admission_replay': len(post_calls) == (2 if stage == 'queued' else 0)
            and all(call['idempotency_key'] == event().idempotency_key for call in post_calls),
        'no_second_server_job_created': len(poison_admissions) == (1 if stage == 'queued' else 0)
            and set(transport.jobs) == {EVENT_ID, HEALTHY_ID},
        'submitted_row_only_polled_same_job': stage != 'submitted' or all(
            call['method'] == 'GET' and call['job_id'] == 'job-protocol-original' for call in poison_calls),
        'repaired_worker_made_progress': bool(repaired_calls),
    })
    return {'stage': stage, 'fault': fault, 'status': 'PASS' if all(checks.values()) else 'FAIL',
            'checks': checks, 'fault_worker': stopped, 'repair_worker': restarted,
            'state_at_stop': retained, 'state_after_repair': restored,
            'http_calls': transport.calls, 'new_server_admissions': transport.admissions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    output = checked_path(ROOT / args.output_dir)
    if not output.is_relative_to(ROOT / 'verification') or output.exists():
        raise ValueError('USE_NEW_VERIFICATION_DIRECTORY')
    output.mkdir(parents=True)
    sources = ['src/workflow/remote.py', 'src/workflow/bounded_http.py', 'src/runtime/io_budget.py',
               'src/lab/kafka/analysis_consumer.py', 'src/lab/kafka/analysis_store.py',
               'qa/tools/qualify_protocol.py', 'qa/tools/qualify_protocol_fix.py']

    def hashes():
        return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}

    before = hashes()
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('EXTERNAL_IO_FORBIDDEN')), \
         patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('EXTERNAL_IO_FORBIDDEN')), \
         patch.object(socket, 'create_connection', side_effect=AssertionError('EXTERNAL_IO_FORBIDDEN')):
        cases = [scenario(stage, fault) for stage in ('queued', 'submitted')
                 for fault in ('malformed_json', 'invalid_job_shape')]
    unchanged = before == hashes()
    result = {'case_id': 'QA-022', 'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'PASS' if unchanged and all(case['status'] == 'PASS' for case in cases) else 'FAIL',
              'cases': cases, 'source_sha256': before, 'sources_unchanged_during_run': unchanged,
              'scope': 'Real WorkflowClient parser, scheduler and worker loop; in-memory HTTP, queue and idle broker. '
                       'One protocol attempt stops each worker; separate explicit repaired restart. No live '
                       'PostgreSQL/Kafka/API, models or browser; socket connect/connect_ex/create_connection forbidden.',
              'limitations': 'HTTP/server persistence and operator repair are controlled doubles. '
                             'This is not real broker delivery, crash durability or live service qualification.'}
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output / 'summary.md').write_text(
        '# QA-022: ' + result['status'] + '\n\n'
        'Четыре случая: queued/submitted × повреждённый JSON/неверная форма задания. '
        'В каждом worker делает одну HTTP попытку и останавливается с WORKFLOW_PROTOCOL_ERROR; '
        'сохраняются event, payload, состояние очереди и присвоенный job_id. '
        'После имитации ремонта новый worker восстанавливает обе исходные строки. '
        'Неопределённый ответ admission повторяется с прежним ключом без создания второго задания.\n\n'
        'Границы: реальные parser/scheduler/worker, HTTP/очередь/broker подменены; сеть запрещена. '
        'Это не live-проверка Kafka, PostgreSQL или моделей.\n\n[Машинное свидетельство](result.json).\n',
        encoding='utf-8')
    print(json.dumps({'status': result['status'], 'cases': len(cases),
                      'failed_checks': [{'stage': case['stage'], 'fault': case['fault'],
                                         'checks': [name for name, passed in case['checks'].items() if not passed]}
                                        for case in cases if case['status'] != 'PASS'],
                      'evidence': str(output / 'result.json')}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
