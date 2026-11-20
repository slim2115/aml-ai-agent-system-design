"""Supervise a reviewed response-repair experiment and bind its evidence."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

from qa.autonomy.candidates import CASE_IDS
from qa.autonomy.demo import EXPECTATIONS
from qa.autonomy.worker import validate_config
from qa.execution.processes import child_environment, run_owned
from qa.execution.provenance import compare_snapshots, environment, snapshot
from qa.execution.runner import pin_file, verify_files, write_new
from qa.reporting import combine, strict_json
from qa.scenario_checks import canonical
from qa.requirements.catalog import ROOT
from src.qa_profile import checked_path

EXIT = {'PASS': 0, 'FAIL': 1, 'ERROR': 2, 'INCOMPLETE': 3}
WORKER_FILES = ('journal.jsonl', 'worker-result.json', 'demo.json', 'baseline.json',
                'model-before.json', 'model-after.json', 'cycle-plan.json', 'cycle.json',
                'candidates.json', 'http-attempts.json')


def output_path(value, root):
    path = checked_path(Path(value) if Path(value).is_absolute() else root / value, root)
    parts = path.relative_to(root).parts
    if (len(parts) != 4 or parts[:3] != ('var', 'qa', 'autonomy')
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', parts[-1]) or path.exists()):
        raise ValueError('AUTONOMY_NEW_OUTPUT_REQUIRED')
    return path


def read(directory, name, root):
    path = checked_path(directory / name, root)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('AUTONOMY_ARTIFACT_TOO_LARGE')
    return strict_json(path.read_text('utf-8'))


def worker_status(directory, process, mode, root):
    if process.get('cleanup_status') != 'PASS':
        return 'ERROR'
    if process.get('timed_out') or process.get('interrupted'):
        return 'INCOMPLETE'
    if type(process.get('returncode')) is not int:
        return 'ERROR'
    try:
        result = read(directory, 'worker-result.json', root)
        if (type(result) is not dict or result.get('status') not in EXIT
                or result.get('mode') != mode or result.get('product_acceptance') != 'INCOMPLETE'
                or result.get('candidate_admission') != 'NOT_EVALUATED'
                or type(result.get('new_model_calls')) is not int or result['new_model_calls'] < 0
                or EXIT[result['status']] != process.get('returncode')):
            return 'ERROR'
        if mode == 'demo':
            demo = read(directory, 'demo.json', root)
            if (demo.get('status') != result['status'] or demo.get('control_count') != 11
                    or len(demo.get('controls', [])) != 11 or result['new_model_calls'] != 0
                    or [row.get('id') for row in demo['controls']] != list(EXPECTATIONS)):
                return 'ERROR'
            expected_events, statuses = [], []
            for row in demo['controls']:
                cycle = row['cycle']
                observed = [cycle['status'], cycle['provider_calls'],
                            [case['final_candidate_status'] for case in cycle['cases']]]
                expectation = list(EXPECTATIONS[row['id']])
                matches = canonical(observed) == canonical(expectation)
                if row['id'] == 'repair':
                    matches = matches and cycle['cases'][0]['first_attempt_status'] == 'FAIL' and len(cycle['observed_failed_attempts']) == 1
                if row['id'] == 'no_progress':
                    matches = matches and cycle['cases'][0]['termination_reason'] == 'NO_PROGRESS'
                status = 'PASS' if matches else 'FAIL'
                if (canonical(row['expected']) != canonical(expectation)
                        or canonical(row['observed']) != canonical(observed) or row['status'] != status
                        or row['baseline']['status'] != 'PASS'):
                    return 'ERROR'
                statuses.append(status)
                expected_events.extend({'control_id': row['id'], **event} for event in cycle['events'])
            if combine(statuses) != result['status']:
                return 'ERROR'
        elif result['status'] in ('PASS', 'FAIL'):
            cycle = read(directory, 'cycle.json', root)
            if (cycle.get('status') != result['status'] or cycle.get('provider_calls') != result['new_model_calls']
                    or result.get('model_identity_stability') != 'PASS'):
                return 'ERROR'
            expected_events = cycle['events']
        else:
            return result['status']
        journal_path = checked_path(directory / 'journal.jsonl', root)
        if journal_path.stat().st_size > 32 * 1024 * 1024:
            return 'ERROR'
        rows = [strict_json(line) for line in journal_path.read_text('utf-8').splitlines()]
        if (len(rows) > 500 or [row.get('journal_sequence') for row in rows] != list(range(1, len(rows) + 1))
                or any(type(row.get('journal_sequence')) is not int for row in rows)
                or canonical([row['data'] for row in rows]) != canonical(expected_events)):
            return 'ERROR'
        return result['status']
    except (OSError, ValueError, KeyError, TypeError):
        return 'ERROR'


def observed_failure(directory, root):
    """Retain known live failures even if final identity checks time out.

    Partial journals are evidence of completed events, never proof of a
    completed run. This reader cannot upgrade an incomplete process to PASS.
    """
    states = []
    cycle = directory / 'cycle.json'
    if cycle.exists():
        try:
            status = read(directory, 'cycle.json', root).get('status')
            if status in ('FAIL', 'ERROR'):
                states.append(status)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    journal = checked_path(directory / 'journal.jsonl', root)
    if journal.exists() and journal.stat().st_size <= 32 * 1024 * 1024:
        for line in journal.read_text('utf-8').splitlines()[:500]:
            try:
                event = strict_json(line)['data']
                if event.get('event') == 'ASSESSED' and event.get('status') in ('FAIL', 'ERROR'):
                    states.append(event['status'])
                elif event.get('event') == 'CYCLE_STOPPED' and event.get('ledger', {}).get('status') in ('FAIL', 'ERROR'):
                    states.append(event['ledger']['status'])
            except (ValueError, KeyError, TypeError, AttributeError):
                # A forcibly stopped writer can leave the last line unfinished.
                break
    return combine(states) if states else 'NOT_OBSERVED'


def render(result):
    lines = ['# Ограниченный цикл исправления ответа', '',
             f"Режим: **{result['mode']}**. Итог: **{result['status']}**. "
             f"Привязка к версии: **{result['version_binding']}**.", '',
             'Кандидаты — только перестановки идентификаторов. Исходный код, SQL, политика и эталоны '
             'не являются действиями этого цикла. Product acceptance: INCOMPLETE; candidate admission: NOT_EVALUATED.', '',
             f"Процесс: {result.get('process', {}).get('cleanup_status', 'NOT_RUN')}; "
             f"тайм-аут: {result.get('process', {}).get('timed_out', False)}.", '']
    if result.get('observed_failure') in ('FAIL', 'ERROR'):
        lines += [f"Сохранённый провал: **{result['observed_failure']}**; неполное завершение процесса его не отменяет.", '']
    if result['mode'] == 'demo':
        lines += ['`demo.json` содержит 11 заранее объявленных контролей и их полные циклы. '
                  'Время и поставщик ответов моделируются; renderer, проверка фактов и бюджет настоящие. '
                  'PASS квалификации означает совпадение ожидаемых исходов, в том числе FAIL/ERROR/INCOMPLETE.', '']
    else:
        lines += ['`cycle.json` показывает каждую попытку, первый исход и отдельный статус итогового кандидата. '
                  'Успешное исправление не стирает первый FAIL. `candidates.json` содержит проверенные '
                  'черновики, а `http-attempts.json` — локальную синтетическую телеметрию. '
                  'Проверяется фиксированный фактический каталог; поиск, полезность порядка и AML-приёмка не измеряются.', '']
    lines += ['`journal.jsonl` фиксирует резерв до запроса и каждое последующее решение. При остановке процесса '
              'последняя попытка может остаться с неизвестным исходом и расходом; не трактуйте её как ноль. '
              'Ledger ограничивает допуск, внешний тайм-аут завершает принадлежащий запуску worker. '
              'Остановка вычислений отдельного сервера Ollama этим не подтверждается.', '',
              'Повтор требует нового каталога; семь внешних профилей общего QA-плана и человеческая калибровка '
              'не закрываются этим экспериментом.', '']
    if result.get('error_code'):
        lines += ['Ошибка: `' + result['error_code'] + '`.']
    return '\n'.join(lines)


def run(output, *, mode='demo', case_ids=None, limits=None, root=ROOT, process_runner=None):
    root = Path(root).absolute()
    config = validate_config({'schema_version': '1.0', 'mode': mode,
        'case_ids': list(CASE_IDS) if case_ids is None else case_ids,
        'limits': {'max_attempts': 6, 'max_tokens': 98304, 'wall_seconds': 180.0} if limits is None else limits,
        'source_digest': '0' * 64})
    directory = output_path(output, root)
    directory.mkdir(parents=True, exist_ok=False)
    result = {'schema_version': '1.0', 'mode': mode, 'run_id': directory.name, 'status': 'ERROR',
              'execution_status': 'NOT_RUN', 'version_binding': 'INCOMPLETE',
              'product_acceptance': 'INCOMPLETE', 'candidate_admission': 'NOT_EVALUATED',
              'human_calibration': 'NOT_MEASURED', 'started_at_utc': datetime.now(timezone.utc).isoformat()}
    write_new(directory, 'started.json', {'status': 'INCOMPLETE'}, root)
    pins, before, env = [], None, None

    def save(name, value):
        write_new(directory, name, value, root)
        pins.append(pin_file(directory / name, root))

    try:
        before, env = snapshot(root), environment()
        result.update(source_digest=before['digest'], environment_digest=env['digest'])
        save('source-before.json', before)
        save('environment-before.json', env)
        config['source_digest'] = before['digest']
        save('worker-config.json', config)
        temporary = checked_path(directory / 'tmp', root)
        temporary.mkdir()
        process = (process_runner or run_owned)([sys.executable, '-I', '-B', 'qa/tools/_autonomy_worker.py',
            '--run-dir', directory.relative_to(root).as_posix()], cwd=root,
            environment=child_environment(os.environ, temporary), log_path=directory / 'worker.log',
            timeout_seconds=config['limits']['wall_seconds'] + 2)
        result['process'] = process
        for name in ('worker.log', *WORKER_FILES):
            if (directory / name).exists():
                pins.append(pin_file(directory / name, root))
        result['execution_status'] = worker_status(directory, process, mode, root)
        result['observed_failure'] = observed_failure(directory, root) if mode == 'ollama' else 'NOT_APPLICABLE'
    except KeyboardInterrupt:
        result.update(execution_status='INCOMPLETE', error_code='INTERRUPTED')
    except Exception:
        result.update(execution_status='ERROR', error_code='AUTONOMY_RUN_ERROR')
    finally:
        stability, env_status = {'status': 'INCOMPLETE'}, 'INCOMPLETE'
        try:
            if before is not None:
                after = snapshot(root)
                save('source-after.json', after)
                stability = compare_snapshots(before, after)
            if env is not None:
                after_env = environment()
                save('environment-after.json', after_env)
                env_status = 'PASS' if env == after_env else 'ERROR'
        except Exception:
            stability = {'status': 'ERROR'}
        integrity = verify_files(pins, root)
        result.update(source_stability=stability, environment_stability=env_status, evidence_integrity=integrity,
                      version_binding=combine([stability['status'], env_status, integrity['status']]))
        known = result.get('observed_failure')
        result['status'] = combine([result['execution_status'], result['version_binding']]
                                  + ([known] if known in ('FAIL', 'ERROR') else []))
        result['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        write_new(directory, 'evidence.json', {'files': pins, 'integrity': integrity}, root)
        write_new(directory, 'result.json', result, root)
        with checked_path(directory / 'report.md', root).open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(render(result))
    return EXIT[result['status']]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--mode', choices=('demo', 'ollama'), default='demo')
    parser.add_argument('--case-id', choices=CASE_IDS, action='append')
    parser.add_argument('--max-attempts', type=int, default=6)
    parser.add_argument('--max-tokens', type=int, default=98304)
    parser.add_argument('--wall-seconds', type=float, default=180.0)
    args = parser.parse_args()
    try:
        return run(args.output_dir, mode=args.mode, case_ids=args.case_id,
                   limits={'max_attempts': args.max_attempts, 'max_tokens': args.max_tokens,
                           'wall_seconds': args.wall_seconds})
    except Exception:
        print(json.dumps({'status': 'ERROR', 'error_code': 'AUTONOMY_INVALID_RUN', 'product_acceptance': 'INCOMPLETE'}))
        return 2
