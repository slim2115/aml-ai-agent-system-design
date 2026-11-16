"""Explicit real AML requests over Kafka; publication is not job acceptance."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from src.config import get_demo_paths, get_settings
from src.lab.database import LabDatabaseError
from src.runtime.policy import load_demo_policy
from src.workflow.store import WorkflowError
from .analysis_contracts import AnalysisEvent, TOPIC, GROUP
from .broker import BrokerUnavailable
from .contracts import KafkaErrorBody, PublishAck


def router(authorize, broker, store):
    routes = APIRouter(prefix='/lab/kafka/analyses', tags=['Kafka AML analysis'],
                       dependencies=[Depends(authorize)])

    @routes.post('', status_code=202, response_model=PublishAck,
                 responses={503: {'model': KafkaErrorBody, 'content': {'application/json': {'example': {
                     'code': 'KAFKA_DELIVERY_UNCONFIRMED', 'publication': 'unknown',
                     'message': 'Подтверждение Kafka не получено. Повторите исходное тело с тем же event_id.'}}}}})
    def publish(event: AnalysisEvent):
        """Publish an AML request. Prepare the API search separately before processing.

        202 means Kafka ACK only. The worker first durably queues the event in
        PostgreSQL, then idempotently submits a job to /v1. Read this resource
        by event_id to obtain job_id and the projected terminal result.
        """
        policy = load_demo_policy(get_demo_paths(get_settings().project_root)[0])
        if event.window != policy.window:
            raise WorkflowError('INVALID_INPUT_FORMAT')
        try:
            ack = broker.publish(TOPIC, key=event.incident_id.encode('utf-8'), value=event.canonical_bytes())
        except BrokerUnavailable as exc:
            return JSONResponse(status_code=503, content=KafkaErrorBody(
                code=exc.code, publication=exc.publication,
                message='Подтверждение Kafka не получено. Сохраните и повторите исходное тело с тем же event_id.',
            ).model_dump())
        return PublishAck(event_id=event.event_id, **ack)

    @routes.get('/status')
    def status():
        """Broker and inbox checks; neither proves an AML worker or model is ready."""
        try:
            broker_state = broker.status()
        except BrokerUnavailable as exc:
            broker_state = {'status': 'unavailable', 'code': exc.code}
        try:
            store.list_results(limit=1)
            database_state = {'status': 'ready', 'schema': 'kafka_lab', 'queue': 'analysis_requests'}
        except LabDatabaseError as exc:
            database_state = {'status': 'unavailable', 'code': exc.code}
        return {'broker': broker_state, 'database': database_state, 'consumer_group': GROUP,
                'worker_check': 'Check AML worker, /v1/readiness and the requested event result.'}

    @routes.get('')
    def requests(limit: Annotated[int, Query(ge=1, le=100)] = 20):
        return {'items': store.list_results(limit=limit), 'limit': limit}

    @routes.get('/{event_id}', responses={404: {'model': KafkaErrorBody, 'content': {'application/json': {'example': {
        'code': 'AML_REQUEST_NOT_FOUND', 'publication': 'not_applicable',
        'message': 'Запрос ещё не записан в очередь PostgreSQL. Проверьте обработчик и AML DLQ.'}}}}})
    def result(event_id: UUID):
        row = store.get(str(event_id))
        if row is None:
            return JSONResponse(status_code=404, content=KafkaErrorBody(
                code='AML_REQUEST_NOT_FOUND', publication='not_applicable',
                message='Запрос ещё не записан в очередь PostgreSQL. Проверьте обработчик, отставание и AML DLQ.',
            ).model_dump())
        return row

    return routes
