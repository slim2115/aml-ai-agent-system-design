"""Real QA path selection and persistence with no model, socket or shared DB writes."""
import json
from pathlib import Path
import shutil
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from src.config import PROJECT_ROOT, get_demo_paths, get_settings
from src.lab.api import create_app
from src.qa_profile import QAProfile, checked_path
from src.runtime.policy import load_demo_policy
from src.runtime.service import AMLAgent
from src.workflow.auth import trusted_local_principal
from src.workflow.remote import RemoteWorkflowService, WorkflowClient
from src.workflow.sources import resolve_report_sources
from src.workflow.store import SQLiteStore, WorkflowError
from tests.lab.test_api import event_loop_socketpair_only, QueryRecorder
from tests.runtime.test_service import FakeIndex
from tests.workflow.test_remote_workflow import READY, Response, Transport


@pytest.fixture
def qa_run(monkeypatch):
    profile = QAProfile('qa-' + uuid4().hex[:12])
    profile.base.mkdir(parents=True)
    (profile.base / 'owner.json').write_text(json.dumps(profile.owner_record()), encoding='utf-8')
    profile.policy_path.parent.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / 'config/demo-policy.json', profile.policy_path)
    shutil.copytree(PROJECT_ROOT / 'data/demo-v1', profile.seed_dir)
    monkeypatch.setenv('AML_QA_RUN_ID', profile.run_id)
    monkeypatch.delenv('CHROMA_PERSIST_DIR', raising=False)
    monkeypatch.delenv('SYNTHETIC_DATA_DIR', raising=False)
    get_settings.cache_clear()
    try:
        yield profile
    finally:
        get_settings.cache_clear()
        for directory in (profile.base, profile.workflow_dir):
            checked_path(directory)
            if directory.exists():
                shutil.rmtree(directory)


def test_default_application_paths_remain_unchanged(monkeypatch):
    monkeypatch.delenv('AML_QA_RUN_ID', raising=False)
    monkeypatch.delenv('CHROMA_PERSIST_DIR', raising=False)
    monkeypatch.delenv('SYNTHETIC_DATA_DIR', raising=False)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert Path(settings.vector_store.persist_dir) == PROJECT_ROOT / 'var/chroma'
        assert Path(settings.data.synthetic_data_dir) == PROJECT_ROOT / 'data/synthetic'
        assert get_demo_paths() == (PROJECT_ROOT / 'config/demo-policy.json', PROJECT_ROOT / 'data/demo-v1')
    finally:
        get_settings.cache_clear()


def test_qa_settings_and_demo_sources_are_owned_but_models_can_be_reused(qa_run):
    settings = get_settings()
    assert get_demo_paths() == (qa_run.policy_path, qa_run.seed_dir)
    assert Path(settings.vector_store.persist_dir) == qa_run.chroma_dir
    assert Path(settings.data.synthetic_data_dir) == qa_run.seed_dir
    assert Path(settings.llm.embedding_model_path).is_relative_to(PROJECT_ROOT / 'var/models')
    assert settings.project_root == PROJECT_ROOT


@pytest.mark.parametrize('variable,path', [
    ('CHROMA_PERSIST_DIR', 'var/chroma'), ('SYNTHETIC_DATA_DIR', 'data/demo-v1'),
])
def test_qa_rejects_inherited_teaching_paths(qa_run, monkeypatch, variable, path):
    monkeypatch.setenv(variable, str(PROJECT_ROOT / path))
    with pytest.raises(ValueError, match='active QA run path'):
        get_settings()


def test_invalid_qa_owner_cannot_fall_back_to_teaching_client(qa_run):
    (qa_run.base / 'owner.json').write_text('{}', encoding='utf-8')
    transport = Transport(Response(READY))
    with pytest.raises(ValueError, match='QA_OWNER_MISMATCH'):
        WorkflowClient({'api_token': 'a' * 64}, transport=transport)
    assert transport.requests == []


def test_client_and_ui_adapter_use_qa_api_and_qa_history(qa_run):
    transport = Transport(Response(READY))
    client = WorkflowClient({'api_token': 'a' * 64}, transport=transport)
    adapter = RemoteWorkflowService(client=client)
    assert adapter.readiness() == READY
    assert transport.requests[0][0].full_url == qa_run.api_url + '/v1/readiness'
    assert adapter.store.path == qa_run.workflow_path
    policy = load_demo_policy(qa_run.policy_path)
    request = {'schema_version': '1.0', 'incident_id': 'INC-000123',
               'window': policy.window.model_dump(mode='json')}
    principal = trusted_local_principal()
    job = adapter.store.create_job(request, principal, 'qa-history')
    assert SQLiteStore(qa_run.workflow_path).get_job(job['job_id'], principal) == job
    with pytest.raises((ValueError, WorkflowError)):
        SQLiteStore(PROJECT_ROOT / 'var/workflow/api-workflow.sqlite3')


def test_qa_api_identity_and_controller_database(qa_run):
    # No workflow is injected: this exercises the actual lazy API/controller path.
    app = create_app({'api_token': 'a' * 64}, database=QueryRecorder(),
                     kafka_broker=object(), analysis_broker=object())
    with TestClient(app) as client:
        health = client.get('/health').json()
        assert health == {'status': 'ok', 'mode': 'local_learning', 'check': 'http_liveness_only',
                          'qa_run_id': qa_run.run_id, 'qa_owner': qa_run.marker}
        readiness = client.get('/v1/readiness', headers={'X-Lab-Key': 'a' * 64})
        assert readiness.status_code == 200 and readiness.json()['state'] == 'not_ready'
        assert qa_run.workflow_path.is_file()
        assert qa_run.run_id in client.get('/openapi.json').json()['info']['title']


def test_runtime_and_source_viewer_use_the_same_qa_snapshot(qa_run):
    # Only vector ranking is substituted. Context hashes, policy and report checks are real.
    from src.contracts.semantics import canonical_digest

    class SourceIndex(FakeIndex):
        def __init__(self, path):
            super().__init__(path)
            # Generic FakeIndex intentionally uses synthetic chunk IDs; this
            # source-viewer scenario needs the real character-range identity.
            for chunk in self.chunks:
                citation = chunk.rule.citation
                identity = {'rule_id': citation.rule_id, 'version': citation.rule_version,
                            'source_ref': citation.source_ref, 'char_start': 0,
                            'char_end': len(chunk.rule.rule_text), 'text_sha256': citation.content_sha256}
                chunk.rule = chunk.rule.model_copy(update={'citation': citation.model_copy(
                    update={'chunk_id': 'chunk-' + canonical_digest(identity)})})
                chunk.citation = chunk.rule.citation

    report = AMLAgent(mode='demo', index_factory=SourceIndex).run('INC-000123')
    assert report['status'] == 'draft'
    assert resolve_report_sources(report)
    (qa_run.seed_dir / 'clients.json').write_text('[]', encoding='utf-8')
    result = AMLAgent(mode='demo', index_factory=FakeIndex).run('INC-000123')
    assert result.get('code') == 'NOT_READY'
    with pytest.raises(WorkflowError, match='NOT_READY'):
        resolve_report_sources(report)
    assert json.loads((PROJECT_ROOT / 'data/demo-v1/clients.json').read_text(encoding='utf-8'))


def test_qa_ui_labels_run_and_keeps_offline_history_in_qa(qa_run, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('AML_WORKFLOW_BACKEND', 'api')
    st.cache_resource.clear()
    try:
        ui = AppTest.from_file(str(PROJECT_ROOT / 'src/ui/app.py'), default_timeout=15).run()
        assert not ui.exception
        assert any(qa_run.run_id in item.value for item in ui.info)
        assert any('QA API сейчас недоступен' in item.value for item in ui.warning)
        assert qa_run.workflow_path.is_file()
    finally:
        st.cache_resource.clear()
