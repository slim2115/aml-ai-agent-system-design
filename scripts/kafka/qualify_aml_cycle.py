"""Opt-in live Kafka -> AML -> SQL/shared-history qualification.

Start API, broker, AML tables and the external AML worker first. Creates one
synthetic request plus an exact duplicate and retains evidence. Never clears
data or starts/stops services. Search preparation requires --prepare-search.
This is a template-engine smoke test, not live fault or LLM qualification.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-search', action='store_true')
    args = parser.parse_args()
    import httpx
    from psycopg import sql
    from src.contracts.semantics import canonical_digest
    from src.lab.config import load_lab_settings
    from src.lab.database import LabDatabase

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = ROOT / 'var/checks/kafka-aml-cycle' / stamp
    output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'INCOMPLETE', 'checks': [], 'scope':
              'Live HTTP, Kafka, external AML worker, real retrieval and template engine, '
              'PostgreSQL projection, shared SQLite draft, duplicate. No LLM/live fault qualification.'}

    def check(name, passed):
        report['checks'].append({'name': name, 'passed': bool(passed)})
        if not passed:
            raise ValueError(name)

    def progress(stage):
        print(json.dumps({'stage': stage}), flush=True)

    def bookkeeping_digest(database):
        result = {}
        with database.connect('reader') as conn:
            for table in ('processed_events', 'deliveries'):
                rows = conn.execute(sql.SQL('SELECT to_jsonb(t) AS body FROM kafka_lab.{} t').format(
                    sql.Identifier(table))).fetchall()
                encoded = '\n'.join(sorted(json.dumps(row['body'], sort_keys=True) for row in rows))
                result[table] = hashlib.sha256(encoded.encode()).hexdigest()
        return result

    try:
        settings = load_lab_settings()
        database = LabDatabase(settings)
        before = bookkeeping_digest(database)
        with httpx.Client(base_url='http://127.0.0.1:8765', timeout=15, trust_env=False,
                          headers={'X-Lab-Key': settings['api_token']}) as client:
            response = client.get('/lab/kafka/analyses/status')
            check('components_endpoint', response.status_code == 200)
            components = response.json()
            report['components'] = components
            check('broker_and_inbox_ready', all(components[name]['status'] == 'ready'
                                               for name in ('broker', 'database')))
            ready = client.get('/v1/readiness')
            check('readiness_endpoint', ready.status_code == 200)
            if not ready.json()['ready']:
                if not args.prepare_search:
                    report['reason'] = 'Prepare search explicitly in integrated UI or pass --prepare-search.'
                    return 2
                progress('preparing_search')
                prepared = client.post('/v1/readiness')
                check('search_preparation_accepted', prepared.status_code == 202)
                deadline = time.monotonic() + 190
                while time.monotonic() < deadline:
                    ready = client.get('/v1/readiness')
                    check('preparation_poll_http', ready.status_code == 200)
                    if ready.json()['state'] != 'preparing':
                        break
                    time.sleep(1)
            report['readiness'] = ready.json()
            check('real_search_ready', ready.json()['ready'])
            report['status'] = 'FAIL'
            event = {'schema_version': 1, 'event_type': 'aml.analysis.requested.v1',
                     'event_id': str(uuid4()), 'occurred_at': datetime.now(timezone.utc).isoformat(),
                     'incident_id': 'INC-000123', 'synthetic': True, 'engine': 'template',
                     'window': json.loads((ROOT / 'config/demo-policy.json').read_text(encoding='utf-8'))['window']}
            report['event_id'] = event['event_id']
            (output / 'request.json').write_text(json.dumps(event, indent=2) + '\n', encoding='utf-8')
            positions, seen_states = [], []
            first_job = None
            for delivery in range(2):
                progress('publishing' if delivery == 0 else 'publishing_exact_duplicate')
                sent = client.post('/lab/kafka/analyses', json=event)
                check(f'publication_{delivery + 1}_acknowledged', sent.status_code == 202)
                ack = sent.json()
                positions.append({key: ack[key] for key in ('topic', 'partition', 'offset')})
                report['positions'] = positions
                deadline = time.monotonic() + (200 if delivery == 0 else 35)
                projected, deliveries = None, []
                while time.monotonic() < deadline:
                    result = client.get('/lab/kafka/analyses/' + event['event_id'])
                    check('projection_poll_http', result.status_code in (200, 404))
                    if result.status_code == 200:
                        projected = result.json()
                        state = projected['status']
                        if not seen_states or seen_states[-1] != state:
                            seen_states.append(state)
                            progress(state)
                    with database.connect('reader') as conn:
                        deliveries = conn.execute('SELECT topic, partition, offset_value, duplicate '
                            'FROM kafka_lab.analysis_deliveries WHERE event_id = %s', (event['event_id'],)).fetchall()
                    persisted = any(row['topic'] == ack['topic'] and row['partition'] == ack['partition']
                                    and row['offset_value'] == ack['offset'] for row in deliveries)
                    if projected and projected['status'] not in ('queued', 'submitted') and persisted:
                        break
                    time.sleep(.5)
                report['observed_states'] = seen_states
                check(f'delivery_{delivery + 1}_persisted', len(deliveries) == delivery + 1 and persisted)
                check(f'delivery_{delivery + 1}_report_succeeded', projected and projected['status'] == 'succeeded')
                if first_job is None:
                    first_job = projected['job_id']
                check('same_job_for_duplicate', projected['job_id'] == first_job)
            check('same_partition_increasing_offsets', positions[0]['partition'] == positions[1]['partition']
                  and positions[1]['offset'] > positions[0]['offset'])
            check('duplicate_marked', sum(row['duplicate'] for row in deliveries) == 1)
            report['job_id'] = first_job
            job_response = client.get('/v1/jobs/' + first_job)
            check('authoritative_job_http', job_response.status_code == 200)
            job = job_response.json()
            check('same_job_in_sql_and_api', projected['job_json'] == job)
            check('draft_requires_human_review', job['status'] == 'succeeded' and job['result']['status'] == 'draft')
            check('projected_hash_matches', projected['content_sha256'] == canonical_digest(job['result']))
            check('projected_report_identity_matches', projected['report_id'] == job['result']['report_id']
                  and projected['report_version'] == job['result']['report_version'])
            with sqlite3.connect((ROOT / 'var/workflow/api-workflow.sqlite3').as_uri() + '?mode=ro', uri=True) as conn:
                row = conn.execute('SELECT payload_json FROM jobs WHERE job_id=?', (first_job,)).fetchone()
                check('shared_ui_history_matches', row is not None and json.loads(row[0]) == job)
                row = conn.execute('SELECT content_sha256 FROM reports WHERE job_id=?', (first_job,)).fetchone()
                check('shared_ui_report_hash_matches', row is not None and row[0] == projected['content_sha256'])
            with database.connect('reader') as conn:
                count = conn.execute('SELECT count(*) AS n FROM kafka_lab.analysis_requests WHERE event_id=%s',
                                     (event['event_id'],)).fetchone()['n']
            check('exactly_one_inbox_result', count == 1)
            check('bookkeeping_data_unchanged', bookkeeping_digest(database) == before)
            report['report_ref'] = {key: projected[key] for key in ('report_id', 'report_version', 'content_sha256')}
            report['status'] = 'PASS'
        return 0
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        if report['status'] == 'INCOMPLETE':
            report['reason'] = 'Prerequisites or live connection unavailable; inspect failed check and service logs.'
        return 1
    finally:
        report['checked_at_utc'] = datetime.now(timezone.utc).isoformat()
        (output / 'result.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print(json.dumps({'status': report['status'], 'evidence': str(output / 'result.json')}, ensure_ascii=True))


if __name__ == '__main__':
    raise SystemExit(main())
