"""Mutation controls for real-LLM evaluation; tests themselves use no models."""
from copy import deepcopy

import pytest

from qa.model_eval.checks import check_repeat, summarize_repeats
from qa.scenario_checks import run_scenario
from qa.scenarios import DEFAULT_FOLDER, load_catalog


@pytest.fixture(scope='module')
def examples():
    return {scenario.case.id: (scenario, run_scenario(scenario, DEFAULT_FOLDER / scenario.fixture))
            for scenario in load_catalog().cases}


def sample(examples, case='QA-S01'):
    scenario, observed = examples[case]
    return scenario, deepcopy(observed)


def row(examples, case='QA-S01', index=1):
    scenario, observed = sample(examples, case)
    return check_repeat(scenario, observed, repeat_index=index)


def test_report_identity_and_clock_do_not_create_variation(examples):
    scenario, observed = sample(examples)
    first = check_repeat(scenario, observed, repeat_index=1)
    observed.result['report_id'] = 'another-report'
    observed.result['generated_at'] = '2026-10-20T12:00:00Z'
    second = check_repeat(scenario, observed, repeat_index=2)
    assert first['status'] == second['status'] == 'PASS'
    assert first['draft'] == second['draft']


def test_valid_order_change_preserves_semantics_and_inputs(examples):
    scenario, observed = sample(examples)
    first = check_repeat(scenario, observed, repeat_index=1)
    observed.result['found_facts'].reverse()
    observed.result['applicable_rules'].reverse()
    before = deepcopy(observed)
    second = check_repeat(scenario, observed, repeat_index=2)
    assert observed == before
    assert second['status'] == 'PASS'
    assert first['draft']['ordered_fingerprint'] != second['draft']['ordered_fingerprint']
    assert first['draft']['semantic_fingerprint'] == second['draft']['semantic_fingerprint']
    summary = summarize_repeats([first, second], planned_case_ids=['QA-S01'], repeats_per_case=2)
    variability = summary['cases'][0]['variability']
    assert variability['status'] == 'evaluated'
    assert variability['distinct_semantic_reports'] == 1
    assert variability['distinct_fact_orders'] == 2


@pytest.mark.parametrize('defect', [
    'wrong_sum', 'invented_fact', 'source_version', 'source_digest',
    'source_reference', 'transaction_timestamp', 'snapshot_id', 'drop_fact',
])
def test_fact_and_source_mutations_fail_independent_oracle(examples, defect):
    scenario, observed = sample(examples)
    baseline = check_repeat(scenario, observed, repeat_index=1)
    facts = observed.result['found_facts']
    total = next(fact for fact in facts if fact['evidence'][0]['kind'] == 'sum')
    citation = observed.result['applicable_rules'][0]['citation']
    if defect == 'wrong_sum':
        total['fact'] = total['fact'].replace(total['evidence'][0]['value'], '1.00')
        total['evidence'][0]['value'] = '1.00'
    elif defect == 'invented_fact':
        facts[0]['fact'] = 'Одобрение оператора подтверждено.'
    elif defect == 'source_version':
        citation['rule_version'] = 'unapproved-version'
    elif defect == 'source_digest':
        citation['content_sha256'] = '0' * 64
    elif defect == 'source_reference':
        citation['source_ref'] = 'Unapproved regulation'
    elif defect == 'transaction_timestamp':
        transaction = next(fact for fact in facts if fact['evidence'][0]['kind'] == 'transaction')
        transaction['fact'] = transaction['fact'].replace('время ', 'время 2099-01-01T00:00:00Z вместо ')
    elif defect == 'snapshot_id':
        observed.result['snapshot_id'] = 'another-snapshot'
    else:
        facts.pop()
    result = check_repeat(scenario, observed, repeat_index=2)
    assert result['status'] == 'FAIL'
    assert result['fact_issues']
    if result['draft']:
        assert result['draft']['semantic_fingerprint'] != baseline['draft']['semantic_fingerprint']


def test_source_collection_order_does_not_change_semantics(examples):
    scenario, observed = sample(examples)
    first = check_repeat(scenario, observed, repeat_index=1)
    for fact in observed.result['found_facts']:
        fact['evidence'].reverse()
        for evidence in fact['evidence']:
            if 'tx_ids' in evidence:
                evidence['tx_ids'].reverse()
    second = check_repeat(scenario, observed, repeat_index=2)
    assert second['status'] == 'PASS'
    assert first['draft'] == second['draft']


def test_real_model_single_repair_needs_retry_event(examples):
    scenario, observed = sample(examples, 'QA-S14')
    observed.model_prompts.append(deepcopy(observed.model_prompts[0]))
    observed.events = [
        {'stage': 'generation', 'code': 'STARTED', 'attempt': 1},
        {'stage': 'validation', 'code': 'RETRY', 'attempt': 1},
        {'stage': 'generation', 'code': 'STARTED', 'attempt': 2},
        {'stage': 'validation', 'code': 'PASSED', 'attempt': 2},
    ]
    assert check_repeat(scenario, observed, repeat_index=1)['status'] == 'PASS'
    observed.events.pop(1)
    broken = check_repeat(scenario, observed, repeat_index=1)
    assert 'MODEL_RETRY_EVENTS_MISMATCH' in broken['issues']
    assert broken['status'] == 'FAIL'


def test_retry_allowance_does_not_suppress_payload_violation(examples):
    scenario, observed = sample(examples, 'QA-S14')
    observed.model_prompts[0]['untrusted'] = scenario.forbidden_prompt_strings[0]
    result = check_repeat(scenario, observed, repeat_index=1)
    assert result['status'] == 'FAIL'
    assert 'UNTRUSTED_TEXT_IN_MODEL_PAYLOAD' in result['issues']


def test_template_and_precondition_control_cannot_call_model(examples):
    for case in ('QA-S01', 'QA-S05'):
        scenario, observed = sample(examples, case)
        observed.model_prompts.append({'injected': 'model call'})
        assert 'MODEL_CALL_COUNT' in check_repeat(scenario, observed, repeat_index=1)['issues']


@pytest.mark.parametrize('case,outcome', [('QA-S05', 'refusal'), ('QA-S07', 'error'),
                                        ('QA-S13', 'dependency_failure')])
def test_non_draft_outcomes_have_no_variability(examples, case, outcome):
    rows = [row(examples, case, index) for index in (1, 2)]
    assert all(item['outcome'] == outcome and item['draft'] is None for item in rows)
    summary = summarize_repeats(rows, planned_case_ids=[case], repeats_per_case=2)
    variability = summary['cases'][0]['variability']
    assert variability['status'] == 'not_measured'
    assert variability['distinct_semantic_reports'] is None


@pytest.mark.parametrize('outcome,status', [('timeout', 'INCOMPLETE'),
                                          ('dependency_failure', 'INCOMPLETE'), ('error', 'FAIL')])
def test_transport_failure_and_missing_run_keep_denominator(examples, outcome, status):
    scenario, _ = sample(examples)
    failed = check_repeat(scenario, None, repeat_index=1, transport_outcome=outcome)
    summary = summarize_repeats([failed], planned_case_ids=['QA-S01'], repeats_per_case=3)
    assert summary['status'] == status
    assert summary['repeat_checks_pass_rate'] == {'numerator': 0, 'denominator': 3, 'value': 0}
    assert summary['missing_repeat_count'] == 2
    assert summary['outcome_counts'][outcome] == 1
    assert summary['cases'][0]['variability']['status'] == 'not_measured'


def test_missing_case_is_not_silently_removed_from_denominator(examples):
    result = summarize_repeats([row(examples)], planned_case_ids=['QA-S01', 'QA-S02'], repeats_per_case=3)
    assert result['status'] == 'INCOMPLETE'
    assert result['planned_repeat_count'] == 6
    assert result['observed_repeat_count'] == 1
    assert result['observed_independence_family_count'] == 1
    assert result['cases'][1]['missing_repeat_indices'] == [1, 2, 3]


def test_repetitions_are_not_independent_cases(examples):
    rows = [row(examples, index=index) for index in (1, 2, 3)]
    result = summarize_repeats(rows, planned_case_ids=['QA-S01'], repeats_per_case=3)
    assert result['status'] == 'PASS'
    assert result['observed_repeat_count'] == 3
    assert result['observed_independence_family_count'] == 1
    assert result['case_checks_pass_rate']['denominator'] == 1


def test_fact_failure_takes_precedence_over_missing_runs(examples):
    scenario, observed = sample(examples)
    observed.result['found_facts'].pop()
    bad = check_repeat(scenario, observed, repeat_index=1)
    result = summarize_repeats([bad], planned_case_ids=['QA-S01'], repeats_per_case=3)
    assert result['status'] == 'FAIL'
    assert result['fact_violation_count'] == 1
    assert result['missing_repeat_count'] == 2
    assert result['cases'][0]['variability']['status'] == 'not_measured'


@pytest.mark.parametrize('defect', ['duplicate', 'unknown_case', 'out_of_range', 'bad_family'])
def test_malformed_coverage_is_rejected(examples, defect):
    first = row(examples)
    second = row(examples, index=2)
    if defect == 'duplicate':
        second['repeat_index'] = 1
    elif defect == 'unknown_case':
        second['case_id'] = 'UNKNOWN'
    elif defect == 'out_of_range':
        second['repeat_index'] = 4
    else:
        second['independence_id'] = 'renamed-family'
    with pytest.raises(ValueError):
        summarize_repeats([first, second], planned_case_ids=['QA-S01'], repeats_per_case=3)


def test_missing_observation_cannot_claim_completed(examples):
    scenario, observed = sample(examples)
    with pytest.raises(ValueError, match='COMPLETED_RUN'):
        check_repeat(scenario, None, repeat_index=1)
    with pytest.raises(ValueError, match='FAILED_TRANSPORT'):
        check_repeat(scenario, observed, repeat_index=1, transport_outcome='timeout')
