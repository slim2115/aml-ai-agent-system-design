"""Read and evaluate an explicit synthetic development catalogue, offline.

No hidden holdout, production measurements, model calls or expert acceptance.
The expected values were recorded before this evaluator was written.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = Path(__file__).with_name('cases.json')
EXPECTED_SHA256 = 'c29e43e8dcba2c497ac5d2bafd8ec210a196d7473627f8d630384313ec4b9b1e'


def _pairs(items):
    output = {}
    for key, value in items:
        if key in output:
            raise ValueError('DUPLICATE_JSON_KEY')
        output[key] = value
    return output


def load_catalog(path: Path = DATA) -> dict:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        raise ValueError('INVALID_CATALOG_FILE')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED_SHA256:
        raise ValueError('CATALOG_HASH_MISMATCH')
    catalog = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('INVALID_NUMBER')))
    if (catalog.get('schema_version') != '1.0' or catalog.get('dataset_version') != '2.0.0-candidate.1'
            or catalog.get('split') != 'development' or catalog.get('purpose') != 'synthetic_learning_only'
            or catalog.get('human_acceptance') != 'INCOMPLETE'):
        raise ValueError('INVALID_CATALOG_METADATA')
    cases = catalog.get('cases')
    hypotheses = catalog.get('unexecuted_hypotheses')
    if not isinstance(cases, list) or not cases or not isinstance(hypotheses, list) or not hypotheses:
        raise ValueError('EMPTY_CATALOG')
    ids = [row.get('id') for row in cases + hypotheses]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and i.startswith('DF-') for i in ids):
        raise ValueError('INVALID_CASE_IDS')
    for row in cases:
        if set(row) != {'id', 'family', 'risk', 'kind', 'input', 'expected'}:
            raise ValueError('INVALID_CASE_FIELDS')
        if row['kind'] == 'identifier':
            if set(row['input']) != {'value'} or row['expected'] not in (None, 'MISSING_INPUT', 'INVALID_INPUT_FORMAT'):
                raise ValueError('INVALID_IDENTIFIER_CASE')
        elif row['kind'] == 'window':
            if set(row['input']) != {'start', 'end', 'as_of', 'timestamp'} or type(row['expected']) is not bool:
                raise ValueError('INVALID_WINDOW_CASE')
        else:
            raise ValueError('UNKNOWN_CASE_KIND')
    return catalog


def observe(case: dict, anchor_context: dict):
    """Call actual production contract checks, never a model or a service.

    Window cases mutate a copy of the real v1 synthetic context and ask the
    actual InvestigationContext validator if it accepts the one altered tx.
    """
    from copy import deepcopy
    from pydantic import ValidationError
    from src.contracts.semantics import classify_incident_id
    from src.contracts.v1 import InvestigationContext

    if case['kind'] == 'identifier':
        return classify_incident_id(case['input']['value'])
    value = case['input']
    context = deepcopy(anchor_context)
    context['window'] = {'start_inclusive': value['start'], 'end_exclusive': value['end'], 'as_of': value['as_of']}
    context['transactions'][0]['timestamp'] = value['timestamp']
    try:
        InvestigationContext.model_validate(context)
    except ValidationError as exc:
        # A different broken invariant is not evidence of window exclusion.
        if not any('transaction outside requested window' in str(e.get('msg', '')) for e in exc.errors()):
            raise ValueError('UNRELATED_CONTEXT_FAILURE') from None
        return False
    return True


def evaluate(catalog: dict) -> dict:
    from qa.scenarios import load_catalog as load_anchor
    scenarios = load_anchor().cases
    anchor = next(case for case in scenarios if case.case.id == 'QA-S01')
    anchor_context = anchor.case.context.model_dump(mode='json')
    rows = []
    for case in catalog['cases']:
        try:
            observed = observe(case, anchor_context)
            status = 'PASS' if observed == case['expected'] else 'FAIL'
        except Exception:
            # Do not leak payloads; errors are distinct from evaluated failures.
            observed, status = None, 'ERROR'
        rows.append({'id': case['id'], 'family': case['family'], 'risk': case['risk'],
                     'status': status, 'expected': case['expected'], 'observed': observed})
    return {'schema_version': '1.0', 'dataset_version': catalog['dataset_version'],
            'scope': 'offline development contract probes only',
            'status': ('FAIL' if any(r['status'] == 'FAIL' for r in rows)
                       else 'ERROR' if any(r['status'] == 'ERROR' for r in rows) else 'PASS'),
            'product_acceptance': 'INCOMPLETE', 'human_acceptance': 'INCOMPLETE',
            'heldout_status': 'NOT_AVAILABLE', 'independent_case_count': None,
            'planned': len(rows), 'passed': sum(r['status'] == 'PASS' for r in rows),
            'failed': sum(r['status'] == 'FAIL' for r in rows),
            'errors': sum(r['status'] == 'ERROR' for r in rows),
            'hypotheses_not_executed': len(catalog['unexecuted_hypotheses']), 'cases': rows}


if __name__ == '__main__':
    report = evaluate(load_catalog())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report['status'] == 'PASS' else 1)
