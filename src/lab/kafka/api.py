"""Authenticated optional routes; no connections at app creation or /docs."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from src.lab.database import LabDatabaseError
from .broker import BrokerUnavailable
from .contracts import GROUP, TOPIC, KafkaErrorBody, PublishAck, ReviewEvent


def router(authorize, broker, store):
    routes = APIRouter(prefix='/lab/kafka', tags=['Kafka practice'], dependencies=[Depends(authorize)])

    @routes.post('/events', status_code=202, response_model=PublishAck,
                 responses={503: {'model': KafkaErrorBody, 'content': {'application/json': {'example': {
                     'code': 'KAFKA_DELIVERY_UNCONFIRMED', 'publication': 'unknown',
                     'message': 'Подтверждение не получено. Повторите то же событие после восстановления брокера.',
                 }}}}})
    def publish(event: ReviewEvent):
        """202 = broker ACK. Retry an uncertain publication with the SAME event_id and body.

        The worker saves a synthetic snapshot summary in PostgreSQL. This route
        never starts an AML model. Query /results/{event_id} for processing.
        """
        try:
            ack = broker.publish(TOPIC, key=event.incident_id.encode('utf-8'), value=event.canonical_bytes())
        except BrokerUnavailable as exc:
            return JSONResponse(status_code=503, content=KafkaErrorBody(
                code=exc.code, publication=exc.publication,
                message='Подтверждение брокера не получено. Для повтора сохраните event_id и всё тело события.',
            ).model_dump())
        return PublishAck(event_id=event.event_id, **ack)

    @routes.get('/status')
    def status():
        """Check broker and SQL separately. Worker availability is observed through group lag/results."""
        try:
            broker_status = broker.status()
        except BrokerUnavailable as exc:
            broker_status = {'status': 'unavailable', 'code': exc.code}
        try:
            database_status = {'status': 'ready', **store.status()}
        except LabDatabaseError as exc:
            database_status = {'status': 'unavailable', 'code': exc.code}
        return {'broker': broker_status, 'database': database_status, 'consumer_group': GROUP,
                'worker_check': 'Use Broker.ps1 -Action Lag and query results; this endpoint does not check the worker.'}

    @routes.get('/results/{event_id}', responses={404: {'model': KafkaErrorBody, 'content': {'application/json': {'example': {
        'code': 'KAFKA_RESULT_NOT_FOUND', 'publication': 'not_applicable',
        'message': 'Результат пока отсутствует; проверьте обработчик, lag и DLQ.',
    }}}}})
    def result(event_id: UUID):
        row = store.get(str(event_id))
        if row is None:
            return JSONResponse(status_code=404, content=KafkaErrorBody(
                code='KAFKA_RESULT_NOT_FOUND', publication='not_applicable',
                message='Результат пока отсутствует. Это не доказывает, что событие не опубликовано: проверьте обработчик, lag и DLQ.',
            ).model_dump())
        return {'status': 'processed', **row}

    @routes.get('/results')
    def results(limit: Annotated[int, Query(ge=1, le=100)] = 20):
        return {'items': store.list_results(limit=limit), 'limit': limit}

    return routes
