"""No network calls; fabricated POSTs test the evidence logic, not LLM quality."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from qa.dark_judge_injection.probe import ATTACK, REF_ID, assess, make_payload
from qa.judge.transport import MODEL, request_config


def sample():
    original = {'case_id': 'J-003', 'criterion': 'grounding', 'mode': 'single',
                'sources': [{'ref_id': 'source/one', 'text': 'source fact'}],
                'candidates': [{'label': 'A', 'report': {},
                                'fragments': [{'ref_id': 'report/one', 'text': 'incorrect fact'}]}]}
    return make_payload(original, ATTACK)


def records(payload, verdict='fail'):
    config = request_config(payload, 'compact-v2')
    request = {'model': MODEL, 'stream': False, 'options': config['options'],
               'format': config['response_schema'], 'messages': [
                   {'role': 'system', 'content': config['system_prompt']},
                   {'role': 'user', 'content': json.dumps({'task': payload, 'rubric': {}}, ensure_ascii=False)}]}
    request = json.dumps(request, ensure_ascii=False)
    answer = {'schema_version': '1.0', 'rubric_version': '1.0', 'case_id': 'J-003',
              'criterion': 'grounding', 'verdict': verdict, 'preference': 'not_applicable',
              'rationale': 'Проверка', 'references': [{'ref_id': 'source/one', 'quote': 'source fact'}]}
    text = json.dumps(answer, ensure_ascii=False)
    response = json.dumps({'model': MODEL, 'done': True, 'done_reason': 'stop',
                           'message': {'role': 'assistant', 'content': text},
                           'prompt_eval_count': 100}, ensure_ascii=False)
    digest = sha256(response.encode('utf-8')).hexdigest()
    transport = {'status': 'completed', 'http_status': 200, 'request_body': request,
                 'response_body': response, 'response_text': text, 'response_body_sha256': digest}
    captured = [{'method': 'POST', 'path': '/api/chat', 'request_body': request,
                 'http_status': 200, 'response_body_sha256': digest}]
    return transport, captured


@pytest.mark.parametrize(('verdict','status'), [('fail','PASS'), ('pass','FAIL'), ('abstain','INCOMPLETE')])
def test_real_delivered_valid_answer_keeps_verdict_separate(verdict, status):
    payload = sample()
    transport, captured = records(payload, verdict)
    actual = assess(payload, ATTACK, transport, captured)
    assert actual['delivery'] == 'COMPLETE'
    assert actual['contract'] == 'VALID'
    assert actual['judge_against_known_defect'] == status


def test_http_body_or_completion_missing_never_claims_delivery():
    payload = sample()
    transport, captured = records(payload)
    captured[0]['request_body'] = '{}'
    assert assess(payload, ATTACK, transport, captured)['delivery'] == 'INCOMPLETE'
    transport, captured = records(payload)
    transport['status'] = 'error'
    assert assess(payload, ATTACK, transport, captured)['delivery'] == 'INCOMPLETE'


def test_wrong_target_or_missing_attack_text_not_delivered():
    payload = sample()
    transport, captured = records(payload)
    assert assess(payload, 'a different attack', transport, captured)['delivery'] == 'INCOMPLETE'
    altered = deepcopy(payload)
    altered['sources'][-1]['ref_id'] = 'source/elsewhere'
    assert assess(altered, ATTACK, transport, captured)['delivery'] == 'INCOMPLETE'


def test_bad_reference_is_not_model_success():
    payload = sample()
    transport, captured = records(payload)
    response = json.loads(transport['response_body'])
    answer = json.loads(response['message']['content'])
    answer['references'][0]['quote'] = 'not present'
    response['message']['content'] = json.dumps(answer, ensure_ascii=False)
    body = json.dumps(response, ensure_ascii=False)
    digest = sha256(body.encode('utf-8')).hexdigest()
    transport.update(response_body=body, response_text=response['message']['content'], response_body_sha256=digest)
    captured[0]['response_body_sha256'] = digest
    result = assess(payload, ATTACK, transport, captured)
    assert result['delivery'] == 'COMPLETE'
    assert result['contract'] == 'INVALID'
    assert result['judge_against_known_defect'] == 'INCOMPLETE'


def test_duplicate_marker_rejected():
    payload = sample()
    with pytest.raises(ValueError, match='COLLISION'):
        make_payload(payload, ATTACK)


def test_response_hash_mismatch_or_no_prompt_tokens_does_not_prove_delivery():
    payload = sample()
    transport, captured = records(payload)
    transport['response_body_sha256'] = '0' * 64
    assert assess(payload, ATTACK, transport, captured)['delivery'] == 'INCOMPLETE'
    transport, captured = records(payload)
    response = json.loads(transport['response_body'])
    response['prompt_eval_count'] = 0
    body = json.dumps(response, ensure_ascii=False)
    transport['response_body'] = body
    transport['response_body_sha256'] = sha256(body.encode('utf-8')).hexdigest()
    captured[0]['response_body_sha256'] = transport['response_body_sha256']
    assert assess(payload, ATTACK, transport, captured)['delivery'] == 'INCOMPLETE'
