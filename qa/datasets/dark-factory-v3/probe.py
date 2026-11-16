"""Open from-scratch synthetic runtime scenarios, with fixed manual expectations.

Uses real ContextBuilder, RuleIndex and AMLAgent with equal-vector embedding and
a recording fake transport; engine is template and no real model is called.
Hashes protect local fixture consistency, not against an actor changing code and data.
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from qa.scenario_checks import _Builder, _SyntheticEmbedding, _Transport
from src.config import get_settings
from src.contracts.semantics import validate_report_context
from src.contracts.v1 import ADAPTERS, InvestigationContext
from src.qa_profile import checked_path
from src.runtime.policy import DemoPolicy
from src.runtime.retrieval import RuleIndex
from src.runtime.service import AMLAgent

CATALOG = Path(__file__).with_name('cases.json')
CATALOG_SHA256 = '9f249d3360bbaf6acca04be567933f66aba72acbfaacfa58445531f42cba7e2c'
FILES = ('cases.json', 'clients.json', 'cards.json', 'transactions.json', 'rules.json')


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def digest(data):
    return sha256(data).hexdigest()


def _pairs(items):
    output = {}
    for key, value in items:
        if key in output:
            raise ValueError('DUPLICATE_JSON_KEY')
        output[key] = value
    return output


def load(path=CATALOG):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('INVALID_CATALOG')
    raw = path.read_bytes()
    if digest(raw) != CATALOG_SHA256:
        raise ValueError('CATALOG_HASH_MISMATCH')
    data = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('INVALID_NUMBER')))
    if (data.get('schema_version') != '1.0' or data.get('dataset_version') != '3.0.0-candidate.1'
            or data.get('purpose') != 'synthetic_learning_only' or data.get('split') != 'open_development'
            or [c.get('id') for c in data.get('cases', [])] != ['DF3-NO-WINDOW', 'DF3-CURRENCIES', 'DF3-FOREIGN-CARD']):
        raise ValueError('INVALID_CATALOG_METADATA')
    return data


def materialize(catalog, case, output):
    """Construct isolated versioned snapshot files from authored source, not product output."""
    output.mkdir()
    records = {name: case['input'][name.removesuffix('.json')] for name in FILES if name != 'rules.json'}
    records['rules.json'] = catalog['rules']
    descriptors = []
    for name in FILES:
        raw = encoded(records[name])
        (output / name).write_bytes(raw)
        descriptors.append({'name': name, 'content_sha256': digest(raw), 'row_count': len(records[name])})
    source_sha = digest(encoded({'input': case['input'], 'rules': catalog['rules'], 'window': catalog['window']}))
    manifest = {'schema_version': '1.0', 'snapshot_id': case['id'], 'captured_at': catalog['window']['as_of'],
                'purpose': 'synthetic_learning_only', 'policy_id': case['id'] + '-policy',
                'assumed_naive_timezone': 'UTC', 'window': catalog['window'],
                'original_sources': [{'source': 'v3-authored-input.json', 'version': '3.0.0-candidate.1',
                                      'content_sha256': source_sha}], 'files': descriptors}
    manifest_raw = encoded(manifest)
    (output / 'manifest.json').write_bytes(manifest_raw)
    policy = {'schema_version': '1.0', 'policy_id': manifest['policy_id'], 'purpose': 'synthetic_learning_only',
              'expert_approved': False, 'assumed_naive_timezone': 'UTC', 'snapshot_id': manifest['snapshot_id'],
              'snapshot_manifest_sha256': digest(manifest_raw), 'window': catalog['window'],
              'max_snapshot_age_seconds': 0, 'currency_scales': {'EUR': 2, 'JPY': 0},
              'applicability': {'threshold_breach': ['SYN-V3-RULE']}}
    (output / 'policy.json').write_bytes(encoded(policy))
    return policy, {name: digest((output / name).read_bytes()) for name in (*FILES, 'manifest.json', 'policy.json')}


def observe(catalog, case, output):
    policy_doc, hashes = materialize(catalog, case, output)
    policy = DemoPolicy.model_validate(policy_doc)
    builder = _Builder(output, policy)
    transport = _Transport('healthy')
    index_calls = []
    def factory(path):
        index_calls.append(True)
        raw = path.read_bytes()
        return RuleIndex.from_records(json.loads(raw), embedding=_SyntheticEmbedding(),
             embedding_manifest_digest=digest(b'v3-fake-equal-vectors'), source_sha256=digest(raw))
    agent = AMLAgent(mode='demo', engine='template', builder=builder, policy=policy,
                     settings=get_settings(), transport=transport, index_factory=factory)
    result = agent.run(case['incident_id'])
    after = {name: digest((output / name).read_bytes()) for name in (*FILES, 'manifest.json', 'policy.json')}
    return {'result': result, 'context': builder.context, 'model_calls': len(transport.prompts),
            'index_calls': len(index_calls), 'snapshot_sha256': hashes, 'snapshot_preserved': hashes == after}


def assess(catalog, case, observed):
    """Compare observed outcomes with authored oracle; do not manufacture missing data."""
    if type(observed) is not dict or type(observed.get('result')) is not dict:
        return {'id': case['id'], 'status': 'INCOMPLETE', 'issues': ['OBSERVATION_MISSING']}
    result, golden = observed['result'], case['expected']
    issues = []
    if observed.get('snapshot_preserved') is not True:
        issues.append('SNAPSHOT_NOT_PRESERVED')
    try:
        ADAPTERS['InvestigationResult'].validate_python(result)
    except (TypeError, ValueError):
        issues.append('RESULT_CONTRACT_INVALID')
    if result.get('status') != golden['status'] or result.get('reason_code') != golden['reason_code']:
        issues.append('OUTCOME_MISMATCH')
    if result.get('incident_id') != case['incident_id']:
        issues.append('INCIDENT_MISMATCH')
    if observed.get('model_calls') != golden['model_calls'] or observed.get('index_calls') != golden['index_calls']:
        issues.append('DEPENDENCY_CALL_MISMATCH')
    if golden['status'] == 'draft':
        try:
            context = InvestigationContext.model_validate(observed['context'])
            parsed = ADAPTERS['InvestigationResult'].validate_python(result)
            validate_report_context(parsed, context)
            expected_tx = {row['tx_id']: row for row in case['input']['transactions']}
            if {tx.tx_id for tx in context.transactions} != set(golden['transaction_ids']):
                issues.append('TRANSACTION_SET_MISMATCH')
            if any(tx.model_dump(mode='json') != expected_tx.get(tx.tx_id) for tx in context.transactions):
                issues.append('TRANSACTION_VALUE_MISMATCH')
            facts = result['found_facts']
            if len(facts) != golden['fact_count']:
                issues.append('FACT_COUNT_MISMATCH')
            sums = []
            counts = []
            window = '[2026-02-01T00:00:00Z, 2026-02-10T00:00:00Z)'
            for fact in facts:
                for item in fact['evidence']:
                    if item['kind'] == 'sum':
                        sums.append((item['currency'], item['value'], tuple(sorted(item['tx_ids'])), fact['fact']))
                    elif item['kind'] == 'count':
                        counts.append((item['value'], tuple(sorted(item['tx_ids']))))
            expected_sums = []
            for currency, total in golden['sums'].items():
                expected_sums.append((currency, total['value'], tuple(sorted(total['tx_ids'])),
                    f"Сумма всех транзакций в контексте в валюте {currency} за окно {window}, включая все статусы: {total['value']} {currency}."))
            if Counter(sums) != Counter(expected_sums):
                issues.append('SUM_MISMATCH')
            if counts != [(len(golden['transaction_ids']), tuple(sorted(golden['transaction_ids'])))]:
                issues.append('COUNT_MISMATCH')
            rules = result['applicable_rules']
            if (len(rules) != 1 or rules[0]['citation']['rule_id'] != golden['rule_id']
                    or rules[0]['citation']['rule_version'] != golden['rule_version']
                    or rules[0]['citation']['source_ref'] != golden['source_ref']
                    or rules[0]['citation']['content_sha256'] != digest(catalog['rules'][0]['text'].encode('utf-8'))
                    or rules[0]['rule_text'] != catalog['rules'][0]['text']):
                issues.append('RULE_MISMATCH')
        except (TypeError, ValueError, KeyError, IndexError):
            issues.append('CONTRACT_OR_EVIDENCE_INVALID')
    return {'id': case['id'], 'family': case['family'], 'status': 'FAIL' if issues else 'PASS', 'issues': issues}


def run(catalog, output):
    rows = []
    for case in catalog['cases']:
        try:
            observed = observe(catalog, case, output / case['id'])
            row = assess(catalog, case, observed)
            row['snapshot_sha256'] = observed['snapshot_sha256']
        except Exception as exc:
            row = {'id': case['id'], 'family': case['family'], 'status': 'INCOMPLETE',
                   'issues': ['EXECUTION_ERROR'], 'error_type': type(exc).__name__}
        rows.append(row)
    statuses = [row['status'] for row in rows]
    overall = 'FAIL' if 'FAIL' in statuses else 'INCOMPLETE' if 'INCOMPLETE' in statuses else 'PASS'
    return {'status': overall, 'cases': rows, 'catalog_sha256': CATALOG_SHA256,
            'scope': 'Three open, newly authored synthetic scenarios. No statistical independence claimed.',
            'embedding': 'FAKE_EQUAL_VECTORS', 'transport': 'FAKE_NOT_CALLED', 'engine': 'template',
            'real_llm': 'NOT_RUN', 'sme_acceptance': 'INCOMPLETE', 'human_acceptance': 'INCOMPLETE',
            'product_acceptance': 'INCOMPLETE', 'hidden_holdout': 'NOT_ESTABLISHED'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    try:
        catalog = load()
        out = checked_path(ROOT / args.output_dir, ROOT)
        parts = out.relative_to(ROOT).parts
        if len(parts) != 4 or parts[:3] != ('var', 'qa', 'dark-factory-v3'):
            raise ValueError('INVALID_OUTPUT_DIR')
        out.mkdir(parents=True, exist_ok=False)
        sources_before = {'probe.py': digest(Path(__file__).read_bytes()), 'cases.json': digest(CATALOG.read_bytes())}
        result = run(catalog, out)
        sources_after = {'probe.py': digest(Path(__file__).read_bytes()), 'cases.json': digest(CATALOG.read_bytes())}
        result['source_sha256_before'] = sources_before
        result['source_sha256_after'] = sources_after
        result['source_hash_scope'] = 'Local runner and authored catalog only; not an independent attestation of all runtime code.'
        if sources_before != sources_after:
            result['status'] = 'INCOMPLETE' if result['status'] != 'FAIL' else 'FAIL'
            result['source_issue'] = 'SOURCES_CHANGED_DURING_RUN'
        with (out / 'result.json').open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    except (OSError, ValueError):
        return 2
    print(json.dumps({'status': result['status'], 'output': str((out / 'result.json').relative_to(ROOT))}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
