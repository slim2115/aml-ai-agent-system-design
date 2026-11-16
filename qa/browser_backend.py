"""Opt-in browser test backend: real HTTP/controller/SQLite, explicit model doubles.

This module is never imported by the ordinary application launcher. It cannot
connect to PostgreSQL, Kafka or a model. Kafka rows here exercise UI navigation
only; they are not evidence of message delivery or model quality.
"""
from contextlib import asynccontextmanager
from hashlib import sha256
import hmac
import json
import threading
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, Header, Request
from pydantic import StrictBool

from qa.scenario_checks import _RecordedIndex, _Transport
from qa.tools.environment import write_json
from src.config import get_settings
from src.contracts.semantics import canonical_digest
from src.contracts.v1 import Identifier, StrictModel
from src.lab.api import create_app
from src.lab.config import load_lab_settings
from src.lab.database import LabDatabaseError
from src.lab.kafka.analysis_contracts import AnalysisEvent
from src.lab.kafka.analysis_store import _result
from src.lab.kafka.broker import BrokerUnavailable
from src.qa_profile import QAProfile, checked_path, get_qa_profile
from src.runtime.context import ContextBuilder
from src.runtime.policy import load_demo_policy
from src.runtime.service import AMLAgent
from src.workflow.auth import trusted_local_principal
from src.workflow.controller import WorkflowService
from src.workflow.store import SQLiteStore, WorkflowError
from src.workflow.worker import ExecutionOutcome


class BrowserConfig(StrictModel):
    prepare_failure: StrictBool = False
    execution: Literal['normal', 'hold', 'deadline', 'corrupt_total'] = 'normal'


class KafkaLink(StrictModel):
    job_id: Identifier


def validate_browser_profile(profile):
    """A valid QA owner alone must never enable browser fault controls."""
    if not isinstance(profile, QAProfile):
        raise ValueError('BROWSER_QA_PROFILE_REQUIRED')
    profile.validate()
    if get_qa_profile() != profile:
        raise ValueError('BROWSER_QA_PROFILE_MISMATCH')
    path = checked_path(profile.base / 'browser-profile.json', profile.root)
    try:
        expected = {**profile.owner_record(), 'mode': 'deterministic'}
        if path.stat().st_size > 4096 or json.loads(path.read_bytes()) != expected:
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise ValueError('BROWSER_QA_PROFILE_REQUIRED') from None
    for source in (profile.policy_path, *profile.seed_dir.glob('*.json')):
        checked_path(source, profile.root)
    return profile


class BrowserWorker:
    """Only execution is doubled; the durable workflow controller remains real."""
    supports_runtime_events = True

    def __init__(self, profile):
        self.profile = profile
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._ready = False
        self.config = BrowserConfig()

    @property
    def is_ready(self):
        with self._lock:
            return self._ready and not self._closed.is_set()

    def configure(self, config):
        with self._lock:
            self.config = config
            self._ready = False

    def start(self, *, timeout_seconds=180):
        with self._lock:
            self._ready = not self.config.prepare_failure and not self._closed.is_set()
            return {'ready': self._ready, 'code': 'READY' if self._ready else 'NOT_READY'}

    def execute(self, incident_id, *, engine='template', timeout_seconds=180,
                cancel_event=None, event_sink=None):
        with self._lock:
            mode = self.config.execution
            if not self.is_ready:
                return ExecutionOutcome('failed', code='NOT_READY')
        if mode in ('hold', 'deadline'):
            # The real controller owns the deadline, persistence and cancel signal.
            while not self._closed.is_set():
                if cancel_event.wait(.02):
                    break
            with self._lock:
                self._ready = False
            return ExecutionOutcome('cancelled')
        selected = []
        sequence = 0

        def emit(event):
            nonlocal sequence
            sequence += 1
            if event_sink is not None:
                event_sink(sequence, event)

        policy = load_demo_policy(self.profile.policy_path)
        agent = AMLAgent(mode='demo', engine=engine, settings=get_settings(), policy=policy,
            builder=ContextBuilder(self.profile.seed_dir, policy),
            index_factory=lambda path: _RecordedIndex(path, selected),
            transport=_Transport('valid'), event_sink=emit)
        result = agent.run(incident_id)
        if mode == 'corrupt_total' and result['status'] == 'draft':
            for fact in result['found_facts']:
                if any(evidence['kind'] == 'sum' for evidence in fact['evidence']):
                    # A deliberately inconsistent display string for red/green
                    # sensitivity checks. Evidence retains the correct sum.
                    fact['fact'] = fact['fact'].rsplit(': ', 1)[0] + ': 999999.00.'
                    break
        return ExecutionOutcome('result', result=result)

    def close(self):
        self._closed.set()
        with self._lock:
            self._ready = False


class BrowserWorkflow(WorkflowService):
    def __init__(self, *args, **kwargs):
        self._browser_threads = set()
        self._browser_threads_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def _execute(self, control, engine):
        thread = threading.current_thread()
        with self._browser_threads_lock:
            self._browser_threads.add(thread)
        try:
            super()._execute(control, engine)
        finally:
            # Browser fixtures are frequently destroyed between tests. Wait for
            # the real durable-state observer to release its last SQLite handle.
            control['watch_stop'].set()
            control['watcher'].join(timeout=3)
            with self._browser_threads_lock:
                self._browser_threads.discard(thread)

    def close(self):
        super().close()
        with self._browser_threads_lock:
            threads = list(self._browser_threads)
        for thread in threads:
            thread.join(timeout=3)

    def submit(self, request, principal, idempotency_key, *, engine='template', timeout_seconds=None):
        # Only the browser fixture profile receives a short accepted-job budget.
        # No product setting or module global is patched.
        budget = 2 if self.worker.config.execution == 'deadline' else timeout_seconds
        return super().submit(request, principal, idempotency_key,
                              engine=engine, timeout_seconds=budget)


class _NoDatabase:
    def connect(self, *args, **kwargs):
        raise LabDatabaseError('UNAVAILABLE')


class _NoBroker:
    def status(self):
        raise BrokerUnavailable('BROWSER_FIXTURE_NO_BROKER')

    def publish(self, *args, **kwargs):
        raise BrokerUnavailable('BROWSER_FIXTURE_NO_BROKER', publication='not_attempted')


class _NoReviewStore:
    def get(self, event_id):
        return None

    def list_results(self, limit=20):
        return []

    def status(self):
        raise LabDatabaseError('UNAVAILABLE')


class BrowserAnalysisLinks:
    """Owned fixture links; reports are always resolved from the real SQLite store."""
    def __init__(self, profile, store):
        self.profile, self.store = profile, store
        self.path = checked_path(profile.base / 'browser-kafka-links.json', profile.root)
        self.principal = trusted_local_principal()
        self._lock = threading.RLock()

    def _read(self):
        checked_path(self.path, self.profile.root)
        if not self.path.exists():
            return []
        try:
            if self.path.stat().st_size > 1024 * 1024:
                raise ValueError
            value = json.loads(self.path.read_bytes())
            if (set(value) != {*self.profile.owner_record(), 'links'}
                    or any(value[key] != expected for key, expected in self.profile.owner_record().items())
                    or not isinstance(value['links'], list) or len(value['links']) > 1000):
                raise ValueError
            seen = set()
            for link in value['links']:
                if (not isinstance(link, dict) or set(link) != {'event_id', 'job_id'}
                        or not isinstance(link['event_id'], str) or not isinstance(link['job_id'], str)
                        or str(UUID(link['event_id'])) != link['event_id']
                        or link['event_id'] in seen):
                    raise ValueError
                seen.add(link['event_id'])
                self.store.get_job(link['job_id'], self.principal)
            return value['links']
        except (ValueError, KeyError, TypeError, OSError):
            raise WorkflowError('NOT_READY') from None

    def _row(self, link):
        job = self.store.get_job(link['job_id'], self.principal)
        policy = load_demo_policy(self.profile.policy_path)
        event = AnalysisEvent(event_id=link['event_id'], occurred_at=job['accepted_at'],
            incident_id=job['incident_id'], engine=self.store.get_engine_internal(job['job_id']), window=policy.window)
        succeeded = job['status'] == 'succeeded'
        row = {**link, 'incident_id': job['incident_id'], 'engine': event.engine,
            'canonical_sha256': sha256(event.canonical_bytes()).hexdigest(),
            'event_json': event.model_dump(mode='json'),
            'status': 'submitted' if job['status'] in ('queued', 'running') else job['status'],
            'job_revision': job['revision'], 'job_json': job,
            'report_id': job['result']['report_id'] if succeeded else None,
            'report_version': job['result']['report_version'] if succeeded else None,
            'content_sha256': canonical_digest(job['result']) if succeeded else None,
            'last_error_code': None, 'created_at': job['accepted_at'],
            'updated_at': job.get('completed_at', job['accepted_at'])}
        return _result(row)

    def link(self, job_id):
        with self._lock:
            self.store.get_job(job_id, self.principal)
            links = self._read()
            existing = next((link for link in links if link['job_id'] == job_id), None)
            if existing is None:
                if len(links) >= 1000:
                    raise WorkflowError('INVALID_STATE')
                existing = {'event_id': str(uuid4()), 'job_id': job_id}
                write_json(self.profile, self.path, {**self.profile.owner_record(), 'links': [existing, *links]})
            return self._row(existing)

    def get(self, event_id):
        with self._lock:
            return next((self._row(link) for link in self._read() if link['event_id'] == event_id), None)

    def list_results(self, limit=20):
        with self._lock:
            return [self._row(link) for link in self._read()[:limit]]


def create_browser_app(profile):
    profile = validate_browser_profile(profile)
    settings = load_lab_settings()
    worker = BrowserWorker(profile)
    workflow = BrowserWorkflow(SQLiteStore(profile.workflow_path), worker=worker)
    links = BrowserAnalysisLinks(profile, workflow.store)
    try:
        app = create_app(settings, workflow=workflow, database=_NoDatabase(),
            kafka_broker=_NoBroker(), kafka_store=_NoReviewStore(),
            analysis_broker=_NoBroker(), analysis_store=links)
    except Exception:
        workflow.close()
        raise
    app.state.browser_workflow = workflow
    app.state.browser_analysis_links = links
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with original_lifespan(application):
                yield
        finally:
            workflow.close()

    app.router.lifespan_context = lifespan

    def authorize(request: Request, key: Annotated[str | None, Header(alias='X-Lab-Key')] = None):
        if key is None or not hmac.compare_digest(key.encode(), settings['api_token'].encode()):
            raise WorkflowError('UNAUTHORIZED')
        if request.client is None or request.client.host not in ('127.0.0.1', '::1'):
            raise WorkflowError('FORBIDDEN')
        try:
            validate_browser_profile(profile)
        except (ValueError, OSError):
            raise WorkflowError('NOT_READY') from None

    @app.post('/qa/browser/config', dependencies=[Depends(authorize)], include_in_schema=False)
    def configure(config: BrowserConfig):
        with workflow._lock:
            if workflow._closed or workflow._active is not None or workflow.readiness()['state'] == 'preparing':
                raise WorkflowError('INVALID_STATE')
            worker.configure(config)
            return {**config.model_dump(), 'mode': 'deterministic', 'qa_run_id': profile.run_id}

    @app.post('/qa/browser/kafka-link', dependencies=[Depends(authorize)], include_in_schema=False)
    def kafka_link(link: KafkaLink):
        return links.link(link.job_id)

    return app
