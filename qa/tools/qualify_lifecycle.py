"""Explicit live restart/preservation proof on one already-qualified QA run."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.qa_profile import QAProfile, checked_path
from qa.tools.environment import local_http, port_free, read_state
from qa.tools import stand


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    profile = QAProfile(args.run_id).validate()
    os.environ['AML_QA_RUN_ID'] = profile.run_id
    output = checked_path(ROOT / args.output_dir)
    if not output.is_relative_to(ROOT / 'verification') or output.exists():
        raise ValueError('USE_NEW_VERIFICATION_DIRECTORY')
    output.mkdir(parents=True)
    from src.lab.config import load_lab_settings
    from src.lab.database import LabDatabase
    token = load_lab_settings()['api_token']
    fixture = json.loads((profile.base / 'last-qualification.json').read_text(encoding='utf-8'))
    checks = []
    result = {'run_id': profile.run_id, 'status': 'FAIL', 'checks': checks,
              'scope': 'Owned lifecycle, same QA records after restart; no models or browser E2E.'}

    def require(name, condition):
        checks.append({'name': name, 'passed': bool(condition)})
        if not condition:
            raise RuntimeError(name)

    def counts():
        with LabDatabase().connect('reader') as conn:
            return {
                'notes': conn.execute('SELECT count(*) AS n FROM practice.case_notes WHERE id=%s',
                                      (fixture['note_id'],)).fetchone()['n'],
                'results': conn.execute('SELECT count(*) AS n FROM kafka_lab.processed_events WHERE event_id=%s',
                                        (fixture['event_id'],)).fetchone()['n'],
                'deliveries': conn.execute('SELECT count(*) AS n FROM kafka_lab.deliveries WHERE event_id=%s',
                                           (fixture['event_id'],)).fetchone()['n'],
            }

    try:
        expected = counts()
        require('first QA records exist', expected == {'notes': 1, 'results': 1, 'deliveries': 2})
        names = ('api', 'ui', 'worker', 'review-worker')
        original = {name: read_state(profile, name)['instance_id'] for name in names}
        stand.start(profile)
        require('repeated start keeps the same owned processes',
                original == {name: read_state(profile, name)['instance_id'] for name in names})
        require('repeated start retains records', counts() == expected)
        result['first_stop'] = stand.stop(profile)
        require('owned ports released', all(port_free(port) for port in
                (profile.api_port, profile.ui_port, profile.pg_port, profile.kafka_port)))
        require('SQLite and PostgreSQL data retained', profile.workflow_path.is_file()
                and (profile.base / 'postgres/cluster/PG_VERSION').is_file())
        result['restart'] = stand.start(profile)
        require('new process instances after restart', all(read_state(profile, name)['instance_id']
                                                        != original[name] for name in names))
        require('same notes, Kafka result and deliveries retained', counts() == expected)
        note = json.loads(local_http(profile, profile.api_port, '/lab/notes/' + fixture['note_id'], token=token))
        event = json.loads(local_http(profile, profile.api_port, '/lab/kafka/results/' + fixture['event_id'], token=token))
        require('same records readable through restarted API', note['id'] == fixture['note_id']
                and event['event_id'] == fixture['event_id'])
        result['status'] = 'PASS'
    except Exception as exc:
        result['error_type'] = type(exc).__name__
        raise
    finally:
        try:
            result['final_stop'] = stand.stop(profile)
            stopped = all(port_free(port) for port in (profile.api_port, profile.ui_port, profile.pg_port, profile.kafka_port))
            checks.append({'name': 'final owned ports released', 'passed': stopped})
            if not stopped:
                result['status'] = 'FAIL'
        except Exception as exc:
            result.update(status='FAIL', cleanup_error_type=type(exc).__name__)
        result['completed_at_utc'] = datetime.now(timezone.utc).isoformat()
        (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': result['status'], 'run_id': profile.run_id,
                          'checks': checks, 'evidence': str(output / 'result.json')}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
