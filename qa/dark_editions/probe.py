"""Technical edition controls using RuleIndex and explicitly fake embeddings.

These are open, authored synthetic records; their dates and expectations are
not legal applicability, expert approval, independent holdout or model quality.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from qa.dark_factory.baseline import write_new
from qa.scenario_checks import _SyntheticEmbedding
from src.runtime.retrieval import RetrievalFailure, RuleIndex


def _record(rule_id, version, start, end, text):
    return {'rule_id': rule_id, 'title': 'Synthetic edition',
            'regulation_ref': 'Synthetic technical source', 'category': 'threshold',
            'text': text, 'effective_from': start, 'effective_until': end, 'version': version}


def cases():
    """Produce new test values with fixed expectations defined before execution."""
    unrelated = _record('SYN-OTHER', 'v1', '2025-01-01', None, 'Other artificial source.')
    expired = _record('SYN-TARGET', 'old', '2025-01-01', '2026-07-20', 'Expired artificial source.')
    new = _record('SYN-TARGET', 'new', '2026-07-20', None, 'New artificial source.')
    overlap = _record('SYN-TARGET', 'overlap', '2026-07-01', None, 'Overlapping artificial source.')
    return [
        {'id': 'ED-01-missing', 'records': [unrelated], 'as_of': '2026-07-20T00:00:00Z',
         'target': 'SYN-TARGET', 'expected': {'kind': 'RESULT', 'active_count': 1,
         'citations': [['SYN-OTHER', 'v1']], 'target_present': False}},
        {'id': 'ED-02-expired', 'records': [expired], 'as_of': '2026-07-20T00:00:00Z',
         'target': 'SYN-TARGET', 'expected': {'kind': 'RESULT', 'active_count': 0,
         'citations': [], 'target_present': False}},
        {'id': 'ED-03-overlap', 'records': [expired, overlap], 'as_of': '2026-07-20T00:00:00Z',
         'target': 'SYN-TARGET', 'expected': {'kind': 'ERROR', 'code': 'AMBIGUOUS_RULE'}},
        {'id': 'ED-04-boundary', 'records': [expired, new], 'as_of': '2026-07-20T03:00:00+03:00',
         'target': 'SYN-TARGET', 'expected': {'kind': 'RESULT', 'active_count': 1,
         'citations': [['SYN-TARGET', 'new']], 'target_present': True}},
    ]


def assess(case, observed):
    """Missing observations are INCOMPLETE; inconsistent completed ones FAIL."""
    if observed is None:
        return {'case_id': case['id'], 'status': 'INCOMPLETE', 'reason': 'NOT_OBSERVED'}
    if (type(observed) is not dict or observed.get('kind') not in ('RESULT', 'ERROR')
            or (observed.get('kind') == 'ERROR' and type(observed.get('code')) is not str)):
        return {'case_id': case['id'], 'status': 'INCOMPLETE', 'reason': 'INVALID_OBSERVATION'}
    if observed.get('kind') == 'ERROR' and observed['code'] == 'EMBEDDING_NOT_READY':
        return {'case_id': case['id'], 'status': 'INCOMPLETE', 'reason': 'EMBEDDING_NOT_READY'}
    return {'case_id': case['id'], 'status': 'PASS' if observed == case['expected'] else 'FAIL',
            'expected': case['expected'], 'observed': observed}


def observe(case):
    """Call real index, but equal vectors deliberately cannot measure ranking."""
    encoded = json.dumps(case['records'], ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    try:
        index = RuleIndex.from_records(case['records'], embedding=_SyntheticEmbedding(),
                                       embedding_manifest_digest=sha256(b'fake-equal-vectors').hexdigest(),
                                       source_sha256=sha256(encoded).hexdigest())
        count = index.active_chunk_count(case['as_of'])
        raw = index.search('threshold', as_of=case['as_of'], k=max(1, count))
        citations = sorted([[chunk.citation.rule_id, chunk.citation.rule_version] for chunk in raw])
        return {'kind': 'RESULT', 'active_count': count, 'citations': citations,
                'target_present': any(item[0] == case['target'] for item in citations)}
    except RetrievalFailure as exc:
        return {'kind': 'ERROR', 'code': exc.code}


def run():
    scenarios = cases()
    rows = [assess(case, observe(case)) for case in scenarios]
    statuses = [row['status'] for row in rows]
    result = 'FAIL' if 'FAIL' in statuses else 'INCOMPLETE' if 'INCOMPLETE' in statuses else 'PASS'
    return {'status': result, 'planned': len(scenarios), 'cases': rows,
            'scope': 'Open synthetic technical controls of RuleIndex only.',
            'embedding': 'FAKE_EQUAL_VECTORS', 'llm': 'NOT_RUN',
            'downstream_selection': 'NOT_OBSERVED', 'legal_applicability': 'NOT_ESTABLISHED',
            'human_acceptance': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE',
            'hidden_holdout': 'NOT_ESTABLISHED',
            'note': 'ED-01 observes an unrelated raw candidate, not the decision to refuse it.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = run()
    try:
        output = write_new(result, args.output)
    except (OSError, ValueError):
        return 2
    print(json.dumps({'status': result['status'], 'output': str(output.relative_to(ROOT))}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
