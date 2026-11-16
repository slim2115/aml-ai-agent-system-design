"""Hermetic QA-023 baseline: real scheduler/client, virtual slow I/O.

No service starts or sockets are opened. A fake monotonic clock measures the
operation sequence, not elapsed production latency. The baseline intentionally
demonstrates the old missing total budget; it is not a product PASS.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.lab.kafka.analysis_consumer import scheduler_tick
from src.lab.kafka.analysis_contracts import AnalysisEvent
from src.workflow.auth import Principal
from src.workflow.remote import WorkflowClient


FIRST = '5884e14d-67f3-4f29-8243-a2666203e2ad'
SECOND = '5884e14d-67f3-4f29-8243-a2666203e2ae'
SOURCES = ('src/lab/kafka/analysis_consumer.py', 'src/workflow/remote.py',
           'src/lab/kafka/analysis_store.py', 'src/lab/database.py',
           'src/lab/kafka/broker.py', 'qa/tools/qualify_deadline.py')


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def event(identifier):
    return AnalysisEvent.model_validate({
        'event_id': identifier, 'occurred_at': '2026-10-04T12:00:00Z',
        'incident_id': 'INC-000123', 'schema_version': 1,
        'event_type': 'aml.analysis.requested.v1', 'synthetic': True, 'engine': 'template',
        'window': {'start_inclusive': '2026-07-01T00:00:00Z',
                   'end_exclusive': '2026-07-20T00:00:00Z', 'as_of': '2026-07-20T00:00:00Z'},
    })


def job(identifier):
    return {'schema_version': '1.0', 'job_id': identifier, 'incident_id': 'INC-000123',
            'revision': 1, 'status': 'queued', 'accepted_at': '2026-10-04T12:00:00Z',
            'deadline_at': '2026-10-04T12:03:00Z'}


class Clock:
    def __init__(self):
        self.now = 0.0
        self.trace = []

    def __call__(self):
        return self.now

    def advance(self, operation, seconds):
        start = self.now
        self.now += seconds
        self.trace.append({'operation': operation, 'started_at_seconds': start,
                           'finished_at_seconds': self.now, 'duration_seconds': seconds})


class Queue:
    def __init__(self, clock):
        self.clock = clock
        self.rows = {
            FIRST: {'event_id': FIRST, 'event_json': event(FIRST).model_dump(mode='json'),
                    'status': 'submitted', 'job_id': 'job-first'},
            SECOND: {'event_id': SECOND, 'event_json': event(SECOND).model_dump(mode='json'),
                     'status': 'queued', 'job_id': None},
        }

    def _read(self, status, limit):
        self.clock.advance('db.' + status, 3.0)
        return deepcopy([row for row in self.rows.values() if row['status'] == status][:limit])

    def submitted(self, limit=1):
        return self._read('submitted', limit)

    def pending(self, limit=1):
        return self._read('queued', limit)

    def record_job(self, event_id, payload):
        self.clock.advance('db.record_job.commit', 3.0)
        self.rows[event_id].update(status='submitted', job_id=payload['job_id'],
                                   job_json=deepcopy(payload))
        return deepcopy(self.rows[event_id])


class SlowResponse:
    def __init__(self, clock, payload, status):
        self.clock = clock
        self.raw = json.dumps(payload).encode('utf-8')
        self.status = status
        self.headers = {'Content-Length': str(len(self.raw))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        # Model eight sequential available chunks, each arriving before the
        # five-second socket timeout. Current read(N) has no elapsed-time check.
        for _ in range(8):
            self.clock.advance('http.body.chunk', 4.0)
        return self.raw[:limit]


class Transport:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append({'method': request.method, 'socket_timeout_seconds': timeout,
                           'idempotency_key': request.get_header('Idempotency-key')})
        return SlowResponse(self.clock, job('job-first' if request.method == 'GET' else 'job-second'),
                            200 if request.method == 'GET' else 202)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    output = (ROOT / args.output_dir).resolve()
    allowed = (ROOT / 'verification/qa-step-05-2026-10-17').resolve()
    if output.parent != allowed or output.exists():
        raise SystemExit('Use a new immediate child of verification/qa-step-05-2026-10-17')
    before = hashes()
    clock = Clock()
    queue = Queue(clock)
    transport = Transport(clock)
    client = WorkflowClient({'api_token': 'a' * 64}, timeout=5, transport=transport)
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('NO_NETWORK_ALLOWED')):
        progress = scheduler_tick(queue, client, Principal('qa-deadline-baseline'))
    checks = {
        'exactly_two_http_calls': len(transport.calls) == 2,
        'individual_chunks_below_socket_timeout': all(
            item['duration_seconds'] < 5 for item in clock.trace if item['operation'] == 'http.body.chunk'),
        'illustrative_10_second_budget_exceeded': clock() > 10,
        'configured_60_second_poll_interval_exceeded': clock() > 60,
        'two_durable_requests_preserved': set(queue.rows) == {FIRST, SECOND},
        'submission_uses_original_event_key': transport.calls[1]['idempotency_key'] == event(SECOND).idempotency_key,
        'both_projections_completed': [item['status'] for item in progress] == ['projected', 'submitted'],
        'sources_unchanged': before == hashes(),
    }
    result = {
        'case': 'QA-023', 'status': 'LIMITATION_REPRODUCED' if all(checks.values()) else 'UNEXPECTED_RESULT',
        'recorded_at': datetime.now(timezone.utc).isoformat(), 'checks': checks,
        'fake_elapsed_seconds': clock(), 'illustrative_budget_seconds': 10,
        'configured_max_poll_interval_seconds': 60, 'http_calls': transport.calls,
        'trace': clock.trace, 'progress': progress,
        'queue': [{'event_id': row['event_id'], 'status': row['status'], 'job_id': row['job_id']}
                  for row in queue.rows.values()],
        'source_sha256': before,
        'scope': {'real': ['scheduler_tick', 'WorkflowClient', 'AnalysisEvent', 'job validation'],
                  'doubles': ['PostgreSQL queue', 'monotonic clock', 'HTTP transport and response'],
                  'not_measured': ['real wall latency', 'Kafka rebalance frequency', 'network timing'],
                  'product_pass': False},
    }
    output.mkdir(parents=True, exist_ok=False)
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'fake_elapsed_seconds': clock(),
                      'checks': checks, 'output': str(output / 'result.json')}, ensure_ascii=False))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
