"""Offline comparison must qualify retained attempts before trusting their labels."""
import base64
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from qa.tools import run_judge, score_judge
from qa.judge.contracts import response_schema


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


@pytest.fixture
def saved_attempt(tmp_path, monkeypatch):
    root = tmp_path
    folder = root / 'verification/qa-step-08-test/pilot'
    (folder / 'cases').mkdir(parents=True)
    dataset_folder = root / 'dataset'
    dataset_folder.mkdir()
    manifest = b'{"synthetic_unit_test":true}'
    (dataset_folder / 'manifest.json').write_bytes(manifest)
    dataset_hash = sha256(manifest).hexdigest()
    payload = {
        'case_id': 'J-001', 'criterion': 'grounding', 'mode': 'single',
        'sources': [{'ref_id': 'source:amount', 'text': 'Total: 12.30 USD.'}],
        'candidates': [{'label': 'A', 'report': {'status': 'draft'},
                        'fragments': [{'ref_id': 'A:amount', 'text': '12.30 USD'}]}],
    }
    case = {'id': 'J-001', 'split': 'calibration', 'family': 'synthetic-test-family',
            'expected_verdict': 'pass', 'expected_preference': 'not_applicable',
            'expected_kind': 'synthetic_rule', 'payload': payload,
            'programmatic_gate': {'status': 'PASS', 'issues': []}}
    dataset = {'cases': [case]}
    rubric = {'criteria': {'grounding': 'Check the supplied exact amount.'}}
    model, prompt = 'qwen2.5:14b', 'Synthetic unit test prompt.'
    run = {'dataset_sha256': dataset_hash, 'rubric_sha256': run_judge.content_hash(rubric),
           'system_prompt': prompt, 'system_prompt_sha256': run_judge.content_hash(prompt),
           'model': model, 'model_digest': 'a' * 64, 'temperature': 0,
           'num_predict': 512, 'num_ctx': 32768, 'planned_case_ids': ['J-001'],
           'selected_case_ids': ['J-001']}
    reply = {'schema_version': '1.0', 'rubric_version': '1.0', 'case_id': 'J-001',
             'criterion': 'grounding', 'verdict': 'pass', 'preference': 'not_applicable',
             'rationale': 'The cited amount matches.',
             'references': [{'ref_id': 'source:amount', 'quote': '12.30 USD'}]}
    response_text = json.dumps(reply)
    envelope = {'model': model, 'done': True, 'done_reason': 'stop',
                'message': {'role': 'assistant', 'content': response_text}}
    raw_bytes = json.dumps(envelope).encode('utf-8')
    request = {'model': model, 'stream': False, 'format': response_schema(),
               'options': {'temperature': 0, 'num_predict': 512, 'num_ctx': 32768},
               'messages': [{'role': 'system', 'content': prompt},
                            {'role': 'user', 'content': json.dumps({'rubric': rubric, 'task': payload})}]}
    transport = {'status': 'completed', 'request_body': json.dumps(request),
                 'response_body_base64': base64.b64encode(raw_bytes).decode('ascii'),
                 'response_body_sha256': sha256(raw_bytes).hexdigest(),
                 'response_body': raw_bytes.decode('utf-8'), 'response_text': response_text,
                 'http_status': 200, 'elapsed_seconds': 1.0}
    key = run_judge.evaluation_key(payload, rubric, run['model_digest'], prompt)
    observation = run_judge.assess_record(case, transport, key)
    record = {'observation': observation, 'transport': transport, 'payload': payload}
    result = {'status': 'INCOMPLETE', 'observations': [observation],
              'source_files_unchanged': True, 'model_identity_stable': True, 'full_planned_matrix': True}
    model_identity = {'model': {'digest': run['model_digest']}, 'server': {'version': 'test'}, 'show': {}}
    files = {'run.json': run, 'result.json': result, 'dataset.json': dataset,
             'rubric.json': rubric, 'source-hashes.json': {'unit-test': 'unchanged'},
             'source-hashes-after.json': {'unit-test': 'unchanged'}, 'cases/J-001.json': record,
             'ollama-before.json': model_identity, 'ollama-after.json': model_identity}
    for name, value in files.items():
        save(folder / name, value)
    monkeypatch.setattr(score_judge, 'ROOT', root)
    monkeypatch.setattr(score_judge, 'DEFAULT_FOLDER', dataset_folder)
    monkeypatch.setattr(score_judge, 'load_dataset', lambda: deepcopy(dataset))
    return {'root': root, 'folder': folder, 'case': case, 'record': record, 'files': files,
            'dataset_hash': dataset_hash, 'reply': reply}


def test_valid_saved_attempt_is_recomputed_and_every_input_is_hashed(saved_attempt):
    state = saved_attempt
    dataset, plan, result, observations, digest, hashes = score_judge.verify_saved(state['folder'])
    assert dataset['cases'] == [state['case']]
    assert digest == state['dataset_hash']
    assert observations == [state['record']['observation']]
    assert observations[0]['response']['verdict'] == 'pass'
    assert result['status'] == 'INCOMPLETE'
    assert plan['planned_case_ids'] == ['J-001']
    assert len(hashes) == 9
    for relative, expected in hashes.items():
        assert sha256((state['root'] / relative).read_bytes()).hexdigest() == expected


@pytest.mark.parametrize('mutation', ['raw_hash', 'raw_bytes', 'response_text', 'request_payload',
                                     'request_model', 'request_prompt', 'missing_request',
                                     'payload', 'observation_verdict', 'observation_cache_key',
                                     'invalid_envelope'])
def test_tampered_saved_attempt_cannot_supply_a_trusted_judge_label(saved_attempt, mutation):
    state = saved_attempt
    record = deepcopy(state['record'])
    raw = record['transport']
    if mutation == 'raw_hash':
        raw['response_body_sha256'] = '0' * 64
    elif mutation == 'raw_bytes':
        raw['response_body_base64'] = base64.b64encode(b'altered bytes').decode('ascii')
    elif mutation == 'response_text':
        raw['response_text'] = '{}'
    elif mutation.startswith('request_'):
        request = json.loads(raw['request_body'])
        if mutation == 'request_payload':
            content = json.loads(request['messages'][1]['content'])
            content['task']['sources'][0]['text'] = 'Total: 120.30 USD.'
            request['messages'][1]['content'] = json.dumps(content)
        elif mutation == 'request_model':
            request['model'] = 'other-model'
        else:
            request['messages'][0]['content'] = 'Changed judging rules.'
        raw['request_body'] = json.dumps(request)
    elif mutation == 'missing_request':
        raw['request_body'] = None
    elif mutation == 'payload':
        record['payload']['sources'][0]['text'] = 'Total: 120.30 USD.'
    elif mutation == 'observation_verdict':
        record['observation']['response']['verdict'] = 'fail'
    elif mutation == 'observation_cache_key':
        record['observation']['cache_key'] = 'other-inputs'
    else:
        envelope = json.loads(raw['response_body'])
        envelope['done'] = False
        body = json.dumps(envelope).encode('utf-8')
        raw.update(response_body=body.decode('utf-8'), response_body_sha256=sha256(body).hexdigest(),
                   response_body_base64=base64.b64encode(body).decode('ascii'))
    save(state['folder'] / 'cases/J-001.json', record)
    with pytest.raises(ValueError, match='QA_JUDGE_SAVED_'):
        score_judge.verify_saved(state['folder'])


def test_missing_case_file_is_detected_against_saved_observation_inventory(saved_attempt):
    state = saved_attempt
    (state['folder'] / 'cases/J-001.json').unlink()
    with pytest.raises(ValueError, match='QA_JUDGE_SAVED_OBSERVATION_SET'):
        score_judge.verify_saved(state['folder'])


@pytest.mark.parametrize('name', ['dataset.json', 'rubric.json', 'source-hashes-after.json'])
def test_altered_dataset_rubric_or_measured_source_identity_is_rejected(saved_attempt, name):
    state = saved_attempt
    altered = deepcopy(state['files'][name])
    altered['unexpected'] = 'tampered'
    save(state['folder'] / name, altered)
    with pytest.raises(ValueError, match='QA_JUDGE_'):
        score_judge.verify_saved(state['folder'])


def test_claimed_model_stability_must_match_retained_identity(saved_attempt):
    state = saved_attempt
    identity = deepcopy(state['files']['ollama-after.json'])
    identity['model']['digest'] = 'b' * 64
    save(state['folder'] / 'ollama-after.json', identity)
    with pytest.raises(ValueError, match='QA_JUDGE_SAVED_MODEL_IDENTITY'):
        score_judge.verify_saved(state['folder'])


def test_offline_human_comparison_uses_supplied_ratings_without_rewriting_history(saved_attempt, monkeypatch):
    import socket
    from qa.judge.transport import JudgeTransport

    def forbidden(*args, **kwargs):
        raise AssertionError('Offline comparison attempted a network or model call')

    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(JudgeTransport, 'complete', forbidden)
    state = saved_attempt
    review = {'schema_version': '1.0', 'rubric_version': '1.0',
              'dataset_sha256': state['dataset_hash'], 'reviewer_id': 'synthetic-unit-test-reviewer',
              'reviewed_at': '2026-10-21T12:00:00Z', 'ratings': [
                  {key: value for key, value in state['reply'].items()
                   if key not in ('schema_version', 'rubric_version')}]}
    review_path = state['root'] / 'review.json'
    save(review_path, review)
    before = {path: path.read_bytes() for path in state['folder'].rglob('*.json')}
    output = state['root'] / 'comparison'
    assert score_judge.run_comparison(state['folder'], review_path, output) == 0
    result = json.loads((output / 'result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'PASS' and result['new_model_calls'] == 0
    assert result['summary']['human_calibration']['status'] == 'EVIDENCE_COMPLETE'
    assert result['advisory_only'] is True and result['product_acceptance'] == 'INCOMPLETE'
    assert result['suitability'] == 'NOT_ESTABLISHED'
    assert result['human_reviews_sha256'] == sha256(review_path.read_bytes()).hexdigest()
    assert json.loads((output / 'human-reviews.json').read_text(encoding='utf-8')) == review
    assert all(path.read_bytes() == contents for path, contents in before.items())
    with pytest.raises(FileExistsError):
        score_judge.run_comparison(state['folder'], review_path, output)


def test_empty_review_template_is_not_filled_from_the_oracle(saved_attempt):
    state = saved_attempt
    review = {'schema_version': '1.0', 'rubric_version': '1.0',
              'dataset_sha256': state['dataset_hash'], 'reviewer_id': '', 'reviewed_at': '', 'ratings': []}
    review_path = state['root'] / 'empty-review.json'
    save(review_path, review)
    output = state['root'] / 'comparison'
    with pytest.raises(ValueError):
        score_judge.run_comparison(state['folder'], review_path, output)
    assert not output.exists()
    assert json.loads(review_path.read_text(encoding='utf-8')) == review


def test_saved_run_path_scope_requires_an_existing_judge_run(saved_attempt):
    state = saved_attempt
    assert score_judge.saved_path(state['folder']) == state['folder']
    with pytest.raises(ValueError, match='SAVED_RUN_SCOPE'):
        score_judge.saved_path(state['root'] / 'src')
    with pytest.raises(ValueError, match='SAVED_RUN_MISSING'):
        score_judge.saved_path(state['root'] / 'var/qa/judge/absent')
