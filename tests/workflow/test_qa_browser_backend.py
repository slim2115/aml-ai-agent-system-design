"""The browser-only doubles retain real workflow safety and fail closed by owner."""
import json
import shutil
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from qa.browser_backend import BrowserAnalysisLinks, create_browser_app
from qa.tools import browser_server, browser_stand, stand
from src.config import PROJECT_ROOT, get_settings
from src.qa_profile import QAProfile, checked_path
from src.runtime.policy import load_demo_policy
from src.workflow.sources import resolve_report_sources
from tests.lab.test_api import event_loop_socketpair_only

TOKEN = 'a' * 64
AUTH = {'X-Lab-Key': TOKEN}


@pytest.fixture
def profile(monkeypatch):
    profile = QAProfile('qa-' + uuid4().hex[:12])
    profile.base.mkdir(parents=True)
    (profile.base / 'owner.json').write_text(json.dumps(profile.owner_record()), encoding='utf-8')
    (profile.base / 'browser-profile.json').write_text(json.dumps(
        {**profile.owner_record(), 'mode': 'deterministic'}), encoding='utf-8')
    profile.policy_path.parent.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / 'config/demo-policy.json', profile.policy_path)
    shutil.copytree(PROJECT_ROOT / 'data/demo-v1', profile.seed_dir)
    profile.config_path.write_text(json.dumps({'host': '127.0.0.1', 'port': profile.pg_port,
        'database': profile.db_name, 'api_port': profile.api_port, 'run_id': profile.run_id,
        'marker': profile.marker, 'api_token': TOKEN,
        'roles': {role: {'user': name, 'password': 'b' * 64} for role, name in profile.role_names.items()}}),
        encoding='utf-8')
    monkeypatch.setenv('AML_QA_RUN_ID', profile.run_id)
    monkeypatch.delenv('CHROMA_PERSIST_DIR', raising=False)
    monkeypatch.delenv('SYNTHETIC_DATA_DIR', raising=False)
    get_settings.cache_clear()
    try:
        yield profile
    finally:
        get_settings.cache_clear()
        for path in (profile.base, profile.workflow_dir):
            checked_path(path)
            if path.exists():
                shutil.rmtree(path)


@pytest.fixture
def api(profile):
    app = create_browser_app(profile)
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        client.headers.update(AUTH)
        yield client
    assert app.state.browser_workflow.readiness()['state'] == 'closed'


def until(call, predicate, *, seconds=5):
    deadline = time.monotonic() + seconds
    while True:
        result = call()
        if predicate(result):
            return result
        if time.monotonic() >= deadline:
            pytest.fail('Browser fixture did not reach its expected state: ' + str(result))
        time.sleep(.01)


def prepare(api):
    assert api.post('/v1/readiness').status_code == 202
    return until(lambda: api.get('/v1/readiness').json(), lambda value: value['state'] == 'ready')


def submit(api, profile, incident='INC-000123'):
    policy = load_demo_policy(profile.policy_path)
    response = api.post('/v1/jobs?demo=true', json={'schema_version': '1.0', 'incident_id': incident,
        'window': policy.window.model_dump(mode='json')}, headers={'Idempotency-Key': uuid4().hex})
    assert response.status_code == 202, response.text
    return response.json()


def completed(api, job_id):
    result = until(lambda: api.get('/v1/jobs/' + job_id).json(),
        lambda value: value['status'] not in ('queued', 'running'))
    until(lambda: api.get('/v1/readiness').json(), lambda value: value['state'] != 'busy')
    return result


@pytest.mark.parametrize('mode', ['missing', 'ordinary', 'wrong_owner'])
def test_browser_opt_in_is_required_before_sqlite_open(profile, mode):
    path = profile.base / 'browser-profile.json'
    if mode == 'missing':
        path.unlink()
    else:
        value = {**profile.owner_record(), 'mode': 'deterministic'}
        value['mode' if mode == 'ordinary' else 'marker'] = mode
        path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='BROWSER_QA_PROFILE_REQUIRED'):
        create_browser_app(profile)
    assert not profile.workflow_path.exists()


def test_browser_profile_requires_matching_process_environment(profile, monkeypatch):
    monkeypatch.delenv('AML_QA_RUN_ID')
    with pytest.raises(ValueError, match='BROWSER_QA_PROFILE_MISMATCH'):
        create_browser_app(profile)
    assert not profile.workflow_path.exists()


def test_controls_require_token_loopback_and_strict_body(profile):
    app = create_browser_app(profile)
    with TestClient(app, client=('203.0.113.8', 50000)) as client:
        assert client.post('/qa/browser/config', json={}).status_code == 401
        assert client.post('/qa/browser/config', json={}, headers=AUTH).status_code == 403
    app = create_browser_app(profile)
    with TestClient(app, client=('127.0.0.1', 50000), headers=AUTH) as client:
        assert client.post('/qa/browser/config', json={'execution': 'arbitrary_code'}).status_code == 400
        assert client.post('/qa/browser/config', json={'prepare_failure': 'false'}).status_code == 400
        assert client.post('/qa/browser/config', json={'path': 'var/workflow/api-workflow.sqlite3'}).status_code == 400


def test_no_automatic_preparation_or_live_infrastructure(api, profile):
    assert api.get('/v1/readiness').json()['state'] == 'not_ready'
    assert api.get('/lab/status').status_code == 503
    assert api.get('/lab/kafka/analyses/status').json()['broker']['code'] == 'BROWSER_FIXTURE_NO_BROKER'
    assert api.get('/lab/kafka/analyses').json()['items'] == []
    assert '/qa/browser/config' not in api.get('/openapi.json').json()['paths']
    response = api.post('/v1/jobs?demo=true', json={'schema_version': '1.0', 'incident_id': 'INC-000123',
        'window': load_demo_policy(profile.policy_path).window.model_dump(mode='json')},
        headers={'Idempotency-Key': 'not-prepared'})
    assert response.status_code == 503 and response.json()['code'] == 'NOT_READY'


def test_preparation_failure_can_recover_explicitly(api):
    assert api.post('/qa/browser/config', json={'prepare_failure': True}).status_code == 200
    assert api.post('/v1/readiness').status_code == 202
    result = until(lambda: api.get('/v1/readiness').json(), lambda value: value['state'] != 'preparing')
    assert result == {'ready': False, 'code': 'NOT_READY', 'state': 'not_ready'}
    assert api.post('/qa/browser/config', json={}).status_code == 200
    assert prepare(api)['ready']


def test_real_template_sources_events_and_fixture_kafka_navigation(api, profile):
    prepare(api)
    accepted = submit(api, profile)
    result = completed(api, accepted['job_id'])
    assert result['status'] == 'succeeded'
    assert resolve_report_sources(result['result'])
    assert api.get('/v1/jobs/' + accepted['job_id'] + '/events').json()
    response = api.post('/qa/browser/kafka-link', json={'job_id': accepted['job_id']})
    assert response.status_code == 200, response.text
    linked = response.json()
    assert linked['job_id'] == accepted['job_id'] and linked['job_json'] == result
    assert api.post('/qa/browser/kafka-link', json={'job_id': accepted['job_id']}).json() == linked
    assert api.get('/lab/kafka/analyses').json()['items'] == [linked]
    assert api.get('/lab/kafka/analyses/' + linked['event_id']).json() == linked
    links = BrowserAnalysisLinks(profile, api.app.state.browser_workflow.store)
    assert links.list_results() == [linked]
    assert api.post('/qa/browser/kafka-link', json={'job_id': 'job-absent'}).status_code == 404


def test_real_policy_refusal_is_not_a_prefilled_fixture(api, profile):
    prepare(api)
    result = completed(api, submit(api, profile, incident='INC-000307')['job_id'])
    assert result['status'] == 'refused'
    assert result['result']['reason_code'] == 'NO_APPLICABLE_RULE'


def test_busy_controller_rejects_fixture_change_and_real_cancel_persists(api, profile):
    assert api.post('/qa/browser/config', json={'execution': 'hold'}).status_code == 200
    prepare(api)
    job = submit(api, profile)
    assert api.post('/qa/browser/config', json={}).status_code == 409
    response = api.post('/v1/jobs/' + job['job_id'] + '/cancel', headers={'Idempotency-Key': 'cancel-once'})
    assert response.status_code == 200
    assert completed(api, job['job_id'])['status'] == 'cancelled'
    assert api.get('/v1/readiness').json()['ready'] is False


def test_deadline_is_persisted_by_real_controller_timer(api, profile):
    assert api.post('/qa/browser/config', json={'execution': 'deadline'}).status_code == 200
    prepare(api)
    started = time.monotonic()
    job = submit(api, profile)
    result = completed(api, job['job_id'])
    assert result['status'] == 'timed_out'
    assert time.monotonic() - started >= 1.5
    assert result['completed_at'] >= result['deadline_at']


def test_explicit_corruption_changes_text_only_and_normal_configuration_recovers(api, profile):
    assert api.post('/qa/browser/config', json={'execution': 'corrupt_total'}).status_code == 200
    prepare(api)
    corrupt = completed(api, submit(api, profile)['job_id'])
    assert corrupt['status'] == 'succeeded'
    facts = corrupt['result']['found_facts']
    assert any('999999.00' in fact['fact'] and any(e['kind'] == 'sum' and e['value'] != '999999.00'
        for e in fact['evidence']) for fact in facts)
    assert api.post('/qa/browser/config', json={}).status_code == 200
    assert api.get('/v1/readiness').json()['ready'] is False
    prepare(api)
    normal = completed(api, submit(api, profile)['job_id'])
    assert normal['status'] == 'succeeded'
    assert not any('999999.00' in fact['fact'] for fact in normal['result']['found_facts'])


def test_marker_removal_revokes_test_control_authority(api, profile):
    (profile.base / 'browser-profile.json').unlink()
    response = api.post('/qa/browser/config', json={})
    assert response.status_code == 503 and response.json()['code'] == 'NOT_READY'


@pytest.mark.parametrize('contents', ['[]', 'null', '{', '"ordinary"', ' ' * 4097])
def test_launcher_rejects_malformed_browser_marker_before_process_start(profile, monkeypatch, contents):
    (profile.base / 'browser-profile.json').write_text(contents, encoding='utf-8')
    monkeypatch.setattr(browser_stand.subprocess, 'Popen',
        lambda *_args, **_kwargs: pytest.fail('No process may start without browser opt-in'))
    with pytest.raises(ValueError, match='QA_BROWSER_PROFILE_REQUIRED'):
        browser_stand.start_api(profile)
    assert not profile.workflow_path.exists()


def test_regular_qa_launcher_refuses_browser_mode_before_starting_infrastructure(profile, monkeypatch):
    monkeypatch.setattr(stand, 'start_postgres', lambda *_args: pytest.fail('PostgreSQL must stay stopped'))
    with pytest.raises(ValueError, match='QA_BROWSER_USE_BROWSER_STAND_LAUNCHER'):
        stand.start(profile)
    assert not profile.workflow_path.exists()


def test_browser_server_requires_opt_in_before_lease_or_state_creation(profile):
    (profile.base / 'browser-profile.json').unlink()
    with pytest.raises(ValueError, match='BROWSER_QA_PROFILE_REQUIRED'):
        browser_server.main()
    assert not profile.workflow_dir.exists()
    assert not (profile.base / 'lab/api-process.json').exists()


@pytest.mark.parametrize('change', ['valid', 'missing', 'array', 'null', 'malformed', 'oversized',
                                  'instance_id', 'root', 'qa_run_id', 'qa_owner'])
def test_only_current_owned_instance_accepts_shutdown(profile, change):
    path = profile.base / 'browser-stop-test.json'
    request = dict(root=str(profile.root), qa_run_id=profile.run_id,
        qa_owner=profile.marker, instance_id='a' * 32)
    record = dict(request)
    if change in request:
        request[change] = 'foreign'
    content = {'array': '[]', 'null': 'null', 'malformed': '{', 'oversized': ' ' * 4097}.get(
        change, json.dumps(request))
    if change != 'missing':
        path.write_text(content, encoding='utf-8')
    assert browser_server.stop_requested(profile, path, record) is (change == 'valid')


def test_browser_start_refuses_foreign_active_run_and_releases_lifecycle_lease(profile, monkeypatch):
    root = profile.base / 'lifecycle-test'
    active = root / 'var/qa/active.json'
    active.parent.mkdir(parents=True)
    foreign = {'run_id': 'qa-000000000000'}
    active.write_text(json.dumps(foreign), encoding='utf-8')
    closed = []
    class Lease:
        def __init__(self, _path):
            pass
        def close(self):
            closed.append(True)
    monkeypatch.setattr(browser_stand, 'ROOT', root)
    monkeypatch.setattr(browser_stand, 'ControllerLease', Lease)
    monkeypatch.setattr(browser_stand, 'start_api', lambda _profile: pytest.fail('Foreign active run'))
    with pytest.raises(RuntimeError, match='ANOTHER_QA_RUN_MUST_BE_STOPPED_FIRST'):
        browser_stand.start(profile)
    assert json.loads(active.read_text(encoding='utf-8')) == foreign
    assert closed == [True]


def test_browser_stop_targets_only_ui_api_and_retains_owned_data(profile, monkeypatch):
    root = profile.base / 'lifecycle-test'
    active = root / 'var/qa/active.json'
    active.parent.mkdir(parents=True)
    active.write_text(json.dumps(profile.owner_record()), encoding='utf-8')
    calls = []
    class Lease:
        def __init__(self, _path):
            pass
        def close(self):
            calls.append('lease-closed')
    monkeypatch.setattr(browser_stand, 'ROOT', root)
    monkeypatch.setattr(browser_stand, 'ControllerLease', Lease)
    monkeypatch.setattr(stand, 'stop_service', lambda owner, service: calls.append((owner.run_id, service)))
    monkeypatch.setattr(browser_stand, 'port_free', lambda port: port in (profile.ui_port, profile.api_port))
    result = browser_stand.stop(profile)
    assert calls == [(profile.run_id, 'ui'), (profile.run_id, 'api'), 'lease-closed']
    assert result['status'] == 'stopped' and result['data'] == 'retained'
    assert not active.exists()
    assert profile.config_path.exists() and profile.seed_dir.exists()


@pytest.mark.parametrize('event_id', [None, 42, [], {}, 'invalid-uuid'])
def test_malformed_fixture_event_identifier_is_unavailable_not_internal_error(api, profile, event_id):
    path = profile.base / 'browser-kafka-links.json'
    path.write_text(json.dumps({**profile.owner_record(),
        'links': [{'event_id': event_id, 'job_id': 'irrelevant'}]}), encoding='utf-8')
    response = api.get('/lab/kafka/analyses')
    assert response.status_code == 503 and response.json()['code'] == 'NOT_READY'


def test_shutdown_cancels_active_job_and_restart_preserves_real_history_and_links(profile):
    app = create_browser_app(profile)
    with TestClient(app, client=('127.0.0.1', 50000), headers=AUTH) as client:
        prepare(client)
        finished = completed(client, submit(client, profile)['job_id'])
        link = client.post('/qa/browser/kafka-link', json={'job_id': finished['job_id']}).json()
        assert client.post('/qa/browser/config', json={'execution': 'hold'}).status_code == 200
        prepare(client)
        active = submit(client, profile)
    assert app.state.browser_workflow.readiness()['state'] == 'closed'
    restarted = create_browser_app(profile)
    with TestClient(restarted, client=('127.0.0.1', 50000), headers=AUTH) as client:
        assert client.get('/v1/readiness').json()['state'] == 'not_ready'
        assert client.get('/v1/jobs/' + finished['job_id']).json() == finished
        assert client.get('/v1/jobs/' + active['job_id']).json()['status'] == 'cancelled'
        assert client.get('/lab/kafka/analyses/' + link['event_id']).json() == link
