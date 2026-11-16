"""Helpers for the explicit, owned SQL/API/Kafka qualification profile.

This module never starts a stand on import. Live clients require the selected
QA owner and fixed loopback configuration. Evidence contains correlation data,
not credentials, raw HTTP responses or report contents.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.request import Request
from uuid import UUID, uuid4

from src.qa_profile import checked_path, get_qa_profile


TERMINAL = frozenset({'succeeded', 'refused', 'failed', 'cancelled', 'timed_out', 'rejected'})


def _owned(profile):
    profile.validate()
    current = get_qa_profile()
    if current is None or current != profile:
        raise ValueError('QA_PIPELINE_PROFILE_MISMATCH')
    if checked_path(profile.base / 'browser-profile.json', profile.root).exists():
        raise ValueError('QA_PIPELINE_REQUIRES_REAL_STAND')
    active = checked_path(profile.root / 'var/qa/active.json', profile.root)
    if json.loads(active.read_text(encoding='utf-8-sig')) != profile.owner_record():
        raise ValueError('QA_PIPELINE_OWNER_MISMATCH')
    return profile


def coordinates(value):
    """The broker ACK, inbox and DLQ all use this exact three-field identity."""
    return {field: value[field] for field in ('topic', 'partition', 'offset')}


class Evidence:
    def __init__(self, profile, case_id, output, secrets):
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', case_id) is None:
            raise ValueError('QA_PIPELINE_INVALID_CASE_ID')
        self.profile, self.case_id, self.output = profile, case_id, output
        self._secrets = secrets
        self.observations = []
        self.started_at = datetime.now(timezone.utc).isoformat()

    def record(self, **fields):
        # Reject common payload/credential keys even if a caller accidentally
        # passes a whole object. Records should be small explicit observations.
        def safe(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    if not isinstance(key, str) or re.search(
                            r'token|password|authorization|headers|payload|event_json|job_json|body', key, re.I):
                        raise ValueError('QA_PIPELINE_UNSAFE_EVIDENCE_FIELD')
                    safe(nested)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    safe(item)
        safe(fields)
        text = json.dumps(fields, ensure_ascii=False, allow_nan=False)
        if len(text.encode('utf-8')) > 65536 or any(secret in text for secret in self._secrets):
            raise ValueError('QA_PIPELINE_UNSAFE_EVIDENCE')
        self.observations.append(json.loads(text))

    def save(self, success, exception_type=None):
        value = {'schema_version': 1, 'case_id': self.case_id,
                 'run_id': self.profile.run_id, 'status': 'PASS' if success else 'FAIL',
                 'started_at_utc': self.started_at,
                 'finished_at_utc': datetime.now(timezone.utc).isoformat(),
                 'observations': self.observations}
        if exception_type:
            value['failure_type'] = exception_type  # Never an exception message/body.
        target = checked_path(self.output / (self.case_id + '.json'), self.profile.root)
        with target.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')


class Pipeline:
    """Real owned clients; lifecycle mutations affect only this run's worker."""
    def __init__(self, profile):
        self.profile = _owned(profile)
        from src.lab.config import load_lab_settings
        from src.lab.database import LabDatabase
        from src.lab.kafka.analysis_store import AnalysisStore
        from src.workflow.store import SQLiteStore
        settings = load_lab_settings()
        self._token = settings['api_token']
        self._secrets = [self._token, *[role['password'] for role in settings['roles'].values()]]
        self.database = LabDatabase(settings)
        self.store = AnalysisStore(self.database)
        if not checked_path(profile.workflow_path, profile.root).is_file():
            raise ValueError('QA_PIPELINE_WORKFLOW_MISSING')
        self.workflow = SQLiteStore(profile.workflow_path)
        target = os.environ.get('AML_QA_PIPELINE_OUTPUT')
        if not target:
            raise ValueError('QA_PIPELINE_OUTPUT_REQUIRED')
        self.output = checked_path(Path(target), profile.root)
        if (not self.output.is_dir() or not (
                self.output.is_relative_to(profile.root / 'verification')
                or self.output.is_relative_to(profile.base))):
            raise ValueError('QA_PIPELINE_INVALID_OUTPUT')
        health = self.request('GET', '/health')
        if health.get('qa_run_id') != profile.run_id or health.get('qa_owner') != profile.marker:
            raise ValueError('QA_PIPELINE_HTTP_OWNER_MISMATCH')

    def request(self, method, path, payload=None, expected=200):
        """Fixed host/port, no redirects/proxies, 15-second socket/deadline budget."""
        _owned(self.profile)
        if (method not in {'GET', 'POST', 'PUT', 'DELETE', 'PATCH'}
                or not isinstance(path, str) or not path.startswith('/')
                or path.startswith('//') or any(char in path for char in '\r\n\\#')
                or not path.isascii()):
            raise ValueError('QA_PIPELINE_INVALID_HTTP_TARGET')
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode('utf-8')
        if body is not None and len(body) > 65536:
            raise ValueError('QA_PIPELINE_REQUEST_TOO_LARGE')
        from src.runtime.io_budget import BudgetExpired, Deadline
        from src.workflow.bounded_http import build_deadline_opener
        deadline = Deadline.after(15)
        headers = {'Accept': 'application/json', 'X-Lab-Key': self._token}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        try:
            request = Request(self.profile.api_url + path, data=body, headers=headers, method=method)
            try:
                response = build_deadline_opener(deadline, 15).open(request, timeout=15)
            except HTTPError as exc:
                response = exc
            with response:
                if response.status != expected:
                    raise AssertionError('QA_PIPELINE_HTTP_STATUS:' + str(response.status) + ':expected:' + str(expected))
                raw = response.read(2097153)
                deadline.check()
                if len(raw) > 2097152:
                    raise ValueError('QA_PIPELINE_RESPONSE_TOO_LARGE')
            return json.loads(raw) if raw else None
        except AssertionError:
            raise
        except (BudgetExpired, OSError, http.client.HTTPException, ValueError, UnicodeError):
            raise RuntimeError('QA_PIPELINE_HTTP_FAILED') from None

    def event(self):
        from src.lab.kafka.analysis_contracts import AnalysisEvent
        from src.runtime.policy import load_demo_policy
        event = AnalysisEvent(event_id=uuid4(), occurred_at=datetime.now(timezone.utc),
                              incident_id='INC-000123', engine='template',
                              window=load_demo_policy(self.profile.policy_path).window)
        return event.model_dump(mode='json')

    def publish(self, event):
        ack = self.request('POST', '/lab/kafka/analyses', event, expected=202)
        assert ack['status'] == 'broker_acknowledged'
        assert ack['event_id'] == event['event_id']
        assert ack['processing'] == 'check_result_separately'
        self._assert_ack(ack)
        return ack

    def _assert_ack(self, ack):
        assert ack['topic'] == self.profile.analysis_topic
        assert type(ack['partition']) is int and 0 <= ack['partition'] < 3
        assert type(ack['offset']) is int and ack['offset'] >= 0

    def raw_publish(self, key, value):
        """Explicit malformed-input exercise, using only the owned AML topic."""
        _owned(self.profile)
        from src.lab.kafka import contracts, analysis_contracts
        from src.lab.kafka.broker import KafkaBroker
        if (contracts.BOOTSTRAP != f'127.0.0.1:{self.profile.kafka_port}'
                or analysis_contracts.TOPIC != self.profile.analysis_topic
                or analysis_contracts.DLQ_TOPIC != self.profile.analysis_dlq
                or analysis_contracts.GROUP != self.profile.analysis_group):
            raise ValueError('QA_PIPELINE_BROKER_TARGET_MISMATCH')
        broker = KafkaBroker(flow='aml')
        try:
            ack = broker.publish(self.profile.analysis_topic, key=key, value=value)
            self._assert_ack(ack)
            return ack
        finally:
            broker.close()

    def deliveries(self, event_id):
        identifier = UUID(str(event_id))
        with self.database.connect('reader') as connection:
            rows = connection.execute(
                'SELECT topic, partition, offset_value AS offset, event_id::text, '
                'canonical_sha256, duplicate FROM kafka_lab.analysis_deliveries '
                'WHERE event_id = %s ORDER BY topic, partition, offset_value', (identifier,)).fetchall()
        return [dict(row) for row in rows]

    def source_deliveries(self, ack):
        self._assert_ack(ack)
        with self.database.connect('reader') as connection:
            rows = connection.execute(
                'SELECT event_id::text FROM kafka_lab.analysis_deliveries '
                'WHERE topic = %s AND partition = %s AND offset_value = %s',
                (ack['topic'], ack['partition'], ack['offset'])).fetchall()
        return [row['event_id'] for row in rows]

    def request_ids(self):
        with self.database.connect('reader') as connection:
            rows = connection.execute('SELECT event_id::text FROM kafka_lab.analysis_requests').fetchall()
        return {row['event_id'] for row in rows}

    def job_ids(self):
        from src.workflow.auth import trusted_local_principal
        return {job['job_id'] for job in self.workflow.list_jobs(trusted_local_principal())}

    def wait_terminal(self, event_id, timeout=120):
        identifier = str(UUID(str(event_id)))
        deadline = time.monotonic() + min(float(timeout), 120)
        while time.monotonic() < deadline:
            row = self.store.get(identifier)
            if row is not None and row['status'] in TERMINAL:
                return row
            time.sleep(.25)
        raise AssertionError('QA_PIPELINE_TERMINAL_TIMEOUT:' + identifier)

    def wait_deliveries(self, event_id, count, timeout=45):
        deadline = time.monotonic() + min(float(timeout), 120)
        while time.monotonic() < deadline:
            rows = self.deliveries(event_id)
            if len(rows) >= count:
                assert len(rows) == count, 'QA_PIPELINE_EXTRA_DELIVERY'
                return rows
            time.sleep(.2)
        raise AssertionError('QA_PIPELINE_DELIVERY_TIMEOUT')

    def assert_consistent(self, event_id):
        """Compare actual PG projection, HTTP, SQLite and the stored report hash.

        Returns only the compact correlation/fingerprint data safe for evidence.
        A succeeded template result is required by the chain/fault scenarios.
        """
        from src.contracts.semantics import canonical_digest
        from src.lab.kafka.analysis_contracts import AnalysisEvent
        from src.workflow.auth import trusted_local_principal
        identifier = str(UUID(str(event_id)))
        projected = self.store.get(identifier)
        assert projected is not None and projected['status'] == 'succeeded'
        assert self.request('GET', '/lab/kafka/analyses/' + identifier) == projected
        job_id = projected['job_id']
        job = self.request('GET', '/v1/jobs/' + job_id)
        assert job == projected['job_json'] == self.workflow.get_job_internal(job_id)
        assert job['status'] == 'succeeded'
        event = AnalysisEvent.model_validate(projected['event_json'])
        assert self.workflow.get_request_internal(job_id) == event.job_request().model_dump(mode='json')
        assert self.workflow.get_engine_internal(job_id) == event.engine == 'template'
        report = job['result']
        report_id, version = report['report_id'], report['report_version']
        assert self.request('GET', f'/v1/reports/{report_id}?version={version}') == report
        principal = trusted_local_principal()
        assert self.workflow.get_report(report_id, version, principal) == report
        reference = self.request('GET', f'/v1/reports/{report_id}/reference?version={version}')
        assert reference == self.workflow.report_ref(report_id, version, principal).model_dump(mode='json')
        digest = canonical_digest(report)
        assert reference['content_sha256'] == projected['content_sha256'] == digest
        assert (projected['report_id'], projected['report_version']) == (report_id, version)
        with self.workflow.transaction() as connection:
            admissions = connection.execute(
                "SELECT response_json FROM idempotency WHERE operation='create_job' AND key=?",
                ('kafka-aml:' + identifier,)).fetchall()
            reports = connection.execute('SELECT report_id FROM reports WHERE job_id=?', (job_id,)).fetchall()
        assert len(admissions) == len(reports) == 1
        assert json.loads(admissions[0]['response_json'])['job_id'] == job_id
        event_digest = hashlib.sha256(event.canonical_bytes()).hexdigest()
        assert all(row['canonical_sha256'] == event_digest for row in self.deliveries(identifier))
        return {'event_id': identifier, 'job_id': job_id, 'status': job['status'],
                'job_revision': job['revision'], 'report_id': report_id, 'report_version': version,
                'content_sha256': digest, 'event_sha256': event_digest,
                'workflow_admissions': len(admissions), 'stored_reports': len(reports)}

    def stop_worker(self):
        _owned(self.profile)
        from qa.tools.stand import stop_service, service_ready
        stop_service(self.profile, 'worker')
        assert not service_ready(self.profile, 'worker'), 'QA_PIPELINE_WORKER_STILL_RUNNING'

    def start_worker(self):
        _owned(self.profile)
        from qa.tools.stand import start_service, service_ready
        start_service(self.profile, 'worker')
        assert service_ready(self.profile, 'worker'), 'QA_PIPELINE_WORKER_NOT_RUNNING'

    @contextmanager
    def evidence(self, case_id):
        evidence = Evidence(self.profile, case_id, self.output, self._secrets)
        try:
            yield evidence
        except BaseException as error:
            evidence.save(False, type(error).__name__)
            raise
        else:
            evidence.save(True)


class DLQProbe:
    """Read new owned DLQ records without joining/committing the worker group.

    Manual assignment starts at each partition's current high watermark, so a
    stale DLQ message from a preceding test cannot satisfy this test's oracle.
    """
    def __init__(self, profile):
        self.profile = _owned(profile)
        self.consumer = None
        self.records = []

    def __enter__(self):
        from confluent_kafka import Consumer, TopicPartition
        probe = os.environ.get('AML_QA_PROBE_ID', '')
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', probe) is None:
            raise ValueError('QA_PIPELINE_PROBE_ID_REQUIRED')
        logger = logging.getLogger('qa.pipeline.dlq')
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        self.consumer = Consumer({
            'bootstrap.servers': f'127.0.0.1:{self.profile.kafka_port}',
            'group.id': self.profile.compose_project + '-probe-' + probe + '-' + uuid4().hex[:12],
            'client.id': self.profile.compose_project + '-dlq-probe',
            'enable.auto.commit': False, 'enable.auto.offset.store': False,
            'allow.auto.create.topics': False, 'auto.offset.reset': 'error',
            'socket.timeout.ms': 5000, 'session.timeout.ms': 10000,
        }, logger=logger)
        try:
            metadata = self.consumer.list_topics(self.profile.analysis_dlq, timeout=5)
            if not metadata.brokers or any(
                    broker.host != '127.0.0.1' or broker.port != self.profile.kafka_port
                    for broker in metadata.brokers.values()):
                raise ValueError('QA_PIPELINE_DLQ_BROKER_MISMATCH')
            topic = metadata.topics.get(self.profile.analysis_dlq)
            if topic is None or topic.error is not None or set(topic.partitions) != {0, 1, 2}:
                raise ValueError('QA_PIPELINE_DLQ_NOT_READY')
            assignments = []
            for partition in sorted(topic.partitions):
                target = TopicPartition(self.profile.analysis_dlq, partition)
                _, high = self.consumer.get_watermark_offsets(target, timeout=5, cached=False)
                assignments.append(TopicPartition(self.profile.analysis_dlq, partition, high))
            self.consumer.assign(assignments)
            return self
        except BaseException:
            self.consumer.close()
            self.consumer = None
            raise

    def wait(self, source_ack, reason, timeout=45):
        if self.consumer is None or source_ack['topic'] != self.profile.analysis_topic:
            raise ValueError('QA_PIPELINE_DLQ_INVALID_SOURCE')
        source = coordinates(source_ack)
        deadline = time.monotonic() + min(float(timeout), 120)
        while time.monotonic() < deadline:
            message = self.consumer.poll(min(.5, max(.001, deadline - time.monotonic())))
            if message is None:
                continue
            if message.error() is not None:
                raise RuntimeError('QA_PIPELINE_DLQ_READ_FAILED')
            if message.topic() != self.profile.analysis_dlq:
                raise ValueError('QA_PIPELINE_DLQ_TOPIC_MISMATCH')
            raw = message.value()
            if not isinstance(raw, bytes) or len(raw) > 65536:
                raise ValueError('QA_PIPELINE_DLQ_INVALID_ENVELOPE')
            try:
                envelope = json.loads(raw)
                observed_source = coordinates(envelope['source'])
            except (TypeError, ValueError, KeyError):
                raise ValueError('QA_PIPELINE_DLQ_INVALID_ENVELOPE') from None
            self.records.append({'topic': message.topic(), 'partition': message.partition(),
                                 'offset': message.offset(), 'source': observed_source,
                                 'reason_code': envelope.get('reason_code')})
            if observed_source != source:
                continue
            assert envelope['reason_code'] == reason
            assert envelope['schema_version'] == 1 and envelope['synthetic'] is True
            assert message.key() == f"{source['topic']}:{source['partition']}:{source['offset']}".encode('ascii')
            return envelope
        raise AssertionError('QA_PIPELINE_DLQ_TIMEOUT:' + reason)

    def __exit__(self, *_args):
        if self.consumer is not None:
            self.consumer.close()  # Auto commit/store are disabled; no explicit commit.
            self.consumer = None
