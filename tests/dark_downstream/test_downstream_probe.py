"""Deterministic harness checks: fake vectors do not measure real embedding."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from qa.dark_downstream.probe import CASES, RecordingIndex, evaluate, run
from qa.scenario_checks import _SyntheticEmbedding
from src.runtime.retrieval import RuleIndex


def raw(rule_id):
    citation = {'rule_id': rule_id, 'rule_version': 'v1', 'source_ref': 'test',
                'chunk_id': 'chunk-test', 'content_sha256': 'a' * 64}
    return {'query': 'high_risk_country', 'as_of': '2026-07-20T00:00:00Z', 'k': 1,
            'raw': [{'citation': citation, 'distance': 0.5}]}


def test_positive_result_must_be_observed_in_search():
    case = CASES[2]
    call = raw('R-115-006')
    result = {'status': 'draft', 'applicable_rules': [{'citation': call['raw'][0]['citation']}]}
    output = evaluate(case, result, [call])
    assert output['status'] == 'PASS'
    assert output['semantic_relevance'] == 'NOT_ESTABLISHED'
    result['applicable_rules'][0]['citation'] = dict(result['applicable_rules'][0]['citation'], rule_id='other')
    assert evaluate(case, result, [call])['status'] == 'INCOMPLETE'


def test_missing_or_failed_search_is_not_a_pass():
    case = CASES[1]
    result = {'status': 'refusal', 'reason_code': 'NO_APPLICABLE_RULE'}
    assert evaluate(case, result, [])['status'] == 'INCOMPLETE'
    assert evaluate(case, {'status': 'error'}, [raw('anything')])['status'] == 'INCOMPLETE'
    assert evaluate(case, result, [dict(raw('anything'), raw=[])])['status'] == 'INCOMPLETE'


def test_unexpected_final_result_remains_failure():
    case = CASES[0]
    result = {'status': 'refusal', 'reason_code': 'CONFLICTING_DATA'}
    assert evaluate(case, result, [])['status'] == 'FAIL'


def test_recorder_delegates_without_filtering_or_reordering():
    chunks = [SimpleNamespace(citation=SimpleNamespace(model_dump=lambda i=i, **_: {'rule_id': str(i)}), distance=i)
              for i in [2, 1]]
    fake = SimpleNamespace(provenance={'source_sha256': 'abc'},
                           active_chunk_count=lambda as_of: 2,
                           search=lambda query, as_of, k: chunks)
    recorder = RecordingIndex(fake)
    assert recorder.search('high_risk_country', as_of='date', k=2) is chunks
    assert [x['citation']['rule_id'] for x in recorder.calls[0]['raw']] == ['2', '1']
    assert recorder.provenance == fake.provenance
    assert recorder.active_chunk_count('date') == 2


def test_end_to_end_with_explicit_fake_embedding(tmp_path, monkeypatch):
    import src.runtime.context as runtime_context
    from src.config import get_settings
    source = Path(__file__).resolve().parents[2] / 'qa/datasets/aml-v1/fixtures/S03'
    copied = tmp_path / 'source'
    shutil.copytree(source, copied)
    destination = tmp_path / 'output'
    destination.mkdir()
    monkeypatch.setattr(runtime_context, 'PROJECT_ROOT', tmp_path)

    def factory(path):
        raw_data = path.read_bytes()
        return RuleIndex.from_records(json.loads(raw_data), embedding=_SyntheticEmbedding(),
                                      embedding_manifest_digest=sha256(b'FAKE').hexdigest(),
                                      source_sha256=sha256(raw_data).hexdigest())

    result = run(copied, destination, get_settings(), factory)
    assert result['status'] == 'PASS'
    assert result['embedding_dependency'] == 'SUPPLIED_BY_CALLER'
    assert result['llm'] == 'NOT_RUN'
    assert [x['status'] for x in result['cases']] == ['PASS'] * 4
    assert [x['search_observed'] for x in result['cases']] == [False, True, True, True]
    assert result['cases'][2]['intentional_policy_mutation']
    assert result['policy_safety'] == 'FAIL_FOR_CONTROLLED_MUTATION'
    from qa.dark_downstream.live import validate_result
    before = {'synthetic-source': 'hash'}
    result.update({'source_sha256_before': before, 'source_sha256_after': before,
                   'embedding_dependency': 'real_local_embedding_cpu',
                   'embedding_artifact_validation': 'PASS'})
    assert validate_result(result, before, before) == 'PASS'
    modified = deepcopy(result)
    modified['cases'][2]['policy_safety'] = 'NOT_ASSESSED'
    with pytest.raises(ValueError):
        validate_result(modified, before, before)
    with pytest.raises(ValueError):
        validate_result(result, before, {'synthetic-source': 'changed'})


def test_summary_keeps_measurement_pass_and_controlled_safety_failure_distinct(monkeypatch):
    from qa.dark_downstream import live
    monkeypatch.setattr(live, 'validate_result', lambda result, before, after: 'PASS')
    summary = {'status': 'INCOMPLETE', 'policy_safety': 'INCOMPLETE'}
    live.incorporate(summary, {'policy_safety': 'FAIL_FOR_CONTROLLED_MUTATION'}, {}, {})
    assert summary['status'] == 'PASS'
    assert summary['policy_safety'] == 'FAIL_FOR_CONTROLLED_MUTATION'


def test_invalid_evidence_cannot_promote_summary(monkeypatch):
    from qa.dark_downstream import live
    def invalid(*args):
        raise ValueError('source changed')
    monkeypatch.setattr(live, 'validate_result', invalid)
    summary = {'status': 'INCOMPLETE', 'policy_safety': 'INCOMPLETE'}
    with pytest.raises(ValueError):
        live.incorporate(summary, {'policy_safety': 'FAIL_FOR_CONTROLLED_MUTATION'}, {}, {})
    assert summary == {'status': 'INCOMPLETE', 'policy_safety': 'INCOMPLETE'}
