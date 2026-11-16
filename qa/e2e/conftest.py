"""Browser-only fixtures: bounded waits, owned addresses, no stored API key."""
from hashlib import sha256
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request
from uuid import uuid4

import pytest
from playwright.sync_api import expect
from qa.tools.browser_boundaries import browser_target_allowed


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, 'rep_' + report.when, report)


@pytest.fixture(scope='session')
def qa_token(qa_profile):
    return json.loads(qa_profile.config_path.read_text(encoding='utf-8'))['api_token']


@pytest.fixture(scope='session')
def base_url(qa_profile):
    return f'http://127.0.0.1:{qa_profile.ui_port}'


@pytest.fixture(scope='session')
def browser_context_args(browser_context_args, base_url):
    return dict(browser_context_args, base_url=base_url, viewport={'width': 1440, 'height': 1050},
                locale='ru-RU', timezone_id='Europe/Moscow', service_workers='block', accept_downloads=True)


@pytest.fixture(scope='session', autouse=True)
def browser_runtime_record(browser):
    (Path(os.environ['AML_QA_BROWSER_OUTPUT']) / 'browser.json').write_text(json.dumps({
        'engine': 'chromium', 'version': browser.version,
        'network_scope': 'Page HTTP/WebSocket routes allow only the owned UI/API loopback ports; not an OS firewall.',
    }, indent=2), encoding='utf-8')


@pytest.fixture
def context(context, qa_profile):
    def http(route):
        if browser_target_allowed(route.request.url, qa_profile):
            route.continue_()
        else:
            route.abort('blockedbyclient')
    def websocket(route):
        if browser_target_allowed(route.url, qa_profile, websocket=True):
            route.connect_to_server()
        else:
            route.close(code=1008, reason='QA address boundary')
    context.route('**/*', http)
    context.route_web_socket('**/*', websocket)
    yield context


@pytest.fixture
def qa_artifacts(request):
    folder = Path(os.environ['AML_QA_BROWSER_OUTPUT']) / 'artifacts' / sha256(request.node.nodeid.encode()).hexdigest()[:16]
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@pytest.fixture
def page(context, request, qa_artifacts):
    page = context.new_page()
    page.set_default_timeout(20000)
    page.set_default_navigation_timeout(30000)
    expect.set_options(timeout=20000)
    secret_flow = request.node.get_closest_marker('swagger_key') is not None
    if not secret_flow:
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
    try:
        yield page
    finally:
        report = getattr(request.node, 'rep_call', None)
        failed = report is None or report.failed
        if not secret_flow:
            if failed and not page.is_closed():
                try:
                    page.screenshot(path=str(qa_artifacts / 'failure.png'), full_page=True, timeout=5000)
                except Exception:
                    pass
            context.tracing.stop(path=str(qa_artifacts / 'trace.zip') if failed else None)
        (qa_artifacts / 'capture.json').write_text(json.dumps({
            'node_id': request.node.nodeid, 'failed': failed,
            'trace_policy': 'disabled_for_secret_entry' if secret_flow else 'retain_on_failure',
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        page.close()


@pytest.fixture
def qa_api(qa_profile, qa_token, qa_artifacts):
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(method, path, *, json=None, headers=None, expected=200):
        import json as codec
        if not path.startswith('/') or path.startswith('//') or '://' in path:
            raise ValueError('QA_RELATIVE_API_PATH_REQUIRED')
        supplied = {'X-Lab-Key': qa_token, **(headers or {})}
        body = codec.dumps(json).encode() if json is not None else None
        if body is not None:
            supplied['Content-Type'] = 'application/json'
        request = Request(qa_profile.api_url + path, data=body, headers=supplied, method=method)
        try:
            response = opener.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError('QA_RESPONSE_TOO_LARGE')
            payload = codec.loads(raw) if raw else None
            # Retain correlation evidence for passing tests too. Never retain
            # request headers, bodies or arbitrary response text here.
            observed = {'at_utc': datetime.now(timezone.utc).isoformat(), 'method': method,
                        'path': path.split('?', 1)[0], 'http_status': response.status}
            fields = ('job_id', 'event_id', 'incident_id', 'report_id', 'report_version',
                      'decision_id', 'revision', 'status', 'state', 'code', 'ready')
            if isinstance(payload, dict):
                observed['response'] = {key: payload[key] for key in fields if key in payload}
                result = payload.get('result')
                if isinstance(result, dict):
                    observed['result'] = {key: result[key] for key in fields if key in result}
            elif isinstance(payload, list):
                observed['job_ids'] = [row['job_id'] for row in payload if isinstance(row, dict) and 'job_id' in row]
            with (qa_artifacts / 'observations.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(codec.dumps(observed, ensure_ascii=False) + '\n')
            if response.status != expected:
                code = payload.get('code', 'UNKNOWN') if isinstance(payload, dict) else 'UNKNOWN'
                raise AssertionError(f'API status {response.status}, expected {expected}; code={code}')
            return payload
    return call


@pytest.fixture
def browser_control(qa_api):
    def control(**overrides):
        deadline = time.monotonic() + 10
        while qa_api('GET', '/v1/readiness')['state'] in {'busy', 'preparing'}:
            if time.monotonic() >= deadline:
                raise AssertionError('Previous QA job/preparation did not settle')
            time.sleep(.1)
        options = {'prepare_failure': False, 'execution': 'normal', **overrides}
        if os.environ.get('AML_QA_BROWSER_DEFECT') == 'corrupt_total' and options['execution'] == 'normal':
            options['execution'] = 'corrupt_total'
        return qa_api('POST', '/qa/browser/config', json=options)
    return control


@pytest.fixture(autouse=True)
def reset_browser_state(request, qa_api, browser_control):
    deterministic = os.environ.get('AML_QA_BROWSER_MODE') == 'deterministic'
    if deterministic:
        browser_control()
    yield
    if deterministic:
        for job in qa_api('GET', '/v1/jobs'):
            if job['status'] in {'queued', 'running'}:
                qa_api('POST', '/v1/jobs/' + job['job_id'] + '/cancel',
                       headers={'Idempotency-Key': 'qa-cleanup-' + uuid4().hex})
