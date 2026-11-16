"""Runner defenses use real frozen labels/indexes and explicit equal vectors.

These tests qualify evidence collection, not semantic ranking or model quality.
"""
from dataclasses import replace
import json
import re
from types import SimpleNamespace

import pytest

from qa.retrieval_dataset import load_dataset
from qa.tools import run_retrieval as runner
from src.runtime.retrieval import RuleIndex


class WhitespaceTokenizerDouble:
    def num_special_tokens_to_add(self, pair=False):
        return 2

    def __call__(self, text, *, add_special_tokens, truncation, return_offsets_mapping=False):
        words = list(re.finditer(r'\S+', text))
        ids = list(range(len(words)))
        result = {'input_ids': [-1, *ids, -2] if add_special_tokens else ids}
        if return_offsets_mapping:
            result['offset_mapping'] = [(word.start(), word.end()) for word in words]
        return result


class EqualVectorEmbeddingDouble:
    _model = SimpleNamespace(tokenizer=WhitespaceTokenizerDouble(), max_seq_length=4096)

    def __call__(self, texts):
        return [[1.0, 1.0] for _ in texts]


DOUBLE_DIGEST = 'd' * 64


def ranking(tmp_path, dataset=None):
    return runner.run_ranking(tmp_path, dataset or load_dataset(), EqualVectorEmbeddingDouble(), DOUBLE_DIGEST)


@pytest.mark.parametrize('relative', ['var/qa/retrieval/example', 'verification/qa-step-06-example/run'])
def test_new_evidence_under_allowed_namespace_is_accepted_without_writing(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    output = runner.output_path(relative)
    assert output == tmp_path / relative
    assert not output.exists()


@pytest.mark.parametrize('relative', ['verification/qa-step-05-example/run', 'verification/qa-step-06-example',
                                    'var/qa/retrieval', 'data/report', 'var/qa/retrieval/../../outside'])
def test_output_cannot_escape_or_replace_other_work(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    with pytest.raises(ValueError, match='^(INVALID_QA_PATH|QA_RETRIEVAL_NEW_OUTPUT_REQUIRED)$'):
        runner.output_path(relative)


def test_absolute_path_outside_project_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    monkeypatch.setattr(runner, 'ROOT', root)
    with pytest.raises(ValueError, match='^INVALID_QA_PATH$'):
        runner.output_path(tmp_path / 'verification/qa-step-06-example/run')


def test_existing_evidence_directory_is_never_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    path = tmp_path / 'var/qa/retrieval/existing'
    path.mkdir(parents=True)
    sentinel = path / 'result.json'
    sentinel.write_text('{"previous":true}', encoding='utf-8')
    with pytest.raises(FileExistsError, match='^QA_EVIDENCE_ALREADY_EXISTS$'):
        runner.output_path(path)
    assert sentinel.read_text(encoding='utf-8') == '{"previous":true}'


def test_artifact_write_is_exclusive_even_inside_new_run(tmp_path):
    runner.write(tmp_path, 'result.json', {'first': True})
    with pytest.raises(FileExistsError):
        runner.write(tmp_path, 'result.json', {'replacement': True})
    assert json.loads((tmp_path / 'result.json').read_bytes()) == {'first': True}


def test_changed_product_query_map_is_rejected_before_indexes_or_evidence(tmp_path, monkeypatch):
    import src.runtime.retrieval as retrieval
    monkeypatch.setattr(retrieval, '_QUERY_TEXT', {**retrieval._QUERY_TEXT, 'threshold': 'changed query'})
    with pytest.raises(ValueError, match='^QA_FROZEN_QUERY_MAP_CHANGED$'):
        ranking(tmp_path)
    assert not (tmp_path / 'ranking').exists()


def test_all_thirteen_controls_are_recorded_but_only_five_enter_macro_metrics(tmp_path):
    result = ranking(tmp_path)
    assert result['status'] == 'PASS'
    assert result['quality_acceptance'] == 'NOT_ESTABLISHED'
    assert result['primary_case_ids'] == ['R01', 'R02', 'R03', 'R04', 'R05']
    assert result['primary_query_count'] == 5
    assert result['diagnostic_query_count'] == 8
    assert [row['case_id'] for row in result['cases']] == [f'R{number:02d}' for number in range(1, 14)]
    assert all(row['status'] == 'PASS' for row in result['cases'])
    assert all(relation['passed'] for relation in result['relations'])
    # Equal vectors give one fixed five-item order: exactly one target wins at k=1.
    assert result['primary_macro']['1']['precision_at_k'] == pytest.approx(1 / 5)
    assert result['primary_macro']['3']['recall_at_k'] == pytest.approx(3 / 5)
    assert result['primary_macro']['mrr_full'] == pytest.approx(sum(1 / rank for rank in range(1, 6)) / 5)
    for case in result['cases']:
        assert json.loads((tmp_path / 'ranking' / (case['case_id'] + '.json')).read_bytes()) == case
    no_relevance, no_active = result['cases'][-2:]
    assert no_relevance['active_chunks'] == 5 and no_active['active_chunks'] == 0
    for case in (no_relevance, no_active):
        assert case['metrics']['1']['ranking_status'] == 'out_of_scope'
        assert case['metrics']['1']['precision_at_k'] is None


@pytest.mark.parametrize('mutation', ['missing', 'duplicate'])
def test_incomplete_or_duplicate_active_inventory_is_an_error_not_low_quality(tmp_path, monkeypatch, mutation):
    original = RuleIndex.search

    def broken(self, query, *, as_of, k=3):
        rows = original(self, query, as_of=as_of, k=k)
        if len(rows) == 5 and k == 5:
            return rows[:-1] if mutation == 'missing' else [*rows[:-1], rows[0]]
        return rows

    monkeypatch.setattr(RuleIndex, 'search', broken)
    result = ranking(tmp_path)
    assert result['status'] == 'ERROR'
    assert result['primary_macro'] == {}
    primary = [row for row in result['cases'] if row['primary']]
    assert all(row['error_code'] == 'QA_ACTIVE_INVENTORY_MISMATCH' for row in primary)


def test_top_k_must_be_exact_prefix_of_same_full_ranking(tmp_path, monkeypatch):
    original = RuleIndex.search

    def inconsistent(self, query, *, as_of, k=3):
        if k == 1:
            rows = original(self, query, as_of=as_of, k=2)
            return rows[-1:]
        return original(self, query, as_of=as_of, k=k)

    monkeypatch.setattr(RuleIndex, 'search', inconsistent)
    result = ranking(tmp_path)
    assert result['status'] == 'ERROR'
    assert result['primary_macro'] == {}
    assert all(row['error_code'] == 'QA_TOP_K_PREFIX_MISMATCH'
               for row in result['cases'] if row['primary'])


@pytest.fixture
def base_inventory():
    corpus = load_dataset()['corpora']['base']
    index = RuleIndex.from_records(corpus['records'], embedding=EqualVectorEmbeddingDouble(),
                                   embedding_manifest_digest=DOUBLE_DIGEST)
    return index, corpus


@pytest.mark.parametrize('mutation', ['missing', 'duplicate'])
def test_chunk_set_must_match_frozen_source_inventory(base_inventory, mutation):
    index, corpus = base_inventory
    chunks = index.chunks[:-1] if mutation == 'missing' else (*index.chunks, index.chunks[0])
    with pytest.raises(ValueError, match='^QA_ACTUAL_CHUNKS_DISAGREE_WITH_FROZEN_SPANS$'):
        runner.assert_inventory(replace(index, chunks=chunks), corpus)


@pytest.mark.parametrize('changes', [
    {'char_start': 1}, {'char_end': 1}, {'text': 'changed source'},
    {'source_text_sha256': '0' * 64}, {'effective_from': '2020-01-01'},
    {'effective_until': '2099-01-01'},
])
def test_span_text_dates_and_source_digest_are_bound_to_frozen_inventory(base_inventory, changes):
    index, corpus = base_inventory
    corrupted = replace(index.chunks[0], **changes)
    with pytest.raises(ValueError, match='^QA_CHUNK_INVENTORY_MISMATCH$'):
        runner.assert_inventory(replace(index, chunks=(corrupted, *index.chunks[1:])), corpus)
