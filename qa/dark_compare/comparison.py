"""Compare prevalidated first-attempt observations, never assess product quality.

This module does not run the product, validate external evidence, or provide a
secret holdout. It checks identity and reports observations given by the caller.
"""
from __future__ import annotations

import re

_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_IDENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
_STATUSES = {'PASS', 'FAIL', 'ERROR', 'TIMEOUT', 'INCOMPLETE'}


class ComparisonError(ValueError):
    """Reject malformed observations, identity conflicts and ambiguous runs."""


def _digest(value):
    return isinstance(value, str) and bool(_DIGEST.fullmatch(value))


def _ident(value):
    return isinstance(value, str) and bool(_IDENT.fullmatch(value))


def _cases(spec):
    if type(spec) is not dict or set(spec) != {'catalog_sha256', 'cases'} or not _digest(spec['catalog_sha256']):
        raise ComparisonError('INVALID_SPEC')
    cases = spec['cases']
    if type(cases) is not list or not cases or len(cases) > 10000:
        raise ComparisonError('INVALID_CASES')
    indexed = {}
    for row in cases:
        if (type(row) is not dict or set(row) != {'case_id', 'family', 'case_sha256'}
                or not _ident(row['case_id']) or not _ident(row['family']) or not _digest(row['case_sha256'])):
            raise ComparisonError('INVALID_CASE')
        if row['case_id'].casefold() in {x.casefold() for x in indexed}:
            raise ComparisonError('DUPLICATE_CASE')
        indexed[row['case_id']] = row
    return indexed


def _run(run, cases, catalog_sha256):
    if (type(run) is not dict or set(run) != {'run_id', 'catalog_sha256', 'code_sha256',
            'model_sha256', 'prompt_sha256', 'attempts'} or not _ident(run['run_id'])
            or any(not _digest(run[x]) for x in ('catalog_sha256', 'code_sha256', 'model_sha256', 'prompt_sha256'))):
        raise ComparisonError('INVALID_RUN')
    if run['catalog_sha256'] != catalog_sha256:
        raise ComparisonError('CATALOG_MISMATCH')
    if type(run['attempts']) is not list or len(run['attempts']) > 100000:
        raise ComparisonError('INVALID_ATTEMPTS')
    indexed = {}
    for row in run['attempts']:
        if (type(row) is not dict or set(row) != {'case_id', 'family', 'case_sha256', 'attempt', 'status'}
                or not _ident(row['case_id']) or not _ident(row['family']) or not _digest(row['case_sha256'])
                or type(row['attempt']) is not int or row['attempt'] < 1
                or type(row['status']) is not str or row['status'] not in _STATUSES):
            raise ComparisonError('INVALID_ATTEMPT')
        case = cases.get(row['case_id'])
        if case is None or row['family'] != case['family'] or row['case_sha256'] != case['case_sha256']:
            raise ComparisonError('CASE_IDENTITY_MISMATCH')
        key = (row['case_id'], row['attempt'])
        if key in indexed:
            raise ComparisonError('DUPLICATE_ATTEMPT')
        indexed[key] = row['status']
    return indexed


def compare(spec: dict, baseline: dict, candidate: dict) -> dict:
    """Measure comparable pairs; retry success never overwrites attempt one.

    A run must use the same complete case catalogue, not necessarily the same
    code, prompt or model; those identities are retained to explain changes.
    Unknown attempt 1 or incomplete first outcome remains explicitly missing.
    """
    cases = _cases(spec)
    a = _run(baseline, cases, spec['catalog_sha256'])
    b = _run(candidate, cases, spec['catalog_sha256'])
    if baseline['run_id'].casefold() == candidate['run_id'].casefold():
        raise ComparisonError('RUN_IDS_MUST_DIFFER')
    rows = []
    for case_id, case in sorted(cases.items()):
        first_a, first_b = a.get((case_id, 1)), b.get((case_id, 1))
        comparable = first_a not in (None, 'INCOMPLETE') and first_b not in (None, 'INCOMPLETE')
        transition = None
        if comparable:
            transition = ('regression' if first_a == 'PASS' and first_b != 'PASS' else
                          'improvement' if first_a != 'PASS' and first_b == 'PASS' else
                          'unchanged' if first_a == first_b else 'changed_failure')
        rows.append({'case_id': case_id, 'family': case['family'], 'case_sha256': case['case_sha256'],
                     'baseline_first': first_a, 'candidate_first': first_b,
                     'pair_status': 'MEASURED' if comparable else 'INCOMPLETE', 'transition': transition})
    measured = sum(r['pair_status'] == 'MEASURED' for r in rows)
    counts = {x: sum(r['transition'] == x for r in rows)
              for x in ('regression', 'improvement', 'unchanged', 'changed_failure')}
    meta = lambda r: {k: r[k] for k in ('run_id', 'catalog_sha256', 'code_sha256', 'model_sha256', 'prompt_sha256')}
    return {'schema_version': '1.0', 'comparison_status': 'MEASURED' if measured == len(rows) else 'INCOMPLETE',
            'scope': 'Caller-provided synthetic first-attempt observations; no product acceptance.',
            'human_acceptance': 'INCOMPLETE', 'hidden_holdout': 'NOT_ESTABLISHED',
            'planned_cases': len(cases), 'distinct_families_descriptive': len({c['family'] for c in cases.values()}),
            'independent_cases': None, 'measured_pairs': measured, 'incomplete_pairs': len(rows) - measured,
            'baseline_attempts': len(a), 'candidate_attempts': len(b), 'transitions': counts,
            'baseline_metadata': meta(baseline), 'candidate_metadata': meta(candidate), 'cases': rows}
