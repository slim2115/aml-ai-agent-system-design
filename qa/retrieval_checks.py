"""Pure ranking metrics for exact, versioned chunk labels.

These metrics measure retrieval against supplied labels, not legal applicability
or AML acceptance. A query with no relevant labels has no ranking denominator;
it remains outside ranking scope even when semantic search returns candidates.
No model, service, filesystem or network access is performed here.
"""
from __future__ import annotations

import math

from src.contracts.v1 import RuleCitation


_IDENTITY_FIELDS = ('rule_id', 'rule_version', 'source_ref', 'chunk_id', 'content_sha256')


def citation_identity(value: dict) -> tuple[str, str, str, str, str]:
    """Validate the existing wire contract and preserve all identity fields."""
    if type(value) is not dict:
        raise ValueError('INVALID_CITATION')
    try:
        citation = RuleCitation.model_validate(value)
    except ValueError:
        raise ValueError('INVALID_CITATION') from None
    return tuple(getattr(citation, field) for field in _IDENTITY_FIELDS)


def evaluate_ranking(observed: list[dict], relevant: list[dict], *, k: int) -> dict:
    """Evaluate a ranked result list at k, without shortening its denominator.

    Each observed item has exactly ``citation`` and ``distance``. Distances must
    be finite numbers in nondecreasing order; ties retain the supplied order.
    Relevant items are complete RuleCitation dictionaries. Both lists reject
    duplicate identities. Validation covers the complete observation, including
    rows beyond k, so truncation cannot conceal invalid retrieval evidence.

    Precision is hits / k, recall is hits / relevant_count and reciprocal rank
    is 1 / first_relevant_rank, or zero for a miss. For no relevant labels all
    three metrics are None, explicitly excluded from ranking aggregation.
    """
    if type(k) is not int or k <= 0:
        raise ValueError('INVALID_K')
    if type(observed) is not list or type(relevant) is not list:
        raise ValueError('INVALID_RANKING_INPUT')

    labels = [citation_identity(item) for item in relevant]
    relevant_identities = set(labels)
    if len(labels) != len(relevant_identities):
        raise ValueError('DUPLICATE_RELEVANT_CITATION')

    ranked = []
    seen = set()
    previous_distance = None
    for row in observed:
        if type(row) is not dict or set(row) != {'citation', 'distance'}:
            raise ValueError('INVALID_OBSERVATION')
        identity = citation_identity(row['citation'])
        if identity in seen:
            raise ValueError('DUPLICATE_OBSERVED_CITATION')
        seen.add(identity)
        distance = row['distance']
        try:
            finite = type(distance) in (int, float) and math.isfinite(distance)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError('INVALID_DISTANCE')
        if previous_distance is not None and distance < previous_distance:
            raise ValueError('UNSORTED_DISTANCES')
        previous_distance = distance
        ranked.append(identity)

    hit_ranks = [rank for rank, identity in enumerate(ranked[:k], start=1)
                 if identity in relevant_identities]
    first_hit = hit_ranks[0] if hit_ranks else None
    evaluable = bool(relevant_identities)
    return {
        'k': k,
        'observed_count': len(ranked),
        'evaluated_count': min(k, len(ranked)),
        'relevant_count': len(relevant_identities),
        'hit_count': len(hit_ranks),
        'first_relevant_rank': first_hit,
        'ranking_status': 'evaluated' if evaluable else 'out_of_scope',
        'precision_at_k': len(hit_ranks) / k if evaluable else None,
        'recall_at_k': len(hit_ranks) / len(relevant_identities) if evaluable else None,
        'reciprocal_rank_at_k': (1 / first_hit if first_hit else 0.0) if evaluable else None,
    }
