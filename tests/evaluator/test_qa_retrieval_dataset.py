"""Labels are independently source-bound before running a tokenizer/model."""
from copy import deepcopy
from hashlib import sha256
import json
import shutil

import pytest

from qa import retrieval_dataset as dataset


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


@pytest.fixture
def packet(tmp_path):
    target = tmp_path / 'retrieval-copy'
    shutil.copytree(dataset.DEFAULT_FOLDER, target)
    return target


def change_catalog(packet, change, monkeypatch):
    """Bypass only the pin to test semantic defenses under a newly authored seal."""
    catalog_path = packet / 'cases.json'
    catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
    change(catalog)
    catalog_path.write_bytes(encoded(catalog))
    manifest_path = packet / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    entry = next(item for item in manifest['files'] if item['path'] == 'cases.json')
    raw = catalog_path.read_bytes()
    entry.update(bytes=len(raw), sha256=sha256(raw).hexdigest())
    manifest_path.write_bytes(encoded(manifest))
    monkeypatch.setattr(dataset, 'MANIFEST_SHA256', sha256(manifest_path.read_bytes()).hexdigest())


def test_frozen_dataset_has_thirteen_controls_and_five_primary_topics():
    loaded = dataset.load_dataset()
    assert loaded['k_values'] == [1, 3]
    assert loaded['metric_mode'] == 'descriptive_no_threshold'
    assert loaded['manifest_sha256'] == dataset.MANIFEST_SHA256
    assert len(loaded['cases']) == 13
    assert [case['id'] for case in loaded['cases'] if case['primary']] == ['R01', 'R02', 'R03', 'R04', 'R05']
    assert len(loaded['corpora']['base']['records']) == 5
    assert len(loaded['corpora']['editions']['inventory']) == 6
    assert loaded['expert_approved'] is False


def test_expected_citation_binds_exact_text_and_whole_span():
    loaded = dataset.load_dataset()
    corpus = loaded['corpora']['base']
    row = next(row for row in corpus['records'] if row['rule_id'] == 'R-115-002')
    span = next(item for item in corpus['inventory'] if item['citation']['rule_id'] == 'R-115-002')
    label = loaded['cases'][0]['relevant'][0]
    assert label == span['citation']
    assert label['rule_version'] == '2024-rev3'
    assert label['content_sha256'] == sha256(row['text'].encode('utf-8')).hexdigest()
    assert span['char_start'] == 0
    assert span['char_end'] == len(row['text'])


def test_edition_oracle_tracks_exact_utc_boundary_and_same_instant():
    before, at, equivalent = dataset.load_dataset()['cases'][8:11]
    assert before['relevant'][0]['rule_version'] == '2024-rev3'
    assert at['relevant'][0]['rule_version'] == 'qa-2026-rev1'
    assert equivalent['relevant'] == at['relevant']
    assert equivalent['expected_active'] == at['expected_active']
    assert len(before['expected_active']) == len(at['expected_active']) == 5
    assert before['relevant'][0] not in at['expected_active']


def test_velocity_negative_is_distinct_from_empty_active_corpus():
    velocity, inactive = dataset.load_dataset()['cases'][11:]
    assert velocity['relevant'] == inactive['relevant'] == []
    assert len(velocity['expected_active']) == 5
    assert inactive['expected_active'] == []
    assert velocity['primary'] is inactive['primary'] is False


def test_repeated_loads_are_independent_values_and_never_rewrite_packet():
    before = {path: path.read_bytes() for path in dataset.DEFAULT_FOLDER.rglob('*') if path.is_file()}
    first = dataset.load_dataset()
    first['cases'][0]['relevant'].clear()
    first['corpora']['base']['records'][0]['text'] = 'changed in caller memory'
    assert dataset.load_dataset()['cases'][0]['relevant']
    assert all(path.read_bytes() == raw for path, raw in before.items())


@pytest.mark.parametrize('relative', ['cases.json', 'corpora/base.json', 'README.md'])
def test_changed_file_rejected_before_labels_load(packet, relative):
    with (packet / relative).open('ab') as handle:
        handle.write(b' ')
    with pytest.raises(dataset.DatasetError, match='^DATASET_FILE_CHANGED$'):
        dataset.load_dataset(packet)


def test_resealed_manifest_cannot_silently_replace_frozen_labels(packet):
    path = packet / 'manifest.json'
    path.write_bytes(path.read_bytes() + b'\n')
    with pytest.raises(dataset.DatasetError, match='^MANIFEST_CHANGED$'):
        dataset.load_dataset(packet)


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'wrong_rule', 'wrong_version', 'wrong_hash'])
def test_invalid_relevance_is_rejected_even_with_test_resealed_catalog(packet, monkeypatch, mutation):
    def change(catalog):
        labels = catalog['cases'][0]['relevant']
        if mutation == 'missing':
            labels.clear()
        elif mutation == 'duplicate':
            labels.append(deepcopy(labels[0]))
        elif mutation == 'wrong_rule':
            labels[:] = deepcopy(catalog['cases'][1]['relevant'])
        elif mutation == 'wrong_version':
            labels[0]['rule_version'] = 'wrong-version'
        else:
            labels[0]['content_sha256'] = '0' * 64
    change_catalog(packet, change, monkeypatch)
    with pytest.raises(dataset.DatasetError, match='^RELEVANCE_LABEL_MISMATCH$'):
        dataset.load_dataset(packet)


def test_wrong_active_edition_not_hidden_by_correct_relevance(packet, monkeypatch):
    def change(catalog):
        catalog['cases'][9]['expected_active'] = deepcopy(catalog['cases'][8]['expected_active'])
    change_catalog(packet, change, monkeypatch)
    with pytest.raises(dataset.DatasetError, match='^ACTIVE_INVENTORY_MISMATCH$'):
        dataset.load_dataset(packet)


def test_alias_cannot_become_an_independent_primary_case(packet, monkeypatch):
    change_catalog(packet, lambda catalog: catalog['cases'][5].update(primary=True), monkeypatch)
    with pytest.raises(dataset.DatasetError, match='^INVALID_PRIMARY_FLAG$'):
        dataset.load_dataset(packet)


@pytest.mark.parametrize('values', [[True, 3], [1, 5], [3, 1], []])
def test_k_values_cannot_change_after_measurement(packet, monkeypatch, values):
    change_catalog(packet, lambda catalog: catalog.update(k_values=values), monkeypatch)
    with pytest.raises(dataset.DatasetError, match='^INVALID_METRIC_CONTRACT$'):
        dataset.load_dataset(packet)


def test_duplicate_case_id_rejected(packet, monkeypatch):
    change_catalog(packet, lambda catalog: catalog['cases'][1].update(id='R01'), monkeypatch)
    with pytest.raises(dataset.DatasetError, match='^INVALID_CASE_SET$'):
        dataset.load_dataset(packet)


def test_source_provenance_cannot_point_elsewhere(packet, monkeypatch):
    change_catalog(packet, lambda catalog: catalog['corpora']['base'].update(
        source_path='qa/datasets/aml-v1/fixtures/S04/rules.json'), monkeypatch)
    with pytest.raises(dataset.DatasetError, match='^INVALID_CORPUS_PROVENANCE$'):
        dataset.load_dataset(packet)


def test_private_source_text_not_exposed_in_error(packet):
    (packet / 'cases.json').write_text('secret source contents', encoding='utf-8')
    with pytest.raises(dataset.DatasetError) as error:
        dataset.load_dataset(packet)
    assert str(error.value) == 'DATASET_FILE_CHANGED'


@pytest.mark.parametrize('raw,code', [(b'{"a":1,"a":2}', 'DUPLICATE_JSON_KEY'),
                                     (b'{"a":NaN}', 'INVALID_JSON_NUMBER'),
                                     (b'{"a":1.0}', 'INVALID_JSON_NUMBER')])
def test_strict_json_rejects_ambiguous_or_noninteger_metadata(raw, code):
    with pytest.raises(dataset.DatasetError, match=f'^{code}$'):
        dataset._json(raw)


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'C:/outside', 'corpora\\base.json'])
def test_manifest_path_cannot_escape_dataset(packet, path):
    with pytest.raises(dataset.DatasetError, match='^INVALID_DATASET_PATH$'):
        dataset._read(packet, path)
