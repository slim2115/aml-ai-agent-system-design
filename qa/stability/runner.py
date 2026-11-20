"""Owned, version-bound statistics demo or read-only historical LLM analysis."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import re

from qa.execution.provenance import compare_snapshots, environment, snapshot
from qa.execution.runner import pin_file, verify_files, write_new
from qa.qualification.runner import exact
from qa.reporting import combine
from qa.requirements.catalog import ROOT
from qa.stability.demo import run_demo
from qa.stability.history import HistoryError, load_history
from qa.stability.statistics import summarize
from src.qa_profile import checked_path

EXIT = {'PASS': 0, 'FAIL': 1, 'ERROR': 2, 'INCOMPLETE': 3}


class StabilityRunError(ValueError):
    """Fixed diagnostic code with no raw source values."""


def output_path(value, root=ROOT):
    root = Path(root).absolute()
    path = checked_path(Path(value) if Path(value).is_absolute() else root / value, root)
    parts = path.relative_to(root).parts
    if (len(parts) != 4 or parts[:3] != ('var', 'qa', 'stability')
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', parts[-1])):
        raise StabilityRunError('STABILITY_OUTPUT_SCOPE')
    if path.exists():
        raise StabilityRunError('STABILITY_OUTPUT_EXISTS')
    return path


def render(result):
    lines = ['# Статистическая устойчивость и бюджет', '',
        f"Режим: **{result['mode']}**. Анализ: **{result['status']}**. Привязка к версии: **{result['version_binding']}**.", '',
        'PASS относится к выполненной обработке и объявленным контролям. Независимость семейств, '
        'перенос на реальную популяцию и продуктовая приёмка не установлены.', '']
    stats = result.get('statistics', {})
    if stats:
        lines += [f"Запланировано слотов: {stats['planned_slots']}; наблюдалось: {stats['observed_slots']}; "
                  f"отсутствует записей: {stats['missing_slots']}; явно NOT_RUN: {stats['not_run_slots']}.", '',
                  'Полные показатели по вариантам, случаям и семействам — в `statistics.json`. '
                  'Повторы не увеличивают число независимых примеров. Интервал Wilson условный, '
                  'его предпосылки не проверены; парная разница описательная и не доказывает превосходство.', '',
                  '| Вариант | Статус | PASS / план | Все проверки семейства PASS / план | p50 / p95, с |',
                  '| --- | --- | --- | --- | --- |']
        def measured(value):
            return 'не измерено' if value is None else f'{value:.3f}'
        for name, arm in stats['arms'].items():
            rate, families, latency = arm['pass_rate'], arm['family_pass_rate'], arm['latency']
            lines.append(f"| {name} | {arm['status']} | {rate['numerator']} / {rate['denominator']} | "
                         f"{families['numerator']} / {families['denominator']} | "
                         f"{measured(latency['p50_seconds'])} / {measured(latency['p95_seconds'])} |")
        lines += ['', '| Вариант | Записано попыток | Известно prompt / output токенов | Неизвестно prompt / output попыток |',
                  '| --- | --- | --- | --- |']
        for name, arm in stats['arms'].items():
            usage = arm['usage']
            prompt, output = usage['prompt_tokens'], usage['output_tokens']
            lines.append(f"| {name} | {usage['recorded_attempts']} | {prompt['known_sum']} / {output['known_sum']} | "
                         f"{prompt['unknown_attempts']} / {output['unknown_attempts']} |")
        pair = stats['paired']
        lines += ['', 'Парное сравнение: ' + (f"полных пар {pair['complete_pairs']} / {pair['planned_pairs']}, "
                  f"статус {pair['status']}." if 'planned_pairs' in pair else 'не применимо: baseline отсутствует.'), '']
    if result['mode'] == 'demo':
        lines += ['| Контроль | Результат |', '| --- | --- |']
        for row in result.get('control_results', []):
            lines.append(f"| {row['id']} | {row['status']} |")
        lines += ['', 'Лимиты и полный ledger — в `demo.json`. Время виртуальное. Ограничивается '
                  'допуск следующего вызова; зависший callback принудительно не прерывается. '
                  'Ошибочная попытка учитывается как расход. PASS учёта бюджета не означает PASS ответа.', '']
    else:
        lines += ['Использованы сохранённые ответы. Новых вызовов LLM нет. '
                  'Исторический общий бюджет не объявлялся: `budget_enforcement=NOT_EVALUATED`. '
                  'Хэши связывают прочитанные файлы с данным пересчётом, а не удостоверяют старую машину.', '']
    lines += ['Неизвестный расход токенов не равен нулю. Время и токены не переводятся в деньги, энергию '
              'или SLA. Human calibration: NOT_MEASURED; product acceptance: INCOMPLETE.', '']
    if result.get('error_code'):
        lines.append('Код ошибки: `' + result['error_code'] + '`.')
    return '\n'.join(lines)


def run(output, *, source=None, root=ROOT):
    root = Path(root).absolute()
    directory = output_path(output, root)
    directory.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    write_new(directory, 'started.json', {'status': 'INCOMPLETE', 'started_at_utc': started}, root)
    result = {'schema_version': '1.0', 'run_id': directory.name, 'mode': 'history' if source else 'demo',
              'status': 'ERROR', 'analysis_status': 'INCOMPLETE', 'qualification_status': 'NOT_RUN',
              'version_binding': 'INCOMPLETE', 'population_inference': 'NOT_ESTABLISHED',
              'product_acceptance': 'INCOMPLETE', 'candidate_admission': 'NOT_EVALUATED',
              'human_calibration': 'NOT_MEASURED', 'new_model_calls': 0,
              'budget_enforcement': 'NOT_EVALUATED' if source else 'SYNTHETIC_CONTROLS',
              'live_integration': 'NOT_RUN', 'remote_ci': 'NOT_RUN'}
    pins, before, env = [], None, None

    def save(name, value):
        write_new(directory, name, value, root)
        pins.append(pin_file(directory / name, root))

    try:
        before, env = snapshot(root), environment()
        result.update(source_digest=before['digest'], environment_digest=env['digest'])
        save('source-before.json', before)
        save('environment-before.json', env)
        if source:
            imported = load_history(Path(source) if Path(source).is_absolute() else root / source, root=root)
            pins += imported['source_files']
            if verify_files(imported['source_files'], root)['status'] != 'PASS':
                raise StabilityRunError('STABILITY_HISTORY_CHANGED')
            stats = summarize(imported['plan'], imported['observations'])
            save('historical-metadata.json', imported['metadata'])
            result['analysis_status'] = combine([stats['status'], imported['metadata']['status']])
            plan, rows = imported['plan'], imported['observations']
        else:
            demo = run_demo()
            stats, plan, rows = demo['statistics'], demo['plan'], demo['observations']
            result['qualification_status'] = demo['qualification_status']
            result['analysis_status'] = combine([stats['status'], demo['qualification_status']])
            result['control_results'] = [{'id': row['id'], 'status': row['status']}
                                         for row in demo['controls'] + demo['budget_controls']]
            save('demo.json', demo)
        save('plan.json', plan)
        save('observations.json', rows)
        save('statistics.json', stats)
        result['statistics'] = stats
    except KeyboardInterrupt:
        result['error_code'] = 'INTERRUPTED'
    except Exception as exc:
        result['error_code'] = str(exc) if type(exc) in (StabilityRunError, HistoryError) else 'STABILITY_ANALYSIS_ERROR'
    finally:
        stability, env_stability = {'status': 'INCOMPLETE'}, 'INCOMPLETE'
        try:
            if before is not None:
                after = snapshot(root)
                save('source-after.json', after)
                stability = compare_snapshots(before, after)
            if env is not None:
                env_after = environment()
                save('environment-after.json', env_after)
                env_stability = 'PASS' if exact(env, env_after) else 'ERROR'
        except Exception:
            stability = {'status': 'ERROR'}
            result.setdefault('error_code', 'STABILITY_FINAL_IDENTITY_ERROR')
        integrity = verify_files(pins, root)
        result.update(source_stability=stability, environment_stability=env_stability,
                      evidence_integrity=integrity, version_binding=combine([stability['status'], env_stability, integrity['status']]))
        result['status'] = 'ERROR' if result.get('error_code') else combine([result['analysis_status'], result['version_binding']])
        result.update(started_at_utc=started, finished_at_utc=datetime.now(timezone.utc).isoformat())
        write_new(directory, 'evidence.json', {'files': pins, 'integrity': integrity}, root)
        write_new(directory, 'result.json', result, root)
        with checked_path(directory / 'report.md', root).open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(render(result))
    return EXIT[result['status']]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--source-dir', help='Read saved run_llm outputs; omission runs fixed synthetic controls.')
    args = parser.parse_args()
    try:
        return run(args.output_dir, source=args.source_dir)
    except Exception as exc:
        import json
        print(json.dumps({'status': 'ERROR', 'error_code': str(exc) if type(exc) is StabilityRunError
                          else 'STABILITY_OUTPUT_ERROR', 'product_acceptance': 'INCOMPLETE'}))
        return 2
