"""Controlled attacks against evaluator inventory, not the real project."""
import pytest

from qa.dark_guards.integrity import InventoryError, capture, commitment, verify


def test_detects_assertion_removal_and_missing_evaluator(tmp_path):
    (tmp_path / 'checks.py').write_text('assert value == 1\n', encoding='utf-8')
    (tmp_path / 'oracle.json').write_text('{"expected":1}', encoding='utf-8')
    original = capture(tmp_path, ['checks.py', 'oracle.json'])
    seal = commitment(original)
    assert verify(tmp_path, original, seal)['status'] == 'PASS'
    (tmp_path / 'checks.py').write_text('assert True\n', encoding='utf-8')
    (tmp_path / 'oracle.json').unlink()
    result = verify(tmp_path, original, seal)
    assert result['status'] == 'FAIL'
    assert {(entry['path'], entry['reason']) for entry in result['changes']} == {
        ('checks.py', 'CONTENT_CHANGED'), ('oracle.json', 'UNREADABLE_OR_UNSAFE')}


def test_rewriting_inventory_cannot_match_original_external_commitment(tmp_path):
    (tmp_path / 'check.py').write_text('assert value == 1', encoding='utf-8')
    original = capture(tmp_path, ['check.py'])
    seal = commitment(original)
    (tmp_path / 'check.py').write_text('assert True', encoding='utf-8')
    rewritten = capture(tmp_path, ['check.py'])
    with pytest.raises(InventoryError, match='INVENTORY_COMMITMENT_MISMATCH'):
        verify(tmp_path, rewritten, seal)


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'C:/outside', 'a/../b', 'a\\b'])
def test_inventory_rejects_escaping_paths(tmp_path, name):
    with pytest.raises(InventoryError, match='INVALID_PATH'):
        capture(tmp_path, [name])


def test_empty_and_case_aliased_inventory_rejected(tmp_path):
    with pytest.raises(InventoryError):
        capture(tmp_path, [])
    with pytest.raises(InventoryError):
        capture(tmp_path, ['test.py', 'TEST.py'])
