"""Loopback learning API. PostgreSQL exercises are independent of model readiness.

The local key protects a single OS-derived operator, not production banking IAM.
Workflow endpoints reuse the verified SQLite controller; practice SQL mutations
do not change the immutable files consumed by that controller.
"""
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import hmac
import json
from pathlib import Path
import threading
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import Field, TypeAdapter, field_validator
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.contracts.v1 import (ActiveJob, CancelledJob, CreateJobRequest, DecisionRecord,
    DecisionRequest, Delivery, DeliveryRequest, DraftReport, ErrorResult, Identifier,
    IncidentId, Job, PendingDelivery, StrictModel, Text, Timestamp)
from src.config import get_demo_paths
from src.qa_profile import get_qa_profile
from src.workflow.auth import trusted_local_principal
from src.workflow.controller import WorkflowService
from src.workflow.delivery import DeliveryService
from src.workflow.store import SQLiteStore, WorkflowError, utc_now
from .config import load_lab_settings
from .database import LabDatabase, LabDatabaseError

ROOT = Path(__file__).resolve().parents[2]
ERROR_STATUS = {'MISSING_INPUT': 400, 'INVALID_INPUT_FORMAT': 400, 'UNAUTHORIZED': 401,
    'FORBIDDEN': 403, 'NOT_FOUND': 404, 'VERSION_CONFLICT': 409, 'IDEMPOTENCY_CONFLICT': 409,
    'INVALID_STATE': 409, 'NOT_READY': 503, 'DEPENDENCY_UNAVAILABLE': 503,
    'DEADLINE_EXCEEDED': 503, 'INTERNAL_ERROR': 500}
ERROR_RESPONSES = {status: {'model': ErrorResult} for status in set(ERROR_STATUS.values())}
API_KEY = APIKeyHeader(name='X-Lab-Key', auto_error=False,
    description='Local learning key from the generated local credentials file; one trusted OS operator, not IAM.')
LIMIT = Annotated[int, Query(ge=1, le=100)]
OFFSET = Annotated[int, Query(ge=0, le=1000000)]
KEY = Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=128,
                            pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')]


class Client(StrictModel):
    client_id: str
    risk_category: str
    kyc_status: str
    client_since: str


class Card(StrictModel):
    card_id: str
    client_id: str
    card_status: str


class Rule(StrictModel):
    rule_id: str
    version: str
    title: str
    regulation_ref: str
    category: str
    text: str
    effective_from: str


class Incident(StrictModel):
    incident_id: IncidentId
    case_id: str
    client_id: str
    alert_type: str
    created_at: Timestamp


class Transaction(StrictModel):
    tx_id: str
    client_id: str
    card_id: str
    amount: str
    currency: str
    counterparty: str
    purpose: str
    channel: str
    status: str
    timestamp: Timestamp


class NoteCreate(StrictModel):
    incident_id: IncidentId
    body: Text

    @field_validator('body')
    @classmethod
    def postgres_text(cls, value):
        if '\x00' in value:
            raise ValueError('Invalid note text')
        return value


class NoteUpdate(StrictModel):
    body: Text
    expected_revision: Annotated[int, Field(ge=1)]

    _postgres_text = field_validator('body')(NoteCreate.postgres_text.__func__)


class Note(StrictModel):
    id: str
    incident_id: IncidentId
    body: Text
    revision: Annotated[int, Field(ge=1)]
    created_at: Timestamp
    updated_at: Timestamp


class Dispatch(StrictModel):
    expected_revision: Annotated[int, Field(ge=0)]


class Readiness(StrictModel):
    ready: bool
    code: str
    state: str


class Health(StrictModel):
    status: Literal['ok']
    mode: Literal['local_learning']
    check: Literal['http_liveness_only']
    qa_run_id: str | None = None
    qa_owner: str | None = None


class DatabaseStatus(StrictModel):
    status: Literal['ready']
    mode: Literal['synthetic_practice']


T = TypeVar('T')


class Page(StrictModel, Generic[T]):
    items: list[T]
    limit: int
    offset: int


def wire(value):
    """Retain exact money; normalize timestamps without browser/local-zone assumptions."""
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise WorkflowError('INTERNAL_ERROR')
        return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    if isinstance(value, (date, UUID)):
        return str(value)
    if isinstance(value, dict):
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire(item) for item in value]
    return value


def error_response(code):
    if code not in ERROR_STATUS:
        code = 'INTERNAL_ERROR'
    error = ErrorResult(schema_version='1.0', status='error', generated_at=utc_now(),
        correlation_id='api-' + uuid4().hex, code=code,
        message='Запрос не выполнен. Используйте код ошибки и идентификатор обращения.',
        retryable=code in {'NOT_READY', 'DEPENDENCY_UNAVAILABLE'})
    return JSONResponse(error.model_dump(mode='json'), status_code=ERROR_STATUS[code])


def create_app(settings=None, *, workflow=None, database=None, kafka_broker=None, kafka_store=None,
               analysis_broker=None, analysis_store=None):
    profile = get_qa_profile()
    from .kafka.api import router as kafka_router
    from .kafka.broker import KafkaBroker
    from .kafka.store import KafkaStore
    from .kafka.analysis_api import router as analysis_router
    from .kafka.analysis_store import AnalysisStore
    settings = settings if settings is not None else load_lab_settings()
    database = database if database is not None else LabDatabase(settings)
    event_broker = kafka_broker if kafka_broker is not None else KafkaBroker()
    event_store = kafka_store if kafka_store is not None else KafkaStore(database)
    aml_broker = analysis_broker if analysis_broker is not None else KafkaBroker(flow='aml')
    aml_store = analysis_store if analysis_store is not None else AnalysisStore(database)
    principal = trusted_local_principal()
    holder, holder_lock = {'workflow': workflow}, threading.Lock()

    def service():
        with holder_lock:
            if holder['workflow'] is None:
                path = profile.workflow_path if profile else ROOT / 'var/workflow/api-workflow.sqlite3'
                holder['workflow'] = WorkflowService(SQLiteStore(path))
            return holder['workflow']

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            from starlette.concurrency import run_in_threadpool
            try:
                if kafka_broker is None:
                    await run_in_threadpool(event_broker.close)
            finally:
                try:
                    if analysis_broker is None:
                        await run_in_threadpool(aml_broker.close)
                finally:
                    if workflow is None and holder['workflow'] is not None:
                        # A Kafka shutdown error must not leave an AML worker alive.
                        await run_in_threadpool(holder['workflow'].close)

    app = FastAPI(title='AML local SQL/API practice', version='1.0.0',
        description='Synthetic local learning stand. PostgreSQL practice data and notes are separate from immutable workflow sources. No real AML approval, banking IAM, external delivery or signature.',
        docs_url=None, redoc_url=None, lifespan=lifespan)
    if profile is not None:
        app.title = 'AML QA SQL/API practice · ' + profile.run_id
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])
    app.mount('/static', StaticFiles(directory=ROOT / 'lab/api/static', check_dir=False), name='static')

    @app.get('/docs', include_in_schema=False)
    def docs():
        return get_swagger_ui_html(openapi_url='/openapi.json', title=app.title,
            swagger_js_url='/static/swagger-ui-bundle.js', swagger_css_url='/static/swagger-ui.css',
            swagger_favicon_url='/static/favicon.svg', swagger_ui_parameters={
                'validatorUrl': None, 'persistAuthorization': False, 'displayRequestDuration': True})

    @app.get('/health', tags=['Service'], response_model=Health, response_model_exclude_none=True)
    def health():
        response = {'status': 'ok', 'mode': 'local_learning', 'check': 'http_liveness_only'}
        if profile is not None:
            response.update(qa_run_id=profile.run_id, qa_owner=profile.marker)
        return response

    @app.exception_handler(WorkflowError)
    async def workflow_error(_request, exc):
        return error_response(exc.code)

    @app.exception_handler(LabDatabaseError)
    async def database_error(_request, exc):
        mapping = {'UNAVAILABLE': 'DEPENDENCY_UNAVAILABLE', 'CONSTRAINT_VIOLATION': 'INVALID_INPUT_FORMAT',
                   'CONFLICT': 'VERSION_CONFLICT', 'INVALID_STATE': 'INVALID_STATE'}
        return error_response(mapping.get(exc.code, 'INTERNAL_ERROR'))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        code = 'MISSING_INPUT' if any(error['type'] == 'missing' for error in exc.errors()) else 'INVALID_INPUT_FORMAT'
        return error_response(code)

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        # Never reflect a framework exception detail or user input.
        return error_response({400: 'INVALID_INPUT_FORMAT', 401: 'UNAUTHORIZED', 403: 'FORBIDDEN', 404: 'NOT_FOUND',
                              405: 'INVALID_INPUT_FORMAT'}.get(exc.status_code, 'INTERNAL_ERROR'))

    @app.exception_handler(Exception)
    async def unknown_error(_request, _exc):
        return error_response('INTERNAL_ERROR')

    def authorize(key: Annotated[str | None, Depends(API_KEY)]):
        if key is None or not hmac.compare_digest(key.encode('utf-8'), settings['api_token'].encode('utf-8')):
            raise WorkflowError('UNAUTHORIZED')

    app.include_router(kafka_router(authorize, event_broker, event_store), responses=ERROR_RESPONSES)
    app.include_router(analysis_router(authorize, aml_broker, aml_store), responses=ERROR_RESPONSES)

    lab = APIRouter(prefix='/lab', tags=['SQL practice'], dependencies=[Depends(authorize)], responses=ERROR_RESPONSES)
    api = APIRouter(prefix='/v1', tags=['AML workflow'], dependencies=[Depends(authorize)], responses=ERROR_RESPONSES)

    def rows(sql, parameters=()):
        with database.connect(role='api') as connection:
            return wire(connection.execute(sql, parameters).fetchall())

    def page(sql, parameters, limit, offset):
        return {'items': rows(sql + ' LIMIT %s OFFSET %s', (*parameters, limit, offset)),
                'limit': limit, 'offset': offset}

    @lab.get('/status', response_model=DatabaseStatus)
    def database_status():
        """PostgreSQL connection check; schema changes made in SQL may affect other routes."""
        rows('SELECT 1 AS connected')
        return {'status': 'ready', 'mode': 'synthetic_practice'}

    @lab.get('/clients', response_model=Page[Client])
    def clients(limit: LIMIT = 20, offset: OFFSET = 0):
        return page('SELECT client_id, risk_category, kyc_status, client_since FROM practice.clients ORDER BY client_id', (), limit, offset)

    @lab.get('/clients/{client_id}', response_model=Client)
    def client(client_id: Identifier):
        result = rows('SELECT client_id, risk_category, kyc_status, client_since FROM practice.clients WHERE client_id = %s', (client_id,))
        if not result:
            raise WorkflowError('NOT_FOUND')
        return result[0]

    @lab.get('/incidents', response_model=Page[Incident])
    def incidents(client_id: Annotated[str | None, Query(max_length=128)] = None,
                  limit: LIMIT = 20, offset: OFFSET = 0):
        sql = 'SELECT incident_id, case_id, client_id, alert_type, created_at FROM practice.incidents'
        return page(sql + (' WHERE client_id = %s' if client_id is not None else '') + ' ORDER BY incident_id',
                    (client_id,) if client_id is not None else (), limit, offset)

    @lab.get('/cards', response_model=Page[Card])
    def cards(client_id: Annotated[str | None, Query(max_length=128)] = None,
              limit: LIMIT = 20, offset: OFFSET = 0):
        return page('SELECT card_id, client_id, card_status FROM practice.cards'
                    + (' WHERE client_id = %s' if client_id is not None else '') + ' ORDER BY card_id',
                    (client_id,) if client_id is not None else (), limit, offset)

    @lab.get('/rules', response_model=Page[Rule])
    def rules(category: Annotated[str | None, Query(max_length=128)] = None,
              limit: LIMIT = 20, offset: OFFSET = 0):
        """Synthetic teaching corpus, not approved AML policy or legal guidance."""
        return page('SELECT rule_id, version, title, regulation_ref, category, text, effective_from FROM practice.rules'
                    + (' WHERE category = %s' if category is not None else '') + ' ORDER BY rule_id, version',
                    (category,) if category is not None else (), limit, offset)

    @lab.get('/transactions', response_model=Page[Transaction])
    def transactions(client_id: Annotated[str | None, Query(max_length=128)] = None,
                     incident_id: IncidentId | None = None,
                     currency: Annotated[str | None, Query(pattern=r'^[A-Z]{3}$')] = None,
                     start_inclusive: datetime | None = None, end_exclusive: datetime | None = None,
                     min_amount: Annotated[Decimal | None, Query(max_digits=38, decimal_places=2)] = None,
                     max_amount: Annotated[Decimal | None, Query(max_digits=38, decimal_places=2)] = None,
                     limit: LIMIT = 20, offset: OFFSET = 0):
        """Incident filter selects that incident's client; time window remains an explicit filter."""
        if any(value is not None and value.tzinfo is None for value in (start_inclusive, end_exclusive)):
            raise WorkflowError('INVALID_INPUT_FORMAT')
        if start_inclusive and end_exclusive and start_inclusive >= end_exclusive:
            raise WorkflowError('INVALID_INPUT_FORMAT')
        if min_amount is not None and max_amount is not None and min_amount > max_amount:
            raise WorkflowError('INVALID_INPUT_FORMAT')
        clauses, parameters = [], []
        for condition, value in [('t.client_id = %s', client_id),
            ('t.client_id = (SELECT client_id FROM practice.incidents WHERE incident_id = %s)', incident_id),
            ('t.currency = %s', currency), ('t.timestamp >= %s', start_inclusive),
            ('t.timestamp < %s', end_exclusive), ('t.amount >= %s', min_amount), ('t.amount <= %s', max_amount)]:
            if value is not None:
                clauses.append(condition)
                parameters.append(value)
        sql = 'SELECT t.tx_id, t.client_id, t.card_id, t.amount, t.currency, t.counterparty, t.purpose, t.channel, t.status, t.timestamp FROM practice.transactions t'
        return page(sql + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY t.timestamp, t.tx_id', parameters, limit, offset)

    @lab.get('/notes', response_model=Page[Note])
    def notes(incident_id: IncidentId | None = None, limit: LIMIT = 20, offset: OFFSET = 0):
        return page('SELECT * FROM practice.case_notes' + (' WHERE incident_id = %s' if incident_id else '') + ' ORDER BY created_at, id',
                    (incident_id,) if incident_id else (), limit, offset)

    @lab.get('/notes/{note_id}', response_model=Note)
    def note(note_id: UUID):
        result = rows('SELECT * FROM practice.case_notes WHERE id = %s', (note_id,))
        if not result:
            raise WorkflowError('NOT_FOUND')
        return result[0]

    @lab.post('/notes', response_model=Note, status_code=201)
    def add_note(body: NoteCreate, response: Response):
        note_id = uuid4()
        result = rows('INSERT INTO practice.case_notes (id, incident_id, body) VALUES (%s, %s, %s) RETURNING *',
                      (note_id, body.incident_id, body.body))[0]
        response.headers['Location'] = '/lab/notes/' + str(note_id)
        return result

    def note_cas(note_id, revision, body=None, *, delete=False):
        with database.connect(role='api') as connection:
            current = connection.execute('SELECT revision FROM practice.case_notes WHERE id = %s FOR UPDATE', (note_id,)).fetchone()
            if current is None:
                raise WorkflowError('NOT_FOUND')
            if current['revision'] != revision:
                raise WorkflowError('VERSION_CONFLICT')
            if delete:
                connection.execute('DELETE FROM practice.case_notes WHERE id = %s AND revision = %s', (note_id, revision))
                return None
            return wire(connection.execute('UPDATE practice.case_notes SET body = %s, revision = revision + 1, updated_at = clock_timestamp() WHERE id = %s AND revision = %s RETURNING *',
                                           (body, note_id, revision)).fetchone())

    @lab.patch('/notes/{note_id}', response_model=Note)
    def update_note(note_id: UUID, body: NoteUpdate):
        return note_cas(note_id, body.expected_revision, body.body)

    @lab.delete('/notes/{note_id}', status_code=204)
    def delete_note(note_id: UUID, expected_revision: Annotated[int, Query(ge=1)]):
        note_cas(note_id, expected_revision, delete=True)
        return Response(status_code=204)

    @api.get('/readiness', response_model=Readiness)
    def readiness():
        return service().readiness()

    @api.post('/readiness', response_model=Readiness, status_code=202)
    def prepare():
        """Explicit asynchronous model/index preparation; poll GET. Does not accept a job."""
        return service().prepare()

    @api.get('/jobs', response_model=list[Job])
    def jobs():
        return service().store.list_jobs(principal)

    @api.post('/jobs', response_model=ActiveJob, status_code=202)
    def create_job(body: CreateJobRequest, response: Response, idempotency_key: KEY,
                   demo: bool = False, engine: Literal['template', 'local-model'] = 'template'):
        if not demo:
            raise WorkflowError('FORBIDDEN')
        result = service().submit(body, principal, idempotency_key, engine=engine)
        response.headers['Location'] = '/v1/jobs/' + result['job_id']
        return result

    @api.get('/jobs/{job_id}', response_model=Job)
    def job(job_id: Identifier):
        return service().store.get_job(job_id, principal)

    @api.get('/jobs/{job_id}/events')
    def job_events(job_id: Identifier):
        return service().store.list_runtime_events(job_id, principal)

    @api.post('/jobs/{job_id}/cancel', response_model=CancelledJob)
    def cancel_job(job_id: Identifier, idempotency_key: KEY):
        return service().cancel(job_id, principal, idempotency_key)

    @api.get('/reports/{report_id}', response_model=DraftReport)
    def report(report_id: Identifier, version: Annotated[int, Query(ge=1)]):
        return service().store.get_report(report_id, version, principal)

    @api.get('/reports/{report_id}/reference')
    def report_reference(report_id: Identifier, version: Annotated[int, Query(ge=1)]):
        """Exact stored report hash/version required by decision and export requests."""
        return service().store.report_ref(report_id, version, principal)

    @api.post('/reports/{report_id}/decisions', response_model=DecisionRecord, status_code=201)
    def decide(report_id: Identifier, body: DecisionRequest, idempotency_key: KEY):
        return service().store.create_decision(report_id, body, principal, idempotency_key)

    @api.get('/decisions/{decision_id}', response_model=DecisionRecord)
    def decision(decision_id: Identifier):
        return service().store.get_decision(decision_id, principal)

    @api.post('/deliveries', response_model=PendingDelivery, status_code=202)
    def create_delivery(body: DeliveryRequest, idempotency_key: KEY):
        return DeliveryService(service().store).create(body, principal, idempotency_key)

    @api.get('/deliveries/{delivery_id}', response_model=Delivery)
    def delivery(delivery_id: Identifier):
        return DeliveryService(service().store).get(delivery_id, principal)

    @api.post('/deliveries/{delivery_id}/dispatch', response_model=Delivery)
    def dispatch(delivery_id: Identifier, body: Dispatch):
        """Local simulator only. Compare-and-set revision; inspect result before retrying."""
        return DeliveryService(service().store).dispatch(delivery_id, principal, body.expected_revision)

    @api.get('/deliveries/{delivery_id}/artifact')
    def artifact(delivery_id: Identifier):
        data = DeliveryService(service().store).export_artifact(delivery_id, principal)
        return Response(data, media_type='application/json', headers={
            'Content-Disposition': 'attachment; filename="synthetic-report.json"'})

    app.include_router(lab)
    app.include_router(api)
    # Validation is intentionally a safe 400 envelope, not FastAPI's default 422/input echo.
    original_openapi = app.openapi
    def openapi():
        schema = original_openapi()
        timestamp = '2026-07-20T00:00:00Z'
        # Give Swagger usable examples; regex-generated samples can contain
        # impossible calendar dates. Canonical v1 models are not modified.
        def timestamp_examples(node):
            if isinstance(node, dict):
                if node.get('format') == 'date-time':
                    node['examples'] = [timestamp]
                elif node.get('format') == 'date':
                    node['examples'] = ['2026-07-20']
                for child in list(node.values()):
                    timestamp_examples(child)
            elif isinstance(node, list):
                for child in node:
                    timestamp_examples(child)
        timestamp_examples(schema)
        window = json.loads(get_demo_paths(ROOT)[0].read_text(encoding='utf-8'))['window']
        job_example = CreateJobRequest(schema_version='1.0', incident_id='INC-000123', window=window).model_dump(mode='json')
        examples = {
            'AnalysisEvent': {'event_id': 'b4c775c0-5076-4c94-8d61-82b4106a9c85',
                'occurred_at': '2026-10-04T12:00:00Z', 'incident_id': 'INC-000123',
                'schema_version': 1, 'event_type': 'aml.analysis.requested.v1',
                'synthetic': True, 'engine': 'template', 'window': window},
            'CreateJobRequest': job_example,
            'NoteCreate': {'incident_id': 'INC-000123', 'body': 'Учебная заметка: проверяю HTTP и SQL.'},
            'NoteUpdate': {'body': 'Учебная заметка обновлена.', 'expected_revision': 1},
            'Readiness': {'ready': False, 'code': 'NOT_READY', 'state': 'not_ready'},
            'Health': {'status': 'ok', 'mode': 'local_learning', 'check': 'http_liveness_only'},
            'DatabaseStatus': {'status': 'ready', 'mode': 'synthetic_practice'},
        }
        for name, example in examples.items():
            schema['components']['schemas'][name]['examples'] = [example]
        def example_error(code):
            return ErrorResult(schema_version='1.0', status='error', generated_at=timestamp,
                correlation_id='api-example', code=code,
                message='Запрос не выполнен. Используйте код ошибки и идентификатор обращения.',
                retryable=code in {'NOT_READY', 'DEPENDENCY_UNAVAILABLE'}).model_dump(mode='json')
        schema['components']['schemas']['ErrorResult']['examples'] = [example_error('INVALID_INPUT_FORMAT')]
        error_examples = {'400': 'INVALID_INPUT_FORMAT', '401': 'UNAUTHORIZED', '403': 'FORBIDDEN',
            '404': 'NOT_FOUND', '409': 'VERSION_CONFLICT', '500': 'INTERNAL_ERROR', '503': 'DEPENDENCY_UNAVAILABLE'}
        for path in schema['paths'].values():
            for operation in path.values():
                if isinstance(operation, dict):
                    responses = operation.get('responses', {})
                    responses.pop('422', None)
                    for status, code in error_examples.items():
                        if status in responses and responses[status].get('content', {}).get('application/json', {}).get('schema', {}).get('$ref', '').endswith('/ErrorResult'):
                            responses[status]['content']['application/json']['example'] = example_error(code)
        return schema
    app.openapi = openapi
    return app
