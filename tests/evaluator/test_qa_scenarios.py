"""Corruption tests for the frozen QA package, with no service or model access."""
from hashlib import sha256
import json
import shutil

import pytest
from pydantic import ValidationError

from qa.scenarios import CatalogError, DEFAULT_FOLDER, load_catalog


def _json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _reseal(folder):
    """Rehash edited members, so semantic failures cannot hide behind hash checks."""
    path = folder / 'manifest.json'
    manifest = _json(path)
    for member in manifest['files']:
        data = (folder / member['path']).read_bytes()
        member.update(bytes=len(data), sha256=sha256(data).hexdigest())
    _write(path, manifest)


def _edit_cases(folder, edit):
    path = folder / 'cases.json'
    payload = _json(path)
    edit(payload)
    _write(path, payload)
    _reseal(folder)


@pytest.fixture
def package(tmp_path):
    destination = tmp_path / 'catalog'
    shutil.copytree(DEFAULT_FOLDER, destination,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    return destination


def test_frozen_values_and_family_denominators_are_explicit():
    catalog = load_catalog()
    cases = {s.case.id: s for s in catalog.cases}
    assert len(cases) == 15
    assert len({s.case.independence_id for s in catalog.cases}) == 7
    assert {s.case.category for s in catalog.cases} == set('ABCDE')
    assert catalog.dataset.kind == 'synthetic_control'
    assert catalog.dataset.sme_acceptance.status == 'pending'
    assert cases['QA-S01'].golden.fact_count == 17
    assert [(a.currency, a.value) for a in cases['QA-S01'].golden.sums] == [('RUB', '1140000.00')]
    assert {a.currency: a.value for a in cases['QA-S03'].golden.sums} == {
        'USD': '1500012.30', 'RUB': '100.05'}
    assert cases['QA-S08'].case.incident_id == ' INC-000123'
    with pytest.raises(ValidationError, match='frozen_instance'):
        catalog.dataset_version = '2.0.0'


def test_unsealed_corruption_is_rejected(package):
    with (package / 'fixtures/S01/transactions.json').open('ab') as stream:
        stream.write(b' ')
    with pytest.raises(CatalogError, match='^MANIFEST_HASH_MISMATCH$'):
        load_catalog(package)


@pytest.mark.parametrize('member', ['../outside.json', '/outside.json',
                                  'fixtures\\S01\\policy.json', 'C:/outside.json'])
def test_manifest_cannot_escape_package(package, member):
    path = package / 'manifest.json'
    payload = _json(path)
    payload['files'][0]['path'] = member
    _write(path, payload)
    with pytest.raises(CatalogError, match='^UNEXPECTED_MANIFEST_MEMBER$'):
        load_catalog(package)


def test_case_insensitive_manifest_alias_is_rejected(package):
    path = package / 'manifest.json'
    payload = _json(path)
    duplicate = dict(payload['files'][0])
    duplicate['path'] = duplicate['path'].upper()
    payload['files'].append(duplicate)
    _write(path, payload)
    with pytest.raises(CatalogError, match='^DUPLICATE_MANIFEST_MEMBER$'):
        load_catalog(package)


def test_unlisted_artifact_is_rejected(package):
    (package / 'extra.json').write_text('{}', encoding='utf-8')
    with pytest.raises(CatalogError, match='^UNLISTED_DATASET_MEMBER$'):
        load_catalog(package)


def test_family_cannot_cross_splits(package):
    _edit_cases(package, lambda p: p['cases'][1].update(split='heldout'))
    with pytest.raises(CatalogError, match='^INVALID_CATALOG$'):
        load_catalog(package)


def test_renaming_family_cannot_hide_incident_leakage(package):
    def edit(payload):
        scenario = payload['cases'][1]
        scenario['split'] = 'heldout'
        scenario['case']['independence_id'] = 'FAMILY-RENAMED'
    _edit_cases(package, edit)
    with pytest.raises(CatalogError, match='^INVALID_CATALOG$'):
        load_catalog(package)


def test_critical_category_c_must_still_expect_refusal(package):
    def edit(payload):
        scenario = next(s for s in payload['cases'] if s['case']['category'] == 'C')
        scenario['case'].update(expected_status='error', expected_reason_code='NOT_READY')
    _edit_cases(package, edit)
    with pytest.raises(CatalogError, match='^INVALID_CATALOG$'):
        load_catalog(package)


def test_resealed_context_still_has_to_match_original_records(package):
    def edit(payload):
        payload['cases'][0]['case']['context']['transactions'][0]['amount'] = '1.00'
    _edit_cases(package, edit)
    with pytest.raises(CatalogError, match='^CONTEXT_SOURCE_MISMATCH$'):
        load_catalog(package)


def test_resealed_golden_sum_is_recomputed_from_source_records(package):
    def edit(payload):
        payload['cases'][0]['golden']['sums'][0]['value'] = '1.00'
    _edit_cases(package, edit)
    with pytest.raises(CatalogError, match='^GOLDEN_SUM_MISMATCH$'):
        load_catalog(package)


def test_resealed_golden_citation_must_bind_exact_source_span(package):
    def edit(payload):
        payload['cases'][0]['golden']['rules'][0]['citation']['content_sha256'] = '0' * 64
    _edit_cases(package, edit)
    with pytest.raises(CatalogError, match='^GOLDEN_CITATION_MISMATCH$'):
        load_catalog(package)


def test_outer_reseal_cannot_hide_snapshot_hash_mismatch(package):
    path = package / 'fixtures/S01/transactions.json'
    payload = _json(path)
    payload[0]['amount'] = '1.00'
    _write(path, payload)
    _reseal(package)
    with pytest.raises(CatalogError, match='^SNAPSHOT_FILE_HASH_MISMATCH$'):
        load_catalog(package)


def test_resealed_schema_drift_is_rejected(package):
    path = package / 'schema.json'
    payload = _json(path)
    payload['title'] = 'ChangedCatalog'
    _write(path, payload)
    _reseal(package)
    with pytest.raises(CatalogError, match='^CATALOG_SCHEMA_MISMATCH$'):
        load_catalog(package)


def test_synthetic_catalog_cannot_claim_sme_acceptance(package):
    def edit(payload):
        payload['dataset']['sme_acceptance']['status'] = 'accepted'
    _edit_cases(package, edit)
    with pytest.raises(CatalogError, match='^INVALID_CATALOG$'):
        load_catalog(package)
