"""Golden controls must detect defective observations, not just valid fixtures."""
from copy import deepcopy
from pathlib import Path

import pytest

from qa.scenarios import load_catalog
from qa.scenario_checks import check_observation, run_scenario
from src.evaluation import evaluate_bundle

FOLDER = Path(__file__).resolve().parents[2] / 'qa/datasets/aml-v1'


@pytest.fixture(scope='module')
def catalog():
    return load_catalog(FOLDER)


@pytest.fixture(scope='module')
def observations(catalog):
    return {scenario.case.id: run_scenario(scenario, FOLDER / scenario.fixture) for scenario in catalog.cases}


@pytest.mark.parametrize('case_id', [f'QA-S{number:02}' for number in range(1, 16)])
def test_versioned_scenario_contract(case_id, catalog, observations):
    scenario = next(item for item in catalog.cases if item.case.id == case_id)
    assert check_observation(scenario, observations[case_id]) == []


def example(catalog, observations, case_id='QA-S01'):
    scenario = next(item for item in catalog.cases if item.case.id == case_id)
    return scenario, deepcopy(observations[case_id])


@pytest.mark.parametrize('defect', ['drop_fact', 'duplicate_fact', 'wrong_sum', 'wrong_currency',
                                    'wrong_members', 'unsupported_text', 'wrong_version', 'wrong_digest'])
def test_golden_detects_corrupted_report(catalog, observations, defect):
    scenario, observed = example(catalog, observations)
    facts = observed.result['found_facts']
    total = next(fact for fact in facts if fact['evidence'][0]['kind'] == 'sum')
    if defect == 'drop_fact':
        facts.pop(0)
    elif defect == 'duplicate_fact':
        facts.append(deepcopy(facts[0]))
    elif defect == 'wrong_sum':
        total['evidence'][0]['value'] = '1.00'
    elif defect == 'wrong_currency':
        total['evidence'][0]['currency'] = 'USD'
    elif defect == 'wrong_members':
        total['evidence'][0]['tx_ids'].pop()
    elif defect == 'unsupported_text':
        facts[0]['fact'] = 'Оператор уже одобрил перевод.'
    elif defect == 'wrong_version':
        observed.result['applicable_rules'][0]['citation']['rule_version'] = 'UNAPPROVED'
    else:
        observed.result['applicable_rules'][0]['citation']['content_sha256'] = '0' * 64
    assert check_observation(scenario, observed), 'Defective report incorrectly passed'


def test_fact_order_and_report_identity_are_not_golden_equality(catalog, observations):
    scenario, observed = example(catalog, observations)
    observed.result['found_facts'].reverse()
    for fact in observed.result['found_facts']:
        fact['evidence'].reverse()
    observed.result['report_id'] = 'new-valid-report-id'
    observed.result['generated_at'] = '2026-10-09T12:00:00Z'
    assert check_observation(scenario, observed) == []


def test_refusal_with_wrong_reason_fails(catalog, observations):
    scenario, observed = example(catalog, observations, 'QA-S15')
    observed.result['reason_code'] = 'UNTRUSTED_INPUT'
    assert 'WRONG_REASON' in check_observation(scenario, observed)


def test_critical_refusal_cannot_be_replaced_with_valid_draft(catalog, observations):
    scenario, observed = example(catalog, observations, 'QA-S15')
    observed.result = deepcopy(observations['QA-S01'].result)
    assert 'WRONG_STATUS' in check_observation(scenario, observed)


def test_forbidden_text_cannot_reach_model_prompt(catalog, observations):
    scenario, observed = example(catalog, observations, 'QA-S14')
    assert scenario.forbidden_prompt_strings
    observed.model_prompts[0]['unsafe_extra'] = scenario.forbidden_prompt_strings[0]
    assert 'UNTRUSTED_TEXT_IN_MODEL_PAYLOAD' in check_observation(scenario, observed)


def test_uppercase_forbidden_text_in_refusal_is_rejected(catalog, observations):
    scenario, observed = example(catalog, observations, 'QA-S15')
    observed.result['reason'] = 'ОДОБРЕНО АВТОМАТИЧЕСКИ'
    assert 'FORBIDDEN_OUTPUT' in check_observation(scenario, observed)


def test_evaluator_failure_cannot_be_hidden_by_passing_case_checks(tmp_path, monkeypatch):
    import src.evaluation
    from qa.tools.check_scenarios import qualify
    monkeypatch.setattr(src.evaluation, 'evaluate_bundle', lambda _replay: {'verdict': 'FAIL'})
    result = qualify(tmp_path / 'evidence', 'calibration')
    assert all(row['status'] == 'PASS' for row in result['cases'])
    assert result['status'] == 'FAIL'
    assert result['qualification_issues'] == ['EVALUATOR_MANDATORY_GATE_FAILED']


def test_missing_outcomes_are_incomplete_not_pass(catalog):
    result = evaluate_bundle({'schema_version': '1.0', 'dataset': catalog.dataset.model_dump(mode='json'),
                              'cases': [item.case.model_dump(mode='json') for item in catalog.cases],
                              'outcomes': []})
    assert result['verdict'] == 'INCOMPLETE'
    assert result['coverage']['missing_outcome_count'] == len(catalog.cases)
    assert result['metrics']['M-08 Draft Acceptance Rate']['status'] == 'not_measured'
