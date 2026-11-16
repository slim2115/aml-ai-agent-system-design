"""Local durable workflow CLI. JSON bodies use the existing v1 contracts."""
import argparse
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.contracts.semantics import classify_incident_id
from src.contracts.v1 import CreateJobRequest, ErrorResult
from src.runtime.policy import load_demo_policy, strict_json
from src.workflow.auth import trusted_local_principal
from src.workflow.controller import WorkflowService
from src.workflow.delivery import DeliveryService
from src.workflow.store import SQLiteStore, WorkflowError, utc_now


def run_one(incident_id, *, engine='template', store=None, readiness_timeout=180, job_timeout=None):
    code = classify_incident_id(incident_id)
    if code:
        raise WorkflowError(code)
    principal = trusted_local_principal()
    with WorkflowService(store=store) as service:
        service.prepare(timeout_seconds=readiness_timeout)
        while service.readiness()['state'] == 'preparing':
            time.sleep(.05)
        if not service.readiness()['ready']:
            raise WorkflowError('NOT_READY')
        policy = load_demo_policy(ROOT / 'config/demo-policy.json')
        request = CreateJobRequest(schema_version='1.0', incident_id=incident_id, window=policy.window)
        job = service.submit(request, principal, 'cli-' + uuid4().hex, engine=engine, timeout_seconds=job_timeout)
        while True:
            current = service.store.get_job(job['job_id'], principal)
            if current['status'] not in ('queued', 'running'):
                return current
            time.sleep(.05)


def load_request(path):
    with Path(path).open('rb') as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise WorkflowError('INVALID_INPUT_FORMAT')
    return strict_json(data)


def safe_error(code):
    return ErrorResult(schema_version='1.0', status='error', generated_at=utc_now(),
        correlation_id='cli-' + uuid4().hex, code=code,
        message='Локальная операция не завершена. Проверьте код, состояние и параметры запроса.',
        retryable=code in {'NOT_READY', 'DEPENDENCY_UNAVAILABLE'}).model_dump(mode='json')


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, help='Database path must be inside rebuild/var/workflow')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('incident_id')
    run.add_argument('--demo', action='store_true')
    run.add_argument('--engine', choices=['template', 'local-model'], default='template')
    commands.add_parser('list')
    get = commands.add_parser('get')
    get.add_argument('job_id')
    events = commands.add_parser('events')
    events.add_argument('job_id')
    cancel = commands.add_parser('cancel')
    cancel.add_argument('job_id')
    cancel.add_argument('--key', required=True)
    for name in ('decide', 'deliver'):
        command = commands.add_parser(name)
        command.add_argument('--request', required=True, type=Path)
        command.add_argument('--key', required=True)
    dispatch = commands.add_parser('dispatch')
    dispatch.add_argument('delivery_id')
    dispatch.add_argument('--revision', type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'run' and not args.demo:
            from src.runtime.service import AMLAgent
            value = AMLAgent().run(args.incident_id)
            code = 2 if value['status'] == 'refusal' else 3
        else:
            store = SQLiteStore(args.database) if args.database else SQLiteStore()
            principal = trusted_local_principal()
            if args.command == 'run':
                value = run_one(args.incident_id, engine=args.engine, store=store)
                code = 0 if value['status'] == 'succeeded' else (2 if value['status'] == 'refused' else 3)
            elif args.command == 'list':
                value, code = store.list_jobs(principal), 0
            elif args.command == 'get':
                value, code = store.get_job(args.job_id, principal), 0
            elif args.command == 'events':
                value, code = store.list_runtime_events(args.job_id, principal), 0
            elif args.command == 'cancel':
                value, code = store.cancel_job(args.job_id, principal, idempotency_key=args.key), 0
            elif args.command == 'decide':
                body = load_request(args.request)
                value = store.create_decision(body['report']['report_id'], body, principal, args.key)
                code = 0
            elif args.command == 'deliver':
                value = DeliveryService(store).create(load_request(args.request), principal, args.key).model_dump(mode='json')
                code = 0
            else:
                value = DeliveryService(store).dispatch(args.delivery_id, principal, args.revision).model_dump(mode='json')
                code = 0 if value['status'] == 'delivered' else 3
    except WorkflowError as exc:
        value, code = safe_error(exc.code), 3
    except (ValueError, TypeError, KeyError, OSError):
        value, code = safe_error('INVALID_INPUT_FORMAT'), 3
    except KeyboardInterrupt:
        value, code = safe_error('INVALID_STATE'), 3
    except Exception:
        value, code = safe_error('INTERNAL_ERROR'), 3
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
