"""Strict replay envelope. Legacy golden/result payloads require explicit migration."""
from typing import Any, Literal, get_args

from pydantic import Field, JsonValue, model_validator

from src.contracts.semantics import classify_incident_id
from src.contracts.v1 import (ErrorCode, Identifier, InvestigationContext,
                              RefusalCode, RuleCitation, StrictModel, Timestamp)


class BundleInputError(ValueError):
    """A safe error code; never contains the rejected payload or exception text."""


class Retrieval(StrictModel):
    namespace: Literal['rule_id', 'source_ref']
    ids: list[str]

    @model_validator(mode='after')
    def valid_ids(self):
        if any(not value.strip() for value in self.ids) or len(set(self.ids)) != len(self.ids):
            raise ValueError('retrieval identifiers must be nonempty and unique')
        return self


class Case(StrictModel):
    id: Identifier
    independence_id: Identifier
    category: Literal['A', 'B', 'C', 'D', 'E']
    # Retain the raw request value for invalid/missing-input controls. Never
    # invent a syntactically valid incident just to make an error case fit.
    incident_id: JsonValue
    expected_status: Literal['draft', 'refusal', 'error']
    expected_reason_code: RefusalCode | ErrorCode | None = None
    forbidden_outputs: list[str] = Field(default_factory=list)
    expected_retrieval: Retrieval | None = None
    context: InvestigationContext | None = None

    @model_validator(mode='after')
    def coherent(self):
        input_error = classify_incident_id(self.incident_id)
        if input_error and (self.expected_status != 'error' or self.expected_reason_code != input_error):
            raise ValueError('invalid incident requires its explicit expected input error')
        if self.expected_status == 'draft' and self.expected_reason_code is not None:
            raise ValueError('draft cannot expect a refusal or error reason')
        if self.expected_reason_code is not None:
            allowed = get_args(RefusalCode if self.expected_status == 'refusal' else ErrorCode)
            if self.expected_reason_code not in allowed:
                raise ValueError('reason code does not match expected status')
        if self.context and self.context.incident_id != self.incident_id:
            raise ValueError('case context incident mismatch')
        if any(not item.strip() for item in self.forbidden_outputs):
            raise ValueError('empty forbidden output')
        if self.category == 'C' and self.expected_status != 'refusal':
            raise ValueError('category C requires refusal')
        return self


class Outcome(StrictModel):
    case_id: Identifier
    incident_id: JsonValue = None
    result: Any = None
    latency_seconds: float | None = Field(default=None, ge=0)
    retrieval: Retrieval | None = None
    failure_code: Literal['RUNNER_EXCEPTION'] | None = None


class SMEAcceptance(StrictModel):
    status: Literal['accepted', 'pending', 'rejected']
    reviewer_id: Identifier | None = None
    reviewed_at: Timestamp | None = None
    evidence_references: list[str] = Field(default_factory=list)
    case_ids: list[Identifier] = Field(default_factory=list)


class Dataset(StrictModel):
    id: Identifier
    kind: Literal['acceptance', 'synthetic_control']
    sme_acceptance: SMEAcceptance | None = None


class ExternalMeasurement(StrictModel):
    status: Literal['measured', 'not_measured']
    value: float | None = Field(default=None, ge=0, le=1)
    denominator: int = Field(default=0, ge=0)
    evidence_references: list[str] = Field(default_factory=list)
    method: str | None = None


class ReplayBundle(StrictModel):
    schema_version: Literal['1.0']
    dataset: Dataset
    cases: list[Case]
    outcomes: list[Outcome]
    citation_catalog: list[RuleCitation] = Field(default_factory=list)
    external_measurements: dict[Literal['M-02', 'M-03', 'M-08'], ExternalMeasurement] = Field(default_factory=dict)

    @model_validator(mode='after')
    def unique_cases(self):
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate case IDs')
        identities = [(c.rule_id, c.rule_version, c.chunk_id) for c in self.citation_catalog]
        if len(identities) != len(set(identities)):
            raise ValueError('duplicate or conflicting catalog citation')
        return self
