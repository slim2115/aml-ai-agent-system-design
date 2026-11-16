"""Prepare/start/status/stop one owned QA stand. Data is retained on stop.

Windows local executables are reused. No installer, reset, recursive delete,
model preparation, operator decision, commit or push is performed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.qa_profile import QAProfile, checked_path
from qa.tools.environment import child_environment, local_http, port_free, read_state, write_json, same_process, process_creation

PYTHON = ROOT / '.venv/Scripts/python.exe'
PG_BIN = ROOT / 'var/tools/postgresql-17.11/pgsql/bin'


def prepare(run_id=None):
    profile = QAProfile(run_id or 'qa-' + secrets.token_hex(6))
    for path in (profile.base, profile.workflow_dir):
        checked_path(path)
        if path.exists():
            raise ValueError('QA_RUN_ALREADY_EXISTS')
    profile.base.mkdir(parents=True)
    (profile.base / 'owner.json').write_text(json.dumps(profile.owner_record()), encoding='utf-8')
    profile.workflow_dir.mkdir(parents=True)
    write_json(profile, profile.workflow_dir / 'owner.json', profile.owner_record(), exclusive=True)
    for name in ('tmp', 'logs', 'lab', 'postgres', 'exports', 'chroma', 'cache/hf/hub',
                 'cache/torch', 'cache/xdg', 'cache/matplotlib', 'data/demo-v1', 'config'):
        (profile.base / name).mkdir(parents=True, exist_ok=True)
    hashes = []
    for source in sorted((ROOT / 'data/demo-v1').glob('*.json')) + [ROOT / 'config/demo-policy.json']:
        checked_path(source)
        destination = profile.policy_path if source.name == 'demo-policy.json' else profile.seed_dir / source.name
        shutil.copyfile(source, destination)
        hashes.append({'source': source.relative_to(ROOT).as_posix(),
                       'copy': destination.relative_to(ROOT).as_posix(),
                       'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()})
    write_json(profile, profile.base / 'source-copies.json', hashes, exclusive=True)
    settings = {'host': '127.0.0.1', 'port': profile.pg_port, 'database': profile.db_name,
                'api_port': profile.api_port, 'run_id': profile.run_id, 'marker': profile.marker,
                'api_token': secrets.token_hex(32),
                'roles': {key: {'user': value, 'password': secrets.token_hex(32)}
                          for key, value in profile.role_names.items()}}
    write_json(profile, profile.config_path, settings, exclusive=True)
    result = public_description(profile)
    write_json(profile, profile.base / 'stand.json', result, exclusive=True)
    return result


def public_description(profile):
    return {'run_id': profile.run_id, 'owner': profile.marker, 'root': str(ROOT),
            'ui': f'http://127.0.0.1:{profile.ui_port}', 'swagger': profile.api_url + '/docs',
            'postgres': {'host': '127.0.0.1', 'port': profile.pg_port, 'database': profile.db_name},
            'kafka': {'bootstrap': f'127.0.0.1:{profile.kafka_port}', 'project': profile.compose_project,
                      'topics': [profile.topic, profile.dlq_topic, profile.analysis_topic, profile.analysis_dlq],
                      'groups': [profile.group, profile.analysis_group]},
            'data_directory': str(profile.base), 'workflow_directory': str(profile.workflow_dir),
            'model_preparation': 'explicit_only_not_started_by_launcher'}


def command(profile, args, label, timeout=40):
    profile.validate()
    path = checked_path(profile.base / 'logs' / (label + '.log'))
    with path.open('ab') as log:
        result = subprocess.run([str(arg) for arg in args], cwd=ROOT, env=child_environment(profile),
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=timeout,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise RuntimeError('QA_COMMAND_FAILED:' + label + ':see-owned-log')


def pg_running(profile):
    profile.validate()
    cluster = checked_path(profile.base / 'postgres/cluster')
    if (cluster / 'PG_VERSION').exists():
        marker_path = checked_path(profile.base / 'postgres/owner.json')
        marker = json.loads(marker_path.read_text(encoding='utf-8'))
        if marker != dict(profile.owner_record(), cluster=str(cluster), port=profile.pg_port):
            raise ValueError('QA_POSTGRES_OWNER_MISMATCH')
    pid_file = checked_path(cluster / 'postmaster.pid')
    if not pid_file.exists():
        return False
    fields = pid_file.read_text(encoding='utf-8').splitlines()
    if len(fields) < 4 or Path(fields[1]).resolve() != cluster or int(fields[3]) != profile.pg_port:
        raise ValueError('QA_POSTGRES_OWNER_MISMATCH')
    result = subprocess.run([str(PG_BIN / 'pg_ctl.exe'), '-D', str(cluster), 'status'],
                            capture_output=True, timeout=5,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode not in (0, 3):
        raise RuntimeError('QA_POSTGRES_STATUS_FAILED')
    return result.returncode == 0


def start_postgres(profile):
    cluster = checked_path(profile.base / 'postgres/cluster')
    if pg_running(profile):
        return
    if not port_free(profile.pg_port):
        raise RuntimeError('QA_POSTGRES_PORT_BUSY')
    settings = json.loads(profile.config_path.read_text(encoding='utf-8'))
    if not (cluster / 'PG_VERSION').exists():
        if cluster.exists() and any(cluster.iterdir()):
            raise RuntimeError('QA_UNFINISHED_CLUSTER_REQUIRES_INSPECTION')
        password = checked_path(profile.base / 'tmp/initdb-password.txt')
        with password.open('x', encoding='utf-8') as file:
            file.write(settings['roles']['admin']['password'])
        try:
            command(profile, [PG_BIN / 'initdb.exe', '-D', cluster, '-U', profile.role_names['admin'],
                              '--pwfile', password, '--auth=scram-sha-256', '--encoding=UTF8', '--locale=C'], 'initdb')
        finally:
            password.unlink()
        with (cluster / 'postgresql.conf').open('a', encoding='utf-8') as file:
            file.write(f"\nlisten_addresses='127.0.0.1'\nport={profile.pg_port}\nmax_connections=20\n"
                       "shared_buffers='64MB'\nwork_mem='4MB'\nmaintenance_work_mem='32MB'\n"
                       "timezone='UTC'\nlog_min_messages=warning\nlog_min_error_statement=panic\n")
        users = ','.join(profile.role_names[key] for key in ('reader', 'student', 'api'))
        (cluster / 'pg_hba.conf').write_text(
            f"host all {profile.role_names['admin']} 127.0.0.1/32 scram-sha-256\n"
            f"host {profile.db_name} {users} 127.0.0.1/32 scram-sha-256\n"
            "host all all 0.0.0.0/0 reject\nhost all all ::0/0 reject\n", encoding='utf-8')
        write_json(profile, profile.base / 'postgres/owner.json',
                   dict(profile.owner_record(), cluster=str(cluster), port=profile.pg_port), exclusive=True)
    marker = json.loads((profile.base / 'postgres/owner.json').read_text(encoding='utf-8'))
    if marker != dict(profile.owner_record(), cluster=str(cluster), port=profile.pg_port):
        raise ValueError('QA_POSTGRES_OWNER_MISMATCH')
    command(profile, [PG_BIN / 'pg_ctl.exe', '-D', cluster, '-l', profile.base / 'logs/postgres.log',
                      '-w', '-t', '30', 'start'], 'postgres-start')


def service_ready(profile, service):
    record = read_state(profile, service)
    if not record or record['status'] != 'running' or not same_process(record):
        return False
    if service == 'api':
        try:
            health = json.loads(local_http(profile, profile.api_port, '/health'))
            return health.get('qa_run_id') == profile.run_id and health.get('qa_owner') == profile.marker
        except (OSError, ValueError):
            return False
    if service == 'ui':
        try:
            return local_http(profile, profile.ui_port, '/_stcore/health') == b'ok'
        except OSError:
            return False
    # Worker ownership/readiness is supplemented by live checks in qualification.
    return True


def start_service(profile, service):
    if service_ready(profile, service):
        return
    port = {'api': profile.api_port, 'ui': profile.ui_port}.get(service)
    if port and not port_free(port):
        raise RuntimeError('QA_SERVICE_PORT_BUSY:' + service)
    prior = read_state(profile, service)
    if prior and prior.get('status') in ('starting', 'running') and same_process(prior):
        raise RuntimeError('QA_SERVICE_STATE_REQUIRES_STOP:' + service)
    launch = read_launch(profile, service)
    if launch and same_process(launch):
        raise RuntimeError('QA_SERVICE_START_ALREADY_PENDING:' + service)
    args = [str(PYTHON), '-I', '-B']
    args += [str(ROOT / 'scripts/lab/run_api.py')] if service == 'api' else [str(ROOT / 'qa/tools/managed_service.py'), service]
    instance = secrets.token_hex(16)
    environment = child_environment(profile)
    environment['AML_QA_SERVICE_INSTANCE'] = instance
    with (profile.base / 'logs' / (service + '.log')).open('ab') as log:
        process = subprocess.Popen(args, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, close_fds=True,
                                   creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0)
    launch = {'root': str(ROOT), 'qa_run_id': profile.run_id, 'qa_owner': profile.marker,
              'instance_id': instance, 'pid': process.pid, 'process_creation': process_creation(process.pid)}
    write_json(profile, profile.base / 'lab' / (service + '-launch.json'), launch)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('QA_SERVICE_EXITED:' + service + ':see-owned-log')
        state = read_state(profile, service)
        if state and state.get('instance_id') == instance and service_ready(profile, service):
            return
        time.sleep(.2)
    # A slow import may not have published process.json yet. The parent-bound
    # instance request remains valid when the child reaches its monitor. Keep
    # the active run and all dependencies until stop confirms that it exited.
    write_json(profile, profile.base / 'lab' / (service + '-stop.json'), launch)
    raise RuntimeError('QA_SERVICE_START_TIMEOUT:' + service)


def read_launch(profile, service):
    path = checked_path(profile.base / 'lab' / (service + '-launch.json'))
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    if (value.get('root') != str(ROOT) or value.get('qa_run_id') != profile.run_id
            or value.get('qa_owner') != profile.marker):
        raise ValueError('QA_PROCESS_OWNER_MISMATCH')
    return value


def stop_service(profile, service):
    record = read_state(profile, service)
    launch = read_launch(profile, service)
    if launch and same_process(launch) and (not record or record.get('instance_id') != launch['instance_id']):
        write_json(profile, profile.base / 'lab' / (service + '-stop.json'), launch)
        deadline = time.monotonic() + 45
        while same_process(launch) and time.monotonic() < deadline:
            time.sleep(.2)
        if same_process(launch):
            raise RuntimeError('QA_STARTING_PROCESS_STILL_RUNNING:' + service + ':dependencies-left-running')
        record = read_state(profile, service)
    if not record:
        return
    if not same_process(record):
        write_json(profile, profile.base / 'lab' / (service + '-process.json'), dict(record, status='stale'))
        return
    if record['status'] in ('stopped', 'failed'):
        # The status is emitted in finally just before the process exits.
        deadline = time.monotonic() + 5
        while same_process(record) and time.monotonic() < deadline:
            time.sleep(.1)
        if same_process(record):
            raise RuntimeError('QA_STOPPING_PROCESS_STILL_RUNNING:' + service)
        return
    request = {key: record[key] for key in ('root', 'instance_id', 'qa_run_id', 'qa_owner')}
    write_json(profile, profile.base / 'lab' / (service + '-stop.json'), request)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        current = read_state(profile, service)
        if current['instance_id'] != record['instance_id']:
            raise RuntimeError('QA_SERVICE_INSTANCE_CHANGED')
        if current['status'] in ('stopped', 'failed') and not same_process(current):
            return
        time.sleep(.2)
    raise RuntimeError('QA_SERVICE_STOP_TIMEOUT:' + service + ':dependencies-left-running')


def start(profile):
    if checked_path(profile.base / 'browser-profile.json').exists():
        raise ValueError('QA_BROWSER_USE_BROWSER_STAND_LAUNCHER')
    from src.workflow.lease import ControllerLease
    from qa.tools import broker
    active = checked_path(ROOT / 'var/qa/active.json')
    lease = ControllerLease(ROOT / 'var/workflow/qa/environment.sqlite3')
    try:
        if active.exists():
            previous = json.loads(active.read_text(encoding='utf-8'))
            if previous != profile.owner_record():
                raise RuntimeError('ANOTHER_QA_RUN_MUST_BE_STOPPED_FIRST')
        else:
            with active.open('x', encoding='utf-8') as file:
                json.dump(profile.owner_record(), file)
        start_postgres(profile)
        command(profile, [PYTHON, '-I', '-B', ROOT / 'scripts/lab/database.py', 'bootstrap'], 'database-bootstrap')
        broker.start(profile)
        command(profile, [PYTHON, '-I', '-B', ROOT / 'scripts/kafka/lab.py', 'initialize-aml'], 'schema-bootstrap')
        for service in ('api', 'worker', 'review-worker', 'ui'):
            start_service(profile, service)
        result = dict(public_description(profile), status='ready', started_at_utc=datetime.now(timezone.utc).isoformat())
        write_json(profile, profile.base / 'last-start.json', result)
        return result
    finally:
        lease.close()


def stop(profile):
    from src.workflow.lease import ControllerLease
    from qa.tools import broker
    lease = ControllerLease(ROOT / 'var/workflow/qa/environment.sqlite3')
    try:
        for service in ('ui', 'worker', 'review-worker', 'api'):
            stop_service(profile, service)
        if pg_running(profile):
            command(profile, [PG_BIN / 'pg_ctl.exe', '-D', profile.base / 'postgres/cluster',
                              '-m', 'fast', '-w', '-t', '30', 'stop'], 'postgres-stop')
        broker.stop(profile)
        active = checked_path(ROOT / 'var/qa/active.json')
        if active.exists() and json.loads(active.read_text(encoding='utf-8')) == profile.owner_record():
            active.unlink()
        return {'status': 'stopped', 'run_id': profile.run_id, 'data': 'retained'}
    finally:
        lease.close()


def status(profile):
    from qa.tools import broker
    services = {name: service_ready(profile, name) for name in ('api', 'ui', 'worker', 'review-worker')}
    result = dict(public_description(profile), services=services, postgres_running=pg_running(profile),
                  broker=broker.status(profile))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'start', 'status', 'stop'))
    parser.add_argument('--run-id')
    args = parser.parse_args()
    if args.action == 'prepare':
        result = prepare(args.run_id)
    else:
        profile = QAProfile(args.run_id).validate()
        os.environ['AML_QA_RUN_ID'] = profile.run_id
        result = {'start': start, 'stop': stop, 'status': status}[args.action](profile)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
