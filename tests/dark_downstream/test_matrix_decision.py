"""Regressions for binding an agent's final citation to the observed search."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from qa.dark_downstream.matrix_decision import SOURCE, assess, observe, selected_cases
from qa.scenario_checks import _SyntheticEmbedding
from src.config import get_settings
from src.runtime.retrieval import RuleIndex


@pytest.fixture
def agent_observation(tmp_path, monkeypatch):
    import src.runtime.context as context
    monkeypatch.setattr(context, 'PROJECT_ROOT', tmp_path)
    source = Path(__file__).resolve().parents[2] / SOURCE

    def factory(path):
        raw = path.read_bytes()
        return RuleIndex.from_records(json.loads(raw), embedding=_SyntheticEmbedding(),
                                      embedding_manifest_digest='a' * 64, source_sha256=sha256(raw).hexdigest())

    def run(case):
        doc = observe(source, tmp_path / case['case_id'], case, get_settings(), factory)
        return doc, doc['calls'][0]['raw'][:3]
    return run


@pytest.mark.parametrize('case', selected_cases(), ids=lambda c: c['case_id'])
def test_exact_report_or_explicit_refusal_with_real_agent_and_fake_vectors(case, agent_observation):
    doc, reference = agent_observation(case)
    row = assess(case, doc, reference)
    assert row['trace_status'] == row['decision_status'] == 'PASS'
    assert row['same_first_three']
    assert row['reported_citations'] == case['relevant']
    assert row['reason'] == (None if case['relevant'] else 'NO_APPLICABLE_RULE')


@pytest.mark.parametrize('change', ['wrong-edition', 'wrong-rule', 'wrong-quote'])
def test_wrong_rule_in_a_well_formed_draft_is_still_a_failure(change, agent_observation):
    case = selected_cases()[0]
    doc, reference = agent_observation(case)
    rule = doc['result']['applicable_rules'][0]
    if change == 'wrong-edition':
        rule['citation']['rule_version'] = 'v2'
    elif change == 'wrong-rule':
        another = next(r for r in doc['calls'][0]['raw'] if r['citation']['rule_id'] == 'MATRIX-02')
        rule['citation'] = deepcopy(another['citation'])
    else:
        rule['rule_text'] = 'Another synthetic quote.'
    row = assess(case, doc, reference)
    assert row['trace_status'] == 'PASS' and row['decision_status'] == 'FAIL'


def test_missing_tail_cannot_prove_the_agent_searched_all_active_rules(agent_observation):
    case = selected_cases()[0]
    doc, reference = agent_observation(case)
    doc['calls'][0]['raw'].pop()
    row = assess(case, doc, reference)
    assert row['trace_status'] == 'INCOMPLETE' and row['same_first_three'] is None


def test_matrix_order_compared_separately_from_correct_agent_decision(agent_observation):
    case = selected_cases()[1]
    doc, reference = agent_observation(case)
    row = assess(case, doc, list(reversed(reference)))
    assert row['same_first_three'] is False and row['decision_status'] == 'PASS'
