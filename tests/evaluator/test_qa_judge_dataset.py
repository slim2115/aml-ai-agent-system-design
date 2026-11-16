"""Reject mislabeled, tampered or leaky synthetic Judge challenges before inference."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

from qa.judge.contracts import validate_payload
from qa.judge.dataset import DEFAULT_FOLDER, JudgeDatasetError, build_dataset, load_dataset


@pytest.fixture(scope='module')
def dataset():
    return load_dataset()


@pytest.fixture
def copied(tmp_path):
    folder = tmp_path / 'judge'
    shutil.copytree(DEFAULT_FOLDER, folder)
    return folder


def reseal(folder, data):
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
    (folder / 'cases.json').write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    manifest['files'][0].update(sha256=sha256(raw).hexdigest(), bytes=len(raw))
    (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')


def test_matrix_preserves_original_family_splits_and_opaque_prompts(dataset):
    cases = dataset['cases']
    assert len(cases) == len({case['id'] for case in cases}) == 20
    assert sum(case['split'] == 'calibration' for case in cases) == 9
    assert sum(case['split'] == 'heldout' for case in cases) == 11
    families = {}
    for case in cases:
        families.setdefault(case['family'], set()).add(case['split'])
        validate_payload(case['payload'])
        assert set(case['payload']) == {'case_id', 'criterion', 'mode', 'sources', 'candidates'}
        assert case['expected_kind'] == 'synthetic_rule'
        if case['payload']['mode'] == 'single':
            assert case['expected_preference'] == 'not_applicable'
    assert len(families) == 3 and all(len(splits) == 1 for splits in families.values())


def test_original_and_reordered_facts_pass_exact_gate_but_mutations_fail(dataset):
    for case in dataset['cases']:
        if case['mutation'] in {'original', 'original_order', 'reverse_order', 'pair_order_ab', 'pair_order_ba'}:
            assert case['programmatic_gate'] == {'status': 'PASS', 'issues': []}
        else:
            assert case['programmatic_gate']['status'] == 'FAIL'
            assert case['programmatic_gate']['issues']


def test_pairs_swap_only_candidate_positions_and_keep_ground_truth_outside_prompt(dataset):
    first, second = dataset['cases'][-2:]
    assert first['pair_id'] == second['pair_id']
    assert first['candidate_mapping']['A'] == second['candidate_mapping']['B']
    assert first['candidate_mapping']['B'] == second['candidate_mapping']['A']
    assert first['reports']['A'] == second['reports']['B']
    assert first['reports']['B'] == second['reports']['A']
    assert first['expected_preference'] == 'A' and second['expected_preference'] == 'B'
    assert 'candidate_mapping' not in first['payload']


def test_unsupported_conclusion_is_citable_and_not_in_source(dataset):
    case = next(case for case in dataset['cases'] if case['mutation'] == 'unsupported_claim')
    candidate = case['payload']['candidates'][0]
    claim = case['reports']['A']['suspicious_patterns'][0]['pattern']
    assert any(claim in fragment['text'] for fragment in candidate['fragments'])
    assert all(claim not in fragment['text'] for fragment in case['payload']['sources'])


def test_source_totals_come_from_records_and_are_not_rewritten_with_wrong_candidate(dataset):
    original, wrong = dataset['cases'][:2]
    assert original['payload']['sources'] == wrong['payload']['sources']
    aggregates = next(item['text'] for item in wrong['payload']['sources'] if item['ref_id'] == 'source/aggregates')
    assert json.loads(aggregates)['sums'] == {'RUB': '1140000.00'}
    assert any('1140100.00' in fact['fact'] for fact in wrong['reports']['A']['found_facts'])


@pytest.mark.parametrize('mutation', ['oracle', 'split', 'source', 'gate', 'missing_case', 'pair_mapping'])
def test_rewritten_manifest_cannot_hide_changed_oracle_sources_or_matrix(copied, mutation):
    data = json.loads((copied / 'cases.json').read_bytes())
    if mutation == 'oracle':
        data['cases'][1]['expected_verdict'] = 'pass'
    elif mutation == 'split':
        data['cases'][0]['split'] = 'heldout'
    elif mutation == 'source':
        data['cases'][0]['payload']['sources'][0]['text'] = 'invented'
    elif mutation == 'gate':
        data['cases'][1]['programmatic_gate'] = {'status': 'PASS', 'issues': []}
    elif mutation == 'missing_case':
        data['cases'].pop()
    else:
        data['cases'][-1]['candidate_mapping'] = data['cases'][-2]['candidate_mapping']
    reseal(copied, data)
    with pytest.raises(JudgeDatasetError, match='RECONSTRUCTION'):
        load_dataset(copied)


def test_changed_source_identity_rejected_even_with_new_manifest(copied):
    data = json.loads((copied / 'cases.json').read_bytes())
    next(iter(data['source_hashes'].values()))['sha256'] = '0' * 64
    reseal(copied, data)
    with pytest.raises(JudgeDatasetError, match='SOURCE_HASH'):
        load_dataset(copied)


@pytest.mark.parametrize('member', ['../cases.json', '/cases.json', 'D:/cases.json', 'x/../cases.json'])
def test_manifest_path_cannot_escape(copied, member):
    manifest = json.loads((copied / 'manifest.json').read_bytes())
    manifest['files'][0]['path'] = member
    (copied / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(JudgeDatasetError):
        load_dataset(copied)


def test_unlisted_member_and_changed_bytes_rejected(copied):
    (copied / 'surprise.json').write_text('{}', encoding='utf-8')
    with pytest.raises(JudgeDatasetError, match='UNLISTED'):
        load_dataset(copied)


def test_frozen_data_cannot_be_overwritten(copied):
    before = (copied / 'cases.json').read_bytes()
    with pytest.raises(FileExistsError):
        build_dataset(copied)
    assert (copied / 'cases.json').read_bytes() == before
