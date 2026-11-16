"""Test one synthetic candidate in a locally isolated Docker container.

No AML model or hidden dataset is involved. The Docker host and its owner can
access all artifacts; this is not an independent evaluator or secret holdout.
"""
from __future__ import annotations

from copy import deepcopy
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.qa_profile import checked_path

IMAGE = 'sha256:77e3df9054047a88b520d0cc46e16696d3b22022e1d580aeccd2632df6532837'
LABEL = 'aml.qa.isolation.attempt'
TMPFS = ('/tmp', '/etc/kafka/secrets', '/mnt/shared/config', '/var/lib/kafka/data')
INPUT = '7\n'  # Public synthetic input; no answer or evaluator files are sent.
EXPECTED = {'value': '14', 'private': 'absent', 'socket': 'absent', 'root_write': 'denied'}
SCRIPT = r'''IFS= read -r n || exit 2
case "$n" in ''|*[!0-9]*) exit 2;; esac
printf 'value=%s\n' "$((n * 2))"
if [ -e /qa-private/answer ]; then echo private=visible; else echo private=absent; fi
if [ -e /var/run/docker.sock ]; then echo socket=visible; else echo socket=absent; fi
if (printf x > /qa-root-write-test) 2>/dev/null; then echo root_write=allowed; else echo root_write=denied; fi'''


def configuration(doc, name, nonce):
    """Check observed Docker configuration; reject any host bind or extra mount."""
    if type(doc) is not dict:
        return False
    cfg, host = doc.get('Config'), doc.get('HostConfig')
    if type(cfg) is not dict or type(host) is not dict:
        return False
    mounts = doc.get('Mounts')
    tmpfs = host.get('Tmpfs')
    labels = cfg.get('Labels')
    if type(labels) is not dict or type(mounts) is not list:
        return False
    if any(type(m) is not dict or m.get('Type') != 'tmpfs'
           or m.get('Destination') not in TMPFS for m in mounts):
        return False
    if len({m['Destination'] for m in mounts}) != len(mounts):
        return False
    return (doc.get('Name') == '/' + name and doc.get('Image') == IMAGE
            and labels.get(LABEL) == nonce
            and cfg.get('User') == '65534:65534' and cfg.get('Entrypoint') == ['/bin/sh']
            and cfg.get('Cmd') == ['-c', SCRIPT] and host.get('NetworkMode') == 'none'
            and host.get('ReadonlyRootfs') is True and host.get('Privileged') is False
            and host.get('CapDrop') == ['ALL']
            and not host.get('CapAdd') and host.get('SecurityOpt') == ['no-new-privileges']
            and host.get('PidMode') in (None, '') and host.get('IpcMode') in (None, 'private')
            and host.get('UTSMode') in (None, '') and host.get('UsernsMode') in (None, '')
            and not host.get('VolumesFrom') and not host.get('DeviceRequests')
            and not host.get('Binds') and not host.get('Mounts') and not host.get('Devices')
            and not host.get('PortBindings') and host.get('PidsLimit') == 64
            and host.get('Memory') == 128 * 1024 * 1024 and host.get('NanoCpus') == 500000000
            and type(tmpfs) is dict and set(tmpfs) == set(TMPFS)
            and all(set(value.split(',')) == {'rw', 'nosuid', 'nodev', 'noexec', 'size=1m'}
                    for value in tmpfs.values() if isinstance(value, str))
            and all(isinstance(value, str) for value in tmpfs.values()))


def owned(doc, container_id, name, nonce):
    """Malformed identity is not authority to delete a resource."""
    if type(doc) is not dict or type(doc.get('Config')) is not dict:
        return False
    labels = doc['Config'].get('Labels')
    return (type(labels) is dict and doc.get('Id') == container_id
            and doc.get('Name') == '/' + name and labels.get(LABEL) == nonce)


def configuration_evidence(doc):
    """Retain only fields used by the validator; omit image environment/secrets."""
    host_fields = ('NetworkMode', 'ReadonlyRootfs', 'Privileged', 'CapDrop', 'CapAdd',
                   'SecurityOpt', 'PidMode', 'IpcMode', 'UTSMode', 'UsernsMode',
                   'VolumesFrom', 'DeviceRequests', 'Binds', 'Mounts', 'Devices',
                   'PortBindings', 'PidsLimit', 'Memory', 'NanoCpus', 'Tmpfs')
    return deepcopy({'Id': doc['Id'], 'Name': doc['Name'], 'Image': doc['Image'],
        'Config': {**{key: doc['Config'][key] for key in ('User', 'Entrypoint', 'Cmd')},
                   'Labels': {LABEL: doc['Config']['Labels'][LABEL]}},
        'HostConfig': {key: doc['HostConfig'].get(key) for key in host_fields},
        'Mounts': doc['Mounts']})


def assess_stdout(raw):
    if type(raw) is not str:
        return 'INCOMPLETE'
    rows = raw.splitlines()
    if len(rows) != len(EXPECTED):
        return 'FAIL'
    try:
        pairs = [line.split('=', 1) for line in rows]
        if any(len(pair) != 2 for pair in pairs) or len({pair[0] for pair in pairs}) != len(pairs):
            return 'FAIL'
        values = dict(pairs)
    except ValueError:
        return 'FAIL'
    return 'PASS' if values == EXPECTED else 'FAIL'


def command(executable, *args, input_text=None, timeout=30):
    # Bytes avoid Windows text-mode newline conversion in the candidate input.
    raw = subprocess.run([executable, *args], input=None if input_text is None else input_text.encode('utf-8'),
                         capture_output=True, timeout=timeout, check=False)
    return subprocess.CompletedProcess(raw.args, raw.returncode,
        raw.stdout.decode('utf-8', errors='replace'), raw.stderr.decode('utf-8', errors='replace'))


def create_args(name, nonce):
    args = ['create', '--name', name, '--label', LABEL + '=' + nonce,
            '--pull=never', '--network=none', '--read-only', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--user', '65534:65534',
            '--pids-limit=64', '--memory=128m', '--cpus=0.5', '--interactive',
            '--entrypoint', '/bin/sh']
    for path in TMPFS:
        args.extend(['--tmpfs', path + ':rw,nosuid,nodev,noexec,size=1m'])
    return [*args, IMAGE, '-c', SCRIPT]


def run(executable='docker', call=command):
    """Create one uniquely labelled container and remove only that exact resource."""
    nonce = uuid.uuid4().hex
    name = 'aml-df-isolation-' + nonce
    output = {'status': 'INCOMPLETE', 'candidate_status': 'INCOMPLETE',
              'evidence_schema': 'docker-isolation-v2',
              'resource': {'name': name, 'label_key': LABEL, 'label_value': nonce, 'id': None},
              'container_configuration': 'INCOMPLETE', 'cleanup': 'NOT_STARTED',
              'network': 'NOT_OBSERVED', 'hidden_holdout': 'NOT_ESTABLISHED',
              'independent_attestation': 'NOT_ESTABLISHED', 'aml_product': 'NOT_TESTED',
              'scope': 'Synthetic arithmetic candidate, no AML, one local Docker host.',
              'image_id': IMAGE, 'image_is_independently_attested': False,
              'input_sha256': sha256(INPUT.encode()).hexdigest(),
              'source_sha256': sha256(Path(__file__).read_bytes()).hexdigest()}
    owned_id = None
    try:
        img = call(executable, 'image', 'inspect', IMAGE, '--format', '{{.Id}}')
        if img.returncode != 0 or img.stdout.strip() != IMAGE:
            output['reason'] = 'LOCAL_IMAGE_NOT_AVAILABLE'
            return output
        # A timeout may leave a created resource without a returned ID. Keep
        # the ownership hint for inspection, but never delete by guessed name.
        output['cleanup'] = 'INCOMPLETE'
        created = call(executable, *create_args(name, nonce))
        if created.returncode != 0 or not re.fullmatch('[0-9a-f]{64}', created.stdout.strip()):
            output['reason'] = 'CREATE_FAILED'
            return output
        owned_id = created.stdout.strip()
        output['resource']['id'] = owned_id
        inspected = call(executable, 'inspect', owned_id)
        if inspected.returncode != 0:
            output['reason'] = 'INSPECT_FAILED'
            return output
        data = json.loads(inspected.stdout)
        if type(data) is not list or len(data) != 1 or type(data[0]) is not dict or data[0].get('Id') != owned_id:
            output['reason'] = 'INVALID_INSPECT'
            return output
        if not configuration(data[0], name, nonce):
            output.update(status='FAIL', container_configuration='FAIL', reason='CONFIGURATION_MISMATCH')
            return output
        output.update(container_configuration='PASS', network='CONFIGURED_NONE')
        output['configuration_evidence'] = configuration_evidence(data[0])
        completed = call(executable, 'start', '--attach', '--interactive', owned_id, input_text=INPUT)
        output['candidate_exit_code'] = completed.returncode
        output['candidate_stdout'] = completed.stdout
        if completed.returncode != 0:
            output['reason'] = 'CANDIDATE_EXECUTION_FAILED'
            return output
        output['candidate_status'] = assess_stdout(completed.stdout)
        output['status'] = output['candidate_status']
        output['reason'] = 'CHECKED_SYNTHETIC_OUTPUT'
        return output
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError):
        output['reason'] = 'DOCKER_UNAVAILABLE_OR_INCOMPLETE'
        return output
    finally:
        if owned_id is not None:
            # Only a successfully returned, exact container ID can be removed;
            # a create timeout leaves an unconfirmed resource untouched.
            try:
                checked = call(executable, 'inspect', owned_id)
                data = json.loads(checked.stdout) if checked.returncode == 0 else []
                if type(data) is list and len(data) == 1 and owned(data[0], owned_id, name, nonce):
                    removed = call(executable, 'rm', '--force', owned_id)
                    output['cleanup'] = 'PASS' if removed.returncode == 0 else 'INCOMPLETE'
                else:
                    output['cleanup'] = 'INCOMPLETE'
            except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError):
                output['cleanup'] = 'INCOMPLETE'
            if output['cleanup'] != 'PASS':
                output['status'] = 'INCOMPLETE' if output['status'] != 'FAIL' else 'FAIL'
        output['source_sha256_after'] = sha256(Path(__file__).read_bytes()).hexdigest()
        if output['source_sha256_after'] != output['source_sha256']:
            output['status'] = 'INCOMPLETE' if output['status'] != 'FAIL' else 'FAIL'
            output['reason'] = 'SOURCE_CHANGED'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    try:
        directory = checked_path(ROOT / args.output_dir, ROOT)
        parts = directory.relative_to(ROOT).parts
        if len(parts) != 4 or parts[:3] != ('var', 'qa', 'dark-isolation'):
            raise ValueError('INVALID_OUTPUT_PATH')
        directory.mkdir(parents=True, exist_ok=False)
        result = run()
        with (directory / 'result.json').open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
    except (OSError, ValueError):
        return 2
    print(json.dumps({'status': result['status'], 'output': str((directory / 'result.json').relative_to(ROOT))}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
