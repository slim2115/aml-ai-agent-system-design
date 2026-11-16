"""Fixed adapters for current owned deterministic runs; no historical imports.

The caller must reserve every output directory before launching children and
record their lifecycle. These parsers alone cannot establish freshness.
"""
from hashlib import sha256
from pathlib import Path
import re
import tomllib

from qa.reporting import assess, junit, strict_json
from qa.requirements.catalog import CATALOG_PATH, canonical_digest, load_catalog, validate_catalog
from qa.requirements.execution import _node_status, build_report
from scripts.release.package_release import valid_path
from src.qa_profile import checked_path


IDS = ('full_regression', 'requirements', 'ui_cli', 'ci_inputs')
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
UI_CHECKS = frozenset((
    'ui_initial_consent_and_readiness_required', 'ui_preparation_and_refresh_preserve_consent',
    'ui_job_and_report_persisted', 'ui_fact_uses_plain_text',
    'ui_exact_source_cells_and_rule_edition_available', 'ui_decision_requires_explicit_choice_and_reason',
    'ui_unsaved_review_survives_stage_change', 'cli_persists_concurrent_decision',
    'ui_stale_decision_rejected', 'ui_refresh_retains_bound_report',
    'ui_refresh_does_not_silently_accept_new_revision', 'ui_decision_saved_with_bound_report',
    'ui_unavailable_sources_block_decision_and_new_export', 'ui_export_requires_explicit_consequence_confirmation',
    'ui_local_export_receipt_and_download', 'export_reopens_exact_saved_bytes',
    'ui_export_freezes_decision_and_reason', 'cli_reads_saved_job_and_replays_receipt',
    'ui_retries_existing_pending_delivery', 'ui_incident_change_hides_previous_report_actions',
    'ui_history_restores_saved_report_and_delivery', 'ui_engine_change_hides_previous_report',
    'ui_policy_consent_change_hides_previous_report', 'ui_unavailable_sources_block_pending_export_retry',
    'ui_untrusted_html_is_text_and_cannot_authorize', 'compatibility_cli_emits_v1_result',
    'no_embedding_or_model_import', 'no_python_network_attempts', 'code_stable_during_qualification',
))
CI_CHECKS = frozenset((
    'explicit_workflow_triggers', 'ci_configuration_matches_defaults', 'one_pytest_config',
    'single_test_selection_includes_api_workflow_release', 'ci_environment_exact_lock',
    'test_dependency_imports', 'windows_python312_isolated_environment',
    'ci_uses_same_hashed_lock', 'ci_runs_all_local_entrypoints', 'ci_results_outside_historical_evidence',
))
UI_SOURCES = frozenset((
    'src/ui/app.py', 'src/ui/presentation.py', 'scripts/run_workflow.py',
    'scripts/run_investigation.py', 'src/workflow/controller.py', 'src/workflow/store.py',
    'src/workflow/delivery.py', 'src/workflow/sources.py', 'scripts/rebuild/qualify_workflow_ui.py',
))
REQUIREMENT_EXTRA_SOURCES = frozenset((
    CATALOG_PATH, 'tests/conftest.py', 'qa/requirements/__init__.py',
    'qa/requirements/catalog.py', 'qa/requirements/execution.py', 'qa/requirements/runner.py',
    'qa/tools/check_requirements.py', 'qa/reporting.py', 'src/qa_profile.py',
))


class AdapterError(ValueError):
    """Stable error code for invalid evidence, never raw report content."""


def require(condition, code):
    if not condition:
        raise AdapterError(code)


def specs(root, run_id):
    require(type(run_id) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', run_id)
            and valid_path(run_id), 'INVALID_EXECUTION_ID')
    base = 'var/qa/execution/' + run_id
    requirement_output = 'var/qa/requirements/execution-' + sha256(run_id.encode()).hexdigest()[:32]
    definitions = (
        ('full_regression', 'scripts/rebuild/run_checks.py', [], base + '/full_regression',
         'unit-verification.json', ('unit-verification.json', 'unit-tests.xml', 'unit-observations.json')),
        ('requirements', 'qa/tools/check_requirements.py', ['run'], requirement_output,
         'result.json', ('result.json', 'catalog.json', 'catalog-validation.json', 'observations.json',
                         'source-before.json', 'source-after.json', 'junit.xml')),
        ('ui_cli', 'scripts/diagnostics/qualify_ci_ui.py', [], base + '/ui_cli',
         'ui-cli-qualification.json', ('ui-cli-qualification.json',)),
        ('ci_inputs', 'scripts/rebuild/check_ci_inputs.py', [], base + '/ci_inputs',
         'ci-inputs.json', ('ci-inputs.json',)),
    )
    result = []
    root = Path(root).absolute()
    for identity, script, arguments, output, filename, artifacts in definitions:
        checked_path(root / output, root)
        result.append({'id': identity, 'run_id': run_id, 'output_dir': output,
                       'argv_tail': ['-I', '-B', script, *arguments, '--output-dir', output],
                       'result_path': output + '/' + filename,
                       'artifact_paths': [output + '/' + name for name in artifacts]})
    return result


def _bytes(root, name):
    require(type(name) is str and valid_path(name), 'INVALID_ARTIFACT_PATH')
    path = checked_path(root / name, root)
    if not path.is_file():
        raise FileNotFoundError(name)
    require(path.stat().st_size <= MAX_ARTIFACT_BYTES, 'ARTIFACT_TOO_LARGE')
    raw = path.read_bytes()
    require(len(raw) <= MAX_ARTIFACT_BYTES, 'ARTIFACT_TOO_LARGE')
    return raw


def _source_hashes(root, names):
    return {name: sha256(_bytes(root, name)).hexdigest() for name in sorted(names)}


def _checks(identity, result, root):
    expected = UI_CHECKS if identity == 'ui_cli' else CI_CHECKS
    checks = result.get('checks')
    require(type(checks) is list and all(type(row) is dict for row in checks), 'INVALID_CHECKS')
    require(all(type(row.get('name')) is str and type(row.get('passed')) is bool for row in checks),
            'INVALID_CHECK_RESULT')
    require(len(checks) == len(expected) and {row['name'] for row in checks} == expected,
            'CHECK_COLLECTION_INCOMPLETE')
    state, metrics, issues = assess('checks', {'result': result})
    if identity == 'ui_cli':
        require(result.get('source_sha256') == _source_hashes(root, UI_SOURCES), 'UI_SOURCE_IDENTITY_MISMATCH')
    else:
        require(result.get('lock_file') == 'requirements/ci-windows-py312.lock', 'CI_LOCK_IDENTITY_MISMATCH')
    if any(not row['passed'] for row in checks):
        state = 'FAIL'
    require(result.get('status') == state, 'CHECK_STATUS_CONTRADICTS_OBSERVATIONS')
    return state, metrics, issues


def _unit_lifecycle(observed):
    require(type(observed) is dict and set(observed) == {
        'schema_version', 'exit_code', 'collection_complete', 'collected', 'reports', 'source_stability'},
        'UNIT_OBSERVATIONS_INVALID')
    require(observed['schema_version'] == '1.0' and observed['source_stability'] == 'NOT_MEASURED_BY_THIS_CHILD'
        and type(observed['exit_code']) is int and observed['exit_code'] in range(6)
        and type(observed['collection_complete']) is bool
        and type(observed['collected']) is list and all(type(node) is str for node in observed['collected'])
        and type(observed['reports']) is list, 'UNIT_OBSERVATIONS_INVALID')
    require(all(type(row) is dict and set(row) == {'nodeid', 'when', 'outcome', 'wasxfail'}
        and type(row['nodeid']) is str and row['when'] in {'setup', 'call', 'teardown'}
        and row['outcome'] in {'passed', 'failed', 'skipped'} and type(row['wasxfail']) is bool
        for row in observed['reports']), 'UNIT_OBSERVATIONS_INVALID')
    known = set(observed['collected'])
    return (bool(known) and len(known) == len(observed['collected'])
        and observed['collection_complete']
        and all(row['nodeid'] in known for row in observed['reports'])
        and all(_node_status([row for row in observed['reports'] if row['nodeid'] == node]) == 'PASS'
                for node in known))


def _unit(files, root):
    result = files['unit-verification.json']
    parsed = junit(files['unit-tests.xml'])
    state, metrics, issues = assess('unit', {'result': result, 'junit': parsed})
    observed = files['unit-observations.json']
    lifecycle = _unit_lifecycle(observed)
    require(result.get('collection_complete') is observed['collection_complete']
        and result.get('lifecycle_complete') is lifecycle
        and result.get('collected_tests') == len(observed['collected'])
        and result.get('pytest_exit_code') == observed['exit_code'], 'UNIT_LIFECYCLE_SUMMARY_MISMATCH')
    if state == 'PASS':
        require(lifecycle, 'UNIT_LIFECYCLE_INCOMPLETE')
    metrics['collection_complete'] = observed['collection_complete']
    metrics['lifecycle_complete'] = lifecycle
    config = tomllib.loads(_bytes(root, 'pyproject.toml').decode('utf-8-sig'))
    expected_paths = config['tool']['pytest']['ini_options']['testpaths']
    require(type(expected_paths) is list and expected_paths and len(expected_paths) == len(set(expected_paths)),
            'INVALID_TEST_SELECTION')
    require(result.get('testpaths') == expected_paths, 'UNIT_TESTPATHS_MISMATCH')
    contracts = result.get('contract_artifacts_match')
    require(type(contracts) is list and len(contracts) == 2
            and all(type(row) is dict and type(row.get('passed')) is bool for row in contracts)
            and {row.get('name') for row in contracts} == {'openapi.json', 'schema.json'},
            'UNIT_CONTRACT_COLLECTION_INCOMPLETE')
    require(type(result.get('pytest_exit_code')) is int and result['pytest_exit_code'] in range(6),
            'INVALID_PYTEST_EXIT_CODE')
    if result.get('status') == 'PASS':
        require(state == 'PASS', 'UNIT_STATUS_CONTRADICTS_OBSERVATIONS')
    elif state == 'PASS':
        raise AdapterError('UNIT_STATUS_CONTRADICTS_OBSERVATIONS')
    # Even without subtests, suite summary counts must agree with parsed nodes.
    require(parsed.get('reported_counts') == {
        'tests': result.get('tests'), 'failure': result.get('failures'),
        'error': result.get('errors'), 'skipped': result.get('skipped')}, 'UNIT_SUITE_COUNTS_MISMATCH')
    return state, metrics, issues


def _requirements(files, root, spec):
    catalog, validation = files['catalog.json'], files['catalog-validation.json']
    require(catalog == load_catalog(root), 'REQUIREMENT_CATALOG_CHANGED')
    require(validation == validate_catalog(catalog, root), 'REQUIREMENT_VALIDATION_CHANGED')
    expected_names = set(validation['source_hashes']) | REQUIREMENT_EXTRA_SOURCES
    expected_sources = _source_hashes(root, expected_names)
    require(files['source-before.json'] == files['source-after.json'] == expected_sources,
            'REQUIREMENT_SOURCE_IDENTITY_MISMATCH')
    observed = files['observations.json']
    computed = build_report(catalog, validation, observed)
    result = files['result.json']
    require(all(result.get(key) == value for key, value in computed.items()), 'REQUIREMENT_REPORT_MISMATCH')
    require(result.get('mode') == 'run' and result.get('run_id') == Path(spec['output_dir']).name,
            'REQUIREMENT_RUN_IDENTITY_MISMATCH')
    parsed = junit(files['junit.xml'])
    require(type(observed) is dict and type(observed.get('collected')) is list, 'INVALID_REQUIREMENT_OBSERVATIONS')
    # This catalogue currently has no aggregated success-only subtests. Each
    # collected case must have a JUnit node; the recorder supplies lifecycle data.
    require(parsed['tests'] == len(observed['collected']) and parsed['tests'] > 0,
            'REQUIREMENT_JUNIT_COLLECTION_MISMATCH')
    require(parsed['reported_counts'] == {key: parsed[key] for key in ('tests', 'failure', 'error', 'skipped')},
            'REQUIREMENT_JUNIT_COUNTS_MISMATCH')
    state = computed['requirements_status']
    if computed['execution_status'] == 'ERROR':
        state = 'ERROR'
    elif state == 'PASS' and computed['execution_status'] != 'PASS':
        state = computed['execution_status']
    if state == 'PASS':
        require(not any(parsed[key] for key in ('failure', 'error', 'skipped')), 'REQUIREMENT_JUNIT_FAILURE_HIDDEN')
    return state, {'requirements': computed['requirement_count'], 'selectors': computed['selector_count'],
                   'confirmed_requirements': computed['confirmed_requirements'],
                   'collected_tests': len(observed['collected'])}, []


def assess_adapter(spec, root, returncode):
    root = Path(root).absolute()
    result = {'id': spec.get('id') if isinstance(spec, dict) else None, 'status': 'ERROR',
              'issues': [], 'metrics': {}, 'files': [], 'observed_failure': False}
    try:
        require(type(spec) is dict, 'INVALID_ADAPTER_SPEC')
        expected = next((item for item in specs(root, spec.get('run_id')) if item['id'] == spec.get('id')), None)
        require(expected is not None and spec == expected, 'INVALID_ADAPTER_SPEC')
        files = {}
        # Preserve an explicitly recorded failure even if its companion
        # evidence is later missing, malformed or cannot be authenticated.
        primary_raw = _bytes(root, spec['result_path'])
        primary = strict_json(primary_raw.decode('utf-8-sig'))
        require(type(primary) is dict, 'INVALID_RESULT_DOCUMENT')
        result['observed_failure'] = primary.get('status') == 'FAIL' or primary.get('requirements_status') == 'FAIL'
        result['files'].append({'path': spec['result_path'], 'sha256': sha256(primary_raw).hexdigest(), 'size': len(primary_raw)})
        files[Path(spec['result_path']).name] = primary
        require(type(returncode) is int and returncode in (0, 1, 2), 'INVALID_ADAPTER_EXIT_CODE')
        for name in spec['artifact_paths']:
            if name == spec['result_path']:
                continue
            raw = _bytes(root, name)
            result['files'].append({'path': name, 'sha256': sha256(raw).hexdigest(), 'size': len(raw)})
            files[Path(name).name] = strict_json(raw.decode('utf-8-sig')) if name.endswith('.json') else raw
        identity = spec['id']
        if identity == 'full_regression':
            state, metrics, issues = _unit(files, root)
        elif identity == 'requirements':
            state, metrics, issues = _requirements(files, root, spec)
        else:
            state, metrics, issues = _checks(identity, primary, root)
        result['observed_failure'] |= state == 'FAIL'
        expected_code = (0 if state == 'PASS' else 1 if state == 'FAIL' else 2)
        require(returncode == expected_code, 'EXIT_CODE_DISAGREES')
        result.update(status=state, metrics=metrics, issues=issues)
    except FileNotFoundError:
        result.update(status='INCOMPLETE', issues=['REQUIRED_ARTIFACT_MISSING'])
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
        result.update(status='ERROR', issues=[str(exc) if isinstance(exc, AdapterError) else type(exc).__name__])
    return result
