"""Pure replay evaluation. Reference checks are mechanical, never AML truth claims."""
from collections import Counter, defaultdict
import json
from hashlib import sha256
from math import ceil
from time import perf_counter

from pydantic import ValidationError

from src.contracts.semantics import ContractViolation, canonical_digest, validate_report_context
from src.contracts.v1 import ADAPTERS, DraftReport
from .gates import (ACCEPTANCE, EVIDENCE, FAITHFULNESS, LATENCY_MAX, LATENCY_P95,
                    PRECISION, RECALL, REFUSAL, RELEVANCE, SCHEMA, TRACEABILITY,
                    gate, quality_gates)
from .models import BundleInputError, ReplayBundle


def ratio(numerator=0, denominator=0, *, reason='NO_ELIGIBLE_OBSERVATIONS'):
    return {'status': 'measured' if denominator else 'not_measured',
            'numerator': numerator, 'denominator': denominator,
            'value': numerator / denominator if denominator else None,
            'reason': None if denominator else reason}


def _strings(value):
    """Inspect decoded strings so JSON escaping cannot hide forbidden text."""
    if isinstance(value, str):
        yield value.casefold()
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key.casefold()
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def run_cases(cases, runner):
    """Call runner(case) -> {result, retrieval?}; preserve each exception as a safe outcome.

    This function itself performs no app/model/DB calls. The caller owns its runner.
    Elapsed time covers only the runner call, not setup or evaluator work.
    """
    outcomes = []
    for case in cases:
        start = perf_counter()
        outcome = {'case_id': case['id'], 'incident_id': case['incident_id']}
        try:
            observed = runner(case)
            if not isinstance(observed, dict) or 'result' not in observed or set(observed) - {'result', 'retrieval'}:
                raise ValueError('INVALID_RUNNER_ENVELOPE')
            outcome.update(observed)
        except Exception:
            # Never serialize an exception, traceback, class name or its source payload.
            outcome.update(result=None, failure_code='RUNNER_EXCEPTION')
        outcome['latency_seconds'] = perf_counter() - start
        outcomes.append(outcome)
    return outcomes


def evaluate_bundle(bundle, settings=None):
    """Evaluate a strict replay 1.0 dictionary without mutating it or performing I/O."""
    try:
        # JSON compatibility rejects NaN, non-string dictionary keys and model objects.
        serialized = json.dumps(bundle, allow_nan=False)
        if json.loads(serialized) != bundle:
            raise ValueError('non-JSON input')
        replay = ReplayBundle.model_validate(bundle)
    except (ValidationError, ValueError, TypeError, OverflowError, RecursionError):
        raise BundleInputError('INVALID_REPLAY_BUNDLE') from None

    counts = defaultdict(lambda: [0, 0])
    outcomes = defaultdict(list)
    parsed_results = {}
    for index, outcome in enumerate(replay.outcomes):
        outcomes[outcome.case_id].append((index, outcome))
        try:
            parsed_results[index] = ADAPTERS['InvestigationResult'].validate_python(outcome.result)
        except (ValidationError, TypeError, ValueError):
            parsed_results[index] = None
        counts[SCHEMA][0] += int(parsed_results[index] is not None)
        counts[SCHEMA][1] += 1
    case_ids = {case.id for case in replay.cases}
    orphan_ids = sorted(set(outcomes) - case_ids)
    rows, latencies, missing = [], [], Counter()
    catalog = {item.model_dump_json() for item in replay.citation_catalog}

    def add(name, good, total=1):
        counts[name][0] += int(good)
        counts[name][1] += total

    for case in replay.cases:
        row = {'case_id': case.id, 'category': case.category, 'expected_status': case.expected_status,
               'actual_status': None, 'issues': [], 'missing_measurements': []}
        rows.append(row)
        candidates = outcomes.get(case.id, [])
        if len(candidates) != 1:
            row['issues' if candidates else 'missing_measurements'].append(
                'DUPLICATE_OUTCOME' if candidates else 'MISSING_OUTCOME')
            missing['outcomes'] += int(not candidates)
            continue
        index, outcome = candidates[0]
        result = parsed_results[index]
        if result is None:
            row['issues'].append('INVALID_RESULT_SCHEMA')
        if outcome.failure_code:
            row['issues'].append('RUNNER_EXCEPTION')
        row['actual_status'] = result.status if result is not None else None
        status_ok = result is not None and result.status == case.expected_status
        raw_incident_matches = (canonical_digest({'incident_id': outcome.incident_id})
                                == canonical_digest({'incident_id': case.incident_id}))
        incident_ok = ('incident_id' in outcome.model_fields_set and raw_incident_matches
                       and (result is None or not hasattr(result, 'incident_id')
                            or result.incident_id == case.incident_id))
        if not status_ok:
            row['issues'].append('STATUS_MISMATCH')
        if not incident_ok:
            row['issues'].append('INCIDENT_MISMATCH')
        text_values = list(_strings(outcome.result))
        forbidden_ok = not any(term.casefold() in value for term in case.forbidden_outputs for value in text_values)
        if not forbidden_ok:
            row['issues'].append('FORBIDDEN_OUTPUT')
        reason_ok = True
        if case.expected_status in ('refusal', 'error'):
            if case.expected_reason_code is None:
                row['missing_measurements'].append('MISSING_EXPECTED_REASON_CODE')
                missing['reason'] += 1
                reason_ok = False
            else:
                actual_reason = getattr(result, 'reason_code', getattr(result, 'code', None))
                reason_ok = actual_reason == case.expected_reason_code
                if not reason_ok:
                    row['issues'].append('REASON_CODE_MISMATCH')
        behavior_ok = status_ok and incident_ok and reason_ok and forbidden_ok and not outcome.failure_code
        if case.expected_status == 'draft' or case.expected_reason_code is not None or row['issues']:
            add('Behavior Correctness', behavior_ok)
        if case.expected_status == 'refusal' and case.expected_reason_code is not None:
            add(REFUSAL, behavior_ok)
        if case.category == 'C' and case.expected_reason_code is not None:
            add('Critical Category C', behavior_ok)

        if isinstance(result, DraftReport):
            citations = [rule.citation for rule in result.applicable_rules]
            citations += [citation for assertion in [*result.found_facts, *result.suspicious_patterns]
                          for citation in assertion.sources]
            if not replay.citation_catalog:
                row['missing_measurements'].append('MISSING_CITATION_CATALOG')
                missing['traceability'] += 1
            else:
                for citation in citations:
                    add(TRACEABILITY, citation.model_dump_json() in catalog)
                for rule in result.applicable_rules:
                    if sha256(rule.rule_text.encode('utf-8')).hexdigest() != rule.citation.content_sha256:
                        row['issues'].append('RULE_TEXT_DIGEST_MISMATCH')
                        # Invalidate its already-counted applicable-rule reference.
                        if rule.citation.model_dump_json() in catalog:
                            counts[TRACEABILITY][0] -= 1
            evidence_count = sum(len(item.evidence) for item in [*result.found_facts, *result.suspicious_patterns])
            if case.context is None:
                row['missing_measurements'].append('MISSING_CONTEXT')
                missing['evidence'] += 1
            else:
                grounded = True
                try:
                    validate_report_context(result, case.context)
                except (ContractViolation, ValidationError, ValueError, TypeError):
                    grounded = False
                    row['issues'].append('INVALID_REPORT_CONTEXT_OR_EVIDENCE')
                add(EVIDENCE, evidence_count if grounded else 0, evidence_count)

        if case.expected_status == 'draft' and (case.expected_retrieval is None or not case.expected_retrieval.ids):
            row['missing_measurements'].append('MISSING_EXPECTED_RETRIEVAL')
            missing['retrieval'] += 1
        if case.expected_retrieval is not None:
            if outcome.retrieval is None:
                row['missing_measurements'].append('MISSING_ACTUAL_RETRIEVAL')
                missing['retrieval'] += 1
            elif outcome.retrieval.namespace != case.expected_retrieval.namespace:
                row['issues'].append('RETRIEVAL_NAMESPACE_MISMATCH')
                missing['retrieval'] += 1
            else:
                expected, actual = set(case.expected_retrieval.ids), set(outcome.retrieval.ids)
                intersection = len(expected & actual)
                add(PRECISION, intersection, len(actual))
                add(RECALL, intersection, len(expected))
        if outcome.latency_seconds is None:
            row['missing_measurements'].append('MISSING_LATENCY')
            missing['latency'] += 1
        else:
            latencies.append(outcome.latency_seconds)

    metric_names = [SCHEMA, TRACEABILITY, REFUSAL, EVIDENCE, PRECISION, RECALL,
                    'Behavior Correctness', 'Critical Category C']
    metrics = {name: ratio(*counts[name]) for name in metric_names}
    for key, name in [('M-02', FAITHFULNESS), ('M-03', RELEVANCE), ('M-08', ACCEPTANCE)]:
        measurement = replay.external_measurements.get(key)
        if (measurement and measurement.status == 'measured' and measurement.value is not None
                and measurement.denominator > 0 and measurement.method and measurement.method.strip()
                and measurement.evidence_references
                and all(ref.strip() for ref in measurement.evidence_references)):
            metrics[name] = ratio(measurement.value * measurement.denominator, measurement.denominator)
            metrics[name]['value'] = measurement.value
            metrics[name]['evidence_references'] = list(measurement.evidence_references)
            metrics[name]['method'] = measurement.method
        else:
            metrics[name] = ratio(reason='MISSING_EXTERNAL_MEASUREMENT_METHOD_OR_EVIDENCE')
    for name, value in [(LATENCY_MAX, max(latencies) if latencies else None),
                        (LATENCY_P95, sorted(latencies)[ceil(.95 * len(latencies)) - 1] if latencies else None)]:
        metrics[name] = {'status': 'measured' if latencies else 'not_measured', 'value': value,
                         'numerator': None, 'denominator': len(latencies), 'unit': 'seconds',
                         'samples': list(latencies), 'method': 'nearest_rank' if name == LATENCY_P95 else 'maximum'}

    gates = quality_gates(metrics, settings)
    for name in ('Behavior Correctness', 'Critical Category C'):
        metric = metrics[name]
        status = 'INCOMPLETE' if metric['value'] is None else 'PASS' if metric['value'] == 1 else 'FAIL'
        gates.append(gate(name, status, value=metric['value'], threshold=1, op='>='))
    # Missing cases are absent observations, not successful schema/latency samples.
    issues = any(row['issues'] for row in rows) or bool(orphan_ids)
    gates.append(gate('Outcome Integrity', 'FAIL' if issues else 'INCOMPLETE' if missing['outcomes'] else 'PASS'))
    gates.append(gate('Measurement Completeness', 'INCOMPLETE' if any(row['missing_measurements'] for row in rows) else 'PASS'))
    categories = Counter(case.category for case in replay.cases)
    independence_ids = {case.independence_id for case in replay.cases}
    # Labels alone cannot inflate coverage: the scenario input must also differ.
    fingerprints = {canonical_digest({'incident_id': case.incident_id,
                                      'context': case.context.model_dump(mode='json') if case.context else None})
                    for case in replay.cases}
    independent_count = min(len(independence_ids), len(fingerprints))
    coverage_ok = independent_count >= 30 and all(categories[category] for category in 'ABCDE')
    gates.append(gate('Dataset Coverage', 'PASS' if coverage_ok else 'INCOMPLETE',
                      value=independent_count, threshold=30, op='>=', reason=None if coverage_ok else 'REQUIRES_30_INDEPENDENT_CASES_AND_A_TO_E'))
    sme = replay.dataset.sme_acceptance
    sme_ok = bool(sme and sme.status == 'accepted' and sme.reviewer_id and sme.reviewed_at
                  and sme.evidence_references and all(ref.strip() for ref in sme.evidence_references)
                  and set(sme.case_ids) == case_ids and case_ids)
    gates.append(gate('SME Dataset Acceptance', 'PASS' if sme_ok else 'INCOMPLETE',
                      reason=None if sme_ok else 'MISSING_ACCEPTED_SME_EVIDENCE'))
    gates.append(gate('Acceptance Evidence Kind', 'PASS' if replay.dataset.kind == 'acceptance' else 'INCOMPLETE',
                      reason=None if replay.dataset.kind == 'acceptance' else 'SYNTHETIC_CONTROL_IS_NOT_ACCEPTANCE'))
    verdict = 'FAIL' if any(g['status'] == 'FAIL' for g in gates) else (
        'INCOMPLETE' if any(g['status'] == 'INCOMPLETE' for g in gates) else 'PASS')
    return {'schema_version': '1.0', 'verdict': verdict, 'dataset_id': replay.dataset.id,
            'dataset_kind': replay.dataset.kind, 'metrics': metrics, 'gates': gates, 'cases': rows,
            'coverage': {'case_count': len(replay.cases), 'outcome_count': len(replay.outcomes),
                         'independent_case_count': independent_count, 'categories': dict(categories),
                         'missing_outcome_count': missing['outcomes'], 'orphan_outcome_ids': orphan_ids},
            'limitations': ['M-04/M-06 check reference identity and context consistency, not AML meaning.',
                            'Rule-text digests bind text to supplied catalog hashes; the catalog contains no independently verified source text.',
                            'Free-text factual grounding remains pending; valid evidence identifiers do not prove the wording of facts or patterns.',
                            'External evidence references and SME metadata are declarations, not independently authenticated approvals.',
                            'Latency measures runner/replay observations; setup, TTI and manual-search savings are not measured.',
                            'Synthetic controls never establish product acceptance.']}
