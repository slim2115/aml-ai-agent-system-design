"""Recheck aggregate integrity of a saved Judge run; never call a model.

Qualification is historical, not an independent signature or a new holdout run.
Evaluation cannot supply AML or human acceptance. Only aggregate fields leave
this tool; raw responses are not copied into its output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from qa.dark_factory.baseline import _read_member, _strict_json, write_new

RESULT = 'verification/qa-judge-v2-2026-10-25/live-restart/result.json'
QUALIFICATION = 'verification/qa-judge-v2-2026-10-25/qualification.json'
REQUIRED_CHECKS = frozenset({'summary_recomputed', 'status_recomputed', 'full20_retained',
                             'no_invented_human_reviews', 'stable_source_model_identity'})


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError('invalid count')
    return value


def _ratio(entry, numerator, denominator):
    if not isinstance(entry, dict) or entry.get('numerator') != numerator or entry.get('denominator') != denominator:
        raise ValueError('ratio mismatch')
    rate = entry.get('rate')
    if denominator:
        if type(rate) not in (int, float) or not math.isfinite(rate) or not math.isclose(rate, numerator / denominator, rel_tol=1e-12):
            raise ValueError('ratio value mismatch')
    elif rate is not None:
        raise ValueError('undefined rate must be null')


def _validate(result, qualification, result_raw):
    if not isinstance(result, dict) or not isinstance(qualification, dict):
        raise ValueError('invalid records')
    if qualification.get('status') != 'PASS' or not isinstance(qualification.get('checks'), list):
        raise ValueError('qualification absent or failed')
    checks = qualification['checks']
    if (not checks or any(not isinstance(c, dict) or c.get('passed') is not True for c in checks)
            or not REQUIRED_CHECKS.issubset({c.get('name') for c in checks})):
        raise ValueError('qualification checks missing')
    expected_hash = qualification.get('verified_inputs', {}).get(RESULT)
    actual_hash = hashlib.sha256(result_raw).hexdigest()
    if expected_hash != actual_hash:
        raise ValueError('historical result hash mismatch')
    if result.get('profile') != 'compact-v2' or result.get('advisory_only') is not True:
        raise ValueError('unexpected profile')
    if result.get('status') not in {'PASS', 'FAIL', 'INCOMPLETE'}:
        raise ValueError('invalid status')
    summary = result['summary']
    if summary.get('advisory') is not True or summary.get('product_acceptance') != 'INCOMPLETE':
        raise ValueError('invalid scope')
    overall = summary['overall']
    keys = ('planned', 'valid_responses', 'completed', 'missing', 'error', 'timeout',
            'invalid_response', 'abstain', 'known_expected_pass', 'known_expected_fail')
    n = {key: _count(overall[key]) for key in keys}
    confusion = overall['confusion']
    if confusion.get('positive_class') != 'fail':
        raise ValueError('confusion orientation')
    tp, tn, fp, fn = (_count(confusion[k]) for k in ('tp', 'tn', 'fp', 'fn'))
    if (n['planned'] == 0 or n['known_expected_pass'] + n['known_expected_fail'] != n['planned']
            or n['valid_responses'] != n['completed']
            or n['completed'] + n['missing'] + n['error'] + n['timeout'] + n['invalid_response'] != n['planned']
            or tp + fn > n['known_expected_fail'] or tn + fp > n['known_expected_pass']
            or tp + tn + fp + fn + n['abstain'] > n['valid_responses']):
        raise ValueError('inconsistent confusion counts')
    correct = tp + tn
    _ratio(overall['all_planned_accuracy'], correct, n['planned'])
    _ratio(overall['valid_response_coverage'], n['valid_responses'], n['planned'])
    _ratio(overall['false_pass_rate'], fn, n['known_expected_fail'])
    human = summary['human_calibration']
    if human.get('status') != 'INCOMPLETE' or human.get('supplied_reviews') != 0:
        raise ValueError('human review is not the qualified baseline')
    if fn > 0 and (result['status'] != 'FAIL' or summary.get('known_challenge_status') != 'FAIL'):
        raise ValueError('false pass suppressed')
    return {'planned': n['planned'], 'valid_responses': n['valid_responses'],
            'correct_known_labels': correct, 'expected_defective': n['known_expected_fail'],
            'false_passes': fn, 'historical_judge_status': result['status'],
            'human_calibration': 'INCOMPLETE', 'result_sha256': actual_hash}


def audit(root: Path = ROOT):
    output = {'schema_version': '1.0', 'audit_status': 'INCOMPLETE',
              'judge_status': 'INCOMPLETE', 'human_calibration': 'INCOMPLETE',
              'product_acceptance': 'INCOMPLETE', 'metrics': None,
              'scope': 'Saved synthetic observations; no new model, expert review or independent holdout.'}
    try:
        result_raw, qualification_raw = _read_member(root, RESULT), _read_member(root, QUALIFICATION)
    except (OSError, ValueError):
        output['issue'] = 'MISSING_OR_UNSAFE_SOURCE'
        return output
    try:
        result, qualification = _strict_json(result_raw), _strict_json(qualification_raw)
        if isinstance(result, dict) and result.get('status') == 'FAIL':
            output['judge_status'] = 'FAIL'
        metrics = _validate(result, qualification, result_raw)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        output['audit_status'] = 'FAIL'
        output['issue'] = 'INVALID_OR_INCONSISTENT_EVIDENCE'
        return output
    output['audit_status'] = 'PASS'  # Only consistency of saved aggregate evidence.
    output['judge_status'] = 'FAIL' if metrics['historical_judge_status'] == 'FAIL' else 'INCOMPLETE'
    output['metrics'] = metrics
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    result = audit()
    try:
        path = write_new(result, args.output)
    except (OSError, ValueError) as exc:
        print('Audit write failed: ' + type(exc).__name__, file=sys.stderr)
        return 2
    print(json.dumps({'audit_status': result['audit_status'], 'judge_status': result['judge_status'],
                      'output': path.relative_to(ROOT).as_posix()}))
    return 0 if result['audit_status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
