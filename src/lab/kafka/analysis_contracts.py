"""Explicit, immutable requests for the existing AML workflow via Kafka."""
from typing import Literal

from pydantic import ConfigDict

from src.contracts.v1 import AnalysisWindow, CreateJobRequest
from src.qa_profile import get_qa_profile
from .contracts import ReviewEvent

_qa = get_qa_profile()
TOPIC = _qa.analysis_topic if _qa else 'aml.lab.analysis-requested.v1'
DLQ_TOPIC = _qa.analysis_dlq if _qa else 'aml.lab.analysis-requested.dlq.v1'
GROUP = _qa.analysis_group if _qa else 'aml-lab-analysis-v1'


class AnalysisEvent(ReviewEvent):
    model_config = ConfigDict(extra='forbid', frozen=True, json_schema_extra={
        'examples': [{
            'event_id': '5884e14d-67f3-4f29-8243-a2666203e2ad',
            'occurred_at': '2026-10-04T12:00:00Z', 'incident_id': 'INC-000123',
            'schema_version': 1, 'event_type': 'aml.analysis.requested.v1',
            'synthetic': True, 'engine': 'template',
            'window': {'start_inclusive': '2026-07-01T00:00:00Z',
                       'end_exclusive': '2026-07-20T00:00:00Z',
                       'as_of': '2026-07-20T00:00:00Z'},
        }],
    })
    event_type: Literal['aml.analysis.requested.v1'] = 'aml.analysis.requested.v1'
    engine: Literal['template', 'local-model'] = 'template'
    window: AnalysisWindow

    def job_request(self) -> CreateJobRequest:
        return CreateJobRequest(schema_version='1.0', incident_id=self.incident_id,
                                window=self.window)

    @property
    def idempotency_key(self) -> str:
        return 'kafka-aml:' + str(self.event_id)
