"""Saved synthetic evidence is rechecked offline, with its original denominator.

Only the three published pilot records are inputs. Repeated copies below are
fixture slots, never new inference or independent evidence about a real model.
"""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

import pytest

from qa.scenario_checks import check_observation, run_scenario
from qa.scenarios import load_catalog
from qa.stability import history
from qa.stability.statistics import summarize
from src.contracts.v1 import ErrorResult
from src.runtime.transport import TransportFailure


ROOT = Path(__file__).resolve().parents[2]
PUBLISHED = ROOT / 'verification/qa-step-07-2026-10-20/pilot-first/cases'
MODEL = 'qwen2.5:14b'
DIGEST = '7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6'


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')


def rewrite(path, change):
    value = read(path)
    change(value)
    write(path, value)


def hashes(folder):
    return {path.relative_to(folder).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in folder.rglob('*') if path.is_file()}


@pytest.fixture
def make_history(tmp_path):
    """Create a portable saved run; frozen catalog and real checkers stay intact."""
    root = tmp_path / 'project'
    dataset = root / 'qa/datasets/aml-v1'
    shutil.copytree(ROOT / 'qa/datasets/aml-v1', dataset)

    def make(*, repeats=1, drafts=('QA-S01',), controls=False):
        source = root / 'var/qa/llm/unit-history'
        selected = [*drafts, *(history.CONTROL_IDS if controls else ())]
        write(source / 'run.json', {'status': 'INCOMPLETE', 'model': MODEL,
              'model_digest': DIGEST, 'selected_case_ids': selected, 'repeats': repeats,
              'partial': True, 'temperature': 0, 'seed': 'not_explicitly_set'})
        source_hashes = {'qa/datasets/aml-v1/manifest.json':
                         sha256((dataset / 'manifest.json').read_bytes()).hexdigest()}
        identity = {'model': {'name': MODEL, 'digest': DIGEST},
                    'server': {'version': 'synthetic-test'}, 'show': {}}
        for name in ('source-hashes.json', 'source-hashes-after.json'):
            write(source / name, source_hashes)
        for name in ('ollama-before.json', 'ollama-after.json'):
            write(source / name, identity)
        write(source / 'result.json', {'status': 'PASS', 'elapsed_seconds': 1000})
        write(source / 'runtime.json', {'fixture': 'synthetic; not a live benchmark'})
        for case_id in drafts:
            original = read(PUBLISHED / case_id / 'repeat-01.json')
            for repeat in range(1, repeats + 1):
                record = deepcopy(original)
                record['evaluation']['repeat_index'] = repeat
                write(source / f'cases/{case_id}/repeat-{repeat:02d}.json', record)
        if controls:
            scenarios = {s.case.id: s for s in load_catalog(dataset).cases}
            for case_id in history.CONTROL_IDS:
                scenario = scenarios[case_id]
                observed = run_scenario(scenario, ROOT / 'qa/datasets/aml-v1' / scenario.fixture)
                issues = check_observation(scenario, observed)
                assert not issues and not observed.model_prompts
                write(source / f'cases/{case_id}/repeat-01.json', {
                    'evaluation': {'case_id': case_id, 'repeat_index': 1, 'status': 'PASS',
                                   'issues': [], 'model_calls': 0},
                    'observation': asdict(observed), 'http_attempts': [], 'rankings': [],
                    'loaded_before': [], 'model_load_state': 'warm',
                    'transport_outcome': 'completed', 'template_issues': [],
                    'template_report': None})
        return root, source

    return make


def test_replay_fifteen_slots_three_families_eight_controls_is_read_only(make_history):
    root, source = make_history(repeats=5, drafts=('QA-S01', 'QA-S03', 'QA-S04'), controls=True)
    before = hashes(root)
    loaded = history.load_history(source, root)
    assert hashes(root) == before
    assert set(loaded) == {'plan', 'observations', 'source_files', 'metadata'}
    assert len(loaded['observations']) == 15
    assert len({case['family_id'] for case in loaded['plan']['cases']}) == 3
    assert loaded['metadata']['status'] == 'PASS'
    assert loaded['metadata']['controls']['planned'] == 8
    assert loaded['metadata']['controls']['observed'] == 8
    assert loaded['metadata']['controls']['status'] == 'PASS'
    assert loaded['metadata']['new_model_calls'] == 0
    assert loaded['metadata']['population_inference'] == 'NOT_ESTABLISHED'
    assert loaded['metadata']['budget_enforcement'] == 'NOT_EVALUATED'
    assert loaded['metadata']['predeclared_total_budget'] == 'ABSENT'
    assert len(loaded['source_files']) == 31
    summary = summarize(loaded['plan'], loaded['observations'])
    assert summary['arms']['candidate']['pass_rate']['denominator'] == 15
    assert summary['arms']['candidate']['family_pass_rate']['denominator'] == 3


def test_missing_repeat_retains_predeclared_denominator(make_history):
    root, source = make_history(repeats=2)
    (source / 'cases/QA-S01/repeat-02.json').unlink()
    loaded = history.load_history(source, root)
    assert loaded['metadata']['status'] == 'INCOMPLETE'
    assert loaded['metadata']['collection_status'] == 'INCOMPLETE'
    assert loaded['plan']['cases'][0]['repeats'] == 2
    assert len(loaded['observations']) == 1
    result = summarize(loaded['plan'], loaded['observations'])
    assert result['status'] == 'INCOMPLETE'
    assert result['arms']['candidate']['pass_rate'] == {'numerator': 1, 'denominator': 2, 'value': .5}


@pytest.mark.parametrize('code,pre_http', [
    ('DEADLINE_EXCEEDED', True), ('NOT_READY', True), ('DEPENDENCY_UNAVAILABLE', True),
    ('DEADLINE_EXCEEDED', False), ('DEPENDENCY_UNAVAILABLE', False),
])
def test_real_collector_failure_codes_survive_replay_without_invented_usage(make_history, code, pre_http):
    from qa.tools.run_llm import RecordingHTTPConnection, RecordingTransport

    root, source = make_history()
    path = source / 'cases/QA-S01/repeat-01.json'
    record = read(path)
    settings = SimpleNamespace(llm=SimpleNamespace(base_url='http://127.0.0.1:11434',
        model_name='unsupported' if code == 'NOT_READY' else MODEL))
    collector = RecordingTransport(settings)

    class UnavailableConnection:
        sock = None

        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            if code == 'DEADLINE_EXCEEDED':
                raise TimeoutError('synthetic timeout; no socket opened')
            raise ConnectionError('synthetic refusal; no socket opened')

        def close(self):
            pass

    def unavailable_factory(host, port, **kwargs):
        if pre_http:
            raise ConnectionError('synthetic connection construction failure')
        return RecordingHTTPConnection(host, port, records=collector.attempts,
                                       connection_type=UnavailableConnection, **kwargs)

    collector.transport.connection_factory = unavailable_factory
    deadline = -1 if pre_http and code == 'DEADLINE_EXCEEDED' else time.monotonic() + 30
    with pytest.raises(TransportFailure) as failure:
        collector.complete(record['observation']['model_prompts'][0], {}, deadline=deadline)
    assert failure.value.code == collector.outcome == code
    assert len(collector.prompts) == 1
    assert len(collector.attempts) == (0 if pre_http else 1)

    record['observation']['result'] = ErrorResult(schema_version='1.0', status='error',
        generated_at='2026-11-18T00:00:00Z', correlation_id='qa-history-failure', code=code,
        message='Synthetic collector failure', retryable=code != 'DEADLINE_EXCEEDED').model_dump(mode='json')
    record['observation']['model_prompts'] = collector.prompts
    record['http_attempts'] = collector.attempts
    record['transport_outcome'] = collector.outcome
    scenario = next(case for case in load_catalog(root / 'qa/datasets/aml-v1').cases if case.case.id == 'QA-S01')
    scenario = scenario.model_copy(update={'engine': 'local-model', 'expected_model_calls': 1})
    record['evaluation'] = history.check_repeat(scenario, history.Observation(**record['observation']),
                                                 repeat_index=1, transport_outcome='completed')
    write(path, record)
    rewrite(source / 'result.json', lambda value: value.update(status='FAIL'))
    loaded = history.load_history(source, root)
    row = loaded['observations'][0]
    assert row['status'] == loaded['metadata']['status'] == 'FAIL'
    assert row['outcome'] == ('timeout' if code == 'DEADLINE_EXCEEDED' else 'dependency_failure')
    assert loaded['metadata']['http_telemetry']['unrecorded_prompt_attempts'] == int(pre_http)
    if pre_http:
        assert len(row['attempts']) == 1
        assert row['attempts'][0]['elapsed_seconds'] is None
        assert loaded['metadata']['http_telemetry']['status'] == 'INCOMPLETE'
    assert row['attempts'][0]['status'] == ('timeout' if code == 'DEADLINE_EXCEEDED' else 'error')
    assert row['attempts'][0]['prompt_tokens'] is None
    assert row['attempts'][0]['output_tokens'] is None
    arm = summarize(loaded['plan'], loaded['observations'])['arms']['candidate']
    assert arm['pass_rate']['denominator'] == 1
    for key in ('prompt_tokens', 'output_tokens'):
        assert arm['usage'][key]['recorded_total'] is None
        assert arm['usage'][key]['unknown_attempts'] == 1


@pytest.mark.parametrize('kind', ['status', 'semantic_fingerprint', 'ordered_fingerprint', 'fact'])
def test_saved_pass_and_fingerprints_are_not_trusted(make_history, kind):
    root, source = make_history()
    path = source / 'cases/QA-S01/repeat-01.json'
    def corrupt(value):
        if kind == 'status':
            value['evaluation']['status'] = 'FAIL'
        elif kind == 'fact':
            value['observation']['result']['found_facts'][0]['fact'] = 'Invented source fact'
        else:
            value['evaluation']['draft'][kind] = '0' * 64
    rewrite(path, corrupt)
    with pytest.raises(history.HistoryError, match='^HISTORY_EVALUATION_MISMATCH$'):
        history.load_history(source, root)


def test_completed_record_cannot_omit_http_attempt(make_history):
    root, source = make_history()
    rewrite(source / 'cases/QA-S01/repeat-01.json', lambda row: row.update(http_attempts=[]))
    with pytest.raises(history.HistoryError, match='^HISTORY_ATTEMPT_COUNT$'):
        history.load_history(source, root)


def test_transport_failure_cannot_claim_no_recorded_prompt(make_history):
    root, source = make_history()
    def corrupt(row):
        row['transport_outcome'] = 'DEADLINE_EXCEEDED'
        row['http_attempts'] = []
        row['observation']['model_prompts'] = []
        row['observation']['result'] = {'status': 'error', 'code': 'DEADLINE_EXCEEDED'}
    rewrite(source / 'cases/QA-S01/repeat-01.json', corrupt)
    with pytest.raises(history.HistoryError, match='^HISTORY_RECORD_STATE$'):
        history.load_history(source, root)


@pytest.mark.parametrize('kind,code', [
    ('response_hash', 'HISTORY_ATTEMPT_RESPONSE_HASH'),
    ('response_json', 'HISTORY_ATTEMPT_RESPONSE_JSON'),
    ('prompt', 'HISTORY_ATTEMPT_PROMPT'),
    ('endpoint', 'HISTORY_ATTEMPT_ENDPOINT'),
])
def test_http_transcript_must_match_prompt_and_response_bytes(make_history, kind, code):
    root, source = make_history()
    def corrupt(value):
        row = value['http_attempts'][0]
        if kind == 'response_hash':
            row['response_body_sha256'] = '0' * 64
        elif kind == 'response_json':
            row['response_json']['eval_count'] += 1
        elif kind == 'endpoint':
            row['endpoint'] = 'https://example.invalid'
        else:
            request = json.loads(row['request_body'])
            request['messages'][1]['content'] = '{}'
            row['request_body'] = json.dumps(request)
    rewrite(source / 'cases/QA-S01/repeat-01.json', corrupt)
    with pytest.raises(history.HistoryError, match=f'^{code}$'):
        history.load_history(source, root)


@pytest.mark.parametrize('name', ['source-hashes.json', 'source-hashes-after.json',
                                  'ollama-before.json', 'ollama-after.json', 'result.json', 'runtime.json'])
def test_partial_headers_are_incomplete_not_pass(make_history, name):
    root, source = make_history()
    (source / name).unlink()
    loaded = history.load_history(source, root)
    assert loaded['metadata']['status'] == 'INCOMPLETE'
    assert name in loaded['metadata']['missing_files']
    assert len(loaded['observations']) == 1


@pytest.mark.parametrize('name,change,field', [
    ('source-hashes-after.json', lambda value: value.update({'changed.py': '0' * 64}),
     'historical_source_stability'),
    ('ollama-after.json', lambda value: value['server'].update(version='changed'),
     'model_identity_stability'),
    ('ollama-after.json', lambda value: value['show'].update(parameters='changed'),
     'model_identity_stability'),
])
def test_source_or_runtime_identity_drift_cannot_pass(make_history, name, change, field):
    root, source = make_history()
    rewrite(source / name, change)
    metadata = history.load_history(source, root)['metadata']
    assert metadata[field] == 'FAIL'
    assert metadata['status'] == 'FAIL'


@pytest.mark.parametrize('field,value', [('digest', '0' * 64), ('name', 'different-model')])
def test_model_identity_must_match_declared_run(make_history, field, value):
    root, source = make_history()
    rewrite(source / 'ollama-after.json', lambda row: row['model'].update({field: value}))
    with pytest.raises(history.HistoryError, match='^HISTORY_MODEL_IDENTITY$'):
        history.load_history(source, root)


def test_dataset_manifest_must_match_historical_pin(make_history):
    root, source = make_history()
    for name in ('source-hashes.json', 'source-hashes-after.json'):
        rewrite(source / name, lambda row: row.update({'qa/datasets/aml-v1/manifest.json': '0' * 64}))
    with pytest.raises(history.HistoryError, match='^HISTORY_FROZEN_DATASET_MISMATCH$'):
        history.load_history(source, root)


@pytest.mark.parametrize('relative,code', [
    ('cases/QA-S03/repeat-01.json', 'HISTORY_UNPLANNED_CASE'),
    ('cases/QA-S01/repeat-02.json', 'HISTORY_UNPLANNED_REPEAT'),
    ('cases/QA-S01/extra.json', 'HISTORY_UNPLANNED_REPEAT'),
])
def test_files_outside_declared_case_repeat_plan_are_rejected(make_history, relative, code):
    root, source = make_history()
    write(source / relative, {})
    with pytest.raises(history.HistoryError, match=f'^{code}$'):
        history.load_history(source, root)


@pytest.mark.parametrize('relative', ['var/qa/runs/copied', 'verification/qa-step-08-2026-10-21/copied',
                                     'var/qa/llm/nested/copied'])
def test_source_must_be_in_owned_historical_namespace(make_history, relative):
    root, source = make_history()
    destination = root / relative
    shutil.copytree(source, destination)
    with pytest.raises(history.HistoryError, match='^HISTORY_SOURCE_SCOPE$'):
        history.load_history(destination, root)


def test_source_outside_root_is_rejected_without_disclosing_path(make_history, tmp_path):
    root, _source = make_history()
    outside = tmp_path / 'private-location'
    outside.mkdir()
    with pytest.raises(history.HistoryError, match='^HISTORY_INVALID_EVIDENCE$'):
        history.load_history(outside, root)


def test_hardlinked_input_is_rejected(make_history):
    root, source = make_history()
    os.link(source / 'run.json', root / 'run-alias.json')
    with pytest.raises(history.HistoryError, match='^HISTORY_INVALID_EVIDENCE$'):
        history.load_history(source, root)


def test_linked_parent_is_rejected_before_reading_evidence(make_history, monkeypatch):
    root, source = make_history()
    original = Path.is_symlink
    # Exercise the link guard on all platforms, without requiring Windows' symlink privilege.
    monkeypatch.setattr(Path, 'is_symlink', lambda path: path == source.parent or original(path))
    with pytest.raises(history.HistoryError, match='^HISTORY_INVALID_EVIDENCE$'):
        history.load_history(source, root)


def test_input_changed_between_pin_and_read_is_rejected(make_history, monkeypatch):
    root, source = make_history()
    original = history.pin_file
    def pin_then_mutate(path, root):
        pin = original(path, root)
        if path == source / 'run.json':
            rewrite(path, lambda value: value.update(seed='changed-after-pin'))
        return pin
    monkeypatch.setattr(history, 'pin_file', pin_then_mutate)
    with pytest.raises(history.HistoryError, match='^HISTORY_FILE_CHANGED$'):
        history.load_history(source, root)


def test_input_changed_after_read_is_detected_by_final_verification(make_history, monkeypatch):
    root, source = make_history()
    original = history.check_repeat
    def check_then_mutate(*args, **kwargs):
        value = original(*args, **kwargs)
        write(source / 'runtime.json', {'changed_after_read': True})
        return value
    monkeypatch.setattr(history, 'check_repeat', check_then_mutate)
    with pytest.raises(history.HistoryError, match='^HISTORY_FILE_CHANGED$'):
        history.load_history(source, root)
