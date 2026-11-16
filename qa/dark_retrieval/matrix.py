"""Measure a public retrieval matrix and replay fixed educational selectors.

Selectors are counterfactual experiments, NOT the AML policy or its decisions.
No threshold is fitted or automatically promoted to the product.
"""
from hashlib import sha256
import json

from qa.dark_retrieval.assessment import assess_run
from qa.dark_retrieval.matrix_cases import cases, citation
from qa.retrieval_checks import citation_identity, evaluate_ranking

# Fixed before the first live measurement; inclusive cosine-distance cutoffs.
SELECTORS = {'top1-unconditional': None, 'top1-distance-025': 0.25,
             'top1-distance-050': 0.50, 'top1-distance-075': 0.75}


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def validate_raw(case, raw):
    evaluate_ranking(raw, case['relevant'], k=3)
    calendar = case['as_of'][:10]  # The frozen catalog uses UTC timestamps only.
    active = {citation_identity(citation(r)) for r in case['records']
              if r['effective_from'] <= calendar < r.get('effective_until', '9999-12-31')}
    if len(raw) != min(3, len(active)):
        raise ValueError('INCOMPLETE_RANKING')
    for row in raw:
        if not 0 <= row['distance'] <= 2 or citation_identity(row['citation']) not in active:
            raise ValueError('INVALID_MATRIX_CANDIDATE')


def _ranking_summary(selected_cases, observations):
    rows = [evaluate_ranking(observations[c['case_id']], c['relevant'], k=3)
            for c in selected_cases if c['relevant'] and observations.get(c['case_id']) is not None]
    count = len(rows)
    return {'positive_observed': count,
            'positive_planned': sum(bool(c['relevant']) for c in selected_cases),
            'hit_at_1': {'numerator': sum(r['first_relevant_rank'] == 1 for r in rows), 'denominator': count},
            'recall_at_3': {'numerator': sum(r['hit_count'] for r in rows),
                            'denominator': sum(r['relevant_count'] for r in rows)},
            'mean_reciprocal_rank_at_3': sum(r['reciprocal_rank_at_k'] for r in rows) / count if count else None}


def score(observations):
    catalog = cases()
    ids = {c['case_id'] for c in catalog}
    if type(observations) is not dict or set(observations) - ids:
        raise ValueError('INVALID_MATRIX_OBSERVATIONS')
    for case in catalog:
        raw = observations.get(case['case_id'])
        if raw is not None:
            validate_raw(case, raw)
    candidates = {}
    for name, cutoff in SELECTORS.items():
        simulated = {}
        for case in catalog:
            raw = observations.get(case['case_id'])
            if raw is None:
                continue
            selected = []
            if raw and (cutoff is None or raw[0]['distance'] <= cutoff):
                selected.append(raw[0]['citation'])
            simulated[case['case_id']] = {'raw': raw, 'selected': selected}
        candidates[name] = {'max_cosine_distance': cutoff,
                            'assessment': assess_run(catalog, simulated)}
    return {'schema_version': 'retrieval-matrix-1',
            'measurement_status': 'PASS' if all(observations.get(i) is not None for i in ids) else 'INCOMPLETE',
            'catalog_sha256': digest(catalog), 'selectors_sha256': digest(SELECTORS),
            'planned_cases': len(catalog), 'observed_cases': sum(observations.get(i) is not None for i in ids),
            'positive_cases': sum(bool(c['relevant']) for c in catalog),
            'negative_cases': sum(not c['relevant'] for c in catalog),
            'ranking': _ranking_summary(catalog, observations),
            'ranking_by_slice': {s: _ranking_summary([c for c in catalog if c['slice'] == s], observations)
                                 for s in sorted({c['slice'] for c in catalog})},
            'selectors': candidates, 'selection_scope': 'offline_counterfactual_top1_only',
            'product_selection_observed': False, 'recommended_threshold': None,
            'independent_cases_claimed': False, 'hidden_holdout': 'NOT_ESTABLISHED',
            'product_acceptance': 'INCOMPLETE', 'expert_acceptance': 'INCOMPLETE'}


def measure(embedding, manifest_digest, *, index_factory=None, on_observation=None):
    if index_factory is None:
        from src.runtime.retrieval import RuleIndex
        index_factory = RuleIndex.from_records
    catalog, indices, observations = cases(), {}, {}
    for case in catalog:
        corpus_id = digest(case['records'])
        if corpus_id not in indices:
            index = index_factory(case['records'], embedding=embedding,
                                  embedding_manifest_digest=manifest_digest)
            # Short synthetic rules must each be one exact chunk. Never infer
            # relevance from the index output or silently relabel split chunks.
            expected = {citation_identity(citation(r)) for r in case['records']}
            actual = [citation_identity(c.model_dump(mode='json')) for c in index.citation_catalog]
            if len(actual) != len(expected) or set(actual) != expected:
                raise ValueError('CHUNK_INVENTORY_MISMATCH')
            indices[corpus_id] = index
        chunks = indices[corpus_id].search(case['query'], as_of=case['as_of'], k=3)
        raw = [{'citation': c.citation.model_dump(mode='json'), 'distance': c.distance} for c in chunks]
        if on_observation is not None:
            on_observation(case['case_id'], raw)  # Retain first result before validation.
        validate_raw(case, raw)
        observations[case['case_id']] = raw
    return {'observations': observations, 'assessment': score(observations),
            'corpus_count': len(indices), 'corpus_sha256': sorted(indices)}
