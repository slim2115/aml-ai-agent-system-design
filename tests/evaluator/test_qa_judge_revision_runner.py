"""Profile separation and interrupted-run accounting, with no live model calls."""
from contextlib import nullcontext
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from qa.judge import dataset, transport
from qa.tools import run_judge as runner
from tests.evaluator.test_qa_judge_runner import planned, reply


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


@pytest.fixture
def compact_run(tmp_path, monkeypatch):
    cases = [planned('CAL'), planned('HOLD', 'heldout')]
    folder = tmp_path / 'dataset'
    folder.mkdir()
    (folder / 'manifest.json').write_text('{}', encoding='utf-8')
    rubric = tmp_path / 'rubric.json'
    rubric.write_text(json.dumps({'criteria': {'grounding': 'Use exact sources.'}}), encoding='utf-8')
    monkeypatch.setattr(dataset, 'DEFAULT_FOLDER', folder)
    monkeypatch.setattr(dataset, 'load_dataset', lambda: {'cases': deepcopy(cases)})
    monkeypatch.setattr(runner, 'RUBRIC', rubric)
    monkeypatch.setattr(runner, 'environment', lambda output: None)
    monkeypatch.setattr(runner, 'source_hashes', lambda: {'frozen': 'unchanged'})
    monkeypatch.setattr(runner, 'network_guard', nullcontext)
    state = SimpleNamespace(output=tmp_path / 'run', cases=cases, calls=[], identity_calls=0,
                            profiles=[], action=None, model_version='fixed', clock=0.0,
                            metadata_seconds=0, deadlines=[])

    def identity():
        state.identity_calls += 1
        return {'model': {'digest': runner.EXPECTED_DIGEST},
                'server': {'version': state.model_version}, 'show': {'config': 'fixed'},
                'loaded': {'state': state.identity_calls}}

    def metadata(path):
        assert path == '/api/ps'
        state.clock += state.metadata_seconds
        return {'models': []}

    class FakeTransport:
        def __init__(self, *, profile):
            state.profiles.append(profile)

        def complete(self, payload, rubric, *, deadline):
            case_id = payload['case_id']
            state.calls.append(case_id)
            state.deadlines.append(deadline)
            marker = read(state.output / 'attempts' / (case_id + '.started.json'))
            assert marker['status'] == 'REQUEST_MAY_BE_IN_FLIGHT'
            assert not (state.output / 'cases' / (case_id + '.json')).exists()
            if state.action is not None:
                state.action(payload)
            return {'status': 'completed', 'response_text': json.dumps(reply(payload)), 'elapsed_seconds': 0.01}

    monkeypatch.setattr(runner, 'identity', identity)
    monkeypatch.setattr(runner, 'ollama_json', metadata)
    monkeypatch.setattr(runner.time, 'monotonic', lambda: state.clock)
    monkeypatch.setattr(transport, 'JudgeTransport', FakeTransport)
    return state


def test_v1_key_remains_exactly_compatible_and_v2_has_a_separate_identity():
    payload, rubric = planned()['payload'], {'rubric': 'unchanged'}
    legacy = runner.evaluation_key(payload, rubric, 'digest', transport.SYSTEM_PROMPT)
    expected = runner.content_hash({'payload': payload, 'rubric': rubric, 'model_digest': 'digest',
        'system_prompt': transport.SYSTEM_PROMPT, 'parameters': {'temperature': 0, 'num_predict': 512,
        'num_ctx': 32768, 'seed': 'not_explicitly_set'}, 'cache_schema': '1.0'})
    assert legacy == expected
    config = transport.request_config(payload, 'compact-v2')
    revised = runner.evaluation_key(payload, rubric, 'digest', config['system_prompt'], profile='compact-v2')
    assert revised != legacy
    assert revised == runner.content_hash({'payload': payload, 'rubric': rubric, 'model_digest': 'digest',
        'request_config': config, 'cache_schema': '2.0'})


@pytest.mark.parametrize('field', ['options', 'response_schema'])
def test_v2_key_changes_when_effective_request_configuration_changes(monkeypatch, field):
    payload, rubric = planned()['payload'], {'rubric': 'unchanged'}
    config = transport.request_config(payload, 'compact-v2')
    original = runner.evaluation_key(payload, rubric, 'digest', config['system_prompt'], profile='compact-v2')
    changed = deepcopy(config)
    if field == 'options':
        changed[field]['num_predict'] += 1
    else:
        changed[field]['properties']['references']['maxItems'] += 1
    monkeypatch.setattr(transport, 'request_config', lambda *_args, **_kwargs: changed)
    assert runner.evaluation_key(payload, rubric, 'digest', config['system_prompt'], profile='compact-v2') != original


def test_v2_key_rejects_a_prompt_different_from_the_effective_request():
    with pytest.raises(ValueError, match='QA_JUDGE_PROFILE_PROMPT_MISMATCH'):
        runner.evaluation_key(planned()['payload'], {}, 'digest', transport.SYSTEM_PROMPT, profile='compact-v2')


@pytest.mark.parametrize('operation', ['run', 'assess', 'key'])
def test_unknown_profile_is_rejected_before_creating_evidence(tmp_path, operation):
    with pytest.raises(ValueError, match='QA_JUDGE_UNKNOWN_PROFILE'):
        if operation == 'run':
            runner.run(tmp_path / 'must-not-exist', profile='unknown')
        elif operation == 'assess':
            runner.assess_record(planned(), {}, 'key', profile='unknown')
        else:
            runner.evaluation_key({}, {}, 'digest', 'system', profile='unknown')
    assert not (tmp_path / 'must-not-exist').exists()


def test_v2_runner_enforces_compact_limits_without_relaxing_legacy_contract():
    item = planned()
    value = reply(item['payload'])
    value['rationale'] = 'x' * 301
    raw = {'status': 'completed', 'response_text': json.dumps(value)}
    assert runner.assess_record(item, raw, 'old')['status'] == 'completed'
    compact = runner.assess_record(item, raw, 'new', profile='compact-v2')
    assert compact['status'] == 'invalid_response'
    assert compact['error_code'] == 'QA_JUDGE_V2_RATIONALE_LIMIT'
    assert compact['response'] is None
    assert compact['operator_decision_written'] is False


def test_new_evidence_namespace_is_allowed_but_existing_evidence_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    relative = 'verification/qa-judge-v2-test/first'
    assert runner.output_path(relative) == tmp_path / relative
    existing = tmp_path / relative
    existing.mkdir(parents=True)
    (existing / 'keep.txt').write_text('prior observation', encoding='utf-8')
    with pytest.raises(FileExistsError, match='QA_EVIDENCE_ALREADY_EXISTS'):
        runner.output_path(relative)
    assert (existing / 'keep.txt').read_text(encoding='utf-8') == 'prior observation'
    with pytest.raises(ValueError, match='QA_JUDGE_NEW_OUTPUT_REQUIRED'):
        runner.output_path('verification/unrelated/first')


def test_compact_prepare_records_effective_profile_without_inference(compact_run):
    state = compact_run
    assert runner.run(state.output, prepare_only=True, profile='compact-v2') == 3
    assert state.calls == state.profiles == [] and state.identity_calls == 0
    meta = read(state.output / 'run.json')
    assert meta['profile'] == 'compact-v2' and meta['num_predict'] == 1024
    assert meta['num_ctx'] == 32768 and meta['temperature'] == 0
    assert meta['seed'] == 'not_explicitly_set'
    assert meta['automatic_retries'] == 0 and meta['cache'] == 'disabled'
    for case in state.cases:
        config = transport.request_config(case['payload'], 'compact-v2')
        assert meta['system_prompt'] == config['system_prompt']
        assert meta['response_schema_sha256_by_case'][case['id']] == runner.content_hash(config['response_schema'])
    result = read(state.output / 'result.json')
    assert result['profile'] == 'compact-v2'
    assert result['status'] == result['product_acceptance'] == 'INCOMPLETE'
    assert result['observed_case_count'] == result['generator_calls'] == 0
    assert all(row['verdict'] == '' for row in read(state.output / 'human-review-template.json')['ratings'])


def test_full_compact_run_journals_each_attempt_and_checks_identity_before_each_call(compact_run):
    state = compact_run
    assert runner.run(state.output, profile='compact-v2') == 3
    assert state.profiles == ['compact-v2']
    assert state.calls == ['CAL', 'HOLD'] and state.identity_calls == 4
    for case_id in state.calls:
        before = read(state.output / 'identities' / (case_id + '.before.json'))
        assert before['server']['version'] == 'fixed'
        started = read(state.output / 'attempts' / (case_id + '.started.json'))
        completed = read(state.output / 'attempts' / (case_id + '.completed.json'))
        observed = state.output / 'cases' / (case_id + '.json')
        assert started['profile'] == 'compact-v2' and started['automatic_retries'] == 0
        assert completed['observation_sha256'] == sha256(observed.read_bytes()).hexdigest()
    result = read(state.output / 'result.json')
    assert result['full_planned_matrix'] and result['model_identity_stable']
    assert result['summary']['known_challenge_status'] == result['summary']['execution_status'] == 'PASS'
    assert result['summary']['human_calibration']['status'] == result['status'] == 'INCOMPLETE'
    assert result['suitability'] == 'NOT_ESTABLISHED'


def test_changed_identity_stops_before_a_second_inference_and_cannot_be_repaired_by_final_snapshot(compact_run, monkeypatch):
    state = compact_run

    def transient_identity_change():
        state.identity_calls += 1
        return {'model': {'digest': runner.EXPECTED_DIGEST},
                'server': {'version': 'changed' if state.identity_calls == 3 else 'fixed'},
                'show': {'config': 'fixed'}}

    monkeypatch.setattr(runner, 'identity', transient_identity_change)
    assert runner.run(state.output, profile='compact-v2') == 1
    assert state.calls == ['CAL']
    assert not (state.output / 'attempts/HOLD.started.json').exists()
    result = read(state.output / 'result.json')
    assert read(state.output / 'ollama-after.json')['server']['version'] == 'fixed'
    assert result['error_code'] == 'QA_JUDGE_MODEL_IDENTITY_CHANGED'
    assert result['status'] == 'FAIL' and result['model_identity_stable'] is False
    assert result['observed_case_count'] == 1 and result['planned_case_count'] == 2


def test_interrupted_request_retains_start_marker_without_inventing_a_response_or_retry(compact_run):
    state = compact_run

    def interrupted(_payload):
        raise RuntimeError('TEST_COLLECTION_INTERRUPTED')

    state.action = interrupted
    assert runner.run(state.output, profile='compact-v2') == 3
    assert state.calls == ['CAL']
    assert (state.output / 'attempts/CAL.started.json').exists()
    assert not (state.output / 'attempts/CAL.completed.json').exists()
    assert not list((state.output / 'cases').glob('*.json'))
    result = read(state.output / 'result.json')
    assert result['error_code'] == 'TEST_COLLECTION_INTERRUPTED'
    assert result['observed_case_count'] == 0 and result['summary']['overall']['missing'] == 2
    assert result['status'] == 'INCOMPLETE'


def test_identity_loaded_state_does_not_change_model_identity():
    initial = {'model': {'digest': 'same'}, 'server': {'version': 'same'}, 'show': {'parameters': 'same'},
               'loaded': {'models': []}}
    warm = deepcopy(initial)
    warm['loaded'] = {'models': [{'digest': 'same'}]}
    assert runner.same_identity(initial, warm)
    for section, key in [('model', 'digest'), ('server', 'version'), ('show', 'parameters')]:
        changed = deepcopy(warm)
        changed[section][key] = 'changed'
        assert not runner.same_identity(initial, changed)


def test_metadata_delay_cannot_extend_absolute_inference_batch_deadline(compact_run, monkeypatch):
    state = compact_run
    monkeypatch.setattr(runner, 'BATCH_SECONDS', 10)
    state.metadata_seconds = 12
    assert runner.run(state.output, profile='compact-v2') == 3
    # The mocked transport accepts it, but the real transport checks remaining()
    # before constructing a connection and rejects this expired absolute deadline.
    assert state.calls == ['CAL'] and state.deadlines == [10]
    result = read(state.output / 'result.json')
    assert result['stop_reason'] == 'BATCH_DEADLINE_EXCEEDED'
    assert result['full_planned_matrix'] is False
