"""Bounded judge transport against doubles only; never contacts Ollama."""
import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from qa.judge.transport import ENDPOINT, MAX_BYTES, MODEL, SYSTEM_PROMPT, JudgeTransport


PAYLOAD = {'case_id': 'blind-1', 'criterion': 'grounding', 'mode': 'single',
           'sources': [{'ref_id': 's1', 'text': 'Amount: 12.30 USD'}],
           'candidates': [{'label': 'A', 'report': {'amount': '12.30'},
                           'fragments': [{'ref_id': 'a1', 'text': '12.30 USD'}]}]}


def envelope(**changes):
    value = {'model': MODEL, 'done': True, 'done_reason': 'stop',
             'message': {'role': 'assistant', 'content': '{"verdict":"abstain"}'},
             'total_duration': 2_500_000_000, 'load_duration': 500_000_000,
             'prompt_eval_count': 100, 'eval_count': 12}
    value.update(changes)
    return json.dumps(value).encode()


class Response:
    def __init__(self, raw=None, *, headers=None, status=200, read=None):
        self.raw = envelope() if raw is None else raw
        self.headers, self.status, self.read = headers or {}, status, read

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def getheaders(self):
        return list(self.headers.items())

    def read1(self, count):
        if self.read:
            self.read()
        chunk, self.raw = self.raw[:count], self.raw[count:]
        return chunk


class Connection:
    def __init__(self, response):
        self.response, self.requests, self.closed = response, [], False
        self.timeouts = []
        self.sock = SimpleNamespace(settimeout=self.timeouts.append)

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        self.sock = None
        return self.response

    def close(self):
        self.closed = True


def call(response=None, clock=lambda: 10., *, deadline=30., payload=None):
    connection, destinations = Connection(response or Response()), []

    def factory(host, port, **kwargs):
        destinations.append((host, port, kwargs))
        return connection

    row = JudgeTransport(connection_factory=factory, clock=clock).complete(
        PAYLOAD if payload is None else payload, {'criterion': 'Check source support.'}, deadline=deadline)
    return row, connection, destinations


def test_success_records_raw_bytes_metrics_and_exact_isolated_request(monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://example.invalid:8080')
    raw = envelope()
    row, connection, destinations = call(Response(raw))
    assert row['status'] == 'completed' and row['error_code'] is None
    assert base64.b64decode(row['response_body_base64']) == raw
    assert row['response_body_sha256'] == hashlib.sha256(raw).hexdigest()
    assert row['response_body'] == raw.decode()
    assert row['timings_seconds'] == {'total': 2.5, 'load': 0.5}
    assert row['tokens'] == {'prompt_eval_count': 100, 'eval_count': 12}
    assert row['queue_wait_seconds'] is None
    assert destinations == [(*ENDPOINT, {'timeout': 20.})]
    assert connection.closed and len(connection.requests) == 1
    args, kwargs = connection.requests[0]
    assert args == ('POST', '/api/chat')
    request = json.loads(kwargs['body'])
    assert request['model'] == MODEL
    assert request['stream'] is False and 'tools' not in request
    assert request['options'] == {'temperature': 0, 'num_predict': 512, 'num_ctx': 32768}
    assert request['messages'][0] == {'role': 'system', 'content': SYSTEM_PROMPT}
    assert json.loads(request['messages'][1]['content'])['task'] == PAYLOAD
    assert kwargs['headers']['Accept-Encoding'] == 'identity'


@pytest.mark.parametrize('raw', [
    b'{"done":true,"done":true}', b'{"a":NaN}', b'not json', b'\xff',
    envelope(done=False), envelope(done=1), envelope(done_reason='length'),
    envelope(model='different-model'), envelope(message={'role': 'user', 'content': '{}'}),
    envelope(message={'role': 'assistant', 'content': '{}', 'tool_calls': [{'name': 'x'}]}),
    envelope(message={'role': 'assistant', 'content': 1}),
    envelope(message={'role': 'assistant', 'content': ' '}),
    envelope(eval_count=True), envelope(total_duration=-1),
])
def test_malformed_incomplete_or_wrong_model_envelope_fails_without_retry(raw):
    row, connection, destinations = call(Response(raw))
    assert row['status'] == 'error' and row['response_text'] is None
    assert len(connection.requests) == len(destinations) == 1
    assert connection.closed
    assert base64.b64decode(row['response_body_base64']) == raw


@pytest.mark.parametrize('status', [301, 307, 401, 500])
def test_http_error_or_redirect_does_not_follow_or_retry(status):
    row, connection, destinations = call(Response(status=status))
    assert row['error_code'] == 'QA_JUDGE_HTTP_STATUS'
    assert len(destinations) == len(connection.requests) == 1


@pytest.mark.parametrize('headers', [{'Content-Encoding': 'gzip'},
    {'Content-Length': str(MAX_BYTES + 1)}, {'Content-Length': '-1'},
    {'Content-Length': '123, 123'}])
def test_bad_response_headers_are_rejected_before_body(headers):
    row, connection, _ = call(Response(headers=headers))
    assert row['status'] == 'error' and row['response_body'] == ''
    assert connection.closed


def test_valid_json_with_short_content_length_body_is_transport_failure():
    raw = envelope()
    row, _, _ = call(Response(raw, headers={'Content-Length': str(len(raw) + 10)}))
    assert row['error_code'] == 'QA_JUDGE_TRUNCATED_RESPONSE'
    assert row['response_text'] is None


def test_unannounced_oversize_response_cannot_publish_completion():
    row, connection, _ = call(Response(b'a' * (MAX_BYTES + 100)))
    assert row['error_code'] == 'QA_JUDGE_RESPONSE_TOO_LARGE'
    assert row['response_text'] is None and connection.closed


def test_large_or_unblinded_request_is_rejected_without_connecting():
    for value in ({**PAYLOAD, 'oracle': 'pass'},
                  {**PAYLOAD, 'case_id': 'x' * MAX_BYTES}):
        row, connection, destinations = call(payload=value)
        assert row['status'] == 'error'
        assert not destinations and not connection.requests


def test_context_budget_rejects_oversized_source_without_truncating_or_connecting():
    from copy import deepcopy
    value = deepcopy(PAYLOAD)
    value['sources'][0]['text'] = 'x' * 30000
    row, connection, destinations = call(payload=value)
    assert row['error_code'] == 'QA_JUDGE_CONTEXT_BUDGET'
    assert not destinations and not connection.requests


def test_expired_deadline_never_connects():
    row, connection, destinations = call(deadline=9.)
    assert row['status'] == 'timeout' and not destinations and not connection.requests


def test_absolute_deadline_rechecked_while_reading_even_after_socket_detaches():
    now = [10.]

    def advance():
        now[0] += 8.

    row, connection, _ = call(Response(read=advance), clock=lambda: now[0], deadline=20.)
    assert row['status'] == 'timeout' and row['response_text'] is None
    assert connection.timeouts == [10., 10., 2.]
    assert connection.closed


def test_socket_timeout_produces_timeout_record_and_closes_connection():
    def timeout():
        raise TimeoutError('private exception details')

    row, connection, _ = call(Response(read=timeout))
    assert row['status'] == 'timeout'
    assert row['error_code'] == 'QA_JUDGE_DEADLINE_EXCEEDED'
    assert 'private' not in json.dumps(row)
    assert connection.closed
