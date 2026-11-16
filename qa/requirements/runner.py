"""Owned local requirement checks, using the project's existing pytest tests."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from hashlib import sha256
import argparse
import json
import os
from pathlib import Path
import re

from src.qa_profile import checked_path
from qa.requirements.catalog import (
    CATALOG_PATH, ROOT, RequirementError, canonical_digest, load_catalog, selectors, validate_catalog,
)
from qa.requirements.execution import Recorder, build_report


def output_path(value, root=ROOT):
    root = Path(root).absolute()
    supplied = Path(value)
    path = checked_path(supplied if supplied.is_absolute() else root / supplied, root)
    relative = path.relative_to(root)
    if (len(relative.parts) != 4 or relative.parts[:3] != ('var', 'qa', 'requirements')
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', relative.parts[-1])):
        raise RequirementError('REQUIREMENTS_OUTPUT_SCOPE')
    if path.exists():
        raise RequirementError('REQUIREMENTS_OUTPUT_EXISTS')
    return path


def write_new(directory, name, value):
    path = checked_path(directory / name, ROOT)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def source_snapshot(names):
    result = {}
    for name in sorted(names):
        try:
            path = checked_path(ROOT / name, ROOT)
            result[name] = sha256(path.read_bytes()).hexdigest()
        except (ValueError, OSError):
            result[name] = None
    return result


@contextmanager
def pytest_environment(directory):
    """Do not allow caller pytest filters/plugins to shrink the checked cases."""
    temporary = directory / 'tmp'
    temporary.mkdir()
    replacements = {'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTEST_ADDOPTS': None,
                    'PYTEST_PLUGINS': None, 'PYTHON_DOTENV_DISABLED': '1',
                    'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                    'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                    'HF_HUB_DISABLE_TELEMETRY': '1', 'ANONYMIZED_TELEMETRY': 'FALSE',
                    'OTEL_SDK_DISABLED': 'true', 'LANGSMITH_TRACING': 'false',
                    'LANGCHAIN_TRACING_V2': 'false', 'DO_NOT_TRACK': '1',
                    'TEMP': str(temporary), 'TMP': str(temporary)}
    previous = {key: os.environ.get(key) for key in replacements}
    cwd = Path.cwd()
    try:
        os.chdir(ROOT)
        for key, value in replacements.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        os.chdir(cwd)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def render_report(report):
    lines = ['# Исполняемые требования — результат', '',
             f"Каталог: **{report['catalog_validation']}**. Выполнение: **{report['execution_status']}**. "
             f"Проверки требований: **{report['requirements_status']}**.", '',
             'PASS относится только к перечисленным программным проверкам и их области. '
             'Продуктовая приёмка INCOMPLETE, допуск кандидата NOT_EVALUATED.', '',
             '| Требование | Область | Проверки |', '| --- | --- | --- |']
    for row in report['requirements']:
        clean = lambda value: value.replace('|', '\\|').replace('\n', ' ')
        lines.append(f"| {row['id']} — {clean(row['title'])} | {clean(row['scope'])} | {row['status']} |")
    lines += ['', '## Неподтверждённые требования', '',
              ', '.join(report['unconfirmed_requirements']) or 'Нет в области выполненных проверок.', '',
              '## Границы', '', report['evidence_scope'], '',
              'Отрицательный сценарий проверяет корректный отказ или блокировку: его тест должен пройти. '
              'NOT_RUN, пропуск и неполное выполнение не подтверждают требование.', '']
    return '\n'.join(lines)


def run(mode, output):
    if mode not in ('validate', 'run'):
        raise RequirementError('INVALID_REQUIREMENTS_MODE')
    directory = output_path(output)
    directory.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    write_new(directory, 'started.json', {'schema_version': '1.0', 'mode': mode,
              'started_at_utc': started, 'status': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE'})
    report = {'schema_version': '1.0', 'catalog_validation': 'ERROR', 'execution_status': 'NOT_RUN',
              'requirements_status': 'INCOMPLETE', 'requirements': [], 'unconfirmed_requirements': [],
              'product_acceptance': 'INCOMPLETE', 'candidate_admission': 'NOT_EVALUATED',
              'evidence_scope': 'Catalogue validation failed; no requirement is confirmed.'}
    code = 2
    try:
        catalog = load_catalog()
        validation = validate_catalog(catalog)
        write_new(directory, 'catalog.json', catalog)
        write_new(directory, 'catalog-validation.json', validation)
        report = build_report(catalog, validation)
        code = 0
        if mode == 'run':
            names = set(validation['source_hashes']) | {CATALOG_PATH, 'tests/conftest.py',
                'qa/requirements/__init__.py', 'qa/requirements/catalog.py', 'qa/requirements/execution.py',
                'qa/requirements/runner.py', 'qa/tools/check_requirements.py', 'qa/reporting.py', 'src/qa_profile.py'}
            before = source_snapshot(names)
            write_new(directory, 'source-before.json', before)
            if any(value is None for value in before.values()):
                raise RequirementError('REQUIREMENTS_SOURCE_MISSING')
            if (any(before[name] != digest for name, digest in validation['source_hashes'].items())
                    or canonical_digest(load_catalog()) != validation['catalog_sha256']):
                raise RequirementError('REQUIREMENTS_SOURCE_CHANGED_AFTER_VALIDATION')
            recorder = Recorder()
            exit_code = 3
            failure = None
            try:
                with pytest_environment(directory), (directory / 'pytest.log').open('x', encoding='utf-8') as log:
                    with redirect_stdout(log), redirect_stderr(log):
                        import pytest
                        exit_code = int(pytest.main(['-c', str(ROOT / 'pyproject.toml'),
                            '--rootdir', str(ROOT), '-o', 'addopts=', '-q', '--strict-markers', '--strict-config',
                            '-p', 'no:cacheprovider', '--basetemp', str(directory / 'pytest-tmp'),
                            '--junitxml', str(directory / 'junit.xml'), *selectors(catalog)], plugins=[recorder]))
            except KeyboardInterrupt:
                exit_code, failure = 2, 'INTERRUPTED'
            except Exception as exc:
                exit_code, failure = 3, type(exc).__name__
            finally:
                after = source_snapshot(names)
                write_new(directory, 'source-after.json', after)
                execution = recorder.result(exit_code, before == after)
                write_new(directory, 'observations.json', execution)
                report = build_report(catalog, validation, execution)
                if failure:
                    report['runner_error_type'] = failure
            code = (0 if report['execution_status'] == report['requirements_status'] == 'PASS'
                    else 1 if report['requirements_status'] == 'FAIL' else 2)
    except (RequirementError, OSError, ValueError) as exc:
        report.update(error_code=str(exc) if isinstance(exc, RequirementError) else type(exc).__name__)
        code = 2
    finally:
        report.update(mode=mode, run_id=directory.name, started_at_utc=started,
                      finished_at_utc=datetime.now(timezone.utc).isoformat())
        write_new(directory, 'result.json', report)
        with (directory / 'report.md').open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(render_report(report))
    print(json.dumps({key: report[key] for key in ('run_id', 'catalog_validation', 'execution_status',
                     'requirements_status', 'product_acceptance', 'candidate_admission')}, ensure_ascii=False))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('validate', 'run'))
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    try:
        return run(args.mode, args.output_dir)
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'ERROR', 'error_code': str(exc) if isinstance(exc, RequirementError)
                          else type(exc).__name__, 'product_acceptance': 'INCOMPLETE'}))
        return 2
