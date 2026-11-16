"""Offline comparison of saved single-case Judge ratings and programmatic gates.

No model is invoked. Historical qualification is a local record, not a
signature, expert review, independent holdout or product acceptance.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from qa.dark_factory.baseline import _read_member, _strict_json, write_new
from qa.judge.scoring import _rows

RUN = 'verification/qa-judge-v2-2026-10-25/live-restart'
QUALIFICATION = 'verification/qa-judge-v2-2026-10-25/qualification.json'
LABELS = ('pass', 'fail', 'abstain', 'invalid_response', 'error', 'timeout', 'missing')
GATES = ('PASS', 'FAIL', 'INCOMPLETE')


def cross(cases, observations):
    """Revalidate responses and produce disjoint counts, including missing rows."""
    rows = _rows(cases, observations)
    by_id = {case['id']: case for case in cases}
    for obs in observations:
        if obs.get('programmatic_gate') != by_id[obs['case_id']]['programmatic_gate']:
            raise ValueError('GATE_RECORD_MISMATCH')
    matrix = {gate: {label: 0 for label in LABELS} for gate in GATES}
    flagged = []
    pairs = {label: 0 for label in LABELS}
    for row in rows:
        case, response = row['case'], row['response']
        label = response['verdict'] if response is not None else row['status']
        if label not in LABELS:
            raise ValueError('INVALID_LABEL')
        if case['payload']['mode'] == 'pair':
            pairs[label] += 1
            continue
        gate = case['programmatic_gate']['status']
        matrix[gate][label] += 1
        if gate == 'FAIL' and label == 'pass':
            flagged.append(case['id'])
    single_total = sum(sum(group.values()) for group in matrix.values())
    return {'planned_single_cases': single_total, 'gate_judge_counts': matrix,
            'gate_fail_judge_pass_case_ids': sorted(flagged),
            'gate_fail_judge_pass_rate': {
                'numerator': len(flagged),
                'denominator': sum(matrix['FAIL'].values()),
                'rate': len(flagged) / sum(matrix['FAIL'].values()) if sum(matrix['FAIL'].values()) else None},
            'planned_pair_observations': sum(pairs.values()), 'pair_response_counts': pairs}


def build():
    """Replay qualified raw request/response records and bind all verified hashes."""
    incomplete = {'status': 'INCOMPLETE', 'historical_judge_status': 'INCOMPLETE',
                  'human_calibration': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE',
                  'scope': 'Historical open synthetic examples; no new model or independent acceptance.'}
    try:
        from qa.tools.score_judge import verify_saved
        qualification_raw = _read_member(ROOT, QUALIFICATION)
        qualification = _strict_json(qualification_raw)
        if qualification.get('status') != 'PASS':
            raise ValueError('QUALIFICATION_NOT_PASS')
        names = {item.get('name') for item in qualification.get('checks', []) if item.get('passed') is True}
        if not {'raw_requests_responses_and_journal_verified', 'full20_retained',
                'stable_source_model_identity'}.issubset(names):
            raise ValueError('QUALIFICATION_CHECKS_MISSING')
        dataset, run, result, observations, manifest_sha, inputs = verify_saved(ROOT / RUN)
        if (result.get('profile') != 'compact-v2' or result.get('status') != 'FAIL'
                or result.get('full_planned_matrix') is not True
                or result.get('model_identity_stable') is not True):
            raise ValueError('HISTORICAL_RUN_NOT_EXPECTED')
        verified = qualification.get('verified_inputs')
        if type(verified) is not dict or not inputs or any(verified.get(key) != value for key, value in inputs.items()):
            raise ValueError('HISTORICAL_HASH_MISMATCH')
        counts = cross(dataset['cases'], observations)
        if counts['planned_single_cases'] + counts['planned_pair_observations'] != len(dataset['cases']):
            raise ValueError('PLANNED_TOTAL_MISMATCH')
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return incomplete
    return {'status': 'PASS', 'historical_judge_status': 'FAIL',
            'human_calibration': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE',
            'scope': incomplete['scope'], 'counts': counts,
            'provenance': {'run': RUN, 'dataset_manifest_sha256': manifest_sha,
                           'qualification_sha256': sha256(qualification_raw).hexdigest(),
                           'verified_run_input_count': len(inputs),
                           'qualification_is_independent_signature': False}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    result = build()
    try:
        path = write_new(result, args.output)
    except (OSError, ValueError) as exc:
        print('Output write failed: ' + type(exc).__name__, file=sys.stderr)
        return 2
    print(json.dumps({'status': result['status'], 'historical_judge_status': result['historical_judge_status'],
                      'output': path.relative_to(ROOT).as_posix()}))
    return 0 if result['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
