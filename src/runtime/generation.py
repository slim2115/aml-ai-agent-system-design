"""Constrained factual drafts: the model orders identifiers, never writes facts.

Callers must establish source snapshot and rule applicability under an explicit
policy before preparing a request. This module does not determine AML suspicion,
legal applicability, KYC eligibility, or an empirical confidence score.
"""
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, localcontext
from hashlib import sha256
import json

from pydantic import ValidationError

from src.contracts import v1
from src.contracts.semantics import ContractViolation, canonical_digest, validate_report_context


MAX_TRANSACTIONS = 1000
MAX_RULE_CHUNKS = 128
MAX_RESPONSE_BYTES = 128 * 1024


class GenerationViolation(ValueError):
    """A constant public code; rejected input/model output is never interpolated."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _inputs(context, rules):
    try:
        # Revalidation through JSON-shaped values detects mutated nested lists.
        checked = v1.InvestigationContext.model_validate(context.model_dump(mode='json'))
        if type(rules) not in (list, tuple):
            raise GenerationViolation('INVALID_RULE_CATALOG')
        verified = [v1.ApplicableRule.model_validate(rule.model_dump(mode='json')) for rule in rules]
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise GenerationViolation('INVALID_GENERATION_INPUT') from None
    if checked.data_completeness != 'full' or checked.consistency != 'consistent':
        raise GenerationViolation('CONTEXT_NOT_ELIGIBLE')
    if not verified:
        raise GenerationViolation('EMPTY_RULE_CATALOG')
    if len(checked.transactions) > MAX_TRANSACTIONS or len(verified) > MAX_RULE_CHUNKS:
        raise GenerationViolation('GENERATION_BUDGET_EXCEEDED')
    identities, versions = set(), {}
    for rule in verified:
        citation = rule.citation
        identity = (citation.rule_id, citation.rule_version, citation.chunk_id)
        if identity in identities:
            raise GenerationViolation('DUPLICATE_RULE_IDENTITY')
        identities.add(identity)
        if citation.rule_id in versions and versions[citation.rule_id] != citation.rule_version:
            raise GenerationViolation('CONFLICTING_RULE_VERSION')
        versions[citation.rule_id] = citation.rule_version
        try:
            digest = sha256(rule.rule_text.encode('utf-8')).hexdigest()
        except UnicodeError:
            raise GenerationViolation('INVALID_RULE_TEXT') from None
        if digest != citation.content_sha256:
            raise GenerationViolation('RULE_TEXT_HASH_MISMATCH')
    verified.sort(key=lambda rule: (rule.citation.rule_id, rule.citation.rule_version,
                                    rule.citation.chunk_id, rule.citation.content_sha256))
    return checked, verified


def _catalog(context: v1.InvestigationContext):
    """Every profile field/transaction and each complete currency group is kept."""
    snapshot = context.snapshot.snapshot_id
    records = []

    def add(fact: str, evidence: list[dict], safe: dict):
        fact_id = f'F{len(records) + 1:06d}'
        records.append((fact_id, v1.FoundFact.model_validate({'fact': fact, 'evidence': evidence, 'sources': []}),
                        {'fact_id': fact_id, **safe}))

    profile_labels = {'risk_category': 'Категория риска в профиле',
                      'kyc_status': 'Статус KYC в профиле', 'client_since': 'Дата начала отношений в профиле'}
    for field, label in profile_labels.items():
        value = getattr(context.client, field)
        add(f'{label}: {value}.', [{'kind': 'profile', 'snapshot_id': snapshot,
                                   'client_id': context.client_id, 'field': field}],
            {'kind': 'profile_field', 'field': field, 'value': value})
    transactions = sorted(context.transactions, key=lambda tx: tx.tx_id)
    for ordinal, tx in enumerate(transactions, 1):
        fields = ('amount', 'currency', 'timestamp', 'status', 'channel')
        add(f'Транзакция {tx.tx_id}: сумма {tx.amount} {tx.currency}; время {tx.timestamp}; '
            f'статус {tx.status}; канал {tx.channel}.',
            [{'kind': 'transaction', 'snapshot_id': snapshot, 'tx_id': tx.tx_id, 'field': field}
             for field in fields],
            {'kind': 'transaction', 'ordinal': ordinal, **{field: getattr(tx, field) for field in fields}})
    window = context.window.model_dump(mode='json')
    window_label = f'[{context.window.start_inclusive}, {context.window.end_exclusive})'
    tx_ids = [tx.tx_id for tx in transactions]
    add(f'Количество всех транзакций в контексте за окно {window_label}, включая все статусы: {len(tx_ids)}.',
        [{'kind': 'count', 'snapshot_id': snapshot, 'tx_ids': tx_ids, 'window': window, 'value': len(tx_ids)}],
        {'kind': 'transaction_count', 'window': window, 'value': len(tx_ids), 'status_scope': 'all'})
    for currency in sorted({tx.currency for tx in transactions}):
        members = [tx for tx in transactions if tx.currency == currency]
        with localcontext() as decimal_context:
            decimal_context.prec = 2 * 80 + len(str(len(members))) + 2
            value = format(sum((Decimal(tx.amount) for tx in members), Decimal(0)), 'f')
        try:
            add(f'Сумма всех транзакций в контексте в валюте {currency} за окно {window_label}, '
                f'включая все статусы: {value} {currency}.',
                [{'kind': 'sum', 'snapshot_id': snapshot, 'tx_ids': [tx.tx_id for tx in members],
                  'field': 'amount', 'window': window, 'currency': currency, 'value': value}],
                {'kind': 'currency_sum', 'window': window, 'currency': currency,
                 'value': value, 'member_count': len(members), 'status_scope': 'all'})
        except ValidationError:
            # The bounded Money contract must not cause a rounded or truncated sum.
            raise GenerationViolation('AGGREGATE_OUT_OF_CONTRACT_RANGE') from None
    return records


@dataclass(frozen=True, slots=True, repr=False)
class GenerationRequest:
    """Immutable value snapshot; properties return detached mutable copies.

    This in-process object is not an authorization token. The orchestrator owns
    policy authorization, provenance, and the publication boundary.
    """

    _context_json: str
    _rules_json: str

    def _restore(self):
        try:
            context = v1.InvestigationContext.model_validate(json.loads(self._context_json))
            rules = [v1.ApplicableRule.model_validate(rule) for rule in json.loads(self._rules_json)]
        except (TypeError, ValueError, ValidationError):
            raise GenerationViolation('INVALID_GENERATION_REQUEST') from None
        context, rules = _inputs(context, rules)
        return context, rules, _catalog(context)

    @property
    def fact_ids(self) -> tuple[str, ...]:
        return tuple(record[0] for record in self._restore()[2])

    @property
    def rule_keys(self) -> tuple[str, ...]:
        return tuple(f'R{number:06d}' for number in range(1, len(self._restore()[1]) + 1))

    @property
    def model_prompt_payload(self) -> dict:
        _, rules, records = self._restore()
        return {'schema_version': '1.0', 'task': 'order_all_catalog_ids',
                'facts': [record[2] for record in records],
                # Even vetted source prose is not needed for this model operation.
                'rule_keys': [f'R{number:06d}' for number in range(1, len(rules) + 1)]}

    @property
    def response_schema(self) -> dict:
        return {'type': 'object', 'additionalProperties': False,
                'required': ['schema_version', 'fact_order', 'rule_order'],
                'properties': {'schema_version': {'type': 'string', 'const': '1.0'},
                               'fact_order': _permutation_schema(self.fact_ids),
                               'rule_order': _permutation_schema(self.rule_keys)}}


def _permutation_schema(ids):
    return {'type': 'array', 'minItems': len(ids), 'maxItems': len(ids), 'uniqueItems': True,
            'items': {'type': 'string', 'enum': list(ids)}}


def prepare_generation(context: v1.InvestigationContext, rules: list[v1.ApplicableRule]) -> GenerationRequest:
    context, rules = _inputs(context, rules)
    _catalog(context)  # Fail before a model call if deterministic rendering is not representable.
    return GenerationRequest(_json(context.model_dump(mode='json')),
                             _json([rule.model_dump(mode='json') for rule in rules]))


def _unique_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GenerationViolation('DUPLICATE_RESPONSE_KEY')
        result[key] = value
    return result


def _no_constant(value):
    raise GenerationViolation('NONFINITE_RESPONSE_VALUE')


def _response(raw_response: str, fact_ids, rule_keys):
    if type(raw_response) is not str:
        raise GenerationViolation('INVALID_MODEL_RESPONSE')
    try:
        if len(raw_response.encode('utf-8')) > MAX_RESPONSE_BYTES:
            raise GenerationViolation('MODEL_RESPONSE_TOO_LARGE')
        payload = json.loads(raw_response, object_pairs_hook=_unique_json_keys, parse_constant=_no_constant)
    except GenerationViolation:
        raise
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise GenerationViolation('INVALID_MODEL_JSON') from None
    if (type(payload) is not dict or set(payload) != {'schema_version', 'fact_order', 'rule_order'}
            or payload['schema_version'] != '1.0'):
        raise GenerationViolation('INVALID_MODEL_RESPONSE')
    for field, allowed in (('fact_order', fact_ids), ('rule_order', rule_keys)):
        values = payload[field]
        if (type(values) is not list or any(type(value) is not str for value in values)
                or len(values) != len(allowed) or set(values) != set(allowed)):
            raise GenerationViolation('INCOMPLETE_OR_UNKNOWN_SELECTION')
    return payload


def render_generation(request: GenerationRequest, raw_response: str, *, report_id: str,
                      generated_at: str, report_version: int = 1) -> v1.DraftReport:
    context, rules, records = request._restore()
    facts = {record[0]: record[1] for record in records}
    rule_catalog = {f'R{number:06d}': rule for number, rule in enumerate(rules, 1)}
    payload = _response(raw_response, tuple(facts), tuple(rule_catalog))
    try:
        report = v1.DraftReport.model_validate({
            'schema_version': '1.0', 'status': 'draft', 'incident_id': context.incident_id,
            'report_id': report_id, 'report_version': report_version, 'generated_at': generated_at,
            'snapshot_id': context.snapshot.snapshot_id, 'policy_version': context.policy_version,
            'data_completeness': 'full', 'found_facts': [facts[key].model_dump(mode='json') for key in payload['fact_order']],
            'suspicious_patterns': [], 'applicable_rules': [rule_catalog[key].model_dump(mode='json') for key in payload['rule_order']],
            'confidence': None})
    except (TypeError, ValueError, ValidationError):
        raise GenerationViolation('INVALID_REPORT_METADATA') from None
    validate_generated_report(report, request)
    return report


def validate_generated_report(report: v1.DraftReport, request: GenerationRequest) -> None:
    """Publication guard: exact factual prose + evidence + complete membership.

    Evidence IDs alone cannot validate prose. Only the deterministic renderings
    in this version of the catalog are accepted; arbitrary narrative is refused.
    """
    context, rules, records = request._restore()
    try:
        checked = v1.DraftReport.model_validate(report.model_dump(mode='json'))
        validate_report_context(checked, context)
    except (AttributeError, TypeError, ValueError, ValidationError, ContractViolation):
        raise GenerationViolation('REPORT_BINDING_VIOLATION') from None
    expected_facts = Counter(canonical_digest(record[1].model_dump(mode='json')) for record in records)
    actual_facts = Counter(canonical_digest(fact.model_dump(mode='json')) for fact in checked.found_facts)
    expected_rules = Counter(canonical_digest(rule.model_dump(mode='json')) for rule in rules)
    actual_rules = Counter(canonical_digest(rule.model_dump(mode='json')) for rule in checked.applicable_rules)
    if (actual_facts != expected_facts or actual_rules != expected_rules
            or checked.suspicious_patterns or checked.confidence is not None):
        raise GenerationViolation('REPORT_CATALOG_MISMATCH')
