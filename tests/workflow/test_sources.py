"""Exact source viewing controls on shipped synthetic data; no model/network."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from src.config import get_settings
from src.contracts.semantics import canonical_digest
from src.contracts.v1 import ApplicableRule
from src.runtime.context import ContextBuilder
from src.runtime.generation import prepare_generation, render_generation
from src.runtime.policy import load_demo_policy
from src.workflow.sources import resolve_report_sources
from src.workflow.store import WorkflowError, utc_now


def demo_report():
    root = get_settings().project_root
    policy = load_demo_policy(root / 'config/demo-policy.json')
    prepared = ContextBuilder(root / 'data/demo-v1', policy).build('INC-000123')
    rules = []
    for row in json.loads((root / 'data/demo-v1/rules.json').read_text(encoding='utf-8')):
        if row['rule_id'] not in policy.applicability[prepared.alert_type]:
            continue
        digest = sha256(row['text'].encode()).hexdigest()
        identity = {'rule_id': row['rule_id'], 'version': row['version'], 'source_ref': row['regulation_ref'],
                    'char_start': 0, 'char_end': len(row['text']), 'text_sha256': digest}
        rules.append(ApplicableRule.model_validate({'citation': {'rule_id': row['rule_id'],
            'rule_version': row['version'], 'source_ref': row['regulation_ref'],
            'chunk_id': 'chunk-' + canonical_digest(identity), 'content_sha256': digest}, 'rule_text': row['text']}))
    request = prepare_generation(prepared.context, rules)
    return render_generation(request, json.dumps({'schema_version': '1.0', 'fact_order': list(request.fact_ids),
        'rule_order': list(request.rule_keys)}), report_id='source-test', generated_at=utc_now()).model_dump(mode='json')


def test_exact_cell_pointer_and_value_and_edition_range():
    report = demo_report()
    resolved = resolve_report_sources(report)
    root = get_settings().project_root / 'data/demo-v1'
    for fact in resolved['facts']:
        for evidence in fact:
            for cell in evidence['cells']:
                raw = (root / cell['source_file']).read_bytes()
                _, row, field = cell['json_pointer'].split('/')
                assert sha256(raw).hexdigest() == cell['content_sha256']
                assert json.loads(raw)[int(row)][field] == cell['value']
    for rule in resolved['rules']:
        assert rule['edition_text'][rule['char_start']:rule['char_end']] == rule['selected_text']
        assert rule['rule_version'] == report['applicable_rules'][0]['citation']['rule_version']
    assert all(cell['field'] not in ('counterparty', 'purpose')
               for fact in resolved['facts'] for evidence in fact for cell in evidence['cells'])


@pytest.mark.parametrize('mutation', ['snapshot', 'foreign_tx', 'fact_text', 'chunk', 'source_ref', 'version', 'rule_text'])
def test_changed_or_unresolvable_report_never_gets_source_confirmation(mutation):
    report = deepcopy(demo_report())
    if mutation == 'snapshot':
        report['snapshot_id'] = 'unavailable'
    elif mutation == 'foreign_tx':
        next(e for fact in report['found_facts'] for e in fact['evidence'] if e['kind'] == 'transaction')['tx_id'] = 'foreign'
    elif mutation == 'fact_text':
        report['found_facts'][0]['fact'] = '<script>untrusted</script>'
    elif mutation == 'rule_text':
        report['applicable_rules'][0]['rule_text'] = 'replaced'
    else:
        field = {'chunk': 'chunk_id', 'source_ref': 'source_ref', 'version': 'rule_version'}[mutation]
        report['applicable_rules'][0]['citation'][field] = 'replaced'
    with pytest.raises(WorkflowError, match='NOT_READY'):
        resolve_report_sources(report)


def test_changed_source_bytes_rejected(monkeypatch):
    report = demo_report()
    original = ContextBuilder._bytes
    def changed(self, name, *args):
        return original(self, name, *args) + (b' ' if name == 'transactions.json' else b'')
    monkeypatch.setattr(ContextBuilder, '_bytes', changed)
    with pytest.raises(WorkflowError, match='NOT_READY'):
        resolve_report_sources(report)
