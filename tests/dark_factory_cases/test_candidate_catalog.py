"""Candidate catalogue validates actual wire contracts, not model quality."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / 'qa/datasets/dark-factory-v2/catalog.py'
SPEC = importlib.util.spec_from_file_location('dark_factory_candidate_catalog', PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_baseline_contract_probes_execute_and_keep_hypotheses_incomplete():
    report = module.evaluate(module.load_catalog())
    assert report['status'] == 'PASS'
    assert report['planned'] == report['passed'] == 9
    assert report['failed'] == report['errors'] == 0
    assert report['hypotheses_not_executed'] == 6
    assert report['product_acceptance'] == report['human_acceptance'] == 'INCOMPLETE'
    assert report['heldout_status'] == 'NOT_AVAILABLE'
    assert report['independent_case_count'] is None


def test_tampering_is_rejected_before_parsing(tmp_path):
    copied = tmp_path / 'cases.json'
    copied.write_bytes(module.DATA.read_bytes() + b' ')
    with pytest.raises(ValueError, match='CATALOG_HASH_MISMATCH'):
        module.load_catalog(copied)


def test_changed_oracle_is_measured_as_failure_not_silently_resealed():
    catalog = deepcopy(module.load_catalog())
    catalog['cases'][0]['expected'] = 'INVALID_INPUT_FORMAT'
    report = module.evaluate(catalog)
    assert report['status'] == 'FAIL'
    assert report['failed'] == 1
    assert report['cases'][0]['observed'] == 'MISSING_INPUT'
    assert report['product_acceptance'] == 'INCOMPLETE'


def test_runner_error_cannot_be_mistaken_for_product_failure_or_pass(monkeypatch):
    original = module.observe
    catalog = module.load_catalog()
    first_id = catalog['cases'][0]['id']
    def first_broken(case, context):
        if case['id'] == first_id:
            raise RuntimeError('controlled evaluator error')
        return original(case, context)
    monkeypatch.setattr(module, 'observe', first_broken)
    report = module.evaluate(catalog)
    assert report['status'] == 'ERROR'
    assert report['errors'] == 1 and report['failed'] == 0
