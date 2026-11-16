"""Owned live dependency outages and explicit ACK-boundary fault injection.

No process killing, offset reset, ordinary database or broker is used. Tests
restore the dependencies they stop, even when an assertion fails.
"""
from contextlib import closing
import json
import logging
import time

import pytest

from qa.pipeline.support import Pipeline
from qa.tools import broker as broker_lifecycle, stand
from qa.tools.environment import read_state
from src.lab.kafka.analysis_consumer import process_one, scheduler_tick
from src.lab.kafka.broker import KafkaBroker
from src.lab.kafka.consumer import WorkerFailure
from src.runtime.io_budget import BudgetExpired, Deadline, use_budget
from src.workflow.remote import WorkflowClient


def wait_for(predicate, *, timeout=45, message='QA observation timed out'):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, message
        time.sleep(.2)


def ensure_search(pipeline):
    current = pipeline.request('GET', '/v1/readiness')
    if not current['ready'] and current['state'] != 'preparing':
        pipeline.request('POST', '/v1/readiness', expected=202)
    def prepared():
        state = pipeline.request('GET', '/v1/readiness')
        assert state['state'] in ('preparing', 'ready', 'busy'), 'Real QA search preparation failed'
        return state['ready'] or state['state'] == 'busy'
    wait_for(prepared, timeout=180, message='Real QA search did not become ready')


def take_record(broker, ack):
    record = wait_for(lambda: broker.poll(timeout=.5), timeout=45, message='Target Kafka record not received')
    assert (record.topic, record.partition, record.offset) == (ack['topic'], ack['partition'], ack['offset'])
    return record


def committed(profile, ack):
    from confluent_kafka import Consumer, TopicPartition
    logger = logging.getLogger('qa.pipeline.offsets')
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    with closing(Consumer({'bootstrap.servers': f'127.0.0.1:{profile.kafka_port}',
        'group.id': profile.analysis_group, 'enable.auto.commit': False,
        'enable.auto.offset.store': False, 'allow.auto.create.topics': False,
        'socket.timeout.ms': 3000}, logger=logger)) as observer:
        # Metadata lookup only: no subscribe, poll, assignment or commit.
        row = observer.committed([TopicPartition(ack['topic'], ack['partition'])], timeout=5)[0]
        assert row.error is None
        return row.offset


def intake_without_scheduler(pipeline, ack):
    with closing(KafkaBroker(worker=True, flow='aml')) as broker:
        record = take_record(broker, ack)
        outcome = process_one(record, pipeline.store, broker)
        assert outcome['status'] == 'queued'


def restart_worker(profile):
    # A failed/stale process must be observed and stopped before a fresh instance.
    stand.stop_service(profile, 'worker')
    stand.start_service(profile, 'worker')


@pytest.mark.live
def test_api_outage_retains_queue_then_uses_same_event(qa_profile):
    p = Pipeline(qa_profile)
    with p.evidence('PF-01-api-outage') as evidence:
        event = p.event()
        stand.stop_service(qa_profile, 'worker')
        try:
            ack = p.publish(event)
            intake_without_scheduler(p, ack)
            stand.stop_service(qa_profile, 'api')
            stand.start_service(qa_profile, 'worker')
            def deferred():
                row = p.store.get(event['event_id'])
                return row if row and row['last_error_code'] == 'NOT_READY' else None
            row = wait_for(deferred, message='API outage not observed in durable queue')
            assert row['status'] == 'queued' and row['job_id'] is None
            evidence.record(event_id=event['event_id'], ack=ack,
                            outage_status=row['status'], outage_code=row['last_error_code'])
        finally:
            stand.start_service(qa_profile, 'api')
            ensure_search(p)
            if not stand.service_ready(qa_profile, 'worker'):
                restart_worker(qa_profile)
        result = p.wait_terminal(event['event_id'])
        p.assert_consistent(event['event_id'])
        assert len(p.deliveries(event['event_id'])) == 1
        evidence.record(recovered_job_id=result['job_id'], report_sha256=result['content_sha256'])


@pytest.mark.live
def test_postgresql_outage_does_not_ack_unstored_event(qa_profile):
    p = Pipeline(qa_profile)
    with p.evidence('PF-02-postgresql-outage') as evidence:
        event = p.event()
        stand.stop_service(qa_profile, 'worker')
        try:
            # pg_running validates the cluster owner, absolute data directory and port.
            assert stand.pg_running(qa_profile)
            stand.command(qa_profile, [stand.PG_BIN / 'pg_ctl.exe', '-D', qa_profile.base / 'postgres/cluster',
                '-m', 'fast', '-w', '-t', '30', 'stop'], 'fault-postgres-stop')
            ack = p.publish(event)
            before = committed(qa_profile, ack)
            with closing(KafkaBroker(worker=True, flow='aml')) as broker:
                record = take_record(broker, ack)
                with pytest.raises(WorkerFailure, match='DATABASE_UNAVAILABLE'):
                    process_one(record, p.store, broker)
            after = committed(qa_profile, ack)
            assert after == before and after <= ack['offset']
            evidence.record(event_id=event['event_id'], ack=ack,
                failure='DATABASE_UNAVAILABLE', committed_before=before, committed_after=after,
                boundary='Real broker/PG outage; process_one called in the test process, no scheduler.')
        finally:
            stand.start_postgres(qa_profile)
            restart_worker(qa_profile)
            if not stand.service_ready(qa_profile, 'review-worker'):
                stand.stop_service(qa_profile, 'review-worker')
                stand.start_service(qa_profile, 'review-worker')
        result = p.wait_terminal(event['event_id'])
        p.assert_consistent(event['event_id'])
        rows = p.deliveries(event['event_id'])
        assert len(rows) == 1 and rows[0]['offset'] == ack['offset']
        evidence.record(recovered_job_id=result['job_id'], report_sha256=result['content_sha256'])


@pytest.mark.live
def test_broker_outage_retries_original_payload_without_double_effect(qa_profile):
    p = Pipeline(qa_profile)
    with p.evidence('PF-03-broker-outage') as evidence:
        event = p.event()
        try:
            broker_lifecycle.stop(qa_profile)
            rejected = p.request('POST', '/lab/kafka/analyses', payload=event, expected=503)
            assert rejected['publication'] in ('unknown', 'not_attempted')
            assert rejected['code'].startswith('KAFKA_')
            evidence.record(event_id=event['event_id'], outage_code=rejected['code'],
                            publication=rejected['publication'])
        finally:
            broker_lifecycle.start(qa_profile)
            restart_worker(qa_profile)
            stand.stop_service(qa_profile, 'review-worker')
            stand.start_service(qa_profile, 'review-worker')
        ack = p.publish(event)
        result = p.wait_terminal(event['event_id'])
        p.assert_consistent(event['event_id'])
        evidence.record(retry_ack=ack, recovered_job_id=result['job_id'],
            report_sha256=result['content_sha256'], deliveries=p.deliveries(event['event_id']))


class DropJobResponseOnce:
    """Real HTTP request; discard one accepted response on the client boundary."""
    def __init__(self):
        self.dropped = False
        self.keys = []

    def open_budgeted(self, request, *, timeout, deadline):
        from src.workflow.bounded_http import build_deadline_opener
        response = build_deadline_opener(deadline, timeout).open(request, timeout=timeout)
        if request.method == 'POST' and '/v1/jobs?' in request.full_url:
            self.keys.append(request.get_header('Idempotency-key'))
            if not self.dropped and response.status == 202:
                self.dropped = True
                response.close()
                raise OSError('QA_LOST_ACCEPTED_RESPONSE')
        return response


@pytest.mark.live
def test_lost_http_acceptance_response_reuses_same_job(qa_profile):
    p = Pipeline(qa_profile)
    with p.evidence('PF-04-http-ack-lost') as evidence:
        event = p.event()
        stand.stop_service(qa_profile, 'worker')
        try:
            before = {row['job_id'] for row in p.request('GET', '/v1/jobs')}
            ack = p.publish(event)
            intake_without_scheduler(p, ack)
            transport = DropJobResponseOnce()
            client = WorkflowClient(timeout=5, transport=transport)
            progress = scheduler_tick(p.store, client)
            assert transport.dropped and progress == [dict(status='deferred', event_id=event['event_id'], code='NOT_READY')]
            pending = p.store.get(event['event_id'])
            assert pending['status'] == 'queued' and pending['job_id'] is None
            accepted = {row['job_id'] for row in p.request('GET', '/v1/jobs')} - before
            assert len(accepted) == 1
            scheduler_tick(p.store, client)
            projected = p.store.get(event['event_id'])
            assert projected['job_id'] in accepted
            assert transport.keys == ['kafka-aml:' + event['event_id']] * 2
            evidence.record(event_id=event['event_id'], ack=ack, accepted_job_ids=sorted(accepted),
                attempts=2, same_idempotency_key=True,
                boundary='Response discarded after actual HTTP 202; no physical network interruption claimed.')
        finally:
            restart_worker(qa_profile)
        result = p.wait_terminal(event['event_id'])
        p.assert_consistent(event['event_id'])
        assert result['job_id'] in accepted
        assert {row['job_id'] for row in p.request('GET', '/v1/jobs')} - before == accepted
        evidence.record(recovered_job_id=result['job_id'], report_sha256=result['content_sha256'])


@pytest.mark.live
@pytest.mark.parametrize('after_commit', [False, True], ids=['before-commit', 'after-commit'])
def test_lost_kafka_commit_ack_preserves_intake_and_recovers(qa_profile, after_commit):
    p = Pipeline(qa_profile)
    with p.evidence('PF-05-commit-' + ('after' if after_commit else 'before')) as evidence:
        event = p.event()
        stand.stop_service(qa_profile, 'worker')
        log_path = qa_profile.base / 'logs/worker.log'
        log_start = log_path.stat().st_size
        try:
            ack = p.publish(event)
            before = committed(qa_profile, ack)
            with closing(KafkaBroker(worker=True, flow='aml')) as broker:
                record = take_record(broker, ack)
                class CommitFault:
                    def commit(self, value):
                        if after_commit:
                            broker.commit(value)
                        raise OSError('QA_COMMIT_ACK_LOST')
                with pytest.raises(WorkerFailure, match='OFFSET_COMMIT_FAILED'):
                    process_one(record, p.store, CommitFault())
            after = committed(qa_profile, ack)
            assert (after == ack['offset'] + 1) if after_commit else (after == before and after <= ack['offset'])
            row = p.store.get(event['event_id'])
            assert row['status'] == 'queued' and row['job_id'] is None
            assert len(p.deliveries(event['event_id'])) == 1
            evidence.record(event_id=event['event_id'], ack=ack, committed_before=before,
                committed_after=after, fault_boundary='after_actual_commit' if after_commit else 'before_actual_commit')
        finally:
            restart_worker(qa_profile)
        result = p.wait_terminal(event['event_id'])
        p.assert_consistent(event['event_id'])
        assert len(p.deliveries(event['event_id'])) == 1
        if not after_commit:
            def replay_observed():
                with log_path.open('rb') as stream:
                    stream.seek(log_start)
                    lines = stream.read().decode('utf-8', errors='replace').splitlines()
                for line in lines:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if (item.get('status') == 'duplicate' and item.get('event_id') == event['event_id']
                            and item.get('offset') == ack['offset']):
                        return item
            evidence.record(actual_redelivery=wait_for(replay_observed, message='Same-position redelivery not observed'))
        evidence.record(recovered_job_id=result['job_id'], report_sha256=result['content_sha256'])


@pytest.mark.live
def test_live_postgresql_query_respects_shared_budget(qa_profile):
    p = Pipeline(qa_profile)
    with p.evidence('PF-06-live-sql-budget') as evidence:
        started = time.monotonic()
        with pytest.raises(BudgetExpired):
            with use_budget(Deadline.after(2.2)):
                with p.database.connect('api') as connection:
                    connection.execute('SELECT pg_catalog.pg_sleep(10)')
        elapsed = time.monotonic() - started
        assert connection.closed, 'Expired PostgreSQL connection was not closed'
        assert 1.5 <= elapsed < 4, 'Shared SQL budget did not bound the caller wait'
        with p.database.connect('reader') as connection:
            assert connection.execute('SELECT 1 AS value').fetchone()['value'] == 1
        evidence.record(budget_seconds=2.2, simulated_sql_seconds=10, elapsed_seconds=elapsed,
            outcome='BudgetExpired', scope='Caller wait and connection close; not a guarantee of immediate server-side cancellation.')
