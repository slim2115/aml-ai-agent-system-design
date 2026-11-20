"""Owned worker evidence, fixed control sensitivity and fail-closed supervision."""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path

import pytest

from qa.autonomy import demo, runner, worker
from qa.autonomy.candidates import prepare_cases
from qa.scenario_checks import canonical


@pytest.fixture(scope='module')
def demo_bundle():
    events = []
    result = demo.run_demo(root=runner.ROOT, emit=lambda event: events.append(deepcopy(event)))
    return result, events


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')


def journal(path, events):
    path.write_text(''.join(json.dumps({'journal_sequence': index,
        'recorded_at_utc': '2026-11-19T00:00:00+00:00', 'data': event}) + '\n'
        for index, event in enumerate(events, 1)), encoding='utf-8')


def child_files(directory, bundle):
    data, events = deepcopy(bundle)
    dump(directory / 'demo.json', data)
    journal(directory / 'journal.jsonl', events)
    dump(directory / 'worker-result.json', {'status': data['status'], 'mode': 'demo',
        'product_acceptance': 'INCOMPLETE', 'candidate_admission': 'NOT_EVALUATED',
        'new_model_calls': 0, 'qualification_status': data['status'], 'control_count': 11,
        'elapsed_seconds': 1.0})
    (directory / 'worker.log').write_text('reviewed worker output', encoding='utf-8')


@pytest.fixture
def project(tmp_path, monkeypatch, demo_bundle):
    monkeypatch.setattr(runner, 'snapshot', lambda root: {'digest': 'a' * 64})
    monkeypatch.setattr(runner, 'compare_snapshots', lambda a, b: {'status': 'PASS' if a == b else 'ERROR'})
    monkeypatch.setattr(runner, 'environment', lambda: {'digest': 'b' * 64})
    output = 'var/qa/autonomy/unit'

    def process(argv, **kwargs):
        child_files(tmp_path / output, demo_bundle)
        return {'returncode': 0, 'timed_out': False, 'interrupted': False,
                'cleanup_status': 'PASS', 'elapsed_seconds': 1.0}

    return tmp_path, output, process


def saved(project):
    root, output, _process = project
    return json.loads((root / output / 'result.json').read_text('utf-8'))


def test_actual_demo_has_independent_expected_failures_and_no_hidden_success(demo_bundle):
    value, events = demo_bundle
    assert value['status'] == 'PASS' and value['new_model_calls'] == 0
    rows = {row['id']: row for row in value['controls']}
    assert len(rows) == value['control_count'] == 11
    assert set(rows) == {'valid', 'repair', 'no_progress', 'attempt_limit', 'call_limit',
        'token_limit', 'timeout', 'unknown_usage', 'reservation_breach', 'deadline', 'oracle_error'}
    repair = rows['repair']['cycle']
    assert repair['status'] == 'FAIL' and repair['cases'][0]['first_attempt_status'] == 'FAIL'
    assert repair['cases'][0]['final_candidate_status'] == 'PASS'
    assert len(repair['observed_failed_attempts']) == 1
    assert rows['no_progress']['cycle']['cases'][0]['termination_reason'] == 'NO_PROGRESS'
    assert rows['call_limit']['cycle']['cases'][1]['first_attempt_status'] == 'NOT_RUN'
    assert rows['unknown_usage']['cycle']['ledger']['total_tokens'] is None
    assert rows['oracle_error']['cycle']['status'] == 'ERROR'
    assert events and all(event['control_id'] in rows for event in events)


def test_real_demo_never_calls_network_or_starts_a_process(monkeypatch):
    import socket
    import subprocess
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network requested'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('process requested'))
    assert demo.run_demo(root=runner.ROOT, emit=lambda event: None)['status'] == 'PASS'


def test_parent_uses_owned_fixed_worker_and_pins_real_controls(project, monkeypatch):
    root, output, process = project
    monkeypatch.setenv('AUTONOMY_PRIVATE_TOKEN', 'do-not-inherit')
    captured = {}

    def checked_process(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return process(argv, **kwargs)

    assert runner.run(output, root=root, process_runner=checked_process) == 0
    value = saved(project)
    assert value['execution_status'] == value['version_binding'] == value['status'] == 'PASS'
    assert value['product_acceptance'] == 'INCOMPLETE'
    assert value['candidate_admission'] == 'NOT_EVALUATED'
    assert captured['argv'][1:4] == ['-I', '-B', 'qa/tools/_autonomy_worker.py']
    assert captured['argv'][-2:] == ['--run-dir', output]
    assert 'AUTONOMY_PRIVATE_TOKEN' not in captured['environment']
    assert captured['timeout_seconds'] > 0 and captured['log_path'] == root / output / 'worker.log'
    evidence = json.loads((root / output / 'evidence.json').read_text('utf-8'))
    assert runner.verify_files(evidence['files'], root)['status'] == 'PASS'
    assert {'demo.json', 'journal.jsonl', 'worker-result.json'} <= {Path(row['path']).name for row in evidence['files']}


def test_existing_result_and_user_database_are_preserved(project):
    root, output, process = project
    database = root / 'operator.sqlite'
    database.write_bytes(b'ordinary local data')
    assert runner.run(output, root=root, process_runner=process) == 0
    result_bytes = (root / output / 'result.json').read_bytes()
    with pytest.raises(ValueError):
        runner.run(output, root=root, process_runner=process)
    assert (root / output / 'result.json').read_bytes() == result_bytes
    assert database.read_bytes() == b'ordinary local data'


@pytest.mark.parametrize('path', ['var/qa/autonomy', 'var/qa/autonomy/a/b', 'var/qa/llm/foreign', '../outside'])
def test_output_is_a_new_immediate_owned_child(tmp_path, path):
    with pytest.raises(ValueError):
        runner.output_path(path, tmp_path)


@pytest.mark.parametrize('kind', ['source', 'environment'])
def test_identity_drift_prevents_version_pass(project, monkeypatch, kind):
    calls = 0

    def changed(*args):
        nonlocal calls
        calls += 1
        return {'digest': str(calls) * 64}

    monkeypatch.setattr(runner, 'snapshot' if kind == 'source' else 'environment', changed)
    root, output, process = project
    assert runner.run(output, root=root, process_runner=process) == 2
    assert saved(project)['version_binding'] == 'ERROR'


@pytest.mark.parametrize('change,code,status', [
    ({'timed_out': True, 'returncode': None}, 3, 'INCOMPLETE'),
    ({'interrupted': True, 'returncode': None}, 3, 'INCOMPLETE'),
    ({'cleanup_status': 'ERROR'}, 2, 'ERROR'),
    ({'returncode': 1}, 2, 'ERROR'),
    ({'returncode': False}, 2, 'ERROR'),
])
def test_process_failure_never_uses_saved_success(project, change, code, status):
    root, output, process = project

    def failed(argv, **kwargs):
        return {**process(argv, **kwargs), **change}

    assert runner.run(output, root=root, process_runner=failed) == code
    assert saved(project)['execution_status'] == status


@pytest.mark.parametrize('artifact', ['cycle', 'journal'])
def test_late_timeout_preserves_already_observed_live_failure(project, demo_bundle, artifact):
    root, output, _process = project
    repair = deepcopy(next(row['cycle'] for row in demo_bundle[0]['controls'] if row['id'] == 'repair'))

    def interrupted(argv, **kwargs):
        directory = root / output
        (directory / 'worker.log').write_text('final identity not completed', encoding='utf-8')
        if artifact == 'cycle':
            dump(directory / 'cycle.json', repair)
        else:
            journal(directory / 'journal.jsonl', repair['events'])
            with (directory / 'journal.jsonl').open('a', encoding='utf-8') as stream:
                stream.write('{"journal_sequence":')
        return {'returncode': None, 'timed_out': True, 'interrupted': False,
                'cleanup_status': 'PASS', 'elapsed_seconds': 182.0}

    assert runner.run(output, root=root, mode='ollama', case_ids=['QA-S01'], process_runner=interrupted) == 1
    value = saved(project)
    assert value['execution_status'] == 'INCOMPLETE'
    assert value['observed_failure'] == value['status'] == 'FAIL'
    assert value['version_binding'] == 'PASS'


def test_missing_worker_result_is_error(project):
    root, output, _process = project
    assert runner.run(output, root=root, process_runner=lambda *a, **k: {
        'returncode': 0, 'timed_out': False, 'interrupted': False, 'cleanup_status': 'PASS'}) == 2
    assert saved(project)['execution_status'] == 'ERROR'


def test_unexpected_process_exception_does_not_disclose_content(project):
    root, output, _process = project

    def fail(*args, **kwargs):
        raise RuntimeError('secret local value')

    assert runner.run(output, root=root, process_runner=fail) == 2
    assert 'secret local value' not in json.dumps(saved(project))


@pytest.mark.parametrize('filename', ['demo.json', 'worker-result.json', 'journal.jsonl'])
def test_evidence_change_after_first_pin_cannot_pass(project, monkeypatch, filename):
    original = runner.pin_file
    modified = False

    def pin_then_change(path, root):
        nonlocal modified
        row = original(path, root)
        if path.name == filename and not modified:
            modified = True
            path.write_text('{}', encoding='utf-8')
        return row

    monkeypatch.setattr(runner, 'pin_file', pin_then_change)
    root, output, process = project
    assert runner.run(output, root=root, process_runner=process) == 2
    assert saved(project)['evidence_integrity']['status'] == 'ERROR'


@pytest.mark.parametrize('tamper', ['duplicate_control', 'forged_observation', 'forged_expectation', 'missing_journal_event'])
def test_claimed_demo_success_requires_complete_real_control_evidence(project, tamper):
    root, output, process = project

    def corrupted(argv, **kwargs):
        status = process(argv, **kwargs)
        directory = root / output
        if tamper == 'missing_journal_event':
            path = directory / 'journal.jsonl'
            lines = path.read_text('utf-8').splitlines()
            path.write_text('\n'.join(lines[1:]) + '\n', encoding='utf-8')
        else:
            data = json.loads((directory / 'demo.json').read_text('utf-8'))
            if tamper == 'duplicate_control':
                data['controls'][-1] = deepcopy(data['controls'][0])
            elif tamper == 'forged_expectation':
                data['controls'][0]['expected'][1] = 999
            else:
                data['controls'][0]['observed'][1] = 999
            dump(directory / 'demo.json', data)
        return status

    assert runner.run(output, root=root, process_runner=corrupted) == 2
    assert saved(project)['execution_status'] == 'ERROR'


def valid_config():
    return {'schema_version': '1.0', 'mode': 'ollama', 'case_ids': ['QA-S01'],
            'limits': {'max_attempts': 2, 'max_tokens': 32768, 'wall_seconds': 180.0},
            'source_digest': 'a' * 64}


@pytest.mark.parametrize('change', [
    {'mode': 'shell'}, {'case_ids': []}, {'case_ids': ['QA-S01', 'QA-S01']},
    {'case_ids': ['QA-S13']}, {'source_digest': '../private'}, {'command': 'unsafe'},
    {'limits': {'max_attempts': True, 'max_tokens': 32768, 'wall_seconds': 180}},
    {'limits': {'max_attempts': 16, 'max_tokens': 32768, 'wall_seconds': 180}},
])
def test_worker_rejects_unbounded_or_extra_configuration(change):
    with pytest.raises(ValueError):
        worker.validate_config({**valid_config(), **change})


@pytest.mark.parametrize('change', [
    {'case_ids': ['QA-S01']},
    {'limits': {'max_attempts': 5, 'max_tokens': 98304, 'wall_seconds': 180.0}},
    {'limits': {'max_attempts': 6, 'max_tokens': 50000, 'wall_seconds': 180.0}},
])
def test_demo_options_cannot_be_silently_ignored(change):
    config = {**valid_config(), 'mode': 'demo', 'case_ids': list(runner.CASE_IDS),
              'limits': {'max_attempts': 6, 'max_tokens': 98304, 'wall_seconds': 180.0}}
    with pytest.raises(ValueError, match='AUTONOMY_DEMO_FIXED_CONTROLS'):
        worker.validate_config({**config, **change})


def test_worker_source_change_stops_before_provider_setup(tmp_path, monkeypatch):
    directory = tmp_path / 'var/qa/autonomy/changed'
    directory.mkdir(parents=True)
    dump(directory / 'worker-config.json', valid_config())
    monkeypatch.setattr(worker, 'snapshot', lambda root: {'digest': 'c' * 64})
    monkeypatch.setattr(worker, 'OllamaProposer', lambda *a: pytest.fail('provider must not start'))
    assert worker.run(directory, tmp_path) == 2
    result = json.loads((directory / 'worker-result.json').read_text('utf-8'))
    assert result['error_code'] == 'AUTONOMY_SOURCE_CHANGED'
    assert result['new_model_calls'] == 0


@pytest.mark.parametrize('drift', [False, True])
def test_worker_live_path_uses_real_cycle_and_oracle_with_fake_http_provider(tmp_path, monkeypatch, drift):
    directory = tmp_path / 'var/qa/autonomy/worker'
    directory.mkdir(parents=True)
    dump(directory / 'worker-config.json', valid_config())
    monkeypatch.setattr(worker, 'snapshot', lambda root: {'digest': 'a' * 64})
    monkeypatch.setattr(worker, 'network_guard', nullcontext)
    monkeypatch.setattr(worker, 'prepare_cases', lambda ids, root: prepare_cases(ids, runner.ROOT))
    identities = []

    class FakeProposer:
        reserve_tokens = 16384
        attempts = []

        def __init__(self, cases):
            self.cases = cases

        def identity(self):
            identities.append(1)
            return {'model': {'digest': 'c' * 64}, 'server': {'version': 'changed' if drift and len(identities) == 2 else 'fixed'}, 'show': {}}

        def __call__(self, case_id, attempt_index, feedback, deadline):
            assert attempt_index == 1 and feedback == ()
            payload = self.cases.payload(case_id)
            raw = canonical({'schema_version': '1.0',
                'fact_order': [row['fact_id'] for row in reversed(payload['facts'])],
                'rule_order': list(reversed(payload['rule_keys']))})
            return {'outcome': 'completed', 'raw': raw, 'tokens': 1000}

    monkeypatch.setattr(worker, 'OllamaProposer', FakeProposer)
    assert worker.run(directory, tmp_path) == (2 if drift else 0)
    result = json.loads((directory / 'worker-result.json').read_text('utf-8'))
    assert result['model_identity_stability'] == ('ERROR' if drift else 'PASS')
    assert result['new_model_calls'] == 1 and result['product_acceptance'] == 'INCOMPLETE'
    assert json.loads((directory / 'cycle.json').read_text('utf-8'))['cases'][0]['final_candidate_status'] == 'PASS'
    assert 'QA-S01' in json.loads((directory / 'candidates.json').read_text('utf-8'))


def test_cli_masks_unexpected_exception(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['run_autonomy.py', '--output-dir', 'var/qa/autonomy/unit'])

    def fail(*args, **kwargs):
        raise RuntimeError('private raw value')

    monkeypatch.setattr(runner, 'run', fail)
    assert runner.main() == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['error_code'] == 'AUTONOMY_INVALID_RUN'
    assert 'private raw value' not in json.dumps(payload)
