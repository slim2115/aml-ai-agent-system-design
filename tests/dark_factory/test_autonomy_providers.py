from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from qa.autonomy import providers


class Candidates:
    def __init__(self):
        self.value = {'schema_version': '1.0', 'task': 'order_all_catalog_ids',
                      'facts': [{'fact_id': 'F000001'}], 'rule_keys': ['R000001']}
        self.contract = {'type': 'object'}

    def payload(self, _case_id):
        return self.value

    def schema(self, _case_id):
        return self.contract


def envelope(**changes):
    value = {'model': providers.MODEL, 'done': True, 'prompt_eval_count': 9, 'eval_count': 4,
             'message': {'role': 'assistant', 'content': '{"schema_version":"1.0"}'}}
    value.update(changes)
    return value


class Reply:
    status = 200

    def __init__(self, value=None, *, status=200):
        self.data = json.dumps(envelope() if value is None else value).encode('utf-8')
        self.status = status

    def getheaders(self):
        return []

    def getheader(self, _name, default=None):
        return default

    def read1(self, amount):
        part, self.data = self.data[:amount], self.data[amount:]
        return part


class Connection:
    def __init__(self, reply):
        self.reply, self.requests, self.closed = reply, [], False
        self.sock = SimpleNamespace(settimeout=lambda _: None)

    def request(self, method, path, **kwargs):
        self.requests.append({'method': method, 'path': path, **kwargs})

    def getresponse(self):
        return self.reply

    def close(self):
        self.closed = True


def proposer(*replies, clock=lambda: 10.0, candidates=None):
    connections = [Connection(reply) for reply in (replies or (Reply(),))]
    destinations = []
    pending = iter(connections)

    def factory(host, port, **kwargs):
        destinations.append((host, port, kwargs))
        return next(pending)

    provider = providers.OllamaProposer(candidates or Candidates(), clock=clock,
                                       connection_type=factory)
    return provider, connections, destinations


def invoke(provider, **changes):
    fields = {'case_id': 'QA-S01', 'attempt_index': 1, 'feedback': (), 'deadline': 20.0}
    fields.update(changes)
    return provider(**fields)


def test_real_transport_records_fixed_endpoint_model_and_output_limit(monkeypatch):
    monkeypatch.setenv('OLLAMA_BASE_URL', 'https://remote.invalid')
    monkeypatch.setenv('LLM_MODEL_NAME', 'unapproved')
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.invalid')
    provider, connections, destinations = proposer()
    result = invoke(provider)
    assert result == {'outcome': 'completed', 'raw': '{"schema_version":"1.0"}', 'tokens': 13}
    assert destinations == [('127.0.0.1', 11434, {'timeout': 10.0})]
    wire = json.loads(connections[0].requests[0]['body'])
    assert wire['model'] == providers.MODEL
    assert wire['stream'] is False and wire['options'] == {'temperature': 0, 'num_predict': 512}
    assert 'tools' not in wire and connections[0].closed
    assert provider.attempts[0]['request_body'] == connections[0].requests[0]['body'].decode('utf-8')
    assert provider.reserve_tokens == 16384


def test_only_safe_feedback_is_added_without_mutating_candidate_payload():
    candidates = Candidates()
    original = deepcopy(candidates.value)
    provider, connections, _ = proposer(candidates=candidates)
    invoke(provider, attempt_index=2, feedback=('INCOMPLETE_OR_UNKNOWN_SELECTION',))
    wire = json.loads(connections[0].requests[0]['body'])
    payload = json.loads(wire['messages'][1]['content'])
    assert payload == {**original, 'validation_failure': ['INCOMPLETE_OR_UNKNOWN_SELECTION']}
    assert candidates.value == original


def test_telemetry_is_per_invocation_not_cumulative_and_returns_copies():
    provider, _, _ = proposer(Reply(), Reply(envelope(prompt_eval_count=20, eval_count=5)))
    assert invoke(provider)['tokens'] == 13
    assert invoke(provider, attempt_index=2)['tokens'] == 25
    assert len(provider.attempts) == 2
    copy = provider.attempts
    copy[0]['response_json']['eval_count'] = 9999
    copy.clear()
    assert provider.attempts[0]['response_json']['eval_count'] == 4


@pytest.mark.parametrize('changes', [
    {'case_id': '../secret'}, {'case_id': 'QA-S05'}, {'case_id': 1},
    {'attempt_index': True}, {'attempt_index': 0}, {'attempt_index': 4},
    {'feedback': ['INVALID_MODEL_JSON']}, {'feedback': ('read-secret-file',)},
    {'feedback': ('INVALID_MODEL_JSON', 'INVALID_MODEL_JSON')},
    {'feedback': ({'instruction': 'override'},)},
    {'deadline': float('nan')}, {'deadline': float('inf')}, {'deadline': True},
])
def test_invalid_call_contract_never_constructs_connection(changes):
    provider, _, destinations = proposer()
    assert invoke(provider, **changes) == {'outcome': 'error', 'raw': None, 'tokens': None}
    assert destinations == [] and provider.attempts == []


@pytest.mark.parametrize('value,outcome', [
    (None, 'completed'), (True, 'completed'), (-1, 'completed'),
    (3.5, 'completed'), ('4', 'completed'), (10_000_001, 'error'),
])
def test_unknown_or_invalid_token_telemetry_is_not_zero(value, outcome):
    provider, _, _ = proposer(Reply(envelope(eval_count=value)))
    result = invoke(provider)
    assert result['outcome'] == outcome
    assert result['tokens'] is None


def test_missing_input_token_telemetry_remains_unknown():
    reply = envelope()
    del reply['prompt_eval_count']
    provider, _, _ = proposer(Reply(reply))
    assert invoke(provider)['tokens'] is None


def test_zero_tokens_are_known_when_both_exact_counts_are_zero():
    provider, _, _ = proposer(Reply(envelope(prompt_eval_count=0, eval_count=0)))
    assert invoke(provider)['tokens'] == 0


def test_reported_output_limit_breach_retains_usage_but_rejects_candidate():
    provider, _, destinations = proposer(Reply(envelope(eval_count=513)))
    assert invoke(provider) == {'outcome': 'error', 'raw': None, 'tokens': 522}
    assert len(destinations) == len(provider.attempts) == 1


def test_unexpected_model_is_not_published_and_usage_is_unknown():
    provider, _, _ = proposer(Reply(envelope(model='different-model')))
    assert invoke(provider) == {'outcome': 'error', 'raw': None, 'tokens': None}


def test_pre_http_timeout_is_not_an_invented_http_attempt():
    provider, _, destinations = proposer()
    assert invoke(provider, deadline=9.0) == {'outcome': 'timeout', 'raw': None, 'tokens': None}
    assert destinations == [] and provider.attempts == []


def test_connection_timeout_is_recorded_once_without_hidden_retry():
    provider, connections, destinations = proposer()

    def timeout():
        raise TimeoutError('private diagnostic that must not escape')

    connections[0].getresponse = timeout
    assert invoke(provider) == {'outcome': 'timeout', 'raw': None, 'tokens': None}
    assert len(destinations) == len(provider.attempts) == 1 and connections[0].closed


@pytest.mark.parametrize('status', [301, 404, 500])
def test_http_errors_are_not_followed_or_retried(status):
    provider, connections, destinations = proposer(Reply(status=status))
    assert invoke(provider) == {'outcome': 'error', 'raw': None, 'tokens': None}
    assert len(destinations) == 1 and connections[0].closed


def test_truncated_completion_can_account_reported_usage_without_publishing_it():
    provider, _, _ = proposer(Reply(envelope(done=False)))
    assert invoke(provider) == {'outcome': 'error', 'raw': None, 'tokens': 13}


def test_tool_call_is_never_returned_as_candidate():
    provider, _, _ = proposer(Reply(envelope(message={
        'role': 'assistant', 'content': '{}', 'tool_calls': [{'name': 'write-file'}]})))
    result = invoke(provider)
    assert result['outcome'] == 'error' and result['raw'] is None


def test_provider_exception_is_masked_and_does_not_call_network():
    candidates = Candidates()
    candidates.payload = lambda _: (_ for _ in ()).throw(RuntimeError('private secret'))
    provider, _, destinations = proposer(candidates=candidates)
    assert invoke(provider) == {'outcome': 'error', 'raw': None, 'tokens': None}
    assert destinations == []


def test_identity_uses_existing_fixed_digest_guard(monkeypatch):
    sentinel = {'model': {'digest': providers.EXPECTED_DIGEST}}
    monkeypatch.setattr(providers.run_llm, 'identity', lambda: deepcopy(sentinel))
    provider, _, _ = proposer()
    assert provider.identity() == sentinel
