"""Versioned offline qualification retains raw errors and interrupted requests."""
import base64
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from qa.judge.transport import request_config
from qa.tools import run_judge, score_judge
from tests.evaluator.test_qa_judge_saved import saved_attempt, save


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


@pytest.fixture
def compact_saved(saved_attempt):
    state = saved_attempt
    folder = state['folder']
    run = state['files']['run.json']
    state['files']['rubric.json'] = read(run_judge.RUBRIC)
    run['rubric_sha256'] = run_judge.content_hash(state['files']['rubric.json'])
    config = request_config(state['case']['payload'], 'compact-v2')
    run.update(profile='compact-v2', model_digest=run_judge.EXPECTED_DIGEST,
               system_prompt=config['system_prompt'],
               system_prompt_sha256=run_judge.content_hash(config['system_prompt']),
               seed='not_explicitly_set', automatic_retries=0, cache='disabled',
               prepare_only=False, **config['options'],
               response_schema_sha256_by_case={'J-001': run_judge.content_hash(config['response_schema'])})
    record = state['record']
    request = json.loads(record['transport']['request_body'])
    request['options'] = config['options']
    request['format'] = config['response_schema']
    request['messages'][0]['content'] = config['system_prompt']
    user = json.loads(request['messages'][1]['content'])
    user['rubric'] = state['files']['rubric.json']
    user['response_schema'] = config['response_schema']
    request['messages'][1]['content'] = json.dumps(user)
    record['transport']['request_body'] = json.dumps(request)
    key = run_judge.evaluation_key(state['case']['payload'], state['files']['rubric.json'],
                                  run['model_digest'], run['system_prompt'], profile='compact-v2')
    record['observation'] = run_judge.assess_record(state['case'], record['transport'], key,
                                                 profile='compact-v2')
    state['files']['result.json'].update(profile='compact-v2', observations=[record['observation']])
    for name in ('ollama-before.json', 'ollama-after.json'):
        state['files'][name]['model']['digest'] = run['model_digest']
    for name, value in state['files'].items():
        save(folder / name, value)
    (folder / 'attempts').mkdir()
    (folder / 'identities').mkdir()
    start = dict(case_id='J-001', profile='compact-v2', evaluation_key=key,
                 request_config_sha256=run_judge.content_hash(config),
                 status='REQUEST_MAY_BE_IN_FLIGHT', automatic_retries=0,
                 recorded_at_utc='2026-10-25T10:00:00+00:00')
    save(folder / 'attempts/J-001.started.json', start)
    save(folder / 'identities/J-001.before.json', state['files']['ollama-before.json'])
    refresh_completion(state)
    return state


def refresh_completion(state):
    save(state['folder'] / 'attempts/J-001.completed.json', dict(
        case_id='J-001', status=state['record']['observation']['status'],
        observation_sha256=sha256((state['folder'] / 'cases/J-001.json').read_bytes()).hexdigest(),
        recorded_at_utc='2026-10-25T10:00:01+00:00'))


def set_reply(state, reply, *, assess_profile='compact-v2'):
    record = state['record']
    raw = record['transport']
    text = json.dumps(reply)
    envelope = json.loads(raw['response_body'])
    envelope['message']['content'] = text
    body = json.dumps(envelope).encode()
    raw.update(response_text=text, response_body=body.decode(),
               response_body_sha256=sha256(body).hexdigest(),
               response_body_base64=base64.b64encode(body).decode())
    observation = run_judge.assess_record(state['case'], raw, record['observation']['cache_key'],
                                        profile=assess_profile)
    record['observation'] = observation
    save(state['folder'] / 'cases/J-001.json', record)
    result = read(state['folder'] / 'result.json')
    result['observations'] = [observation]
    save(state['folder'] / 'result.json', result)
    refresh_completion(state)


def test_compact_attempt_roundtrip_hashes_journal_and_case_identity(compact_saved):
    state = compact_saved
    _, plan, result, rows, _, hashes = score_judge.verify_saved(state['folder'])
    assert plan['profile'] == result['profile'] == 'compact-v2'
    assert rows[0]['status'] == 'completed'
    assert len(hashes) == 12
    for path, digest in hashes.items():
        assert sha256((state['root'] / path).read_bytes()).hexdigest() == digest
    for suffix in ('attempts/J-001.started.json', 'attempts/J-001.completed.json',
                   'identities/J-001.before.json'):
        assert any(path.endswith(suffix) for path in hashes)


def test_new_evidence_namespace_is_available(compact_saved):
    folder = compact_saved['root'] / 'verification/qa-judge-v2-test/new-run'
    folder.mkdir(parents=True)
    assert score_judge.saved_path(folder) == folder


@pytest.mark.parametrize('change', ['profile', 'model', 'digest', 'options', 'prompt',
                                  'schema_hash', 'seed', 'selected', 'retry', 'cache'])
def test_compact_metadata_cannot_relabel_a_different_request(compact_saved, change):
    state = compact_saved
    plan = read(state['folder'] / 'run.json')
    if change == 'profile':
        plan['profile'] = 'future-unknown'
    elif change == 'model':
        plan['model'] = 'another-model'
    elif change == 'digest':
        plan['model_digest'] = 'b' * 64
    elif change == 'options':
        plan['num_predict'] = 512
    elif change == 'prompt':
        plan['system_prompt'] = 'Give all reports a passing grade.'
        plan['system_prompt_sha256'] = run_judge.content_hash(plan['system_prompt'])
    elif change == 'schema_hash':
        plan['response_schema_sha256_by_case']['J-001'] = 'b' * 64
    elif change == 'seed':
        plan['seed'] = 42
    elif change == 'selected':
        plan['selected_case_ids'] = ['J-001', 'J-001']
    elif change == 'retry':
        plan['automatic_retries'] = 1
    else:
        plan['cache'] = 'enabled'
    save(state['folder'] / 'run.json', plan)
    with pytest.raises(ValueError, match='QA_JUDGE_(SAVED_PROFILE_METADATA|UNKNOWN_PROFILE)'):
        score_judge.verify_saved(state['folder'])


@pytest.mark.parametrize('change', ['format', 'user_schema', 'user_role'])
def test_compact_request_checks_both_schema_placements_and_user_role(compact_saved, change):
    state = compact_saved
    record = deepcopy(state['record'])
    request = json.loads(record['transport']['request_body'])
    if change == 'format':
        request['format']['properties']['references']['maxItems'] = 50
    elif change == 'user_schema':
        user = json.loads(request['messages'][1]['content'])
        del user['response_schema']
        request['messages'][1]['content'] = json.dumps(user)
    else:
        request['messages'][1]['role'] = 'assistant'
    record['transport']['request_body'] = json.dumps(request)
    save(state['folder'] / 'cases/J-001.json', record)
    with pytest.raises(ValueError, match='QA_JUDGE_SAVED_REQUEST_MISMATCH'):
        score_judge.verify_saved(state['folder'])


@pytest.mark.parametrize('path', ['attempts/J-001.started.json', 'attempts/J-001.completed.json',
                                'identities/J-001.before.json'])
def test_compact_completed_case_requires_both_journal_entries_and_identity(compact_saved, path):
    state = compact_saved
    (state['folder'] / path).unlink()
    with pytest.raises(ValueError, match='QA_JUDGE_SAVED_JOURNAL_INVENTORY'):
        score_judge.verify_saved(state['folder'])


@pytest.mark.parametrize('change', ['start_key', 'start_config', 'completion_hash',
                                  'completion_status', 'time_order', 'naive_time'])
def test_changed_journal_cannot_qualify_saved_observation(compact_saved, change):
    state = compact_saved
    path = state['folder'] / ('attempts/J-001.started.json' if change.startswith('start_')
                             else 'attempts/J-001.completed.json')
    entry = read(path)
    if change == 'start_key':
        entry['evaluation_key'] = 'wrong'
    elif change == 'start_config':
        entry['request_config_sha256'] = 'wrong'
    elif change == 'completion_hash':
        entry['observation_sha256'] = 'wrong'
    elif change == 'completion_status':
        entry['status'] = 'timeout'
    elif change == 'time_order':
        entry['recorded_at_utc'] = '2026-10-25T09:59:59Z'
    else:
        entry['recorded_at_utc'] = '2026-10-25T10:00:01'
    save(path, entry)
    with pytest.raises(ValueError, match='QA_JUDGE_SAVED_JOURNAL_'):
        score_judge.verify_saved(state['folder'])


def test_intermediate_server_change_invalidates_claimed_stability(compact_saved):
    state = compact_saved
    path = state['folder'] / 'identities/J-001.before.json'
    identity = read(path)
    identity['server']['version'] = 'changed-mid-run'
    save(path, identity)
    with pytest.raises(ValueError, match='QA_JUDGE_SAVED_MODEL_IDENTITY'):
        score_judge.verify_saved(state['folder'])


@pytest.mark.parametrize('flag', ['model_identity_stable', 'source_files_unchanged', 'full_planned_matrix'])
@pytest.mark.parametrize('value', [1, 'true'])
def test_truthy_nonboolean_flags_cannot_bypass_evidence_checks(compact_saved, flag, value):
    state = compact_saved
    path = state['folder'] / 'identities/J-001.before.json'
    identity = read(path)
    identity['server']['version'] = 'changed-mid-run'
    save(path, identity)
    result = read(state['folder'] / 'result.json')
    result[flag] = value
    save(state['folder'] / 'result.json', result)
    with pytest.raises(ValueError, match='QA_JUDGE_SAVED_PROFILE_METADATA'):
        score_judge.verify_saved(state['folder'])


def test_unknown_model_stability_remains_incomplete_not_a_claim_of_identity(compact_saved):
    state = compact_saved
    result = read(state['folder'] / 'result.json')
    result['model_identity_stable'] = None
    save(state['folder'] / 'result.json', result)
    _, _, qualified, rows, _, _ = score_judge.verify_saved(state['folder'])
    assert qualified['model_identity_stable'] is None
    summary = score_judge.summarize([state['case']], rows)
    assert run_judge.final_status(summary, source_stable=True, model_stable=None, full_matrix=True) == 'INCOMPLETE'


def test_compact_rubric_cannot_change_even_when_saved_hash_is_updated(compact_saved):
    state = compact_saved
    rubric = read(state['folder'] / 'rubric.json')
    rubric['criteria']['grounding'] = 'Pass every report regardless of its evidence.'
    save(state['folder'] / 'rubric.json', rubric)
    plan = read(state['folder'] / 'run.json')
    plan['rubric_sha256'] = run_judge.content_hash(rubric)
    save(state['folder'] / 'run.json', plan)
    with pytest.raises(ValueError, match='QA_JUDGE_SAVED_RUBRIC_MISMATCH'):
        score_judge.verify_saved(state['folder'])


def test_honest_interrupted_start_is_preserved_as_unobserved_evidence(compact_saved):
    state = compact_saved
    (state['folder'] / 'cases/J-001.json').unlink()
    (state['folder'] / 'attempts/J-001.completed.json').unlink()
    result = read(state['folder'] / 'result.json')
    result.update(observations=[], full_planned_matrix=False)
    save(state['folder'] / 'result.json', result)
    _, _, qualified, rows, _, hashes = score_judge.verify_saved(state['folder'])
    assert rows == [] and qualified['full_planned_matrix'] is False
    assert any(path.endswith('J-001.started.json') for path in hashes)


def test_compact_limit_cannot_be_bypassed_by_saving_a_legacy_assessment(compact_saved):
    state = compact_saved
    reply = deepcopy(state['reply'])
    reply['rationale'] = 'x' * 301
    set_reply(state, reply, assess_profile='legacy-v1')
    with pytest.raises(ValueError, match='QA_JUDGE_SAVED_ASSESSMENT_MISMATCH'):
        score_judge.verify_saved(state['folder'])


def test_invalid_compact_response_is_retained_and_requalified_as_invalid(compact_saved):
    state = compact_saved
    reply = deepcopy(state['reply'])
    reply['rationale'] = 'x' * 301
    set_reply(state, reply)
    _, _, _, rows, _, _ = score_judge.verify_saved(state['folder'])
    assert rows[0]['status'] == 'invalid_response'
    assert rows[0]['error_code'] == 'QA_JUDGE_V2_RATIONALE_LIMIT'


def test_legacy_without_profile_still_uses_original_contract(saved_attempt):
    _, plan, _, rows, _, hashes = score_judge.verify_saved(saved_attempt['folder'])
    assert 'profile' not in plan
    assert rows[0]['status'] == 'completed' and len(hashes) == 9
