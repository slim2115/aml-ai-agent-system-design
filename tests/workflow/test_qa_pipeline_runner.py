"""A reduced, duplicated or wrong selection must not qualify the live profile."""
import xml.etree.ElementTree as ET

import pytest

from qa.tools.run_pipeline import collection_complete, expected_cases


def junit(tmp_path, names):
    root = ET.Element('testsuite')
    for name in names:
        ET.SubElement(root, 'testcase', name=name)
    path = tmp_path / 'tests.xml'
    ET.ElementTree(root).write(path, encoding='utf-8')
    return path


@pytest.mark.parametrize('suite,count', [('chain', 5), ('faults', 7), ('all', 12)])
def test_full_scope_requires_every_expected_case(tmp_path, suite, count):
    names = sorted(expected_cases(suite))
    assert len(names) == count
    assert collection_complete(junit(tmp_path, names), suite)
    assert not collection_complete(junit(tmp_path, names[:-1]), suite)
    assert not collection_complete(junit(tmp_path, names[:-1] + [names[0]]), suite)
    assert not collection_complete(junit(tmp_path, names + ['unexpected_test']), suite)


def test_parametrized_ack_selection_requires_both_boundaries(tmp_path):
    name = 'test_lost_kafka_commit_ack_preserves_intake_and_recovers'
    expected = sorted(expected_cases('faults', name))
    assert len(expected) == 2
    assert collection_complete(junit(tmp_path, expected), 'faults', name)
    assert not collection_complete(junit(tmp_path, expected[:1]), 'faults', name)


def test_empty_or_unknown_selection_is_not_complete(tmp_path):
    assert not collection_complete(tmp_path / 'absent.xml', 'all')
    assert not collection_complete(junit(tmp_path, []), 'all')
    assert not collection_complete(junit(tmp_path, ['test_unknown']), 'chain', 'test_unknown')
