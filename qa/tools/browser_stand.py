"""Owned UI/API-only browser stand, without PostgreSQL/Kafka or real models."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.qa_profile import QAProfile, checked_path
from src.workflow.lease import ControllerLease
from qa.tools import stand
from qa.tools.environment import child_environment, process_creation, same_process, write_json, port_free


def require_browser(profile):
    if not isinstance(profile, QAProfile):
        raise ValueError('QA_BROWSER_PROFILE_REQUIRED')
    profile.validate()
    path = checked_path(profile.base / 'browser-profile.json', profile.root)
    try:
        if path.stat().st_size > 4096:
            raise ValueError
        value = json.loads(path.read_text(encoding='utf-8'))
        if value != dict(profile.owner_record(), mode='deterministic'):
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise ValueError('QA_BROWSER_PROFILE_REQUIRED') from None
    return profile


def description(profile):
    return {'run_id': profile.run_id, 'mode': 'browser-deterministic',
            'ui': f'http://127.0.0.1:{profile.ui_port}', 'swagger': profile.api_url + '/docs',
            'data_directory': str(profile.base), 'workflow_directory': str(profile.workflow_dir),
            'real_models': False, 'postgresql': 'not_started', 'kafka': 'fixture_rows_only'}


def prepare():
    result = stand.prepare()
    profile = QAProfile(result['run_id']).validate()
    write_json(profile, profile.base / 'browser-profile.json',
               dict(profile.owner_record(), mode='deterministic'), exclusive=True)
    return description(profile)


def start_api(profile):
    require_browser(profile)
    if stand.service_ready(profile, 'api'):
        state = stand.read_state(profile, 'api')
        if state.get('mode') != 'browser-deterministic':
            raise ValueError('QA_BROWSER_SERVER_MODE_MISMATCH')
        return
    if not port_free(profile.api_port):
        raise RuntimeError('QA_SERVICE_PORT_BUSY:api')
    for prior in (stand.read_state(profile, 'api'), stand.read_launch(profile, 'api')):
        if prior and same_process(prior):
            raise RuntimeError('QA_SERVICE_START_ALREADY_PENDING:api')
    instance = secrets.token_hex(16)
    environment = child_environment(profile)
    environment['AML_QA_SERVICE_INSTANCE'] = instance
    with checked_path(profile.base / 'logs/api.log').open('ab') as log:
        process = subprocess.Popen([str(stand.PYTHON), '-I', '-B', str(ROOT / 'qa/tools/browser_server.py')],
            cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0)
    launch = dict(root=str(ROOT), qa_run_id=profile.run_id, qa_owner=profile.marker,
                  instance_id=instance, pid=process.pid, process_creation=process_creation(process.pid))
    write_json(profile, profile.base / 'lab/api-launch.json', launch)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('QA_BROWSER_API_EXITED:see-owned-log')
        record = stand.read_state(profile, 'api')
        if record and record.get('instance_id') == instance and stand.service_ready(profile, 'api'):
            return
        time.sleep(.2)
    write_json(profile, profile.base / 'lab/api-stop.json', launch)
    raise RuntimeError('QA_SERVICE_START_TIMEOUT:api')


def start(profile):
    require_browser(profile)
    active = checked_path(ROOT / 'var/qa/active.json')
    lease = ControllerLease(ROOT / 'var/workflow/qa/environment.sqlite3')
    try:
        if active.exists():
            if json.loads(active.read_text(encoding='utf-8')) != profile.owner_record():
                raise RuntimeError('ANOTHER_QA_RUN_MUST_BE_STOPPED_FIRST')
        else:
            with active.open('x', encoding='utf-8') as stream:
                json.dump(profile.owner_record(), stream)
        start_api(profile)
        stand.start_service(profile, 'ui')
        result = dict(description(profile), status='ready', started_at_utc=datetime.now(timezone.utc).isoformat())
        write_json(profile, profile.base / 'last-browser-start.json', result)
        return result
    finally:
        lease.close()


def stop(profile):
    require_browser(profile)
    lease = ControllerLease(ROOT / 'var/workflow/qa/environment.sqlite3')
    try:
        for service in ('ui', 'api'):
            stand.stop_service(profile, service)
        if not all(port_free(port) for port in (profile.ui_port, profile.api_port)):
            raise RuntimeError('QA_BROWSER_PORTS_STILL_IN_USE')
        active = checked_path(ROOT / 'var/qa/active.json')
        if active.exists() and json.loads(active.read_text(encoding='utf-8')) == profile.owner_record():
            active.unlink()
        return dict(description(profile), status='stopped', data='retained')
    finally:
        lease.close()


def status(profile):
    require_browser(profile)
    return dict(description(profile), services={name: stand.service_ready(profile, name) for name in ('api', 'ui')})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'start', 'stop', 'status'))
    parser.add_argument('--run-id')
    args = parser.parse_args()
    if args.action == 'prepare':
        result = prepare()
    else:
        profile = require_browser(QAProfile(args.run_id))
        os.environ['AML_QA_RUN_ID'] = profile.run_id
        result = {'start': start, 'stop': stop, 'status': status}[args.action](profile)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
