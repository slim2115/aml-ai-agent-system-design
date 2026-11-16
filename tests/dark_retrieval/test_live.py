"""Supervisor and measurement semantics with fakes only; no model/network."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from qa.dark_retrieval import live
from qa.dark_retrieval.scenarios import development_cases


class Citation:
    def __init__(self, value):
        self.value = value

    def model_dump(self, mode):
        assert mode == 'json'
        return self.value


class FakeIndex:
    def __init__(self, records):
        self.records = records

    def search(self, query, *, as_of, k):
        cases = development_cases()
        label = cases[0]['relevant'][0] if as_of < '2026-07-20T00:00:00Z' else cases[1]['relevant'][0]
        return [SimpleNamespace(citation=Citation(label), distance=0.2)]


def fake_factory(records, *, embedding, embedding_manifest_digest):
    assert embedding_manifest_digest == 'a' * 64
    return FakeIndex(records)


def test_measure_is_real_retrieval_shape_but_never_invents_selection():
    result = live.measure(development_cases(), object(), 'a' * 64, index_factory=fake_factory)
    assert result['measurement_status'] == 'PASS'
    assert result['assessment']['status'] == 'INCOMPLETE'
    assert result['expert_acceptance'] == result['product_quality'] == 'INCOMPLETE'
    assert result['selection_observed'] is False and result['secret_holdout'] is False
    assert result['dependency'] == 'unknown' and result['dependency_validation'] == 'INCOMPLETE'
    assert result['network'] == 'not_established_by_measure'
    assert [item['as_of'] for item in result['cases']] == [
        '2026-07-19T23:59:59Z', '2026-07-20T00:00:00Z', '2026-07-20T00:00:00Z']
    assert result['assessment']['rows'][2]['ranking']['recall_at_k'] is None
    assert result['assessment']['false_accept']['denominator'] == 0


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(live, 'ROOT', tmp_path)
    monkeypatch.setattr(live, 'source_hashes', lambda: {'source': 'a' * 64})
    return tmp_path


def test_supervisor_accepts_only_complete_matching_evidence(root):
    def fake_process(args, **kwargs):
        assert kwargs['timeout'] == 50 and args[-2] == '--child'
        output = Path(args[-1])
        assert (output / 'attempt.json').is_file()
        report = live.measure(development_cases(), object(), 'a' * 64, index_factory=fake_factory)
        report.update(source_sha256_before={'source': 'a' * 64}, source_sha256_after={'source': 'a' * 64},
                      dependency='real_local_embedding_cpu', dependency_validation='PASS')
        live.write_new(output / 'child-result.json', report)
        return SimpleNamespace(returncode=0)
    summary = live.run('var/qa/dark-retrieval/run-01', 50, process_run=fake_process)
    assert summary['measurement_status'] == 'PASS'
    assert summary['status'] == summary['product_quality'] == 'INCOMPLETE'
    assert summary['reason'] == 'DOWNSTREAM_SELECTION_NOT_MEASURED'
    assert json.loads((root / 'var/qa/dark-retrieval/run-01/summary.json').read_text()) == summary
    with pytest.raises(FileExistsError):
        live.run('var/qa/dark-retrieval/run-01', 50, process_run=fake_process)


def test_timeout_preserves_attempt_and_is_not_success(root):
    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs['timeout'])
    summary = live.run('var/qa/dark-retrieval/timeout', 1, process_run=timeout)
    assert summary['reason'] == 'CHILD_TIMEOUT'
    assert summary['measurement_status'] == 'INCOMPLETE'
    assert (root / 'var/qa/dark-retrieval/timeout/attempt.json').is_file()
    assert (root / 'var/qa/dark-retrieval/timeout/summary.json').is_file()


def test_missing_or_tampered_child_result_is_incomplete(root):
    def empty(args, **kwargs):
        return SimpleNamespace(returncode=0)
    result = live.run('var/qa/dark-retrieval/no-result', 3, process_run=empty)
    assert result['reason'] == 'MISSING_CHILD_RESULT'
    def tampered(args, **kwargs):
        output = Path(args[-1])
        live.write_new(output / 'child-result.json', {'measurement_status': 'PASS',
            'source_sha256_before': {'source': 'bad'}, 'cases': [{}, {}, {}],
            'selection_observed': False, 'assessment': {'status': 'INCOMPLETE'}})
        return SimpleNamespace(returncode=0)
    result = live.run('var/qa/dark-retrieval/bad-result', 3, process_run=tampered)
    assert result['reason'] == 'RESULT_OR_SOURCE_MISMATCH'


@pytest.mark.parametrize('path', ['var/qa/dark-retrieval', 'var/qa/dark-retrieval/a/b',
                                  'var/qa/dark-retrieval/../escape', 'verification/old'])
def test_output_paths_cannot_escape_namespace(root, path):
    with pytest.raises(ValueError):
        live.output_path(path)


@pytest.mark.parametrize('timeout', [0, -1, 901, True, 3.5])
def test_timeout_must_be_bounded(root, timeout):
    with pytest.raises(ValueError, match='INVALID_TIMEOUT'):
        live.run('var/qa/dark-retrieval/fresh', timeout)


def test_network_guard_refuses_connections():
    with pytest.raises(RuntimeError, match='NETWORK_DISABLED'):
        live.deny_network('example.org', 443)


def test_changed_raw_or_claimed_judgement_fails_independent_recomputation():
    report = live.measure(development_cases(), object(), 'a' * 64, index_factory=fake_factory)
    report.update(source_sha256_before={'source': 'a' * 64}, source_sha256_after={'source': 'a' * 64},
                  dependency='real_local_embedding_cpu', dependency_validation='PASS')
    live.validate_result(report, {'source': 'a' * 64}, {'source': 'a' * 64})
    report['assessment']['rows'][0]['ranking']['hit_count'] = 999
    with pytest.raises(ValueError, match='RESULT_OR_SOURCE_MISMATCH'):
        live.validate_result(report, {'source': 'a' * 64}, {'source': 'a' * 64})


def test_child_rejects_history_and_existing_results(root):
    history = root / 'verification' / 'old'
    history.mkdir(parents=True)
    live.write_new(history / 'attempt.json', {'status': 'STARTED', 'timeout_seconds': 4,
                                               'source_sha256_before': {}})
    with pytest.raises(ValueError, match='INVALID_CHILD_OUTPUT'):
        live.validate_child_output(history)
    output = root / 'var' / 'qa' / 'dark-retrieval' / 'valid'
    output.mkdir(parents=True)
    live.write_new(output / 'attempt.json', {'status': 'STARTED', 'timeout_seconds': 4,
                                              'source_sha256_before': {}})
    assert live.validate_child_output(output) == output
    live.write_new(output / 'child-result.json', {'status': 'old'})
    with pytest.raises(FileExistsError, match='CHILD_OUTPUT_EXISTS'):
        live.validate_child_output(output)
