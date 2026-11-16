"""Fixed-loopback workflow client and shared-history UI adapter.

Only the API owns the controller lease. This process can read/review the same
SQLite reports without recovering active jobs or stopping the API on exit.
"""
import json
import math
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from src.contracts.semantics import validate_job
from src.contracts.v1 import CreateJobRequest, ErrorResult
from src.lab.config import load_lab_settings
from src.qa_profile import get_qa_profile
from src.runtime.io_budget import BudgetExpired, check_budget, current_budget
from .bounded_http import build_deadline_opener
from .store import SQLiteStore, WorkflowError


ROOT = Path(__file__).resolve().parents[2]
API_BASE = 'http://127.0.0.1:8765'
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ERROR_STATUS = {'MISSING_INPUT': 400, 'INVALID_INPUT_FORMAT': 400, 'UNAUTHORIZED': 401,
    'FORBIDDEN': 403, 'NOT_FOUND': 404, 'VERSION_CONFLICT': 409, 'IDEMPOTENCY_CONFLICT': 409,
    'INVALID_STATE': 409, 'NOT_READY': 503, 'DEPENDENCY_UNAVAILABLE': 503,
    'DEADLINE_EXCEEDED': 503, 'INTERNAL_ERROR': 500}


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('DUPLICATE_JSON_KEY')
        value[key] = item
    return value


def _invalid_json_constant(value):
    raise ValueError('INVALID_JSON_CONSTANT')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def _identifier(value):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is None:
        raise WorkflowError('INVALID_INPUT_FORMAT')
    return value


class WorkflowClient:
    """No caller-supplied URLs, proxies, redirects, identities or raw errors.

    The optional transport implements urllib's ``open(request, timeout=...)``
    interface and exists for hermetic tests. Under a scheduler budget it must
    explicitly implement ``open_budgeted(..., deadline=...)``; an arbitrary
    unbounded transport is rejected. No transport is loaded from env.
    """
    def __init__(self, settings=None, timeout=5, *, transport=None):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise WorkflowError('INVALID_INPUT_FORMAT')
        self._timeout = timeout
        profile = get_qa_profile()
        self._base_url = profile.api_url if profile else API_BASE
        try:
            token = (load_lab_settings() if settings is None else settings)['api_token']
            if not isinstance(token, str) or re.fullmatch(r'[0-9a-f]{64}', token) is None:
                raise ValueError
            self._token = token
        except (RuntimeError, KeyError, TypeError, ValueError):
            # Offline history must remain available even before lab setup.
            self._token = None
        self._transport = transport if transport is not None else build_opener(ProxyHandler({}), _NoRedirect())
        self._custom_transport = transport is not None

    def _request(self, method, path, *, body=None, key=None, expected=200):
        check_budget()
        if self._token is None:
            raise WorkflowError('NOT_READY')
        headers = {'X-Lab-Key': self._token, 'Accept': 'application/json'}
        if key is not None:
            headers['Idempotency-Key'] = _identifier(key)
        data = None
        if body is not None:
            data = json.dumps(body, allow_nan=False, separators=(',', ':')).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = Request(self._base_url + path, data=data, headers=headers, method=method)
        try:
            try:
                budget = current_budget()
                if budget is None:
                    response = self._transport.open(request, timeout=self._timeout)
                elif self._custom_transport:
                    bounded_open = getattr(self._transport, 'open_budgeted', None)
                    if not callable(bounded_open):
                        raise WorkflowError('PROTOCOL_ERROR')
                    response = bounded_open(request, timeout=min(self._timeout, budget.remaining()),
                                            deadline=budget)
                else:
                    opener = build_deadline_opener(budget, self._timeout)
                    response = opener.open(request, timeout=min(self._timeout, budget.remaining()))
            except HTTPError as exc:
                response = exc
            with response:
                check_budget()
                status = response.status
                # Protocol failures must not be retried as mere unavailability.
                # Never consume or follow redirect responses with our key.
                if type(status) is not int or not 100 <= status <= 599 or 300 <= status < 400:
                    raise WorkflowError('PROTOCOL_ERROR')
                length = response.headers.get('Content-Length')
                if length is not None and (not isinstance(length, str)
                        or re.fullmatch(r'[0-9]+', length) is None or int(length) > MAX_RESPONSE_BYTES):
                    raise WorkflowError('PROTOCOL_ERROR')
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                check_budget()
                if len(raw) > MAX_RESPONSE_BYTES or (length is not None and len(raw) != int(length)):
                    raise WorkflowError('PROTOCOL_ERROR')
                payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_json_object,
                                     parse_constant=_invalid_json_constant)
                check_budget()
            if status != expected:
                error = ErrorResult.model_validate(payload)
                check_budget()
                if ERROR_STATUS[error.code] != status:
                    raise WorkflowError('PROTOCOL_ERROR')
                raise WorkflowError(error.code)
            return payload
        except BudgetExpired:
            raise
        except WorkflowError:
            check_budget()
            raise
        except (OSError, URLError):
            check_budget()
            raise WorkflowError('NOT_READY') from None
        except Exception:
            check_budget()
            # URLs, keys, request bodies and dependency exceptions stay local.
            raise WorkflowError('PROTOCOL_ERROR') from None

    @staticmethod
    def _readiness(payload):
        check_budget()
        codes_by_state = {'ready': ('READY',), 'busy': ('BUSY',),
                          'preparing': ('NOT_READY', 'DEADLINE_EXCEEDED'),
                          'not_ready': ('NOT_READY', 'DEADLINE_EXCEEDED'),
                          'closed': ('NOT_READY',)}
        if (not isinstance(payload, dict) or set(payload) != {'ready', 'code', 'state'}
                or type(payload['ready']) is not bool
                or not isinstance(payload['state'], str) or payload['state'] not in codes_by_state
                or payload['code'] not in codes_by_state[payload['state']]
                or payload['ready'] != (payload['state'] == 'ready')):
            raise WorkflowError('PROTOCOL_ERROR')
        check_budget()
        return payload

    def readiness(self):
        return self._readiness(self._request('GET', '/v1/readiness'))

    def prepare(self):
        return self._readiness(self._request('POST', '/v1/readiness', expected=202))

    @staticmethod
    def _job(payload, *, job_id=None, incident_id=None, statuses=None):
        check_budget()
        try:
            job = validate_job(payload)
            if ((job_id is not None and job.job_id != job_id)
                    or (incident_id is not None and job.incident_id != incident_id)
                    or (statuses is not None and job.status not in statuses)):
                raise ValueError
            result = job.model_dump(mode='json')
            check_budget()
            return result
        except (TypeError, ValueError):
            check_budget()
            raise WorkflowError('PROTOCOL_ERROR') from None

    def submit(self, request, principal, idempotency_key, *, engine='template'):
        # Identity is established by the API's OS account, never by this body.
        try:
            validated = CreateJobRequest.model_validate(
                request.model_dump(mode='json') if hasattr(request, 'model_dump') else request)
        except (TypeError, ValueError):
            raise WorkflowError('INVALID_INPUT_FORMAT') from None
        if engine not in ('template', 'local-model'):
            raise WorkflowError('INVALID_INPUT_FORMAT')
        payload = self._request('POST', '/v1/jobs?demo=true&engine=' + engine,
            body=validated.model_dump(mode='json'), key=idempotency_key, expected=202)
        return self._job(payload, incident_id=validated.incident_id, statuses=('queued', 'running'))

    def get_job(self, job_id):
        return self._job(self._request('GET', '/v1/jobs/' + _identifier(job_id)), job_id=job_id)

    def cancel(self, job_id, principal, idempotency_key):
        return self._job(self._request('POST', '/v1/jobs/' + _identifier(job_id) + '/cancel',
            key=idempotency_key), job_id=job_id, statuses=('cancelled',))

    def analysis_requests(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise WorkflowError('INVALID_INPUT_FORMAT')
        payload = self._request('GET', '/lab/kafka/analyses?limit=' + str(limit))
        if (not isinstance(payload, dict) or set(payload) != {'items', 'limit'}
                or type(payload['limit']) is not int or payload['limit'] != limit
                or not isinstance(payload.get('items'), list)
                or len(payload['items']) > limit or any(not isinstance(item, dict) for item in payload['items'])):
            raise WorkflowError('PROTOCOL_ERROR')
        try:
            seen = set()
            for item in payload['items']:
                if (not isinstance(item['event_id'], str) or str(UUID(item['event_id'])) != item['event_id']
                        or item['event_id'] in seen
                        or not isinstance(item['incident_id'], str)
                        or re.fullmatch(r'INC-[0-9]{6}', item['incident_id']) is None
                        or item['status'] not in ('queued', 'submitted', 'succeeded', 'refused',
                                                  'failed', 'timed_out', 'cancelled', 'rejected')):
                    raise ValueError
                seen.add(item['event_id'])
                if (item['status'] in ('queued', 'rejected')) != (item.get('job_id') is None):
                    raise ValueError
                if item.get('job_id') is not None:
                    _identifier(item['job_id'])
                if item.get('last_error_code') is not None and (not isinstance(item['last_error_code'], str)
                        or re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', item['last_error_code']) is None):
                    raise ValueError
        except (KeyError, TypeError, ValueError, WorkflowError):
            raise WorkflowError('PROTOCOL_ERROR') from None
        return payload

    def close(self):
        """No controller ownership and no shutdown request."""


class RemoteWorkflowService:
    def __init__(self, *, client=None, store=None):
        profile = get_qa_profile()
        path = profile.workflow_path if profile else ROOT / 'var/workflow/api-workflow.sqlite3'
        self.store = store if store is not None else SQLiteStore(path)
        self.client = client if client is not None else WorkflowClient()
        self.api_available = False

    def readiness(self):
        try:
            result = self.client.readiness()
            self.api_available = True
            return result
        except WorkflowError:
            self.api_available = False
            return {'ready': False, 'code': 'NOT_READY', 'state': 'not_ready'}

    def prepare(self):
        return self.client.prepare()

    def submit(self, request, principal, idempotency_key, *, engine='template'):
        return self.client.submit(request, principal, idempotency_key, engine=engine)

    def cancel(self, job_id, principal, idempotency_key):
        return self.client.cancel(job_id, principal, idempotency_key)

    def close(self):
        """Closing the UI must never stop or recover the shared API controller."""
