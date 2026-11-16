"""Change planning explains declared impact without fabricating executed evidence."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from qa.impact.planner import ImpactError, build_plan, normalize_changes, validate_map
from qa.requirements.catalog import canonical_digest, load_catalog


ROOT = Path(__file__).resolve().parents[2]
PROFILES = {'full_regression', 'requirements', 'ui_cli', 'browser', 'retrieval',
            'llm', 'judge', 'live_integration', 'documentation', 'mapping_review'}


def change(path, status='M', previous_path=None):
    row = {'status': status, 'path': path}
    if previous_path is not None:
        row['previous_path'] = previous_path
    return row


def changes(*rows, models=()):
    return {'schema_version': '1.0', 'changes': list(rows), 'model_changes': list(models)}


@pytest.fixture
def inputs():
    catalog = load_catalog(ROOT)

    def component(identifier, paths, requirements=(), depends_on=(), profiles=()):
        return {'id': identifier, 'paths': list(paths), 'requirements': list(requirements),
                'depends_on': list(depends_on), 'profiles': list(profiles),
                'reason': 'Synthetic dependency for testing the planner boundary.'}

    mapping = {
        'schema_version': '1.0', 'map_id': 'dark-factory-impact-v1', 'version': '1.0',
        'catalog_sha256': canonical_digest(catalog),
        'components': [
            component('foundation', ['config/', 'src/contracts/'],
                      [row['id'] for row in catalog['requirements']], profiles=['full_regression']),
            component('model_llm', [], ['DFR-004'], ['foundation'], ['llm']),
            component('model_embedding', [], ['DFR-005'], ['foundation'], ['retrieval']),
            component('retrieval', ['src/retrieval/'], ['DFR-005'],
                      ['foundation', 'model_embedding'], ['retrieval']),
            component('workflow', ['src/workflow/controller.py'], ['DFR-001'],
                      ['retrieval', 'model_llm'], ['ui_cli']),
            component('delivery', ['src/workflow/delivery.py'], ['DFR-007'],
                      ['workflow'], ['live_integration']),
            component('documentation', ['docs/operator/'], profiles=['documentation']),
        ],
    }
    return catalog, mapping


def plan(inputs, document):
    catalog, mapping = inputs
    return build_plan(catalog, mapping, document)


def requirement_ids(report):
    return {row['id'] for row in report['requirements']}


def profile_ids(report):
    return {row['id'] for row in report['profiles']}


def assert_not_execution(report):
    assert report['execution_status'] == 'NOT_RUN'
    assert report['requirements_status'] == report['product_acceptance'] == 'INCOMPLETE'
    assert report['candidate_admission'] == 'NOT_EVALUATED'
    assert report['input_completeness'] == 'NOT_VERIFIED'
    assert report['mandatory_full_regression'] is True
    assert 'full_regression' in profile_ids(report)
    assert all(row['status'] == 'NOT_RUN' for row in report['profiles'])


def test_shipped_map_is_bound_to_current_catalogue():
    catalog = load_catalog(ROOT)
    mapping = json.loads((ROOT / 'qa/impact/map-v1.json').read_text('utf-8-sig'))
    validate_map(mapping, catalog)


def test_shipped_map_names_existing_files_and_component_directories():
    mapping = json.loads((ROOT / 'qa/impact/map-v1.json').read_text('utf-8-sig'))
    for component in mapping['components']:
        for pattern in component['paths']:
            path = ROOT / pattern.rstrip('/')
            assert path.is_dir() if pattern.endswith('/') else path.is_file(), (component['id'], pattern)


def test_shared_http_client_change_includes_its_kafka_consumer():
    catalog = load_catalog(ROOT)
    mapping = json.loads((ROOT / 'qa/impact/map-v1.json').read_text('utf-8-sig'))
    report = build_plan(catalog, mapping, changes(change('src/workflow/remote.py')))
    assert report['planning_status'] == 'READY'
    assert {'kafka', 'ui'} <= set(report['affected_components'])
    assert {'DFR-008', 'DFR-009'} <= requirement_ids(report)
    assert 'live_integration' in profile_ids(report)
    assert_not_execution(report)


def test_ui_only_change_does_not_require_unrelated_live_model_profiles():
    catalog = load_catalog(ROOT)
    mapping = json.loads((ROOT / 'qa/impact/map-v1.json').read_text('utf-8-sig'))
    report = build_plan(catalog, mapping, changes(change('src/ui/app.py')))
    assert report['planning_status'] == 'READY'
    assert {'ui_cli', 'browser', 'full_regression'} <= profile_ids(report)
    assert not {'llm', 'judge'}.intersection(profile_ids(report))
    assert_not_execution(report)


def test_impact_reaches_transitive_consumers_and_explains_the_chain(inputs):
    report = plan(inputs, changes(change('src/retrieval/search.py')))
    assert report['planning_status'] == 'READY'
    assert report['unknown_paths'] == []
    assert set(report['affected_components']) == {'retrieval', 'workflow', 'delivery'}
    assert requirement_ids(report) == {'DFR-005', 'DFR-001', 'DFR-007'}
    delivery = next(row for row in report['requirements'] if row['id'] == 'DFR-007')
    assert any(reason['chain'] == ['retrieval', 'workflow', 'delivery']
               for reason in delivery['reasons'])
    assert {'retrieval', 'ui_cli', 'live_integration'} <= profile_ids(report)
    assert_not_execution(report)


def test_consumer_change_does_not_invent_reverse_impact_on_its_dependencies(inputs):
    report = plan(inputs, changes(change('src/workflow/delivery.py')))
    assert set(report['affected_components']) == {'delivery'}
    assert requirement_ids(report) == {'DFR-007'}
    assert_not_execution(report)


def test_shared_contract_change_keeps_all_requirement_checks(inputs):
    catalog, _ = inputs
    report = plan(inputs, changes(change('src/contracts/v1.py')))
    assert requirement_ids(report) == {row['id'] for row in catalog['requirements']}
    expected = {selector for row in catalog['requirements'] for scenario in row['scenarios']
                for selector in scenario['test_selectors']}
    assert set(report['selected_test_selectors']) == expected
    assert_not_execution(report)


@pytest.mark.parametrize('path', ['src/new_domain/handler.py', 'src/retrieval_extra/search.py'])
def test_unknown_or_prefix_lookalike_is_incomplete_and_keeps_full_fallback(inputs, path):
    catalog, _ = inputs
    report = plan(inputs, changes(change(path, 'A')))
    assert report['planning_status'] == 'INCOMPLETE'
    assert report['unknown_paths'] == [path]
    assert requirement_ids(report) == {row['id'] for row in catalog['requirements']}
    assert profile_ids(report) == PROFILES
    assert_not_execution(report)


@pytest.mark.parametrize('status', ['A', 'D'])
def test_absent_added_or_deleted_path_can_be_planned_without_reading_it(inputs, status):
    path = 'src/retrieval/does_not_exist_for_planner_test.py'
    assert not (ROOT / path).exists()
    report = plan(inputs, changes(change(path, status)))
    assert report['planning_status'] == 'READY'
    assert requirement_ids(report) == {'DFR-005', 'DFR-001', 'DFR-007'}
    assert_not_execution(report)


def test_rename_keeps_old_path_impact_and_does_not_hide_unknown_new_destination(inputs):
    report = plan(inputs, changes(change('src/new_domain/moved.py', 'R', 'src/workflow/delivery.py')))
    assert report['planning_status'] == 'INCOMPLETE'
    assert report['unknown_paths'] == ['src/new_domain/moved.py']
    assert 'delivery' in report['affected_components']
    assert 'DFR-007' in requirement_ids(report)
    assert_not_execution(report)


def test_known_rename_is_union_of_both_components(inputs):
    report = plan(inputs, changes(change('docs/operator/delivery.md', 'R', 'src/workflow/delivery.py')))
    assert report['planning_status'] == 'READY'
    assert set(report['affected_components']) == {'delivery', 'documentation'}
    assert requirement_ids(report) == {'DFR-007'}
    assert {'documentation', 'live_integration'} <= profile_ids(report)


def test_case_only_rename_keeps_impact_on_windows(inputs):
    report = plan(inputs, changes(change('src/workflow/DELIVERY.py', 'R', 'src/workflow/delivery.py')))
    assert report['planning_status'] == 'READY'
    assert requirement_ids(report) == {'DFR-007'}


def test_changed_mapped_test_selects_its_requirements_but_is_not_test_evidence(inputs):
    catalog, _ = inputs
    path = 'tests/workflow/test_controller.py'
    expected = {row['id'] for row in catalog['requirements']
                if any(selector.split('::')[0] == path
                       for scenario in row['scenarios'] for selector in scenario['test_selectors'])}
    assert expected
    report = plan(inputs, changes(change(path)))
    assert report['planning_status'] == 'READY'
    assert report['unknown_paths'] == []
    assert requirement_ids(report) == expected
    assert_not_execution(report)


def test_changed_expectation_source_keeps_its_requirement_visible(inputs):
    catalog, _ = inputs
    path = 'docs/rebuild/retrieval-v1.md'
    expected = {row['id'] for row in catalog['requirements'] if row['expectation_source']['path'] == path}
    report = plan(inputs, changes(change(path)))
    assert report['planning_status'] == 'READY'
    assert report['unknown_paths'] == []
    assert requirement_ids(report) == expected
    assert 'mapping_review' in profile_ids(report)
    assert_not_execution(report)


@pytest.mark.parametrize('model,profile,requirement', [
    ('llm', 'llm', 'DFR-004'), ('embedding', 'retrieval', 'DFR-005'),
])
def test_declared_external_model_change_requires_live_measurement_in_the_plan(inputs, model, profile, requirement):
    report = plan(inputs, changes(models=[model]))
    assert report['planning_status'] == 'READY'
    assert profile in profile_ids(report)
    assert requirement in requirement_ids(report)
    assert_not_execution(report)


@pytest.mark.parametrize('document', [changes(), changes(change('docs/operator/guide.md'))])
def test_empty_or_documentation_only_input_never_means_product_acceptance(inputs, document):
    report = plan(inputs, document)
    assert report['planning_status'] == 'READY'
    assert report['selected_test_selectors'] == []
    assert requirement_ids(report) == set()
    assert_not_execution(report)


def test_equivalent_input_order_produces_same_plan_and_identity(inputs):
    rows = [change('src/workflow/delivery.py'), change('src/retrieval/search.py'), change('docs/operator/guide.md')]
    first = plan(inputs, changes(*rows, models=['llm', 'embedding']))
    second = plan(inputs, changes(*reversed(rows), models=['embedding', 'llm']))
    for field in ('requirements', 'affected_components', 'selected_test_selectors', 'profiles',
                  'unknown_paths', 'change_set_sha256'):
        assert first[field] == second[field]


def test_planning_neither_mutates_inputs_nor_opens_changed_files_or_starts_a_process(inputs, monkeypatch):
    catalog, mapping = inputs
    document = changes(change('src/retrieval/search.py'))
    original = deepcopy((catalog, mapping, document))

    def forbidden(*args, **kwargs):
        raise AssertionError('Planning must not execute checks or read changed source content')

    monkeypatch.setattr(Path, 'read_text', forbidden)
    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(pytest, 'main', forbidden)
    report = build_plan(catalog, mapping, document)
    assert (catalog, mapping, document) == original
    assert_not_execution(report)


def test_windows_separators_are_normalized_before_matching(inputs):
    normalized = normalize_changes(changes(change(r'src\workflow\delivery.py')))
    assert normalized['changes'][0]['path'] == 'src/workflow/delivery.py'
    assert requirement_ids(plan(inputs, normalized)) == {'DFR-007'}


@pytest.mark.parametrize('path', [
    '../outside.py', 'src/../outside.py', '/outside.py', r'C:\outside.py',
    r'\\server\share\outside.py', 'src/file.py:secret', 'src/CON.py',
    'src/file.py\n--execute', '', 'src/folder/',
])
def test_invalid_declared_paths_are_rejected_without_filesystem_access(path):
    with pytest.raises(ImpactError):
        normalize_changes(changes(change(path)))


@pytest.mark.parametrize('document', [
    {'schema_version': '1.0', 'changes': [], 'model_changes': [], 'automatic_acceptance': True},
    changes({'status': 'X', 'path': 'src/file.py'}),
    changes({'status': 'M', 'path': 'src/file.py', 'previous_path': 'old.py'}),
    changes({'status': 'R', 'path': 'src/file.py'}),
    changes(change('src/file.py', 'R', 'src/file.py')),
    changes(change('src/file.py'), change(r'src\file.py')),
    changes(change('src/file.py'), change('SRC/FILE.py')),
    changes(models=['unknown_model']),
    changes(models=['llm', 'llm']),
])
def test_ambiguous_or_undeclared_change_contract_cannot_be_silently_accepted(document):
    with pytest.raises(ImpactError):
        normalize_changes(document)


@pytest.mark.parametrize('defect', ['policy_pin', 'unknown_component', 'cycle', 'unknown_requirement',
                                   'uncovered_requirement', 'duplicate_component', 'unknown_profile',
                                   'wildcard_path', 'unexpected_field', 'missing_model_seed'])
def test_invalid_dependency_map_cannot_produce_a_ready_plan(inputs, defect):
    catalog, mapping = inputs
    if defect == 'policy_pin':
        mapping['catalog_sha256'] = '0' * 64
    elif defect == 'unknown_component':
        mapping['components'][3]['depends_on'].append('missing')
    elif defect == 'cycle':
        mapping['components'][0]['depends_on'].append('delivery')
    elif defect == 'unknown_requirement':
        mapping['components'][0]['requirements'].append('DFR-999')
    elif defect == 'uncovered_requirement':
        mapping['components'][0]['requirements'].remove('DFR-012')
    elif defect == 'duplicate_component':
        mapping['components'].append(deepcopy(mapping['components'][3]))
    elif defect == 'unknown_profile':
        mapping['components'][3]['profiles'].append('auto_deploy')
    elif defect == 'wildcard_path':
        mapping['components'][3]['paths'] = ['src/**']
    elif defect == 'unexpected_field':
        mapping['components'][3]['approve_product'] = True
    else:
        mapping['components'] = [component for component in mapping['components'] if component['id'] != 'model_llm']
        mapping['components'][3]['depends_on'] = ['retrieval']
    with pytest.raises(ImpactError):
        validate_map(mapping, catalog)


def test_catalogue_semantic_change_requires_explicit_mapping_reconciliation(inputs):
    catalog, mapping = inputs
    catalog['requirements'][0]['statement'] = 'A different contract requiring deliberate review.'
    with pytest.raises(ImpactError):
        validate_map(mapping, catalog)


@pytest.mark.parametrize('identifier', ['model_llm', 'model_embedding'])
def test_model_seed_cannot_lose_its_mandatory_live_profile(inputs, identifier):
    catalog, mapping = inputs
    next(row for row in mapping['components'] if row['id'] == identifier)['profiles'] = []
    with pytest.raises(ImpactError, match='MODEL_PROFILE_REQUIRED'):
        build_plan(catalog, mapping, changes(models=['llm' if identifier == 'model_llm' else 'embedding']))
