"""QA Kafka names and lifecycle boundaries; Docker/Kafka never contacted."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qa.tools import broker
from src.qa_profile import QAProfile


@pytest.fixture
def profile(tmp_path, monkeypatch):
    profile = QAProfile('qa-012345abcdef', root=tmp_path)
    profile.base.mkdir(parents=True)
    (profile.base / 'owner.json').write_text(json.dumps(profile.owner_record()), encoding='utf-8')
    monkeypatch.setattr('src.qa_profile.get_qa_profile', lambda: profile.validate())
    return profile


def docker_fixture(profile):
    container, volume, network = broker._names(profile)
    labels = {broker.OWNER_LABEL: profile.marker, broker.PROJECT_LABEL: profile.compose_project,
              broker.FILE_LABEL: str(broker._config_path(profile))}
    resource = {'Name': '/' + container, 'Config': {'Labels': labels},
                'State': {'Running': True},
                'HostConfig': {'PortBindings': {'9092/tcp': [
                    {'HostIp': '127.0.0.1', 'HostPort': str(profile.kafka_port)}]}}}
    return resource


@pytest.mark.parametrize('active', [False, True])
def test_contract_coordinates_isolate_both_flows(profile, monkeypatch, active):
    monkeypatch.setattr('src.qa_profile.get_qa_profile', lambda: profile if active else None)
    root = Path(__file__).resolve().parents[2]
    loaded = []
    # Load fresh module objects under the existing package to preserve relative
    # imports, without replacing modules already referenced by other tests.
    for filename in ('contracts.py', 'analysis_contracts.py'):
        spec = importlib.util.spec_from_file_location('src.lab.kafka._qa_probe',
                                                       root / 'src/lab/kafka' / filename)
        module = importlib.util.module_from_spec(spec)
        # dataclasses resolves annotations via sys.modules during construction.
        monkeypatch.setitem(__import__('sys').modules, spec.name, module)
        spec.loader.exec_module(module)
        loaded.append(module)
    review, analysis = loaded
    assert review.TOPIC == (profile.topic if active else 'aml.lab.incident-review.v1')
    assert review.DLQ_TOPIC == (profile.dlq_topic if active else 'aml.lab.incident-review.dlq.v1')
    assert review.GROUP == (profile.group if active else 'aml-lab-review-v1')
    assert review.BOOTSTRAP == ('127.0.0.1:29092' if active else '127.0.0.1:9092')
    assert analysis.TOPIC == (profile.analysis_topic if active else 'aml.lab.analysis-requested.v1')
    assert analysis.DLQ_TOPIC == (profile.analysis_dlq if active else 'aml.lab.analysis-requested.dlq.v1')
    assert analysis.GROUP == (profile.analysis_group if active else 'aml-lab-analysis-v1')


def test_compose_is_separate_loopback_and_reuses_installed_image(profile):
    config = broker._compose_config(profile)
    service = config['services']['kafka']
    assert service['ports'] == ['127.0.0.1:29092:9092']
    assert service['image'] == 'apache/kafka:4.3.1'
    assert service['pull_policy'] == 'never'
    assert config['name'] == profile.compose_project != 'aml-kafka-lab'
    assert service['labels'] == {broker.OWNER_LABEL: profile.marker}
    assert config['volumes']['kafka-data']['name'] == profile.compose_project + '-kafka-data'
    assert config['networks']['default']['labels'][broker.OWNER_LABEL] == profile.marker
    assert 'aml-kafka-lab' not in json.dumps(config)


def test_profile_required_before_any_docker_access(profile, monkeypatch):
    monkeypatch.setattr('src.qa_profile.get_qa_profile', lambda: None)
    docker = Mock()
    monkeypatch.setattr(broker, '_docker', docker)
    with pytest.raises(broker.QABrokerError, match='QA_PROFILE_REQUIRED'):
        broker.start(profile)
    docker.assert_not_called()


def test_marker_revalidated_before_any_docker_access(profile, monkeypatch):
    (profile.base / 'owner.json').write_text('{}', encoding='utf-8')
    docker = Mock()
    monkeypatch.setattr(broker, '_docker', docker)
    with pytest.raises(ValueError, match='QA_OWNER_MISMATCH'):
        broker.stop(profile)
    docker.assert_not_called()


@pytest.mark.parametrize('endpoint', ['tcp://127.0.0.1:2375', 'ssh://other',
                                      'npipe:////other/pipe/docker_engine'])
def test_remote_context_rejected_before_engine_command(monkeypatch, endpoint):
    docker = Mock(return_value=json.dumps([{'Endpoints': {'docker': {'Host': endpoint}}}]))
    monkeypatch.setattr(broker, '_docker', docker)
    with pytest.raises(broker.QABrokerError, match='QA_DOCKER_LOCAL_ENGINE_REQUIRED'):
        broker._local_engine()
    assert docker.call_count == 1


def test_foreign_container_stops_start_before_mutation(profile, monkeypatch):
    container = docker_fixture(profile)
    container['Config']['Labels'][broker.OWNER_LABEL] = 'normal-lab'
    calls = []
    def docker(args, **kwargs):
        calls.append(args)
        if args[:2] == ['ps', '-aq']:
            return 'a123'
        if args[0] == 'inspect':
            return json.dumps([container])
        raise AssertionError(args)
    monkeypatch.setattr(broker, '_local_engine', lambda: None)
    monkeypatch.setattr(broker, '_docker', docker)
    with pytest.raises(broker.QABrokerError, match='QA_KAFKA_CONTAINER_OWNERSHIP'):
        broker.start(profile)
    assert not any('up' in args for args in calls)
    assert not broker._config_path(profile).exists()


@pytest.mark.parametrize('kind', ['volume', 'network'])
def test_foreign_persistent_resource_blocks_changes(profile, monkeypatch, kind):
    _, volume, network = broker._names(profile)
    name = volume if kind == 'volume' else network
    def docker(args, **kwargs):
        if args[:2] == ['ps', '-aq']:
            return ''
        if args[:2] == [kind, 'ls']:
            return name
        if args[:2] == [kind, 'inspect']:
            return json.dumps([{'Name': name, 'Labels': {}}])
        if args[:2] in (['volume', 'ls'], ['network', 'ls']):
            return ''
        raise AssertionError(args)
    monkeypatch.setattr(broker, '_docker', docker)
    with pytest.raises(broker.QABrokerError, match='QA_KAFKA_RESOURCE_OWNERSHIP'):
        broker._ownership(profile)


def test_owned_container_with_wrong_port_cannot_authorize_topic_writes(profile, monkeypatch):
    container = docker_fixture(profile)
    container['HostConfig']['PortBindings']['9092/tcp'][0]['HostPort'] = '9092'
    monkeypatch.setattr(broker, '_docker', lambda args, **kwargs:
                        'a123' if args[0] == 'ps' else json.dumps([container]))
    with pytest.raises(broker.QABrokerError, match='QA_KAFKA_PORT_OWNERSHIP'):
        broker._ownership(profile)


def test_config_mismatch_is_preserved(profile, monkeypatch):
    path = broker._config_path(profile)
    path.write_text('{"foreign": true}', encoding='utf-8')
    docker = Mock()
    monkeypatch.setattr(broker, '_docker', docker)
    with pytest.raises(broker.QABrokerError, match='QA_KAFKA_CONFIG_MISMATCH'):
        broker.start(profile)
    assert json.loads(path.read_text(encoding='utf-8')) == {'foreign': True}
    docker.assert_not_called()


def test_status_does_not_generate_config_or_start_broker(profile, monkeypatch):
    monkeypatch.setattr(broker, '_local_engine', lambda: None)
    docker = Mock(return_value='')
    monkeypatch.setattr(broker, '_docker', docker)
    assert broker.status(profile)['status'] == 'absent'
    assert not broker._config_path(profile).exists()
    assert not any('compose' in call.args[0] for call in docker.call_args_list)


def test_stop_only_owned_service_and_retains_volume(profile, monkeypatch):
    config = broker._compose_config(profile)
    broker._config_path(profile).write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(broker, '_inspect', lambda current: (config, [docker_fixture(profile)]))
    compose = Mock()
    monkeypatch.setattr(broker, '_compose', compose)
    result = broker.stop(profile)
    assert result['data_retained'] and result['status'] == 'stopped'
    compose.assert_called_once_with(profile, ['stop', '--timeout', '30', 'kafka'])


def test_start_never_pulls_and_creates_only_four_qa_topics(profile, monkeypatch):
    config = broker._compose_config(profile)
    monkeypatch.setattr(broker, '_inspect', lambda current: (config, [docker_fixture(profile)]))
    monkeypatch.setattr(broker, '_ownership', lambda current: [docker_fixture(profile)])
    docker, compose = Mock(), Mock()
    monkeypatch.setattr(broker, '_docker', docker)
    monkeypatch.setattr(broker, '_compose', compose)
    topics = Mock(return_value={name: 3 for name in broker._topic_names(profile)})
    monkeypatch.setattr(broker, '_topics', topics)
    result = broker.start(profile)
    assert result['status'] == 'ready'
    docker.assert_called_once_with(['image', 'inspect', 'apache/kafka:4.3.1'])
    arguments = compose.call_args.args[1]
    assert arguments[arguments.index('--pull') + 1] == 'never'
    assert '--no-build' in arguments
    topics.assert_called_once_with(profile, create=True)
    assert len(set(broker._topic_names(profile))) == 4
    assert all(name.startswith(profile.topic_prefix + '.') for name in broker._topic_names(profile))


def test_docker_subprocess_is_hidden_bounded_and_context_fixed(monkeypatch, tmp_path):
    fake = tmp_path / 'docker.exe'
    fake.write_bytes(b'')
    monkeypatch.setattr(broker, '_docker_path', lambda: str(fake))
    monkeypatch.setenv('DOCKER_HOST', 'tcp://untrusted:2375')
    monkeypatch.setenv('DOCKER_CONTEXT', 'other')
    process = Mock(return_value=SimpleNamespace(returncode=0, stdout='ok'))
    monkeypatch.setattr(broker.subprocess, 'run', process)
    assert broker._docker(['info'], timeout=12) == 'ok'
    args, kwargs = process.call_args
    assert args[0] == [str(fake), '--context', 'desktop-linux', 'info']
    assert kwargs['timeout'] == 12 and kwargs['capture_output']
    assert 'DOCKER_HOST' not in kwargs['env'] and 'DOCKER_CONTEXT' not in kwargs['env']
    assert kwargs['creationflags'] == getattr(broker.subprocess, 'CREATE_NO_WINDOW', 0)
