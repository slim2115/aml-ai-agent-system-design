"""Read-only Chromium smoke of an already running local QA analyst UI."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def allowed_url(url, port, *, websocket=False):
    try:
        value = urlsplit(url)
        return (value.scheme == ('ws' if websocket else 'http') and value.hostname == '127.0.0.1'
                and value.port == port and value.username is None and value.password is None)
    except ValueError:
        return False


def main():
    from qa.analyst import reader
    from qa.execution.provenance import snapshot, compare_snapshots
    from qa.tools.browser_tooling import activate
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--port', type=int, choices=(8502, 18502), default=8502)
    parser.add_argument('--kind', choices=reader.KINDS, default='execution')
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    try:
        directory = reader.path_for(ROOT, args.output_dir)
        relative = directory.relative_to(ROOT).parts
        reader.require(len(relative) == 4 and relative[:3] == ('var', 'qa', 'analyst-browser')
                       and reader.SAFE_ID.fullmatch(relative[-1]) and not directory.exists(),
                       'ANALYST_BROWSER_OUTPUT_INVALID')
        reader.run_path(ROOT, args.kind, args.run_id)
    except (OSError, ValueError):
        print('ANALYST_BROWSER_INPUT_INVALID')
        return 2
    directory.mkdir(parents=True)
    result = {'status': 'ERROR', 'checks': [], 'kind': args.kind, 'run_id': args.run_id,
              'product_acceptance': 'INCOMPLETE', 'scope': 'Selected QA analyst view only; no AML operator, API, model or integration run',
              'started_at_utc': datetime.now(timezone.utc).isoformat()}
    before = None
    try:
        before = snapshot(ROOT)
        expected = reader.load_run(args.kind, args.run_id, verify=True)
        reader.require(expected['read_status'] == 'PASS', 'ANALYST_BROWSER_EVIDENCE_UNREADABLE')
        activate()
        from playwright.sync_api import sync_playwright, expect
        from qa.analyst.app import LABELS
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(viewport={'width': 1440, 'height': 1100},
                    locale='ru-RU', timezone_id='Europe/Moscow', service_workers='block', accept_downloads=False)
                context.route('**/*', lambda route: route.continue_() if allowed_url(route.request.url, args.port) else route.abort())
                context.route_web_socket('**/*', lambda route: route.connect_to_server()
                    if allowed_url(route.url, args.port, websocket=True) else route.close())
                page = context.new_page()
                page.set_default_timeout(30000)
                page.goto(f'http://127.0.0.1:{args.port}', wait_until='domcontentloaded')
                expect(page.get_by_role('heading', name='AML — аналитик QA', exact=True)).to_be_visible()
                result['checks'].append('page_loaded')
                page.get_by_label('Тип проверки', exact=True).click()
                page.get_by_role('option', name=LABELS[args.kind], exact=True).click()
                # Streamlit reruns asynchronously. Wait for the selected kind's
                # content before opening a selector left over from the old view.
                expect(page.get_by_role('heading', name=LABELS[args.kind], exact=True)).to_be_visible()
                page.get_by_label('Сохранённый запуск', exact=True).click()
                page.get_by_role('option', name=args.run_id, exact=True).click()
                expect(page.get_by_text('Запуск: ' + args.run_id, exact=True)).to_be_visible()
                result['checks'].append('run_selected')
                expect(page.get_by_text(re.compile('^Записанный итог: ' + expected['recorded_status']))).to_be_visible()
                result['checks'].append('recorded_verdict_preserved')
                page.get_by_role('button', name='Проверить файлы и текущий код', exact=True).click()
                expect(page.get_by_text('Свидетельства по локальному перечню: ' + expected['evidence_check'], exact=True)).to_be_visible(timeout=60000)
                expect(page.get_by_text('Совпадение с текущими исходниками: ' + expected['source_check'], exact=True)).to_be_visible()
                result['checks'].append('explicit_verification')
                expect(page.get_by_text(re.compile('^Продуктовая приёмка: INCOMPLETE'))).to_be_visible()
                result['checks'].append('acceptance_not_upgraded')
                page.screenshot(path=str(directory / 'verified.png'), full_page=True)
                page.get_by_role('button', name='Обновить список', exact=True).click()
                expect(page.get_by_text('Совпадение с текущими исходниками: NOT_CHECKED', exact=True)).to_be_visible()
                result['checks'].append('verification_not_cached')
                result.update(browser_version=browser.version, recorded_status=expected['recorded_status'],
                    evidence_check=expected['evidence_check'], source_check=expected['source_check'])
            finally:
                browser.close()
        result['status'] = 'PASS'
    except Exception as exc:
        result['error_code'] = 'ANALYST_BROWSER_CHECK_FAILED'
        result['error_type'] = type(exc).__name__
    finally:
        try:
            after = snapshot(ROOT)
            result['source_digest'] = before['digest'] if before else None
            result['source_stability'] = compare_snapshots(before, after)['status'] if before else 'INCOMPLETE'
            if result['source_stability'] != 'PASS':
                result['status'] = 'ERROR'
        except Exception:
            result.update(status='ERROR', source_stability='ERROR')
        result['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        result['files'] = [{'path': path.relative_to(ROOT).as_posix(), 'sha256': sha256(path.read_bytes()).hexdigest()}
                           for path in directory.iterdir() if path.is_file()]
        (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
