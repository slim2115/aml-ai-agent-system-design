"""HTTP boundary controls; no socket, PostgreSQL server, embedding or Ollama."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import socket
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from src.config import get_settings
from src.lab.api import create_app, wire, NoteCreate, NoteUpdate, Readiness, Health, DatabaseStatus
from src.contracts.v1 import CreateJobRequest, ErrorResult
from src.lab.database import LabDatabaseError
from src.lab.kafka.contracts import KafkaErrorBody
from src.runtime.policy import load_demo_policy
from src.workflow.auth import trusted_local_principal
from src.workflow.controller import WorkflowService
from src.workflow.store import SQLiteStore, WORKFLOW_ROOT, utc_now
from src.workflow.worker import ExecutionOutcome
from tests.evaluator.fixtures import bundle

TOKEN = 'a' * 64
SETTINGS = {'api_token': TOKEN}
AUTH = {'X-Lab-Key': TOKEN}
_native_connect = socket.socket.connect
_native_socketpair = socket.socketpair


@pytest.fixture(autouse=True)
def event_loop_socketpair_only(deny_network_by_default, monkeypatch):
    """Windows asyncio needs its own socketpair; application connects stay denied."""
    internal = threading.local()
    denied_connect = socket.socket.connect
    def connect(sock, address):
        if getattr(internal, 'pair', False):
            return _native_connect(sock, address)
        return denied_connect(sock, address)
    def socketpair(*args, **kwargs):
        internal.pair = True
        try:
            return _native_socketpair(*args, **kwargs)
        finally:
            internal.pair = False
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket, 'socketpair', socketpair)


class QueryRecorder:
    def __init__(self):
        self.calls = []
        self.records = []
        self.failure = None
    @contextmanager
    def connect(self, role):
        assert role == 'api'
        if self.failure:
            raise self.failure
        yield self
    def execute(self, sql, parameters=()):
        self.calls.append((sql, parameters))
        return self
    def fetchall(self):
        return self.records


class FakeWorker:
    def __init__(self):
        self.is_ready = False
        self.delay = .05
        self.calls = 0
    def start(self, timeout_seconds=180):
        self.is_ready = True
        return {'ready': True, 'code': 'READY'}
    def execute(self, incident_id, engine='template', timeout_seconds=180, cancel_event=None, **kwargs):
        self.calls += 1
        if cancel_event.wait(self.delay):
            return ExecutionOutcome('cancelled')
        return ExecutionOutcome('result', result={
            'schema_version': '1.0', 'status': 'refusal', 'incident_id': incident_id,
            'generated_at': utc_now(), 'data_completeness': 'unassessed',
            'reason_code': 'POLICY_NOT_APPROVED', 'reason': 'Synthetic test', 'recommended_action': 'manual_review'})
    def close(self):
        self.is_ready = False


def request():
    policy = load_demo_policy(get_settings().project_root / 'config/demo-policy.json')
    return {'schema_version': '1.0', 'incident_id': 'INC-000123', 'window': policy.window.model_dump()}


def until(predicate):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('TEST_TIMEOUT')


@pytest.fixture
def http():
    database = QueryRecorder()
    worker = FakeWorker()
    with WorkflowService(SQLiteStore(WORKFLOW_ROOT / 'test-lab-api' / uuid4().hex / 'workflow.sqlite3'), worker=worker) as service:
        app = create_app(SETTINGS, workflow=service, database=database)
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, database, service


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    data = response.json()
    assert data['status'] == 'error' and data['code'] == code
    assert set(data) == {'schema_version', 'status', 'generated_at', 'correlation_id', 'code', 'message', 'retryable'}


def test_docs_liveness_and_sql_reads_never_construct_worker(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('MODEL_CONTROLLER_CONSTRUCTED')
    monkeypatch.setattr('src.lab.api.WorkflowService', forbidden)
    app = create_app(SETTINGS, database=QueryRecorder())
    with TestClient(app) as client:
        assert client.get('/health').json()['check'] == 'http_liveness_only'
        docs = client.get('/docs').text
        assert '/static/swagger-ui-bundle.js' in docs
        assert '/static/swagger-ui.css' in docs
        assert 'cdn.jsdelivr' not in docs and 'unpkg.com' not in docs
        assert '"validatorUrl": null' in docs
        assert client.get('/lab/clients', headers=AUTH).json()['items'] == []


@pytest.mark.parametrize('path', ['/lab/clients', '/lab/cards', '/lab/rules', '/lab/transactions', '/lab/notes', '/lab/status', '/v1/readiness', '/v1/jobs'])
@pytest.mark.parametrize('headers', [{}, {'X-Lab-Key': 'wrong'}])
def test_all_data_endpoints_require_key(http, path, headers):
    client, database, service = http
    assert_error(client.get(path, headers=headers), 401, 'UNAUTHORIZED')
    assert database.calls == [] and service.worker.calls == 0


def test_openapi_describes_auth_money_and_actual_error_responses(http):
    client, _, _ = http
    spec = client.get('/openapi.json').json()
    assert TOKEN not in client.get('/openapi.json').text
    assert spec['components']['securitySchemes']['APIKeyHeader']['name'] == 'X-Lab-Key'
    assert spec['components']['schemas']['Transaction']['properties']['amount']['type'] == 'string'
    for path, methods in spec['paths'].items():
        if path.startswith(('/lab/', '/v1/')):
            for operation in methods.values():
                assert operation['security'] == [{'APIKeyHeader': []}]
                assert '400' in operation['responses'] and '422' not in operation['responses']


def test_swagger_examples_are_valid_and_match_demo_window(http):
    client, _, _ = http
    spec = client.get('/openapi.json').json()
    schemas = spec['components']['schemas']
    for model in (CreateJobRequest, NoteCreate, NoteUpdate, Readiness, Health, DatabaseStatus, ErrorResult):
        model.model_validate(schemas[model.__name__]['examples'][0])
    assert schemas['CreateJobRequest']['examples'][0] == request()
    def verify_timestamps(node):
        if isinstance(node, dict):
            if node.get('format') == 'date-time':
                assert datetime.fromisoformat(node['examples'][0]).tzinfo is not None
            for child in node.values():
                verify_timestamps(child)
        elif isinstance(node, list):
            for child in node:
                verify_timestamps(child)
    verify_timestamps(spec)
    for path, methods in spec['paths'].items():
        if path.startswith(('/lab/', '/v1/')):
            for operation in methods.values():
                for status, response in operation['responses'].items():
                    if int(status) >= 400:
                        example = response['content']['application/json']['example']
                        if response['content']['application/json']['schema'].get('$ref', '').endswith('/KafkaErrorBody'):
                            KafkaErrorBody.model_validate(example)
                            assert path.startswith('/lab/kafka/')
                            expected_code, expected_publication = {
                                ('/lab/kafka/events', '503'): ('KAFKA_DELIVERY_UNCONFIRMED', 'unknown'),
                                ('/lab/kafka/results/{event_id}', '404'): ('KAFKA_RESULT_NOT_FOUND', 'not_applicable'),
                                ('/lab/kafka/analyses', '503'): ('KAFKA_DELIVERY_UNCONFIRMED', 'unknown'),
                                ('/lab/kafka/analyses/{event_id}', '404'): ('AML_REQUEST_NOT_FOUND', 'not_applicable'),
                            }[(path, status)]
                            assert (example['code'], example['publication']) == (expected_code, expected_publication)
                            continue
                        ErrorResult.model_validate(example)
                        assert example['code'] == {'400': 'INVALID_INPUT_FORMAT', '401': 'UNAUTHORIZED',
                            '403': 'FORBIDDEN', '404': 'NOT_FOUND', '409': 'VERSION_CONFLICT',
                            '500': 'INTERNAL_ERROR', '503': 'DEPENDENCY_UNAVAILABLE'}[status]


def test_parameterized_filters_do_not_turn_input_into_sql(http):
    client, database, _ = http
    injection = "x' OR true --"
    response = client.get('/lab/transactions', headers=AUTH, params={
        'client_id': injection, 'currency': 'RUB', 'min_amount': '0.10', 'max_amount': '95000.10',
        'start_inclusive': '2026-07-01T03:00:00+03:00', 'limit': 100, 'offset': 2})
    assert response.status_code == 200
    sql, parameters = database.calls[0]
    assert injection not in sql and parameters[0] == injection
    assert Decimal('0.10') in parameters and parameters[-2:] == (100, 2)


@pytest.mark.parametrize('path,parameter', [('/lab/cards', 'client_id'), ('/lab/rules', 'category')])
def test_reference_projection_filters_bind_values(http, path, parameter):
    client, database, _ = http
    injection = "x' OR true --"
    assert client.get(path, headers=AUTH, params={parameter: injection}).status_code == 200
    sql, parameters = database.calls[0]
    assert injection not in sql and parameters == (injection, 20, 0)


@pytest.mark.parametrize('params', [
    {'limit': 101}, {'limit': 0}, {'offset': -1}, {'currency': "RUB'; DROP TABLE clients;--"},
    {'start_inclusive': '2026-07-01T00:00:00'}, {'min_amount': '10', 'max_amount': '1'},
    {'start_inclusive': '2026-07-02T00:00:00Z', 'end_exclusive': '2026-07-01T00:00:00Z'},
    {'min_amount': 'NaN'}, {'min_amount': 'Infinity'}, {'min_amount': '0.001'},
])
def test_invalid_filters_safe_before_database(http, params):
    client, database, _ = http
    assert_error(client.get('/lab/transactions', headers=AUTH, params=params), 400, 'INVALID_INPUT_FORMAT')
    assert database.calls == []


@pytest.mark.parametrize('body', [
    {'incident_id': 'INC-000123', 'body': 'note', 'actor': 'administrator'},
    {'incident_id': 'INC-000123', 'body': ' '},
    {'incident_id': 'INC-000123', 'body': 'x' * 4001},
    {'incident_id': 'INC-000123', 'body': 1},
    {'incident_id': 'INC-000123', 'body': 'invalid\x00text'},
])
def test_note_body_is_strict_and_never_echoed(http, body):
    client, database, _ = http
    response = client.post('/lab/notes', headers=AUTH, json=body)
    assert_error(response, 400, 'INVALID_INPUT_FORMAT')
    assert 'administrator' not in response.text and database.calls == []


def test_notes_require_revision_and_cannot_accept_actor_metadata(http):
    client, database, _ = http
    path = '/lab/notes/' + str(uuid4())
    assert_error(client.patch(path, headers=AUTH, json={'body': 'next'}), 400, 'MISSING_INPUT')
    assert_error(client.delete(path, headers=AUTH), 400, 'MISSING_INPUT')
    assert_error(client.patch(path, headers=AUTH, json={'body': 'next', 'expected_revision': True}), 400, 'INVALID_INPUT_FORMAT')
    assert_error(client.patch(path, headers=AUTH, json={'body': 'invalid\x00text', 'expected_revision': 1}), 400, 'INVALID_INPUT_FORMAT')
    assert database.calls == []


@pytest.mark.parametrize('failure,status,code', [
    (LabDatabaseError('UNAVAILABLE'), 503, 'DEPENDENCY_UNAVAILABLE'),
    (LabDatabaseError('CONSTRAINT_VIOLATION'), 400, 'INVALID_INPUT_FORMAT'),
    (RuntimeError('password=secret SQL INSERT PII'), 500, 'INTERNAL_ERROR'),
])
def test_safe_database_errors(http, failure, status, code):
    client, database, _ = http
    database.failure = failure
    response = client.get('/lab/clients', headers=AUTH)
    assert_error(response, status, code)
    assert 'secret' not in response.text and 'INSERT' not in response.text


def test_money_and_time_wire_format_retains_precision():
    assert wire(Decimal('123456789012345678901234567890123456.10')) == '123456789012345678901234567890123456.10'
    assert wire(datetime(2026, 7, 1, tzinfo=timezone.utc)) == '2026-07-01T00:00:00Z'
    with pytest.raises(ValueError):
        wire(datetime(2026, 7, 1))


def test_http_job_admission_replay_refusal_and_local_delivery(http):
    client, _, service = http
    key = dict(AUTH, **{'Idempotency-Key': 'job-one'})
    assert_error(client.post('/v1/jobs', headers=key, json=request()), 403, 'FORBIDDEN')
    assert_error(client.post('/v1/jobs?demo=true', headers=key, json=request()), 503, 'NOT_READY')
    assert service.store.list_jobs(trusted_local_principal()) == []
    assert client.post('/v1/readiness', headers=AUTH).status_code == 202
    until(lambda: client.get('/v1/readiness', headers=AUTH).json()['ready'])
    accepted = client.post('/v1/jobs?demo=true', headers=key, json=request())
    assert accepted.status_code == 202
    location = accepted.headers['Location']
    assert location == '/v1/jobs/' + accepted.json()['job_id']
    assert client.post('/v1/jobs?demo=true', headers=key, json=request()).json() == accepted.json()
    changed = dict(request(), incident_id='INC-000250')
    assert_error(client.post('/v1/jobs?demo=true', headers=key, json=changed), 409, 'IDEMPOTENCY_CONFLICT')
    result = until(lambda: (j if (j := client.get(location, headers=AUTH).json())['status'] == 'refused' else None))
    assert service.worker.calls == 1
    delivery = client.post('/v1/deliveries', headers=dict(AUTH, **{'Idempotency-Key': 'delivery-one'}), json={
        'schema_version': '1.0', 'job_id': result['job_id'], 'report': None, 'decision_id': None,
        'operation': 'manual_queue', 'destination': 'simulator-manual-queue'})
    assert delivery.status_code == 202
    dispatched = client.post('/v1/deliveries/' + delivery.json()['delivery_id'] + '/dispatch', headers=AUTH,
                             json={'expected_revision': delivery.json()['revision']})
    assert dispatched.status_code == 200
    assert dispatched.json()['status'] == 'delivered'
    assert dispatched.json()['receipt']['mode'] == 'local_simulator'


def test_http_cancel_persists_before_acknowledgement(http):
    client, _, service = http
    service.worker.delay = 2
    client.post('/v1/readiness', headers=AUTH)
    until(lambda: client.get('/v1/readiness', headers=AUTH).json()['ready'])
    accepted = client.post('/v1/jobs?demo=true', headers=dict(AUTH, **{'Idempotency-Key': 'start'}), json=request())
    path = accepted.headers['Location']
    cancel_key = dict(AUTH, **{'Idempotency-Key': 'cancel'})
    cancelled = client.post(path + '/cancel', headers=cancel_key)
    assert cancelled.status_code == 200 and cancelled.json()['status'] == 'cancelled'
    assert client.get(path, headers=AUTH).json() == cancelled.json()
    assert client.post(path + '/cancel', headers=cancel_key).json() == cancelled.json()


def test_real_store_decision_http_hash_binding_cas_and_export(http):
    client, _, service = http
    principal = trusted_local_principal()
    create = dict(request(), incident_id='INC-000001')
    job = service.store.create_job(create, principal, 'seed')
    running = service.store.transition_job(job['job_id'], 0, dict(job, status='running', revision=1))
    report = bundle(1)['outcomes'][0]['result']
    report.update(report_id='report-' + uuid4().hex, generated_at=utc_now(), confidence=None)
    service.store.transition_job(job['job_id'], 1, dict(running, status='succeeded', revision=2, completed_at=utc_now(), result=report))
    path = '/v1/reports/' + report['report_id']
    ref = client.get(path + '/reference?version=1', headers=AUTH).json()
    assert client.get(path + '?version=1', headers=AUTH).json() == report
    body = {'schema_version': '1.0', 'report': ref, 'expected_revision': 0, 'action': 'approve', 'reason': 'Synthetic human test'}
    approved = client.post(path + '/decisions', headers=dict(AUTH, **{'Idempotency-Key': 'approve'}), json=body)
    assert approved.status_code == 201
    assert approved.json()['actor'] == principal.actor()
    assert_error(client.post(path + '/decisions', headers=dict(AUTH, **{'Idempotency-Key': 'stale'}), json=body), 409, 'VERSION_CONFLICT')
    pending = client.post('/v1/deliveries', headers=dict(AUTH, **{'Idempotency-Key': 'export'}), json={
        'schema_version': '1.0', 'job_id': job['job_id'], 'report': ref, 'decision_id': approved.json()['decision_id'],
        'operation': 'export', 'destination': 'simulator-export'})
    delivery_path = '/v1/deliveries/' + pending.json()['delivery_id']
    assert client.post(delivery_path + '/dispatch', headers=AUTH, json={'expected_revision': 0}).json()['status'] == 'delivered'
    artifact = client.get(delivery_path + '/artifact', headers=AUTH)
    assert artifact.status_code == 200 and artifact.json() == report


def test_unknown_resource_and_untrusted_host(http):
    client, _, _ = http
    assert_error(client.get('/v1/jobs/missing', headers=AUTH), 404, 'NOT_FOUND')
    assert client.get('/health', headers={'Host': 'attacker.example'}).status_code == 400


def test_network_guard_does_not_allow_application_loopback_connects():
    with socket.socket() as sock, pytest.raises(RuntimeError, match='NETWORK_DISABLED'):
        sock.connect(('127.0.0.1', 55432))


def test_every_documented_data_operation_is_authenticated_before_io(http):
    client, database, service = http
    spec = client.get('/openapi.json').json()
    for path, methods in spec['paths'].items():
        if not path.startswith(('/lab/', '/v1/')):
            continue
        for method in methods:
            response = client.request(method, path, json={})
            assert_error(response, 401, 'UNAUTHORIZED')
    assert database.calls == [] and service.worker.calls == 0


@pytest.mark.parametrize('payload', [b'{', b'\xff', b'null', b'[]'])
def test_malformed_json_and_wrong_json_shape_are_safe(http, payload):
    client, database, _ = http
    response = client.post('/lab/notes', content=payload,
                           headers=dict(AUTH, **{'Content-Type': 'application/json'}))
    assert response.status_code == 400
    assert response.json()['status'] == 'error'
    assert response.json()['code'] in {'MISSING_INPUT', 'INVALID_INPUT_FORMAT'}
    assert database.calls == []


class NoteRecorder(QueryRecorder):
    def __init__(self, revision=1):
        super().__init__()
        self.revision = revision
        self.note_id = uuid4()
    def fetchone(self):
        sql, parameters = self.calls[-1]
        if sql.startswith('SELECT revision'):
            return None if self.revision is None else {'revision': self.revision}
        assert sql.startswith('UPDATE practice.case_notes')
        return {'id': self.note_id, 'incident_id': 'INC-000123', 'body': parameters[0],
                'revision': self.revision + 1, 'created_at': datetime(2026, 7, 1, tzinfo=timezone.utc),
                'updated_at': datetime(2026, 7, 2, tzinfo=timezone.utc)}


@pytest.mark.parametrize('method', ['patch', 'delete'])
@pytest.mark.parametrize('revision,status,code', [(2, 409, 'VERSION_CONFLICT'), (None, 404, 'NOT_FOUND')])
def test_notes_cas_rejects_stale_or_missing_without_writes(method, revision, status, code):
    database = NoteRecorder(revision)
    with TestClient(create_app(SETTINGS, database=database)) as client:
        path = '/lab/notes/' + str(database.note_id)
        kwargs = {'json': {'body': 'next', 'expected_revision': 1}} if method == 'patch' else {'params': {'expected_revision': 1}}
        assert_error(client.request(method, path, headers=AUTH, **kwargs), status, code)
    assert len(database.calls) == 1
    assert database.calls[0][0].endswith('FOR UPDATE')


def test_note_update_binds_body_uuid_and_revision_to_locked_row():
    database = NoteRecorder(3)
    body = "x'); DELETE FROM practice.case_notes; --"
    with TestClient(create_app(SETTINGS, database=database)) as client:
        response = client.patch('/lab/notes/' + str(database.note_id), headers=AUTH,
                                json={'body': body, 'expected_revision': 3})
        assert response.status_code == 200 and response.json()['revision'] == 4
    sql, parameters = database.calls[1]
    assert body not in sql and parameters == (body, database.note_id, 3)
    assert 'revision = revision + 1' in sql and 'AND revision = %s' in sql


def test_local_swagger_assets_exist_and_are_served(http):
    client, _, _ = http
    for asset in ('swagger-ui-bundle.js', 'swagger-ui.css', 'favicon.svg'):
        response = client.get('/static/' + asset)
        assert response.status_code == 200 and len(response.content) > 20
