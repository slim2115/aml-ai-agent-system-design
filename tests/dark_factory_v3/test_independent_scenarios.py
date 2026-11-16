"""Open synthetic scenarios and corruption checks, with no live model."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / 'qa/datasets/dark-factory-v3/probe.py'
spec = importlib.util.spec_from_file_location('dark_factory_v3_probe', PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.fixture(scope='module')
def observed(tmp_path_factory):
    import src.runtime.context as context
    catalog = probe.load()
    root = tmp_path_factory.mktemp('df3')
    values = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(context, 'PROJECT_ROOT', root)
        for case in catalog['cases']:
            values.append(probe.observe(catalog, case, root / case['id']))
    return catalog, values


@pytest.mark.parametrize('number', [0, 1, 2])
def test_runtime_scenarios_with_frozen_oracles(observed, number):
    catalog, rows = observed
    assert probe.assess(catalog, catalog['cases'][number], rows[number])['status'] == 'PASS'


def test_no_window_refuses_before_index_or_model(observed):
    _, rows = observed
    assert rows[0]['result']['status'] == 'refusal'
    assert rows[0]['result']['reason_code'] == 'INSUFFICIENT_DATA'
    assert rows[0]['index_calls'] == rows[0]['model_calls'] == 0


def test_foreign_card_refuses_before_index_or_model(observed):
    _, rows = observed
    assert rows[2]['result']['status'] == 'refusal'
    assert rows[2]['result']['reason_code'] == 'CONFLICTING_DATA'
    assert rows[2]['index_calls'] == rows[2]['model_calls'] == 0


def test_draft_includes_two_precise_separate_currencies(observed):
    catalog, rows = observed
    assert rows[1]['result']['status'] == 'draft'
    totals = {e['currency']: e['value'] for f in rows[1]['result']['found_facts']
              for e in f['evidence'] if e['kind'] == 'sum'}
    assert totals == {'EUR': '0.30', 'JPY': '1000'}
    assert rows[1]['index_calls'] == 1 and rows[1]['model_calls'] == 0
    assert probe.assess(catalog, catalog['cases'][1], rows[1])['status'] == 'PASS'


@pytest.mark.parametrize('mutation', ['sum', 'sum_text', 'sum_members', 'count', 'rule', 'transaction', 'index', 'snapshot'])
def test_evaluator_rejects_corrupted_draft(observed, mutation):
    catalog, rows = observed
    row = deepcopy(rows[1])
    facts = row['result']['found_facts']
    sum_fact = next(f for f in facts if any(e['kind'] == 'sum' and e['currency'] == 'EUR' for e in f['evidence']))
    if mutation == 'sum':
        sum_fact['evidence'][0]['value'] = '0.31'
    elif mutation == 'sum_text':
        sum_fact['fact'] = 'Wrong amount.'
    elif mutation == 'sum_members':
        sum_fact['evidence'][0]['tx_ids'].pop()
    elif mutation == 'count':
        next(e for f in facts for e in f['evidence'] if e['kind'] == 'count')['value'] = 3
    elif mutation == 'rule':
        row['result']['applicable_rules'][0]['citation']['rule_version'] = 'untrusted'
    elif mutation == 'transaction':
        row['context']['transactions'][0]['amount'] = '9.99'
    elif mutation == 'index':
        row['index_calls'] = 0
    else:
        row['snapshot_preserved'] = False
    assert probe.assess(catalog, catalog['cases'][1], row)['status'] == 'FAIL'


def test_error_does_not_pass_as_expected_refusal(observed):
    catalog, rows = observed
    row = deepcopy(rows[0])
    row['result']['status'] = 'error'
    assert probe.assess(catalog, catalog['cases'][0], row)['status'] == 'FAIL'
    assert probe.assess(catalog, catalog['cases'][0], None)['status'] == 'INCOMPLETE'


def test_unverified_catalog_cannot_be_loaded(tmp_path):
    altered = tmp_path / 'cases.json'
    altered.write_bytes(probe.CATALOG.read_bytes() + b' ')
    with pytest.raises(ValueError, match='CATALOG_HASH_MISMATCH'):
        probe.load(altered)
