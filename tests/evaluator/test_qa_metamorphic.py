"""Hermetic qualification of the QA relation checker, not embedding quality."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from qa import metamorphic_checks as checks
from src.config import get_settings
import src.runtime.context as context_module


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


DOUBLE_DIGEST = sha256(b'hermetic-metamorphic-equal-vector-double-v1').hexdigest()


@pytest.fixture(scope='module')
def completed(tmp_path_factory):
    temporary_root = tmp_path_factory.mktemp('metamorphic')
    output = temporary_root / 'verification' / 'example'
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(checks, 'ROOT', temporary_root)
        patch.setattr(context_module, 'PROJECT_ROOT', temporary_root)
        summary = checks.run_metamorphic(output, EqualVectorEmbeddingDouble(), DOUBLE_DIGEST, get_settings())
    return summary, output / 'metamorphic'


def load_case(completed, case_id):
    _summary, output = completed
    folder = output / case_id
    observations = [json.loads((folder / (label + '-observation.json')).read_bytes()) for label in ('before', 'after')]
    values = [{name: json.loads((folder / label / name).read_bytes()) for name in checks._MEMBERS}
              for label in ('before', 'after')]
    spec = next(item for item in checks.SPECS if item['id'] == case_id)
    return spec, values, observations


def test_five_declared_families_pass_with_real_product_and_explicit_double(completed):
    summary, output = completed
    assert summary['status'] == 'PASS', summary
    assert summary['family_count'] == summary['passed_families'] == 5
    assert summary['observation_count'] == 10
    assert summary['frozen_dataset_unchanged'] is True
    specs = json.loads((output / 'specifications.json').read_bytes())
    assert specs['embedding_manifest_sha256'] == DOUBLE_DIGEST
    assert specs['families'] == list(checks.SPECS)


@pytest.mark.parametrize('case_id', ['MT-01', 'MT-02', 'MT-03', 'MT-04', 'MT-05'])
def test_transformed_fixtures_rebind_each_content_hash_and_policy(completed, case_id):
    spec, values, observations = load_case(completed, case_id)
    folder = completed[1] / case_id
    for label, value in zip(('before', 'after'), values):
        manifest = value['manifest.json']
        assert value['policy.json']['snapshot_manifest_sha256'] == sha256((folder / label / 'manifest.json').read_bytes()).hexdigest()
        for descriptor in manifest['files']:
            assert descriptor['content_sha256'] == sha256((folder / label / descriptor['name']).read_bytes()).hexdigest()
            assert descriptor['row_count'] == len(value[descriptor['name']])
    assert checks.check_relation(spec, *observations) == []


@pytest.mark.parametrize('defect', ['missing_fact', 'duplicate_fact', 'wrong_amount_text',
                                  'wrong_total', 'wrong_rule_version', 'wrong_rule_text',
                                  'wrong_context', 'missing_runtime_chunk'])
def test_independent_oracle_detects_defective_output(completed, defect):
    spec, values, observations = load_case(completed, 'MT-03')
    observed = deepcopy(observations[1])
    result = observed['result']
    if defect == 'missing_fact':
        result['found_facts'].pop()
    elif defect == 'duplicate_fact':
        result['found_facts'].append(deepcopy(result['found_facts'][0]))
    elif defect == 'wrong_amount_text':
        next(fact for fact in result['found_facts'] if fact['evidence'][0]['kind'] == 'transaction')['fact'] = 'Wrong ungrounded amount.'
    elif defect == 'wrong_total':
        next(fact for fact in result['found_facts'] if fact['evidence'][0]['kind'] == 'sum')['evidence'][0]['value'] = '0.01'
    elif defect == 'wrong_rule_version':
        result['applicable_rules'][0]['citation']['rule_version'] = 'incorrect'
    elif defect == 'wrong_rule_text':
        rule = result['applicable_rules'][0]
        rule['rule_text'] = 'Unsupported source text.'
        rule['citation']['content_sha256'] = sha256(rule['rule_text'].encode()).hexdigest()
    elif defect == 'wrong_context':
        observed['context']['transactions'].pop()
    else:
        observed['ranking'][0]['chunks'] = []
    assert checks.check_observation(values[1], spec['incident_id'], observed)


@pytest.mark.parametrize('defect', ['invented_confidence', 'invented_pattern'])
def test_template_oracle_rejects_schema_valid_unsupported_claims(completed, defect):
    spec, values, observations = load_case(completed, 'MT-03')
    observed = deepcopy(observations[1])
    result = observed['result']
    if defect == 'invented_confidence':
        result['confidence'] = 1.0
    else:
        result['suspicious_patterns'] = [{
            'pattern': 'Unsupported inference about the selected client.',
            'evidence': deepcopy(result['found_facts'][0]['evidence']),
            'sources': [deepcopy(result['applicable_rules'][0]['citation'])],
        }]
    # The references and wire shape remain valid: this is an oracle defect,
    # rather than an unrelated contract/schema rejection.
    parsed = checks.ADAPTERS['InvestigationResult'].validate_python(result)
    checks.validate_report_context(parsed, checks._expected_context(values[1], spec['incident_id']))
    assert checks.check_observation(values[1], spec['incident_id'], observed) == ['UNSUPPORTED_MODEL_CLAIM']


@pytest.mark.parametrize('case_id, defect, expected', [
    ('MT-01', 'fact', 'FACT_INVARIANCE_FAILED'),
    ('MT-02', 'window', 'CONTEXT_INVARIANCE_FAILED'),
    ('MT-03', 'amount_noop', 'AMOUNT_CHANGED_UNEXPECTED_FACTS'),
    ('MT-03', 'wrong_delta', 'AMOUNT_DELTA_NOT_EXACT'),
    ('MT-04', 'wrong_refusal', 'REQUIRED_DATA_RELATION_FAILED'),
    ('MT-05', 'old_version', 'EDITION_BOUNDARY_FAILED'),
])
def test_relation_gate_detects_deliberately_broken_pair(completed, case_id, defect, expected):
    spec, _values, observations = load_case(completed, case_id)
    before, after = deepcopy(observations)
    if defect == 'fact':
        after['result']['found_facts'].pop()
    elif defect == 'window':
        after['context']['window']['end_exclusive'] = '2026-07-20T00:00:01Z'
    elif defect == 'amount_noop':
        after = deepcopy(before)
    elif defect == 'wrong_delta':
        next(fact for fact in after['result']['found_facts'] if fact['evidence'][0]['kind'] == 'sum'
             and fact['evidence'][0]['currency'] == 'RUB')['evidence'][0]['value'] = '1.00'
    elif defect == 'wrong_refusal':
        after['result']['reason_code'] = 'CONFLICTING_DATA'
    else:
        after['result']['applicable_rules'][0]['citation']['rule_version'] = '2024-rev3'
    assert expected in checks.check_relation(spec, before, after)


def test_timezone_relation_preserves_start_boundary_and_excludes_end_boundary(completed):
    _spec, values, observations = load_case(completed, 'MT-02')
    assert values[0]['policy.json']['window'] != values[1]['policy.json']['window']
    for observed in observations:
        ids = {row['tx_id'] for row in observed['context']['transactions']}
        assert 'TX-7700' in ids
        assert 'TX-7701' not in ids


def test_required_record_removal_is_single_source_edit_for_selected_incident(completed):
    spec, values, observations = load_case(completed, 'MT-04')
    before, after = values
    assert [row['tx_id'] for row in before['transactions.json'] if row['client_id'] == 'C-00077'] == ['TX-8900']
    assert after['transactions.json'] == [row for row in before['transactions.json'] if row['tx_id'] != 'TX-8900']
    for name in checks.FILES:
        if name != 'transactions.json':
            assert before[name] == after[name]
    assert observations[1]['context'] is None
    assert observations[1]['ranking'] == []
    assert observations[1]['result']['incident_id'] == spec['incident_id'] == 'INC-000400'


@pytest.mark.parametrize('path', ['qa/datasets/aml-v1', 'src', 'verification/../qa', '../outside'])
def test_reject_output_outside_artifact_scope_before_any_write(tmp_path, monkeypatch, path):
    monkeypatch.setattr(checks, 'ROOT', tmp_path)
    with pytest.raises(ValueError):
        checks.run_metamorphic(Path(path), EqualVectorEmbeddingDouble(), DOUBLE_DIGEST, get_settings())
    assert list(tmp_path.iterdir()) == []


def test_refuse_existing_evidence_without_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, 'ROOT', tmp_path)
    destination = tmp_path / 'verification' / 'taken' / 'metamorphic'
    destination.mkdir(parents=True)
    sentinel = destination / 'keep.json'
    sentinel.write_text('unchanged', encoding='utf-8')
    with pytest.raises(ValueError, match='OUTPUT_EXISTS'):
        checks.run_metamorphic(destination.parent, EqualVectorEmbeddingDouble(), DOUBLE_DIGEST, get_settings())
    assert sentinel.read_text(encoding='utf-8') == 'unchanged'
    assert len(list(destination.iterdir())) == 1


@pytest.mark.parametrize('digest', ['', 'x' * 64, 'a' * 63, None, 123])
def test_invalid_embedding_provenance_rejected_before_write(tmp_path, monkeypatch, digest):
    monkeypatch.setattr(checks, 'ROOT', tmp_path)
    with pytest.raises(ValueError, match='EMBEDDING_INPUT_INVALID'):
        checks.run_metamorphic(tmp_path / 'verification' / 'new', EqualVectorEmbeddingDouble(), digest, get_settings())
    assert not (tmp_path / 'verification').exists()


def test_invalid_frozen_catalog_cannot_produce_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, 'ROOT', tmp_path)
    def corrupt_catalog():
        raise ValueError('MANIFEST_HASH_MISMATCH')
    monkeypatch.setattr(checks, 'load_catalog', corrupt_catalog)
    with pytest.raises(ValueError, match='MANIFEST_HASH_MISMATCH'):
        checks.run_metamorphic(tmp_path / 'verification' / 'new', EqualVectorEmbeddingDouble(), DOUBLE_DIGEST, get_settings())
    assert not (tmp_path / 'verification').exists()


def test_runtime_exception_is_error_not_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, 'ROOT', tmp_path)
    def broken_runtime(*args):
        raise RuntimeError('private-payload-that-must-not-be-persisted')
    monkeypatch.setattr(checks, '_observe', broken_runtime)
    summary = checks.run_metamorphic(tmp_path / 'verification' / 'new', EqualVectorEmbeddingDouble(), DOUBLE_DIGEST, get_settings())
    assert summary['status'] == 'ERROR'
    assert all(row['status'] == 'ERROR' for row in summary['cases'])
    assert 'private-payload' not in json.dumps(summary)
