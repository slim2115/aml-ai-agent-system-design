"""Requirements mapping never turns absent or partial execution into acceptance."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qa.requirements.catalog import RequirementError, canonical_digest, load_catalog, validate_catalog
from qa.requirements.execution import Recorder, build_report


ROOT = Path(__file__).resolve().parents[2]
POSITIVE = 'tests/dark_factory/test_sample.py::test_positive'
NEGATIVE = 'tests/dark_factory/test_sample.py::test_negative'


@pytest.fixture
def catalogue(tmp_path):
    policy_path = tmp_path / 'qa/strategy/quality-policy.json'
    policy_path.parent.mkdir(parents=True)
    policy_bytes = (ROOT / 'qa/strategy/quality-policy.json').read_bytes()
    policy_path.write_bytes(policy_bytes)
    (tmp_path / 'pyproject.toml').write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests/dark_factory"]\n', encoding='utf-8')
    source = tmp_path / 'tests/dark_factory/test_sample.py'
    source.parent.mkdir(parents=True)
    source.write_text(
        "raise RuntimeError('CATALOG_VALIDATION_MUST_NOT_IMPORT_TESTS')\n\n"
        "def test_positive():\n    assert True\n\n"
        "def test_negative():\n    assert True\n", encoding='utf-8')
    doc = {
        'schema_version': '1.0',
        'catalog_id': 'dark-factory-requirements-v1',
        'version': '1.0',
        'title': 'Synthetic requirements catalogue',
        'scope': 'Mechanical controls only, using synthetic data.',
        'policy': {'path': 'qa/strategy/quality-policy.json', 'canonical_sha256': canonical_digest(json.loads(policy_bytes))},
        'requirements': [{
            'id': 'DFR-001', 'version': '1.0', 'title': 'Approval precedes export',
            'statement': 'Export requires approval of the current report version.',
            'risk': 'An unapproved report is delivered.', 'priority': 'P0', 'owner': 'qa_automation',
            'scope': 'Stored local simulator controls.',
            'preconditions': ['A synthetic report is available.'],
            'allowed_outcomes': ['approved_export', 'export_blocked'],
            'expectation_source': {'path': 'qa/strategy/quality-policy.json',
                                   'reference': 'coverage.risk_priorities'},
            'limitations': ['Does not prove operator competence or external delivery.'],
            'scenarios': [
                {'id': 'DFR-001-POS', 'kind': 'positive', 'given': 'Current version is approved.',
                 'action': 'Request local export.', 'expected_outcome': 'approved_export',
                 'test_selectors': [POSITIVE]},
                {'id': 'DFR-001-NEG', 'kind': 'negative', 'given': 'Current version is not approved.',
                 'action': 'Request local export.', 'expected_outcome': 'export_blocked',
                 'test_selectors': [NEGATIVE]},
            ],
        }],
    }
    target = tmp_path / 'qa/requirements/catalog-v1.json'
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(doc), encoding='utf-8')
    return tmp_path, doc


def completed_run(*positive_nodes):
    collected = [*(positive_nodes or [POSITIVE]), NEGATIVE]
    return {
        'exit_code': 0, 'collection_complete': True, 'collected': collected,
        'reports': [{'nodeid': node, 'when': phase, 'outcome': 'passed', 'wasxfail': False}
                    for node in collected for phase in ('setup', 'call', 'teardown')],
        'source_unchanged': True,
    }


def report_for(catalogue, run=None):
    root, doc = catalogue
    return build_report(doc, validate_catalog(doc, root), run)


def assert_no_product_acceptance(report):
    assert report['product_acceptance'] == 'INCOMPLETE'
    assert report['candidate_admission'] == 'NOT_EVALUATED'


def test_catalogue_validation_resolves_source_without_importing_test_modules(catalogue):
    root, expected = catalogue
    doc = load_catalog(root)
    assert doc == expected
    checked = validate_catalog(doc, root)
    assert checked['status'] == 'PASS'
    assert checked['requirement_count'] == 1
    assert checked['scenario_count'] == checked['selector_count'] == 2
    assert len(checked['catalog_sha256']) == 64


def test_shipped_catalogue_maps_real_configured_tests():
    doc = load_catalog(ROOT)
    checked = validate_catalog(doc, ROOT)
    assert checked['status'] == 'PASS'
    assert checked['requirement_count'] >= 8
    assert checked['scenario_count'] >= checked['requirement_count'] * 2


@pytest.mark.parametrize('retained_kind', ['positive', 'negative'])
def test_critical_requirement_requires_both_behaviour_sides(catalogue, retained_kind):
    root, doc = catalogue
    row = doc['requirements'][0]
    row['scenarios'] = [s for s in row['scenarios'] if s['kind'] == retained_kind]
    with pytest.raises(RequirementError):
        validate_catalog(doc, root)


@pytest.mark.parametrize('level', ['catalogue', 'requirement', 'scenario', 'source'])
def test_unknown_fields_cannot_silently_change_contract(catalogue, level):
    root, doc = catalogue
    row = doc['requirements'][0]
    target = {'catalogue': doc, 'requirement': row, 'scenario': row['scenarios'][0],
              'source': row['expectation_source']}[level]
    target['automatic_product_acceptance'] = True
    with pytest.raises(RequirementError):
        validate_catalog(doc, root)


@pytest.mark.parametrize('which', ['requirement', 'scenario', 'selector'])
def test_duplicate_identity_or_check_is_rejected(catalogue, which):
    root, doc = catalogue
    row = doc['requirements'][0]
    if which == 'requirement':
        doc['requirements'].append(deepcopy(row))
    elif which == 'scenario':
        row['scenarios'].append(deepcopy(row['scenarios'][0]))
    else:
        row['scenarios'][0]['test_selectors'].append(POSITIVE)
    with pytest.raises(RequirementError):
        validate_catalog(doc, root)


@pytest.mark.parametrize('selector', [
    'tests/dark_factory/test_missing.py::test_positive',
    'tests/dark_factory/test_sample.py::test_missing',
    'tests/dark_factory/test_sample.py::MissingClass::test_positive',
    '../outside.py::test_positive',
    'tests/dark_factory/../outside.py::test_positive',
])
def test_missing_or_escaping_selector_does_not_count_as_coverage(catalogue, selector):
    root, doc = catalogue
    doc['requirements'][0]['scenarios'][0]['test_selectors'] = [selector]
    with pytest.raises(RequirementError):
        validate_catalog(doc, root)


def test_existing_test_outside_configured_suite_is_not_silently_accepted(catalogue):
    root, doc = catalogue
    path = root / 'tests/legacy/test_old.py'
    path.parent.mkdir()
    path.write_text('def test_old():\n    pass\n', encoding='utf-8')
    doc['requirements'][0]['scenarios'][0]['test_selectors'] = ['tests/legacy/test_old.py::test_old']
    with pytest.raises(RequirementError):
        validate_catalog(doc, root)


def test_expectation_outside_declared_outcomes_is_rejected(catalogue):
    root, doc = catalogue
    doc['requirements'][0]['scenarios'][0]['expected_outcome'] = 'unrestricted_export'
    with pytest.raises(RequirementError):
        validate_catalog(doc, root)


def test_policy_change_requires_explicit_catalogue_reconciliation(catalogue):
    root, doc = catalogue
    (root / doc['policy']['path']).write_text('{}', encoding='utf-8')
    with pytest.raises(RequirementError):
        validate_catalog(doc, root)


def test_policy_cannot_change_between_its_pin_check_and_source_reference(catalogue, monkeypatch):
    from qa.requirements import catalog as module

    root, doc = catalogue
    original = module.tomllib.loads

    def parse_configuration_then_change_policy(value):
        parsed = original(value)
        (root / doc['policy']['path']).write_text('{}', encoding='utf-8')
        return parsed

    monkeypatch.setattr(module.tomllib, 'loads', parse_configuration_then_change_policy)
    with pytest.raises(RequirementError, match='SOURCE_CHANGED_DURING_VALIDATION'):
        validate_catalog(doc, root)


def test_policy_identity_survives_git_line_ending_conversion(catalogue):
    root, doc = catalogue
    path = root / doc['policy']['path']
    serialized = json.dumps(json.loads(path.read_text('utf-8-sig')), indent=2, ensure_ascii=False) + '\n'
    path.write_bytes(serialized.encode('utf-8'))
    lf = validate_catalog(doc, root)
    path.write_bytes(serialized.replace('\n', '\r\n').encode('utf-8'))
    crlf = validate_catalog(doc, root)
    assert lf['status'] == crlf['status'] == 'PASS'
    assert lf['catalog_sha256'] == crlf['catalog_sha256']
    # The portable policy identity is stable; local evidence still records exact bytes.
    assert lf['source_hashes'][doc['policy']['path']] != crlf['source_hashes'][doc['policy']['path']]


@pytest.mark.parametrize('raw', [
    '{"schema_version":"1.0","schema_version":"2.0"}',
    '{"schema_version":"1.0","unexpected":NaN}',
])
def test_ambiguous_json_is_rejected_before_any_execution(catalogue, raw):
    root, _ = catalogue
    (root / 'qa/requirements/catalog-v1.json').write_text(raw, encoding='utf-8')
    with pytest.raises(RequirementError):
        load_catalog(root)


def test_static_validity_cannot_become_execution_success(catalogue):
    report = report_for(catalogue)
    assert report['catalog_validation'] == 'PASS'
    assert report['execution_status'] == 'NOT_RUN'
    assert report['requirements_status'] == 'INCOMPLETE'
    assert all(r['status'] == 'INCOMPLETE' for r in report['requirements'])
    assert_no_product_acceptance(report)


def test_successful_negative_test_proves_expected_blocking_behaviour(catalogue):
    report = report_for(catalogue, completed_run())
    assert report['execution_status'] == report['requirements_status'] == 'PASS'
    assert report['requirements'][0]['scenarios'][1]['status'] == 'PASS'
    assert_no_product_acceptance(report)


@pytest.mark.parametrize('missing_phase', ['setup', 'call', 'teardown'])
def test_every_collected_parameter_case_requires_a_complete_lifecycle(catalogue, missing_phase):
    first, second = POSITIVE + '[one]', POSITIVE + '[two]'
    run = completed_run(first, second)
    run['reports'] = [r for r in run['reports'] if not (r['nodeid'] == second and r['when'] == missing_phase)]
    report = report_for(catalogue, run)
    assert report['requirements_status'] == 'INCOMPLETE'
    assert report['requirements'][0]['scenarios'][0]['status'] == 'INCOMPLETE'
    assert_no_product_acceptance(report)


def test_one_missing_mapped_selector_prevents_requirement_pass(catalogue):
    run = completed_run()
    run['collected'].remove(NEGATIVE)
    run['reports'] = [r for r in run['reports'] if r['nodeid'] != NEGATIVE]
    report = report_for(catalogue, run)
    assert report['requirements_status'] == 'INCOMPLETE'


@pytest.mark.parametrize('phase', ['setup', 'call', 'teardown'])
def test_skipped_phase_is_not_evidence_of_success(catalogue, phase):
    run = completed_run()
    next(r for r in run['reports'] if r['nodeid'] == POSITIVE and r['when'] == phase)['outcome'] = 'skipped'
    report = report_for(catalogue, run)
    assert report['requirements_status'] == 'INCOMPLETE'


@pytest.mark.parametrize('outcome', ['passed', 'skipped'])
def test_xfail_or_xpass_cannot_qualify_a_requirement(catalogue, outcome):
    run = completed_run()
    call = next(r for r in run['reports'] if r['nodeid'] == POSITIVE and r['when'] == 'call')
    call.update(outcome=outcome, wasxfail=True)
    assert report_for(catalogue, run)['requirements_status'] == 'INCOMPLETE'


def test_later_pass_does_not_erase_an_observed_failure(catalogue):
    run = completed_run()
    failing = {'nodeid': POSITIVE, 'when': 'call', 'outcome': 'failed', 'wasxfail': False}
    run['reports'].insert(1, failing)
    run['exit_code'] = 1
    report = report_for(catalogue, run)
    assert report['requirements_status'] == 'FAIL'
    assert report['requirements'][0]['scenarios'][0]['status'] == 'FAIL'
    assert_no_product_acceptance(report)


def test_observed_failure_survives_missing_other_evidence(catalogue):
    run = completed_run()
    next(r for r in run['reports'] if r['nodeid'] == POSITIVE and r['when'] == 'call')['outcome'] = 'failed'
    run['reports'] = [r for r in run['reports'] if r['nodeid'] != NEGATIVE]
    run['exit_code'] = 1
    assert report_for(catalogue, run)['requirements_status'] == 'FAIL'


@pytest.mark.parametrize('exit_code', [2, 3, 4, 5])
def test_collection_or_runner_failure_cannot_publish_complete_success(catalogue, exit_code):
    run = completed_run()
    run.update(exit_code=exit_code, collection_complete=False)
    report = report_for(catalogue, run)
    assert report['execution_status'] != 'PASS'
    assert report['requirements_status'] != 'PASS'
    assert_no_product_acceptance(report)


def test_source_modified_during_execution_invalidates_success_claim(catalogue):
    run = completed_run()
    run['source_unchanged'] = False
    report = report_for(catalogue, run)
    assert report['execution_status'] != 'PASS'
    assert report['requirements_status'] != 'PASS'
    assert_no_product_acceptance(report)


def test_report_building_does_not_mutate_catalogue_or_observations(catalogue):
    _, doc = catalogue
    run = completed_run(POSITIVE + '[one]', POSITIVE + '[two]')
    before_doc, before_run = deepcopy(doc), deepcopy(run)
    report = report_for(catalogue, run)
    assert report['requirements_status'] == 'PASS'
    assert doc == before_doc and run == before_run


def test_deselected_parameter_prevents_qualifying_remaining_successes(catalogue):
    recorder = Recorder()
    nodes = [POSITIVE + '[retained]', NEGATIVE]
    recorder.pytest_deselected([SimpleNamespace(nodeid=POSITIVE + '[hidden]')])
    recorder.pytest_collection_finish(SimpleNamespace(items=[SimpleNamespace(nodeid=node) for node in nodes]))
    for node in nodes:
        for phase in ('setup', 'call', 'teardown'):
            recorder.pytest_runtest_logreport(SimpleNamespace(nodeid=node, when=phase, outcome='passed'))
    report = report_for(catalogue, recorder.result(0, True))
    assert report['requirements_status'] == 'INCOMPLETE'
    assert report['execution_status'] != 'PASS'
    assert_no_product_acceptance(report)
