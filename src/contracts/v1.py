"""Wire contracts v1. No I/O, source mutation, model calls or implicit coercion.

JSON Schema describes shape. Cross-field and source-resolution checks live here
and in semantics.py. Neither proves factual/AML correctness of generated prose.
"""
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, model_validator


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def valid_timestamp(value: str) -> str:
    instant(value)  # Reject impossible calendar dates as well as malformed syntax.
    return value


IncidentId = Annotated[str, Field(pattern=r'^INC-[0-9]{6}$', min_length=10, max_length=10)]
Identifier = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$', min_length=1, max_length=128,
                                  json_schema_extra={'not': {'pattern': '[\r\n]'}})]
Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$', min_length=64, max_length=64)]
Text = Annotated[str, Field(min_length=1, max_length=4000, pattern=r'\S')]
Timestamp = Annotated[str, Field(
    pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$',
    min_length=20, max_length=32, json_schema_extra={'format': 'date-time', 'not': {'pattern': '[\r\n]'}}), AfterValidator(valid_timestamp)]
Money = Annotated[str, Field(pattern=r'^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$', min_length=1, max_length=80,
                             json_schema_extra={'not': {'pattern': '[\r\n]'}})]
Currency = Annotated[str, Field(pattern=r'^[A-Z]{3}$', min_length=3, max_length=3)]
PositiveInt = Annotated[int, Field(ge=1)]
Revision = Annotated[int, Field(ge=0)]
TxField = Literal['amount', 'currency', 'counterparty', 'purpose', 'channel', 'status', 'timestamp']
ProfileField = Literal['risk_category', 'kyc_status', 'client_since']
Completeness = Literal['full', 'partial', 'insufficient', 'unassessed']
ErrorCode = Literal['MISSING_INPUT', 'INVALID_INPUT_FORMAT', 'NOT_FOUND', 'UNAUTHORIZED', 'FORBIDDEN',
                    'VERSION_CONFLICT', 'IDEMPOTENCY_CONFLICT', 'INVALID_STATE', 'NOT_READY',
                    'DEADLINE_EXCEEDED', 'DEPENDENCY_UNAVAILABLE', 'INTERNAL_ERROR']
RefusalCode = Literal['INSUFFICIENT_DATA', 'CONFLICTING_DATA', 'NO_APPLICABLE_RULE',
                      'AMBIGUOUS_RULE', 'UNTRUSTED_INPUT', 'POLICY_NOT_APPROVED', 'VALIDATION_FAILED']


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, allow_inf_nan=False)


class Versioned(StrictModel):
    schema_version: Literal['1.0']


class AnalysisWindow(StrictModel):
    start_inclusive: Timestamp
    end_exclusive: Timestamp
    as_of: Timestamp

    @model_validator(mode='after')
    def ordered(self):
        if not instant(self.start_inclusive) < instant(self.end_exclusive) <= instant(self.as_of):
            raise ValueError('window must satisfy start < end <= as_of')
        return self


class SourceVersion(StrictModel):
    source: Identifier
    version: Identifier
    content_sha256: Digest


class Snapshot(StrictModel):
    snapshot_id: Identifier
    captured_at: Timestamp
    sources: Annotated[list[SourceVersion], Field(min_length=1)]

    @model_validator(mode='after')
    def unique_sources(self):
        require_unique([s.source for s in self.sources], 'snapshot sources')
        return self


class ClientProfile(StrictModel):
    client_id: Identifier
    risk_category: Literal['low', 'medium', 'high']
    kyc_status: Literal['verified', 'pending', 'expired']
    client_since: Annotated[str, Field(pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$', min_length=10, max_length=10,
                                       json_schema_extra={'format': 'date'})]

    @model_validator(mode='after')
    def calendar_date(self):
        from datetime import date
        date.fromisoformat(self.client_since)
        return self


class CardRecord(StrictModel):
    card_id: Identifier
    client_id: Identifier
    card_status: Literal['active', 'blocked', 'closed']


class TransactionRecord(StrictModel):
    tx_id: Identifier
    client_id: Identifier
    card_id: Identifier | None
    amount: Money
    currency: Currency
    counterparty: Text
    purpose: Text
    channel: Literal['atm', 'pos', 'online', 'transfer']
    status: Literal['completed', 'pending', 'declined']
    timestamp: Timestamp


class InvestigationContext(Versioned):
    incident_id: IncidentId
    client_id: Identifier
    snapshot: Snapshot
    window: AnalysisWindow
    policy_version: Identifier | None
    data_completeness: Completeness
    consistency: Literal['consistent', 'conflicting', 'unassessed']
    missing_fields: list[Identifier]
    client: ClientProfile | None
    cards: list[CardRecord]
    transactions: list[TransactionRecord]

    @model_validator(mode='after')
    def coherent(self):
        require_unique([t.tx_id for t in self.transactions], 'transactions')
        require_unique([c.card_id for c in self.cards], 'cards')
        require_unique(self.missing_fields, 'missing fields')
        if self.client and self.client.client_id != self.client_id:
            raise ValueError('foreign client profile')
        if any(c.client_id != self.client_id for c in self.cards):
            raise ValueError('foreign card')
        cards = {c.card_id for c in self.cards}
        for tx in self.transactions:
            if tx.client_id != self.client_id or (tx.card_id is not None and tx.card_id not in cards):
                raise ValueError('foreign transaction or unresolved card')
            if not instant(self.window.start_inclusive) <= instant(tx.timestamp) < instant(self.window.end_exclusive):
                raise ValueError('transaction outside requested window')
        if instant(self.snapshot.captured_at) < instant(self.window.as_of):
            raise ValueError('snapshot predates as_of')
        if self.data_completeness == 'full' and (not self.client or not self.transactions or self.missing_fields
                or self.consistency != 'consistent' or self.policy_version is None):
            raise ValueError('full completeness requires data and explicit assessment policy')
        return self


class RuleCitation(StrictModel):
    rule_id: Identifier
    rule_version: Identifier
    source_ref: Text
    chunk_id: Identifier
    content_sha256: Digest


class TransactionEvidence(StrictModel):
    kind: Literal['transaction']
    snapshot_id: Identifier
    tx_id: Identifier
    field: TxField


class ProfileEvidence(StrictModel):
    kind: Literal['profile']
    snapshot_id: Identifier
    client_id: Identifier
    field: ProfileField


class SumEvidence(StrictModel):
    kind: Literal['sum']
    snapshot_id: Identifier
    tx_ids: Annotated[list[Identifier], Field(min_length=1)]
    field: Literal['amount']
    window: AnalysisWindow
    currency: Currency
    value: Money

    @model_validator(mode='after')
    def distinct(self):
        require_unique(self.tx_ids, 'aggregate members')
        return self


class CountEvidence(StrictModel):
    kind: Literal['count']
    snapshot_id: Identifier
    tx_ids: Annotated[list[Identifier], Field(min_length=1)]
    window: AnalysisWindow
    value: PositiveInt

    @model_validator(mode='after')
    def distinct_count(self):
        require_unique(self.tx_ids, 'aggregate members')
        if self.value != len(self.tx_ids):
            raise ValueError('count differs from distinct members')
        return self


Evidence = Annotated[Union[TransactionEvidence, ProfileEvidence, SumEvidence, CountEvidence], Field(discriminator='kind')]


class FoundFact(StrictModel):
    fact: Text
    evidence: Annotated[list[Evidence], Field(min_length=1)]
    sources: list[RuleCitation]


class SuspiciousPattern(StrictModel):
    pattern: Text
    evidence: Annotated[list[Evidence], Field(min_length=1)]
    sources: Annotated[list[RuleCitation], Field(min_length=1)]


class ApplicableRule(StrictModel):
    citation: RuleCitation
    rule_text: Text


class ReportRef(StrictModel):
    report_id: Identifier
    report_version: PositiveInt
    content_sha256: Digest


class DraftReport(Versioned):
    status: Literal['draft']
    incident_id: IncidentId
    report_id: Identifier
    report_version: PositiveInt
    generated_at: Timestamp
    snapshot_id: Identifier
    policy_version: Identifier
    data_completeness: Literal['full']
    found_facts: Annotated[list[FoundFact], Field(min_length=1)]
    suspicious_patterns: list[SuspiciousPattern]
    applicable_rules: Annotated[list[ApplicableRule], Field(min_length=1)]
    # Pre-release contract refinement: null explicitly means not calibrated.
    # Never manufacture a probability from successful structural validation.
    confidence: Annotated[float, Field(ge=0, le=1)] | None


class RefusalResult(Versioned):
    status: Literal['refusal']
    incident_id: IncidentId
    generated_at: Timestamp
    data_completeness: Completeness
    reason_code: RefusalCode
    reason: Text
    recommended_action: Literal['manual_review']


class ErrorResult(Versioned):
    status: Literal['error']
    generated_at: Timestamp
    correlation_id: Identifier
    code: ErrorCode
    message: Text
    retryable: bool


InvestigationResult = Annotated[Union[DraftReport, RefusalResult, ErrorResult], Field(discriminator='status')]


class CreateJobRequest(Versioned):
    incident_id: IncidentId
    window: AnalysisWindow


class JobBase(Versioned):
    job_id: Identifier
    incident_id: IncidentId
    revision: Revision
    accepted_at: Timestamp
    deadline_at: Timestamp

    @model_validator(mode='after')
    def deadline_order(self):
        if instant(self.deadline_at) <= instant(self.accepted_at):
            raise ValueError('deadline must follow acceptance')
        return self


class ActiveJob(JobBase):
    status: Literal['queued', 'running']


class SucceededJob(JobBase):
    status: Literal['succeeded']
    completed_at: Timestamp
    result: DraftReport


class RefusedJob(JobBase):
    status: Literal['refused']
    completed_at: Timestamp
    result: RefusalResult


class FailedJob(JobBase):
    status: Literal['failed']
    completed_at: Timestamp
    result: ErrorResult


class CancelledJob(JobBase):
    status: Literal['cancelled']
    completed_at: Timestamp


class TimedOutJob(JobBase):
    status: Literal['timed_out']
    completed_at: Timestamp


Job = Annotated[Union[ActiveJob, SucceededJob, RefusedJob, FailedJob, CancelledJob, TimedOutJob], Field(discriminator='status')]


class DecisionRequest(Versioned):
    report: ReportRef
    expected_revision: Revision
    action: Literal['approve', 'reject', 'request_changes']
    reason: Text


class Actor(StrictModel):
    subject_id: Identifier
    role: Literal['operator', 'supervisor']


class DecisionRecord(Versioned):
    decision_id: Identifier
    incident_id: IncidentId
    report: ReportRef
    previous_revision: Revision
    revision: PositiveInt
    action: Literal['approve', 'reject', 'request_changes']
    reason: Text
    actor: Actor
    decided_at: Timestamp
    audit_event_id: Identifier

    @model_validator(mode='after')
    def next_revision(self):
        if self.revision != self.previous_revision + 1:
            raise ValueError('decision revision must increment by one')
        return self


class DeliveryRequest(Versioned):
    job_id: Identifier
    decision_id: Identifier | None
    report: ReportRef | None
    destination: Identifier
    operation: Literal['manual_queue', 'export']

    @model_validator(mode='after')
    def export_binding(self):
        if self.operation == 'export' and (self.report is None or self.decision_id is None):
            raise ValueError('export requires report and human decision')
        return self


class Receipt(StrictModel):
    receipt_id: Identifier
    delivery_id: Identifier
    job_id: Identifier
    report: ReportRef | None
    destination: Identifier
    operation: Literal['manual_queue', 'export']
    mode: Literal['local_simulator', 'external']
    outcome: Literal['accepted']
    received_at: Timestamp


class DeliveryBase(DeliveryRequest):
    delivery_id: Identifier
    mode: Literal['local_simulator', 'external']
    revision: Revision
    attempt_count: Revision


class PendingDelivery(DeliveryBase):
    status: Literal['pending']


class ConfirmedDelivery(DeliveryBase):
    status: Literal['delivered']
    receipt: Receipt

    @model_validator(mode='after')
    def matching_receipt(self):
        for name in ('delivery_id', 'job_id', 'report', 'destination', 'operation', 'mode'):
            if getattr(self, name) != getattr(self.receipt, name):
                raise ValueError('receipt binding mismatch')
        if self.attempt_count < 1:
            raise ValueError('delivery requires an attempt')
        return self


class FailedDelivery(DeliveryBase):
    status: Literal['failed']
    error: ErrorResult


Delivery = Annotated[Union[PendingDelivery, ConfirmedDelivery, FailedDelivery], Field(discriminator='status')]


def require_unique(values: list, label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f'duplicate {label}')


ADAPTERS = {name: TypeAdapter(model) for name, model in {
    'InvestigationContext': InvestigationContext, 'InvestigationResult': InvestigationResult,
    'CreateJobRequest': CreateJobRequest, 'Job': Job, 'DecisionRequest': DecisionRequest,
    'DecisionRecord': DecisionRecord, 'DeliveryRequest': DeliveryRequest,
    'Delivery': Delivery, 'ErrorResult': ErrorResult,
}.items()}
