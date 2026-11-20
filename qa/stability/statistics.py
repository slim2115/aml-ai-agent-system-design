"""Descriptive fixed-panel statistics; repeated slots are not independent cases.

Family intervals are conditional Wilson calculations, never a population claim
or an acceptance gate. Curated synthetic families do not establish IID sampling.
"""
from collections import Counter
from math import ceil, isfinite, sqrt
from statistics import NormalDist, median
import re


STATUSES = ('PASS', 'FAIL', 'ERROR', 'INCOMPLETE', 'NOT_RUN')
OUTCOMES = ('draft', 'refusal', 'error', 'timeout', 'dependency_failure', 'not_run')
MAX_SECONDS = 31_536_000
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_FIELDS = {'arm', 'case_id', 'repeat_index', 'status', 'outcome',
           'semantic_fingerprint', 'ordered_fingerprint', 'latency_seconds', 'attempts'}
_ATTEMPT_FIELDS = {'status', 'elapsed_seconds', 'prompt_tokens', 'output_tokens'}


def _require(value, code):
    if not value:
        raise ValueError(code)


def _identity(value):
    return type(value) is str and _ID.fullmatch(value) is not None


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _seconds(value):
    return value is None or (type(value) in (int, float) and 0 <= value <= MAX_SECONDS
                             and isfinite(value))


def validate_plan(plan):
    """Validate a finite declared panel without importing data or changing it."""
    _require(type(plan) is dict and set(plan) == {
        'schema_version', 'plan_id', 'evidence_kind', 'arms', 'cases'}, 'STABILITY_PLAN_FIELDS')
    _require(plan['schema_version'] == '1.0' and _identity(plan['plan_id']), 'STABILITY_PLAN_ID')
    _require(plan['evidence_kind'] in ('synthetic_control', 'historical_llm'), 'STABILITY_EVIDENCE_KIND')
    _require(type(plan['arms']) is list and plan['arms'] in (
        ['candidate'], ['baseline', 'candidate']), 'STABILITY_ARMS')
    cases = plan['cases']
    _require(type(cases) is list and 1 <= len(cases) <= 100, 'STABILITY_CASES')
    seen = set()
    for case in cases:
        _require(type(case) is dict and set(case) == {'case_id', 'family_id', 'repeats'},
                 'STABILITY_CASE_FIELDS')
        _require(_identity(case['case_id']) and _identity(case['family_id']), 'STABILITY_CASE_ID')
        _require(case['case_id'] not in seen, 'STABILITY_DUPLICATE_CASE')
        seen.add(case['case_id'])
        _require(_integer(case['repeats'], 1, 20), 'STABILITY_REPEATS')
    _require(sum(case['repeats'] for case in cases) * len(plan['arms']) <= 2000,
             'STABILITY_PANEL_SIZE')
    return plan


def _validate_observations(plan, observations):
    _require(type(observations) is list and len(observations) <= 2000, 'STABILITY_OBSERVATIONS')
    cases = {case['case_id']: case for case in plan['cases']}
    indexed = {}
    for row in observations:
        _require(type(row) is dict and set(row) == _FIELDS, 'STABILITY_OBSERVATION_FIELDS')
        _require(type(row['arm']) is str and row['arm'] in plan['arms']
                 and type(row['case_id']) is str and row['case_id'] in cases, 'STABILITY_UNPLANNED_SLOT')
        _require(_integer(row['repeat_index'], 1, cases[row['case_id']]['repeats']),
                 'STABILITY_UNPLANNED_REPEAT')
        key = (row['arm'], row['case_id'], row['repeat_index'])
        _require(key not in indexed, 'STABILITY_DUPLICATE_SLOT')
        _require(type(row['status']) is str and row['status'] in STATUSES
                 and type(row['outcome']) is str and row['outcome'] in OUTCOMES, 'STABILITY_RESULT')
        _require(_seconds(row['latency_seconds']), 'STABILITY_LATENCY')
        fingerprints = [row['semantic_fingerprint'], row['ordered_fingerprint']]
        _require(all(value is None or (type(value) is str and _HASH.fullmatch(value))
                     for value in fingerprints), 'STABILITY_FINGERPRINT')
        _require(all(value is None for value in fingerprints) or all(value is not None for value in fingerprints),
                 'STABILITY_FINGERPRINT_PAIR')
        _require(row['outcome'] == 'draft' or all(value is None for value in fingerprints),
                 'STABILITY_NON_DRAFT_FINGERPRINT')
        if row['status'] == 'PASS':
            _require(row['outcome'] in ('draft', 'refusal'), 'STABILITY_PASS_OUTCOME')
            _require(row['outcome'] != 'draft' or all(value is not None for value in fingerprints),
                     'STABILITY_DRAFT_FINGERPRINT_REQUIRED')
        _require((row['status'] == 'NOT_RUN') == (row['outcome'] == 'not_run'), 'STABILITY_NOT_RUN_OUTCOME')
        attempts = row['attempts']
        _require(type(attempts) is list and len(attempts) <= 20, 'STABILITY_ATTEMPTS')
        if row['status'] == 'NOT_RUN':
            _require(not attempts and row['latency_seconds'] is None, 'STABILITY_NOT_RUN_TELEMETRY')
        for attempt in attempts:
            _require(type(attempt) is dict and set(attempt) == _ATTEMPT_FIELDS, 'STABILITY_ATTEMPT_FIELDS')
            _require(type(attempt['status']) is str and attempt['status'] in ('completed', 'timeout', 'error'),
                     'STABILITY_ATTEMPT_STATUS')
            _require(_seconds(attempt['elapsed_seconds']), 'STABILITY_ATTEMPT_ELAPSED')
            _require(all(attempt[key] is None or _integer(attempt[key], 0, 10**9)
                         for key in ('prompt_tokens', 'output_tokens')), 'STABILITY_TOKENS')
        _require(row['status'] != 'PASS' or not attempts or attempts[-1]['status'] == 'completed',
                 'STABILITY_PASS_ATTEMPT')
        indexed[key] = row
    return indexed


def _combined(states):
    return next((status for status in ('ERROR', 'FAIL', 'INCOMPLETE') if status in states), 'PASS')


def _status(rows, planned):
    states = [row['status'] for row in rows]
    if len(rows) != planned or 'NOT_RUN' in states:
        states.append('INCOMPLETE')
    return _combined(states)


def _rate(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'value': numerator / denominator if denominator else None}


def wilson(successes, total):
    """Two-sided 95% Wilson interval, conditional on an IID Bernoulli design.

    Formula: NIST/SEMATECH e-Handbook, section 7.2.4.1. Empty samples have no
    interval. This arithmetic does not establish a sampling design for its user.
    """
    _require(_integer(total, 0, 10**9) and _integer(successes, 0, total), 'STABILITY_BINOMIAL_COUNTS')
    if total == 0:
        return None
    z = NormalDist().inv_cdf(0.975)
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half = z * sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return {'method': 'wilson', 'confidence_level': 0.95, 'successes': successes, 'total': total,
            'lower': max(0.0, center - half), 'upper': min(1.0, center + half),
            'interpretation': 'CONDITIONAL_ON_IID_FAMILY_BERNOULLI_DESIGN'}


def _latency(rows, planned):
    values = sorted(row['latency_seconds'] for row in rows if row['latency_seconds'] is not None)
    return {'measured_slots': len(values), 'planned_slots': planned,
            'missing_measurements': planned - len(values),
            'minimum_seconds': values[0] if values else None,
            'maximum_seconds': values[-1] if values else None,
            'median_seconds': median(values) if values else None,
            'p50_seconds': values[ceil(len(values) * 0.5) - 1] if values else None,
            'p95_seconds': values[ceil(len(values) * 0.95) - 1] if values else None,
            'quantile_method': 'nearest_rank', 'scope': 'DESCRIPTIVE_RECORDED_LATENCIES_NOT_SLA'}


def _usage(rows, planned):
    attempts = [attempt for row in rows for attempt in row['attempts']]
    result = {'recorded_attempts': len(attempts), 'observed_slots': len(rows), 'planned_slots': planned,
              'attempt_status_counts': {status: sum(a['status'] == status for a in attempts)
                                        for status in ('completed', 'timeout', 'error')},
              'scope': 'RECORDED_ATTEMPTS_ONLY_UNRECORDED_USAGE_NOT_INFERRED'}
    for key in ('prompt_tokens', 'output_tokens', 'elapsed_seconds'):
        values = [a[key] for a in attempts if a[key] is not None]
        result[key] = {'known_sum': sum(values), 'known_attempts': len(values),
                       'unknown_attempts': len(attempts) - len(values),
                       'recorded_total': sum(values) if len(values) == len(attempts) else None}
    return result


def _case(definition, rows):
    passed = [row for row in rows if row['status'] == 'PASS' and row['outcome'] == 'draft']
    measured = len(passed) >= 2
    return {'case_id': definition['case_id'], 'family_id': definition['family_id'],
            'status': _status(rows, definition['repeats']),
            'planned_slots': definition['repeats'], 'observed_slots': len(rows),
            'missing_slots': definition['repeats'] - len(rows),
            'not_run_slots': sum(row['status'] == 'NOT_RUN' for row in rows),
            'pass_rate': _rate(sum(row['status'] == 'PASS' for row in rows), definition['repeats']),
            'variability': {'status': 'MEASURED' if measured else 'NOT_MEASURED',
                'population': 'PASS_DRAFTS_WITHIN_THIS_CASE_ONLY',
                'eligible_drafts': len(passed), 'planned_slots': definition['repeats'],
                'excluded_observations': len(rows) - len(passed),
                'distinct_semantic_reports': len({r['semantic_fingerprint'] for r in passed}) if measured else None,
                'distinct_ordered_reports': len({r['ordered_fingerprint'] for r in passed}) if measured else None}}


def _arm(plan, name, indexed):
    rows = [row for key, row in indexed.items() if key[0] == name]
    planned = sum(case['repeats'] for case in plan['cases'])
    cases = [_case(case, [row for row in rows if row['case_id'] == case['case_id']]) for case in plan['cases']]
    families = []
    for family in dict.fromkeys(case['family_id'] for case in plan['cases']):
        members = [case for case in cases if case['family_id'] == family]
        families.append({'family_id': family, 'status': _combined([case['status'] for case in members]),
                         'case_count': len(members), 'planned_slots': sum(case['planned_slots'] for case in members),
                         'passed_all_slots': all(case['status'] == 'PASS' for case in members)})
    success = sum(family['passed_all_slots'] for family in families)
    complete_panel = len(rows) == planned and all(row['status'] in ('PASS', 'FAIL') for row in rows)
    return {'status': _status(rows, planned), 'planned_slots': planned, 'observed_slots': len(rows),
            'missing_slots': planned - len(rows), 'not_run_slots': sum(row['status'] == 'NOT_RUN' for row in rows),
            'status_counts': {status: sum(row['status'] == status for row in rows) for status in STATUSES},
            'outcome_counts': {outcome: sum(row['outcome'] == outcome for row in rows) for outcome in OUTCOMES},
            'pass_rate': _rate(sum(row['status'] == 'PASS' for row in rows), planned),
            'cases': cases, 'families': families, 'planned_family_count': len(families),
            'family_pass_rate': _rate(success, len(families)),
            'conditional_family_interval': wilson(success, len(families)) if complete_panel else None,
            'interval_status': 'ASSUMPTION_DEPENDENT' if complete_panel else 'UNAVAILABLE_INCOMPLETE_MEASUREMENT',
            'latency': _latency(rows, planned), 'usage': _usage(rows, planned)}


def _paired(plan, indexed):
    if plan['arms'] != ['baseline', 'candidate']:
        return {'status': 'NOT_APPLICABLE', 'reason': 'NO_BASELINE_ARM'}
    pairs = []
    for case in plan['cases']:
        for index in range(1, case['repeats'] + 1):
            base = indexed.get(('baseline', case['case_id'], index))
            candidate = indexed.get(('candidate', case['case_id'], index))
            complete = all(row is not None and row['status'] in ('PASS', 'FAIL') for row in (base, candidate))
            pairs.append({'case_id': case['case_id'], 'family_id': case['family_id'], 'repeat_index': index,
                          'complete': complete,
                          'baseline_status': base['status'] if base else 'MISSING',
                          'candidate_status': candidate['status'] if candidate else 'MISSING',
                          'delta': int(candidate['status'] == 'PASS') - int(base['status'] == 'PASS') if complete else None})
    known = [pair['delta'] for pair in pairs if pair['complete']]
    families = []
    for family in dict.fromkeys(case['family_id'] for case in plan['cases']):
        members = [pair for pair in pairs if pair['family_id'] == family]
        complete = all(pair['complete'] for pair in members)
        families.append({'family_id': family, 'planned_pairs': len(members),
                         'complete_pairs': sum(pair['complete'] for pair in members),
                         'delta': sum(pair['delta'] for pair in members) / len(members) if complete else None})
    complete = len(known) == len(pairs)
    return {'status': 'COMPLETE' if complete else 'INCOMPLETE', 'planned_pairs': len(pairs),
            'complete_pairs': len(known), 'incomplete_pairs': len(pairs) - len(known),
            'complete_pair_rate': _rate(len(known), len(pairs)),
            'paired_pass_delta': sum(known) / len(pairs) if complete else None,
            'known_pair_contribution': _rate(sum(known), len(pairs)),
            'mean_family_delta': sum(family['delta'] for family in families) / len(families) if complete else None,
            'pairs': pairs, 'families': families, 'superiority': 'NOT_ESTABLISHED',
            'scope': 'FIXED_MATCHED_SLOTS_DESCRIPTIVE_ONLY_NO_SELECTION_OF_COMPLETE_SUBSET'}


def summarize(plan, observations):
    """Describe a predeclared panel without discarding missing/error slots."""
    validate_plan(plan)
    indexed = _validate_observations(plan, observations)
    arms = {name: _arm(plan, name, indexed) for name in plan['arms']}
    planned = sum(arm['planned_slots'] for arm in arms.values())
    return {'schema_version': '1.0', 'plan_id': plan['plan_id'], 'evidence_kind': plan['evidence_kind'],
            'status': _combined([arm['status'] for arm in arms.values()]),
            'planned_slots': planned, 'observed_slots': len(indexed), 'missing_slots': planned - len(indexed),
            'not_run_slots': sum(arm['not_run_slots'] for arm in arms.values()),
            'arms': arms, 'paired': _paired(plan, indexed),
            'population_inference': 'NOT_ESTABLISHED', 'product_acceptance': 'INCOMPLETE',
            'sampling_scope': 'CURATED_SYNTHETIC_CASES_REPEATS_ARE_NOT_INDEPENDENT_SAMPLES',
            'interval_assumptions': 'Each predeclared family would need an independent identically distributed '
                'Bernoulli outcome for generalization; this calculation does not establish those assumptions.',
            'budget_enforcement': 'NOT_EVALUATED'}
