"""Synthetic saved reports exercise aggregate audit without local evidence."""
import hashlib
import json

import pytest

from qa.dark_factory import judge_audit


@pytest.fixture
def records(tmp_path):
    ratio = lambda n, d: {'numerator': n, 'denominator': d, 'rate': n / d}
    result = {'status': 'FAIL', 'profile': 'compact-v2', 'advisory_only': True,
              'summary': {'advisory': True, 'product_acceptance': 'INCOMPLETE',
                          'known_challenge_status': 'FAIL',
                          'human_calibration': {'status': 'INCOMPLETE', 'supplied_reviews': 0},
                          'overall': {'planned': 20, 'valid_responses': 19, 'completed': 19,
                                      'missing': 0, 'error': 0, 'timeout': 0,
                                      'invalid_response': 1, 'abstain': 0,
                                      'known_expected_pass': 6, 'known_expected_fail': 14,
                                      'confusion': {'positive_class': 'fail', 'tp': 10,
                                                    'tn': 6, 'fp': 0, 'fn': 3},
                                      'all_planned_accuracy': ratio(16, 20),
                                      'valid_response_coverage': ratio(19, 20),
                                      'false_pass_rate': ratio(3, 14)}}}
    qualification = {'status': 'PASS',
                     'checks': [{'name': n, 'passed': True} for n in sorted(judge_audit.REQUIRED_CHECKS)],
                     'verified_inputs': {}}

    def write(*, reseal=True):
        target = tmp_path / judge_audit.RESULT
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(result).encode('utf-8')
        target.write_bytes(raw)
        if reseal:
            qualification['verified_inputs'][judge_audit.RESULT] = hashlib.sha256(raw).hexdigest()
        qual = tmp_path / judge_audit.QUALIFICATION
        qual.parent.mkdir(parents=True, exist_ok=True)
        qual.write_text(json.dumps(qualification), encoding='utf-8')

    write()
    return tmp_path, result, qualification, write


def test_known_false_passes_never_become_acceptance(records):
    root, _, _, _ = records
    report = judge_audit.audit(root)
    assert report['audit_status'] == 'PASS'
    assert report['judge_status'] == 'FAIL'
    assert report['human_calibration'] == report['product_acceptance'] == 'INCOMPLETE'
    assert {key: report['metrics'][key] for key in ('planned', 'valid_responses', 'correct_known_labels', 'false_passes')} == {
        'planned': 20, 'valid_responses': 19, 'correct_known_labels': 16, 'false_passes': 3}
    assert report['metrics']['expected_defective'] == 14


def test_missing_result_is_incomplete(records):
    root, _, _, _ = records
    (root / judge_audit.RESULT).unlink()
    report = judge_audit.audit(root)
    assert report['audit_status'] == report['judge_status'] == 'INCOMPLETE'


def test_changed_result_without_requalification_is_not_a_pass(records):
    root, result, _, write = records
    result['summary']['overall']['confusion']['fn'] = 0
    write(reseal=False)
    report = judge_audit.audit(root)
    assert report['audit_status'] == 'FAIL'
    assert report['judge_status'] == 'FAIL'
    assert report['metrics'] is None


def test_qualification_must_contain_required_checks(records):
    root, _, qualification, write = records
    qualification['checks'].pop()
    write()
    assert judge_audit.audit(root)['audit_status'] == 'FAIL'


@pytest.mark.parametrize('mutation', ['denominator', 'ratio', 'suppress_failure', 'human', 'counts'])
def test_resealed_internal_inconsistency_fails(records, mutation):
    root, result, _, write = records
    overall = result['summary']['overall']
    if mutation == 'denominator':
        overall['false_pass_rate']['denominator'] = 20
    elif mutation == 'ratio':
        overall['false_pass_rate']['rate'] = 0
    elif mutation == 'suppress_failure':
        result['status'] = 'PASS'
        result['summary']['known_challenge_status'] = 'PASS'
    elif mutation == 'human':
        result['summary']['human_calibration']['supplied_reviews'] = 1
    else:
        overall['valid_responses'] = 20
    write()
    assert judge_audit.audit(root)['audit_status'] == 'FAIL'


def test_no_false_pass_does_not_establish_human_calibration(records):
    root, result, _, write = records
    result['status'] = result['summary']['known_challenge_status'] = 'PASS'
    overall = result['summary']['overall']
    overall['confusion']['fn'], overall['confusion']['tp'] = 0, 13
    overall['all_planned_accuracy'] = {'numerator': 19, 'denominator': 20, 'rate': 0.95}
    overall['false_pass_rate'] = {'numerator': 0, 'denominator': 14, 'rate': 0.0}
    write()
    report = judge_audit.audit(root)
    assert report['audit_status'] == 'PASS'
    assert report['judge_status'] == report['human_calibration'] == report['product_acceptance'] == 'INCOMPLETE'
