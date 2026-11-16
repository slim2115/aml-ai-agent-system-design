from copy import deepcopy

import pytest

from qa.dark_downstream import policy_pair
from qa.dark_factory import report_v2 as report


PATH = 'var/qa/dark-downstream/pair-demo'


def evidence():
    return {'baseline_status': 'PASS', 'candidate_status': 'FAIL', 'measurement_status': 'PASS',
            'mutation_check': 'PASS', 'pairs': [{'baseline': {'decision': 'PASS'}, 'candidate': {'decision': 'FAIL'}}]}


@pytest.mark.parametrize('missing', ['summary', 'child', 'both', 'neither'])
def test_successful_detection_never_approves_candidate(monkeypatch, missing):
    def saved(_):
        if missing in ('summary', 'both'):
            raise FileNotFoundError()
        return evidence(), 'a' * 64
    def replay(_):
        if missing in ('child', 'both'):
            raise FileNotFoundError()
        return evidence()
    monkeypatch.setattr(report, '_document', saved)
    monkeypatch.setattr(policy_pair, 'replay', replay)
    row = report.build({'policy_pair': PATH})['components']['policy_pair']
    assert row['evidence_status'] == ('PASS' if missing == 'neither' else 'INCOMPLETE')
    assert row['candidate_status'] == ('INCOMPLETE' if missing == 'both' else 'FAIL')
    assert report.evidence_exit_code({'components': {'policy_pair': row}}) == (2 if missing == 'both' else 1)
    if missing == 'neither':
        assert row['regressions'] == 1 and row['mutation_check'] == row['measurement_status'] == 'PASS'


def test_conflicting_summaries_preserve_individual_case_failure(monkeypatch):
    saved = evidence()
    saved['candidate_status'] = 'PASS'
    recomputed = deepcopy(saved)
    recomputed['pairs'][0]['candidate']['decision'] = 'PASS'
    monkeypatch.setattr(report, '_document', lambda _: (saved, 'a' * 64))
    monkeypatch.setattr(policy_pair, 'replay', lambda _: recomputed)
    row = report.policy_pair_report(PATH)
    assert row['evidence_status'] == 'INCOMPLETE' and row['candidate_status'] == 'FAIL'
