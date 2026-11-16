"""Hermetic qualification of versioned examples; no live models or services.

The source reader, edition/chunk logic, agent, renderer and contracts are real.
Tokenizer, vectors and Ollama are explicit local doubles. This does not measure ranking,
model quality, infrastructure availability, browser behavior or AML acceptance.
"""
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace

from src.config import get_settings
from src.contracts.semantics import validate_report_context
from src.contracts.v1 import ADAPTERS
from src.runtime.context import ContextBuilder
from src.runtime.policy import DemoPolicy
from src.runtime.retrieval import RuleIndex
from src.runtime.service import AMLAgent
from src.runtime.transport import TransportFailure


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _decoded_strings(value):
    if isinstance(value, str):
        yield value.casefold()
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key).casefold()
            yield from _decoded_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _decoded_strings(item)


def _contains_forbidden(value, forbidden):
    strings = tuple(_decoded_strings(value))
    return any(token.casefold() in text for token in forbidden for text in strings)


class _Tokenizer:
    def num_special_tokens_to_add(self, pair=False):
        return 2

    def __call__(self, text, *, add_special_tokens, truncation, return_offsets_mapping=False):
        words = list(re.finditer(r'\S+', text))
        ids = list(range(len(words)))
        result = {'input_ids': [-1, *ids, -2] if add_special_tokens else ids}
        if return_offsets_mapping:
            result['offset_mapping'] = [(word.start(), word.end()) for word in words]
        return result


class _SyntheticEmbedding:
    _model = SimpleNamespace(tokenizer=_Tokenizer(), max_seq_length=4096)

    def __call__(self, texts):
        # Equal vectors deliberately provide no semantic ranking evidence.
        return [[1.0, 1.0] for _text in texts]


class _RecordedChunk:
    def __init__(self, chunk, selected):
        self.chunk, self.selected = chunk, selected
        self.citation = chunk.citation

    def as_applicable(self):
        self.selected.append(self.citation.rule_id)
        return self.chunk.as_applicable()


class _RecordedIndex:
    def __init__(self, path, selected):
        raw = path.read_bytes()
        self.index = RuleIndex.from_records(
            json.loads(raw), embedding=_SyntheticEmbedding(),
            embedding_manifest_digest=sha256(b'qa-synthetic-equal-vectors-v1').hexdigest(),
            source_sha256=sha256(raw).hexdigest())
        self.selected = selected
        self.provenance = self.index.provenance

    def active_chunk_count(self, as_of):
        return self.index.active_chunk_count(as_of)

    def search(self, query, *, as_of, k):
        return [_RecordedChunk(chunk, self.selected)
                for chunk in self.index.search(query, as_of=as_of, k=k)]


class _Transport:
    def __init__(self, dependency):
        self.dependency, self.prompts = dependency, []

    def complete(self, payload, schema, *, deadline):
        self.prompts.append(json.loads(canonical(payload)))
        if self.dependency == 'unavailable':
            raise TransportFailure('DEPENDENCY_UNAVAILABLE')
        if self.dependency == 'invalid_selection':
            return canonical({'schema_version': '1.0', 'fact_order': ['QA-INVENTED-FACT'],
                              'rule_order': payload['rule_keys']})
        # Reversal checks that valid ordering variation retains every fact/rule.
        return canonical({'schema_version': '1.0',
                          'fact_order': [item['fact_id'] for item in reversed(payload['facts'])],
                          'rule_order': list(reversed(payload['rule_keys']))})


class _Builder:
    def __init__(self, folder, policy):
        self.builder = ContextBuilder(folder, policy)
        self.context = None

    def build(self, incident_id):
        prepared = self.builder.build(incident_id)
        self.context = prepared.context.model_dump(mode='json')
        return prepared


@dataclass
class Observation:
    result: dict
    context: dict | None
    selected_rule_ids: list[str]
    model_prompts: list[dict]
    latency_seconds: float
    events: list[dict]


def run_scenario(scenario, folder: Path) -> Observation:
    """Called only after load_catalog has validated immutable fixture hashes."""
    policy = DemoPolicy.model_validate(json.loads((folder / 'policy.json').read_bytes()))
    builder = _Builder(folder, policy)
    selected = []
    transport = _Transport(scenario.dependency)
    agent = AMLAgent(mode=scenario.mode, engine=scenario.engine, builder=builder, policy=policy,
                     settings=get_settings(), transport=transport,
                     index_factory=lambda path: _RecordedIndex(path, selected))
    start = time.perf_counter()
    result = agent.run(scenario.case.incident_id)
    elapsed = time.perf_counter() - start
    return Observation(result, builder.context, selected, transport.prompts, elapsed,
                       [event.to_dict() for event in agent.last_events])


def expected_facts(scenario):
    """Text oracle from frozen inputs and hand-calculated totals, never renderer output.

    This intentionally fixes the current factual wording; a wording change needs
    an explained dataset revision. Fact/evidence order is not part of the oracle.
    """
    context = scenario.case.context
    golden = scenario.golden
    snapshot = context.snapshot.snapshot_id
    facts = []
    labels = {'risk_category': 'Категория риска в профиле',
              'kyc_status': 'Статус KYC в профиле', 'client_since': 'Дата начала отношений в профиле'}
    for field, label in labels.items():
        facts.append({'fact': f'{label}: {getattr(context.client, field)}.', 'sources': [],
                      'evidence': [{'kind': 'profile', 'snapshot_id': snapshot,
                                    'client_id': context.client_id, 'field': field}]})
    by_id = {tx.tx_id: tx for tx in context.transactions}
    for tx_id in golden.transaction_ids:
        tx = by_id[tx_id]
        facts.append({'fact': f'Транзакция {tx_id}: сумма {tx.amount} {tx.currency}; время {tx.timestamp}; '
                      f'статус {tx.status}; канал {tx.channel}.', 'sources': [],
                      'evidence': [{'kind': 'transaction', 'snapshot_id': snapshot, 'tx_id': tx_id, 'field': field}
                                   for field in ('amount', 'currency', 'timestamp', 'status', 'channel')]})
    window = context.window.model_dump(mode='json')
    label = f'[{context.window.start_inclusive}, {context.window.end_exclusive})'
    count = len(golden.transaction_ids)
    facts.append({'fact': f'Количество всех транзакций в контексте за окно {label}, включая все статусы: {count}.',
                  'sources': [], 'evidence': [{'kind': 'count', 'snapshot_id': snapshot,
                    'tx_ids': golden.transaction_ids, 'window': window, 'value': count}]})
    for total in golden.sums:
        facts.append({'fact': f'Сумма всех транзакций в контексте в валюте {total.currency} за окно {label}, '
                      f'включая все статусы: {total.value} {total.currency}.', 'sources': [],
                      'evidence': [{'kind': 'sum', 'snapshot_id': snapshot, 'tx_ids': total.tx_ids,
                                    'field': 'amount', 'window': window, 'currency': total.currency,
                                    'value': total.value}]})
    return facts


def _fact_key(fact):
    copied = json.loads(canonical(fact))
    for evidence in copied.get('evidence', []):
        if 'tx_ids' in evidence:
            evidence['tx_ids'].sort()
    copied['evidence'] = sorted(copied.get('evidence', []), key=canonical)
    copied['sources'] = sorted(copied.get('sources', []), key=canonical)
    return canonical(copied)


def check_observation(scenario, observed: Observation) -> list[str]:
    """Return explicit failures; never rewrite goldens to fit observed results."""
    issues = []
    result = observed.result
    try:
        parsed = ADAPTERS['InvestigationResult'].validate_python(result)
    except (TypeError, ValueError):
        return ['RESULT_CONTRACT_INVALID']
    expected = scenario.case
    if result['status'] != expected.expected_status:
        issues.append('WRONG_STATUS')
    actual_reason = result.get('reason_code', result.get('code'))
    if actual_reason != expected.expected_reason_code:
        issues.append('WRONG_REASON')
    if result['status'] != 'error' and result.get('incident_id') != expected.incident_id:
        issues.append('WRONG_INCIDENT')
    if _contains_forbidden(result, expected.forbidden_outputs):
        issues.append('FORBIDDEN_OUTPUT')
    if len(observed.model_prompts) != scenario.expected_model_calls:
        issues.append('MODEL_CALL_COUNT')
    if _contains_forbidden(observed.model_prompts, scenario.forbidden_prompt_strings):
        issues.append('UNTRUSTED_TEXT_IN_MODEL_PAYLOAD')
    expected_context = expected.context.model_dump(mode='json') if expected.context else None
    if observed.context != expected_context:
        issues.append('CONTEXT_MISMATCH')
    if expected.expected_retrieval is not None:
        if set(observed.selected_rule_ids) != set(expected.expected_retrieval.ids):
            issues.append('SELECTED_RULES_MISMATCH')
    if result['status'] == 'draft':
        if not scenario.golden or not expected.context:
            issues.append('UNEXPECTED_DRAFT')
        else:
            try:
                validate_report_context(parsed, expected.context)
            except (TypeError, ValueError):
                issues.append('EVIDENCE_CONTEXT_INVALID')
            if len(result['found_facts']) != scenario.golden.fact_count:
                issues.append('FACT_COUNT_MISMATCH')
            if Counter(map(_fact_key, result['found_facts'])) != Counter(map(_fact_key, expected_facts(scenario))):
                issues.append('FACT_CATALOG_MISMATCH')
            expected_rules = [rule.model_dump(mode='json') for rule in scenario.golden.rules]
            if Counter(map(canonical, result['applicable_rules'])) != Counter(map(canonical, expected_rules)):
                issues.append('RULE_CATALOG_MISMATCH')
            if result['suspicious_patterns'] != [] or result['confidence'] is not None:
                issues.append('UNSUPPORTED_MODEL_CLAIM')
    return issues
