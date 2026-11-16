"""Versioned synthetic event and explicit Kafka delivery coordinates."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from src.qa_profile import get_qa_profile

_qa = get_qa_profile()
TOPIC = _qa.topic if _qa else 'aml.lab.incident-review.v1'
DLQ_TOPIC = _qa.dlq_topic if _qa else 'aml.lab.incident-review.dlq.v1'
GROUP = _qa.group if _qa else 'aml-lab-review-v1'
BOOTSTRAP = f'127.0.0.1:{_qa.kafka_port}' if _qa else '127.0.0.1:9092'
PARTITIONS = 3


class ReviewEvent(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, json_schema_extra={
        'examples': [{
            'event_id': 'b4c775c0-5076-4c94-8d61-82b4106a9c85',
            'occurred_at': '2026-10-01T12:00:00Z', 'incident_id': 'INC-000123',
            'schema_version': 1, 'event_type': 'incident.review.requested.v1', 'synthetic': True,
        }],
    })
    event_id: UUID
    occurred_at: datetime
    incident_id: Annotated[str, Field(strict=True, pattern=r'^INC-[0-9]{6}$')]
    schema_version: Literal[1] = 1
    event_type: Literal['incident.review.requested.v1'] = 'incident.review.requested.v1'
    synthetic: Literal[True] = True

    @field_validator('schema_version', mode='before')
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError('Expected integer schema version')
        return value

    @field_validator('synthetic', mode='before')
    @classmethod
    def only_synthetic(cls, value):
        if value is not True:
            raise ValueError('Synthetic events only')
        return value

    @field_validator('occurred_at', mode='before')
    @classmethod
    def timestamp_input(cls, value):
        if not isinstance(value, (str, datetime)):
            raise ValueError('Expected ISO timestamp with timezone')
        return value

    @field_validator('occurred_at')
    @classmethod
    def utc_timestamp(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Timezone is required')
        return value.astimezone(timezone.utc)

    def canonical_bytes(self) -> bytes:
        return json.dumps(self.model_dump(mode='json'), ensure_ascii=True,
                          sort_keys=True, separators=(',', ':')).encode('utf-8')


@dataclass(frozen=True)
class Record:
    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes | None


class PublishAck(BaseModel):
    status: Literal['broker_acknowledged'] = 'broker_acknowledged'
    event_id: UUID
    topic: str
    partition: int
    offset: int
    processing: Literal['check_result_separately'] = 'check_result_separately'


class KafkaErrorBody(BaseModel):
    code: str
    publication: Literal['not_attempted', 'unknown', 'not_applicable']
    message: str
