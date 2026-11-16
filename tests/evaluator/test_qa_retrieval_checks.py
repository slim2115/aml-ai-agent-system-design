"""Hand-worked ranking examples and invalid evidence must stay distinguishable."""
from copy import deepcopy

import pytest

from qa.retrieval_checks import citation_identity, evaluate_ranking


def citation(name='A', **changes):
    return {'rule_id': 'rule-' + name, 'rule_version': 'v1',
            'source_ref': 'Synthetic training source ' + name, 'chunk_id': 'chunk-' + name,
            'content_sha256': 'a' * 64, **changes}


def ranked(*names):
    return [{'citation': citation(name), 'distance': rank / 10}
            for rank, name in enumerate(names)]


def test_hand_worked_two_hits_among_four_with_three_relevant_labels():
    result = evaluate_ranking(ranked('B', 'C', 'A', 'D', 'E'),
                              [citation(name) for name in ('A', 'C', 'E')], k=4)
    assert result == {
        'k': 4, 'observed_count': 5, 'evaluated_count': 4,
        'relevant_count': 3, 'hit_count': 2, 'first_relevant_rank': 2,
        'ranking_status': 'evaluated', 'precision_at_k': 0.5,
        'recall_at_k': 2 / 3, 'reciprocal_rank_at_k': 0.5,
    }


def test_relevant_item_after_cutoff_does_not_count():
    observed = ranked('B', 'C', 'A')
    short = evaluate_ranking(observed, [citation()], k=2)
    longer = evaluate_ranking(observed, [citation()], k=3)
    assert (short['precision_at_k'], short['recall_at_k'], short['reciprocal_rank_at_k']) == (0, 0, 0)
    assert short['first_relevant_rank'] is None
    assert (longer['precision_at_k'], longer['recall_at_k'], longer['reciprocal_rank_at_k']) == (1 / 3, 1, 1 / 3)


def test_missing_result_slots_do_not_inflate_precision():
    result = evaluate_ranking(ranked('A'), [citation()], k=5)
    assert result['evaluated_count'] == 1
    assert result['precision_at_k'] == 0.2
    assert result['recall_at_k'] == result['reciprocal_rank_at_k'] == 1


def test_no_results_with_positive_labels_is_a_measured_miss():
    result = evaluate_ranking([], [citation()], k=3)
    assert result['ranking_status'] == 'evaluated'
    assert result['hit_count'] == 0
    assert result['precision_at_k'] == result['recall_at_k'] == result['reciprocal_rank_at_k'] == 0


@pytest.mark.parametrize('observed', [[], ranked('B', 'C')])
def test_no_relevant_labels_are_outside_ranking_even_with_candidates(observed):
    result = evaluate_ranking(observed, [], k=3)
    assert result['ranking_status'] == 'out_of_scope'
    assert result['relevant_count'] == 0
    assert result['precision_at_k'] is result['recall_at_k'] is result['reciprocal_rank_at_k'] is None


def test_no_relevant_labels_do_not_bypass_observation_validation():
    with pytest.raises(ValueError, match='^UNSORTED_DISTANCES$'):
        evaluate_ranking(list(reversed(ranked('A', 'B'))), [], k=1)


@pytest.mark.parametrize('changes', [
    {'rule_id': 'other-rule'}, {'rule_version': 'v2'}, {'source_ref': 'another source'},
    {'chunk_id': 'another-chunk'}, {'content_sha256': 'b' * 64},
])
def test_every_citation_identity_field_matters(changes):
    result = evaluate_ranking([{'citation': citation(**changes), 'distance': 0}], [citation()], k=1)
    assert result['hit_count'] == 0
    assert result['recall_at_k'] == 0


def test_old_version_does_not_hide_new_version_at_rank_two():
    old, current = citation(), citation(rule_version='v2')
    result = evaluate_ranking([{'citation': old, 'distance': 0},
                               {'citation': current, 'distance': 0.1}], [current], k=2)
    assert result['hit_count'] == 1
    assert result['precision_at_k'] == result['reciprocal_rank_at_k'] == 0.5
    assert result['recall_at_k'] == 1


def test_tied_distances_preserve_supplied_rank_order():
    observed = [{'citation': citation('B'), 'distance': 0.25},
                {'citation': citation('A'), 'distance': 0.25}]
    result = evaluate_ranking(observed, [citation()], k=2)
    assert result['first_relevant_rank'] == 2


@pytest.mark.parametrize('k', [True, False, 0, -1, 1.0, '3', None])
def test_k_must_be_a_positive_integer(k):
    with pytest.raises(ValueError, match='^INVALID_K$'):
        evaluate_ranking(ranked('A'), [citation()], k=k)


def test_duplicate_result_beyond_cutoff_is_invalid_not_truncated_away():
    with pytest.raises(ValueError, match='^DUPLICATE_OBSERVED_CITATION$'):
        evaluate_ranking(ranked('A', 'B', 'A'), [citation()], k=1)


def test_duplicate_label_cannot_change_recall_denominator():
    with pytest.raises(ValueError, match='^DUPLICATE_RELEVANT_CITATION$'):
        evaluate_ranking(ranked('A'), [citation(), deepcopy(citation())], k=1)


@pytest.mark.parametrize('distance', [float('nan'), float('inf'), -float('inf'), True, '0.1', None, 10 ** 400])
def test_nonfinite_or_nonnumeric_distance_is_invalid(distance):
    with pytest.raises(ValueError, match='^INVALID_DISTANCE$'):
        evaluate_ranking([{'citation': citation(), 'distance': distance}], [citation()], k=1)


def test_unsorted_distance_beyond_cutoff_is_invalid():
    observed = ranked('A', 'B', 'C')
    observed[2]['distance'] = 0.05
    with pytest.raises(ValueError, match='^UNSORTED_DISTANCES$'):
        evaluate_ranking(observed, [citation()], k=1)


@pytest.mark.parametrize('field', ['rule_id', 'rule_version', 'source_ref', 'chunk_id', 'content_sha256'])
def test_citation_must_be_complete_in_both_observations_and_labels(field):
    malformed = citation()
    del malformed[field]
    for observed, relevant in [([{'citation': malformed, 'distance': 0}], [citation()]),
                                (ranked('A'), [malformed])]:
        with pytest.raises(ValueError, match='^INVALID_CITATION$'):
            evaluate_ranking(observed, relevant, k=1)


@pytest.mark.parametrize('changes', [{'rule_id': 'bad id'}, {'rule_version': 1},
                                    {'source_ref': ' '}, {'content_sha256': 'f' * 63},
                                    {'chunk_id': ''}, {'unknown': 'extra'}])
def test_citation_uses_existing_wire_constraints(changes):
    with pytest.raises(ValueError, match='^INVALID_CITATION$'):
        citation_identity(citation(**changes))


@pytest.mark.parametrize('row', [None, {}, {'citation': citation()},
                                {'citation': citation(), 'distance': 0, 'score': 1},
                                {'citation': None, 'distance': 0}])
def test_malformed_observation_is_not_a_zero_score(row):
    with pytest.raises(ValueError, match='^INVALID_(OBSERVATION|CITATION)$'):
        evaluate_ranking([row], [citation()], k=1)


@pytest.mark.parametrize('observed,relevant', [(None, []), ([], None), ((), []), ([], ()), ({}, [])])
def test_inputs_must_be_lists(observed, relevant):
    with pytest.raises(ValueError, match='^INVALID_RANKING_INPUT$'):
        evaluate_ranking(observed, relevant, k=1)


def test_evaluation_preserves_the_callers_evidence():
    observed, relevant = ranked('B', 'A'), [citation()]
    before = deepcopy((observed, relevant))
    evaluate_ranking(observed, relevant, k=2)
    assert (observed, relevant) == before
