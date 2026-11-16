"""Owned lifecycle failure paths with temporary files and process/service doubles."""
import json

import pytest

from qa.tools import environment, stand
from src.qa_profile import QAProfile, checked_path


@pytest.fixture
def qa_run(tmp_path, monkeypatch):
    profile = QAProfile('qa-123456abcdef', root=tmp_path)
    profile.base.mkdir(parents=True)
    (profile.base / 'owner.json').write_text(json.dumps(profile.owner_record()), encoding='utf-8')
    (profile.base / 'lab').mkdir()
    (profile.base / 'logs').mkdir()
    # stand's default checked_path root is captured at import. Bind both the
    # module root and its path guard explicitly to tmp_path for every test.
    monkeypatch.setattr(stand, 'ROOT', tmp_path)
    monkeypatch.setattr(stand, 'checked_path', lambda path: checked_path(path, tmp_path))
    monkeypatch.setattr(stand.subprocess, 'Popen', lambda *_args, **_kwargs: pytest.fail('process launched'))
    monkeypatch.setattr(stand.subprocess, 'run', lambda *_args, **_kwargs: pytest.fail('command executed'))
    monkeypatch.setattr(environment.os, 'kill', lambda *_args, **_kwargs: pytest.fail('process signalled'))
    monkeypatch.setattr(stand, 'local_http', lambda *_args, **_kwargs: pytest.fail('HTTP requested'))
    assert profile.validate() == profile
    return profile


def process_record(profile, service, *, status='running', creation=111):
    return {'root': str(profile.root), 'qa_run_id': profile.run_id, 'qa_owner': profile.marker,
            'instance_id': 'a' * 32, 'pid': 43210, 'process_creation': creation,
            'service': service, 'status': status}


@pytest.mark.parametrize('service', ['worker', 'review-worker'])
@pytest.mark.parametrize('observed_creation', [None, 222])
def test_stale_worker_state_never_counts_as_ready(qa_run, monkeypatch, service, observed_creation):
    record = process_record(qa_run, service)
    environment.write_json(qa_run, qa_run.base / 'lab' / (service + '-process.json'), record)
    probes = []

    def observed(pid):
        probes.append(pid)
        return observed_creation

    monkeypatch.setattr(environment, 'process_creation', observed)
    assert stand.service_ready(qa_run, service) is False
    assert probes == [record['pid']]
    assert environment.read_state(qa_run, service) == record


def test_reused_live_pid_is_not_stopped_and_record_becomes_stale(qa_run, monkeypatch):
    record = process_record(qa_run, 'worker')
    path = qa_run.base / 'lab/worker-process.json'
    environment.write_json(qa_run, path, record)
    # The numeric PID is live, but its creation identity belongs to another
    # process. Neither a stop request nor a process signal may target it.
    monkeypatch.setattr(environment, 'process_creation', lambda _pid: 222)
    stand.stop_service(qa_run, 'worker')
    assert environment.read_state(qa_run, 'worker') == dict(record, status='stale')
    assert not (qa_run.base / 'lab/worker-stop.json').exists()
    assert not (qa_run.base / 'lab/worker-launch.json').exists()


def test_busy_api_port_refuses_start_before_spawning_any_process(qa_run, monkeypatch):
    ports = []

    def busy(port):
        ports.append(port)
        return False

    monkeypatch.setattr(stand, 'port_free', busy)
    monkeypatch.setattr(environment, 'process_creation', lambda _pid: pytest.fail('unexpected process lookup'))
    with pytest.raises(RuntimeError, match='QA_SERVICE_PORT_BUSY:api'):
        stand.start_service(qa_run, 'api')
    assert ports == [qa_run.api_port]
    assert not list((qa_run.base / 'lab').iterdir())
    assert not list((qa_run.base / 'logs').iterdir())


def test_another_active_run_blocks_start_before_postgres(qa_run, monkeypatch):
    from qa.tools import broker
    from src.workflow import lease

    foreign = QAProfile('qa-000000000000', root=qa_run.root)
    active = qa_run.root / 'var/qa/active.json'
    active.write_text(json.dumps(foreign.owner_record()), encoding='utf-8')
    before = active.read_bytes()
    leases = []

    class FakeLease:
        def __init__(self, path):
            assert path == qa_run.root / 'var/workflow/qa/environment.sqlite3'
            self.closed = False
            leases.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(lease, 'ControllerLease', FakeLease)
    monkeypatch.setattr(stand, 'start_postgres', lambda _profile: pytest.fail('PostgreSQL started'))
    monkeypatch.setattr(stand, 'command', lambda *_args, **_kwargs: pytest.fail('bootstrap executed'))
    monkeypatch.setattr(broker, 'start', lambda _profile: pytest.fail('broker started'))
    monkeypatch.setattr(stand, 'start_service', lambda *_args: pytest.fail('service started'))
    with pytest.raises(RuntimeError, match='ANOTHER_QA_RUN_MUST_BE_STOPPED_FIRST'):
        stand.start(qa_run)
    assert len(leases) == 1 and leases[0].closed
    assert active.read_bytes() == before
    assert not (qa_run.root / 'var/workflow').exists()
    assert not (qa_run.base / 'postgres').exists()
    assert not (qa_run.base / 'last-start.json').exists()

