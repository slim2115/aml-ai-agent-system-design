"""Offline controls for response durability and locally replayed identity checks."""
from hashlib import sha256
import json

import pytest

from qa.dark_judge_injection import live_v2
from qa.tools.run_llm import EXPECTED_DIGEST


def _identity(server='0.1', model_info='configuration'):
    return {'server': {'version': server}, 'model': {'digest': EXPECTED_DIGEST},
            'show': {'details': model_info}}


def _writer(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def _assessment(*_args):
    return {'delivery': 'COMPLETE', 'contract': 'VALID', 'verdict': 'fail',
            'judge_against_known_defect': 'PASS', 'request_sha256': 'a' * 64}


def test_first_response_is_saved_before_postcall_identity_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(live_v2, 'assess', _assessment)
    class Transport:
        def complete(self, payload, rubric, *, deadline):
            return {'status': 'completed', 'response_text': 'synthetic first answer'}
    snapshot = live_v2.identity_snapshot(_identity())
    def failed_identity():
        saved = json.loads((tmp_path / 'attack-raw.json').read_text(encoding='utf-8'))
        assert saved['transport']['response_text'] == 'synthetic first answer'
        raise RuntimeError('metadata unavailable')
    _, after, result = live_v2.execute_attempt(
        tmp_path, 'attack', {}, {}, snapshot, snapshot, Transport(), [], failed_identity,
        deadline=100, writer=_writer)
    assert after is None
    assert result['identity'] == 'INCOMPLETE'
    assert result['judge_against_known_defect'] == 'INCOMPLETE'


def test_identity_records_only_hashes_and_compares_all_three_snapshots():
    original = live_v2.identity_snapshot(_identity(model_info='sensitive metadata'))
    changed = live_v2.identity_snapshot(_identity(server='0.2'))
    assert 'sensitive metadata' not in json.dumps(original)
    assert live_v2.compare_identity(original, original, original) == 'STABLE'
    assert live_v2.compare_identity(original, original, changed) == 'CHANGED'
    assert live_v2.compare_identity(original, original, None) == 'INCOMPLETE'
    bad = dict(original, server_sha256='not-a-hash')
    assert live_v2.compare_identity(original, original, bad) == 'INCOMPLETE'


def _saved_fixture(tmp_path, monkeypatch, *, invalid=False):
    import qa.judge.dataset
    root = tmp_path / 'project'
    root.mkdir()
    rubric_path = root / 'qa' / 'judge' / 'rubric-v1.json'
    rubric_path.parent.mkdir(parents=True)
    rubric_path.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(live_v2, 'ROOT', root)
    case = {'id': 'J-003', 'expected_verdict': 'fail',
            'programmatic_gate': {'status': 'FAIL', 'issues': []},
            'payload': {'sources': []}}
    monkeypatch.setattr(qa.judge.dataset, 'load_dataset', lambda: {'cases': [case]})
    # The attacking case is deliberately invalid while the neutral case remains valid.
    from qa.dark_judge_injection.probe import ATTACK
    def fake_assess(payload, text, record, captured, rubric):
        result = _assessment()
        if invalid and text == ATTACK:
            result.update(contract='INVALID', verdict=None, judge_against_known_defect='INCOMPLETE')
        return result
    monkeypatch.setattr(live_v2, 'assess', fake_assess)
    out = root / 'run'
    out.mkdir()
    identity = live_v2.identity_snapshot(_identity())
    source_hashes = {'source': 'fixed'}
    rows = []
    for variant, text in live_v2.VARIANTS:
        payload = live_v2.make_payload(case['payload'], text)
        payload_hash = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode('utf-8')).hexdigest()
        _writer(out / f'{variant}-start.json', {'status': 'REQUEST_MAY_BE_IN_FLIGHT', 'attempt': 1,
                 'identity_before': identity, 'payload_sha256': payload_hash})
        _writer(out / f'{variant}-raw.json', {'variant': variant, 'planned_attempts': 1, 'attempt': 1,
                 'initial_identity': identity, 'identity_before': identity, 'transport': {}, 'wire': []})
        assessment = fake_assess(payload, text, {}, [], {})
        assessment['identity'] = 'STABLE'
        item = {'variant': variant, 'planned_attempts': 1, 'attempt': 1, 'assessment': assessment,
                'initial_identity': identity, 'identity_before': identity, 'identity_after': identity,
                'expected_verdict': 'fail', 'programmatic_gate': case['programmatic_gate']}
        _writer(out / f'{variant}-result.json', item)
        rows.append({'variant': variant, **assessment})
    _writer(out / 'child-result.json', {'source_sha256_before': source_hashes,
            'source_sha256_after': source_hashes, 'initial_identity': identity,
            'historical_judge_status': 'FAIL', 'cases': rows,
            'measurement_status': 'INCOMPLETE' if invalid else 'PASS'})
    return out, source_hashes


def test_offline_replay_recomputes_identity_and_rejects_changed_snapshot(tmp_path, monkeypatch):
    out, hashes = _saved_fixture(tmp_path, monkeypatch)
    assert live_v2.validate_saved(out, hashes, hashes)[0] == 'PASS'
    path = out / 'attack-result.json'
    item = json.loads(path.read_text(encoding='utf-8'))
    item['identity_after']['server_sha256'] = 'b' * 64
    _writer(path, item)
    with pytest.raises(ValueError, match='INCONSISTENT_ASSESSMENT'):
        live_v2.validate_saved(out, hashes, hashes)


def test_invalid_first_answer_stays_incomplete(tmp_path, monkeypatch):
    out, hashes = _saved_fixture(tmp_path, monkeypatch, invalid=True)
    status, rows = live_v2.validate_saved(out, hashes, hashes)
    assert status == 'INCOMPLETE'
    assert rows[1]['contract'] == 'INVALID'
    assert rows[1]['judge_against_known_defect'] == 'INCOMPLETE'


def test_offline_replay_keeps_saved_answer_incomplete_when_identity_is_unavailable(tmp_path, monkeypatch):
    out, hashes = _saved_fixture(tmp_path, monkeypatch)
    path = out / 'neutral-result.json'
    item = json.loads(path.read_text(encoding='utf-8'))
    item['identity_after'] = None
    item['assessment']['identity'] = 'INCOMPLETE'
    item['assessment']['judge_against_known_defect'] = 'INCOMPLETE'
    _writer(path, item)
    child_path = out / 'child-result.json'
    child = json.loads(child_path.read_text(encoding='utf-8'))
    child['cases'] = [{'variant': 'neutral', **item['assessment']}]
    child['measurement_status'] = 'INCOMPLETE'
    _writer(child_path, child)
    status, rows = live_v2.validate_saved(out, hashes, hashes)
    assert status == 'INCOMPLETE'
    assert rows[0]['delivery'] == 'COMPLETE'
    assert rows[0]['identity'] == 'INCOMPLETE'
    assert (out / 'neutral-raw.json').exists()
