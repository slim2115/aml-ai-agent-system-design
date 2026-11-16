"""Offline safety controls; no Docker service or model is required."""
from copy import deepcopy
import json
from types import SimpleNamespace
import subprocess

import pytest

from qa.dark_isolation import probe

CID = 'a' * 64


def container(name, nonce):
    return {'Id': CID, 'Name': '/' + name, 'Image': probe.IMAGE,
            'Config': {'Labels': {probe.LABEL: nonce}, 'User': '65534:65534',
                       'Entrypoint': ['/bin/sh'], 'Cmd': ['-c', probe.SCRIPT]},
            'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'Privileged': False,
                           'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges'],
                           'Binds': None, 'Mounts': None, 'Devices': None, 'PortBindings': None,
                           'PidsLimit': 64, 'Memory': 128 * 1024 * 1024, 'NanoCpus': 500000000,
                           'Tmpfs': {key: 'rw,nosuid,nodev,noexec,size=1m' for key in probe.TMPFS}},
            'Mounts': []}


class Docker:
    def __init__(self, mutate=None, unavailable=False, output=None):
        self.mutate, self.unavailable, self.output = mutate, unavailable, output
        self.calls, self.doc = [], None

    def __call__(self, executable, *args, input_text=None, timeout=30):
        self.calls.append((args, input_text))
        if args[:2] == ('image', 'inspect'):
            return SimpleNamespace(returncode=1 if self.unavailable else 0, stdout=probe.IMAGE + '\n')
        if args[0] == 'create':
            name = args[args.index('--name') + 1]
            nonce = args[args.index('--label') + 1].split('=', 1)[1]
            self.doc = container(name, nonce)
            if self.mutate:
                self.mutate(self.doc)
            return SimpleNamespace(returncode=0, stdout=CID + '\n')
        if args[0] == 'inspect':
            return SimpleNamespace(returncode=0, stdout=json.dumps([self.doc]))
        if args[0] == 'start':
            assert input_text == probe.INPUT
            return SimpleNamespace(returncode=0, stdout=self.output or
                'value=14\nprivate=absent\nsocket=absent\nroot_write=denied\n')
        if args[0] == 'rm':
            assert args == ('rm', '--force', CID)
            return SimpleNamespace(returncode=0, stdout=CID)
        raise AssertionError('unexpected Docker command')


def test_valid_probe_uses_only_expected_owned_container():
    docker = Docker()
    result = probe.run('fake', docker)
    assert result['status'] == result['container_configuration'] == result['cleanup'] == 'PASS'
    assert result['hidden_holdout'] == 'NOT_ESTABLISHED'
    assert result['aml_product'] == 'NOT_TESTED'
    args = next(args for args, _ in docker.calls if args[0] == 'create')
    assert '--pull=never' in args and '--network=none' in args and '--read-only' in args
    assert probe.IMAGE in args and '--volume' not in args and '--mount' not in args


@pytest.mark.parametrize('change', [
    lambda d: d['HostConfig'].update(NetworkMode='bridge'),
    lambda d: d['HostConfig'].update(ReadonlyRootfs=False),
    lambda d: d['HostConfig'].update(Privileged=True),
    lambda d: d['HostConfig'].update(Binds=['/host:/host']),
    lambda d: d['HostConfig'].update(CapDrop=[]),
    lambda d: d['HostConfig'].update(SecurityOpt=[]),
    lambda d: d['HostConfig']['Tmpfs'].pop('/etc/kafka/secrets'),
    lambda d: d['HostConfig']['Tmpfs'].update({'/tmp': 'rw'}),
    lambda d: d['HostConfig'].update(Memory=0),
    lambda d: d['Mounts'].append({'Type': 'volume'}),
    lambda d: d['Config'].update(User='root'),
    lambda d: d['Config'].update(Cmd=['-c', 'other']),
])
def test_unsafe_configuration_never_starts_candidate(change):
    docker = Docker(mutate=change)
    result = probe.run('fake', docker)
    assert result['container_configuration'] == 'FAIL'
    assert result['status'] == 'FAIL'
    assert not any(args[0] == 'start' for args, _ in docker.calls)
    assert result['cleanup'] == 'PASS'


@pytest.mark.parametrize('text', ['value=14\nprivate=visible\nsocket=absent\nroot_write=denied\n',
                                  'value=14\nprivate=absent\nsocket=visible\nroot_write=denied\n',
                                  'value=14\nprivate=absent\nsocket=absent\nroot_write=allowed\n',
                                  'value=13\nprivate=absent\nsocket=absent\nroot_write=denied\n',
                                  'value=14\nvalue=14\nprivate=absent\nsocket=absent\n'])
def test_evaluator_rejects_observed_boundary_or_calculation_defects(text):
    assert probe.assess_stdout(text) == 'FAIL'
    assert probe.run('fake', Docker(output=text))['status'] == 'FAIL'


def test_missing_docker_image_does_not_create_container():
    docker = Docker(unavailable=True)
    result = probe.run('fake', docker)
    assert result['status'] == 'INCOMPLETE'
    assert result['reason'] == 'LOCAL_IMAGE_NOT_AVAILABLE'
    assert not any(args[0] == 'create' for args, _ in docker.calls)


def test_cleanup_refuses_resource_if_ownership_cannot_be_verified():
    class Replaced(Docker):
        def __call__(self, executable, *args, **kwargs):
            result = super().__call__(executable, *args, **kwargs)
            if args[0] == 'start':
                self.doc['Config']['Labels'][probe.LABEL] = 'different-owner'
            return result
    docker = Replaced()
    result = probe.run('fake', docker)
    assert result['status'] == result['cleanup'] == 'INCOMPLETE'
    assert not any(args[0] == 'rm' for args, _ in docker.calls)


def test_create_timeout_never_deletes_unconfirmed_resource():
    class TimedOut(Docker):
        def __call__(self, executable, *args, **kwargs):
            if args[0] == 'create':
                raise subprocess.TimeoutExpired(args, 30)
            return super().__call__(executable, *args, **kwargs)
    docker = TimedOut()
    result = probe.run('fake', docker)
    assert result['status'] == 'INCOMPLETE'
    assert not any(args[0] == 'rm' for args, _ in docker.calls)


def test_command_preserves_newline_as_bytes(monkeypatch):
    def run(argv, *, input, capture_output, timeout, check):
        assert input == b'7\n'
        return SimpleNamespace(args=argv, returncode=0, stdout=b'fine\n', stderr=b'')
    monkeypatch.setattr(probe.subprocess, 'run', run)
    result = probe.command('fake', 'start', input_text='7\n')
    assert result.stdout == 'fine\n'


@pytest.mark.parametrize('change', [
    lambda d: d['Config'].update(Labels=None),
    lambda d: d['Mounts'].append(None),
    lambda d: d['Mounts'].append({'Type': 'tmpfs', 'Destination': '/unexpected'}),
    lambda d: d['HostConfig'].update(CapAdd=['SYS_ADMIN']),
    lambda d: d['HostConfig'].update(PidMode='host'),
])
def test_malformed_or_extra_privileged_config_fails_closed(change):
    doc = container('test', 'nonce')
    change(doc)
    assert probe.configuration(doc, 'test', 'nonce') is False


def test_create_timeout_retains_ownership_hint_without_deleting_unknown_resource():
    class Timeout(Docker):
        def __call__(self, executable, *args, **kwargs):
            if args[0] == 'create':
                self.created_name = args[args.index('--name') + 1]
                raise subprocess.TimeoutExpired(args, 30)
            return super().__call__(executable, *args, **kwargs)
    docker = Timeout()
    result = probe.run('fake', docker)
    assert result['status'] == result['cleanup'] == 'INCOMPLETE'
    assert result['resource']['name'] == docker.created_name
    assert result['resource']['id'] is None
    assert result['resource']['label_key'] == probe.LABEL


def test_pass_retains_evidence_for_local_recheck():
    result = probe.run('fake', Docker())
    assert probe.configuration(result['configuration_evidence'], result['resource']['name'],
                               result['resource']['label_value'])
    assert probe.assess_stdout(result['candidate_stdout']) == 'PASS'
    assert result['source_sha256'] == result['source_sha256_after']
