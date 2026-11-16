"""One explicit live environment proof; no model preparation or operator decision."""
from datetime import datetime, timezone
import json
import os
import socket
import time
from uuid import uuid4

import pytest

from qa.tools.environment import local_http, write_json


@pytest.mark.live
def test_owned_api_kafka_sql_cycle_and_network_boundary(qa_profile):
    from src.lab.config import load_lab_settings
    from src.lab.database import LabDatabase
    from src.workflow.store import SQLiteStore, WorkflowError
    from src.workflow.auth import trusted_local_principal
    profile = qa_profile
    token = load_lab_settings()['api_token']
    database = LabDatabase()
    checks = []

    def request(path, **kwargs):
        return json.loads(local_http(profile, profile.api_port, path, token=token, **kwargs))

    health = request('/health')
    assert health['qa_run_id'] == profile.run_id and health['qa_owner'] == profile.marker
    assert profile.run_id in request('/openapi.json')['info']['title']
    assert b'swagger-ui' in local_http(profile, profile.api_port, '/docs')
    assert local_http(profile, profile.ui_port, '/_stcore/health') == b'ok'
    checks.append('owned API, Swagger and UI HTTP ready')
    for target in [('127.0.0.1', 8765), ('127.0.0.1', 55432), ('127.0.0.1', 9092), ('example.com', 443)]:
        with pytest.raises(RuntimeError, match='QA_NETWORK_TARGET_FORBIDDEN'):
            socket.create_connection(target, timeout=.1)
    checks.append('Python sockets reject ordinary stand and external targets')

    with pytest.raises(WorkflowError, match='INVALID_INPUT_FORMAT'):
        SQLiteStore(profile.root / 'var/workflow/api-workflow.sqlite3')
    with database.connect('reader') as connection:
        row = connection.execute('SELECT current_database() AS db, current_user AS role').fetchone()
        assert row['db'] == profile.db_name and row['role'] == profile.role_names['reader']
    note = request('/lab/notes', method='POST', payload={
        'incident_id': 'INC-000123', 'body': 'QA isolation proof ' + profile.run_id})
    with database.connect('reader') as connection:
        saved = connection.execute('SELECT body FROM practice.case_notes WHERE id=%s', (note['id'],)).fetchone()
        assert saved['body'] == note['body']
    checks.append('API write is present only in the validated QA database')

    event = {'event_id': str(uuid4()), 'occurred_at': datetime.now(timezone.utc).isoformat(),
             'incident_id': 'INC-000123', 'synthetic': True}
    positions = []
    for attempt in range(2):
        ack = request('/lab/kafka/events', method='POST', payload=event)
        assert ack['topic'] == profile.topic
        positions.append(ack)
        deadline = time.monotonic() + 30
        while True:
            with database.connect('reader') as connection:
                deliveries = connection.execute('SELECT topic, partition, offset_value, duplicate '
                    'FROM kafka_lab.deliveries WHERE event_id=%s', (event['event_id'],)).fetchall()
            if len(deliveries) == attempt + 1:
                break
            assert time.monotonic() < deadline, 'QA Kafka delivery did not reach QA PostgreSQL'
            time.sleep(.2)
    assert all(row['topic'] == profile.topic for row in deliveries)
    assert sum(row['duplicate'] for row in deliveries) == 1
    assert positions[0]['partition'] == positions[1]['partition']
    assert positions[1]['offset'] > positions[0]['offset']
    assert request('/lab/kafka/results/' + event['event_id'])['status'] == 'processed'
    with database.connect('reader') as connection:
        assert connection.execute('SELECT count(*) AS n FROM kafka_lab.processed_events WHERE event_id=%s',
                                  (event['event_id'],)).fetchone()['n'] == 1
    checks.append('real API -> QA Kafka -> review worker -> QA PostgreSQL, duplicate without double effect')

    # Querying readiness creates only the owned history/controller, not models.
    readiness = request('/v1/readiness')
    assert readiness['ready'] is False
    assert profile.workflow_path.is_file()
    SQLiteStore(profile.workflow_path).list_jobs(trusted_local_principal())
    checks.append('owned SQLite history; models remain unprepared')
    result = {'status': 'PASS', 'run_id': profile.run_id, 'checks': checks, 'event_id': event['event_id'],
              'probe_id': os.environ['AML_QA_PROBE_ID'],
              'note_id': note['id'], 'positions': positions, 'models_started': False,
              'scope': 'Real HTTP readiness and bookkeeping Kafka/SQL cycle. No browser E2E or AML model result.'}
    write_json(profile, profile.base / 'last-qualification.json', result)
