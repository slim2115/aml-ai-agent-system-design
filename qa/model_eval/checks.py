"""Pure repeated-run checks against frozen, independently calculated facts.

Repetitions are observations of the same synthetic case, never new independent
cases. Fingerprints describe report content/order, not open-ended linguistic
quality, model reasoning or expert acceptance.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from hashlib import sha256
import math

from qa.scenario_checks import canonical, check_observation


OUTCOMES = ('draft', 'refusal', 'error', 'timeout', 'dependency_failure')
_TRANSPORT_OUTCOMES = ('completed', 'timeout', 'dependency_failure', 'error')
_FACT_ISSUES = frozenset({
    'RESULT_CONTRACT_INVALID', 'EVIDENCE_CONTEXT_INVALID', 'FACT_COUNT_MISMATCH',
    'FACT_CATALOG_MISMATCH', 'RULE_CATALOG_MISMATCH', 'UNSUPPORTED_MODEL_CLAIM',
    'UNEXPECTED_DRAFT', 'CONTEXT_MISMATCH', 'SELECTED_RULES_MISMATCH',
    'FORBIDDEN_OUTPUT', 'UNTRUSTED_TEXT_IN_MODEL_PAYLOAD',
})


def _digest(value):
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def _normalized_fact(value):
    """Ignore collection order, retaining every source value and timestamp."""
    fact = deepcopy(value)
    for item in fact.get('evidence', []):
        if 'tx_ids' in item:
            item['tx_ids'] = sorted(item['tx_ids'])
    fact['evidence'] = sorted(fact.get('evidence', []), key=canonical)
    fact['sources'] = sorted(fact.get('sources', []), key=canonical)
    return fact


def _fingerprints(result):
    report = deepcopy(result)
    # Only these two generated envelope fields are nondeterministic metadata.
    # Incident/snapshot IDs, transaction times and source versions stay intact.
    report.pop('report_id', None)
    report.pop('generated_at', None)
    report['found_facts'] = [_normalized_fact(item) for item in report['found_facts']]
    ordered = _digest(report)
    fact_order = [_digest(item) for item in report['found_facts']]
    rule_order = [deepcopy(item['citation']) for item in report['applicable_rules']]
    report['found_facts'] = sorted(report['found_facts'], key=canonical)
    report['applicable_rules'] = sorted(report['applicable_rules'], key=canonical)
    return {
        'ordered_fingerprint': ordered,
        'semantic_fingerprint': _digest(report),
        'fact_order': fact_order,
        'rule_order': rule_order,
    }


def _model_call_issues(scenario, observed):
    calls = len(observed.model_prompts)
    if scenario.engine != 'local-model' or scenario.expected_model_calls == 0:
        return [] if calls == 0 else ['MODEL_CALL_COUNT']
    # Live positive runs may repair one rejected response. The frozen fixture's
    # synthetic transport call count is not a constraint on real model quality.
    allowed = (1, 2) if scenario.case.expected_status == 'draft' else (scenario.expected_model_calls,)
    issues = [] if calls in allowed else ['MODEL_CALL_COUNT']
    starts = [event.get('attempt') for event in observed.events
              if event.get('stage') == 'generation' and event.get('code') == 'STARTED']
    retries = [event.get('attempt') for event in observed.events
               if event.get('stage') == 'validation' and event.get('code') == 'RETRY']
    if starts != list(range(1, calls + 1)):
        issues.append('MODEL_ATTEMPT_EVENTS_MISMATCH')
    if retries != ([1] if calls == 2 else []):
        issues.append('MODEL_RETRY_EVENTS_MISMATCH')
    return issues


def check_repeat(scenario, observation, *, repeat_index, transport_outcome='completed'):
    """Check one run, keeping transport outcomes separate from report statuses.

    ``scenario`` describes the requested engine. A live runner may use a model
    copy with engine=local-model and expected_model_calls=1; frozen files remain
    unchanged. A template comparison uses engine=template and zero calls.
    Missing observations require an explicit transport failure.
    """
    if type(repeat_index) is not int or repeat_index < 1:
        raise ValueError('INVALID_REPEAT_INDEX')
    if transport_outcome not in _TRANSPORT_OUTCOMES:
        raise ValueError('INVALID_TRANSPORT_OUTCOME')
    base = {
        'case_id': scenario.case.id,
        'independence_id': scenario.case.independence_id,
        'repeat_index': repeat_index,
        'expected_status': scenario.case.expected_status,
        'engine': scenario.engine,
        'transport_outcome': transport_outcome,
        'draft': None,
    }
    if observation is None:
        if transport_outcome == 'completed':
            raise ValueError('COMPLETED_RUN_REQUIRES_OBSERVATION')
        return {
            **base, 'outcome': transport_outcome,
            'status': 'FAIL' if transport_outcome == 'error' else 'INCOMPLETE',
            'issues': ['TRANSPORT_' + transport_outcome.upper()],
            'fact_issues': [], 'model_calls': 0, 'latency_seconds': None,
        }
    if transport_outcome != 'completed':
        raise ValueError('FAILED_TRANSPORT_CANNOT_HAVE_OBSERVATION')
    if not isinstance(observation.result, dict):
        raise ValueError('OBSERVATION_RESULT_MUST_BE_OBJECT')
    if (type(observation.latency_seconds) not in (int, float)
            or not math.isfinite(observation.latency_seconds)
            or observation.latency_seconds < 0):
        raise ValueError('INVALID_LATENCY')

    issues = check_observation(scenario, observation)
    # Only call-count handling is replaced. Never suppress a factual, safety,
    # source-integrity or behavioral failure from the independent oracle.
    issues = [issue for issue in issues if issue != 'MODEL_CALL_COUNT']
    issues.extend(_model_call_issues(scenario, observation))
    result = observation.result
    outcome = result.get('status')
    if outcome not in ('draft', 'refusal', 'error'):
        outcome = 'error'
    elif outcome == 'error':
        if result.get('code') == 'DEADLINE_EXCEEDED':
            outcome = 'timeout'
        elif result.get('code') in ('DEPENDENCY_UNAVAILABLE', 'NOT_READY'):
            outcome = 'dependency_failure'
    if outcome == 'draft' and 'RESULT_CONTRACT_INVALID' not in issues:
        base['draft'] = _fingerprints(result)
    return {
        **base, 'outcome': outcome, 'status': 'FAIL' if issues else 'PASS',
        'issues': list(dict.fromkeys(issues)),
        'fact_issues': sorted(set(issues) & _FACT_ISSUES),
        'model_calls': len(observation.model_prompts),
        'latency_seconds': observation.latency_seconds,
    }


def _metric(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'value': numerator / denominator if denominator else None}


def _summarize_case(case_id, rows, repeats):
    present = {row['repeat_index'] for row in rows}
    missing = [index for index in range(1, repeats + 1) if index not in present]
    outcomes = {name: sum(row['outcome'] == name for row in rows) for name in OUTCOMES}
    statuses = {name: sum(row['status'] == name for row in rows)
                for name in ('PASS', 'FAIL', 'INCOMPLETE')}
    accepted = [row['draft'] for row in rows if row['status'] == 'PASS' and row['draft'] is not None]
    eligible = len(accepted)
    status = ('FAIL' if statuses['FAIL'] else
              'INCOMPLETE' if missing or statuses['INCOMPLETE'] else 'PASS')
    return {
        'case_id': case_id,
        'independence_id': rows[0]['independence_id'] if rows else None,
        'status': status,
        'planned_repeats': repeats,
        'observed_repeats': len(rows),
        'missing_repeat_indices': missing,
        'outcome_counts': outcomes,
        'status_counts': statuses,
        'checks_pass_rate': _metric(statuses['PASS'], repeats),
        'draft_rate': _metric(outcomes['draft'], repeats),
        'validated_draft_rate': _metric(eligible, repeats),
        'fact_violation_count': sum(bool(row['fact_issues']) for row in rows),
        'variability': {
            'status': 'evaluated' if eligible >= 2 else 'not_measured',
            'population': 'drafts_passing_all_checks',
            'eligible_draft_count': eligible,
            'excluded_observation_count': len(rows) - eligible,
            'distinct_ordered_reports': len({row['ordered_fingerprint'] for row in accepted}) if eligible >= 2 else None,
            'distinct_semantic_reports': len({row['semantic_fingerprint'] for row in accepted}) if eligible >= 2 else None,
            'distinct_fact_orders': len({canonical(row['fact_order']) for row in accepted}) if eligible >= 2 else None,
            'distinct_rule_orders': len({canonical(row['rule_order']) for row in accepted}) if eligible >= 2 else None,
        },
    }


def summarize_repeats(case_rows, *, planned_case_ids, repeats_per_case):
    """Aggregate by scenario with fixed planned-slot denominators.

    A failed check takes precedence over incompleteness; missing repetitions can
    never pass. Variability is descriptive within a scenario and requires two
    validated drafts. Failure/refusal-only runs cannot imply stable answers.
    """
    if type(repeats_per_case) is not int or repeats_per_case < 1:
        raise ValueError('INVALID_REPEATS_PER_CASE')
    planned = list(planned_case_ids)
    if (not planned or any(not isinstance(case, str) or not case for case in planned)
            or len(planned) != len(set(planned))):
        raise ValueError('INVALID_PLANNED_CASES')
    groups = {case: [] for case in planned}
    seen = set()
    for row in case_rows:
        if not isinstance(row, dict) or row.get('case_id') not in groups:
            raise ValueError('UNPLANNED_CASE')
        index = row.get('repeat_index')
        if type(index) is not int or not 1 <= index <= repeats_per_case:
            raise ValueError('UNPLANNED_REPEAT')
        identity = (row['case_id'], index)
        if identity in seen:
            raise ValueError('DUPLICATE_REPEAT')
        seen.add(identity)
        if row.get('status') not in ('PASS', 'FAIL', 'INCOMPLETE') or row.get('outcome') not in OUTCOMES:
            raise ValueError('INVALID_REPEAT_RESULT')
        if not isinstance(row.get('independence_id'), str) or not row['independence_id']:
            raise ValueError('MISSING_INDEPENDENCE_ID')
        previous = groups[row['case_id']]
        if previous and row['independence_id'] != previous[0]['independence_id']:
            raise ValueError('INCONSISTENT_INDEPENDENCE_ID')
        previous.append(row)
    cases = [_summarize_case(case, groups[case], repeats_per_case) for case in planned]
    statuses = Counter(case['status'] for case in cases)
    planned_slots = len(planned) * repeats_per_case
    observed = sum(case['observed_repeats'] for case in cases)
    passed = sum(case['status_counts']['PASS'] for case in cases)
    return {
        'status': 'FAIL' if statuses['FAIL'] else 'INCOMPLETE' if statuses['INCOMPLETE'] else 'PASS',
        'planned_case_count': len(planned),
        'observed_case_count': sum(bool(groups[case]) for case in planned),
        'observed_independence_family_count': len({row['independence_id'] for rows in groups.values() for row in rows}),
        'planned_repeat_count': planned_slots,
        'observed_repeat_count': observed,
        'missing_repeat_count': planned_slots - observed,
        'repeat_checks_pass_rate': _metric(passed, planned_slots),
        'case_checks_pass_rate': _metric(statuses['PASS'], len(planned)),
        'outcome_counts': {name: sum(case['outcome_counts'][name] for case in cases) for name in OUTCOMES},
        'fact_violation_count': sum(case['fact_violation_count'] for case in cases),
        'independence_note': 'Repetitions do not increase the number of independent cases; no confidence interval is inferred.',
        'cases': cases,
    }
