"""Check workflow, test selection and the exact CI environment; no remote run."""
import argparse
from datetime import datetime, timezone
import importlib
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.config import get_settings
from scripts.rebuild.run_checks import configured_testpaths, output_directory

LOCK_PATH = 'requirements/ci-windows-py312.lock'


def locked_packages() -> dict[str, str]:
    locked = {}
    for line in (ROOT / LOCK_PATH).read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        match = re.fullmatch(r'(.+?) --hash=sha256:([0-9a-f]{64})', line)
        if match is None:
            raise ValueError('Malformed CI lock: expected one SHA256 wheel hash per requirement.')
        requirement = Requirement(match[1])
        versions = list(requirement.specifier)
        if (requirement.url or requirement.extras or len(versions) != 1
                or versions[0].operator != '==' or '*' in versions[0].version):
            raise ValueError('CI lock must contain exact distribution versions.')
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        name = canonicalize_name(requirement.name)
        if name in locked:
            raise ValueError(f'Duplicate CI lock entry: {name}')
        locked[name] = versions[0].version
    if not locked:
        raise ValueError('CI lock is empty.')
    return locked


def dependency_imports() -> list[dict]:
    # Exercise modules that previously failed in a minimal unit-only environment.
    # This imports tools only: it does not create an app or connect to a database.
    checks = []
    for module in ('pytest', 'pydantic', 'jsonschema', 'yaml', 'streamlit.testing.v1',
                   'fastapi.testclient', 'httpx', 'psycopg', 'psycopg_binary'):
        try:
            importlib.import_module(module)
        except Exception as exc:
            checks.append({'module': module, 'passed': False, 'exception_type': type(exc).__name__})
        else:
            checks.append({'module': module, 'passed': True})
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', default='var/checks/ci')
    args = parser.parse_args()
    directory = output_directory(args.output_dir)
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'))
    testpaths = configured_testpaths()
    settings = get_settings()
    expected = {'OLLAMA_BASE_URL': settings.llm.base_url, 'LLM_MODEL_NAME': settings.llm.model_name,
                'EMBEDDING_MODEL_NAME': settings.llm.embedding_model_name,
                'GENERATION_TIMEOUT_SEC': str(settings.performance.generation_timeout_sec),
                'RAG_RECALL_THRESHOLD': str(settings.thresholds.rag_recall),
                'RAG_PRECISION_THRESHOLD': str(settings.thresholds.rag_precision)}
    locked = locked_packages()
    installed = {canonicalize_name(d.metadata['Name']): d.version for d in importlib.metadata.distributions()
                 if canonicalize_name(d.metadata['Name']) != 'pip'}
    imports = dependency_imports()
    steps = workflow['jobs']['hermetic-windows']['steps']
    commands = [step.get('run', '') for step in steps]
    setup = next(step for step in steps if step.get('uses', '').startswith('actions/setup-python@'))
    upload = next(step for step in steps if step.get('uses', '').startswith('actions/upload-artifact@'))
    difference = {name: {'locked': locked.get(name), 'installed': installed.get(name)}
                  for name in sorted(locked.keys() | installed.keys()) if locked.get(name) != installed.get(name)}
    checks = [
        {'name': 'explicit_workflow_triggers', 'passed': set(workflow['on']) == {'push', 'pull_request', 'workflow_dispatch'}},
        {'name': 'ci_configuration_matches_defaults', 'passed': all(workflow['env'].get(key) == value for key, value in expected.items())},
        {'name': 'one_pytest_config', 'passed': not (ROOT / 'tests/pytest.ini').exists()},
        {'name': 'single_test_selection_includes_api_workflow_release',
         'passed': {'tests/lab', 'tests/workflow', 'tests/release'}.issubset(testpaths) and 'tests/unit' not in testpaths,
         'testpaths': testpaths},
        {'name': 'ci_environment_exact_lock', 'passed': not difference, 'packages': len(installed), 'differences': difference},
        {'name': 'test_dependency_imports', 'passed': all(item['passed'] for item in imports), 'imports': imports},
        {'name': 'windows_python312_isolated_environment',
         'passed': sys.platform == 'win32' and sys.version_info[:2] == (3, 12) and sys.prefix != sys.base_prefix
         and workflow['jobs']['hermetic-windows']['runs-on'].startswith('windows-')
         and setup['with']['python-version'] == '3.12'
         and any('python -m venv .venv-ci' in command for command in commands)},
        {'name': 'ci_uses_same_hashed_lock', 'passed': setup['with']['cache-dependency-path'] == LOCK_PATH
         and any('--require-hashes' in command and '--only-binary=:all:' in command
                 and f'-r {LOCK_PATH}' in command for command in commands)},
        {'name': 'ci_runs_all_local_entrypoints', 'passed': all(
            any(entrypoint in command for command in commands) for entrypoint in
            ('-m pip check', 'scripts/rebuild/check_ci_inputs.py', 'scripts/rebuild/run_checks.py',
             'scripts/diagnostics/qualify_ci_ui.py'))},
        {'name': 'ci_results_outside_historical_evidence',
         'passed': upload['with']['path'] == 'var/checks/ci-report/' and upload.get('if') == 'always()'
         and all('--output-dir var/checks/ci' in command for command in commands
                 if any(script in command for script in ('check_ci_inputs.py', 'run_checks.py', 'qualify_ci_ui.py')))},
    ]
    passed = all(check['passed'] for check in checks)
    result = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'PASS' if passed else 'FAIL', 'checks': checks,
              'lock_file': LOCK_PATH,
              'scope': 'Local workflow/configuration/package/import checks only. No remote GitHub Actions run, Linux, model or database validation.'}
    (directory / 'ci-inputs.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
