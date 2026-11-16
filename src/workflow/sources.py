"""Resolve stored report references to exact cells/editions without model loading.

Only the current hash-pinned synthetic snapshot is supported. This read-only
viewer must be called after report authorization; it never accepts a file path.
"""
from hashlib import sha256

from src.config import get_demo_paths, get_settings
from src.contracts.semantics import canonical_digest
from src.contracts.v1 import DraftReport, instant
from src.runtime.context import ContextBuilder
from src.runtime.generation import prepare_generation, validate_generated_report
from src.runtime.policy import load_demo_policy, strict_json
from .store import WorkflowError


def resolve_report_sources(payload):
    try:
        return _resolve(DraftReport.model_validate(payload))
    except Exception:
        # No paths, source text or internal exception details in public errors.
        raise WorkflowError('NOT_READY') from None


def _resolve(report):
    root = get_settings().project_root
    policy_path, snapshot_dir = get_demo_paths(root)
    policy = load_demo_policy(policy_path)
    builder = ContextBuilder(snapshot_dir, policy)
    prepared = builder.build(report.incident_id)
    context = prepared.context
    validate_generated_report(report, prepare_generation(context, report.applicable_rules))
    versions = {item.source: item.content_sha256 for item in context.snapshot.sources}
    rows = {}
    for file in ('clients.json', 'transactions.json', 'rules.json'):
        raw = builder._bytes(file)
        if sha256(raw).hexdigest() != versions[file]:
            raise ValueError('SOURCE_CHANGED')
        rows[file] = strict_json(raw)

    def cell(file, key, identifier, field):
        matches = [(index, row) for index, row in enumerate(rows[file]) if row[key] == identifier]
        if len(matches) != 1:
            raise ValueError('SOURCE_NOT_UNIQUE')
        index, row = matches[0]
        return {'source_file': file, 'content_sha256': versions[file], 'record_id': identifier,
                'json_pointer': '/' + str(index) + '/' + field, 'field': field, 'value': row[field]}

    facts = []
    for fact in report.found_facts:
        resolved = []
        for evidence in fact.evidence:
            if evidence.kind == 'profile':
                resolved.append({'kind': evidence.kind, 'cells': [cell('clients.json', 'client_id', evidence.client_id, evidence.field)]})
            elif evidence.kind == 'transaction':
                resolved.append({'kind': evidence.kind, 'cells': [cell('transactions.json', 'tx_id', evidence.tx_id, evidence.field)]})
            else:
                fields = ('tx_id', 'amount', 'currency', 'timestamp', 'status') if evidence.kind == 'sum' else ('tx_id', 'timestamp', 'status')
                resolved.append({'kind': evidence.kind, 'value': evidence.value,
                    'window': evidence.window.model_dump(mode='json'), 'all_statuses': True,
                    'cells': [cell('transactions.json', 'tx_id', tx_id, field)
                              for tx_id in evidence.tx_ids for field in fields]})
        facts.append(resolved)

    rules = []
    for rule in report.applicable_rules:
        citation = rule.citation
        matches = [(index, row) for index, row in enumerate(rows['rules.json'])
                   if row['rule_id'] == citation.rule_id and row['version'] == citation.rule_version]
        if len(matches) != 1 or citation.rule_id not in policy.applicability[prepared.alert_type]:
            raise ValueError('RULE_NOT_RESOLVED')
        index, row = matches[0]
        as_of_date = instant(context.window.as_of).date().isoformat()
        if (row['regulation_ref'] != citation.source_ref or row['effective_from'] > as_of_date
                or (row.get('effective_until') is not None and as_of_date >= row['effective_until'])
                or sha256(rule.rule_text.encode('utf-8')).hexdigest() != citation.content_sha256):
            raise ValueError('EDITION_MISMATCH')
        # The exact character range is part of the existing chunk identity.
        # Searching verified text does not recreate embeddings or guess a chunk.
        start, ranges = 0, []
        while True:
            start = row['text'].find(rule.rule_text, start)
            if start < 0:
                break
            end = start + len(rule.rule_text)
            identity = {'rule_id': citation.rule_id, 'version': citation.rule_version,
                        'source_ref': citation.source_ref, 'char_start': start, 'char_end': end,
                        'text_sha256': citation.content_sha256}
            if 'chunk-' + canonical_digest(identity) == citation.chunk_id:
                ranges.append((start, end))
            start += 1
        if len(ranges) != 1:
            raise ValueError('CHUNK_NOT_RESOLVED')
        rules.append({'source_file': 'rules.json', 'content_sha256': versions['rules.json'],
                      'json_pointer': '/' + str(index) + '/text', 'rule_id': row['rule_id'],
                      'rule_version': row['version'], 'source_ref': row['regulation_ref'],
                      'effective_from': row['effective_from'], 'effective_until': row.get('effective_until'),
                      'char_start': ranges[0][0], 'char_end': ranges[0][1],
                      'selected_text': rule.rule_text, 'edition_text': row['text']})
    return {'snapshot_id': report.snapshot_id, 'facts': facts, 'rules': rules}
