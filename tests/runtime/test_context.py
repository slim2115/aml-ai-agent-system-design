"""Data-boundary tests use copied fixtures, never edit the shipped demo."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

import src.runtime.context as context_module
import src.runtime.policy as policy_module
from src.runtime.context import ContextBuilder, ContextFailure
from src.runtime.policy import DemoPolicy, PolicyFailure, load_demo_policy, strict_json

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    folder = tmp_path / 'snapshot'
    shutil.copytree(ROOT / 'data/demo-v1', folder)
    policy = json.loads((ROOT / 'config/demo-policy.json').read_text(encoding='utf-8'))
    monkeypatch.setattr(context_module, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(policy_module, 'PROJECT_ROOT', tmp_path)
    return folder, policy


def reseal(bundle, name=None, edit=None, raw=None, manifest_edit=None):
    """An intentional new test fixture with a matching explicit test policy."""
    folder, policy = bundle
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if name:
        path = folder / name
        rows = json.loads(path.read_text(encoding='utf-8'))
        if edit:
            edit(rows)
        data = raw if raw is not None else json.dumps(rows, ensure_ascii=False).encode()
        path.write_bytes(data)
        descriptor = next(f for f in manifest['files'] if f['name'] == name)
        descriptor.update(content_sha256=sha256(data).hexdigest(), row_count=len(rows))
    if manifest_edit:
        manifest_edit(manifest)
    data = json.dumps(manifest).encode()
    (folder / 'manifest.json').write_bytes(data)
    policy['snapshot_manifest_sha256'] = sha256(data).hexdigest()


def build(bundle, incident='INC-000123'):
    return ContextBuilder(bundle[0], DemoPolicy.model_validate(bundle[1])).build(incident)


def fails(bundle, code, incident='INC-000123'):
    with pytest.raises(ContextFailure) as failure:
        build(bundle, incident)
    assert failure.value.code == code
    assert 'TX-' not in str(failure.value) and 'Системная' not in str(failure.value)


def test_context_minimizes_identity_and_preserves_decimal_strings(bundle):
    result = build(bundle)
    context = result.context
    assert len(context.transactions) == 12
    assert context.transactions[0].amount == '95000.00'
    assert context.window.end_exclusive == '2026-07-20T00:00:00Z'
    assert {'full_name', 'inn', 'pan_masked', 'issue_date'}.isdisjoint(context.client.model_dump())
    assert set(context.cards[0].model_dump()) == {'card_id', 'client_id', 'card_status'}
    assert result.rule_corpus_sha256 == sha256(result.rule_corpus_path.read_bytes()).hexdigest()
    assert len(context.snapshot.sources) == 5


@pytest.mark.parametrize('value,code', [(None, 'MISSING_INPUT'), ('', 'MISSING_INPUT'),
    (' INC-000123', 'INVALID_INPUT_FORMAT'), ('INC-１２３４５６', 'INVALID_INPUT_FORMAT'),
    ('INC-000123\n', 'INVALID_INPUT_FORMAT'), (123, 'INVALID_INPUT_FORMAT'),
    ('INC-999999', 'NOT_FOUND'), ('INC-000999', 'INSUFFICIENT_DATA')])
def test_input_and_incompleteness_are_distinct(bundle, value, code):
    fails(bundle, code, value)


@pytest.mark.parametrize('name', ['cases.json', 'clients.json', 'cards.json', 'transactions.json'])
def test_duplicate_primary_keys_fail_even_outside_selected_client(bundle, name):
    reseal(bundle, name, lambda rows: rows.append(deepcopy(rows[-1])))
    fails(bundle, 'CONFLICTING_DATA')


@pytest.mark.parametrize('edit', [lambda rows: rows[-1].update(card_id='CARD-1001'),
    lambda rows: rows[-1].update(card_id='CARD-MISSING'),
    lambda rows: rows[-1].update(client_id='C-MISSING'),
    lambda rows: rows[-1].update(amount='0.001'),
    lambda rows: rows[-1].update(currency='JPY'),
    lambda rows: rows[-1].update(timestamp='2026-07-18T16:45:00'),
    lambda rows: rows[-1].update(timestamp='2027-01-01T00:00:00Z'),
    lambda rows: rows[-1].update(amount=0.1),
    lambda rows: rows[-1].update(amount=100),
    lambda rows: rows[-1].update(amount='1e2')])
def test_invalid_foreign_rows_are_never_silently_filtered(bundle, edit):
    reseal(bundle, 'transactions.json', edit)
    fails(bundle, 'CONFLICTING_DATA')


def test_conflicting_card_owner_is_detected(bundle):
    reseal(bundle, 'cards.json', lambda rows: rows[0].update(client_id='C-00055'))
    fails(bundle, 'CONFLICTING_DATA')


def test_missing_profile_without_transactions_is_insufficient(bundle):
    reseal(bundle, 'clients.json', lambda rows: rows.pop())
    fails(bundle, 'INSUFFICIENT_DATA')


def test_window_boundaries_are_inclusive_then_exclusive(bundle):
    start, end = bundle[1]['window']['start_inclusive'], bundle[1]['window']['end_exclusive']
    def edit(rows):
        rows[0]['timestamp'] = start
        rows[1]['timestamp'] = end
    reseal(bundle, 'transactions.json', edit)
    selected = {t.tx_id for t in build(bundle).context.transactions}
    assert 'TX-7700' in selected and 'TX-7701' not in selected and len(selected) == 11


def test_capture_lag_does_not_admit_incident_created_after_as_of(bundle):
    bundle[1]['max_snapshot_age_seconds'] = 3600
    reseal(bundle, 'cases.json', lambda rows: rows[0].update(created_at='2026-07-20T00:30:00Z'),
           manifest_edit=lambda m: m.update(captured_at='2026-07-20T01:00:00Z'))
    fails(bundle, 'CONFLICTING_DATA')


@pytest.mark.parametrize('client_since,accepted', [('2026-07-20', True), ('2026-07-21', False)])
def test_client_since_cannot_follow_as_of_utc_calendar_date(bundle, client_since, accepted):
    reseal(bundle, 'clients.json', lambda rows: rows[0].update(client_since=client_since))
    if accepted:
        assert build(bundle).context.client.client_since == client_since
    else:
        fails(bundle, 'CONFLICTING_DATA')


@pytest.mark.parametrize('captured', ['2026-07-19T23:59:59Z', '2026-07-20T00:00:01Z'])
def test_replay_snapshot_capture_policy_is_checked(bundle, captured):
    reseal(bundle, manifest_edit=lambda m: m.update(captured_at=captured))
    fails(bundle, 'NOT_READY')


def test_source_change_invalidates_existing_builder_instead_of_cached_return(bundle):
    builder = ContextBuilder(bundle[0], DemoPolicy.model_validate(bundle[1]))
    assert builder.build('INC-000123').context.transactions
    path = bundle[0] / 'transactions.json'
    path.write_bytes(path.read_bytes().replace(b'95000.00', b'95000.01', 1))
    with pytest.raises(ContextFailure) as failure:
        builder.build('INC-000123')
    assert failure.value.code == 'NOT_READY'


def test_rule_corpus_update_is_also_bound_to_manifest(bundle):
    path = bundle[0] / 'rules.json'
    path.write_bytes(path.read_bytes() + b' ')
    fails(bundle, 'NOT_READY')


def test_duplicate_json_keys_rejected_even_if_hash_is_resealed(bundle):
    data = (bundle[0] / 'transactions.json').read_bytes()
    data = data.replace(b'"tx_id": "TX-7700"', b'"tx_id": "OTHER", "tx_id": "TX-7700"', 1)
    reseal(bundle, 'transactions.json', raw=data)
    fails(bundle, 'CONFLICTING_DATA')


def test_manifest_replacement_mid_read_is_rejected(bundle, monkeypatch):
    builder = ContextBuilder(bundle[0], DemoPolicy.model_validate(bundle[1]))
    original = builder._bytes
    manifest_reads = 0
    def changing(name, limit=16 * 1024 * 1024):
        nonlocal manifest_reads
        data = original(name, limit)
        if name == 'manifest.json':
            manifest_reads += 1
            if manifest_reads == 2:
                return data + b' '
        return data
    monkeypatch.setattr(builder, '_bytes', changing)
    with pytest.raises(ContextFailure) as failure:
        builder.build('INC-000123')
    assert failure.value.code == 'NOT_READY'


def test_missing_file_is_safe_readiness_error(bundle):
    (bundle[0] / 'cards.json').unlink()
    fails(bundle, 'NOT_READY')


def test_policy_requires_explicit_unapproved_synthetic_purpose(bundle):
    path = bundle[0].parent / 'policy.json'
    path.write_text(json.dumps(bundle[1]), encoding='utf-8')
    assert load_demo_policy(path).expert_approved is False
    changed = deepcopy(bundle[1])
    changed['expert_approved'] = True
    path.write_text(json.dumps(changed), encoding='utf-8')
    with pytest.raises(PolicyFailure):
        load_demo_policy(path)


@pytest.mark.parametrize('value', [b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":0.1}'])
def test_strict_source_json_forbids_ambiguous_numbers_or_keys(value):
    with pytest.raises(ValueError):
        strict_json(value)
