"""Run the pyproject test selection locally and in CI without rewriting evidence."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def output_directory(value: str) -> Path:
    directory = (ROOT / value).resolve()
    relative = directory.relative_to(ROOT)
    parts = tuple(part.lower() for part in relative.parts)
    if not parts or parts[0] not in {'var', 'verification'}:
        raise ValueError('Check output must be under var/ or verification/.')
    if any(part.startswith('block-') for part in parts):
        raise ValueError('Historical block evidence must not be overwritten.')
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def configured_testpaths() -> list[str]:
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    paths = config['tool']['pytest']['ini_options']['testpaths']
    if not paths or len(paths) != len(set(paths)):
        raise ValueError('Test selection must be nonempty and unique.')
    for name in paths:
        path = (ROOT / name).resolve()
        relative = path.relative_to(ROOT / 'tests')
        if not relative.parts or not path.is_dir():
            raise ValueError(f'Invalid test directory: {name}')
    return paths


def compare_contract_artifacts() -> list[dict]:
    from src.contracts.export import export_artifacts
    with tempfile.TemporaryDirectory(prefix='aml-contract-check-') as temporary:
        generated = Path(temporary)
        export_artifacts(generated)
        return [
            {'name': path.name,
             'passed': (ROOT / 'contracts/v1' / path.name).is_file()
             and path.read_text(encoding='utf-8') == (ROOT / 'contracts/v1' / path.name).read_text(encoding='utf-8')}
            for path in sorted(generated.iterdir())
        ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', default='var/checks/ci')
    args = parser.parse_args()
    directory = output_directory(args.output_dir)
    testpaths = configured_testpaths()
    contract_checks = compare_contract_artifacts()
    import pytest
    from qa.requirements.execution import Recorder, _node_status
    collected = Recorder()
    junit = directory / 'unit-tests.xml'
    # A failed invocation must not inherit the counts of a previous run.
    junit.unlink(missing_ok=True)
    code = pytest.main(['-c', str(ROOT / 'pyproject.toml'), '-o', 'addopts=',
                        '--strict-markers', '--strict-config', '-q', '-p', 'no:cacheprovider',
                        '--junitxml=' + str(junit),
                        *[str(ROOT / name) for name in testpaths]], plugins=[collected])
    # This child records test lifecycle only. The owning execution runner
    # measures source stability; do not invent an unchanged-source claim here.
    observations = {'schema_version': '1.0', 'exit_code': int(code),
                    'collection_complete': collected.collection_complete,
                    'collected': collected.collected, 'reports': collected.reports,
                    'source_stability': 'NOT_MEASURED_BY_THIS_CHILD'}
    known = set(collected.collected)
    lifecycle_complete = (bool(known) and len(known) == len(collected.collected)
        and collected.collection_complete
        and all(row['nodeid'] in known for row in collected.reports)
        and all(_node_status([row for row in collected.reports if row['nodeid'] == node]) == 'PASS'
                for node in known))
    (directory / 'unit-observations.json').write_text(json.dumps(observations, indent=2) + '\n', encoding='utf-8')
    counts = {name: 0 for name in ('tests', 'failures', 'errors', 'skipped')}
    if junit.exists():
        tree = ET.parse(junit)
        for suite in tree.iter('testsuite'):
            for name in counts:
                counts[name] += int(suite.attrib.get(name, 0))
    passed = (code == 0 and lifecycle_complete and counts['tests'] > 0
              and not any(counts[name] for name in ('failures', 'errors', 'skipped'))
              and bool(contract_checks) and all(item['passed'] for item in contract_checks))
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'PASS' if passed else 'FAIL',
              'pytest_exit_code': int(code), **counts, 'collected_tests': len(collected.collected),
              'additional_subtests': max(0, counts['tests'] - len(collected.collected)),
              'collection_complete': collected.collection_complete, 'lifecycle_complete': lifecycle_complete,
              'testpaths': testpaths, 'contract_artifacts_match': contract_checks,
              'scope': 'Configured contracts, configuration, evaluator, runtime, workflow, API and release controls. '
                       'Real subprocess lifecycle and SQLite; in-process HTTP and database doubles. '
                       'No live PostgreSQL, browser, model, network integration or SME acceptance.'}
    (directory / 'unit-verification.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
