"""Compact Judge protocol keeps strict evidence checks and legacy defaults.

All transport observations use an in-memory HTTP double. No model or service
is contacted; valid quotations here do not establish semantic judgment quality.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from qa.judge import protocol_v2
from qa.judge.contracts import response_schema, validate_response as validate_v1
from qa.judge.transport import ENDPOINT, MODEL, SYSTEM_PROMPT, JudgeTransport, request_config


PAYLOAD = {
    'case_id': 'blind-compact-1', 'criterion': 'grounding', 'mode': 'single',
    'sources': [
        {'ref_id': 'source/amount', 'text': 'Amount: 12.30 USD'},
        {'ref_id': 'source/status', 'text': 'Status: completed'},
    ],
    'candidates': [{'label': 'A', 'report': {'amount': '12.30'}, 'fragments': [
        {'ref_id': 'A/fact/0', 'text': 'The amount is 12.30 USD.'},
    ]}],
}


def response(**changes):
    value = dict(schema_version='1.0', rubric_version='1.0', case_id=PAYLOAD['case_id'],
        criterion='grounding', verdict='pass',
        references=[dict(ref_id='source/amount', quote='12.30 USD')],
        rationale='Указанная сумма есть в источнике.', preference='not_applicable')
    value.update(changes)
    return value


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def test_exact_short_quote_preserves_existing_response_contract():
    value = response()
    assert protocol_v2.validate_response(encoded(value), PAYLOAD) == value
    assert validate_v1(encoded(value), PAYLOAD) == value


def test_ordering_prompt_matches_frozen_payload_representation():
    from qa.judge.dataset import load_dataset
    for case in load_dataset()['cases']:
        for candidate in case['payload']['candidates']:
            assert 'found_facts' not in candidate['report']
            facts = [json.loads(fragment['text']) for fragment in candidate['fragments']
                     if '/fact/' in fragment['ref_id']]
            assert facts and all('fact' in fact and 'evidence' in fact for fact in facts)
    assert 'candidate.fragments in the supplied array order' in protocol_v2.SYSTEM_PROMPT
    assert 'candidate.report.found_facts' not in protocol_v2.SYSTEM_PROMPT


def test_every_frozen_case_fits_without_truncating_task_or_rubric():
    from qa.judge.dataset import load_dataset
    from qa.tools.run_judge import RUBRIC
    rubric = json.loads(RUBRIC.read_text(encoding='utf-8'))
    def offline_connection(*args, **kwargs):
        raise RuntimeError('offline request construction only')
    for case in load_dataset()['cases']:
        raw = JudgeTransport(profile='compact-v2', connection_factory=offline_connection,
                             clock=lambda: 10).complete(case['payload'], rubric, deadline=30)
        assert raw['request_body'] is not None, (case['id'], raw['error_code'])
        request = json.loads(raw['request_body'])
        user = json.loads(request['messages'][1]['content'])
        assert user['task'] == case['payload'] and user['rubric'] == rubric


@pytest.mark.parametrize('reference,error', [
    ({'ref_id': 'source/missing', 'quote': '12.30 USD'}, 'QA_JUDGE_REFERENCE_ID'),
    ({'ref_id': 'source/amount', 'quote': 'The amount is 12.30 USD.'}, 'QA_JUDGE_REFERENCE_QUOTE'),
    ({'ref_id': 'source/amount', 'quote': 'twelve dollars and thirty cents'}, 'QA_JUDGE_REFERENCE_QUOTE'),
    ({'ref_id': 'source/amount', 'quote': ' '}, 'QA_JUDGE_REFERENCE_QUOTE'),
])
def test_compact_profile_never_repairs_wrong_or_paraphrased_quotes(reference, error):
    with pytest.raises(ValueError, match='^' + error + '$'):
        protocol_v2.validate_response(encoded(response(references=[reference])), PAYLOAD)


def test_duplicate_reference_is_still_invalid_even_below_compact_limit():
    reference = dict(ref_id='source/amount', quote='12.30 USD')
    with pytest.raises(ValueError, match='^QA_JUDGE_REFERENCE_ID$'):
        protocol_v2.validate_response(encoded(response(references=[reference, reference])), PAYLOAD)


def test_third_valid_reference_is_rejected_without_weakening_v1():
    references = [dict(ref_id='source/amount', quote='12.30 USD'),
        dict(ref_id='source/status', quote='completed'),
        dict(ref_id='A/fact/0', quote='The amount')]
    raw = encoded(response(references=references))
    assert len(validate_v1(raw, PAYLOAD)['references']) == 3
    with pytest.raises(ValueError, match='^QA_JUDGE_V2_REFERENCE_LIMIT$'):
        protocol_v2.validate_response(raw, PAYLOAD)


@pytest.mark.parametrize('length,accepted', [(160, True), (161, False)])
def test_quote_length_boundary_counts_unicode_characters(length, accepted):
    payload = deepcopy(PAYLOAD)
    payload['sources'][0]['text'] = 'я' * 200
    raw = encoded(response(references=[dict(ref_id='source/amount', quote='я' * length)]))
    assert validate_v1(raw, payload)['references'][0]['quote'] == 'я' * length
    if accepted:
        assert protocol_v2.validate_response(raw, payload)['references'][0]['quote'] == 'я' * length
    else:
        with pytest.raises(ValueError, match='^QA_JUDGE_V2_QUOTE_LIMIT$'):
            protocol_v2.validate_response(raw, payload)


@pytest.mark.parametrize('length,accepted', [(300, True), (301, False)])
def test_rationale_length_boundary_keeps_short_explanation(length, accepted):
    raw = encoded(response(rationale='я' * length))
    assert len(validate_v1(raw, PAYLOAD)['rationale']) == length
    if accepted:
        assert len(protocol_v2.validate_response(raw, PAYLOAD)['rationale']) == length
    else:
        with pytest.raises(ValueError, match='^QA_JUDGE_V2_RATIONALE_LIMIT$'):
            protocol_v2.validate_response(raw, PAYLOAD)


def pair_payload():
    payload = deepcopy(PAYLOAD)
    payload['mode'] = 'pair'
    payload['candidates'].append({'label': 'B', 'report': {'amount': '12.30'},
        'fragments': [{'ref_id': 'B/fact/0', 'text': '12.30 USD is the amount.'}]})
    return payload


@pytest.mark.parametrize('verdict,preference', [('fail', 'A'), ('pass', 'not_applicable'), ('abstain', 'A')])
def test_pair_preference_rules_are_not_relaxed(verdict, preference):
    with pytest.raises(ValueError, match='^QA_JUDGE_PAIR_PREFERENCE$'):
        protocol_v2.validate_response(encoded(response(verdict=verdict, preference=preference)), pair_payload())


def test_pair_can_cite_both_candidates_without_exhaustive_references():
    value = response(preference='tie', references=[
        dict(ref_id='A/fact/0', quote='12.30 USD'), dict(ref_id='B/fact/0', quote='12.30 USD')])
    assert protocol_v2.validate_response(encoded(value), pair_payload()) == value


def test_abstention_does_not_require_fabricated_citation():
    value = response(verdict='abstain', references=[])
    assert protocol_v2.validate_response(encoded(value), PAYLOAD) == value


def test_dynamic_schema_binds_task_and_available_reference_ids():
    schema = protocol_v2.schema(PAYLOAD)
    properties = schema['properties']
    assert schema['additionalProperties'] is False
    assert properties['case_id']['const'] == PAYLOAD['case_id']
    assert properties['criterion']['const'] == PAYLOAD['criterion']
    assert set(properties['references']['items']['properties']['ref_id']['enum']) == {
        'source/amount', 'source/status', 'A/fact/0'}
    assert properties['references']['maxItems'] == 2
    assert properties['references']['items']['properties']['quote']['maxLength'] == 160
    assert properties['rationale']['maxLength'] == 300
    assert set(schema['required']) == set(response_schema()['required'])


def test_generated_schema_and_options_do_not_leak_between_cases():
    first = protocol_v2.schema(PAYLOAD)
    first['properties']['case_id']['const'] = 'modified'
    first['properties']['references']['items']['properties']['ref_id']['enum'].append('foreign')
    changed_options = protocol_v2.options()
    changed_options['num_predict'] = 9999
    other = deepcopy(PAYLOAD)
    other['case_id'] = 'blind-compact-2'
    second = protocol_v2.schema(other)
    assert second['properties']['case_id']['const'] == 'blind-compact-2'
    assert 'foreign' not in second['properties']['references']['items']['properties']['ref_id']['enum']
    assert protocol_v2.options() == {'temperature': 0, 'num_predict': 1024, 'num_ctx': 32768}


def test_legacy_request_profile_remains_the_default():
    config = request_config(PAYLOAD)
    assert config == request_config(PAYLOAD, profile='legacy-v1')
    assert config['profile'] == 'legacy-v1'
    assert config['system_prompt'] == SYSTEM_PROMPT
    assert config['options'] == {'temperature': 0, 'num_predict': 512, 'num_ctx': 32768}
    assert config['response_schema'] == response_schema()


@pytest.mark.parametrize('profile', ['unknown', '', None])
def test_unknown_transport_profile_is_rejected_before_any_connection(profile):
    with pytest.raises(ValueError, match='^QA_JUDGE_UNKNOWN_PROFILE$'):
        JudgeTransport(profile=profile)


class _Response:
    status = 200

    def __init__(self, done_reason='stop'):
        self.raw = encoded(dict(model=MODEL, done=True, done_reason=done_reason,
            message=dict(role='assistant', content=encoded(response())))).encode('utf-8')

    def getheaders(self):
        return []

    def getheader(self, _name, default=None):
        return default

    def read1(self, count):
        block, self.raw = self.raw[:count], self.raw[count:]
        return block


class _Connection:
    def __init__(self, done_reason):
        self.reply, self.requests, self.closed = _Response(done_reason), [], False
        self.sock = SimpleNamespace(settimeout=lambda _value: None)

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self.reply

    def close(self):
        self.closed = True


@pytest.mark.parametrize('profile,predict', [('legacy-v1', 512), ('compact-v2', 1024)])
def test_transport_sends_selected_profile_once_and_retains_raw_evidence(profile, predict):
    connection = _Connection('stop')
    destinations = []

    def factory(host, port, **kwargs):
        destinations.append((host, port))
        return connection

    result = JudgeTransport(profile=profile, connection_factory=factory, clock=lambda: 10).complete(
        PAYLOAD, {'grounding': 'Check provided sources.'}, deadline=30)
    assert destinations == [ENDPOINT]
    assert connection.closed and len(connection.requests) == 1
    assert result['status'] == 'completed'
    args, kwargs = connection.requests[0]
    request = json.loads(kwargs['body'])
    config = request_config(PAYLOAD, profile=profile)
    assert args == ('POST', '/api/chat')
    assert request['options']['num_predict'] == predict
    assert request['options'] == config['options']
    assert request['format'] == config['response_schema']
    assert request['messages'][0]['content'] == config['system_prompt']
    if profile == 'compact-v2':
        assert protocol_v2.SYSTEM_PROMPT == request['messages'][0]['content']
    assert json.loads(request['messages'][1]['content'])['task'] == PAYLOAD
    assert result['request_body'] == kwargs['body'].decode('utf-8')
    assert json.loads(result['response_body'])['message']['content'] == encoded(response())


def test_more_output_tokens_never_turn_truncation_into_success():
    connection = _Connection('length')
    result = JudgeTransport(profile='compact-v2', connection_factory=lambda *_args, **_kwargs: connection,
        clock=lambda: 10).complete(PAYLOAD, {'grounding': 'Check sources.'}, deadline=30)
    assert result['status'] == 'error'
    assert result['error_code'] == 'QA_JUDGE_INVALID_ENVELOPE'
    assert result['response_text'] is None
    assert json.loads(result['response_body'])['done_reason'] == 'length'
    assert connection.closed and len(connection.requests) == 1
