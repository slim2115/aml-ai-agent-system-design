"""Create a local change-impact plan. Does not run tests, models or Git."""
import argparse
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import re

from qa.reporting import strict_json
from qa.requirements.catalog import ROOT, RequirementError, load_catalog, validate_catalog
from qa.impact.planner import ImpactError, build_plan, conservative_fallback, normalize_changes, require
from src.qa_profile import checked_path

MAP_PATH = 'qa/impact/map-v1.json'


def read_json(name, root=ROOT):
    root = Path(root).absolute()
    supplied = Path(name)
    path = checked_path(supplied if supplied.is_absolute() else root / supplied, root)
    require(path.is_file() and path.stat().st_size <= 2 * 1024 * 1024, 'INVALID_IMPACT_INPUT_FILE')
    return strict_json(path.read_text('utf-8-sig'))


def output_path(name, root=ROOT):
    root = Path(root).absolute()
    supplied = Path(name)
    path = checked_path(supplied if supplied.is_absolute() else root / supplied, root)
    parts = path.relative_to(root).parts
    require(len(parts) == 4 and parts[:3] == ('var', 'qa', 'impact')
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', parts[-1]), 'INVALID_IMPACT_OUTPUT')
    require(not path.exists(), 'IMPACT_OUTPUT_EXISTS')
    return path


def write_json(directory, name, value, root):
    with checked_path(directory / name, root).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def markdown(value):
    text = escape(str(value), quote=True).replace('\n', ' ')
    return re.sub(r'([\\`*_{}\[\]()#+.!|])', r'\\\1', text)


def render(report):
    lines = ['# План проверок по изменениям', '',
             f"Планирование: **{report['planning_status']}**. Выполнение: **NOT_RUN**.", '',
             'Полная регрессия обязательна. План задаёт приоритеты проверок, '
             'не подтверждает их выполнение и не разрешает выпуск кандидата.', '',
             'Полнота входного списка: NOT_VERIFIED. Изменения модели вне файлов учитываются '
             'только при явном указании. Карта зависимостей составлена вручную.', '',
             '## Неизвестные пути', '']
    lines += [f'- {markdown(path)}' for path in report.get('unknown_paths', [])] or ['В объявленном списке не обнаружены.']
    if report.get('catalog_validation_error'):
        lines += ['', 'Ссылки каталога неполны: ' + markdown(report['catalog_validation_error']) + '. '
                  'План сохранён для восстановления связей; требуется весь набор проверок после исправления каталога.']
    lines += ['', '## Требования и основания', '', '| Требование | Причина выбора |', '| --- | --- |']
    for row in report.get('requirements', []):
        explanations = []
        for reason in row['reasons']:
            chain = ' → '.join([str(reason['input'] or 'Резервное правило'), *reason['chain']])
            explanations.append(markdown(chain + ': ' + reason['reason']))
        lines.append('| ' + markdown(row['id'] + ' — ' + row['title']) + ' | ' + '<br>'.join(explanations) + ' |')
    lines += ['', '## Профили проверок', '', '| Профиль | Назначение | Состояние |', '| --- | --- | --- |']
    for row in report.get('profiles', []):
        lines.append(f"| {markdown(row['id'])} | {markdown(row['description'])} | NOT_RUN |")
    lines += ['', '## Приоритетные селекторы', '', '```text', *report.get('selected_test_selectors', []), '```', '',
              'Селекторы предназначены для разбора и приоритетного запуска. Они не заменяют полную регрессию. '
              'Для requirements применяется весь каталог. Остальные профили выполняются по инструкции '
              'оператора, разделам 7 и 10; отсутствие зависимости не считается PASS.', '',
              'Продуктовая приёмка: INCOMPLETE. Допуск кандидата: NOT_EVALUATED.', '']
    if report.get('error_code'):
        lines += ['Ошибка планирования: ' + markdown(report['error_code']), '']
    return '\n'.join(lines)


def run(changes, output, root=ROOT):
    root = Path(root).absolute()
    directory = output_path(output, root)
    directory.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    write_json(directory, 'started.json', {'schema_version': '1.0', 'status': 'INCOMPLETE',
               'started_at_utc': started, 'execution_status': 'NOT_RUN'}, root)
    report = {'schema_version': '1.0', 'planning_status': 'ERROR', 'execution_status': 'NOT_RUN',
              'requirements_status': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE',
              'candidate_admission': 'NOT_EVALUATED', 'input_completeness': 'NOT_VERIFIED'}
    code = 2
    try:
        changes = normalize_changes(changes)
        catalog = load_catalog(root)
        mapping = read_json(MAP_PATH, root)
        report = build_plan(catalog, mapping, changes)
        try:
            validation = validate_catalog(catalog, root)
        except RequirementError as exc:
            if str(exc) not in {'SOURCE_NOT_FOUND', 'TEST_DEFINITION_NOT_FOUND'}:
                raise
            validation = {'status': 'INCOMPLETE', 'error_code': str(exc),
                          'scope': 'Required catalogue source or test declaration is missing; references need repair.'}
            report = conservative_fallback(report, catalog,
                'Ссылка каталога не разрешается: восстановить источник/тест и повторить полный набор проверок.')
            report['catalog_validation_error'] = str(exc)
        report['catalog_validation'] = validation['status']
        for filename, value in [('changes.json', changes), ('catalog.json', catalog), ('map.json', mapping),
                                ('catalog-validation.json', validation)]:
            write_json(directory, filename, value, root)
        code = 0 if report['planning_status'] == 'READY' else 2
    except (ValueError, OSError, KeyError, TypeError) as exc:
        report.update(planning_status='ERROR', error_code=str(exc) if isinstance(exc, (ImpactError, RequirementError)) else type(exc).__name__)
    finally:
        report.update(run_id=directory.name, started_at_utc=started,
                      finished_at_utc=datetime.now(timezone.utc).isoformat())
        write_json(directory, 'result.json', report, root)
        with checked_path(directory / 'plan.md', root).open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(render(report))
    print(json.dumps({key: report[key] for key in ('run_id', 'planning_status', 'execution_status',
                                                  'product_acceptance', 'candidate_admission')}))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument('--changed', action='append', help='Declared relative file path; repeat for multiple files.')
    inputs.add_argument('--changes-file', help='Strict JSON declaration inside the project; supports A/M/D/R.')
    parser.add_argument('--model-changed', action='append', choices=('llm', 'embedding'), default=[])
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    if not (args.changed or args.changes_file or args.model_changed):
        parser.error('Declare --changed, --changes-file or --model-changed.')
    try:
        changes = read_json(args.changes_file) if args.changes_file else {
            'schema_version': '1.0', 'changes': [{'status': 'M', 'path': name} for name in args.changed or []],
            'model_changes': []}
        changes = normalize_changes(changes)
        changes['model_changes'].extend(args.model_changed)
        return run(changes, args.output_dir)
    except (ValueError, OSError) as exc:
        print(json.dumps({'planning_status': 'ERROR', 'execution_status': 'NOT_RUN',
                          'error_code': str(exc) if isinstance(exc, ImpactError) else type(exc).__name__,
                          'product_acceptance': 'INCOMPLETE'}))
        return 2
