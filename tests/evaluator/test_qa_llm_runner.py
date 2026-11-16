"""Hermetic collector tests: no real model, service or network is started."""
import base64
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from qa.tools import run_llm as runner


@pytest.mark.parametrize('relative', ['var/qa/llm/new', 'verification/qa-step-07-example/new'])
def test_output_is_new_scoped_directory(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    assert runner.output_path(relative) == tmp_path / relative
    assert not (tmp_path / relative).exists()


@pytest.mark.parametrize('relative', ['var/qa/llm', 'verification/qa-step-06-a/new',
    'verification/qa-step-07-a', 'data/new', 'var/qa/llm/../../escape'])
def test_output_cannot_touch_existing_product_or_other_evidence(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    with pytest.raises(ValueError):
        runner.output_path(relative)


def test_existing_evidence_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    target = tmp_path / 'var/qa/llm/previous'
    target.mkdir(parents=True)
    (target / 'result.json').write_text('preserve', encoding='utf-8')
    with pytest.raises(FileExistsError):
        runner.output_path(target)
    assert (target / 'result.json').read_text() == 'preserve'


@pytest.mark.parametrize('address', [('localhost', 11434), ('127.0.0.1', 8765),
    ('::1', 11434), ('8.8.8.8', 11434), ('api.example.test', 443), '/tmp/socket'])
def test_only_literal_ollama_endpoint_allowed(address):
    with pytest.raises(RuntimeError, match='QA_LLM_NETWORK_ENDPOINT_DENIED'):
        runner.require_endpoint(address)


def test_guard_denies_connections_dns_and_udp_without_contacting_host():
    with runner.network_guard():
        with pytest.raises(RuntimeError):
            socket.create_connection(('127.0.0.1', 8765))
        with pytest.raises(RuntimeError):
            socket.getaddrinfo('example.test', 443)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
            with pytest.raises(RuntimeError):
                stream.sendto(b'no outbound request', ('127.0.0.1', 11434))


def request_body():
    return json.dumps({'messages': [{'role': 'system', 'content': 'system'},
        {'role': 'user', 'content': json.dumps({'facts': [{'fact_id': 'f1'}, {'fact_id': 'f2'}],
                                              'rule_keys': ['r1']})}]}).encode()


def response_body(selection=None):
    selection = selection or {'schema_version': '1.0', 'fact_order': ['f2', 'f1'], 'rule_order': ['r1']}
    return json.dumps({'message': {'role': 'assistant', 'content': json.dumps(selection)},
                      'done': True, 'done_reason': 'stop', 'total_duration': 2500000000,
                      'load_duration': 500000000, 'eval_count': 10}).encode()


class ResponseDouble:
    status = 200

    def __init__(self, raw):
        self.raw = raw

    def getheaders(self):
        return [('Content-Type', 'application/json')]

    def read1(self, amount):
        chunk, self.raw = self.raw[:amount], self.raw[amount:]
        return chunk


class ConnectionDouble:
    def __init__(self, host, port, timeout):
        self.sock = None
        self.response = ResponseDouble(response_body())
        self.closed = False

    def request(self, method, path, body, headers):
        self.request_data = (method, path, body, headers)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def test_http_adapter_preserves_exact_chunked_bytes_and_does_not_replace_transport():
    records = []
    connection = runner.RecordingHTTPConnection(*runner.ENDPOINT, timeout=10,
        records=records, connection_type=ConnectionDouble)
    connection.request('POST', '/api/chat', request_body(), {'Content-Type': 'application/json'})
    response = connection.getresponse()
    chunks = []
    while chunk := response.read1(19):
        chunks.append(chunk)
    connection.close()
    row = records[0]
    assert connection.connection.closed
    assert b''.join(chunks) == base64.b64decode(row['response_body_base64']) == response_body()
    assert row['request_body'].encode() == request_body()
    assert row['completion']['valid_complete_selection']
    assert row['completion']['timings_seconds']['total'] == 2.5
    assert row['completion']['queue_wait_seconds'] is None


@pytest.mark.parametrize('selection', [
    {'schema_version': '1.0', 'fact_order': ['f1', 'f1'], 'rule_order': ['r1']},
    {'schema_version': '1.0', 'fact_order': ['invented', 'f2'], 'rule_order': ['r1']},
    {'schema_version': '1.0', 'fact_order': ['f1', 'f2'], 'rule_order': ['r1'], 'prose': 'added'},
])
def test_raw_model_selection_cannot_pass_with_duplicate_missing_or_added_values(selection):
    actual = runner.completion_metadata({'http_status': 200, 'request_body': request_body().decode(),
                                         'response_json': json.loads(response_body(selection))})
    assert not actual['valid_complete_selection']


@pytest.mark.parametrize('change', ['http_error', 'not_done', 'wrong_role', 'tool_calls'])
def test_valid_selection_with_invalid_http_envelope_is_not_model_success(change):
    record = {'http_status': 200, 'request_body': request_body().decode(),
              'response_json': json.loads(response_body())}
    if change == 'http_error':
        record['http_status'] = 500
    elif change == 'not_done':
        record['response_json']['done'] = False
    elif change == 'wrong_role':
        record['response_json']['message']['role'] = 'user'
    else:
        record['response_json']['message']['tool_calls'] = [{'name': 'unexpected'}]
    assert not runner.completion_metadata(record)['valid_complete_selection']


def test_frozen_selection_excludes_fake_dependency_scenarios():
    from qa.scenarios import load_catalog
    catalog = load_catalog()
    selected = runner.selected_cases(catalog, None)
    assert [s.case.id for s in selected] == list(runner.DRAFT_IDS + runner.CONTROL_IDS)
    assert len(selected) == 13
    assert runner.selected_cases(catalog, 'QA-S14')[0].case.id == 'QA-S14'
    with pytest.raises(ValueError, match='QA_LLM_UNSUPPORTED_CASE'):
        runner.selected_cases(catalog, 'QA-S15')


@pytest.mark.parametrize('invalid_field', [b'"done":false,', b'"extra":NaN,'])
def test_recorded_envelope_rejects_duplicate_keys_and_nonfinite_values(invalid_field):
    raw = b'{' + invalid_field + response_body()[1:]
    records = []
    connection = runner.RecordingHTTPConnection(*runner.ENDPOINT, timeout=10,
        records=records, connection_type=ConnectionDouble)
    connection.connection.response = ResponseDouble(raw)
    connection.request('POST', '/api/chat', request_body(), {})
    response = connection.getresponse()
    while response.read1(19):
        pass
    connection.close()
    assert base64.b64decode(records[0]['response_body_base64']) == raw
    assert 'response_json' not in records[0]
    assert not records[0]['completion']['valid_complete_selection']
