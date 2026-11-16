"""An injected allowlist defect must be caught without changing the fixed oracle."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

from qa.dark_downstream import policy_pair as pair
from qa.scenario_checks import _SyntheticEmbedding
from src.config import get_settings
from src.runtime.retrieval import RuleIndex


@pytest.fixture
def executed(tmp_path, monkeypatch):
    import src.runtime.context as context
    source = Path(__file__).resolve().parents[2] / pair.SOURCE
    copied = tmp_path / 'source'
    copied.mkdir()
    for name in ('cases.json', 'clients.json', 'cards.json', 'transactions.json'):
        shutil.copyfile(source / name, copied / name)
    monkeypatch.setattr(pair, 'ROOT', tmp_path)
    monkeypatch.setattr(pair, 'SOURCE', 'source')
    monkeypatch.setattr(context, 'PROJECT_ROOT', tmp_path)
    output = tmp_path / 'paired'
    output.mkdir()
    def factory(path):
        raw = path.read_bytes()
        return RuleIndex.from_records(json.loads(raw), embedding=_SyntheticEmbedding(),
                                      embedding_manifest_digest='a' * 64, source_sha256=sha256(raw).hexdigest())
    docs = {}
    for case in pair.selected_cases():
        docs[case['case_id']] = {
            arm: pair.observe(output, case, arm, get_settings(), factory) for arm in pair.ARMS}
    return output, docs


def assessed(case, docs):
    anchor = docs[case['case_id']]['baseline']
    arms = {arm: pair.classify(case, doc, anchor) for arm, doc in docs[case['case_id']].items()}
    return pair.compare_pair(case, arms, same_search=True)


def test_same_corpus_and_search_but_two_added_citations_are_caught(executed):
    output, docs = executed
    for case in pair.selected_cases():
        name = case['case_id']
        assert docs[name]['baseline']['calls'] == docs[name]['candidate']['calls']
        report = assessed(case, docs)
        assert report['baseline']['decision'] == report['mutation_check'] == 'PASS'
        if case['relevant']:
            assert report['candidate']['decision'] == 'FAIL'
            assert [c['rule_id'] for c in report['candidate']['extra']] == [pair.EXTRA]
            assert not report['candidate']['missing']
        else:
            assert report['candidate']['decision'] == 'PASS'
            assert report['candidate']['reason'] == 'NO_APPLICABLE_RULE'
        baseline = pair.variant_files(pair.ROOT / pair.SOURCE, case, 'baseline')
        candidate = pair.variant_files(pair.ROOT / pair.SOURCE, case, 'candidate')
        assert [file for file in baseline if baseline[file] != candidate[file]] == ['policy.json']


def test_changed_search_cannot_establish_a_policy_only_regression(executed):
    _, docs = executed
    case = pair.selected_cases()[0]
    report = assessed(case, docs)
    unpaired = pair.compare_pair(case, {arm: report[arm] for arm in pair.ARMS}, same_search=False)
    assert unpaired['measurement_status'] == unpaired['mutation_check'] == 'INCOMPLETE'
    assert unpaired['candidate']['decision'] == 'FAIL'


@pytest.mark.parametrize('damage', ['quote', 'missing-required', 'stale-extra'])
def test_another_defect_cannot_masquerade_as_exact_intended_mutation(executed, damage):
    _, docs = executed
    case = pair.selected_cases()[0]
    changed = deepcopy(docs)
    rules = changed[case['case_id']]['candidate']['result']['applicable_rules']
    extra = next(r for r in rules if r['citation']['rule_id'] == pair.EXTRA)
    if damage == 'quote':
        extra['rule_text'] = 'Counterfeit text'
    elif damage == 'missing-required':
        rules[:] = [extra]
    else:
        extra['citation']['rule_version'] = 'v99'
    report = assessed(case, changed)
    assert report['candidate']['decision'] == 'FAIL' and report['mutation_check'] == 'FAIL'


def test_offline_replay_keeps_observed_failure_when_another_row_goes_missing(executed, monkeypatch):
    output, docs = executed
    binding = {'synthetic': 'test-binding'}
    anchors = {name: arms['baseline'] for name, arms in docs.items()}
    monkeypatch.setattr(pair, 'checked_inputs', lambda _: (binding, anchors))
    monkeypatch.setattr(pair, 'read_doc', lambda path: json.loads(path.read_bytes()))
    hashes = {}
    for name, arms in docs.items():
        for arm, doc in arms.items():
            key = arm + '/' + name + '.json'
            (output / key).write_text(json.dumps(doc), encoding='utf-8')
            hashes[key] = pair.digest(doc)
    seal = {'profile': pair.PROFILE, 'run_id': output.name, 'model': pair.MODEL, 'engine': 'template',
            'inputs_sha256': pair.digest(binding), 'rows': hashes}
    (output / 'child-result.json').write_text(json.dumps(seal), encoding='utf-8')
    result = pair.replay(output)
    assert result['mutation_check'] == result['measurement_status'] == 'PASS'
    assert result['candidate_status'] == 'FAIL'
    (output / 'baseline/MX-01-old-active.json').unlink()
    partial = pair.replay(output)
    assert partial['measurement_status'] == partial['mutation_check'] == 'INCOMPLETE'
    assert partial['candidate_status'] == 'FAIL'


def test_candidate_cannot_change_expected_labels(executed):
    _, docs = executed
    for case in pair.selected_cases()[:2]:
        original = deepcopy(case['relevant'])
        assert assessed(case, docs)['candidate']['decision'] == 'FAIL'
        assert case['relevant'] == original
