"""Real file boundary and freshness tests; no services, model calls or old evidence writes."""
from hashlib import sha256
import json
import os

import pytest

from qa.analyst import reader as r
from qa.execution.provenance import snapshot
from tests.dark_factory.test_execution_provenance import project, write


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def pin(path, root):
    raw = path.read_bytes()
    return {'path': path.relative_to(root).as_posix(), 'size': len(raw), 'sha256': sha256(raw).hexdigest()}


def make_run(root, kind='execution', name='sample', result=None, details=None):
    directory = root / 'var/qa' / kind / name
    directory.mkdir(parents=True)
    source = snapshot(root)
    value = {'schema_version': '1.0', 'run_id': name, 'status': 'INCOMPLETE',
             'source_digest': source['digest'], 'product_acceptance': 'INCOMPLETE',
             'candidate_admission': 'NOT_EVALUATED', 'profiles': [
                 {'id': 'full_regression', 'status': 'PASS', 'metrics': {'collected_tests': 12}},
                 {'id': 'llm', 'status': 'NOT_RUN', 'issues': ['REQUIRES_SEPARATE_PROFILE_OR_REVIEW']}],
             'pending_profiles': ['llm'], **(result or {})}
    artifacts = {'source-before.json': source, **(details or {})}
    save(directory / 'result.json', value)
    for filename, content in artifacts.items():
        save(directory / filename, content)
    save(directory / 'evidence.json', {'files': [pin(directory / filename, root) for filename in artifacts]})
    return directory


def test_read_does_not_claim_verification_or_acceptance(project, monkeypatch):
    directory = make_run(project)
    before = {p: p.read_bytes() for p in directory.iterdir()}
    monkeypatch.setattr(r, 'snapshot', lambda _: pytest.fail('view must not scan sources implicitly'))
    view = r.load_run('execution', 'sample', root=project)
    assert view['read_status'] == 'PASS' and view['recorded_status'] == 'INCOMPLETE'
    assert view['evidence_check'] == view['source_check'] == 'NOT_CHECKED'
    assert view['product_acceptance'] == 'INCOMPLETE'
    assert view['summary']['pending_profiles'] == 'llm'
    assert {p: p.read_bytes() for p in directory.iterdir()} == before


def test_verify_checks_actual_sources_without_upgrading_stored_result(project):
    make_run(project)
    view = r.load_run('execution', 'sample', root=project, verify=True)
    assert view['evidence_check'] == view['source_check'] == 'MATCH'
    assert view['recorded_status'] == 'INCOMPLETE' and view['checked_at_utc']
    write(project, 'src/service.py', 'changed')
    changed = r.load_run('execution', 'sample', root=project, verify=True)
    assert changed['evidence_check'] == 'MATCH' and changed['source_check'] == 'DIFFERENT'
    assert changed['changed_sources'] == [{'change': 'changed', 'path': 'src/service.py'}]


def test_unfinished_run_is_not_assumed_running_or_passed(project):
    directory = project / 'var/qa/autonomy/unfinished'
    directory.mkdir(parents=True)
    save(directory / 'started.json', {'status': 'INCOMPLETE'})
    view = r.load_run('autonomy', 'unfinished', root=project, verify=True)
    assert view['read_status'] == view['recorded_status'] == 'INCOMPLETE'
    assert view['issues'] == ['FINAL_RESULT_MISSING']
    assert view['source_check'] == 'NOT_CHECKED'


@pytest.mark.parametrize('name', ['../sample', '/sample', 'C:/sample', 'x\\y', 'x:y', '.', '', 'x' * 65])
def test_no_user_path_can_escape_run_selector(project, name):
    view = r.load_run('execution', name, root=project)
    assert view['read_status'] == 'ERROR' and view['issues'] == ['ANALYST_RUN_INVALID']


@pytest.mark.parametrize('malformed', ['{"status":"PASS","status":"FAIL"}', '{"value":NaN}', '[]', '{'])
def test_invalid_result_is_visible_error(project, malformed):
    directory = make_run(project)
    (directory / 'result.json').write_text(malformed, encoding='utf-8')
    view = r.load_run('execution', 'sample', root=project)
    assert view['read_status'] == 'ERROR' and view['recorded_status'] == 'UNAVAILABLE'


@pytest.mark.parametrize('change', ['missing', 'modified'])
def test_evidence_failure_does_not_erase_recorded_fail(project, change):
    directory = make_run(project, result={'status': 'FAIL'})
    source = directory / 'source-before.json'
    if change == 'missing':
        source.unlink()
    else:
        source.write_text('{}', encoding='utf-8')
    view = r.load_run('execution', 'sample', root=project, verify=True)
    assert view['recorded_status'] == 'FAIL'
    assert view['evidence_check'] == 'ERROR' and view['source_check'] == 'UNAVAILABLE'


@pytest.mark.parametrize('target', ['.env', 'var/postgres/lab-connection.json',
    'var/qa/execution/other/result.json', 'var/workflow/api-workflow.sqlite3', '../outside', 'C:/outside'])
def test_manifest_cannot_reference_unrelated_files(project, target, monkeypatch):
    directory = make_run(project)
    save(directory / 'evidence.json', {'files': [{'path': target, 'sha256': '0' * 64, 'size': 1}]})
    calls = []
    original = r.Reader.raw
    def guarded(self, name):
        calls.append(name)
        return original(self, name)
    monkeypatch.setattr(r.Reader, 'raw', guarded)
    view = r.load_run('execution', 'sample', root=project, verify=True)
    assert view['read_status'] == 'ERROR' and target not in calls


@pytest.mark.parametrize('bad', ['duplicate', 'boolean_size', 'negative_size', 'empty', 'unknown_key'])
def test_manifest_is_strict(project, bad):
    directory = make_run(project)
    evidence = json.loads((directory / 'evidence.json').read_text('utf-8'))
    if bad == 'duplicate': evidence['files'] *= 2
    if bad == 'boolean_size': evidence['files'][0]['size'] = True
    if bad == 'negative_size': evidence['files'][0]['size'] = -1
    if bad == 'empty': evidence['files'] = []
    if bad == 'unknown_key': evidence['files'][0]['execute'] = 'command'
    save(directory / 'evidence.json', evidence)
    assert r.load_run('execution', 'sample', root=project)['read_status'] == 'ERROR'


def test_hardlink_is_rejected_before_content_read(project):
    directory = make_run(project)
    original = directory / 'result.json'
    original.rename(directory / 'original.json')
    os.link(directory / 'original.json', original)
    assert r.load_run('execution', 'sample', root=project)['issues'] == ['ANALYST_PATH_UNSAFE']


def test_file_and_directory_bounds_fail_explicitly(project, monkeypatch):
    directory = make_run(project)
    monkeypatch.setattr(r, 'MAX_FILE', 16)
    assert r.load_run('execution', 'sample', root=project)['issues'] == ['ANALYST_SIZE_LIMIT']
    monkeypatch.setattr(r, 'MAX_RUNS', 0)
    with pytest.raises(r.EvidenceError, match='ANALYST_TOO_MANY_RUNS'):
        r.list_runs('execution', project)


def test_readers_handle_each_report_family(project):
    for kind in r.KINDS:
        details, result = {}, {}
        if kind == 'autonomy':
            result = {'mode': 'ollama', 'status': 'ERROR'}
        make_run(project, kind, result=result, details=details)
        assert r.load_run(kind, 'sample', root=project, verify=True)['evidence_check'] == 'MATCH'


def test_changed_detail_never_renders_as_valid(project):
    directory = make_run(project, details={'plan.json': {'requirements': [], 'profiles': []}})
    save(directory / 'plan.json', {'requirements': [], 'profiles': [], 'extra': 'changed'})
    view = r.load_run('execution', 'sample', root=project)
    assert view['read_status'] == 'ERROR' and view['tables'] == {}
    assert view['issues'] == ['ANALYST_DETAIL_CHANGED']


def test_repair_and_unknown_usage_are_projected_without_raw_payloads(project):
    from qa.autonomy.demo import run_demo
    demo = run_demo(root=r.ROOT, emit=lambda event: None)
    make_run(project, 'autonomy', result={'status': 'PASS', 'mode': 'demo'}, details={'demo.json': demo})
    view = r.load_run('autonomy', 'sample', root=project)
    assert view['read_status'] == 'PASS', view['issues']
    repair = next(row for row in view['tables']['Случаи и исправления'] if row['control'] == 'repair')
    assert repair['first_attempt_status'] == repair['status'] == 'FAIL'
    assert repair['final_candidate_status'] == 'PASS'
    unknown = next(row for row in view['tables']['Бюджет'] if row['control'] == 'unknown_usage')
    assert unknown['total_tokens'] is None and unknown['unknown_usage_attempts'] == 1
    assert not any('raw' in row or 'feedback' in row for values in view['tables'].values() for row in values)


def test_result_change_during_verification_is_detected(project, monkeypatch):
    directory = make_run(project)
    original = r.snapshot
    def change(root):
        data = json.loads((directory / 'result.json').read_text('utf-8'))
        data['status'] = 'PASS'
        save(directory / 'result.json', data)
        return original(root)
    monkeypatch.setattr(r, 'snapshot', change)
    view = r.load_run('execution', 'sample', root=project, verify=True)
    assert view['read_status'] == 'ERROR' and 'ANALYST_FILE_CHANGED' in view['issues']


def test_list_is_read_only_and_does_not_guess_latest_by_mtime(project):
    make_run(project, name='a-newer')
    make_run(project, name='z-older')
    assert r.list_runs('execution', project) == ['z-older', 'a-newer']
    assert r.list_runs('environments', project) == []


def test_result_source_digest_must_match_snapshot(project):
    make_run(project, result={'source_digest': '0' * 64})
    view = r.load_run('execution', 'sample', root=project, verify=True)
    assert view['source_check'] == 'UNAVAILABLE'
    assert view['issues'] == ['ANALYST_SOURCE_IDENTITY_MISMATCH']


@pytest.mark.parametrize('url,websocket,allowed', [
    ('http://127.0.0.1:8502/', False, True),
    ('ws://127.0.0.1:8502/_stcore/stream', True, True),
    ('https://127.0.0.1:8502/', False, False),
    ('http://localhost:8502/', False, False),
    ('http://127.0.0.1:8501/', False, False),
    ('http://127.0.0.1:8502@example.com/', False, False),
    ('http://user@127.0.0.1:8502/', False, False),
    ('http://example.com/', False, False),
    ('file:///C:/Windows/win.ini', False, False),
    ('http://127.0.0.1:bad/', False, False),
    ('ws://127.0.0.1:8502/', False, False),
])
def test_browser_routes_stay_on_owned_loopback(url, websocket, allowed):
    from qa.tools.check_analyst_browser import allowed_url
    assert allowed_url(url, 8502, websocket=websocket) is allowed
