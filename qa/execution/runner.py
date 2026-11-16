"""Execute the fixed deterministic checks and preserve unresolved plan profiles."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys

from qa.execution.adapters import assess_adapter, specs
from qa.execution.processes import child_environment, run_owned
from qa.execution.provenance import ProvenanceError, compare_snapshots, environment, snapshot
from qa.impact.planner import ImpactError, build_plan, conservative_fallback, normalize_changes
from qa.impact.runner import MAP_PATH, read_json
from qa.reporting import combine
from qa.requirements.catalog import ROOT, RequirementError, canonical_digest, load_catalog, validate_catalog
from src.qa_profile import checked_path

BASE_PROFILES = ('full_regression', 'requirements', 'ui_cli', 'ci_inputs')


class ExecutionError(ValueError):
    """Fixed code for unsafe, stale or incomplete execution inputs."""


def require(condition, code):
    if not condition:
        raise ExecutionError(code)


def output_path(value, root):
    root = Path(root).absolute()
    given = Path(value)
    directory = checked_path(given if given.is_absolute() else root / given, root)
    parts = directory.relative_to(root).parts
    require(len(parts) == 4 and parts[:3] == ('var', 'qa', 'execution')
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', parts[-1]), 'EXECUTION_OUTPUT_SCOPE')
    require(not directory.exists(), 'EXECUTION_OUTPUT_EXISTS')
    return directory


def write_new(directory, name, value, root):
    path = checked_path(directory / name, root)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def prepare_plan(changes, root):
    catalog = load_catalog(root)
    mapping = read_json(MAP_PATH, root)
    plan = build_plan(catalog, mapping, normalize_changes(changes))
    try:
        validation = validate_catalog(catalog, root)
    except RequirementError as exc:
        if str(exc) not in {'SOURCE_NOT_FOUND', 'TEST_DEFINITION_NOT_FOUND'}:
            raise
        validation = {'status': 'INCOMPLETE', 'error_code': str(exc)}
        plan = conservative_fallback(plan, catalog, 'Catalogue reference is missing; restore it before execution.')
        plan['catalog_validation_error'] = str(exc)
    plan['catalog_validation'] = validation['status']
    return plan, catalog, mapping, validation


def saved_declaration(value, root=ROOT):
    root = Path(root).absolute()
    path = checked_path(root / value, root)
    parts = path.relative_to(root).parts
    require(len(parts) == 4 and parts[:3] == ('var', 'qa', 'impact') and path.is_dir(), 'EXECUTION_PLAN_SCOPE')
    changes = normalize_changes(read_json(path / 'changes.json', root))
    saved = read_json(path / 'result.json', root)
    current, _, _, _ = prepare_plan(changes, root)
    require(type(saved) is dict and saved.get('planning_status') in {'READY', 'INCOMPLETE'}, 'INVALID_SAVED_PLAN')
    require(all(saved.get(key) == current[key] for key in ('change_set_sha256', 'catalog_sha256', 'map_sha256')),
            'STALE_IMPACT_PLAN')
    # Saved profile rows and entrypoints never provide commands to execute.
    return changes


def pin_file(path, root):
    path = checked_path(path, root)
    require(path.is_file() and path.stat().st_size <= 32 * 1024 * 1024, 'EXECUTION_ARTIFACT_INVALID')
    raw = path.read_bytes()
    require(len(raw) <= 32 * 1024 * 1024, 'EXECUTION_ARTIFACT_TOO_LARGE')
    return {'path': path.relative_to(root).as_posix(), 'sha256': sha256(raw).hexdigest(), 'size': len(raw)}


def verify_files(rows, root):
    issues = []
    for row in rows:
        try:
            if pin_file(root / row['path'], root) != row:
                issues.append(row['path'])
        except (OSError, ValueError):
            issues.append(row['path'])
    return {'status': 'ERROR' if issues else 'PASS', 'changed_or_missing': issues}


def summarize(plan, profiles, source_stability, environment_stability, integrity):
    base = [next((row for row in profiles if row['id'] == name), {'status': 'NOT_RUN'}) for name in BASE_PROFILES]
    deterministic = combine([row['status'] if row['status'] in {'PASS', 'FAIL', 'ERROR'} else 'INCOMPLETE' for row in base])
    execution = 'PASS' if all(row['status'] in {'PASS', 'FAIL'} for row in base) else 'INCOMPLETE'
    pending = [row['id'] for row in profiles if row['status'] == 'NOT_RUN']
    required = {row['id'] for row in plan.get('profiles', [])} | set(BASE_PROFILES)
    states = [row['status'] if row['status'] != 'NOT_RUN' else 'INCOMPLETE' for row in profiles if row['id'] in required]
    fulfillment = combine(states + ([] if plan.get('planning_status') == 'READY' else ['INCOMPLETE']))
    binding = combine([source_stability['status'], environment_stability, integrity['status']])
    overall = combine([deterministic, fulfillment, binding])
    return {'status': overall, 'execution_status': execution, 'deterministic_status': deterministic,
            'plan_fulfillment': fulfillment, 'version_binding': binding, 'pending_profiles': pending,
            'observed_failed_profiles': [row['id'] for row in profiles if row['status'] == 'FAIL'
                                        or row.get('observed_failure') or row.get('observed_status') == 'FAIL']}


def render(report):
    rows = ['# Единый запуск QA', '',
            'Общий итог: **' + report['status'] + '**. Детерминированные проверки: **'
            + report['deterministic_status'] + '**. Выполнение всего плана: **' + report['plan_fulfillment'] + '**.', '',
            'Продуктовая приёмка: INCOMPLETE. Допуск кандидата: NOT_EVALUATED.', '',
            '| Профиль | Результат |', '| --- | --- |']
    rows += ['| ' + row['id'] + ' | ' + row['status'] + ' |' for row in report['profiles']]
    rows += ['', 'Версия исходников: `' + str(report.get('source_digest', 'NOT_CAPTURED')) + '`.', '',
             'Связь результатов с версией: ' + report['version_binding'] + '.', '',
             'NOT_RUN означает отсутствие текущего измерения. Этот запуск не выполняет браузерные, '
             'модельные, живые SQL/API/Kafka или человеческие проверки. Их статусы не наследуются из старых отчётов.', '',
             'Хэши связывают локальные файлы и наблюдаемое окружение. Они не являются подписью независимого '
             'проверяющего, доказательством целостности ОС или всех установленных библиотек.', '']
    if report.get('error_code'):
        rows += ['Ошибка выполнения: `' + report['error_code'] + '`.', '']
    return '\n'.join(rows)


def run(changes, output, *, root=ROOT, timeout_seconds=1800):
    root = Path(root).absolute()
    require(type(timeout_seconds) is int and 1 <= timeout_seconds <= 7200, 'EXECUTION_TIMEOUT_RANGE')
    changes = normalize_changes(changes)
    directory = output_path(output, root)
    directory.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    write_new(directory, 'started.json', {'status': 'INCOMPLETE', 'started_at_utc': started, 'run_id': directory.name}, root)
    report = {'schema_version': '1.0', 'run_id': directory.name, 'status': 'ERROR', 'execution_status': 'NOT_RUN',
              'deterministic_status': 'INCOMPLETE', 'plan_fulfillment': 'INCOMPLETE', 'version_binding': 'INCOMPLETE',
              'profiles': [], 'product_acceptance': 'INCOMPLETE', 'candidate_admission': 'NOT_EVALUATED',
              'human_calibration': 'NOT_MEASURED', 'historical_judge': 'NOT_REASSESSED',
              'input_completeness': 'NOT_VERIFIED', 'remote_ci': 'NOT_RUN', 'model_identity': 'NOT_MEASURED'}
    before = env_before = plan = None
    pins = []
    try:
        before, env_before = snapshot(root), environment()
        report.update(source_digest=before['digest'], environment_digest=env_before['digest'])
        write_new(directory, 'source-before.json', before, root)
        write_new(directory, 'environment-before.json', env_before, root)
        plan, catalog, mapping, validation = prepare_plan(changes, root)
        report['plan_digest'] = canonical_digest(plan)
        for name, value in [('changes.json', changes), ('plan.json', plan), ('catalog.json', catalog),
                            ('map.json', mapping), ('catalog-validation.json', validation)]:
            write_new(directory, name, value, root)
        for name in ('source-before.json', 'environment-before.json', 'changes.json', 'plan.json',
                     'catalog.json', 'map.json', 'catalog-validation.json'):
            pins.append(pin_file(directory / name, root))
        definitions = specs(root, directory.name)
        require([item['id'] for item in definitions] == list(BASE_PROFILES), 'EXECUTION_ADAPTER_SET_INVALID')
        # All child outputs must be fresh, including requirements' separate namespace.
        for spec in definitions:
            target = checked_path(root / spec['output_dir'], root)
            require(not target.exists(), 'EXECUTION_CHILD_OUTPUT_EXISTS')
        report['profiles'] = [{'id': name, 'status': 'NOT_RUN', 'issues': ['NOT_STARTED'], 'files': []}
                              for name in dict.fromkeys([*BASE_PROFILES, *[row['id'] for row in plan['profiles']]])]
        for row in report['profiles']:
            if row['id'] not in BASE_PROFILES:
                row['issues'] = ['REQUIRES_SEPARATE_PROFILE_OR_REVIEW']
        require(validation['status'] == 'PASS', 'EXECUTION_CATALOG_INCOMPLETE')
        temporary = checked_path(directory / 'tmp', root)
        temporary.mkdir()
        child_env = child_environment(os.environ, temporary)
        for spec in definitions:
            stable = compare_snapshots(before, snapshot(root))
            require(stable['status'] == 'PASS', 'SOURCE_CHANGED_BEFORE_PROFILE')
            log_path = directory / (spec['id'] + '.log')
            argv = [sys.executable, *spec['argv_tail']]
            process = run_owned(argv, root, child_env, log_path, timeout_seconds)
            row = assess_adapter(spec, root, process['returncode'])
            row.update(process=process, command=argv, source_digest=before['digest'],
                       environment_digest=env_before['digest'], plan_digest=report['plan_digest'])
            if process['timed_out'] or process['interrupted'] or process['cleanup_status'] != 'PASS':
                row['observed_status'] = row['status']
                row['status'] = 'ERROR'
                row['issues'].append('TIMEOUT_INTERRUPT_OR_CLEANUP_FAILURE')
            row['files'].append(pin_file(log_path, root))
            pins.extend(row['files'])
            report['profiles'][list(BASE_PROFILES).index(spec['id'])] = row
            if process['timed_out'] or process['interrupted'] or process['cleanup_status'] != 'PASS':
                break
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report['error_code'] = str(exc) if isinstance(exc, (ExecutionError, RequirementError, ImpactError, ProvenanceError)) else type(exc).__name__
    except KeyboardInterrupt:
        report['error_code'] = 'INTERRUPTED'
    finally:
        stability = {'status': 'INCOMPLETE', 'added': [], 'removed': [], 'changed': []}
        env_stability = 'INCOMPLETE'
        try:
            if before is not None:
                after = snapshot(root)
                write_new(directory, 'source-after.json', after, root)
                stability = compare_snapshots(before, after)
            if env_before is not None:
                env_after = environment()
                write_new(directory, 'environment-after.json', env_after, root)
                env_stability = 'PASS' if env_before == env_after else 'ERROR'
        except (OSError, ValueError, TypeError):
            stability['status'] = 'ERROR'
            report.setdefault('error_code', 'FINAL_IDENTITY_CAPTURE_FAILED')
        integrity = verify_files(pins, root)
        report.update(source_stability=stability, environment_stability=env_stability, evidence_integrity=integrity)
        if plan is not None:
            report.update(summarize(plan, report['profiles'], stability, env_stability, integrity))
        if report.get('error_code'):
            report['status'] = 'ERROR'
        report.update(started_at_utc=started, finished_at_utc=datetime.now(timezone.utc).isoformat())
        write_new(directory, 'evidence.json', {'files': pins, 'integrity': integrity}, root)
        write_new(directory, 'result.json', report, root)
        with checked_path(directory / 'report.md', root).open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(render(report))
    print(json.dumps({key: report[key] for key in ('run_id', 'status', 'deterministic_status', 'plan_fulfillment', 'version_binding')}))
    return {'PASS': 0, 'FAIL': 1, 'ERROR': 2, 'INCOMPLETE': 3}[report['status']]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--plan-dir', help='Saved stage-2 plan; declarations are recomputed with current code.')
    inputs.add_argument('--changes-file', help='Strict declaration JSON inside the project.')
    inputs.add_argument('--changed', action='append', help='Changed path; repeat for multiple paths.')
    parser.add_argument('--model-changed', action='append', choices=('llm', 'embedding'), default=[])
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--timeout-seconds', type=int, default=1800, help='Limit for each owned deterministic child (1..7200).')
    args = parser.parse_args()
    try:
        if args.plan_dir:
            require(not args.model_changed, 'MODEL_CHANGE_REQUIRES_NEW_PLAN')
            changes = saved_declaration(args.plan_dir)
        elif args.changes_file:
            changes = normalize_changes(read_json(args.changes_file))
            changes['model_changes'].extend(args.model_changed)
        else:
            changes = {'schema_version': '1.0', 'changes': [{'status': 'M', 'path': path} for path in args.changed],
                       'model_changes': args.model_changed}
        return run(changes, args.output_dir, timeout_seconds=args.timeout_seconds)
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'ERROR', 'error_code': str(exc) if isinstance(exc, ExecutionError) else type(exc).__name__,
                          'product_acceptance': 'INCOMPLETE'}))
        return 2
