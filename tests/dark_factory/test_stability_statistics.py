"""Independent small-panel checks for statistics, failure denominators and costs."""
from copy import deepcopy
import math

import pytest

from qa.stability.statistics import summarize, validate_plan, wilson


def plan(*, arms=None, repeats=3):
    return {'schema_version': '1.0', 'plan_id': 'unit-panel', 'evidence_kind': 'synthetic_control',
            'arms': arms or ['candidate'], 'cases': [
                {'case_id': 'CASE-A', 'family_id': 'FAMILY-1', 'repeats': repeats},
                {'case_id': 'CASE-B', 'family_id': 'FAMILY-1', 'repeats': repeats},
                {'case_id': 'CASE-C', 'family_id': 'FAMILY-2', 'repeats': repeats},
                {'case_id': 'CASE-D', 'family_id': 'FAMILY-2', 'repeats': repeats},
                {'case_id': 'CASE-E', 'family_id': 'FAMILY-3', 'repeats': repeats},
            ]}


def observations(panel):
    return [{'arm': arm, 'case_id': case['case_id'], 'repeat_index': index,
             'status': 'PASS', 'outcome': 'draft', 'semantic_fingerprint': 'a' * 64,
             'ordered_fingerprint': 'b' * 64, 'latency_seconds': index,
             'attempts': [{'status': 'completed', 'elapsed_seconds': index / 2,
                           'prompt_tokens': 10, 'output_tokens': 2}]}
            for arm in panel['arms'] for case in panel['cases'] for index in range(1, case['repeats'] + 1)]


def test_fifteen_repeats_are_only_three_predeclared_families():
    panel = plan()
    rows = observations(panel)
    saved = deepcopy((panel, rows))
    result = summarize(panel, rows)
    arm = result['arms']['candidate']
    assert result['status'] == 'PASS'
    assert arm['pass_rate'] == {'numerator': 15, 'denominator': 15, 'value': 1.0}
    assert arm['family_pass_rate'] == {'numerator': 3, 'denominator': 3, 'value': 1.0}
    assert arm['conditional_family_interval']['total'] == 3
    assert arm['conditional_family_interval']['lower'] == pytest.approx(0.4385029682449546)
    assert arm['conditional_family_interval']['lower'] < wilson(15, 15)['lower']
    assert result['population_inference'] == 'NOT_ESTABLISHED'
    assert result['product_acceptance'] == 'INCOMPLETE'
    assert result['paired']['status'] == 'NOT_APPLICABLE'
    assert (panel, rows) == saved


@pytest.mark.parametrize('successes,total,lower,upper', [
    (4, 20, 0.08065766257979806, 0.4160174322518936),
    (0, 3, 0.0, 0.5614970317550454),
    (3, 3, 0.4385029682449546, 1.0),
    (50, 100, 0.4038315303659956, 0.5961684696340044),
])
def test_wilson_matches_independent_reference_values(successes, total, lower, upper):
    interval = wilson(successes, total)
    assert interval['confidence_level'] == .95
    assert interval['lower'] == pytest.approx(lower, abs=1e-12)
    assert interval['upper'] == pytest.approx(upper, abs=1e-12)
    assert 0 <= interval['lower'] <= interval['upper'] <= 1


def test_empty_binomial_sample_has_no_interval():
    assert wilson(0, 0) is None


@pytest.mark.parametrize('successes,total', [(True, 2), (1, True), (1.0, 2), (1, 2.0),
                                          (-1, 2), (3, 2), (1, 0), (0, -1), (0, 10**10)])
def test_invalid_binomial_counts_fail_closed(successes, total):
    with pytest.raises(ValueError, match='STABILITY_BINOMIAL_COUNTS'):
        wilson(successes, total)


@pytest.mark.parametrize('kind', ['missing', 'not_run', 'timeout', 'error'])
def test_incomplete_or_failed_measurements_keep_full_denominator_without_interval(kind):
    panel = plan()
    rows = observations(panel)
    if kind == 'missing':
        rows.pop()
    else:
        rows[-1].update(status={'not_run': 'NOT_RUN', 'timeout': 'INCOMPLETE', 'error': 'ERROR'}[kind],
                        outcome={'not_run': 'not_run', 'timeout': 'timeout', 'error': 'error'}[kind],
                        semantic_fingerprint=None, ordered_fingerprint=None, latency_seconds=None,
                        attempts=[] if kind == 'not_run' else [
                            {'status': kind, 'elapsed_seconds': 1, 'prompt_tokens': None, 'output_tokens': None}])
    result = summarize(panel, rows)
    assert result['status'] == ('ERROR' if kind == 'error' else 'INCOMPLETE')
    assert result['planned_slots'] == 15
    assert result['arms']['candidate']['pass_rate'] == {'numerator': 14, 'denominator': 15, 'value': 14 / 15}
    assert result['missing_slots'] == int(kind == 'missing')
    assert result['not_run_slots'] == int(kind == 'not_run')
    assert result['arms']['candidate']['conditional_family_interval'] is None


def test_failure_precedes_missing_and_error_precedes_failure():
    panel, rows = plan(), observations(plan())
    rows[0]['status'] = 'FAIL'
    rows.pop()
    assert summarize(panel, rows)['status'] == 'FAIL'
    rows[1]['status'] = 'ERROR'
    assert summarize(panel, rows)['status'] == 'ERROR'


def test_one_failure_is_a_family_failure_without_reweighting_large_families():
    panel, rows = plan(), observations(plan())
    rows[0]['status'] = 'FAIL'
    arm = summarize(panel, rows)['arms']['candidate']
    assert arm['pass_rate']['value'] == 14 / 15
    assert arm['family_pass_rate'] == {'numerator': 2, 'denominator': 3, 'value': 2 / 3}
    assert arm['families'][0]['status'] == 'FAIL'
    assert arm['conditional_family_interval']['successes'] == 2


def test_order_variation_is_separate_from_semantic_variation():
    panel = plan(repeats=2)
    rows = observations(panel)
    rows[1]['ordered_fingerprint'] = 'c' * 64
    rows[3]['semantic_fingerprint'] = 'd' * 64
    cases = summarize(panel, rows)['arms']['candidate']['cases']
    assert cases[0]['variability']['distinct_semantic_reports'] == 1
    assert cases[0]['variability']['distinct_ordered_reports'] == 2
    assert cases[1]['variability']['distinct_semantic_reports'] == 2


@pytest.mark.parametrize('kind', ['single', 'refusals', 'failures'])
def test_insufficient_valid_drafts_never_imply_stable_answers(kind):
    panel = plan(repeats=1 if kind == 'single' else 3)
    rows = observations(panel)
    if kind == 'refusals':
        for row in rows:
            row.update(outcome='refusal', semantic_fingerprint=None, ordered_fingerprint=None, attempts=[])
    elif kind == 'failures':
        for row in rows:
            row['status'] = 'FAIL'
    for case in summarize(panel, rows)['arms']['candidate']['cases']:
        assert case['variability']['status'] == 'NOT_MEASURED'
        assert case['variability']['distinct_semantic_reports'] is None


def test_latency_includes_errors_and_missing_is_not_zero_or_dropped():
    panel = plan(repeats=1)
    rows = observations(panel)
    for row, latency in zip(rows, [1, 2, 3, 40, None]):
        row['latency_seconds'] = latency
    rows[3]['status'] = 'ERROR'
    arm = summarize(panel, rows)['arms']['candidate']
    assert arm['latency'] == {
        'measured_slots': 4, 'planned_slots': 5, 'missing_measurements': 1,
        'minimum_seconds': 1, 'maximum_seconds': 40, 'median_seconds': 2.5,
        'p50_seconds': 2, 'p95_seconds': 40, 'quantile_method': 'nearest_rank',
        'scope': 'DESCRIPTIVE_RECORDED_LATENCIES_NOT_SLA'}


def test_retry_and_failed_attempt_costs_are_counted_and_unknown_is_not_free():
    panel = plan(repeats=1)
    rows = observations(panel)
    rows[0]['attempts'].insert(0, {'status': 'timeout', 'elapsed_seconds': 3,
                                  'prompt_tokens': 7, 'output_tokens': None})
    usage = summarize(panel, rows)['arms']['candidate']['usage']
    assert usage['recorded_attempts'] == 6
    assert usage['attempt_status_counts'] == {'completed': 5, 'timeout': 1, 'error': 0}
    assert usage['prompt_tokens']['known_sum'] == 57
    assert usage['prompt_tokens']['recorded_total'] == 57
    assert usage['output_tokens'] == {'known_sum': 10, 'known_attempts': 5,
                                     'unknown_attempts': 1, 'recorded_total': None}
    assert usage['elapsed_seconds']['known_sum'] == 5.5


def test_exact_matching_pairs_and_equal_family_weighting():
    panel = plan(arms=['baseline', 'candidate'], repeats=1)
    rows = observations(panel)
    rows[-1]['status'] = 'FAIL'
    paired = summarize(panel, list(reversed(rows)))['paired']
    assert paired['status'] == 'COMPLETE'
    assert paired['planned_pairs'] == paired['complete_pairs'] == 5
    assert paired['paired_pass_delta'] == -.2
    assert paired['mean_family_delta'] == -1 / 3
    assert paired['superiority'] == 'NOT_ESTABLISHED'


@pytest.mark.parametrize('defect', ['missing', 'not_run', 'error', 'incomplete'])
def test_paired_panel_does_not_select_only_complete_pairs(defect):
    panel = plan(arms=['baseline', 'candidate'], repeats=1)
    rows = observations(panel)
    if defect == 'missing':
        rows.pop()
    elif defect == 'not_run':
        rows[-1].update(status='NOT_RUN', outcome='not_run', semantic_fingerprint=None,
                        ordered_fingerprint=None, latency_seconds=None, attempts=[])
    else:
        rows[-1]['status'] = defect.upper()
    paired = summarize(panel, rows)['paired']
    assert paired['planned_pairs'] == 5 and paired['complete_pairs'] == 4
    assert paired['complete_pair_rate']['denominator'] == 5
    assert paired['paired_pass_delta'] is None
    assert paired['mean_family_delta'] is None
    assert paired['families'][-1]['delta'] is None


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(extra=True), lambda p: p.update(plan_id='../escape'),
    lambda p: p.update(schema_version='2.0'), lambda p: p.update(evidence_kind='production'),
    lambda p: p.update(arms=['candidate', 'baseline']), lambda p: p.update(arms=['candidate', 'candidate']),
    lambda p: p.update(cases=[]), lambda p: p['cases'].append(deepcopy(p['cases'][0])),
    lambda p: p['cases'][0].update(repeats=True), lambda p: p['cases'][0].update(repeats=21),
    lambda p: p['cases'][0].update(repeats=1.0), lambda p: p['cases'][0].update(family_id='bad space'),
    lambda p: p['cases'][0].update(independence_verified=True),
])
def test_invalid_plans_are_rejected(mutation):
    panel = plan()
    mutation(panel)
    with pytest.raises(ValueError, match='STABILITY_'):
        validate_plan(panel)


def test_panel_size_bound_counts_both_arms():
    panel = plan(arms=['baseline', 'candidate'], repeats=20)
    panel['cases'] = [{'case_id': f'C{i}', 'family_id': 'F', 'repeats': 20} for i in range(51)]
    with pytest.raises(ValueError, match='STABILITY_PANEL_SIZE'):
        validate_plan(panel)


@pytest.mark.parametrize('mutation', [
    lambda r: r.update(extra='secret'), lambda r: r.update(arm='foreign'),
    lambda r: r.update(case_id='foreign'), lambda r: r.update(repeat_index=True),
    lambda r: r.update(repeat_index=4), lambda r: r.update(status='UNKNOWN'),
    lambda r: r.update(status='PASS', outcome='timeout'),
    lambda r: r.update(outcome='refusal'), lambda r: r.update(semantic_fingerprint=None),
    lambda r: r.update(semantic_fingerprint='not-a-sha'),
    lambda r: r.update(latency_seconds=float('nan')), lambda r: r.update(latency_seconds=float('inf')),
    lambda r: r.update(latency_seconds=True), lambda r: r.update(latency_seconds=10**400),
    lambda r: r.update(attempts='one'), lambda r: r['attempts'][0].update(prompt_tokens=True),
    lambda r: r['attempts'][0].update(output_tokens=-1), lambda r: r['attempts'][0].update(elapsed_seconds=math.inf),
    lambda r: r['attempts'][0].update(status='timeout'), lambda r: r['attempts'][0].update(secret='bad'),
])
def test_invalid_observations_fail_closed_without_unvalidated_metric_values(mutation):
    panel, rows = plan(), observations(plan())
    mutation(rows[0])
    with pytest.raises(ValueError, match='STABILITY_'):
        summarize(panel, rows)


def test_duplicate_slot_is_not_an_additional_sample():
    panel, rows = plan(), observations(plan())
    rows.append(deepcopy(rows[0]))
    with pytest.raises(ValueError, match='STABILITY_DUPLICATE_SLOT'):
        summarize(panel, rows)


def test_no_observations_preserves_plan_and_has_no_measured_variability():
    result = summarize(plan(), [])
    assert result['status'] == 'INCOMPLETE'
    assert result['planned_slots'] == result['missing_slots'] == 15
    arm = result['arms']['candidate']
    assert arm['pass_rate']['value'] == 0
    assert arm['latency']['p95_seconds'] is None
    assert arm['conditional_family_interval'] is None
    assert arm['usage']['recorded_attempts'] == 0
