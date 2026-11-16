"""Pure semantic checks for later service integration; no durable state here."""
from decimal import Decimal, localcontext
import hashlib
import json
import re

from .v1 import (ADAPTERS, CountEvidence, DraftReport, InvestigationContext,
                 ProfileEvidence, SumEvidence, TransactionEvidence, instant)


class ContractViolation(ValueError):
    """Safe code only: do not include rejected payload or source PII in errors."""


def classify_incident_id(value) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return 'MISSING_INPUT'
    if not isinstance(value, str) or re.fullmatch(r'INC-[0-9]{6}', value) is None:
        return 'INVALID_INPUT_FORMAT'
    return None


def canonical_digest(payload: dict) -> str:
    """Local v1 hash recipe. Not a signature or cross-language JCS claim."""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def validate_report_context(report: DraftReport, context: InvestigationContext) -> None:
    # frozen BaseModel does not freeze its nested lists. Recheck wire values at
    # the publication boundary before resolving references or doing arithmetic.
    report = ADAPTERS['InvestigationResult'].validate_python(report.model_dump(mode='json'))
    context = ADAPTERS['InvestigationContext'].validate_python(context.model_dump(mode='json'))
    if not isinstance(report, DraftReport):
        raise ContractViolation('DRAFT_REQUIRED')
    if (report.incident_id != context.incident_id or report.snapshot_id != context.snapshot.snapshot_id
            or report.policy_version != context.policy_version):
        raise ContractViolation('REPORT_CONTEXT_MISMATCH')
    if context.data_completeness != 'full' or context.consistency != 'consistent':
        raise ContractViolation('CONTEXT_NOT_ELIGIBLE')
    if instant(report.generated_at) < instant(context.snapshot.captured_at):
        raise ContractViolation('REPORT_PREDATES_SNAPSHOT')
    rules = [r.citation.model_dump_json() for r in report.applicable_rules]
    if len(rules) != len(set(rules)):
        raise ContractViolation('DUPLICATE_RULE_CITATION')
    identities = [(r.citation.rule_id, r.citation.rule_version, r.citation.chunk_id) for r in report.applicable_rules]
    if len(identities) != len(set(identities)):
        raise ContractViolation('CONFLICTING_RULE_CITATION')
    txs = {t.tx_id: t for t in context.transactions}
    for assertion in [*report.found_facts, *report.suspicious_patterns]:
        for citation in assertion.sources:
            if citation.model_dump_json() not in rules:
                raise ContractViolation('UNBOUND_RULE_CITATION')
        for evidence in assertion.evidence:
            if evidence.snapshot_id != context.snapshot.snapshot_id:
                raise ContractViolation('EVIDENCE_SNAPSHOT_MISMATCH')
            if isinstance(evidence, ProfileEvidence):
                if context.client is None or evidence.client_id != context.client_id:
                    raise ContractViolation('UNRESOLVED_PROFILE')
            elif isinstance(evidence, TransactionEvidence):
                if evidence.tx_id not in txs:
                    raise ContractViolation('UNRESOLVED_TRANSACTION')
            elif isinstance(evidence, (SumEvidence, CountEvidence)):
                if evidence.window != context.window:
                    raise ContractViolation('AGGREGATE_WINDOW_MISMATCH')
                if any(tx_id not in txs for tx_id in evidence.tx_ids):
                    raise ContractViolation('UNRESOLVED_AGGREGATE_MEMBER')
                if isinstance(evidence, SumEvidence):
                    members = [txs[tx_id] for tx_id in evidence.tx_ids]
                    if any(tx.currency != evidence.currency for tx in members):
                        raise ContractViolation('CROSS_CURRENCY_SUM')
                    # Enough precision for every bounded decimal plus carry digits.
                    with localcontext() as decimal_context:
                        decimal_context.prec = 2 * 80 + len(str(len(members))) + 2
                        total = sum((Decimal(tx.amount) for tx in members), Decimal(0))
                    if total != Decimal(evidence.value):
                        raise ContractViolation('AGGREGATE_VALUE_MISMATCH')


TRANSITIONS = {
    'queued': {'running', 'cancelled', 'timed_out', 'failed'},
    'running': {'succeeded', 'refused', 'failed', 'cancelled', 'timed_out'},
    'succeeded': set(), 'refused': set(), 'failed': set(), 'cancelled': set(), 'timed_out': set(),
}


def validate_job(payload: dict):
    job = ADAPTERS['Job'].validate_python(payload)
    if job.status not in ('queued', 'running'):
        completed = instant(job.completed_at)
        if completed < instant(job.accepted_at):
            raise ContractViolation('COMPLETION_BEFORE_ACCEPTANCE')
        if job.status in ('succeeded', 'refused', 'failed'):
            if completed >= instant(job.deadline_at):
                raise ContractViolation('LATE_RESULT')
            result = job.result
            if hasattr(result, 'incident_id') and result.incident_id != job.incident_id:
                raise ContractViolation('JOB_INCIDENT_MISMATCH')
            if not instant(job.accepted_at) <= instant(result.generated_at) <= completed:
                raise ContractViolation('RESULT_TIME_MISMATCH')
        if job.status == 'timed_out' and completed < instant(job.deadline_at):
            raise ContractViolation('PREMATURE_TIMEOUT')
    return job


def validate_job_transition(previous: dict, following: dict) -> None:
    before, after = validate_job(previous), validate_job(following)
    for field in ('job_id', 'incident_id', 'accepted_at', 'deadline_at', 'schema_version'):
        if getattr(before, field) != getattr(after, field):
            raise ContractViolation('JOB_IDENTITY_CHANGED')
    if after.status not in TRANSITIONS[before.status]:
        raise ContractViolation('INVALID_STATE')
    if after.revision != before.revision + 1:
        raise ContractViolation('VERSION_CONFLICT')


def check_idempotent_replay(original_payload: dict, replay_payload: dict) -> None:
    """Service must scope key by authenticated principal+route and persist it."""
    if canonical_digest(original_payload) != canonical_digest(replay_payload):
        raise ContractViolation('IDEMPOTENCY_CONFLICT')
