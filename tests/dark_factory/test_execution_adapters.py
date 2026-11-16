"""An owned process exit is insufficient without complete, consistent evidence."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from qa.execution import adapters
from qa.requirements.catalog import CATALOG_PATH, load_catalog, selectors, validate_catalog
from qa.requirements.execution import build_report


ROOT = Path(__file__).resolve().parents[2]


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if isinstance(value, (dict, list)) else value, encoding='utf-8')


def spec(root, identity):
    return next(row for row in adapters.specs(root, 'test-owned-01') if row['id'] == identity)


def result_file(root, spec):
    return json.loads((root / spec['result_path']).read_text('utf-8'))


def xml(count, *, total=None, failures=0, skipped=0):
    cases = ''.join('<testcase name="test_' + str(index) + '" classname="tests.runtime.test_x">'
                    + ('<failure/>' if index < failures else '<skipped/>' if index < failures + skipped else '')
                    + '</testcase>' for index in range(count))
    return f'<testsuites><testsuite tests="{count if total is None else total}" failures="{failures}" errors="0" skipped="{skipped}">{cases}</testsuite></testsuites>'


def unit(root, *, subtests=0, failures=0, skipped=0):
    current = spec(root, 'full_regression')
    write(root, 'pyproject.toml', '[tool.pytest.ini_options]\ntestpaths = ["tests/runtime"]\n')
    report = {'status': 'FAIL' if failures or skipped else 'PASS', 'pytest_exit_code': 1 if failures else 0,
              'tests': 3 + subtests, 'collected_tests': 3, 'additional_subtests': subtests,
              'failures': failures, 'errors': 0, 'skipped': skipped,
              'testpaths': ['tests/runtime'],
              'collection_complete': True, 'lifecycle_complete': not (failures or skipped),
              'contract_artifacts_match': [{'name': 'openapi.json', 'passed': True},
                                           {'name': 'schema.json', 'passed': True}]}
    write(root, current['result_path'], report)
    write(root, current['output_dir'] + '/unit-tests.xml', xml(3, total=3 + subtests, failures=failures, skipped=skipped))
    nodes = ['tests/runtime/test_x.py::test_' + str(index) for index in range(3)]
    observations = {'schema_version': '1.0', 'exit_code': report['pytest_exit_code'],
        'collection_complete': True, 'collected': nodes, 'source_stability': 'NOT_MEASURED_BY_THIS_CHILD',
        'reports': [{'nodeid': node, 'when': phase, 'outcome':
                     'failed' if phase == 'call' and index < failures else
                     'skipped' if phase == 'call' and index < failures + skipped else 'passed',
                     'wasxfail': False}
                    for index, node in enumerate(nodes) for phase in ('setup', 'call', 'teardown')]}
    write(root, current['output_dir'] + '/unit-observations.json', observations)
    return current


def checks(root, identity):
    current = spec(root, identity)
    names = adapters.UI_CHECKS if identity == 'ui_cli' else adapters.CI_CHECKS
    report = {'status': 'PASS', 'checks': [{'name': name, 'passed': True} for name in sorted(names)]}
    if identity == 'ui_cli':
        for name in adapters.UI_SOURCES:
            write(root, name, '# synthetic source\n')
        report['source_sha256'] = adapters._source_hashes(root, adapters.UI_SOURCES)
    else:
        report['lock_file'] = 'requirements/ci-windows-py312.lock'
    write(root, current['result_path'], report)
    return current


@pytest.fixture
def requirements(tmp_path):
    catalog = load_catalog(ROOT)
    validation = validate_catalog(catalog, ROOT)
    for name in set(validation['source_hashes']) | adapters.REQUIREMENT_EXTRA_SOURCES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    current = spec(tmp_path, 'requirements')
    nodes = selectors(catalog)
    observations = {'exit_code': 0, 'collection_complete': True, 'collected': nodes,
                    'reports': [{'nodeid': node, 'when': phase, 'outcome': 'passed', 'wasxfail': False}
                                for node in nodes for phase in ('setup', 'call', 'teardown')],
                    'source_unchanged': True}
    report = build_report(catalog, validation, observations)
    report.update(mode='run', run_id=Path(current['output_dir']).name)
    snapshot = adapters._source_hashes(tmp_path, set(validation['source_hashes']) | adapters.REQUIREMENT_EXTRA_SOURCES)
    for name, value in {'result.json': report, 'catalog.json': catalog, 'catalog-validation.json': validation,
                        'observations.json': observations, 'source-before.json': snapshot, 'source-after.json': snapshot,
                        'junit.xml': xml(len(nodes))}.items():
        write(tmp_path, current['output_dir'] + '/' + name, value)
    return tmp_path, current


def test_specs_only_fixed_entrypoints_and_unique_owned_paths(tmp_path):
    rows = adapters.specs(tmp_path, 'a' * 64)
    assert tuple(row['id'] for row in rows) == adapters.IDS
    assert all(row['argv_tail'][:2] == ['-I', '-B'] for row in rows)
    assert len({row['output_dir'] for row in rows}) == 4
    assert len(Path(rows[1]['output_dir']).name) < 96
    assert rows[1]['argv_tail'][3] == 'run'
    assert rows == adapters.specs(tmp_path, 'a' * 64)
    assert rows != adapters.specs(tmp_path, 'different-id')


@pytest.mark.parametrize('identity', ['', '../escape', 'run/x', 'NUL', 'x:y', 'x' * 65, None])
def test_unsafe_run_id_is_rejected(tmp_path, identity):
    with pytest.raises(ValueError):
        adapters.specs(tmp_path, identity)


@pytest.mark.parametrize('subtests', [0, 5])
def test_unit_accepts_consistent_current_counts_including_pytest9_subtests(tmp_path, subtests):
    current = unit(tmp_path, subtests=subtests)
    report = adapters.assess_adapter(current, tmp_path, 0)
    assert report['status'] == 'PASS'
    assert report['metrics']['tests'] == 3 + subtests
    assert len(report['files']) == 3
    assert all(len(row['sha256']) == 64 and row['size'] > 0 for row in report['files'])


@pytest.mark.parametrize('case', ['failures', 'skipped'])
def test_unit_measured_failure_or_skip_is_not_success(tmp_path, case):
    current = unit(tmp_path, **{case: 1})
    report = adapters.assess_adapter(current, tmp_path, 1)
    assert report['status'] == 'FAIL'
    assert report['observed_failure']


@pytest.mark.parametrize('mutation', ['zero', 'collection', 'contracts', 'suite', 'hidden_failure'])
def test_unit_rejects_inconsistent_or_incomplete_reports(tmp_path, mutation):
    current = unit(tmp_path)
    value = result_file(tmp_path, current)
    if mutation == 'zero':
        value.update(tests=0, collected_tests=0)
    elif mutation == 'collection':
        value['testpaths'] = ['tests/contracts']
    elif mutation == 'contracts':
        value['contract_artifacts_match'].pop()
    elif mutation == 'suite':
        write(tmp_path, current['output_dir'] + '/unit-tests.xml', xml(3, total=4))
    elif mutation == 'hidden_failure':
        write(tmp_path, current['output_dir'] + '/unit-tests.xml', xml(3, failures=1))
    write(tmp_path, current['result_path'], value)
    assert adapters.assess_adapter(current, tmp_path, 0)['status'] == 'ERROR'


@pytest.mark.parametrize('identity', ['ui_cli', 'ci_inputs'])
def test_checks_require_complete_named_collection(tmp_path, identity):
    current = checks(tmp_path, identity)
    assert adapters.assess_adapter(current, tmp_path, 0)['status'] == 'PASS'
    report = result_file(tmp_path, current)
    report['checks'].pop()
    write(tmp_path, current['result_path'], report)
    assert adapters.assess_adapter(current, tmp_path, 0)['status'] == 'ERROR'


@pytest.mark.parametrize('mutation', ['duplicate', 'not_bool', 'changed_source', 'changed_source_list'])
def test_ui_cannot_hide_missing_checks_or_source_changes(tmp_path, mutation):
    current = checks(tmp_path, 'ui_cli')
    report = result_file(tmp_path, current)
    if mutation == 'duplicate':
        report['checks'][0] = report['checks'][1]
    elif mutation == 'not_bool':
        report['checks'][0]['passed'] = 1
    elif mutation == 'changed_source':
        write(tmp_path, 'src/ui/app.py', '# changed\n')
    else:
        report['source_sha256'].pop('src/ui/app.py')
    write(tmp_path, current['result_path'], report)
    assert adapters.assess_adapter(current, tmp_path, 0)['status'] == 'ERROR'


def test_check_failure_is_preserved(tmp_path):
    current = checks(tmp_path, 'ci_inputs')
    report = result_file(tmp_path, current)
    report['checks'][0]['passed'] = False
    report['status'] = 'FAIL'
    write(tmp_path, current['result_path'], report)
    result = adapters.assess_adapter(current, tmp_path, 1)
    assert result['status'] == 'FAIL' and result['observed_failure'] is True


@pytest.mark.parametrize('identity', adapters.IDS)
def test_exit_zero_without_required_artifact_is_incomplete(tmp_path, identity):
    current = spec(tmp_path, identity)
    assert adapters.assess_adapter(current, tmp_path, 0)['status'] == 'INCOMPLETE'


@pytest.mark.parametrize('code', [1, 2, 4, None, True])
def test_exit_code_cannot_contradict_success(tmp_path, code):
    current = checks(tmp_path, 'ci_inputs')
    assert adapters.assess_adapter(current, tmp_path, code)['status'] == 'ERROR'


@pytest.mark.parametrize('body', ['{"status":"PASS","status":"PASS"}', '{"value":NaN}', '[]', 'invalid'])
def test_malformed_or_ambiguous_json_is_rejected(tmp_path, body):
    current = spec(tmp_path, 'ci_inputs')
    write(tmp_path, current['result_path'], body)
    assert adapters.assess_adapter(current, tmp_path, 0)['status'] == 'ERROR'


def test_spec_cannot_select_arbitrary_command_or_artifact(tmp_path):
    current = spec(tmp_path, 'ci_inputs')
    for field, value in [('argv_tail', ['-c', 'print(1)']), ('artifact_paths', ['../secret.json'])]:
        altered = deepcopy(current)
        altered[field] = value
        assert adapters.assess_adapter(altered, tmp_path, 0)['issues'] == ['INVALID_ADAPTER_SPEC']


def test_requirements_report_is_recomputed_from_current_sources(requirements):
    root, current = requirements
    report = adapters.assess_adapter(current, root, 0)
    assert report['status'] == 'PASS'
    assert report['metrics']['requirements'] == 12
    assert report['metrics']['selectors'] == 41
    assert len(report['files']) == 7


@pytest.mark.parametrize('mutation', ['run_id', 'validation', 'source', 'observations', 'claimed_result', 'junit'])
def test_requirement_identity_and_observations_cannot_be_substituted(requirements, mutation):
    root, current = requirements
    report = result_file(root, current)
    if mutation == 'run_id':
        report['run_id'] = 'old-run'
    elif mutation == 'claimed_result':
        report['confirmed_requirements'] = 999
    elif mutation == 'validation':
        path = current['output_dir'] + '/catalog-validation.json'
        value = json.loads((root / path).read_text('utf-8'))
        value['selector_count'] = 1
        write(root, path, value)
    elif mutation == 'source':
        with (root / 'tests/conftest.py').open('a', encoding='utf-8') as stream:
            stream.write('\n# mutation\n')
    elif mutation == 'observations':
        path = current['output_dir'] + '/observations.json'
        value = json.loads((root / path).read_text('utf-8'))
        value['reports'][1]['outcome'] = 'skipped'
        write(root, path, value)
    else:
        write(root, current['output_dir'] + '/junit.xml', xml(1))
    write(root, current['result_path'], report)
    assert adapters.assess_adapter(current, root, 0)['status'] == 'ERROR'


def test_requirement_explicit_failure_survives_consistent_recomputation(requirements):
    root, current = requirements
    base = current['output_dir']
    observations = json.loads((root / base / 'observations.json').read_text('utf-8'))
    observations['reports'][1]['outcome'] = 'failed'
    observations['exit_code'] = 1
    catalog = json.loads((root / base / 'catalog.json').read_text('utf-8'))
    validation = json.loads((root / base / 'catalog-validation.json').read_text('utf-8'))
    report = build_report(catalog, validation, observations)
    report.update(mode='run', run_id=Path(base).name)
    write(root, base + '/observations.json', observations)
    write(root, current['result_path'], report)
    write(root, base + '/junit.xml', xml(len(observations['collected']), failures=1))
    result = adapters.assess_adapter(current, root, 1)
    assert result['status'] == 'FAIL'
    assert result['observed_failure'] is True


@pytest.mark.parametrize('companion', ['missing', 'invalid_json', 'invalid_exit'])
def test_explicit_failure_survives_unverifiable_companion_evidence(tmp_path, companion):
    current = unit(tmp_path, failures=1)
    code = 1
    if companion == 'missing':
        (tmp_path / current['output_dir'] / 'unit-tests.xml').unlink()
    elif companion == 'invalid_json':
        write(tmp_path, current['output_dir'] + '/unit-observations.json', 'invalid')
    else:
        code = None
    assessed = adapters.assess_adapter(current, tmp_path, code)
    assert assessed['status'] in {'ERROR', 'INCOMPLETE'}
    assert assessed['observed_failure'] is True
    assert current['result_path'] in {row['path'] for row in assessed['files']}


@pytest.mark.parametrize('mutation', ['deselected', 'xpass', 'missing_teardown', 'duplicate_node', 'unknown_node'])
def test_unit_rejects_hidden_incomplete_lifecycle(tmp_path, mutation):
    current = unit(tmp_path)
    path = current['output_dir'] + '/unit-observations.json'
    observations = json.loads((tmp_path / path).read_text('utf-8'))
    if mutation == 'deselected':
        observations['collection_complete'] = False
    elif mutation == 'xpass':
        observations['reports'][1]['wasxfail'] = True
    elif mutation == 'missing_teardown':
        observations['reports'].pop()
    elif mutation == 'duplicate_node':
        observations['collected'][1] = observations['collected'][0]
    else:
        observations['reports'][0]['nodeid'] = 'tests/runtime/test_x.py::not_collected'
    write(tmp_path, path, observations)
    assessed = adapters.assess_adapter(current, tmp_path, 0)
    assert assessed['status'] == 'ERROR'


@pytest.mark.parametrize('case', ['config_filter', 'deselection', 'xpass', 'parameter_ids'])
def test_real_full_runner_rejects_partial_collection_or_xpass(tmp_path, case):
    # Run the real entrypoint against a tiny owned project. Only contract
    # generation is substituted; pytest and its lifecycle hooks are real.
    addopts = '-k keep' if case == 'config_filter' else ''
    write(tmp_path, 'pyproject.toml', '[tool.pytest.ini_options]\ntestpaths=["tests/sample"]\naddopts=' + json.dumps(addopts) + '\n')
    source = 'def test_keep():\n    assert True\n\ndef test_other():\n    assert False\n'
    if case == 'deselection':
        source = source.replace('assert False', 'assert True')
        write(tmp_path, 'tests/sample/conftest.py',
            'def pytest_collection_modifyitems(config, items):\n'
            '    removed = items.pop()\n'
            '    config.hook.pytest_deselected(items=[removed])\n')
    elif case == 'xpass':
        source = 'import pytest\n@pytest.mark.xfail(strict=False)\ndef test_xpass():\n    assert True\n'
    elif case == 'parameter_ids':
        source = ('import pytest\n@pytest.mark.parametrize("value", [1, 2], ids='
                  + repr(['src//escape.py', 'src\\escape.py'])
                  + ')\ndef test_case(value):\n    assert value > 0\n')
    write(tmp_path, 'tests/sample/test_sample.py', source)
    code = ('import sys\nfrom pathlib import Path\n'
            + 'sys.path.insert(0, ' + repr(str(ROOT)) + ')\n'
            + 'from scripts.rebuild import run_checks\n'
            + 'run_checks.ROOT = Path(' + repr(str(tmp_path)) + ')\n'
            + 'run_checks.compare_contract_artifacts = lambda: [{"name":"openapi.json","passed":True},{"name":"schema.json","passed":True}]\n'
            + 'sys.argv = ["run_checks.py", "--output-dir", "var/checks/unit"]\n'
            + 'raise SystemExit(run_checks.main())\n')
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    env.pop('PYTEST_ADDOPTS', None)
    env.pop('PYTEST_PLUGINS', None)
    completed = subprocess.run([sys.executable, '-I', '-B', '-c', code], cwd=tmp_path, env=env,
                               capture_output=True, timeout=30)
    assert completed.returncode == (0 if case == 'parameter_ids' else 1), completed.stdout.decode('utf-8', errors='replace')
    report = json.loads((tmp_path / 'var/checks/unit/unit-verification.json').read_text('utf-8'))
    observed = json.loads((tmp_path / 'var/checks/unit/unit-observations.json').read_text('utf-8'))
    assert observed['source_stability'] == 'NOT_MEASURED_BY_THIS_CHILD'
    if case == 'parameter_ids':
        assert report['status'] == 'PASS' and report['lifecycle_complete'] is True
        assert len(observed['collected']) == len(set(observed['collected'])) == 2
        assert {row['nodeid'] for row in observed['reports']} == set(observed['collected'])
        return
    assert report['status'] == 'FAIL' and report['lifecycle_complete'] is False
    if case == 'config_filter':
        assert report['collected_tests'] == 2 and report['failures'] == 1
    elif case == 'deselection':
        assert report['pytest_exit_code'] == 0 and report['collection_complete'] is False
    else:
        assert report['pytest_exit_code'] == 0 and any(row['wasxfail'] for row in observed['reports'])
