"""Allowlisted operational events; no free text, input IDs, prompts or payloads."""
from dataclasses import asdict, dataclass
from typing import Literal

from pydantic import TypeAdapter
from src.contracts.v1 import ErrorCode, Identifier, RefusalCode

STAGES = frozenset({'input', 'policy', 'context', 'retrieval', 'generation', 'validation', 'complete'})
CODES = frozenset({'STARTED', 'PASSED', 'RETRY', 'DRAFT', 'REFUSAL', 'ERROR'})
CODE_ADAPTER = TypeAdapter(ErrorCode | RefusalCode | Literal['STARTED', 'PASSED', 'RETRY', 'DRAFT', 'REFUSAL', 'ERROR'])
ID_ADAPTER = TypeAdapter(Identifier)


@dataclass(frozen=True)
class RunEvent:
    correlation_id: str
    stage: str
    code: str
    attempt: int = 0

    def __post_init__(self):
        ID_ADAPTER.validate_python(self.correlation_id)
        CODE_ADAPTER.validate_python(self.code)
        if self.stage not in STAGES or type(self.attempt) is not int or not 0 <= self.attempt <= 2:
            raise ValueError('INVALID_EVENT')

    def to_dict(self):
        return asdict(self)
