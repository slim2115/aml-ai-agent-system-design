"""Descriptive, advisory Judge metrics with fixed planned denominators.

``fail`` is the positive class: a true positive is a detected known defect.
Synthetic labels are mutation expectations, not human AML judgements. Repeated
or counterbalanced examples do not establish independent observations.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import math
import re


_VERDICTS = ('pass', 'fail', 'abstain')
_STATUSES = ('completed', 'error', 'timeout', 'invalid_response')
_RATING_KEYS = {'case_id', 'criterion', 'verdict', 'preference', 'references', 'rationale'}


def _ratio(numerator, denominator):
    return {
        'numerator': numerator,
        'denominator': denominator,
        'rate': numerator / denominator if denominator else None,
        'undefined_reason': None if denominator else 'zero_denominator',
    }


def _index_cases(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError('A nonempty planned case list is required')
    result = {}
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get('id'), str) or not case['id']:
            raise ValueError('Every case requires a nonempty id')
        if case['id'] in result:
            raise ValueError('Duplicate planned case id')
        if case.get('expected_verdict') not in ('pass', 'fail'):
            raise ValueError('Known challenge expected verdict must be pass or fail')
        if case.get('expected_preference') not in ('not_applicable', 'A', 'B', 'tie'):
            raise ValueError('Invalid expected preference')
        payload = case.get('payload', {})
        if payload.get('mode') not in ('single', 'pair') or not payload.get('criterion'):
            raise ValueError('Every case requires a mode and criterion')
        if payload.get('case_id') != case['id']:
            raise ValueError('Payload and case ids must match')
        if payload['mode'] == 'single' and case['expected_preference'] != 'not_applicable':
            raise ValueError('Single case preference must be not_applicable')
        if payload['mode'] == 'pair':
            mapping = case.get('candidate_mapping')
            if (not isinstance(mapping, dict) or set(mapping) != {'A', 'B'}
                    or any(not isinstance(value, str) or not value for value in mapping.values())
                    or len(set(mapping.values())) != 2):
                raise ValueError('Pair case requires distinct stable A/B candidate mapping')
            if (not isinstance(case.get('pair_id'), str) or not case['pair_id']
                    or case['expected_verdict'] != 'pass'
                    or case['expected_preference'] not in ('A', 'B', 'tie')):
                raise ValueError('Pair case requires pair_id and a valid expected comparison')
        for key in ('split', 'family', 'expected_kind'):
            if not isinstance(case.get(key), str) or not case[key]:
                raise ValueError(f'Every case requires {key}')
        gate = case.get('programmatic_gate')
        if (not isinstance(gate, dict) or gate.get('status') not in ('PASS', 'FAIL', 'INCOMPLETE')
                or not isinstance(gate.get('issues'), list)):
            raise ValueError('Every case requires an independent programmatic gate')
        result[case['id']] = case
    return result


def _validated_response(response, case):
    # Shared contract also verifies each reference quote against the supplied
    # evidence. No model, transport, clock, filesystem or network is used here.
    from qa.judge.contracts import validate_response
    if not isinstance(response, dict):
        raise ValueError('Response must be an object')
    return validate_response(json.dumps(response, ensure_ascii=False, allow_nan=False), case['payload'])


def validate_human_reviews(data, cases, *, dataset_sha256):
    """Validate an actual review file, returning only explicitly supplied ratings.

    Review files may cover a subset; absent labels are never filled from the
    challenge expectations. Hash, version and evidence mismatches are errors.
    """
    indexed = _index_cases(cases)
    required = {'schema_version', 'rubric_version', 'dataset_sha256',
                'reviewer_id', 'reviewed_at', 'ratings'}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError('Human review envelope fields do not match the contract')
    if data['schema_version'] != '1.0' or data['rubric_version'] != '1.0':
        raise ValueError('Unsupported human review schema or rubric version')
    for value in (data['dataset_sha256'], dataset_sha256):
        if not isinstance(value, str) or re.fullmatch(r'[0-9a-fA-F]{64}', value) is None:
            raise ValueError('Dataset hash must be a SHA-256 hex digest')
    if data['dataset_sha256'].lower() != dataset_sha256.lower():
        raise ValueError('Human review dataset hash does not match')
    if not isinstance(data['reviewer_id'], str) or not data['reviewer_id'].strip():
        raise ValueError('An explicit reviewer id is required')
    try:
        reviewed_at = datetime.fromisoformat(data['reviewed_at'])
        if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
            raise ValueError('Timezone is required')
    except (ValueError, TypeError) as exc:
        raise ValueError('reviewed_at must be an ISO timestamp with timezone') from exc
    return _validate_ratings(data['ratings'], indexed)


def _validate_ratings(ratings, indexed):
    if not isinstance(ratings, list) or not ratings:
        raise ValueError('Human ratings must be a nonempty list')
    normalized = []
    seen = set()
    for rating in ratings:
        if not isinstance(rating, dict) or set(rating) != _RATING_KEYS:
            raise ValueError('Human rating fields do not match the contract')
        case_id = rating['case_id']
        if not isinstance(case_id, str) or case_id not in indexed or case_id in seen:
            raise ValueError('Unknown or duplicate human rating case id')
        response = _validated_response({
            'schema_version': '1.0', 'rubric_version': '1.0', **rating,
        }, indexed[case_id])
        normalized.append({key: response[key] for key in _RATING_KEYS})
        seen.add(case_id)
    return normalized


def _rows(cases, observations):
    indexed = _index_cases(cases)
    if not isinstance(observations, list):
        raise ValueError('Observations must be a list')
    by_case = {}
    for observation in observations:
        if not isinstance(observation, dict):
            raise ValueError('Observation must be an object')
        case_id = observation.get('case_id')
        if not isinstance(case_id, str) or case_id not in indexed or case_id in by_case:
            raise ValueError('Unknown or duplicate observed case id')
        if observation.get('status') not in _STATUSES:
            raise ValueError('Unknown observation status')
        by_case[case_id] = observation
    result = []
    for case_id, case in indexed.items():
        observation = by_case.get(case_id)
        status = observation['status'] if observation else 'missing'
        response = None
        if status == 'completed':
            try:
                response = _validated_response(observation.get('response'), case)
            except (TypeError, ValueError, KeyError):
                status = 'invalid_response'
        result.append({'case': case, 'status': status, 'response': response})
    return result


def _stats(rows):
    counts = Counter(row['status'] for row in rows)
    planned = len(rows)
    valid = [row for row in rows if row['status'] == 'completed']
    tp = tn = fp = fn = correct = preference_correct = abstain = 0
    planned_pairs = 0
    for row in rows:
        case, response = row['case'], row['response']
        planned_pairs += case['payload']['mode'] == 'pair'
        if response is None:
            continue
        expected, actual = case['expected_verdict'], response['verdict']
        pref_match = response['preference'] == case['expected_preference']
        correct += actual == expected and pref_match
        preference_correct += case['payload']['mode'] == 'pair' and pref_match
        abstain += actual == 'abstain'
        tp += expected == 'fail' and actual == 'fail'
        tn += expected == 'pass' and actual == 'pass'
        fp += expected == 'pass' and actual == 'fail'
        fn += expected == 'fail' and actual == 'pass'
    expected_fail = sum(row['case']['expected_verdict'] == 'fail' for row in rows)
    expected_pass = planned - expected_fail
    return {
        'planned': planned, 'valid_responses': len(valid),
        'completed': counts['completed'], 'missing': counts['missing'],
        'error': counts['error'], 'timeout': counts['timeout'],
        'invalid_response': counts['invalid_response'], 'abstain': abstain,
        'known_expected_pass': expected_pass, 'known_expected_fail': expected_fail,
        'confusion': {'positive_class': 'fail', 'tp': tp, 'tn': tn, 'fp': fp, 'fn': fn},
        'all_planned_accuracy': _ratio(correct, planned),
        'valid_only_accuracy': _ratio(correct, len(valid)),
        'detection_rate': _ratio(tp, expected_fail),
        'false_pass_rate': _ratio(fn, expected_fail),
        'false_alarm_rate': _ratio(fp, expected_pass),
        'pair_preference_accuracy': _ratio(preference_correct, planned_pairs),
        'valid_response_coverage': _ratio(len(valid), planned),
        'abstention_rate': _ratio(abstain, planned),
        'missing_rate': _ratio(counts['missing'], planned),
        'format_error_rate': _ratio(counts['invalid_response'], planned),
        'error_rate': _ratio(counts['error'], planned),
        'timeout_rate': _ratio(counts['timeout'], planned),
    }


def _selected_candidate(row):
    response = row['response']
    if response is None or response['verdict'] == 'abstain':
        return None
    preference = response['preference']
    return 'tie' if preference == 'tie' else row['case']['candidate_mapping'][preference]


def _counterbalanced(rows):
    grouped = {}
    for row in rows:
        if row['case']['payload']['mode'] == 'pair':
            grouped.setdefault(row['case']['pair_id'], []).append(row)
    records = []
    for pair_id, pair in sorted(grouped.items()):
        if len(pair) != 2:
            raise ValueError('Each counterbalanced pair requires exactly two planned cases')
        first, second = pair
        left, right = first['case']['candidate_mapping'], second['case']['candidate_mapping']
        if left['A'] != right['B'] or left['B'] != right['A']:
            raise ValueError('Counterbalanced candidate mappings must be swapped')
        if (first['case']['payload']['criterion'] != second['case']['payload']['criterion']
                or first['case']['split'] != second['case']['split']):
            raise ValueError('Counterbalanced cases must share criterion and split')
        expected_choices = [
            'tie' if row['case']['expected_preference'] == 'tie'
            else row['case']['candidate_mapping'][row['case']['expected_preference']]
            for row in pair
        ]
        if len(set(expected_choices)) != 1:
            raise ValueError('Counterbalanced expected preferences must name the same candidate')
        choices = [_selected_candidate(row) for row in pair]
        comparable = all(choice is not None for choice in choices)
        agreement = choices[0] == choices[1] if comparable else None
        correct = all(choice == expected_choices[0] for choice in choices) if comparable else None
        same_position = (all(row['response']['preference'] in ('A', 'B') for row in pair)
                         and pair[0]['response']['preference'] == pair[1]['response']['preference']) if comparable else None
        records.append({
            'pair_id': pair_id, 'case_ids': [row['case']['id'] for row in pair],
            'selected_candidate_ids': choices, 'comparable': comparable,
            'candidate_agreement': agreement, 'both_correct': correct,
            'same_position_selected': same_position,
        })
    comparable = sum(record['comparable'] for record in records)
    agreement = sum(record['candidate_agreement'] is True for record in records)
    return {
        'planned_pairs': len(records), 'comparable_pairs': comparable,
        'coverage': _ratio(comparable, len(records)),
        'candidate_agreement': _ratio(agreement, comparable),
        'all_planned_candidate_agreement': _ratio(agreement, len(records)),
        'both_correct': _ratio(sum(record['both_correct'] is True for record in records), len(records)),
        'same_position_selection': _ratio(sum(record['same_position_selected'] is True for record in records), comparable),
        'pairs': records,
        'interpretation': 'Same-position selection is a descriptive signal, not a statistical proof of bias.',
    }


def _human_agreement(rows, human_reviews):
    planned = len(rows)
    ratings = [] if human_reviews is None else _validate_ratings(
        human_reviews, {row['case']['id']: row['case'] for row in rows})
    by_case = {rating['case_id']: rating for rating in ratings}
    paired = [row for row in rows if row['status'] == 'completed' and row['case']['id'] in by_case]
    matrix = {judge: {human: 0 for human in _VERDICTS} for judge in _VERDICTS}
    exact = verdict_exact = preference_exact = pair_count = 0
    for row in paired:
        response, rating = row['response'], by_case[row['case']['id']]
        matrix[response['verdict']][rating['verdict']] += 1
        verdict_match = response['verdict'] == rating['verdict']
        preference_match = response['preference'] == rating['preference']
        verdict_exact += verdict_match
        exact += verdict_match and preference_match
        if row['case']['payload']['mode'] == 'pair':
            pair_count += 1
            preference_exact += preference_match
    count = len(paired)
    judge_marginals = {label: sum(matrix[label].values()) for label in _VERDICTS}
    human_marginals = {label: sum(matrix[judge][label] for judge in _VERDICTS) for label in _VERDICTS}
    expected = sum(judge_marginals[label] * human_marginals[label] for label in _VERDICTS) / count ** 2 if count else None
    reason = None
    if not count:
        reason = 'no_paired_valid_observations'
    elif sum(value > 0 for value in judge_marginals.values()) < 2 or sum(value > 0 for value in human_marginals.values()) < 2:
        reason = 'constant_labels_in_at_least_one_rater'
    elif math.isclose(expected, 1.0):
        reason = 'chance_agreement_is_one'
    kappa = (verdict_exact / count - expected) / (1 - expected) if reason is None else None
    return {
        'status': 'EVIDENCE_COMPLETE' if count == planned else 'INCOMPLETE',
        'suitability': 'NOT_ESTABLISHED',
        'supplied_reviews': len(ratings), 'paired_valid_observations': count,
        'review_coverage': _ratio(len(ratings), planned), 'paired_coverage': _ratio(count, planned),
        'matrix': matrix, 'matrix_axes': {'rows': 'judge_verdict', 'columns': 'human_verdict'},
        'exact_agreement': _ratio(exact, count),
        'verdict_agreement': _ratio(verdict_exact, count),
        'pair_preference_agreement': _ratio(preference_exact, pair_count),
        'cohen_kappa': {'value': kappa, 'paired_denominator': count,
                        'expected_chance_agreement': expected, 'undefined_reason': reason,
                        'scope': 'verdict_only; descriptive; constant-label sets are not qualified'},
        'interpretation': 'Coverage and agreement do not establish SME validity or production acceptance.',
    }


def summarize(cases, observations, *, human_reviews=None):
    """Summarize a planned matrix without treating missing observations as passes."""
    rows = _rows(cases, observations)
    totals = _stats(rows)
    incorrect = totals['valid_responses'] - totals['all_planned_accuracy']['numerator']
    if totals['error'] + totals['timeout'] + totals['invalid_response']:
        execution = 'FAIL'
    elif totals['missing']:
        execution = 'INCOMPLETE'
    else:
        execution = 'PASS'
    known = 'FAIL' if incorrect else 'PASS' if totals['valid_responses'] == len(rows) else 'INCOMPLETE'
    split_values = sorted({row['case']['split'] for row in rows})
    criteria = sorted({row['case']['payload']['criterion'] for row in rows})
    gates = Counter(row['case']['programmatic_gate']['status'] for row in rows)
    return {
        'schema_version': '1.0', 'advisory': True,
        'execution_status': execution, 'known_challenge_status': known,
        'product_acceptance': 'INCOMPLETE',
        'overall': totals,
        'per_split': {value: _stats([row for row in rows if row['case']['split'] == value]) for value in split_values},
        'per_criterion': {value: _stats([row for row in rows if row['case']['payload']['criterion'] == value]) for value in criteria},
        'per_split_and_criterion': {
            split: {criterion: _stats([row for row in rows if row['case']['split'] == split
                                      and row['case']['payload']['criterion'] == criterion])
                    for criterion in criteria if any(row['case']['split'] == split and row['case']['payload']['criterion'] == criterion for row in rows)}
            for split in split_values
        },
        'programmatic_gates': {'counts': {status: gates[status] for status in ('PASS', 'FAIL', 'INCOMPLETE')},
                               'overridden_by_judge': False,
                               'cases': {row['case']['id']: row['case']['programmatic_gate'] for row in rows}},
        'counterbalanced': _counterbalanced(rows),
        'human_calibration': _human_agreement(rows, human_reviews),
        'expected_label_kinds': dict(sorted(Counter(row['case']['expected_kind'] for row in rows).items())),
        'observed_family_count': len({row['case']['family'] for row in rows}),
        'limitations': ['Known synthetic labels do not calibrate a Judge against humans.',
                        'No inference of independence, statistical confidence, or model superiority.',
                        'Judge verdicts cannot override deterministic programmatic gates.'],
    }
