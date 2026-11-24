"""Fresh pack ownership and fail-closed evidence reading."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from qa.autonomy import demo
from qa.investigations import runner, storage
from qa.investigations.core import LearningError
from src.workflow import store as workflow_store
from tests.dark_factory.test_execution_provenance import project, write

REAL_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def built(project, monkeypatch):
    # Only the fixed synthetic candidate corpus comes from the checkout. All
    # writable artifacts, SQLite and provenance are isolated inside this fixture.
    original = demo.prepare_cases
    monkeypatch.setattr(demo, 'prepare_cases', lambda ids, root: original(ids, REAL_ROOT))
    # These tests exercise pack isolation and consistency. Real CLI verification
    # records installed-distribution identities once; no need to rehash them per
    # malformed-artifact case in this hermetic fixture.
    monkeypatch.setattr(runner, 'environment', lambda: {'digest': 'b' * 64})
    monkeypatch.setattr(workflow_store, 'WORKFLOW_ROOT', project / 'var/workflow')
    for name in ('qa/investigations/catalog-v1.json', 'qa/environments/catalog-v1.json'):
        write(project, name, (REAL_ROOT / name).read_text('utf-8'))
    assert runner.build('sample', project)['status'] == 'PASS'
    return project


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')


def repin(root, name):
    from qa.execution.runner import pin_file
    folder = storage.pack_path('sample', root)
    manifest = json.loads((folder / 'evidence.json').read_text('utf-8'))
    pin = pin_file(folder / name, root)
    manifest['files'] = [pin if row['path'] == pin['path'] else row for row in manifest['files']]
    save(folder / 'evidence.json', manifest)


def test_real_pack_is_reproducible_readonly_and_does_not_claim_human_acceptance(built):
    folder = storage.pack_path('sample', built)
    before = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    view = storage.load_pack('sample', built)
    assert view['assessment_enabled'] and view['source_check'] == 'MATCH'
    assert len(view['lessons']) == 6
    assert view['operator_practice'] == 'NOT_MEASURED'
    assert view['product_acceptance'] == 'INCOMPLETE'
    assert storage.list_packs(built) == ['sample']
    assert before == {p: p.read_bytes() for p in before}


def test_stale_source_keeps_readonly_facts_but_disables_assessment(built):
    write(built, 'src/service.py', 'changed source')
    view = storage.load_pack('sample', built)
    assert view['source_check'] == 'DIFFERENT' and not view['assessment_enabled']
    assert len(view['lessons']) == 6


@pytest.mark.parametrize('name', ['bundle.json', 'catalog.json', 'result.json', 'observations.json',
                                  'source-before.json', 'fixtures/checksum-control.json'])
def test_changed_or_missing_evidence_is_rejected(built, name):
    path = storage.pack_path('sample', built) / name
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError):
        storage.load_pack('sample', built)


def test_duplicate_outside_and_missing_pins_are_rejected(built):
    path = storage.pack_path('sample', built) / 'evidence.json'
    original = json.loads(path.read_text('utf-8'))
    for kind in ('duplicate', 'outside', 'missing'):
        value = deepcopy(original)
        if kind == 'duplicate':
            value['files'][1] = value['files'][0]
        elif kind == 'outside':
            value['files'][0]['path'] = '.env'
        else:
            value['files'].pop()
        save(path, value)
        with pytest.raises(ValueError):
            storage.load_pack('sample', built)


def test_repinned_fabricated_projection_is_rejected(built):
    from qa.execution.runner import pin_file
    folder = storage.pack_path('sample', built)
    path = folder / 'bundle.json'
    value = json.loads(path.read_text('utf-8'))
    value[0]['facts'][0]['value'] = 'invented PASS'
    save(path, value)
    manifest = json.loads((folder / 'evidence.json').read_text('utf-8'))
    manifest['files'] = [pin_file(path, built) if row['path'].endswith('/bundle.json') else row
                         for row in manifest['files']]
    save(folder / 'evidence.json', manifest)
    with pytest.raises(ValueError, match='LEARNING_FACTS_CHANGED'):
        storage.load_pack('sample', built)


def test_repinned_fixture_must_match_the_recorded_integrity_observation(built):
    path = storage.pack_path('sample', built) / 'fixtures/checksum-control.json'
    save(path, {'synthetic_training_copy': True, 'value': 3})
    repin(built, 'fixtures/checksum-control.json')
    with pytest.raises(ValueError, match='LEARNING_FIXTURE_BINDING'):
        storage.load_pack('sample', built)


def test_integrity_observations_cannot_refer_to_another_run(built):
    path = storage.pack_path('sample', built) / 'observations.json'
    value = json.loads(path.read_text('utf-8'))
    wrong = 'var/qa/investigations/another/fixtures/checksum-control.json'
    value['integrity']['before_pin']['path'] = wrong
    value['integrity']['after_pin']['path'] = wrong
    value['integrity']['after_verification']['changed_or_missing'] = [wrong]
    save(path, value)
    repin(built, 'observations.json')
    with pytest.raises(ValueError, match='LEARNING_FIXTURE_BINDING'):
        storage.load_pack('sample', built)


@pytest.mark.parametrize(('field', 'value', 'error'), [
    ('schema_version', '2.0', 'LEARNING_RESULT'),
    ('lesson_count', True, 'LEARNING_RESULT'),
    ('lesson_count', 5, 'LEARNING_RESULT'),
    ('new_model_calls', False, 'LEARNING_RESULT'),
    ('new_model_calls', 1, 'LEARNING_RESULT'),
    ('operator_practice', 'PASS', 'LEARNING_RESULT'),
    ('product_acceptance', 'PASS', 'LEARNING_RESULT'),
    ('source_digest', '0' * 64, 'LEARNING_RESULT_SOURCE'),
    ('finished_at_utc', 'not-a-timestamp', 'LEARNING_RESULT_TIME'),
    ('finished_at_utc', '2026-11-21T00:00:00', 'LEARNING_RESULT_TIME'),
    ('finished_at_utc', '2026-11-21T00:00:00+03:00', 'LEARNING_RESULT_TIME'),
    ('injected_metadata', 'unexpected', 'LEARNING_RESULT'),
])
def test_repinned_result_metadata_is_checked_and_bound_to_source(built, field, value, error):
    path = storage.pack_path('sample', built) / 'result.json'
    result = json.loads(path.read_text('utf-8'))
    result[field] = value
    save(path, result)
    repin(built, 'result.json')
    with pytest.raises(ValueError, match=error):
        storage.load_pack('sample', built)


def test_new_build_never_overwrites_existing_pack_or_workspace(built):
    with pytest.raises(LearningError, match='LEARNING_OUTPUT_EXISTS'):
        runner.build('sample', built)
    folder = built / 'var/workflow/investigations/orphan'
    folder.mkdir()
    with pytest.raises(LearningError, match='LEARNING_OUTPUT_EXISTS'):
        runner.build('orphan', built)
    assert not storage.pack_path('orphan', built).exists()


def test_environment_change_during_build_prevents_a_completed_pack(built, monkeypatch):
    identities = iter(({'digest': 'b' * 64}, {'digest': 'c' * 64}))
    monkeypatch.setattr(runner, 'environment', lambda: next(identities))
    with pytest.raises(LearningError, match='LEARNING_VERSION_CHANGED'):
        runner.build('changed-env', built)
    folder = storage.pack_path('changed-env', built)
    assert json.loads((folder / 'error.json').read_text('utf-8'))['status'] == 'ERROR'
    assert not (folder / 'result.json').exists()
    assert not (folder / 'evidence.json').exists()


@pytest.mark.parametrize('name', ['../escape', 'a/b', 'a\\b', '', '.hidden', 'x' * 65])
def test_invalid_run_ids_cannot_select_paths(project, name):
    with pytest.raises(ValueError):
        runner.build(name, project)
