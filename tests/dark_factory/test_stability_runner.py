"""Evidence lifecycle, control sensitivity and budget/stats integration."""
from copy import deepcopy
import json

import pytest

from qa.stability import demo, runner


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'snapshot', lambda root: {'digest': 'a' * 64})
    monkeypatch.setattr(runner, 'compare_snapshots', lambda a, b: {'status': 'PASS' if a == b else 'ERROR'})
    monkeypatch.setattr(runner, 'environment', lambda: {'digest': 'b' * 64})
    return tmp_path, 'var/qa/stability/unit'


def result(project):
    root, output = project
    return json.loads((root / output / 'result.json').read_text('utf-8'))


def test_real_demo_all_declared_controls_and_stopped_slots():
    value = demo.run_demo()
    assert value['qualification_status'] == 'PASS'
    assert len(value['controls']) == 4 and len(value['budget_controls']) == 6
    stopped = value['budgeted_panel']
    assert stopped['ledger']['attempted_calls'] == 8
    assert stopped['statistics']['status'] == 'INCOMPLETE'
    assert stopped['statistics']['planned_slots'] == 12
    assert stopped['statistics']['not_run_slots'] == 4
    assert stopped['statistics']['paired']['planned_pairs'] == 6
    assert stopped['statistics']['paired']['complete_pairs'] == 2
    assert stopped['statistics']['paired']['paired_pass_delta'] is None
    assert value['negative_statistics']['known_failure']['status'] == 'FAIL'
    assert value['negative_statistics']['missing_repeat']['missing_slots'] == 1
    unknown = next(r for r in value['budget_controls'] if r['id'] == 'unknown_usage')
    assert unknown['ledger']['charged_tokens'] == 10 and unknown['ledger']['total_tokens'] is None


def test_canary_detects_broken_budget_admission(monkeypatch):
    original = demo.BudgetLedger.reserve
    def too_permissive(self, tokens):
        self._limits['max_attempts'] = 100
        self._limits['max_tokens'] = 10000
        return original(self, tokens)
    monkeypatch.setattr(demo.BudgetLedger, 'reserve', too_permissive)
    value = demo.run_demo()
    assert value['qualification_status'] == 'FAIL'
    assert next(r for r in value['controls'] if r['id'] == 'budget_stopped_panel')['status'] == 'FAIL'


def test_ledger_does_not_retry_or_invoke_any_provider(monkeypatch):
    import socket
    import subprocess
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('unexpected network'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('unexpected process'))
    assert demo.run_demo()['qualification_status'] == 'PASS'


def test_owned_run_pins_outputs_and_separates_product_acceptance(project):
    root, output = project
    assert runner.run(output, root=root) == 0
    value = result(project)
    assert value['status'] == value['analysis_status'] == value['version_binding'] == 'PASS'
    assert value['qualification_status'] == 'PASS'
    assert value['population_inference'] == 'NOT_ESTABLISHED'
    assert value['product_acceptance'] == 'INCOMPLETE' and value['new_model_calls'] == 0
    evidence = json.loads((root / output / 'evidence.json').read_text('utf-8'))
    assert runner.verify_files(evidence['files'], root)['status'] == 'PASS'
    assert 'NOT_MEASURED' in (root / output / 'report.md').read_text('utf-8')


def test_existing_run_and_user_files_untouched(project):
    root, output = project
    sentinel = root / 'user.sqlite'
    sentinel.write_bytes(b'ordinary user data')
    assert runner.run(output, root=root) == 0
    saved = (root / output / 'result.json').read_bytes()
    with pytest.raises(runner.StabilityRunError, match='STABILITY_OUTPUT_EXISTS'):
        runner.run(output, root=root)
    assert (root / output / 'result.json').read_bytes() == saved
    assert sentinel.read_bytes() == b'ordinary user data'


@pytest.mark.parametrize('path', ['var/qa/stability', 'var/qa/stability/a/b', 'var/qa/llm/x', '../outside'])
def test_wrong_output_namespace_rejected(tmp_path, path):
    with pytest.raises(ValueError):
        runner.output_path(path, tmp_path)


@pytest.mark.parametrize('kind', ['source', 'environment'])
def test_changed_execution_identity_is_error(project, monkeypatch, kind):
    calls = 0
    def changing(*args):
        nonlocal calls
        calls += 1
        return {'digest': str(calls) * 64}
    monkeypatch.setattr(runner, 'snapshot' if kind == 'source' else 'environment', changing)
    root, output = project
    assert runner.run(output, root=root) == 2
    assert result(project)['version_binding'] == 'ERROR'


def test_unexpected_error_is_saved_without_raw_exception(project, monkeypatch):
    def fail():
        raise RuntimeError('do-not-log-secret')
    monkeypatch.setattr(runner, 'run_demo', fail)
    root, output = project
    assert runner.run(output, root=root) == 2
    value = result(project)
    assert value['error_code'] == 'STABILITY_ANALYSIS_ERROR'
    assert 'do-not-log-secret' not in json.dumps(value)
    assert value['analysis_status'] == 'INCOMPLETE'


def test_interruption_keeps_result_incomplete_not_pass(project, monkeypatch):
    def interrupt():
        raise KeyboardInterrupt()
    monkeypatch.setattr(runner, 'run_demo', interrupt)
    root, output = project
    assert runner.run(output, root=root) == 2
    assert result(project)['error_code'] == 'INTERRUPTED'


@pytest.mark.parametrize('state,exit_code', [('PASS', 0), ('FAIL', 1), ('INCOMPLETE', 3)])
def test_history_status_and_budget_claim_preserved(project, monkeypatch, state, exit_code):
    root, output = project
    plan, rows = demo.panel()
    plan['arms'] = ['candidate']
    plan['evidence_kind'] = 'historical_llm'
    rows = [row for row in rows if row['arm'] == 'candidate']
    historical = root / 'archive.json'
    historical.write_text('{}', encoding='utf-8')
    imported = {'plan': plan, 'observations': rows, 'source_files': [runner.pin_file(historical, root)],
                'metadata': {'status': state, 'budget_enforcement': 'NOT_EVALUATED'}}
    monkeypatch.setattr(runner, 'load_history', lambda *a, **k: deepcopy(imported))
    assert runner.run(output, root=root, source='var/qa/llm/fixture') == exit_code
    value = result(project)
    assert value['budget_enforcement'] == 'NOT_EVALUATED' and value['qualification_status'] == 'NOT_RUN'
    assert value['new_model_calls'] == 0 and value['statistics']['planned_slots'] == 6


def test_history_mutation_between_read_and_pin_check_prevents_pass(project, monkeypatch):
    root, output = project
    original = root / 'archive.json'
    original.write_text('{}', encoding='utf-8')
    pin = runner.pin_file(original, root)
    def imported(*args, **kwargs):
        original.write_text('{"changed":true}', encoding='utf-8')
        return {'source_files': [pin]}
    monkeypatch.setattr(runner, 'load_history', imported)
    assert runner.run(output, root=root, source='var/qa/llm/fixture') == 2
    assert result(project)['error_code'] == 'STABILITY_HISTORY_CHANGED'


def test_evidence_mutation_after_assessment_is_detected(project, monkeypatch):
    root, output = project
    original = runner.pin_file
    def corrupt(path, root):
        pinned = original(path, root)
        if path.name == 'statistics.json':
            path.write_text('{}', encoding='utf-8')
        return pinned
    monkeypatch.setattr(runner, 'pin_file', corrupt)
    assert runner.run(output, root=root) == 2
    assert result(project)['evidence_integrity']['status'] == 'ERROR'


def test_failed_control_is_not_hidden_by_good_statistics(project, monkeypatch):
    original = runner.run_demo
    def bad():
        value = original()
        value['qualification_status'] = 'FAIL'
        return value
    monkeypatch.setattr(runner, 'run_demo', bad)
    root, output = project
    assert runner.run(output, root=root) == 1
    assert result(project)['statistics']['status'] == 'PASS'


def test_cli_masks_output_errors(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['run_stability.py', '--output-dir', 'var/qa/stability/unit'])
    def bad(*args, **kwargs):
        raise RuntimeError('raw source value')
    monkeypatch.setattr(runner, 'run', bad)
    assert runner.main() == 2
    assert json.loads(capsys.readouterr().out)['error_code'] == 'STABILITY_OUTPUT_ERROR'
