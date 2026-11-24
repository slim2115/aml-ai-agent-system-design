"""Chromium smoke of the learning form on an already running local QA server."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from qa.analyst import reader
from qa.investigations import storage
from qa.tools.check_analyst_browser import allowed_url

CHECKS = ('page_loaded', 'pack_selected', 'empty_answer_incomplete',
          'correct_answer_pass', 'rationale_not_scored', 'download_matches_pack', 'wrong_answer_fail',
          'lesson_switch_clears_feedback', 'acceptance_not_upgraded')


def output_path(output, run_id, root=ROOT):
    """Validate both identifiers before creating any artifacts or browser process."""
    directory = reader.path_for(root, output)
    relative = directory.relative_to(root).parts
    reader.require(len(relative) == 4 and relative[:3] == ('var', 'qa', 'investigation-browser')
                   and reader.SAFE_ID.fullmatch(relative[-1]) and not directory.exists(),
                   'INVESTIGATION_BROWSER_OUTPUT_INVALID')
    storage.pack_path(run_id, root)
    return directory


def _select(page, label, option):
    # A multiselect's accessible name includes its selected values. Anchor by
    # the stable visible label and type into the actual combobox. A click on a
    # filled React Aria input may focus it without opening the option list.
    control = page.locator('[data-testid="stSelectbox"], [data-testid="stMultiSelect"]').filter(
        has=page.get_by_text(label, exact=True))
    control.get_by_role('combobox').fill(option)
    control.get_by_role('combobox').press('ArrowDown')
    page.get_by_role('option', name=option, exact=True).click()


def main():
    from qa.execution.provenance import snapshot, compare_snapshots
    from qa.tools.browser_tooling import activate
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--port', type=int, choices=(8502, 18502), default=8502)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    try:
        directory = output_path(args.output_dir, args.run_id)
    except (OSError, ValueError):
        print('INVESTIGATION_BROWSER_INPUT_INVALID')
        return 2
    directory.mkdir(parents=True)
    result = {'status': 'ERROR', 'checks': [], 'run_id': args.run_id,
              'operator_practice': 'NOT_MEASURED', 'product_acceptance': 'INCOMPLETE',
              'scope': 'Automated open-rubric UI exercise only; no human competence, AML operator, live infrastructure or model measurement',
              'started_at_utc': datetime.now(timezone.utc).isoformat()}
    before = None
    try:
        before = snapshot(ROOT)
        (directory / 'source-before.json').write_text(json.dumps(before, indent=2) + '\n', 'utf-8')
        expected = storage.load_pack(args.run_id)
        reader.require(expected['assessment_enabled'] and expected['source_check'] == 'MATCH',
                       'INVESTIGATION_BROWSER_PACK_NOT_CURRENT')
        lesson, second = expected['catalog']['lessons'][:2]
        activate()
        from playwright.sync_api import sync_playwright, expect
        # Playwright's assertion timeout is independent of page timeout. Each
        # rerun rechecks source hashes and may take longer than the default 5s.
        expect.set_options(timeout=60000)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(viewport={'width': 1440, 'height': 1100},
                    locale='ru-RU', timezone_id='Europe/Moscow', service_workers='block', accept_downloads=True)
                context.route('**/*', lambda route: route.continue_()
                    if allowed_url(route.request.url, args.port) else route.abort())
                context.route_web_socket('**/*', lambda route: route.connect_to_server()
                    if allowed_url(route.url, args.port, websocket=True) else route.close())
                page = context.new_page()
                page.set_default_timeout(60000)
                page.goto(f'http://127.0.0.1:{args.port}', wait_until='domcontentloaded')
                expect(page.get_by_role('heading', name='AML — аналитик QA', exact=True)).to_be_visible()
                result['checks'].append('page_loaded')
                # Streamlit overlays its native radio input. Use the visible
                # label, then assert the underlying state without forcing a click.
                page.get_by_test_id('stSidebar').get_by_text('Учебные расследования', exact=True).click()
                expect(page.get_by_role('radio', name='Учебные расследования', exact=True)).to_be_checked()
                expect(page.get_by_role('heading', name='Учебные расследования', exact=True)).to_be_visible()
                _select(page, 'Учебный пакет', args.run_id)
                expect(page.get_by_text('Целостность пакета: MATCH · Текущий код: MATCH', exact=True)).to_be_visible()
                _select(page, 'Расследование', lesson['id'] + ' — ' + lesson['title'])
                expect(page.get_by_text(lesson['claim'], exact=True)).to_be_visible()
                result['checks'].append('pack_selected')
                submit = page.get_by_role('button', name='Проверить разбор', exact=True)
                submit.click()
                expect(page.get_by_text('Результат разбора: INCOMPLETE', exact=True)).to_be_visible()
                result['checks'].append('empty_answer_incomplete')
                for name in ('conclusion', 'root_cause', 'next_action'):
                    question = lesson['questions'][name]
                    label = next(row['label'] for row in question['options'] if row['id'] == lesson['rubric'][name])
                    _select(page, question['prompt'], label)
                for fact in ('F01', 'F02'):
                    _select(page, 'Подтверждающие факты', fact)
                    page.keyboard.press('Escape')
                page.get_by_label('Обоснование своими словами (без автоматической оценки)', exact=True).fill(
                    'Автоматизированный учебный пример. Это не человеческая оценка.')
                submit.click()
                expect(page.get_by_text('Результат разбора: PASS', exact=True)).to_be_visible()
                result['checks'].append('correct_answer_pass')
                expect(page.get_by_text('Обоснование: NOT_SCORED. Практика человека: NOT_MEASURED. '
                                       'Продуктовая приёмка: INCOMPLETE.', exact=True)).to_be_visible()
                result['checks'].append('rationale_not_scored')
                expect(page.get_by_role('button', name='Скачать учебный разбор JSON', exact=True)).to_be_visible()
                page.screenshot(path=str(directory / 'correct-answer.png'), full_page=True)
                with page.expect_download() as download:
                    page.get_by_role('button', name='Скачать учебный разбор JSON', exact=True).click()
                downloaded = directory / 'learning-answer.json'
                download.value.save_as(str(downloaded))
                reader.require(downloaded.stat().st_size <= 32768, 'INVESTIGATION_DOWNLOAD_SIZE')
                answer = json.loads(downloaded.read_text('utf-8'))
                reader.require(answer['run_id'] == args.run_id and answer['pack_fingerprint'] == expected['fingerprint']
                    and answer['answer']['lesson_id'] == lesson['id'] and answer['evaluation']['status'] == 'PASS'
                    and answer['evaluation']['operator_practice'] == 'NOT_MEASURED'
                    and answer['evaluation']['rationale_evaluation'] == 'NOT_SCORED'
                    and answer['evaluation']['product_acceptance'] == 'INCOMPLETE', 'INVESTIGATION_DOWNLOAD_CONTENT')
                result['checks'].append('download_matches_pack')
                # Download triggers a Streamlit rerun. Do not open a dropdown
                # on the old form while that rerun is replacing the DOM.
                expect(page.get_by_text('Результат разбора: PASS', exact=True)).to_have_count(0)
                expect(submit).to_be_visible()
                question = lesson['questions']['conclusion']
                wrong = next(row['label'] for row in question['options'] if row['id'] != lesson['rubric']['conclusion'])
                _select(page, question['prompt'], wrong)
                submit.click()
                expect(page.get_by_text('Результат разбора: FAIL', exact=True)).to_be_visible()
                result['checks'].append('wrong_answer_fail')
                page.screenshot(path=str(directory / 'wrong-answer.png'), full_page=True)
                _select(page, 'Расследование', second['id'] + ' — ' + second['title'])
                expect(page.get_by_text(second['claim'], exact=True)).to_be_visible()
                expect(page.get_by_text(re.compile('^Результат разбора: '))).to_have_count(0)
                expect(page.get_by_role('button', name='Скачать учебный разбор JSON', exact=True)).to_have_count(0)
                result['checks'].append('lesson_switch_clears_feedback')
                expect(page.get_by_text(re.compile('^Продуктовая приёмка: INCOMPLETE'))).to_be_visible()
                result['checks'].append('acceptance_not_upgraded')
                result['browser_version'] = browser.version
            finally:
                browser.close()
        fresh = storage.load_pack(args.run_id)
        reader.require(fresh['fingerprint'] == expected['fingerprint'] and fresh['assessment_enabled'],
                       'INVESTIGATION_BROWSER_PACK_CHANGED')
        reader.require(tuple(result['checks']) == CHECKS, 'INVESTIGATION_BROWSER_CHECKS_INCOMPLETE')
        result.update(status='PASS', pack_fingerprint=expected['fingerprint'])
    except Exception as exc:
        result.update(error_code='INVESTIGATION_BROWSER_CHECK_FAILED', error_type=type(exc).__name__)
    finally:
        try:
            after = snapshot(ROOT)
            (directory / 'source-after.json').write_text(json.dumps(after, indent=2) + '\n', 'utf-8')
            result['source_digest'] = before['digest'] if before else None
            result['source_stability'] = compare_snapshots(before, after)['status'] if before else 'INCOMPLETE'
            if result['source_stability'] != 'PASS':
                result['status'] = 'ERROR'
        except Exception:
            result.update(status='ERROR', source_stability='ERROR')
        result['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        result['files'] = [{'path': path.relative_to(ROOT).as_posix(), 'sha256': sha256(path.read_bytes()).hexdigest(),
                           'size': path.stat().st_size} for path in directory.iterdir() if path.is_file()]
        (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
