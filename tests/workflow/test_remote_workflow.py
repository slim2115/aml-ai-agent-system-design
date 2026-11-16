"""Loopback transport, safe failures and shared history without controller ownership."""
from io import BytesIO
import json
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler
from uuid import uuid4

import pytest

from src.workflow import remote
from src.workflow.auth import trusted_local_principal
from src.workflow.store import SQLiteStore, WorkflowError, WORKFLOW_ROOT
from tests.workflow.test_store import request
from tests.evaluator.fixtures import bundle


TOKEN = 'a' * 64
READY = {'ready': True, 'code': 'READY', 'state': 'ready'}


class Response(BytesIO):
    def __init__(self, payload=None, *, status=200, raw=None, headers=None):
        super().__init__(json.dumps(payload).encode() if raw is None else raw)
        self.status = status
        self.headers = {} if headers is None else headers


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(*responses):
    transport = Transport(*responses)
    return remote.WorkflowClient({'api_token': TOKEN}, transport=transport), transport


def api_error(code):
    return {'schema_version': '1.0', 'status': 'error', 'generated_at': '2026-10-18T10:00:00Z',
            'correlation_id': 'api-test', 'code': code, 'message': TOKEN,
            'retryable': code in {'NOT_READY', 'DEPENDENCY_UNAVAILABLE'}}


@pytest.fixture
def store():
    return SQLiteStore(WORKFLOW_ROOT / 'remote-tests' / uuid4().hex / 'workflow.sqlite3')


def test_readiness_key_fixed_host_timeout_and_no_identity():
    api, transport = client(Response(READY))
    assert api.readiness() == READY
    sent, timeout = transport.requests[0]
    assert timeout == 5
    assert sent.full_url == 'http://127.0.0.1:8765/v1/readiness'
    assert sent.get_header('X-lab-key') == TOKEN
    assert sent.method == 'GET' and sent.data is None
    assert set(sent.headers) == {'X-lab-key', 'Accept'}


def test_proxy_environment_ignored_and_redirects_disabled(monkeypatch):
    monkeypatch.setenv('http_proxy', 'http://proxy.invalid:8080')
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.invalid:8080')
    api = remote.WorkflowClient({'api_token': TOKEN})
    assert all(not handler.proxies for handler in api._transport.handlers if isinstance(handler, ProxyHandler))
    redirects = [h for h in api._transport.handlers if isinstance(h, HTTPRedirectHandler)]
    assert len(redirects) == 1
    assert redirects[0].redirect_request(None, None, 302, '', {}, 'http://external.invalid') is None


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_redirects_refused_without_second_request(status):
    api, transport = client(Response({'code': 'READY'}, status=status,
        headers={'Location': 'http://external.invalid/key'}))
    with pytest.raises(WorkflowError, match='^PROTOCOL_ERROR$'):
        api.readiness()
    assert len(transport.requests) == 1


@pytest.mark.parametrize('failure', [TimeoutError(TOKEN), URLError(TOKEN), OSError(TOKEN)])
def test_network_errors_are_safe(failure):
    api, _ = client(failure)
    with pytest.raises(WorkflowError, match='^NOT_READY$') as caught:
        api.readiness()
    assert TOKEN not in ''.join(traceback.format_exception(caught.value))


@pytest.mark.parametrize('code,status', list(remote.ERROR_STATUS.items()))
def test_known_api_error_codes_only(code, status):
    body = BytesIO(json.dumps(api_error(code)).encode())
    api, _ = client(HTTPError(remote.API_BASE, status, TOKEN, {}, body))
    with pytest.raises(WorkflowError, match='^' + code + '$') as caught:
        api.readiness()
    assert TOKEN not in ''.join(traceback.format_exception(caught.value))


@pytest.mark.parametrize('response', [
    lambda: Response({'code': TOKEN}, status=500),
    lambda: Response(raw=b'<html>server error</html>'),
    lambda: Response(raw=b'x' * (remote.MAX_RESPONSE_BYTES + 1)),
    lambda: Response(READY, headers={'Content-Length': str(remote.MAX_RESPONSE_BYTES + 1)}),
    lambda: Response(READY, headers={'Content-Length': '-2'}),
    lambda: Response(dict(READY, ready='true')),
    lambda: Response(dict(READY, state='busy')),
    lambda: Response(READY, status=202),
    lambda: Response(READY, status=True),
    lambda: Response(READY, headers={'Content-Length': '999'}),
    lambda: Response(READY, headers={'Content-Length': '\u00b2'}),
    lambda: Response(raw=b'{"ready":true,"ready":false,"code":"NOT_READY","state":"not_ready"}'),
    lambda: Response(raw=b'{"ready":NaN,"code":"READY","state":"ready"}'),
    lambda: Response(raw=b'\xff'),
])
def test_untrusted_responses_fail_closed(response):
    api, _ = client(response())
    with pytest.raises(WorkflowError, match='^PROTOCOL_ERROR$'):
        api.readiness()


@pytest.mark.parametrize('payload,status', [
    ({'code': 'NOT_READY', 'message': TOKEN}, 503),
    (dict(api_error('NOT_READY'), retryable='true'), 503),
    (dict(api_error('NOT_READY'), debug=TOKEN), 503),
    (dict(api_error('NOT_READY'), code='PROTOCOL_ERROR'), 503),
    (api_error('NOT_READY'), 401),
    (api_error('UNAUTHORIZED'), 409),
    (api_error('INTERNAL_ERROR'), 200),
    (api_error('INTERNAL_ERROR'), 418),
])
def test_error_response_requires_strict_shape_and_matching_http_status(payload, status):
    api, transport = client(Response(payload, status=status))
    with pytest.raises(WorkflowError, match='^PROTOCOL_ERROR$') as caught:
        api.readiness()
    assert len(transport.requests) == 1
    assert TOKEN not in ''.join(traceback.format_exception(caught.value))


@pytest.mark.parametrize('timeout', [0, -1, 11, float('nan'), float('inf'), True, '5'])
def test_timeout_has_finite_small_bound(timeout):
    with pytest.raises(WorkflowError, match='INVALID_INPUT_FORMAT'):
        remote.WorkflowClient({'api_token': TOKEN}, timeout=timeout)


def test_missing_key_keeps_adapter_history_available(monkeypatch, store):
    monkeypatch.setattr(remote, 'load_lab_settings', lambda: (_ for _ in ()).throw(RuntimeError(TOKEN)))
    service = remote.RemoteWorkflowService(store=store)
    assert service.readiness() == {'ready': False, 'state': 'not_ready', 'code': 'NOT_READY'}
    assert service.api_available is False
    assert service.store.list_jobs(trusted_local_principal()) == []


def test_submit_preparation_and_poll_contract(store):
    principal = trusted_local_principal()
    accepted = store.create_job(request(), principal, 'saved-key')
    preparing = {'ready': False, 'code': 'NOT_READY', 'state': 'preparing'}
    api, transport = client(Response(preparing, status=202), Response(accepted, status=202), Response(accepted))
    assert api.prepare() == preparing
    assert api.submit(request(), principal, 'stable-key') == accepted
    assert api.get_job(accepted['job_id']) == accepted
    sent, _ = transport.requests[1]
    assert sent.full_url == remote.API_BASE + '/v1/jobs?demo=true&engine=template'
    assert sent.get_header('Idempotency-key') == 'stable-key'
    assert json.loads(sent.data) == request()
    assert 'subject_id' not in sent.data.decode() and 'role' not in sent.data.decode()


@pytest.mark.parametrize('job_id', ['../shutdown', 'job?q=x', 'a\r\nsecret', 'http://evil'])
def test_job_identifier_cannot_redirect_route(job_id):
    api, transport = client()
    with pytest.raises(WorkflowError, match='INVALID_INPUT_FORMAT'):
        api.get_job(job_id)
    assert transport.requests == []


def test_cancel_and_wrong_job_response(store):
    principal = trusted_local_principal()
    accepted = store.create_job(request(), principal, 'cancel-key')
    cancelled = store.cancel_job(accepted['job_id'], principal, idempotency_key='cancel-command')
    api, transport = client(Response(cancelled), Response(accepted))
    assert api.cancel(accepted['job_id'], principal, 'cancel-command') == cancelled
    assert transport.requests[0][0].method == 'POST'
    with pytest.raises(WorkflowError, match='^PROTOCOL_ERROR$'):
        api.get_job('different-job')


def test_invalid_admission_body_or_engine_never_sent():
    api, transport = client()
    with pytest.raises(WorkflowError, match='INVALID_INPUT_FORMAT'):
        api.submit(dict(request(), role='supervisor'), None, 'key')
    with pytest.raises(WorkflowError, match='INVALID_INPUT_FORMAT'):
        api.submit(request(), None, 'key', engine='template&demo=false')
    with pytest.raises(WorkflowError, match='INVALID_INPUT_FORMAT'):
        api.submit(request(), None, 'key\nleak')
    assert transport.requests == []


def test_analysis_list_is_bounded_and_renderable():
    item = {'event_id': str(uuid4()), 'incident_id': 'INC-000123', 'status': 'queued',
            'job_id': None, 'last_error_code': 'NOT_READY'}
    api, transport = client(Response({'items': [item], 'limit': 20}),
                            Response({'items': [dict(item, job_id='../x')]}))
    assert api.analysis_requests()['items'] == [item]
    assert transport.requests[0][0].full_url == remote.API_BASE + '/lab/kafka/analyses?limit=20'
    with pytest.raises(WorkflowError, match='^PROTOCOL_ERROR$'):
        api.analysis_requests()
    with pytest.raises(WorkflowError, match='INVALID_INPUT_FORMAT'):
        api.analysis_requests(101)


def test_remote_service_never_acquires_lease_recovers_or_stops_api(monkeypatch, store):
    import src.workflow.controller as controller
    principal = trusted_local_principal()
    accepted = store.create_job(request(), principal, 'pending')
    def forbidden(*args, **kwargs):
        raise AssertionError('Remote UI must not own the controller')
    monkeypatch.setattr(controller, 'ControllerLease', forbidden)
    monkeypatch.setattr(SQLiteStore, 'recover_active_jobs', forbidden)
    api, transport = client(Response(READY))
    monkeypatch.setattr(api, 'close', forbidden)
    shared = SQLiteStore(store.path)
    service = remote.RemoteWorkflowService(client=api, store=shared)
    assert service.readiness() == READY and service.api_available
    assert service.store.get_job(accepted['job_id'], principal)['status'] == 'queued'
    service.close()
    assert len(transport.requests) == 1
    assert store.get_job(accepted['job_id'], principal)['status'] == 'queued'


@pytest.mark.parametrize('state,code', [('busy', 'READY'), ('busy', 'NOT_READY'),
    ('not_ready', 'READY'), ('preparing', 'BUSY'), ('closed', 'DEADLINE_EXCEEDED')])
def test_inconsistent_readiness_codes_are_not_rendered(state, code):
    api, _ = client(Response({'ready': False, 'state': state, 'code': code}))
    with pytest.raises(WorkflowError, match='^PROTOCOL_ERROR$'):
        api.readiness()


def terminal_job(status):
    """Wire fixtures exercise the actual strict job union and semantic checks."""
    job = {'schema_version': '1.0', 'job_id': 'job-fixture', 'incident_id': 'INC-000001',
           'revision': 2, 'accepted_at': '2026-10-04T12:00:00Z',
           'deadline_at': '2026-10-04T12:03:00Z', 'status': status,
           'completed_at': '2026-10-04T12:00:02Z'}
    if status == 'succeeded':
        job['result'] = bundle(1)['outcomes'][0]['result']
        job['result']['generated_at'] = '2026-10-04T12:00:01Z'
    elif status == 'refused':
        job['result'] = {'schema_version': '1.0', 'status': 'refusal',
            'incident_id': job['incident_id'], 'generated_at': '2026-10-04T12:00:01Z',
            'data_completeness': 'unassessed', 'reason_code': 'INSUFFICIENT_DATA',
            'reason': 'Synthetic refusal', 'recommended_action': 'manual_review'}
    elif status == 'failed':
        job['result'] = {'schema_version': '1.0', 'status': 'error',
            'generated_at': '2026-10-04T12:00:01Z', 'correlation_id': 'run-fixture',
            'code': 'INTERNAL_ERROR', 'message': 'Synthetic error', 'retryable': False}
    elif status == 'timed_out':
        job['completed_at'] = job['deadline_at']
    return job


@pytest.mark.parametrize('status', ['succeeded', 'refused', 'failed', 'cancelled', 'timed_out'])
def test_poll_accepts_each_exact_terminal_shape(status):
    job = terminal_job(status)
    api, _ = client(Response(job))
    assert api.get_job(job['job_id']) == job


@pytest.mark.parametrize('change', ['result_kind', 'missing_result', 'extra_field',
    'wrong_incident', 'late_result', 'premature_timeout'])
def test_poll_rejects_invalid_result_shape_or_semantics(change):
    job = terminal_job('refused')
    if change == 'result_kind':
        job['result']['status'] = 'draft'
    elif change == 'missing_result':
        del job['result']
    elif change == 'extra_field':
        job['raw_model_output'] = TOKEN
    elif change == 'wrong_incident':
        job['result']['incident_id'] = 'INC-999999'
    elif change == 'late_result':
        job['completed_at'] = job['deadline_at']
    else:
        job = terminal_job('timed_out')
        job['completed_at'] = job['accepted_at']
    api, _ = client(Response(job))
    with pytest.raises(WorkflowError, match='^PROTOCOL_ERROR$') as caught:
        api.get_job(job['job_id'])
    assert TOKEN not in ''.join(traceback.format_exception(caught.value))


def test_replay_keeps_original_acceptance_then_polls_current_terminal_job(store):
    principal = trusted_local_principal()
    original = store.create_job(request(), principal, 'stable-event-key')
    cancelled = store.cancel_job(original['job_id'], principal, idempotency_key='cancel')
    replay = store.lookup_job_replay(request(), principal, 'stable-event-key')
    assert replay == original and replay['status'] == 'queued'
    api, _ = client(Response(replay, status=202), Response(cancelled))
    accepted = api.submit(request(), principal, 'stable-event-key')
    assert accepted == original
    assert api.get_job(accepted['job_id']) == cancelled
    assert len(store.list_jobs(principal)) == 1


@pytest.mark.parametrize('operation', ['submit', 'cancel'])
def test_endpoint_specific_status_is_checked(operation):
    job = terminal_job('succeeded')
    api, _ = client(Response(job, status=202 if operation == 'submit' else 200))
    with pytest.raises(WorkflowError, match='^PROTOCOL_ERROR$'):
        if operation == 'submit':
            api.submit(request(), None, 'key')
        else:
            api.cancel(job['job_id'], None, 'cancel')


@pytest.mark.parametrize('change', ['duplicate_event', 'missing_job', 'unexpected_job',
    'wrong_limit', 'bool_limit', 'html_status', 'unsafe_job_id'])
def test_kafka_list_rejects_inconsistent_or_unsafe_rows(change):
    item = {'event_id': str(uuid4()), 'incident_id': 'INC-000123', 'status': 'queued',
            'job_id': None, 'last_error_code': None}
    payload = {'items': [item], 'limit': 20}
    if change == 'duplicate_event':
        payload['items'].append(dict(item))
    elif change == 'missing_job':
        item['status'] = 'succeeded'
    elif change == 'unexpected_job':
        item['job_id'] = 'job-unexpected'
    elif change == 'wrong_limit':
        payload['limit'] = 100
    elif change == 'bool_limit':
        payload['limit'] = True
    elif change == 'html_status':
        item['status'] = '<script>unsafe</script>'
    else:
        item.update(status='submitted', job_id='../unsafe')
    api, _ = client(Response(payload))
    with pytest.raises(WorkflowError, match='^PROTOCOL_ERROR$'):
        api.analysis_requests()
