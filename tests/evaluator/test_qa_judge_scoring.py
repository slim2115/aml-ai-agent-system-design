"""Known defects and missing evidence cannot disappear from Judge metrics."""
from copy import deepcopy

import pytest

from qa.judge.scoring import summarize, validate_human_reviews


DATASET_SHA = 'a' * 64


def case(case_id='J-001', *, expected='pass', split='calibration', criterion='grounding'):
    return {
        'id': case_id, 'expected_verdict': expected,
        'expected_preference': 'not_applicable', 'expected_kind': 'synthetic_rule',
        'split': split, 'family': 'source-family-1',
        'programmatic_gate': {'status': 'PASS', 'issues': []},
        'payload': {
            'case_id': case_id, 'criterion': criterion, 'mode': 'single',
            'sources': [{'ref_id': 'source:amount', 'text': 'Total: 12.30 USD.'}],
            'candidates': [{'label': 'A', 'report': {'amount': '12.30'},
                            'fragments': [{'ref_id': 'A:amount', 'text': '12.30 USD'}]}],
        },
    }


def response(planned, *, verdict='pass', preference=None):
    if preference is None:
        preference = 'not_applicable' if planned['payload']['mode'] == 'single' else 'A'
    return {
        'schema_version': '1.0', 'rubric_version': '1.0', 'case_id': planned['id'],
        'criterion': planned['payload']['criterion'], 'verdict': verdict,
        'preference': preference, 'rationale': 'The supplied total is cited.',
        'references': [{'ref_id': 'source:amount', 'quote': '12.30 USD'}],
    }


def observed(planned, *, status='completed', **kwargs):
    return {'case_id': planned['id'], 'status': status, 'response': response(planned, **kwargs)}


def rating(planned, **kwargs):
    return {key: value for key, value in response(planned, **kwargs).items()
            if key not in {'schema_version', 'rubric_version'}}


def human_file(ratings):
    return {'schema_version': '1.0', 'rubric_version': '1.0', 'dataset_sha256': DATASET_SHA,
            'reviewer_id': 'synthetic-reviewer-for-unit-test',
            'reviewed_at': '2026-10-21T12:30:00+03:00', 'ratings': ratings}


def pair_cases():
    result = []
    for case_id, swap in [('J-AB', False), ('J-BA', True)]:
        item = case(case_id, criterion='ordering')
        item.update(pair_id='PAIR-001', expected_preference='B' if swap else 'A',
                    candidate_mapping={'A': 'reversed' if swap else 'original',
                                       'B': 'original' if swap else 'reversed'})
        item['payload']['mode'] = 'pair'
        item['payload']['candidates'].append({
            'label': 'B', 'report': {'amount': '12.30'},
            'fragments': [{'ref_id': 'B:amount', 'text': '12.30 USD'}],
        })
        result.append(item)
    return result


def test_missing_observations_keep_the_planned_denominator_and_never_pass():
    cases = [case('J-001'), case('J-002', expected='fail')]
    result = summarize(cases, [observed(cases[0])])
    assert result['execution_status'] == result['known_challenge_status'] == 'INCOMPLETE'
    totals = result['overall']
    assert totals['planned'] == 2 and totals['missing'] == 1
    assert totals['all_planned_accuracy']['rate'] == 0.5
    assert totals['valid_only_accuracy']['rate'] == 1
    assert totals['detection_rate']['denominator'] == 1
    assert totals['detection_rate']['rate'] == 0
    assert totals['valid_response_coverage']['rate'] == 0.5
    empty = summarize(cases, [])
    assert empty['known_challenge_status'] == 'INCOMPLETE'
    assert empty['overall']['valid_only_accuracy']['rate'] is None
    assert empty['overall']['valid_only_accuracy']['undefined_reason'] == 'zero_denominator'


def test_fail_is_positive_class_and_defect_and_clean_denominators_are_separate():
    cases = [case('TP', expected='fail'), case('FN', expected='fail'),
             case('FP'), case('TN'), case('MISSING-DEFECT', expected='fail')]
    observations = [observed(cases[0], verdict='fail'), observed(cases[1]),
                    observed(cases[2], verdict='fail'), observed(cases[3])]
    result = summarize(cases, observations)
    totals = result['overall']
    assert totals['confusion'] == {'positive_class': 'fail', 'tp': 1, 'tn': 1, 'fp': 1, 'fn': 1}
    assert totals['all_planned_accuracy']['rate'] == 2 / 5
    assert totals['valid_only_accuracy']['rate'] == 2 / 4
    assert totals['detection_rate']['rate'] == 1 / 3
    assert totals['false_pass_rate']['rate'] == 1 / 3
    assert totals['false_alarm_rate']['rate'] == 1 / 2
    assert result['known_challenge_status'] == 'FAIL'


@pytest.mark.parametrize('status,rate_key', [
    ('error', 'error_rate'), ('timeout', 'timeout_rate'),
    ('invalid_response', 'format_error_rate'),
])
def test_transport_and_format_failures_are_visible_and_do_not_count_as_valid(status, rate_key):
    cases = [case('FAILED'), case('OK')]
    result = summarize(cases, [observed(cases[0], status=status), observed(cases[1])])
    assert result['execution_status'] == 'FAIL'
    assert result['known_challenge_status'] == 'INCOMPLETE'
    assert result['overall'][status] == 1
    assert result['overall'][rate_key]['rate'] == 0.5
    assert result['overall']['valid_responses'] == 1
    assert result['overall']['all_planned_accuracy']['rate'] == 0.5


@pytest.mark.parametrize('mutation', ['wrong_case', 'wrong_quote', 'extra_field', 'missing_field', 'not_object'])
def test_completed_observation_is_revalidated_before_it_can_affect_accuracy(mutation):
    item = case()
    row = observed(item)
    if mutation == 'wrong_case':
        row['response']['case_id'] = 'OTHER'
    elif mutation == 'wrong_quote':
        row['response']['references'][0]['quote'] = '120.30 USD'
    elif mutation == 'extra_field':
        row['response']['confidence'] = 1
    elif mutation == 'missing_field':
        del row['response']['verdict']
    else:
        row['response'] = 'pass'
    result = summarize([item], [row])
    assert result['execution_status'] == 'FAIL'
    assert result['overall']['invalid_response'] == 1
    assert result['overall']['all_planned_accuracy']['numerator'] == 0


@pytest.mark.parametrize('expected', ['pass', 'fail'])
def test_abstention_is_valid_evidence_but_never_a_success_or_false_pass(expected):
    item = case(expected=expected)
    row = observed(item, verdict='abstain')
    row['response']['references'] = []
    result = summarize([item], [row])
    assert result['execution_status'] == 'PASS'
    assert result['known_challenge_status'] == 'FAIL'
    assert result['overall']['valid_responses'] == result['overall']['abstain'] == 1
    assert result['overall']['all_planned_accuracy']['numerator'] == 0
    assert result['overall']['confusion'] == {'positive_class': 'fail', 'tp': 0, 'tn': 0, 'fp': 0, 'fn': 0}


def test_split_and_criterion_metrics_use_only_their_own_planned_rows():
    cases = [case('C-G', expected='fail'), case('H-G', split='heldout'),
             case('C-C', criterion='clarity')]
    cases[1]['family'] = 'source-family-2'
    result = summarize(cases, [observed(cases[0], verdict='fail'), observed(cases[2])])
    assert result['per_split']['calibration']['all_planned_accuracy']['rate'] == 1
    assert result['per_split']['heldout']['missing'] == 1
    assert result['per_criterion']['grounding']['all_planned_accuracy']['rate'] == 0.5
    assert result['per_split_and_criterion']['heldout']['grounding']['planned'] == 1
    assert 'clarity' not in result['per_split_and_criterion']['heldout']
    assert result['observed_family_count'] == 2
    assert result['expected_label_kinds'] == {'synthetic_rule': 3}


@pytest.mark.parametrize('mutation', ['empty_plan', 'duplicate_plan', 'unknown_observation',
                                     'duplicate_observation', 'unknown_status', 'not_list'])
def test_unknown_or_duplicate_slots_cannot_inflate_denominators_or_evidence(mutation):
    cases, rows = [case()], []
    if mutation == 'empty_plan':
        cases = []
    elif mutation == 'duplicate_plan':
        cases.append(deepcopy(cases[0]))
    elif mutation == 'unknown_observation':
        rows = [observed(case('UNKNOWN'))]
    elif mutation == 'duplicate_observation':
        rows = [observed(cases[0]), observed(cases[0])]
    elif mutation == 'unknown_status':
        rows = [observed(cases[0], status='success')]
    else:
        rows = {}
    with pytest.raises(ValueError):
        summarize(cases, rows)


def test_counterbalancing_compares_stable_candidates_not_display_positions():
    cases = pair_cases()
    result = summarize(cases, [observed(cases[0], preference='A'), observed(cases[1], preference='B')])
    pair = result['counterbalanced']
    assert result['known_challenge_status'] == 'PASS'
    assert pair['candidate_agreement']['rate'] == pair['both_correct']['rate'] == 1
    assert pair['same_position_selection']['rate'] == 0
    assert pair['pairs'][0]['selected_candidate_ids'] == ['original', 'original']
    assert result['overall']['pair_preference_accuracy']['rate'] == 1


def test_always_selecting_a_exposes_position_signal_and_one_wrong_preference():
    cases = pair_cases()
    result = summarize(cases, [observed(item, preference='A') for item in cases])
    assert result['known_challenge_status'] == 'FAIL'
    assert result['overall']['pair_preference_accuracy']['rate'] == 0.5
    assert result['counterbalanced']['candidate_agreement']['rate'] == 0
    assert result['counterbalanced']['same_position_selection']['rate'] == 1
    assert result['counterbalanced']['both_correct']['rate'] == 0


def test_agreeing_on_the_wrong_candidate_is_not_accuracy():
    cases = pair_cases()
    result = summarize(cases, [observed(cases[0], preference='B'), observed(cases[1], preference='A')])
    assert result['counterbalanced']['candidate_agreement']['rate'] == 1
    assert result['counterbalanced']['both_correct']['rate'] == 0
    assert result['known_challenge_status'] == 'FAIL'


@pytest.mark.parametrize('second_status', ['missing', 'abstain', 'invalid_response'])
def test_unavailable_pair_member_is_not_stability(second_status):
    cases = pair_cases()
    rows = [observed(cases[0])]
    if second_status == 'abstain':
        rows.append(observed(cases[1], verdict='abstain', preference='abstain'))
    elif second_status == 'invalid_response':
        rows.append(observed(cases[1], status='invalid_response'))
    pair = summarize(cases, rows)['counterbalanced']
    assert pair['planned_pairs'] == 1 and pair['comparable_pairs'] == 0
    assert pair['candidate_agreement']['rate'] is None
    assert pair['all_planned_candidate_agreement']['rate'] == 0
    assert pair['pairs'][0]['candidate_agreement'] is None
    assert pair['pairs'][0]['both_correct'] is None


def test_explicit_ties_are_comparable_but_are_not_position_choices():
    cases = pair_cases()
    for item in cases:
        item['expected_preference'] = 'tie'
    result = summarize(cases, [observed(item, preference='tie') for item in cases])
    assert result['known_challenge_status'] == 'PASS'
    assert result['counterbalanced']['pairs'][0]['selected_candidate_ids'] == ['tie', 'tie']
    assert result['counterbalanced']['same_position_selection']['rate'] == 0


@pytest.mark.parametrize('mutation', ['missing_member', 'unswapped', 'same_candidate', 'no_mapping',
                                     'criterion', 'split', 'opposite_expected_candidate'])
def test_counterbalanced_plan_must_describe_the_same_candidates_and_task(mutation):
    cases = pair_cases()
    if mutation == 'missing_member':
        cases.pop()
    elif mutation == 'unswapped':
        cases[1]['candidate_mapping'] = deepcopy(cases[0]['candidate_mapping'])
    elif mutation == 'same_candidate':
        cases[0]['candidate_mapping'] = {'A': 'original', 'B': 'original'}
    elif mutation == 'no_mapping':
        del cases[0]['candidate_mapping']
    elif mutation == 'criterion':
        cases[1]['payload']['criterion'] = 'clarity'
    elif mutation == 'split':
        cases[1]['split'] = 'heldout'
    else:
        cases[1]['expected_preference'] = 'A'
    with pytest.raises(ValueError):
        summarize(cases, [])


@pytest.mark.parametrize('gate_status', ['FAIL', 'INCOMPLETE'])
def test_judge_pass_never_overrides_the_independent_fact_gate(gate_status):
    item = case(expected='fail')
    item['programmatic_gate'] = {'status': gate_status, 'issues': ['SUM_MISMATCH']}
    before = deepcopy(item)
    result = summarize([item], [observed(item)])
    assert result['overall']['false_pass_rate']['rate'] == 1
    assert result['programmatic_gates']['counts'][gate_status] == 1
    assert result['programmatic_gates']['cases'][item['id']] == before['programmatic_gate']
    assert result['programmatic_gates']['overridden_by_judge'] is False
    assert result['product_acceptance'] == 'INCOMPLETE' and result['advisory'] is True
    assert item == before


def test_human_review_subset_is_valid_but_does_not_fill_omitted_labels():
    cases = [case('ONE'), case('TWO', expected='fail')]
    ratings = validate_human_reviews(human_file([rating(cases[0])]), cases, dataset_sha256=DATASET_SHA)
    assert ratings == [rating(cases[0])]
    human = summarize(cases, [observed(item) for item in cases], human_reviews=ratings)['human_calibration']
    assert human['status'] == 'INCOMPLETE'
    assert human['supplied_reviews'] == human['paired_valid_observations'] == 1
    assert human['review_coverage']['rate'] == human['paired_coverage']['rate'] == 0.5


@pytest.mark.parametrize('field,value', [
    ('schema_version', '2.0'), ('rubric_version', 1), ('dataset_sha256', 'b' * 64),
    ('dataset_sha256', 'not-a-hash'), ('reviewer_id', ' '),
    ('reviewed_at', '2026-10-21T12:30:00'), ('reviewed_at', '2026-02-30T12:30:00Z'),
    ('reviewed_at', None), ('ratings', []),
])
def test_human_envelope_requires_identity_bound_version_hash_and_zoned_date(field, value):
    item = case()
    data = human_file([rating(item)])
    data[field] = value
    with pytest.raises(ValueError):
        validate_human_reviews(data, [item], dataset_sha256=DATASET_SHA)


@pytest.mark.parametrize('mutation', ['extra_envelope', 'missing_envelope', 'duplicate_rating',
                                     'unknown_case', 'criterion', 'reference_id', 'quote',
                                     'extra_rating', 'missing_rating'])
def test_human_ratings_require_exact_schema_and_real_references(mutation):
    item = case()
    data = human_file([rating(item)])
    record = data['ratings'][0]
    if mutation == 'extra_envelope':
        data['approved'] = True
    elif mutation == 'missing_envelope':
        del data['reviewer_id']
    elif mutation == 'duplicate_rating':
        data['ratings'].append(deepcopy(record))
    elif mutation == 'unknown_case':
        record['case_id'] = 'UNKNOWN'
    elif mutation == 'criterion':
        record['criterion'] = 'clarity'
    elif mutation == 'reference_id':
        record['references'][0]['ref_id'] = 'other-source'
    elif mutation == 'quote':
        record['references'][0]['quote'] = '120.30 USD'
    elif mutation == 'extra_rating':
        record['expert_approved'] = True
    else:
        del record['rationale']
    with pytest.raises(ValueError):
        validate_human_reviews(data, [item], dataset_sha256=DATASET_SHA)


def test_human_agreement_and_kappa_come_from_supplied_labels_not_synthetic_oracle():
    cases = [case(str(index)) for index in range(4)]
    verdicts = ['pass', 'pass', 'fail', 'fail']
    human_verdicts = ['pass', 'fail', 'fail', 'fail']
    rows = [observed(item, verdict=label) for item, label in zip(cases, verdicts)]
    ratings = [rating(item, verdict=label) for item, label in zip(cases, human_verdicts)]
    result = summarize(cases, rows, human_reviews=ratings)
    human = result['human_calibration']
    assert human['status'] == 'EVIDENCE_COMPLETE'
    assert human['suitability'] == 'NOT_ESTABLISHED'
    assert human['verdict_agreement']['rate'] == human['exact_agreement']['rate'] == 0.75
    assert human['matrix'] == {'pass': {'pass': 1, 'fail': 1, 'abstain': 0},
                               'fail': {'pass': 0, 'fail': 2, 'abstain': 0},
                               'abstain': {'pass': 0, 'fail': 0, 'abstain': 0}}
    assert human['cohen_kappa']['expected_chance_agreement'] == 0.5
    assert human['cohen_kappa']['value'] == 0.5
    assert result['known_challenge_status'] == 'FAIL'
    assert result['product_acceptance'] == 'INCOMPLETE'


@pytest.mark.parametrize('varying_judge,varying_human', [(False, False), (True, False), (False, True)])
def test_constant_labels_in_either_rater_leave_kappa_unqualified(varying_judge, varying_human):
    cases = [case('ONE'), case('TWO')]
    rows = [observed(cases[0]), observed(cases[1], verdict='fail' if varying_judge else 'pass')]
    ratings = [rating(cases[0]), rating(cases[1], verdict='fail' if varying_human else 'pass')]
    human = summarize(cases, rows, human_reviews=ratings)['human_calibration']
    assert human['status'] == 'EVIDENCE_COMPLETE'
    assert human['cohen_kappa']['value'] is None
    assert human['cohen_kappa']['undefined_reason'] == 'constant_labels_in_at_least_one_rater'


def test_no_human_labels_are_not_invented_from_challenge_expectations():
    item = case()
    human = summarize([item], [observed(item)])['human_calibration']
    assert human['status'] == 'INCOMPLETE'
    assert human['supplied_reviews'] == 0
    assert human['exact_agreement']['rate'] is None
    assert human['cohen_kappa']['undefined_reason'] == 'no_paired_valid_observations'


def test_human_coverage_and_valid_judge_coverage_are_separate():
    cases = [case('ONE'), case('TWO')]
    ratings = [rating(item) for item in cases]
    human = summarize(cases, [observed(cases[0]), observed(cases[1], status='timeout')],
                      human_reviews=ratings)['human_calibration']
    assert human['review_coverage']['rate'] == 1
    assert human['paired_coverage']['rate'] == 0.5
    assert human['status'] == 'INCOMPLETE'


def test_pair_human_agreement_measures_preferences_as_well_as_verdicts():
    cases = pair_cases()
    rows = [observed(cases[0], preference='A'), observed(cases[1], preference='B')]
    ratings = [rating(item, preference='A') for item in cases]
    human = summarize(cases, rows, human_reviews=ratings)['human_calibration']
    assert human['verdict_agreement']['rate'] == 1
    assert human['pair_preference_agreement']['rate'] == human['exact_agreement']['rate'] == 0.5
    assert human['status'] == 'EVIDENCE_COMPLETE'
