"""Owned QA UI/workers with instance-bound cooperative shutdown; no PID killing."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.qa_profile import get_qa_profile, checked_path
from src.workflow.lease import ControllerLease
from qa.tools.environment import write_json, process_creation


UI_GRACEFUL_SHUTDOWN_SECONDS = 5
UI_STOP_TIMEOUT_SECONDS = 20


def configure_ui_shutdown(server):
    """Bound the pinned Streamlit 1.60 / Uvicorn connection-drain wait.

    Streamlit does not expose this Uvicorn option in its public configuration.
    Fail explicitly if a dependency upgrade changes the embedded server shape.
    This touches only the server object owned by this QA process.
    """
    embedded = getattr(getattr(server, '_starlette_server', None), '_server', None)
    configuration = getattr(embedded, 'config', None)
    if configuration is None or not hasattr(configuration, 'timeout_graceful_shutdown'):
        raise RuntimeError('QA_UI_SHUTDOWN_CONFIG_UNSUPPORTED')
    configuration.timeout_graceful_shutdown = UI_GRACEFUL_SHUTDOWN_SECONDS


async def stop_ui(server):
    """Request cooperative shutdown and report failure if it does not complete."""
    server.stop()
    try:
        await asyncio.wait_for(server.stopped, timeout=UI_STOP_TIMEOUT_SECONDS)
    except TimeoutError:
        raise RuntimeError('QA_UI_SHUTDOWN_TIMEOUT') from None


async def serve_ui(server, save, stop_requested):
    # Configuration or state-record errors after start must still stop this
    # server. The parent retains dependencies until the owned process exits.
    await server.start()
    try:
        configure_ui_shutdown(server)
        save('running')
        while not stop_requested():
            await asyncio.sleep(.2)
    finally:
        await stop_ui(server)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('service', choices=('ui', 'worker', 'review-worker'))
    args = parser.parse_args()
    profile = get_qa_profile()
    if profile is None:
        raise ValueError('QA_PROFILE_REQUIRED')
    lease = ControllerLease(profile.workflow_dir / (args.service + '-server.sqlite3'))
    instance = os.environ.get('AML_QA_SERVICE_INSTANCE', uuid4().hex)
    if re.fullmatch('[0-9a-f]{32}', instance) is None:
        raise ValueError('INVALID_QA_SERVICE_INSTANCE')
    record = {'root': str(ROOT), 'qa_run_id': profile.run_id, 'qa_owner': profile.marker,
              'instance_id': instance, 'pid': os.getpid(), 'service': args.service,
              'process_creation': process_creation(os.getpid()),
              'started_at_utc': datetime.now(timezone.utc).isoformat()}
    state = profile.base / 'lab' / (args.service + '-process.json')
    stop_path = profile.base / 'lab' / (args.service + '-stop.json')

    def save(status):
        write_json(profile, state, dict(record, status=status))

    def stop_requested():
        checked_path(stop_path, ROOT)
        try:
            value = json.loads(stop_path.read_text(encoding='utf-8-sig'))
            return all(value.get(key) == record[key] for key in ('root', 'qa_run_id', 'qa_owner', 'instance_id'))
        except (OSError, ValueError):
            return False

    save('starting')
    code = 1
    try:
        if args.service == 'ui':
            from streamlit.web import bootstrap
            from streamlit.web.server import Server
            options = {'server.address': '127.0.0.1', 'server.port': profile.ui_port,
                       'server.headless': True, 'browser.gatherUsageStats': False,
                       'server.fileWatcherType': 'none', 'server.runOnSave': False}
            bootstrap.load_config_options(options)

            script = str(ROOT / 'src/ui/app.py')
            sys.argv = [script]

            asyncio.run(serve_ui(Server(script, False), save, stop_requested))
            code = 0
        else:
            from src.lab.database import LabDatabase
            from src.lab.kafka.broker import KafkaBroker
            from src.lab.kafka.store import KafkaStore
            database = LabDatabase()
            if args.service == 'worker':
                from src.lab.kafka.analysis_store import AnalysisStore
                from src.lab.kafka.analysis_consumer import run_worker
                from src.workflow.remote import WorkflowClient
                store = AnalysisStore(database)
                store.list_results(limit=1)
                broker = KafkaBroker(worker=True, flow='aml')
                client = WorkflowClient()
                call = lambda: run_worker(store, broker, client, stop=stop_requested)
            else:
                from src.lab.kafka.consumer import run_worker
                store = KafkaStore(database)
                store.status()
                broker = KafkaBroker(worker=True)
                call = lambda: run_worker(store, broker, stop=stop_requested)
            try:
                broker.status()
                save('running')
                code = call()
            finally:
                broker.close()
    finally:
        save('stopped' if code == 0 else 'failed')
        lease.close()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
