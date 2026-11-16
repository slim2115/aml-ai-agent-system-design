"""Single local server with an instance-bound graceful stop request."""
import json
import os
import re
from pathlib import Path
import sys
import threading
from datetime import datetime, timezone
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.lab.api import create_app
from src.qa_profile import checked_path, get_qa_profile
from src.workflow.lease import ControllerLease
import uvicorn

PROFILE = get_qa_profile()
STATE = checked_path(PROFILE.base / 'lab/api-process.json') if PROFILE else ROOT / 'var/lab/api-process.json'
STOP = checked_path(PROFILE.base / 'lab/api-stop.json') if PROFILE else ROOT / 'var/lab/api-stop.json'


def main():
    lease_path = PROFILE.workflow_dir / 'lab-api-server.sqlite3' if PROFILE else ROOT / 'var/workflow/lab-api-server.sqlite3'
    lease = ControllerLease(lease_path)
    instance = uuid4().hex
    if PROFILE:
        instance = os.environ.get('AML_QA_SERVICE_INSTANCE', instance)
        if re.fullmatch('[0-9a-f]{32}', instance) is None:
            raise ValueError('INVALID_QA_SERVICE_INSTANCE')
    record = {'root': str(ROOT), 'pid': os.getpid(), 'instance_id': instance,
              'started_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'starting'}
    if PROFILE is not None:
        from qa.tools.environment import process_creation
        record.update(qa_run_id=PROFILE.run_id, qa_owner=PROFILE.marker,
                      process_creation=process_creation(os.getpid()))
    STATE.parent.mkdir(parents=True, exist_ok=True)

    def save(status):
        record['status'] = status
        if PROFILE:
            from qa.tools.environment import write_json
            write_json(PROFILE, STATE, record)
            return
        temporary = STATE.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, indent=2), encoding='utf-8')
        temporary.replace(STATE)

    server = None
    finished = threading.Event()
    try:
        save('starting')
        server = uvicorn.Server(uvicorn.Config(create_app(), host='127.0.0.1', port=PROFILE.api_port if PROFILE else 8765,
                                log_level='warning', access_log=False))

        def monitor():
            announced = False
            while not finished.wait(.25):
                if server.started and not announced:
                    save('running')
                    announced = True
                try:
                    request = json.loads(STOP.read_text(encoding='utf-8-sig'))
                    qa_matches = PROFILE is None or (request.get('qa_run_id') == PROFILE.run_id
                                                     and request.get('qa_owner') == PROFILE.marker)
                    if request.get('instance_id') == instance and request.get('root') == str(ROOT) and qa_matches:
                        server.should_exit = True
                        return
                except (OSError, ValueError):
                    pass

        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        server.run()
    finally:
        finished.set()
        if 'watcher' in locals():
            watcher.join(timeout=2)
        save('stopped')
        lease.close()


if __name__ == '__main__':
    main()
