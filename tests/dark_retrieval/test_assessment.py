"""Known defects must be caught; a test double does not prove embedding quality."""
from copy import deepcopy
import re
from types import SimpleNamespace

import pytest

from qa.dark_retrieval.assessment import assess_case, assess_run
from qa.dark_retrieval.scenarios import development_cases
from src.runtime.retrieval import RuleIndex


class Tokenizer:
    def num_special_tokens_to_add(self, pair=False):
        return 2

    def __call__(self, text, *, add_special_tokens, truncation, return_offsets_mapping=False):
        spans = list(re.finditer(r'\S+', text))
        ids = list(range(len(spans)))
        result = {'input_ids': [-1, *ids, -2] if add_special_tokens else ids}
        if return_offsets_mapping:
            result['offset_mapping'] = [(m.start(), m.end()) for m in spans]
        return result


class FakeEmbedding:
    def __init__(self):
        self._model = SimpleNamespace(tokenizer=Tokenizer(), max_seq_length=16)

    def __call__(self, texts):
        return [[float(len(text)), 1., 2.] for text in texts]


def raw_search(case):
    index = RuleIndex.from_records(case['records'], embedding=FakeEmbedding(),
                                   embedding_manifest_digest='a' * 64)
    found = index.search(case['query'], as_of=case['as_of'], k=3)
    return [{'citation': chunk.citation.model_dump(mode='json'), 'distance': chunk.distance}
            for chunk in found]


def test_real_rule_index_edition_boundary_matches_independent_reference_identity():
    for case in development_cases()[:2]:
        raw = raw_search(case)
        assert len(raw) == 1
        assert raw[0]['citation'] == case['relevant'][0]
        result = assess_case(case, raw, [raw[0]['citation']])
        assert result['status'] == 'PASS'
        assert result['ranking']['recall_at_k'] == 1


def test_unrelated_query_exposes_naive_top_one_without_claiming_real_policy():
    case = development_cases()[2]
    raw = raw_search(case)
    assert raw  # semantic ranking returns a candidate; this is not a policy selection
    not_observed = assess_case(case, raw, None)
    assert not_observed['status'] == 'INCOMPLETE'
    assert not_observed['ranking']['precision_at_k'] is None
    naive_top_one = assess_case(case, raw, [raw[0]['citation']])
    assert naive_top_one['status'] == 'FAIL'
    assert naive_top_one['decision']['false_accept'] is True
    assert assess_case(case, raw, [])['status'] == 'PASS'


def test_stale_version_selected_is_wrong_even_when_rule_id_matches():
    before, after, _ = development_cases()
    wrong = before['relevant'][0]
    raw = [{'citation': wrong, 'distance': 0.0}]
    result = assess_case(after, raw, [wrong])
    assert result['status'] == 'FAIL'
    assert result['decision']['unsupported_selected_count'] == 1
    assert result['decision']['missed_relevant_count'] == 1
    assert result['ranking']['recall_at_k'] == 0


def test_counterfeit_and_duplicate_selection_are_errors_not_passes():
    case = development_cases()[0]
    raw = raw_search(case)
    counterfeit = deepcopy(raw[0]['citation'])
    counterfeit['content_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='SELECTION_NOT_IN_RAW'):
        assess_case(case, raw, [counterfeit])
    with pytest.raises(ValueError, match='DUPLICATE_SELECTION'):
        assess_case(case, raw, [raw[0]['citation'], raw[0]['citation']])


def test_missing_results_cannot_mask_measured_false_rejection():
    cases = development_cases()
    raw = raw_search(cases[0])
    summary = assess_run(cases, {cases[0]['case_id']: {'raw': raw, 'selected': []}})
    assert summary['status'] == 'FAIL'
    assert summary['false_reject'] == {'numerator': 1, 'denominator': 1}
    assert summary['false_accept'] == {'numerator': 0, 'denominator': 0}
    assert summary['incomplete'] == 2
    assert summary['expert_acceptance'] == 'INCOMPLETE'
    assert summary['secret_holdout'] is False


def test_empty_observations_are_incomplete_and_case_copies_are_independent():
    cases = development_cases()
    assert assess_run(cases, {})['status'] == 'INCOMPLETE'
    cases[0]['records'][0]['text'] = 'tampered'
    assert development_cases()[0]['records'][0]['text'] == 'Synthetic threshold edition one.'
