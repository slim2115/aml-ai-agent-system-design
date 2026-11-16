"""Explicit live API/client check for the pre-Docker stage; no broker is expected."""
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import socket
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    import httpx
    from src.lab.config import load_lab_settings
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = ROOT / 'var/checks/kafka-readiness' / stamp
    output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'FAIL', 'checks': [], 'scope': 'Live loopback HTTP, installed Kafka client, '
              'bounded broker-unavailable response. Kafka delivery is NOT tested.'}
    def check(name, passed, **details):
        report['checks'].append({'name': name, 'passed': bool(passed), **details})
        if not passed:
            raise AssertionError(name)
    try:
        with socket.socket() as probe:
            probe.settimeout(1)
            check('broker_absent_precondition', probe.connect_ex(('127.0.0.1', 9092)) != 0)
        report['client_version'] = importlib.metadata.version('confluent-kafka')
        cfg = load_lab_settings()
        headers = {'X-Lab-Key': cfg['api_token']}
        with httpx.Client(base_url='http://127.0.0.1:8765', timeout=15, trust_env=False) as client:
            check('health_without_broker', client.get('/health').status_code == 200)
            check('swagger_without_broker', client.get('/docs').status_code == 200)
            spec = client.get('/openapi.json').json()
            expected = {'/lab/kafka/events', '/lab/kafka/status', '/lab/kafka/results', '/lab/kafka/results/{event_id}'}
            check('kafka_routes_in_openapi', expected.issubset(spec['paths']))
            check('auth_required', client.get('/lab/kafka/status').status_code == 401)
            check('invalid_input_rejected', client.post('/lab/kafka/events', headers=headers,
                  json={'synthetic': False}).status_code == 400)
            before = time.monotonic()
            status = client.get('/lab/kafka/status', headers=headers).json()
            check('component_status_separates_broker_from_db',
                  status['broker']['status'] == 'unavailable' and status['database']['status'] == 'ready',
                  seconds=round(time.monotonic() - before, 3))
            event = {'event_id': str(uuid4()), 'incident_id': 'INC-000123',
                     'occurred_at': datetime.now(timezone.utc).isoformat()}
            report['event_id'] = event['event_id']
            before = time.monotonic()
            response = client.post('/lab/kafka/events', headers=headers, json=event)
            elapsed = time.monotonic() - before
            check('real_client_publish_timeout_is_bounded_and_uncertain', response.status_code == 503
                  and response.json().get('publication') == 'unknown' and elapsed < 12,
                  seconds=round(elapsed, 3), http_status=response.status_code)
            result = client.get('/lab/kafka/results/' + event['event_id'], headers=headers)
            check('no_false_success_result', result.status_code == 404
                  and result.json().get('code') == 'KAFKA_RESULT_NOT_FOUND')
            check('sql_api_stays_available', client.get('/lab/clients', headers=headers).status_code == 200)
        report['status'] = 'PASS'
        return 0
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        return 1
    finally:
        report['checked_at_utc'] = datetime.now(timezone.utc).isoformat()
        (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': report['status'], 'evidence': str(output / 'result.json')}))


if __name__ == '__main__':
    raise SystemExit(main())
