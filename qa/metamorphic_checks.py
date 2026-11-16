"""Metamorphic controls over copied synthetic data and an injected embedding.

This module never loads models, starts services, or edits the frozen catalogue.
The caller records whether the injected model is real or a hermetic test double.
Relations concern deterministic facts, eligibility and edition selection, not SME
acceptance, legal interpretation, or the semantic quality of an LLM.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import timedelta, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
import json
from pathlib import Path
import re
from types import SimpleNamespace

from qa.scenario_checks import canonical, expected_facts
from qa.scenarios import DEFAULT_FOLDER, load_catalog
from src.config import PROJECT_ROOT
from src.contracts.semantics import validate_report_context
from src.contracts.v1 import ADAPTERS, InvestigationContext, instant
from src.runtime.context import ContextBuilder, FILES
from src.runtime.policy import DemoPolicy
from src.runtime.retrieval import RuleIndex
from src.runtime.service import AMLAgent

ROOT = PROJECT_ROOT
_MEMBERS = (*FILES, 'manifest.json', 'policy.json')
_STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})')
SPECS = (
    {'id': 'MT-01', 'family': 'row_permutation', 'fixture': 'S01', 'incident_id': 'INC-000123',
     'operation': 'Reverse all five source-row lists; update copied hash manifests.',
     'relation': 'Semantic context, complete fact catalogue and citations remain identical.'},
    {'id': 'MT-02', 'family': 'equivalent_timezone', 'fixture': 'S02', 'incident_id': 'INC-000123',
     'operation': 'Represent all timestamps and window endpoints at UTC-04:00 without changing instants.',
     'relation': 'Included/excluded boundary transactions, normalized window, facts and citations remain identical.'},
    {'id': 'MT-03', 'family': 'exact_decimal_amount', 'fixture': 'S02', 'incident_id': 'INC-000123',
     'operation': 'Increase TX-7700 amount by exact Decimal 0.01; preserve all other source fields.',
     'relation': 'Only TX-7700 fact and RUB aggregate change; exact delta 0.01; all other facts and citations preserved.'},
    {'id': 'MT-04', 'family': 'required_data_removed', 'fixture': 'S04', 'incident_id': 'INC-000400',
     'operation': 'Remove only TX-8900, the sole transaction of the selected incident client C-00077.',
     'relation': 'DRAFT becomes INSUFFICIENT_DATA for the selected incident before retrieval.'},
    {'id': 'MT-05', 'family': 'edition_boundary', 'fixture': 'S04', 'incident_id': 'INC-000400',
     'operation': 'Compare 2026-07-19T23:59:59Z with 2026-07-20T00:00:00Z; '
                  'set window end and snapshot capture to the selected instant.',
     'relation': 'R-115-006 changes from 2024-rev3 to qa-2026-rev1 exactly at the exclusive/inclusive boundary.'},
)


def _write(path: Path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')


def _hashes(folder):
    return {name: sha256((folder / name).read_bytes()).hexdigest() for name in _MEMBERS}


def _destination(output_dir):
    raw = Path(output_dir)
    if '..' in raw.parts:
        raise ValueError('METAMORPHIC_OUTPUT_TRAVERSAL')
    root = Path(ROOT).resolve(strict=True)
    target = raw if raw.is_absolute() else root / raw
    relative = target.resolve().relative_to(root)
    if not (relative.parts[:1] == ('verification',) or relative.parts[:2] == ('var', 'qa')):
        raise ValueError('METAMORPHIC_OUTPUT_SCOPE')
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.exists() and (current.is_symlink() or getattr(current.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('METAMORPHIC_OUTPUT_LINK')
    result = target / 'metamorphic'
    if result.exists() or result.is_symlink():
        raise ValueError('METAMORPHIC_OUTPUT_EXISTS')
    return result


def _read_fixture(name):
    source = DEFAULT_FOLDER / 'fixtures' / name
    return {member: json.loads((source / member).read_bytes()) for member in _MEMBERS}


def _seal_fixture(folder, values):
    """Write detached records and rebound hashes; retain explicit frozen lineage."""
    folder.mkdir()
    values = deepcopy(values)
    for member in FILES:
        _write(folder / member, values[member])
    manifest = values['manifest.json']
    manifest['files'] = [{'name': name, 'content_sha256': sha256((folder / name).read_bytes()).hexdigest(),
                          'row_count': len(values[name])} for name in FILES]
    _write(folder / 'manifest.json', manifest)
    policy = values['policy.json']
    policy['snapshot_manifest_sha256'] = sha256((folder / 'manifest.json').read_bytes()).hexdigest()
    _write(folder / 'policy.json', policy)
    return values


def _variants(spec):
    before = _read_fixture(spec['fixture'])
    if spec['family'] == 'edition_boundary':
        stamp = '2026-07-19T23:59:59Z'
        before['policy.json']['window'].update(as_of=stamp, end_exclusive=stamp)
        before['manifest.json']['window'] = deepcopy(before['policy.json']['window'])
        before['manifest.json']['captured_at'] = stamp
    after = deepcopy(before)
    if spec['family'] == 'row_permutation':
        for member in FILES:
            after[member].reverse()
    elif spec['family'] == 'equivalent_timezone':
        def shifted(value):
            return instant(value).astimezone(timezone(-timedelta(hours=4))).isoformat()
        for row in after['cases.json']:
            row['created_at'] = shifted(row['created_at'])
        for row in after['transactions.json']:
            row['timestamp'] = shifted(row['timestamp'])
        after['manifest.json']['captured_at'] = shifted(after['manifest.json']['captured_at'])
        for member in ('policy.json', 'manifest.json'):
            after[member]['window'] = {key: shifted(value) for key, value in after[member]['window'].items()}
    elif spec['family'] == 'exact_decimal_amount':
        tx = next(row for row in after['transactions.json'] if row['tx_id'] == 'TX-7700')
        tx['amount'] = format(Decimal(tx['amount']) + Decimal('0.01'), 'f')
    elif spec['family'] == 'required_data_removed':
        after['transactions.json'] = [row for row in after['transactions.json'] if row['tx_id'] != 'TX-8900']
    elif spec['family'] == 'edition_boundary':
        stamp = '2026-07-20T00:00:00Z'
        after['policy.json']['window'].update(as_of=stamp, end_exclusive=stamp)
        after['manifest.json']['window'] = deepcopy(after['policy.json']['window'])
        after['manifest.json']['captured_at'] = stamp
    return before, after


def _expected_context(values, incident_id):
    """Independent source selection, not the context builder's observed output."""
    case = next(row for row in values['cases.json'] if row['incident_id'] == incident_id)
    client = next(row for row in values['clients.json'] if row['client_id'] == case['client_id'])
    policy, manifest = values['policy.json'], values['manifest.json']
    window = policy['window']
    selected = [deepcopy(row) for row in values['transactions.json']
                if row['client_id'] == case['client_id'] and
                instant(window['start_inclusive']) <= instant(row['timestamp']) < instant(window['end_exclusive'])]
    return InvestigationContext.model_validate({
        'schema_version': '1.0', 'incident_id': incident_id, 'client_id': case['client_id'],
        'snapshot': {'snapshot_id': manifest['snapshot_id'], 'captured_at': manifest['captured_at'],
                     'sources': [{'source': item['name'], 'version': item['content_sha256'],
                                  'content_sha256': item['content_sha256']} for item in manifest['files']]},
        'window': window, 'policy_version': policy['policy_id'], 'data_completeness': 'full',
        'consistency': 'consistent', 'missing_fields': [], 'client': client,
        'cards': [row for row in values['cards.json'] if row['client_id'] == case['client_id']],
        'transactions': selected})


def _expected_facts(context):
    totals = []
    for currency in sorted({tx.currency for tx in context.transactions}):
        members = sorted((tx for tx in context.transactions if tx.currency == currency), key=lambda tx: tx.tx_id)
        with localcontext() as decimal_context:
            decimal_context.prec = 170
            total = sum((Decimal(tx.amount) for tx in members), Decimal(0))
        totals.append(SimpleNamespace(currency=currency, value=format(total, 'f'), tx_ids=[tx.tx_id for tx in members]))
    # Reuse the separately maintained QA wording oracle; never call the renderer.
    return expected_facts(SimpleNamespace(case=SimpleNamespace(context=context), golden=SimpleNamespace(
        transaction_ids=sorted(tx.tx_id for tx in context.transactions), sums=totals)))


def _normalize(value):
    """Normalize representations, never amount, version, membership or prose."""
    if isinstance(value, str):
        return _STAMP.sub(lambda match: instant(match[0]).astimezone(timezone.utc).isoformat(), value)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    return value


def _facts(report):
    result = []
    for fact in report.get('found_facts', []):
        copied = _normalize(deepcopy(fact))
        for evidence in copied.get('evidence', []):
            if 'tx_ids' in evidence:
                evidence['tx_ids'].sort()
        copied['evidence'].sort(key=canonical)
        copied['sources'].sort(key=canonical)
        result.append(canonical(copied))
    return Counter(result)


def _rules(report):
    return Counter(canonical(rule) for rule in report.get('applicable_rules', []))


def check_observation(values, incident_id, observed, *, refusal=False):
    """Reject invalid outputs independently of the metamorphic relation itself."""
    result, context = observed['result'], observed['context']
    issues = []
    try:
        parsed = ADAPTERS['InvestigationResult'].validate_python(result)
    except (TypeError, ValueError):
        return ['RESULT_CONTRACT_INVALID']
    if refusal:
        if result.get('status') != 'refusal' or result.get('reason_code') != 'INSUFFICIENT_DATA':
            issues.append('REQUIRED_DATA_REFUSAL_MISSING')
        if result.get('incident_id') != incident_id:
            issues.append('WRONG_INCIDENT')
        if context is not None or observed['ranking']:
            issues.append('INCOMPLETE_DATA_REACHED_RETRIEVAL')
        return issues
    if result.get('status') != 'draft':
        return ['EXPECTED_DRAFT']
    expected = _expected_context(values, incident_id)
    if context != expected.model_dump(mode='json'):
        issues.append('SOURCE_CONTEXT_MISMATCH')
    try:
        validate_report_context(parsed, expected)
    except (TypeError, ValueError):
        issues.append('REPORT_CONTEXT_INVALID')
    if _facts(result) != _facts({'found_facts': _expected_facts(expected)}):
        issues.append('INDEPENDENT_FACT_ORACLE_MISMATCH')
    if result['suspicious_patterns'] != [] or result['confidence'] is not None:
        issues.append('UNSUPPORTED_MODEL_CLAIM')
    case = next(row for row in values['cases.json'] if row['incident_id'] == incident_id)
    allowed = set(values['policy.json']['applicability'][case['alert_type']])
    as_of_date = instant(expected.window.as_of).astimezone(timezone.utc).date().isoformat()
    active = {row['rule_id']: row for row in values['rules.json'] if row['rule_id'] in allowed
              and row['effective_from'] <= as_of_date and
              (not row.get('effective_until') or as_of_date < row['effective_until'])}
    actual = result.get('applicable_rules', [])
    if {rule['citation']['rule_id'] for rule in actual} != allowed or set(active) != allowed:
        issues.append('RULE_ELIGIBILITY_MISMATCH')
    for rule in actual:
        citation, text = rule['citation'], rule['rule_text']
        source = active.get(citation['rule_id'])
        if (source is None or citation['rule_version'] != source['version'] or
                citation['source_ref'] != source['regulation_ref'] or text not in source['text'] or
                sha256(text.encode('utf-8')).hexdigest() != citation['content_sha256']):
            issues.append('RULE_SOURCE_OR_VERSION_MISMATCH')
    # All selected chunks must be emitted once; source dates/IDs/text are checked
    # independently above, while this checks the actual runtime selection flow.
    candidates = [item for search in observed['ranking'] for item in search['chunks']
                  if item['citation']['rule_id'] in allowed]
    if Counter(canonical({'citation': item['citation'], 'rule_text': item['text']}) for item in candidates) != _rules(result):
        issues.append('SELECTED_CHUNK_LOST_OR_DUPLICATED')
    return sorted(set(issues))


def check_relation(spec, before, after):
    """Separate relational gate: both individually valid observations can differ wrongly."""
    family = spec['family']
    left, right = before['result'], after['result']
    if family == 'required_data_removed':
        return [] if (left.get('status') == 'draft' and right.get('status') == 'refusal'
                      and right.get('reason_code') == 'INSUFFICIENT_DATA') else ['REQUIRED_DATA_RELATION_FAILED']
    if left.get('status') != 'draft' or right.get('status') != 'draft':
        return ['DRAFT_PAIR_REQUIRED']
    issues = []
    if family in {'row_permutation', 'equivalent_timezone'}:
        if _facts(left) != _facts(right):
            issues.append('FACT_INVARIANCE_FAILED')
        if _rules(left) != _rules(right):
            issues.append('CITATION_INVARIANCE_FAILED')
        def semantic_context(value):
            value = _normalize(deepcopy(value))
            value['snapshot'].pop('sources', None)  # Physical row representation changes hashes.
            for name in ('cards', 'transactions'):
                value[name].sort(key=canonical)
            return value
        if semantic_context(before['context']) != semantic_context(after['context']):
            issues.append('CONTEXT_INVARIANCE_FAILED')
    elif family == 'exact_decimal_amount':
        lost, gained = _facts(left) - _facts(right), _facts(right) - _facts(left)
        def kind_keys(counter):
            keys = []
            for encoded, count in counter.items():
                evidence = json.loads(encoded)['evidence'][0]
                keys.extend([(evidence['kind'], evidence.get('tx_id', evidence.get('currency')))] * count)
            return sorted(keys)
        expected = [('sum', 'RUB'), ('transaction', 'TX-7700')]
        if kind_keys(lost) != expected or kind_keys(gained) != expected:
            issues.append('AMOUNT_CHANGED_UNEXPECTED_FACTS')
        def total(report):
            return next(Decimal(e['value']) for fact in report['found_facts'] for e in fact['evidence']
                        if e['kind'] == 'sum' and e['currency'] == 'RUB')
        if total(right) - total(left) != Decimal('0.01'):
            issues.append('AMOUNT_DELTA_NOT_EXACT')
        if _rules(left) != _rules(right):
            issues.append('AMOUNT_CHANGED_CITATIONS')
    elif family == 'edition_boundary':
        def editions(report):
            return {(rule['citation']['rule_id'], rule['citation']['rule_version']) for rule in report['applicable_rules']}
        if editions(left) != {('R-115-006', '2024-rev3')} or editions(right) != {('R-115-006', 'qa-2026-rev1')}:
            issues.append('EDITION_BOUNDARY_FAILED')
        if {row['tx_id'] for row in before['context']['transactions']} != {row['tx_id'] for row in after['context']['transactions']}:
            issues.append('EDITION_TEST_CHANGED_TRANSACTION_MEMBERSHIP')
    return issues


def _observe(folder, incident_id, embedding, digest, settings):
    policy = DemoPolicy.model_validate_json((folder / 'policy.json').read_bytes())
    recorded = {'context': None, 'ranking': []}
    builder = ContextBuilder(folder, policy)

    class RecordingBuilder:
        def build(self, identifier):
            prepared = builder.build(identifier)
            recorded['context'] = prepared.context.model_dump(mode='json')
            return prepared

    class RecordingIndex:
        def __init__(self, path):
            raw = path.read_bytes()
            self.index = RuleIndex.from_records(json.loads(raw), embedding=embedding,
                embedding_manifest_digest=digest, source_sha256=sha256(raw).hexdigest())
            self.provenance = self.index.provenance

        def active_chunk_count(self, as_of):
            return self.index.active_chunk_count(as_of)

        def search(self, query, *, as_of, k):
            result = self.index.search(query, as_of=as_of, k=k)
            recorded['ranking'].append({'query': query, 'as_of': as_of, 'requested_k': k,
                'provenance': self.provenance, 'chunks': [
                    {'citation': chunk.citation.model_dump(mode='json'), 'text': chunk.text,
                     'distance': chunk.distance, 'token_count': chunk.token_count,
                     'char_start': chunk.char_start, 'char_end': chunk.char_end} for chunk in result]})
            return result

    agent = AMLAgent(mode='demo', engine='template', policy=policy, builder=RecordingBuilder(),
                     index_factory=RecordingIndex, settings=settings)
    recorded['result'] = agent.run(incident_id)
    recorded['events'] = [event.to_dict() for event in agent.last_events]
    return recorded


def run_metamorphic(output_dir: Path, embedding, embedding_manifest_digest: str, settings) -> dict:
    """Run five declared transformation families with caller-owned local embedding.

    Create a fresh ``metamorphic`` child under verification/ or var/qa/; never
    overwrite evidence. Input/manifest errors fail before evaluating any case.
    """
    if (not callable(embedding) or not isinstance(embedding_manifest_digest, str) or
            re.fullmatch(r'[0-9a-f]{64}', embedding_manifest_digest) is None):
        raise ValueError('METAMORPHIC_EMBEDDING_INPUT_INVALID')
    destination = _destination(output_dir)
    catalog = load_catalog()
    frozen_manifest = sha256((DEFAULT_FOLDER / 'manifest.json').read_bytes()).hexdigest()
    lineage = {name: _hashes(DEFAULT_FOLDER / 'fixtures' / name) for name in ('S01', 'S02', 'S04')}
    destination.mkdir(parents=True)
    _write(destination / 'specifications.json', {'schema_version': 1, 'dataset_version': catalog.dataset_version,
        'dataset_manifest_sha256': frozen_manifest, 'embedding_manifest_sha256': embedding_manifest_digest,
        'engine': 'template', 'lineage': lineage, 'families': list(SPECS),
        'scope': 'Synthetic metamorphic relations; injected embedding provenance is recorded by caller; '
                 'not model ranking quality, LLM quality, SME acceptance, or independent heldout samples.'})
    outcomes = []
    observation_count = 0
    for spec in SPECS:
        folder = destination / spec['id']
        folder.mkdir()
        row = {'id': spec['id'], 'family': spec['family'], 'status': 'ERROR', 'issues': []}
        try:
            variants = _variants(spec)
            observations, inputs = [], []
            for label, values in zip(('before', 'after'), variants):
                snapshot = folder / label
                inputs.append(_seal_fixture(snapshot, values))
                observed = _observe(snapshot, spec['incident_id'], embedding, embedding_manifest_digest, settings)
                observation_count += 1
                observations.append(observed)
                _write(folder / (label + '-observation.json'), observed)
                issues = check_observation(inputs[-1], spec['incident_id'], observed,
                    refusal=spec['family'] == 'required_data_removed' and label == 'after')
                row['issues'].extend(label.upper() + ':' + issue for issue in issues)
            if not row['issues']:
                row['issues'].extend(check_relation(spec, *observations))
            _write(folder / 'inputs.json', {'before': _hashes(folder / 'before'), 'after': _hashes(folder / 'after'),
                'changed_members': [name for name in _MEMBERS if (folder / 'before' / name).read_bytes()
                                    != (folder / 'after' / name).read_bytes()]})
            row['status'] = 'FAIL' if row['issues'] else 'PASS'
        except Exception as exc:
            # Full stack/private paths are deliberately not persisted as QA payload.
            row['issues'] = ['METAMORPHIC_EXECUTION_ERROR:' + type(exc).__name__]
        _write(folder / 'result.json', row)
        outcomes.append(row)
    load_catalog()  # Complete frozen-package hash validation after execution too.
    if sha256((DEFAULT_FOLDER / 'manifest.json').read_bytes()).hexdigest() != frozen_manifest:
        raise ValueError('FROZEN_DATASET_CHANGED')
    status = 'ERROR' if any(row['status'] == 'ERROR' for row in outcomes) else (
        'PASS' if all(row['status'] == 'PASS' for row in outcomes) else 'FAIL')
    summary = {'schema_version': 1, 'status': status, 'family_count': len(SPECS),
               'passed_families': sum(row['status'] == 'PASS' for row in outcomes),
               'observation_count': observation_count, 'expected_observation_count': len(SPECS) * 2, 'cases': outcomes,
               'dataset_manifest_sha256': frozen_manifest,
               'embedding_manifest_sha256': embedding_manifest_digest,
               'artifact_directory': destination.relative_to(Path(ROOT).resolve()).as_posix(),
               'frozen_dataset_unchanged': True, 'engine': 'template'}
    _write(destination / 'summary.json', summary)
    return summary
