"""Bounded QA-022 reproduction: real HTTP parser/scheduler, in-memory I/O doubles.

This command does not start services or access PostgreSQL/Kafka. Four failed
attempts document observed retries; unbounded retry is a source-review finding,
not an inference of infinity from those four observations.
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

from src.lab.kafka.analysis_consumer import scheduler_tick, WorkerFailure
from src.lab.kafka.analysis_contracts import AnalysisEvent
from src.workflow.auth import Principal
from src.workflow.remote import WorkflowClient

EVENT_ID = '7588866f-f6a9-446f-8d1d-f3ebedcf4c01'
HEALTHY_ID = '7588866f-f6a9-446f-8d1d-f3ebedcf4c02'
ACTOR = Principal('qa-protocol-operator')


def event(identifier=EVENT_ID):
    return AnalysisEvent.model_validate({
        'event_id': identifier, 'occurred_at': '2026-10-17T10:00:00Z',
        'incident_id': 'INC-000123', 'schema_version': 1,
        'event_type': 'aml.analysis.requested.v1', 'synthetic': True, 'engine': 'template',
        'window': {'start_inclusive': '2026-07-01T00:00:00Z',
                   'end_exclusive': '2026-07-20T00:00:00Z', 'as_of': '2026-07-20T00:00:00Z'},
    })


def job(identifier):
    return {'schema_version': '1.0', 'job_id': identifier, 'incident_id': 'INC-000123',
            'revision': 0, 'accepted_at': '2026-10-17T10:00:00Z',
            'deadline_at': '2026-10-17T10:03:00Z', 'status': 'queued'}


class Queue:
    """Minimal deterministic queue double preserving identity and fair rotation."""
    def __init__(self):
        self.rows, self.sequence = {}, 0

    def touch(self, row):
        self.sequence += 1
        row['order'] = self.sequence

    def add(self, item):
        row = {'event_id': str(item.event_id), 'event_json': item.model_dump(mode='json'),
               'status': 'queued', 'job_id': None, 'job_json': None, 'last_error_code': None}
        self.touch(row)
        self.rows[row['event_id']] = row

    def work(self, status, limit):
        return deepcopy(sorted((row for row in self.rows.values() if row['status'] == status),
                               key=lambda row: row['order'])[:limit])

    def pending(self, limit=1):
        return self.work('queued', limit)

    def submitted(self, limit=1):
        return self.work('submitted', limit)

    def defer(self, identifier, code):
        row = self.rows[identifier]
        row['last_error_code'] = code
        self.touch(row)
        return deepcopy(row)

    def reject(self, identifier, code):
        self.rows[identifier]['status'] = 'rejected'
        return self.defer(identifier, code)

    def record_job(self, identifier, value):
        row = self.rows[identifier]
        row.update(job_id=value['job_id'], job_json=deepcopy(value),
                   status='submitted' if value['status'] in ('queued', 'running') else value['status'],
                   last_error_code=None)
        self.touch(row)
        return deepcopy(row)


class Transport:
    def __init__(self, raw, status):
        self.raw, self.status, self.calls = raw, status, []

    def open(self, request, *, timeout):
        key = request.get_header('Idempotency-key')
        poison = key == event().idempotency_key or request.full_url.endswith('/job-protocol-original')
        self.calls.append({'method': request.method, 'event_id': EVENT_ID if poison else HEALTHY_ID,
                           'idempotency_key': key, 'timeout_seconds': timeout,
                           'job_id': request.full_url.rsplit('/', 1)[1] if request.method == 'GET' else None})
        if poison:
            response = BytesIO(self.raw)
            response.status = self.status
        else:
            value = job('job-protocol-healthy')
            if request.method == 'GET':
                value.update(status='cancelled', revision=1, completed_at='2026-10-17T10:00:01Z')
            response = BytesIO(json.dumps(value).encode())
            response.status = 202 if request.method == 'POST' else 200
        response.headers = {}
        return response


def scenario(stage, fault):
    queue = Queue()
    queue.add(event())
    if stage == 'submitted':
        queue.record_job(EVENT_ID, job('job-protocol-original'))
    queue.add(event(HEALTHY_ID))
    before = deepcopy(queue.rows[EVENT_ID])
    raw = b'{' if fault == 'malformed_json' else b'{"job_id":"unexpected","status":"running"}'
    status = 200 if stage == 'submitted' or fault == 'malformed_json' else 202
    transport = Transport(raw, status)
    client = WorkflowClient({'api_token': 'a' * 64}, transport=transport)
    ticks = []
    # One extra tick serves the healthy row after fair rotation in either stage.
    for number in range(1, 6):
        try:
            progress = scheduler_tick(queue, client, ACTOR)
            stop_code = None
        except WorkerFailure as exc:
            progress, stop_code = [], exc.code
        ticks.append({'tick': number, 'progress': progress, 'stop_code': stop_code,
                      'poison_status': queue.rows[EVENT_ID]['status'],
                      'last_error_code': queue.rows[EVENT_ID]['last_error_code']})
        if stop_code:
            break
    row = queue.rows[EVENT_ID]
    attempts = [call for call in transport.calls if call['event_id'] == EVENT_ID]
    codes = [item['code'] for tick in ticks for item in tick['progress'] if item.get('event_id') == EVENT_ID]
    checks = {
        'exactly_four_protocol_attempts': len(attempts) == 4,
        'all_protocol_attempts_misclassified_not_ready': codes == ['NOT_READY'] * 4,
        'same_event_payload_retained': row['event_json'] == before['event_json'],
        'same_job_reference_retained': row['job_id'] == before['job_id'],
        'same_queue_state_retained': row['status'] == stage,
        'healthy_row_processed_despite_retries': queue.rows[HEALTHY_ID]['status'] == 'cancelled',
        'same_idempotency_key_on_every_submit': all(call['idempotency_key'] == event().idempotency_key
            for call in attempts if call['method'] == 'POST'),
        'no_scheduler_escalation': all(tick['stop_code'] is None for tick in ticks),
        'exactly_two_original_rows': set(queue.rows) == {EVENT_ID, HEALTHY_ID},
    }
    return {'stage': stage, 'fault': fault, 'response_status': status,
            'observed_protocol_attempts': len(attempts), 'checks': checks, 'ticks': ticks,
            'http_calls': transport.calls, 'final_poison': row,
            'baseline_reproduced': all(checks.values()), 'target_policy_passed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    output = (ROOT / args.output_dir).resolve()
    if not output.is_relative_to(ROOT / 'verification') or output.exists():
        raise ValueError('USE_NEW_VERIFICATION_DIRECTORY')
    output.mkdir(parents=True)
    sources = ['src/workflow/remote.py', 'src/lab/kafka/analysis_consumer.py',
               'src/lab/kafka/analysis_store.py', 'qa/tools/qualify_protocol.py']
    def hashes():
        return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}
    before = hashes()
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('EXTERNAL_IO_FORBIDDEN')), \
         patch.object(socket, 'create_connection', side_effect=AssertionError('EXTERNAL_IO_FORBIDDEN')):
        cases = [scenario(stage, fault) for stage in ('queued', 'submitted')
                 for fault in ('malformed_json', 'invalid_job_shape')]
    unchanged = before == hashes()
    result = {'case_id': 'QA-022', 'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'BASELINE_REPRODUCED' if unchanged and all(case['baseline_reproduced'] for case in cases)
                        else 'BASELINE_NOT_REPRODUCED',
              'target_policy_status': 'FAIL', 'cases': cases,
              'source_sha256': before, 'sources_unchanged_during_run': unchanged,
              'scope': 'Real WorkflowClient parser and scheduler; in-memory HTTP/queue doubles; '
                       'no live services, PostgreSQL, Kafka, model or browser.',
              'limitation': 'Four bounded attempts demonstrate repeated NOT_READY. Absence of a retry '
                            'limit follows source inspection; this run does not measure infinite time.'}
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'target_policy_status': 'FAIL',
                      'cases': len(cases), 'attempts': [case['observed_protocol_attempts'] for case in cases],
                      'evidence': str(output / 'result.json')}))
    return 0 if result['status'] == 'BASELINE_REPRODUCED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
