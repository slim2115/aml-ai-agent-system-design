"""Assess ranked candidates and independently supplied downstream selections.

Never infer a downstream rejection from raw retrieval. This module does not
implement, configure or claim an application no-answer threshold.
"""
from __future__ import annotations

from qa.retrieval_checks import citation_identity, evaluate_ranking


def assess_case(case: dict, raw: list[dict] | None, selected: list[dict] | None) -> dict:
    if type(case) is not dict or set(case) != {'case_id', 'family', 'slice', 'query', 'as_of', 'records', 'relevant'}:
        raise ValueError('INVALID_CASE')
    if raw is None:
        if selected is not None:
            raise ValueError('SELECTION_WITHOUT_RAW')
        return {'case_id': case['case_id'], 'status': 'INCOMPLETE', 'reason': 'RAW_NOT_OBSERVED',
                'ranking': None, 'decision': None}
    ranking = evaluate_ranking(raw, case['relevant'], k=3)
    if selected is None:
        return {'case_id': case['case_id'], 'status': 'INCOMPLETE', 'reason': 'SELECTION_NOT_OBSERVED',
                'ranking': ranking, 'decision': None}
    if type(selected) is not list:
        raise ValueError('INVALID_SELECTION')
    picked = [citation_identity(c) for c in selected]
    if len(picked) != len(set(picked)):
        raise ValueError('DUPLICATE_SELECTION')
    candidates = {citation_identity(row['citation']) for row in raw}
    if not set(picked).issubset(candidates):
        raise ValueError('SELECTION_NOT_IN_RAW')
    relevant = {citation_identity(c) for c in case['relevant']}
    false_accept = not relevant and bool(picked)
    false_reject = bool(relevant) and not picked
    unsupported = len(set(picked) - relevant)
    missed = len(relevant - set(picked))
    decision = {'relevant_count': len(relevant), 'selected_count': len(picked),
                'false_accept': false_accept, 'false_reject': false_reject,
                'unsupported_selected_count': unsupported, 'missed_relevant_count': missed}
    passed = not (false_accept or false_reject or unsupported or missed)
    return {'case_id': case['case_id'], 'status': 'PASS' if passed else 'FAIL',
            'reason': None, 'ranking': ranking, 'decision': decision}


def assess_run(cases: list[dict], observations: dict) -> dict:
    if type(cases) is not list or not cases or type(observations) is not dict:
        raise ValueError('INVALID_RUN')
    ids = [case['case_id'] for case in cases]
    if len(ids) != len(set(ids)) or set(observations) - set(ids):
        raise ValueError('INVALID_CASE_IDS')
    rows = []
    for case in cases:
        observation = observations.get(case['case_id'])
        if observation is None:
            rows.append(assess_case(case, None, None))
        elif type(observation) is not dict or set(observation) != {'raw', 'selected'}:
            raise ValueError('INVALID_OBSERVATION')
        else:
            rows.append(assess_case(case, observation['raw'], observation['selected']))
    positives = [r for r in rows if r['decision'] is not None and r['decision']['relevant_count'] > 0]
    negatives = [r for r in rows if r['decision'] is not None and r['decision']['relevant_count'] == 0]
    failures = sum(r['status'] == 'FAIL' for r in rows)
    incomplete = sum(r['status'] == 'INCOMPLETE' for r in rows)
    return {'status': 'FAIL' if failures else 'INCOMPLETE' if incomplete else 'PASS',
            'scope': 'synthetic_development_observations_only', 'expert_acceptance': 'INCOMPLETE',
            'secret_holdout': False, 'rows': rows,
            'false_accept': {'numerator': sum(r['decision']['false_accept'] for r in negatives), 'denominator': len(negatives)},
            'false_reject': {'numerator': sum(r['decision']['false_reject'] for r in positives), 'denominator': len(positives)},
            'failures': failures, 'incomplete': incomplete}
