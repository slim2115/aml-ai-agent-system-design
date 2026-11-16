"""Hand-authored synthetic development cases; no product or model is invoked.

Public cases are not a secret holdout or AML-expert-approved ground truth.
Expected citation identities are derived independently from the synthetic text.
"""
from __future__ import annotations

from hashlib import sha256
import json


def _citation(rule_id: str, version: str, source: str, text: str) -> dict:
    text_hash = sha256(text.encode('utf-8')).hexdigest()
    payload = {'rule_id': rule_id, 'version': version, 'source_ref': source,
               'char_start': 0, 'char_end': len(text), 'text_sha256': text_hash}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                            separators=(',', ':'), allow_nan=False).encode('utf-8')
    return {'rule_id': rule_id, 'rule_version': version, 'source_ref': source,
            'chunk_id': 'chunk-' + sha256(serialized).hexdigest(), 'content_sha256': text_hash}


def development_cases() -> list[dict]:
    """Return fresh objects so callers cannot mutate another run's labels."""
    old_text = 'Synthetic threshold edition one.'
    new_text = 'Synthetic threshold edition two.'
    source = 'Synthetic source for threshold'
    old = {'rule_id': 'DARK-THRESHOLD', 'title': 'Synthetic threshold',
           'regulation_ref': source, 'category': 'threshold', 'text': old_text,
           'effective_from': '2024-01-01', 'effective_until': '2026-07-20', 'version': 'v1'}
    new = {'rule_id': 'DARK-THRESHOLD', 'title': 'Synthetic threshold',
           'regulation_ref': source, 'category': 'threshold', 'text': new_text,
           'effective_from': '2026-07-20', 'version': 'v2'}
    cases = [
        {'case_id': 'DR-01', 'family': 'edition-boundary', 'slice': 'old-active',
         'query': 'threshold', 'as_of': '2026-07-19T23:59:59Z', 'records': [old, new],
         'relevant': [_citation('DARK-THRESHOLD', 'v1', source, old_text)]},
        {'case_id': 'DR-02', 'family': 'edition-boundary', 'slice': 'new-active',
         'query': 'threshold', 'as_of': '2026-07-20T00:00:00Z', 'records': [old, new],
         'relevant': [_citation('DARK-THRESHOLD', 'v2', source, new_text)]},
        {'case_id': 'DR-03', 'family': 'unrelated-query', 'slice': 'no-direct-match',
         'query': 'velocity', 'as_of': '2026-07-20T00:00:00Z', 'records': [old, new],
         'relevant': []},
    ]
    return json.loads(json.dumps(cases, ensure_ascii=False))

