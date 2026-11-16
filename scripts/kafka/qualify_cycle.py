"""Opt-in live HTTP -> Kafka -> external worker -> PostgreSQL smoke test.

Start the broker, API, schema and foreground worker first. Creates one synthetic
event and its duplicate; deliberately retains the result for SQL inspection.
Never resets a consumer group, starts/stops services, or clears any data.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    import httpx
    from src.lab.config import load_lab_settings
    from src.lab.database import LabDatabase
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = ROOT / 'var/checks/kafka-cycle' / stamp
    output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'INCOMPLETE', 'checks': [], 'scope': 'Live HTTP publication, broker acknowledgements, '
              'external worker, real PostgreSQL result, duplicate delivery. No live fault/recovery qualification.'}
    def check(name, passed):
        report['checks'].append({'name': name, 'passed': bool(passed)})
        if not passed:
            raise ValueError(name)
    try:
        settings = load_lab_settings()
        database = LabDatabase(settings)
        with httpx.Client(base_url='http://127.0.0.1:8765', timeout=15, trust_env=False,
                          headers={'X-Lab-Key': settings['api_token']}) as client:
            response = client.get('/lab/kafka/status')
            check('status_endpoint', response.status_code == 200)
            components = response.json()
            report['components'] = components
            if any(components[name]['status'] != 'ready' for name in ('broker', 'database')):
                report['reason'] = 'Start Docker/Kafka, initialize the SQL schema, and run the worker first.'
                return 2
            report['status'] = 'FAIL'
            event = {'event_id': str(uuid4()), 'occurred_at': datetime.now(timezone.utc).isoformat(),
                     'incident_id': 'INC-000123', 'synthetic': True}
            report['event_id'] = event['event_id']
            positions = []
            for delivery in range(2):
                sent = client.post('/lab/kafka/events', json=event)
                check(f'publication_{delivery + 1}_acknowledged', sent.status_code == 202)
                ack = sent.json()
                positions.append({key: ack[key] for key in ('topic', 'partition', 'offset')})
                report['positions'] = positions
                deadline = time.monotonic() + 30
                seen = []
                while time.monotonic() < deadline:
                    with database.connect('reader') as conn:
                        seen = conn.execute('SELECT topic, partition, offset_value, duplicate '
                                            'FROM kafka_lab.deliveries WHERE event_id = %s',
                                            (event['event_id'],)).fetchall()
                    if any(row['topic'] == ack['topic'] and row['partition'] == ack['partition']
                           and row['offset_value'] == ack['offset'] for row in seen):
                        break
                    time.sleep(.25)
                check(f'delivery_{delivery + 1}_persisted', len(seen) == delivery + 1)
            check('same_key_same_partition', positions[0]['partition'] == positions[1]['partition'])
            check('offset_increased', positions[1]['offset'] > positions[0]['offset'])
            check('duplicate_recorded', sum(row['duplicate'] for row in seen) == 1)
            result = client.get('/lab/kafka/results/' + event['event_id'])
            check('http_result', result.status_code == 200 and result.json()['status'] == 'processed')
            with database.connect('reader') as conn:
                count = conn.execute('SELECT count(*) AS n FROM kafka_lab.processed_events WHERE event_id = %s',
                                     (event['event_id'],)).fetchone()['n']
            check('exactly_one_stored_result', count == 1)
            report['status'] = 'PASS'
        return 0
    except Exception as exc:
        # Raw HTTP/driver exception strings may include private local settings.
        report['error_type'] = type(exc).__name__
        return 1
    finally:
        report['checked_at_utc'] = datetime.now(timezone.utc).isoformat()
        (output / 'result.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print(json.dumps({'status': report['status'], 'evidence': str(output / 'result.json')}, ensure_ascii=True))


if __name__ == '__main__':
    raise SystemExit(main())
