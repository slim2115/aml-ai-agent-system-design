"""Fail-closed QA selection and local storage boundaries, without live services."""
import json
import os
from pathlib import Path

import pytest

from qa.tools import environment
from src import qa_profile
from src.qa_profile import QAProfile, checked_path
from src.workflow import store


@pytest.fixture
def profile(tmp_path):
    value = QAProfile('qa-123456abcdef', root=tmp_path)
    value.base.mkdir(parents=True)
    (value.base / 'owner.json').write_text(json.dumps(value.owner_record()), encoding='utf-8')
    return value


def test_qa_is_explicitly_opt_in_and_absence_does_not_read_owner(monkeypatch):
    monkeypatch.delenv('AML_QA_RUN_ID', raising=False)
    monkeypatch.setattr(QAProfile, 'validate', lambda _self: pytest.fail('unexpected owner read'))
    assert qa_profile.get_qa_profile() is None


def test_selected_profile_uses_owned_namespace(profile, monkeypatch):
    monkeypatch.setattr(qa_profile, 'ROOT', profile.root)
    monkeypatch.setenv('AML_QA_RUN_ID', profile.run_id)
    assert qa_profile.get_qa_profile() == profile


@pytest.mark.parametrize('run_id', ['', 'qa-123', 'qa-123456ABCDEf', 'qa-123456abcdef/other',
                                   '../qa-123456abcdef', 'qa-123456abcdef\n'])
def test_invalid_explicit_run_cannot_fall_back_to_default(profile, monkeypatch, run_id):
    monkeypatch.setattr(qa_profile, 'ROOT', profile.root)
    monkeypatch.setenv('AML_QA_RUN_ID', run_id)
    with pytest.raises(ValueError, match='INVALID_QA_RUN_ID'):
        qa_profile.get_qa_profile()


def test_missing_owner_is_an_error_instead_of_a_default_profile(profile, monkeypatch):
    monkeypatch.setattr(qa_profile, 'ROOT', profile.root)
    monkeypatch.setenv('AML_QA_RUN_ID', 'qa-000000000000')
    with pytest.raises(ValueError, match='QA_OWNER_MISMATCH'):
        qa_profile.get_qa_profile()


@pytest.mark.parametrize('change', ['root', 'run_id', 'marker', 'schema_version',
                                  'missing', 'not_object', 'not_json', 'oversized'])
def test_owner_must_identify_exact_run_and_root(profile, change):
    owner = profile.owner_record()
    if change in ('root', 'run_id', 'marker'):
        owner[change] = 'foreign'
    elif change == 'schema_version':
        owner[change] = 2
    elif change == 'missing':
        owner.pop('marker')
    elif change == 'not_object':
        owner = []
    content = json.dumps(owner)
    if change == 'not_json':
        content = '{'
    elif change == 'oversized':
        content = content + ' ' * 4096
    (profile.base / 'owner.json').write_text(content, encoding='utf-8')
    with pytest.raises(ValueError, match='QA_OWNER_MISMATCH'):
        profile.validate()


def test_profile_root_must_be_absolute():
    with pytest.raises(ValueError, match='INVALID_QA_ROOT'):
        QAProfile('qa-123456abcdef', root=Path('relative'))


def test_distinct_runs_separate_data_namespaces_but_reuse_reserved_ports(profile):
    other = QAProfile('qa-fedcba654321', root=profile.root)
    for attribute in ('base', 'workflow_dir', 'workflow_path', 'config_path', 'seed_dir',
                      'policy_path', 'export_dir', 'chroma_dir', 'marker', 'db_name',
                      'compose_project', 'topic', 'dlq_topic', 'analysis_topic',
                      'analysis_dlq', 'group', 'analysis_group'):
        assert getattr(profile, attribute) != getattr(other, attribute)
    assert not set(profile.role_names.values()) & set(other.role_names.values())
    assert len({profile.topic, profile.dlq_topic, profile.analysis_topic, profile.analysis_dlq}) == 4
    assert {profile.api_port, profile.ui_port, profile.pg_port, profile.kafka_port}.isdisjoint(
        {8765, 8501, 55432, 9092})
    # Launchers serialize these reserved ports; run IDs do not imply parallel stands.
    assert (profile.api_port, profile.ui_port, profile.pg_port, profile.kafka_port) == (
        other.api_port, other.ui_port, other.pg_port, other.kafka_port)


@pytest.mark.parametrize('kind', ['outside', 'traversal'])
def test_checked_path_rejects_lexical_escape_before_creation(profile, kind):
    target = (profile.root.parent / 'outside') if kind == 'outside' else (
        profile.base / '..' / 'another-run' / 'output.json')
    with pytest.raises(ValueError, match='INVALID_QA_PATH'):
        checked_path(target, profile.root)


@pytest.mark.parametrize('link_type', ['is_symlink', 'is_junction'])
def test_checked_path_rejects_linked_ancestor_without_os_privileges(profile, monkeypatch, link_type):
    # Simulate filesystem link metadata instead of requiring Windows symlink rights.
    monkeypatch.setattr(Path, link_type, lambda value: value == profile.base.parent, raising=False)
    with pytest.raises(ValueError, match='INVALID_QA_PATH'):
        checked_path(profile.base / 'logs/result.json', profile.root)


def test_checked_path_rejects_real_hardlinked_file(profile):
    original = profile.root / 'original.json'
    original.write_text('protected', encoding='utf-8')
    alias = profile.base / 'result.json'
    os.link(original, alias)
    with pytest.raises(ValueError, match='INVALID_QA_PATH'):
        checked_path(alias, profile.root)
    assert original.read_text(encoding='utf-8') == 'protected'


def test_child_environment_has_isolated_cache_and_temp_without_mutating_parent(profile, monkeypatch):
    for key in ('PYTHONPATH', 'PYTHONHOME', 'DATABASE_URL', 'SQLALCHEMY_DATABASE_URI',
                'CHROMA_PERSIST_DIR', 'SYNTHETIC_DATA_DIR', 'KNOWLEDGE_BASE_COLLECTION',
                'TEMP', 'TMP', 'HF_HOME', 'HF_HUB_CACHE', 'TORCH_HOME', 'XDG_CACHE_HOME',
                'MPLCONFIGDIR', 'SENTENCE_TRANSFORMERS_HOME', 'AML_QA_RUN_ID'):
        monkeypatch.setenv(key, 'inherited-ordinary-value')
    before = dict(os.environ)
    entries_before = sorted(profile.base.rglob('*'))
    child = environment.child_environment(profile)
    assert dict(os.environ) == before
    assert sorted(profile.base.rglob('*')) == entries_before
    for key in ('PYTHONPATH', 'PYTHONHOME', 'DATABASE_URL', 'SQLALCHEMY_DATABASE_URI'):
        assert key not in child
    for key in ('TEMP', 'TMP', 'HF_HOME', 'HF_HUB_CACHE', 'SENTENCE_TRANSFORMERS_HOME',
                'TORCH_HOME', 'XDG_CACHE_HOME', 'MPLCONFIGDIR', 'CHROMA_PERSIST_DIR',
                'SYNTHETIC_DATA_DIR'):
        assert Path(child[key]).is_absolute() and Path(child[key]).is_relative_to(profile.base)
    assert child['AML_QA_RUN_ID'] == profile.run_id
    assert child['KNOWLEDGE_BASE_COLLECTION'] == 'aml_qa_' + profile.suffix
    assert child['AML_WORKFLOW_BACKEND'] == 'api'
    assert child['PYTHON_DOTENV_DISABLED'] == '1'
    assert child['HF_HUB_OFFLINE'] == child['TRANSFORMERS_OFFLINE'] == '1'
    assert child['OLLAMA_BASE_URL'] == 'http://127.0.0.1:11434'
    assert Path(child['EMBEDDING_MODEL_PATH']).is_relative_to(profile.root / 'var/models')
    assert Path(child['EMBEDDING_MANIFEST_PATH']).is_relative_to(profile.root / 'var/models')


def test_environment_does_not_ignore_invalid_owner(profile):
    (profile.base / 'owner.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='QA_OWNER_MISMATCH'):
        environment.child_environment(profile)


@pytest.mark.parametrize('target', ['ordinary', 'foreign_run', 'foreign_workflow'])
def test_json_writes_reject_other_namespaces_without_creating_files(profile, target):
    paths = {
        'ordinary': profile.root / 'var/lab/connection.json',
        'foreign_run': QAProfile('qa-000000000000', root=profile.root).base / 'state.json',
        'foreign_workflow': QAProfile('qa-000000000000', root=profile.root).workflow_dir / 'state.json',
    }
    path = paths[target]
    with pytest.raises(ValueError, match='INVALID_QA_OUTPUT'):
        environment.write_json(profile, path, {'test': True})
    assert not path.exists() and not path.parent.exists()


def test_json_exclusive_write_preserves_existing_owned_evidence(profile):
    path = profile.base / 'evidence/result.json'
    environment.write_json(profile, path, {'original': True}, exclusive=True)
    with pytest.raises(FileExistsError):
        environment.write_json(profile, path, {'replacement': True}, exclusive=True)
    assert json.loads(path.read_text(encoding='utf-8')) == {'original': True}
    workflow_path = profile.workflow_dir / 'test-state.json'
    environment.write_json(profile, workflow_path, {'run_id': profile.run_id})
    assert json.loads(workflow_path.read_text(encoding='utf-8')) == {'run_id': profile.run_id}


@pytest.mark.parametrize('target', ['ordinary', 'foreign_run', 'traversal'])
def test_sqlite_rejects_other_namespaces_before_connect_or_mkdir(profile, monkeypatch, target):
    monkeypatch.setattr(store, 'get_qa_profile', lambda: profile)
    monkeypatch.setattr(store.sqlite3, 'connect', lambda *_args, **_kwargs: pytest.fail('SQLite opened'))
    paths = {
        'ordinary': profile.root / 'var/workflow/api-workflow.sqlite3',
        'foreign_run': QAProfile('qa-000000000000', root=profile.root).workflow_path,
        'traversal': profile.workflow_dir / '..' / 'api-workflow.sqlite3',
    }
    with pytest.raises(store.WorkflowError, match='INVALID_INPUT_FORMAT'):
        store.SQLiteStore(paths[target])
    assert not (profile.root / 'var/workflow').exists()


def test_sqlite_default_is_created_only_inside_active_run(profile, monkeypatch):
    monkeypatch.setattr(store, 'get_qa_profile', lambda: profile)
    database = store.SQLiteStore()
    assert database.path == profile.workflow_dir / 'workflow.sqlite3'
    assert database.path.is_file()
    assert not (profile.root / 'var/workflow/workflow.sqlite3').exists()


def test_atomic_state_write_retries_windows_sharing_violation_without_partial_json(profile, monkeypatch):
    from qa.tools import environment
    target = profile.base / 'state.json'
    target.write_text('{"status":"old"}', encoding='utf-8')
    original = Path.replace
    calls = []

    def transient(path, destination):
        calls.append(destination)
        assert json.loads(target.read_text(encoding='utf-8')) == {'status': 'old'}
        if len(calls) == 1:
            error = PermissionError('sharing violation')
            error.winerror = 32
            raise error
        return original(path, destination)

    monkeypatch.setattr(Path, 'replace', transient)
    monkeypatch.setattr(environment.time, 'sleep', lambda _delay: None)
    environment.write_json(profile, target, {'status': 'new'})
    assert len(calls) == 2
    assert json.loads(target.read_text(encoding='utf-8')) == {'status': 'new'}


def test_atomic_state_write_deadline_preserves_previous_complete_json(profile, monkeypatch):
    from qa.tools import environment
    target = profile.base / 'state.json'
    target.write_text('{"status":"old"}', encoding='utf-8')

    def blocked(*_args):
        error = PermissionError('sharing violation')
        error.winerror = 5
        raise error

    with monkeypatch.context() as patch:
        times = iter((0.0, 2.0))
        patch.setattr(environment.time, 'monotonic', lambda: next(times))
        patch.setattr(environment.time, 'sleep', lambda _delay: pytest.fail('Must not sleep after deadline'))
        patch.setattr(Path, 'replace', blocked)
        with pytest.raises(PermissionError):
            environment.write_json(profile, target, {'status': 'new'})
    assert json.loads(target.read_text(encoding='utf-8')) == {'status': 'old'}


def test_atomic_state_write_does_not_retry_other_permission_errors(profile, monkeypatch):
    from qa.tools import environment
    target = profile.base / 'state.json'
    target.write_text('{"status":"old"}', encoding='utf-8')
    calls = []

    def denied(*_args):
        calls.append(True)
        raise PermissionError('not a Windows sharing error')

    monkeypatch.setattr(Path, 'replace', denied)
    monkeypatch.setattr(environment.time, 'sleep', lambda _delay: pytest.fail('Unexpected retry'))
    with pytest.raises(PermissionError):
        environment.write_json(profile, target, {'status': 'new'})
    assert len(calls) == 1
    assert json.loads(target.read_text(encoding='utf-8')) == {'status': 'old'}
