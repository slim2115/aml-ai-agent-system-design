"""Bounded environment schedules around real intake/scheduler/client/SQLite code.

Kafka delivery, PostgreSQL queue persistence and HTTP failures are explicit local
models. A terminal refusal is a contract-valid control, not AML generation.
"""
from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from src.lab.database import LabDatabaseError
from src.lab.kafka.analysis_contracts import AnalysisEvent, DLQ_TOPIC, TOPIC
from src.lab.kafka.analysis_consumer import process_one, scheduler_tick
from src.lab.kafka.consumer import WorkerFailure
from src.lab.kafka.contracts import Record
from src.lab.kafka.store import EventConflict
from src.workflow.auth import Principal
from src.workflow.remote import WorkflowClient
from src.workflow.store import SQLiteStore, utc_now


SCENARIO_IDS = (
    'baseline', 'db-recovery', 'offset-redelivery', 'http-ack-replay',
    'projection-replay', 'budget-reconcile', 'not-found-recovery',
    'protocol-recovery', 'dlq-recovery',
)
REQUIRED_INVARIANTS = (
    'durable_receipt_before_offset', 'stable_idempotency',
    'no_admission_after_budget',
)
REAL_DEPENDENCIES = (
    'analysis_consumer.process_one', 'analysis_consumer.scheduler_tick',
    'WorkflowClient', 'SQLiteStore', 'v1 contract validation',
)
MODELED_DEPENDENCIES = (
    'Kafka delivery and acknowledgements', 'PostgreSQL queue persistence',
    'HTTP transport and faults', 'monotonic clock', 'terminal synthetic refusal',
)
_ACTOR = Principal('environment-model-operator')
_FAULTS = {
    'receive': {'db_unavailable', 'offset_ack_lost', 'dlq_unavailable'},
    'tick': {'http_ack_lost', 'projection_unavailable', 'budget_after_admission',
             'get_not_found', 'protocol_error'},
    'finish': set(), 'restart': set(),
}
_STATUSES = {None, 'queued', 'submitted', 'running', 'refused', 'rejected'}
_ERRORS = {None, 'NOT_READY', 'NOT_FOUND', 'PROTOCOL_ERROR'}
_OUTCOMES = {
    'queued', 'duplicate', 'submitted', 'projected', 'refused', 'restarted',
    'dead_lettered', 'deferred:NOT_READY',
    'budget_exhausted:SCHEDULER_BUDGET_EXHAUSTED',
    'failure:DATABASE_UNAVAILABLE', 'failure:OFFSET_COMMIT_FAILED',
    'failure:DLQ_PUBLISH_FAILED', 'failure:WORKFLOW_JOB_UNAVAILABLE',
    'failure:WORKFLOW_PROTOCOL_ERROR',
}
_PROJECTION_KEYS = {
    'action', 'outcome', 'queue_status', 'queue_error', 'job_status',
    'job_count', 'submit_calls', 'key_count', 'offset_acks', 'dlq_acks',
}


def _text(value, maximum=600):
    return isinstance(value, str) and 0 < len(value) <= maximum and value.strip() == value


def _validate_scenario(scenario):
    if (not isinstance(scenario, dict) or set(scenario) != {
            'id', 'title', 'purpose', 'limitations', 'requirements', 'steps', 'expected'}
            or scenario['id'] not in SCENARIO_IDS
            or not _text(scenario['title']) or not _text(scenario['purpose'])):
        raise ValueError('INVALID_ENVIRONMENT_SCENARIO')
    for name in ('limitations', 'requirements'):
        values = scenario[name]
        if (not isinstance(values, list) or not 1 <= len(values) <= 12
                or any(not _text(value) for value in values)
                or len(set(values)) != len(values)):
            raise ValueError('INVALID_ENVIRONMENT_METADATA')
    if any(re.fullmatch(r'DFR-[0-9]{3}', item) is None for item in scenario['requirements']):
        raise ValueError('INVALID_ENVIRONMENT_REQUIREMENT')
    steps, expected = scenario['steps'], scenario['expected']
    if (not isinstance(steps, list) or not 1 <= len(steps) <= 12
            or not isinstance(expected, list) or len(expected) != len(steps)):
        raise ValueError('INVALID_ENVIRONMENT_STEPS')
    for step, projection in zip(steps, expected):
        if (not isinstance(step, dict) or not {'action'} <= set(step)
                or not set(step) <= {'action', 'fault', 'invalid'}
                or not isinstance(step['action'], str) or step['action'] not in _FAULTS):
            raise ValueError('INVALID_ENVIRONMENT_ACTION')
        action = step['action']
        if ('fault' in step and (not isinstance(step['fault'], str)
                                or step['fault'] not in _FAULTS[action])):
            raise ValueError('INVALID_ENVIRONMENT_FAULT')
        if 'invalid' in step and (action != 'receive' or type(step['invalid']) is not bool):
            raise ValueError('INVALID_ENVIRONMENT_INPUT')
        if (not isinstance(projection, dict) or set(projection) != _PROJECTION_KEYS
                or projection['action'] != action
                or not isinstance(projection['outcome'], list)
                or not 1 <= len(projection['outcome']) <= 2
                or any(not isinstance(item, str) or item not in _OUTCOMES
                       for item in projection['outcome'])):
            raise ValueError('INVALID_ENVIRONMENT_EXPECTATION')
        for name in ('queue_status', 'job_status'):
            if projection[name] is not None and not isinstance(projection[name], str):
                raise ValueError('INVALID_ENVIRONMENT_EXPECTATION')
            if projection[name] not in _STATUSES:
                raise ValueError('INVALID_ENVIRONMENT_EXPECTATION')
        if (projection['queue_error'] is not None and not isinstance(projection['queue_error'], str)
                or projection['queue_error'] not in _ERRORS):
            raise ValueError('INVALID_ENVIRONMENT_EXPECTATION')
        for name in ('job_count', 'submit_calls', 'key_count', 'offset_acks', 'dlq_acks'):
            if type(projection[name]) is not int or not 0 <= projection[name] <= 12:
                raise ValueError('INVALID_ENVIRONMENT_EXPECTATION')
    return scenario


def validate_catalog(catalog):
    """Accept only the finite v1 schedule DSL; never resolve code or input paths."""
    if (not isinstance(catalog, dict) or set(catalog) != {'schema_version', 'catalog_id', 'scenarios'}
            or catalog['schema_version'] != '1.0'
            or catalog['catalog_id'] != 'environment-scenarios-v1'
            or not isinstance(catalog['scenarios'], list)
            or len(catalog['scenarios']) != len(SCENARIO_IDS)):
        raise ValueError('INVALID_ENVIRONMENT_CATALOG')
    ids = [_validate_scenario(item)['id'] for item in catalog['scenarios']]
    if tuple(ids) != SCENARIO_IDS:
        raise ValueError('INVALID_ENVIRONMENT_SCENARIO_IDS')
    return catalog


def _event():
    return AnalysisEvent.model_validate({
        'event_id': '5884e14d-67f3-4f29-8243-a2666203e2ad',
        'occurred_at': '2026-10-04T12:00:00Z', 'incident_id': 'INC-000123',
        'schema_version': 1, 'event_type': 'aml.analysis.requested.v1',
        'synthetic': True, 'engine': 'template',
        'window': {'start_inclusive': '2026-07-01T00:00:00Z',
                   'end_exclusive': '2026-07-20T00:00:00Z', 'as_of': '2026-07-20T00:00:00Z'},
    })


class _Environment:
    def __init__(self, workspace):
        self.path = workspace / 'workflow.sqlite3'
        self.sqlite = SQLiteStore(self.path)
        self.trace, self.keys, self.admitted_ids = [], [], []
        self.step, self.now = 0, 0.0
        self.fault, self.consumed = None, False
        self.offset_acks = self.dlq_acks = 0
        self.queue = _Queue(self)
        self.broker = _Broker(self)
        self.client = self.new_client()

    def call(self, name):
        if len(self.trace) >= 512:
            raise RuntimeError('ENVIRONMENT_TRACE_LIMIT')
        self.trace.append({'step': self.step, 'call': name})

    def hit(self, name):
        if self.fault == name and not self.consumed:
            self.consumed = True
            self.call('fault.' + name)
            return True
        return False

    def new_client(self):
        return WorkflowClient({'api_token': 'a' * 64}, transport=_Transport(self))

    def project(self, action, outcome):
        jobs = self.sqlite.list_jobs(_ACTOR)
        row = self.queue.row
        return {'action': action, 'outcome': outcome,
                'queue_status': row['status'] if row else None,
                'queue_error': row['last_error_code'] if row else None,
                'job_status': jobs[0]['status'] if len(jobs) == 1 else None,
                'job_count': len(jobs), 'submit_calls': len(self.keys),
                'key_count': len(set(self.keys)), 'offset_acks': self.offset_acks,
                'dlq_acks': self.dlq_acks}


class _Queue:
    """One modeled durable PostgreSQL row; never opens a PG connection."""
    def __init__(self, env):
        self.env, self.row = env, None

    def enqueue(self, event, record):
        self.env.call('db.enqueue')
        if self.env.hit('db_unavailable'):
            raise LabDatabaseError('DATABASE_UNAVAILABLE')
        payload = event.model_dump(mode='json')
        if self.row is not None and self.row['event_json'] != payload:
            raise EventConflict()
        status = 'duplicate' if self.row else 'queued'
        if self.row is None:
            self.row = {'event_id': str(event.event_id), 'event_json': payload,
                        'status': 'queued', 'job_id': None, 'job_json': None,
                        'last_error_code': None}
        self.env.call('db.receipt')
        return {'status': status}

    def _rows(self, status, limit):
        self.env.call('db.read.' + status)
        if limit != 1:
            raise RuntimeError('UNBOUNDED_QUEUE_READ')
        return [deepcopy(self.row)] if self.row and self.row['status'] == status else []

    def pending(self, *, limit):
        return self._rows('queued', limit)

    def submitted(self, *, limit):
        return self._rows('submitted', limit)

    def record_job(self, event_id, job):
        self.env.call('db.project')
        if self.env.hit('projection_unavailable'):
            raise LabDatabaseError('DATABASE_UNAVAILABLE')
        if self.row is None or self.row['event_id'] != event_id:
            raise RuntimeError('UNKNOWN_ENVIRONMENT_EVENT')
        self.row.update(job_id=job['job_id'], job_json=deepcopy(job), last_error_code=None,
                        status='submitted' if job['status'] in ('queued', 'running') else job['status'])
        self.env.call('db.projection.receipt')

    def defer(self, event_id, code):
        self.env.call('db.defer')
        if self.row is None or self.row['event_id'] != event_id:
            raise RuntimeError('UNKNOWN_ENVIRONMENT_EVENT')
        self.row['last_error_code'] = code

    def reject(self, event_id, code):
        self.defer(event_id, code)
        self.row['status'] = 'rejected'


class _Broker:
    def __init__(self, env):
        self.env = env

    def commit(self, record):
        self.env.call('offset.attempt')
        if self.env.hit('offset_ack_lost'):
            raise OSError('SCRIPTED_OFFSET_ACK_LOSS')
        self.env.offset_acks += 1
        self.env.call('offset.ack')

    def publish(self, topic, *, key, value):
        self.env.call('dlq.publish')
        if topic != DLQ_TOPIC or not isinstance(key, bytes) or not isinstance(value, bytes):
            raise RuntimeError('INVALID_ENVIRONMENT_DLQ')
        if self.env.hit('dlq_unavailable'):
            raise OSError('SCRIPTED_DLQ_UNAVAILABLE')
        self.env.dlq_acks += 1
        self.env.call('dlq.ack')


def _response(payload, status):
    response = BytesIO(json.dumps(payload).encode('utf-8'))
    response.status, response.headers = status, {}
    return response


class _Transport:
    """Scripted urllib transport: request handling touches only the new SQLite."""
    def __init__(self, env):
        self.env = env

    def open_budgeted(self, request, *, timeout, deadline):
        deadline.check()
        response = self.open(request, timeout=timeout)
        try:
            deadline.check()
        except BaseException:
            response.close()
            raise
        return response

    def open(self, request, *, timeout):
        env = self.env
        if not 0 < timeout <= 10:
            raise RuntimeError('INVALID_ENVIRONMENT_TIMEOUT')
        parsed = urlsplit(request.full_url)
        store = SQLiteStore(env.path)
        if request.method == 'POST':
            if parsed.path != '/v1/jobs' or parsed.query != 'demo=true&engine=template':
                raise RuntimeError('UNEXPECTED_ENVIRONMENT_REQUEST')
            env.call('http.submit')
            key = request.get_header('Idempotency-key')
            env.keys.append(key)
            payload = store.create_job(json.loads(request.data), _ACTOR, key, engine='template')
            env.admitted_ids.append(payload['job_id'])
            env.call('sqlite.admission.receipt')
            if env.hit('http_ack_lost'):
                raise TimeoutError('SCRIPTED_ACK_LOSS')
            if env.hit('budget_after_admission'):
                env.now += 11.0
                env.call('clock.expired')
            status = 202
        elif request.method == 'GET' and parsed.path.startswith('/v1/jobs/'):
            env.call('http.get')
            if env.hit('get_not_found'):
                return _response({'schema_version': '1.0', 'status': 'error',
                    'generated_at': utc_now(), 'correlation_id': 'environment-not-found',
                    'code': 'NOT_FOUND', 'message': 'Scripted unavailable job reference.',
                    'retryable': False}, 404)
            payload, status = store.get_job(parsed.path.rsplit('/', 1)[1], _ACTOR), 200
        else:
            raise RuntimeError('UNEXPECTED_ENVIRONMENT_REQUEST')
        if env.hit('protocol_error'):
            payload = {}
        return _response(payload, status)


def _finish(env):
    accepted, = env.sqlite.list_jobs(_ACTOR)
    running = env.sqlite.transition_job(accepted['job_id'], accepted['revision'],
        dict(accepted, status='running', revision=accepted['revision'] + 1))
    now = utc_now()
    env.sqlite.transition_job(running['job_id'], running['revision'],
        dict(running, status='refused', revision=running['revision'] + 1, completed_at=now,
             result={'schema_version': '1.0', 'status': 'refusal', 'incident_id': 'INC-000123',
                     'generated_at': now, 'data_completeness': 'unassessed',
                     'reason_code': 'POLICY_NOT_APPROVED',
                     'reason': 'Synthetic environment-model terminal control.',
                     'recommended_action': 'manual_review'}))
    env.call('sqlite.terminal.refused')


def _invariants(env, steps):
    receipt_before_offset, bounded = True, True
    for number in range(len(steps)):
        calls = [item['call'] for item in env.trace if item['step'] == number]
        for index, call in enumerate(calls):
            if call == 'offset.attempt':
                receipt_before_offset &= any(item in ('db.receipt', 'dlq.ack') for item in calls[:index])
            if call == 'clock.expired':
                bounded &= not any(item.startswith(('db.', 'http.', 'sqlite.admission'))
                                   for item in calls[index + 1:])
    stable = (len(set(env.keys)) <= 1 and all(key == _event().idempotency_key for key in env.keys)
              and len(set(env.admitted_ids)) <= 1 and all(step['job_count'] <= 1 for step in steps))
    return [{'id': REQUIRED_INVARIANTS[0], 'passed': bool(receipt_before_offset)},
            {'id': REQUIRED_INVARIANTS[1], 'passed': bool(stable)},
            {'id': REQUIRED_INVARIANTS[2], 'passed': bool(bounded)}]


def execute(scenario, workspace: Path):
    """Run a validated bounded schedule in an existing, empty, isolated folder."""
    _validate_scenario(scenario)
    workspace = Path(workspace)
    if not workspace.is_dir() or any(workspace.iterdir()):
        raise ValueError('ENVIRONMENT_WORKSPACE_NOT_EMPTY')
    # SQLiteStore separately enforces permitted workflow containment and no links.
    env, steps = _Environment(workspace), []
    event = _event()
    for number, step in enumerate(scenario['steps']):
        env.step, env.fault, env.consumed = number, step.get('fault'), False
        action = step['action']
        env.call('action.' + action)
        try:
            if action == 'receive':
                record = Record(TOPIC, 0, 7, event.incident_id.encode('utf-8'),
                                b'{' if step.get('invalid', False) else event.canonical_bytes())
                outcome = [process_one(record, env.queue, env.broker)['status']]
            elif action == 'tick':
                results = scheduler_tick(env.queue, env.client, _ACTOR, clock=lambda: env.now)
                outcome = [item['status'] + (':' + item['code'] if 'code' in item else '') for item in results]
            elif action == 'finish':
                _finish(env)
                outcome = ['refused']
            else:
                env.sqlite = SQLiteStore(env.path)
                env.client = env.new_client()
                outcome = ['restarted']
        except WorkerFailure as exc:
            outcome = ['failure:' + exc.code]
        if env.fault is not None and not env.consumed:
            raise ValueError('ENVIRONMENT_FAULT_NOT_REACHED')
        steps.append(env.project(action, outcome))
    return {'steps': steps, 'invariants': _invariants(env, steps), 'trace': env.trace,
            'dependencies': {'real': list(REAL_DEPENDENCIES), 'modeled': list(MODELED_DEPENDENCIES)}}
