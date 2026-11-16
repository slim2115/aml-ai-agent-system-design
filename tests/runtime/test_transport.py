from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from src.config import get_settings
from src.runtime.events import RunEvent
from src.runtime.transport import LocalOllamaTransport, TransportFailure


class Reply:
    status = 200
    def __init__(self, data=None, headers=None):
        self.data = data or json.dumps({'done': True, 'message': {'role': 'assistant', 'content': '{}'}}).encode()
        self.headers = headers or {}
    def getheader(self, name, default=None):
        return self.headers.get(name, default)
    def read1(self, count):
        part, self.data = self.data[:count], self.data[count:]
        return part


class Connection:
    def __init__(self, response):
        self.reply, self.closed, self.calls = response, False, []
        self.sock = SimpleNamespace(settimeout=lambda value: None)
    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
    def getresponse(self):
        return self.reply
    def close(self):
        self.closed = True


def call(response=None, *, settings=None, clock=lambda: 10.0, deadline=20.0):
    connection = Connection(response or Reply())
    destinations = []
    def factory(host, port, **kwargs):
        destinations.append((host, port, kwargs))
        return connection
    client = LocalOllamaTransport(settings or get_settings(), connection_factory=factory, clock=clock)
    return client, connection, destinations


def test_only_literal_loopback_no_proxy_or_redirect_and_constrained_request(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://remote.invalid')
    settings = get_settings()
    settings = replace(settings, llm=replace(settings.llm, base_url='http://localhost:11434'))
    client, connection, destinations = call(settings=settings)
    assert client.complete({'facts': []}, {'type': 'object'}, deadline=20) == '{}'
    assert destinations == [('127.0.0.1', 11434, {'timeout': 10.0})]
    args, kwargs = connection.calls[0]
    assert args == ('POST', '/api/chat')
    payload = json.loads(kwargs['body'])
    assert payload['stream'] is False and 'tools' not in payload
    assert payload['model'] == 'qwen2.5:14b'
    assert connection.closed


@pytest.mark.parametrize('status', [301, 302, 307, 400, 404, 500])
def test_redirects_and_error_bodies_never_follow_or_expose(status):
    reply = Reply(b'PRIVATE SECRET PAN 4111111111111111')
    reply.status = status
    client, connection, destinations = call(reply)
    with pytest.raises(TransportFailure, match='^DEPENDENCY_UNAVAILABLE$'):
        client.complete({}, {}, deadline=20)
    assert len(destinations) == 1 and connection.closed


@pytest.mark.parametrize('data', [b'{"done":false}', b'{"done":true,"done":true}',
    b'{"done":NaN}', b'{"done":true,"message":{"role":"user","content":"secret"}}',
    b'{"done":true,"message":{"role":"assistant","content":"{}","tool_calls":[{}]}}', b'not json'])
def test_malformed_truncated_tool_outputs_rejected(data):
    client, connection, _ = call(Reply(data))
    with pytest.raises(TransportFailure):
        client.complete({}, {}, deadline=20)
    assert connection.closed


@pytest.mark.parametrize('headers', [{'Content-Length': '999999'}, {'Content-Encoding': 'gzip'}, {'Content-Length': '-1'}])
def test_bounded_identity_encoded_response(headers):
    client, connection, _ = call(Reply(headers=headers))
    with pytest.raises(TransportFailure):
        client.complete({}, {}, deadline=20)
    assert connection.closed


def test_unannounced_oversize_rejected():
    client, connection, _ = call(Reply(b'a' * (LocalOllamaTransport.MAX_BYTES + 1)))
    with pytest.raises(TransportFailure):
        client.complete({}, {}, deadline=20)
    assert connection.closed


def test_short_body_that_happens_to_be_valid_json_is_not_accepted():
    reply = Reply()
    reply.headers['Content-Length'] = str(len(reply.data) + 1)
    client, connection, _ = call(reply)
    with pytest.raises(TransportFailure, match='^DEPENDENCY_UNAVAILABLE$'):
        client.complete({}, {}, deadline=20)
    assert connection.closed


def test_close_response_keeps_shrinking_read_timeout_on_detached_socket():
    ticks = iter([10., 11., 12., 13., 14.])
    client, connection, _ = call(clock=lambda: next(ticks))
    timeouts = []
    connection.sock = SimpleNamespace(settimeout=timeouts.append)
    def getresponse():
        connection.sock = None
        return connection.reply
    connection.getresponse = getresponse
    assert client.complete({}, {}, deadline=20) == '{}'
    assert timeouts == [9., 8., 7.]
    assert connection.closed


def test_timeout_never_publishes_late_body():
    ticks = iter([10., 11., 12., 21.])
    client, connection, _ = call(clock=lambda: next(ticks))
    with pytest.raises(TransportFailure, match='^DEADLINE_EXCEEDED$'):
        client.complete({}, {}, deadline=20)
    assert connection.closed


def test_unapproved_model_is_not_called_or_pulled():
    cfg = get_settings()
    client, _, destinations = call(settings=replace(cfg, llm=replace(cfg.llm, model_name='unknown')))
    with pytest.raises(TransportFailure, match='^NOT_READY$'):
        client.complete({}, {}, deadline=20)
    assert not destinations


@pytest.mark.parametrize('field,value', [('stage', 'secret text'), ('code', 'secret text'), ('attempt', True)])
def test_operational_events_have_no_free_text_escape(field, value):
    fields = {'correlation_id': 'run-123', 'stage': 'context', 'code': 'PASSED', 'attempt': 0, field: value}
    with pytest.raises(ValueError):
        RunEvent(**fields)
