"""Dedicated QA-only API process; ordinary API launcher never imports adapters."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import threading
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def stop_requested(profile, path, record):
    """Ignore malformed/stale requests; only the current owned instance can stop."""
    from src.qa_profile import checked_path
    try:
        path = checked_path(path, profile.root)
        if path.stat().st_size > 4096:
            return False
        request = json.loads(path.read_text(encoding='utf-8-sig'))
        return isinstance(request, dict) and all(
            request.get(key) == record[key]
            for key in ('root', 'qa_run_id', 'qa_owner', 'instance_id'))
    except (OSError, ValueError):
        return False


def main():
    import uvicorn
    from src.qa_profile import get_qa_profile, checked_path
    from src.workflow.lease import ControllerLease
    from qa.tools.environment import process_creation, write_json
    from qa.browser_backend import create_browser_app, validate_browser_profile
    profile = get_qa_profile()
    if profile is None:
        raise ValueError('QA_PROFILE_REQUIRED')
    # A regular QA run must not acquire a browser lease or process-state file.
    validate_browser_profile(profile)
    instance = os.environ.get('AML_QA_SERVICE_INSTANCE', uuid4().hex)
    if re.fullmatch('[0-9a-f]{32}', instance) is None:
        raise ValueError('INVALID_QA_SERVICE_INSTANCE')
    lease = ControllerLease(profile.workflow_dir / 'lab-api-server.sqlite3')
    state = checked_path(profile.base / 'lab/api-process.json')
    stop = checked_path(profile.base / 'lab/api-stop.json')
    record = dict(root=str(ROOT), qa_run_id=profile.run_id, qa_owner=profile.marker,
                  instance_id=instance, pid=os.getpid(), process_creation=process_creation(os.getpid()),
                  started_at_utc=datetime.now(timezone.utc).isoformat(), mode='browser-deterministic')
    finished = threading.Event()
    app = None

    def save(status):
        write_json(profile, state, dict(record, status=status))

    try:
        save('starting')
        app = create_browser_app(profile)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=profile.api_port,
                                              log_level='warning', access_log=False))

        def monitor():
            announced = False
            while not finished.wait(.2):
                if server.started and not announced:
                    save('running')
                    announced = True
                if stop_requested(profile, stop, record):
                    server.should_exit = True
                    return
        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        server.run()
    finally:
        finished.set()
        if 'watcher' in locals():
            watcher.join(timeout=2)
        if app is not None:
            app.state.browser_workflow.close()
        try:
            save('stopped')
        finally:
            lease.close()


if __name__ == '__main__':
    main()
