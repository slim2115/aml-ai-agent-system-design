"""Explicit, owned QA Kafka lifecycle; status is read-only and the CLI default.

No software or image is downloaded. Stop retains the container and named volume.
Docker calls are limited to the local Desktop Linux named pipe. The normal lab
Compose project, port, topics, volume and consumer offsets are never used.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OWNER_LABEL = 'aml.qa.owner'
PROJECT_LABEL = 'com.docker.compose.project'
FILE_LABEL = 'com.docker.compose.project.config_files'
CONTEXT = 'desktop-linux'
TOPIC_CONFIG = {'retention.ms': '604800000', 'retention.bytes': '134217728',
                'segment.bytes': '16777216', 'cleanup.policy': 'delete'}


class QABrokerError(RuntimeError):
    """Stable error codes never include environment variables or CLI stderr."""


def _profile(profile):
    from src.qa_profile import get_qa_profile
    active = get_qa_profile()
    if active is None or active != profile:
        raise QABrokerError('QA_PROFILE_REQUIRED')
    return active


def _docker_path():
    candidates = [shutil.which('docker'),
                  str(Path(os.environ.get('LOCALAPPDATA', '')) /
                      'Programs/DockerDesktop/resources/bin/docker.exe'),
                  r'C:\Program Files\Docker\Docker\resources\bin\docker.exe']
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise QABrokerError('QA_DOCKER_CLI_NOT_FOUND')


def _docker(arguments, *, timeout=20):
    executable = _docker_path()
    environment = os.environ.copy()
    # An explicit validated context has precedence; remove external overrides too.
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        environment.pop(key, None)
    environment['PATH'] = str(Path(executable).parent) + os.pathsep + environment.get('PATH', '')
    try:
        result = subprocess.run([executable, '--context', CONTEXT, *arguments],
                                cwd=ROOT, env=environment, capture_output=True,
                                text=True, encoding='utf-8', errors='replace',
                                timeout=timeout, check=False,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise QABrokerError('QA_DOCKER_TIMEOUT_CHECK_STATUS') from None
    except OSError:
        raise QABrokerError('QA_DOCKER_EXEC_FAILED') from None
    if result.returncode:
        raise QABrokerError('QA_DOCKER_COMMAND_FAILED')
    return result.stdout.strip()


def _json(arguments):
    try:
        return json.loads(_docker(arguments))
    except (ValueError, TypeError):
        raise QABrokerError('QA_DOCKER_INVALID_RESPONSE') from None


def _local_engine():
    contexts = _json(['context', 'inspect', CONTEXT])
    try:
        endpoint = contexts[0]['Endpoints']['docker']['Host'].replace('\\', '/')
    except (KeyError, IndexError, TypeError, AttributeError):
        raise QABrokerError('QA_DOCKER_LOCAL_ENGINE_REQUIRED') from None
    if not re.fullmatch(r'npipe:/+\./pipe/(docker_engine|dockerDesktopLinuxEngine)', endpoint):
        raise QABrokerError('QA_DOCKER_LOCAL_ENGINE_REQUIRED')
    if _docker(['info', '--format', '{{.OSType}}']) != 'linux':
        raise QABrokerError('QA_DOCKER_LINUX_REQUIRED')


def _names(profile):
    return (profile.compose_project + '-kafka', profile.compose_project + '-kafka-data',
            profile.compose_project + '-network')


def _config_path(profile):
    return profile.base / 'kafka-compose.json'


def _compose_config(profile):
    import yaml
    source = yaml.safe_load((ROOT / 'lab/kafka/compose.yaml').read_text(encoding='utf-8'))
    service = copy.deepcopy(source['services']['kafka'])
    if not re.fullmatch(r'apache/kafka:[0-9]+\.[0-9]+\.[0-9]+', service['image']):
        raise QABrokerError('QA_KAFKA_IMAGE_PIN_REQUIRED')
    container, volume, network = _names(profile)
    labels = {OWNER_LABEL: profile.marker}
    service['container_name'] = container
    service['labels'] = labels
    service['ports'] = [f'127.0.0.1:{profile.kafka_port}:9092']
    service['environment']['KAFKA_ADVERTISED_LISTENERS'] = (
        f'HOST://127.0.0.1:{profile.kafka_port},INTERNAL://kafka:19092')
    service['pull_policy'] = 'never'
    service['volumes'] = ['kafka-data:/var/lib/kafka/data']
    return {'name': profile.compose_project, 'services': {'kafka': service},
            'volumes': {'kafka-data': {'name': volume, 'labels': labels}},
            'networks': {'default': {'name': network, 'labels': labels}}}


def _check_config(profile, expected):
    from src.qa_profile import checked_path
    path = _config_path(profile)
    try:
        checked_path(path, profile.root)
    except ValueError:
        raise QABrokerError('QA_KAFKA_CONFIG_PATH_INVALID')
    if path.exists():
        try:
            matches = json.loads(path.read_text(encoding='utf-8')) == expected
        except (ValueError, OSError):
            matches = False
        if not matches:
            raise QABrokerError('QA_KAFKA_CONFIG_MISMATCH')


def _ownership(profile):
    container_name, volume_name, network_name = _names(profile)
    ids = set(_docker(['ps', '-aq', '--filter', f'label={PROJECT_LABEL}={profile.compose_project}']).split())
    ids.update(_docker(['ps', '-aq', '--filter', f'name=^/{container_name}$']).split())
    containers = _json(['inspect', *sorted(ids)]) if ids else []
    if not isinstance(containers, list) or len(containers) > 1:
        raise QABrokerError('QA_KAFKA_CONTAINER_OWNERSHIP')
    for container in containers:
        labels = container.get('Config', {}).get('Labels') or {}
        config_path = labels.get(FILE_LABEL, '')
        if (labels.get(OWNER_LABEL) != profile.marker
                or labels.get(PROJECT_LABEL) != profile.compose_project
                or Path(config_path).resolve() != _config_path(profile).resolve()
                or container.get('Name') != '/' + container_name):
            raise QABrokerError('QA_KAFKA_CONTAINER_OWNERSHIP')
        bindings = container.get('HostConfig', {}).get('PortBindings', {})
        if bindings.get('9092/tcp') != [{'HostIp': '127.0.0.1', 'HostPort': str(profile.kafka_port)}]:
            raise QABrokerError('QA_KAFKA_PORT_OWNERSHIP')
    for kind, name in (('volume', volume_name), ('network', network_name)):
        objects = _docker([kind, 'ls', '--quiet', '--filter', f'name=^{name}$']).split()
        if objects:
            details = _json([kind, 'inspect', name])
            if (not isinstance(details, list) or len(details) != 1
                    or details[0].get('Name') != name
                    or (details[0].get('Labels') or {}).get(OWNER_LABEL) != profile.marker
                    or (details[0].get('Labels') or {}).get(PROJECT_LABEL) != profile.compose_project):
                raise QABrokerError('QA_KAFKA_RESOURCE_OWNERSHIP')
    return containers


def _compose(profile, arguments, *, timeout=45):
    return _docker(['compose', '--project-name', profile.compose_project,
                    '--project-directory', str(profile.base), '--file', str(_config_path(profile)),
                    *arguments], timeout=timeout)


def _topic_names(profile):
    return (profile.topic, profile.dlq_topic, profile.analysis_topic, profile.analysis_dlq)


def _topics(profile, *, create=False):
    from confluent_kafka.admin import AdminClient, NewTopic
    from confluent_kafka import KafkaError, KafkaException
    admin = AdminClient({'bootstrap.servers': f'127.0.0.1:{profile.kafka_port}',
                         'socket.timeout.ms': 3000, 'request.timeout.ms': 3000})
    names = _topic_names(profile)
    try:
        if create:
            futures = admin.create_topics(
                [NewTopic(name, num_partitions=3, replication_factor=1, config=TOPIC_CONFIG)
                 for name in names], operation_timeout=5, request_timeout=8)
            for future in futures.values():
                try:
                    future.result(timeout=10)
                except KafkaException as exc:
                    if not exc.args or exc.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
                        raise
        metadata = admin.list_topics(timeout=5)
        result = {}
        for name in names:
            topic = metadata.topics.get(name)
            if (topic is None or topic.error is not None or len(topic.partitions) != 3
                    or any(part.error is not None or part.leader < 0 or len(part.replicas) != 1
                           for part in topic.partitions.values())):
                raise QABrokerError('QA_KAFKA_TOPICS_NOT_READY')
            result[name] = len(topic.partitions)
        return result
    except QABrokerError:
        raise
    except Exception:
        raise QABrokerError('QA_KAFKA_TOPICS_NOT_READY') from None


def _inspect(profile):
    _profile(profile)
    expected = _compose_config(profile)
    _check_config(profile, expected)
    _local_engine()
    return expected, _ownership(profile)


def status(profile):
    """Inspect only; missing services are represented, never implicitly started."""
    _, containers = _inspect(profile)
    running = bool(containers and containers[0].get('State', {}).get('Running'))
    result = {'status': 'running' if running else ('stopped' if containers else 'absent'),
              'run_id': profile.run_id, 'project': profile.compose_project,
              'bootstrap': f'127.0.0.1:{profile.kafka_port}', 'data_retained': True}
    if running:
        try:
            result['topics'] = _topics(profile)
            result['status'] = 'ready'
        except QABrokerError as exc:
            result['topics_status'] = str(exc)
    return result


def start(profile):
    expected, containers = _inspect(profile)
    # Check the already installed image before writing or starting anything.
    _docker(['image', 'inspect', expected['services']['kafka']['image']])
    if not (containers and containers[0].get('State', {}).get('Running')):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if os.name == 'nt':
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(('127.0.0.1', profile.kafka_port))
        except OSError:
            raise QABrokerError('QA_KAFKA_PORT_IN_USE') from None
    _profile(profile)
    path = _config_path(profile)
    if not path.exists():
        with path.open('x', encoding='utf-8') as stream:
            json.dump(expected, stream, ensure_ascii=True, indent=2)
            stream.write('\n')
    _compose(profile, ['up', '-d', '--pull', 'never', '--no-build',
                       '--wait', '--wait-timeout', '120', 'kafka'], timeout=145)
    _profile(profile)
    _ownership(profile)
    topics = _topics(profile, create=True)
    return {'status': 'ready', 'run_id': profile.run_id, 'project': profile.compose_project,
            'bootstrap': f'127.0.0.1:{profile.kafka_port}', 'topics': topics,
            'data_retained': True}


def stop(profile):
    _, containers = _inspect(profile)
    if containers:
        if not _config_path(profile).is_file():
            raise QABrokerError('QA_KAFKA_CONFIG_MISSING')
        _profile(profile)
        _compose(profile, ['stop', '--timeout', '30', 'kafka'])
    return {'status': 'stopped' if containers else 'absent', 'run_id': profile.run_id,
            'project': profile.compose_project, 'data_retained': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('action', nargs='?', default='status', choices=('status', 'start', 'stop'))
    args = parser.parse_args()
    os.environ['AML_QA_RUN_ID'] = args.run_id
    try:
        from src.qa_profile import get_qa_profile
        result = {'status': status, 'start': start, 'stop': stop}[args.action](get_qa_profile())
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except QABrokerError as exc:
        print(json.dumps({'status': 'failed', 'code': str(exc)}))
        return 1
    except Exception:
        print(json.dumps({'status': 'failed', 'code': 'QA_KAFKA_COMMAND_FAILED'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
