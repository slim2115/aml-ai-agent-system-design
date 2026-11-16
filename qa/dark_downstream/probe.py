"""Observe AMLAgent's actual template-mode rule decisions on copied fixtures.

The index wrapper records and delegates; it never filters, ranks or selects.
All scenarios are controlled mutations of one open development fixture.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import shutil

from src.runtime.context import ContextBuilder
from src.runtime.policy import DemoPolicy
from src.runtime.service import AMLAgent

FILES = ('cases.json', 'clients.json', 'cards.json', 'transactions.json',
         'rules.json', 'manifest.json', 'policy.json')
INCIDENT = 'INC-000250'
ALERT = 'high_risk_country'
CASES = (
    ('empty_allowlist', [], 'refusal', 'NO_APPLICABLE_RULE', False, []),
    ('missing_allowlist_id', ['DARK-MISSING-001'], 'refusal', 'NO_APPLICABLE_RULE', True, []),
    ('deliberately_wrong_allowlist', ['R-115-006'], 'draft', None, True, ['R-115-006']),
    ('original_allowlist_control', ['R-AML-010'], 'draft', None, True, ['R-AML-010']),
)


def hashes(folder):
    return {name: sha256((folder / name).read_bytes()).hexdigest() for name in FILES}


def prepare(source: Path, target: Path, allowed):
    target.mkdir()
    for name in FILES:
        member = source / name
        if member.is_symlink() or not member.is_file():
            raise ValueError('INVALID_FIXTURE_MEMBER')
        shutil.copyfile(member, target / name)
    path = target / 'policy.json'
    policy = json.loads(path.read_bytes())
    if policy['applicability'][ALERT] != ['R-AML-010']:
        raise ValueError('SOURCE_POLICY_CHANGED')
    policy['applicability'][ALERT] = list(allowed)
    path.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


class RecordingIndex:
    """Observe exact results of the real index without changing returned objects."""
    def __init__(self, underlying):
        self.index = underlying
        self.calls = []

    @property
    def provenance(self):
        return self.index.provenance

    def active_chunk_count(self, as_of):
        return self.index.active_chunk_count(as_of)

    def search(self, query, *, as_of, k):
        chunks = self.index.search(query, as_of=as_of, k=k)
        self.calls.append({'query': query, 'as_of': as_of, 'k': k,
                           'raw': [{'citation': c.citation.model_dump(mode='json'),
                                    'distance': c.distance} for c in chunks]})
        return chunks


def evaluate(case, result, calls):
    case_id, allowed, expected_status, expected_reason, expect_search, expected_rules = case
    if type(result) is not dict or result.get('status') not in ('draft', 'refusal'):
        return {'case_id': case_id, 'status': 'INCOMPLETE', 'reason': 'AGENT_RESULT_UNAVAILABLE'}
    if type(calls) is not list or len(calls) > 1 or bool(calls) != expect_search:
        return {'case_id': case_id, 'status': 'INCOMPLETE', 'reason': 'SEARCH_TRACE_INCOMPLETE'}
    if calls and (type(calls[0].get('raw')) is not list or not calls[0]['raw']
                  or calls[0].get('query') != ALERT):
        return {'case_id': case_id, 'status': 'INCOMPLETE', 'reason': 'SEARCH_TRACE_INCOMPLETE'}
    if result['status'] == 'draft':
        rules = result.get('applicable_rules')
        if type(rules) is not list:
            return {'case_id': case_id, 'status': 'INCOMPLETE', 'reason': 'REPORT_RULES_MISSING'}
        try:
            citations = [r['citation'] for r in rules]
            ids = sorted(c['rule_id'] for c in citations)
            raw_citations = [r['citation'] for r in calls[0]['raw']]
            if not all(c in raw_citations for c in citations):
                return {'case_id': case_id, 'status': 'INCOMPLETE', 'reason': 'REPORT_NOT_IN_TRACE'}
        except (KeyError, TypeError, IndexError):
            return {'case_id': case_id, 'status': 'INCOMPLETE', 'reason': 'REPORT_RULES_INVALID'}
    else:
        citations, ids = [], []
    reason = result.get('reason_code')
    matches = (result['status'] == expected_status and reason == expected_reason
               and ids == sorted(expected_rules))
    return {'case_id': case_id, 'status': 'PASS' if matches else 'FAIL',
            'intentional_policy_mutation': case_id != 'original_allowlist_control',
            'allowed_rule_ids': allowed, 'agent_status': result['status'], 'reason_code': reason,
            'search_observed': bool(calls), 'search': calls[0] if calls else None,
            'reported_citations': citations, 'report_rule_ids': ids,
            'expected_agent_status': expected_status, 'expected_report_rule_ids': expected_rules,
            'semantic_relevance': 'NOT_ESTABLISHED',
            'controlled_observation': ('ACCEPTED_RULE_FROM_INTENTIONALLY_WRONG_ALLOWLIST'
                                       if case_id == 'deliberately_wrong_allowlist'
                                       and result['status'] == 'draft' and ids == ['R-115-006']
                                       else 'NOT_APPLICABLE'),
            'policy_safety': ('FAIL_FOR_CONTROLLED_MUTATION'
                              if case_id == 'deliberately_wrong_allowlist'
                              and 'R-115-006' in ids else 'NOT_ASSESSED'),
            'interpretation': ('Intentionally wrong allowlist tests policy trust, not a relevance classifier.'
                               if case_id == 'deliberately_wrong_allowlist'
                               else 'Only the final report or refusal is observed; internal partial selection is not inferred.')}


def run(source, output, settings, index_factory):
    before = hashes(source)
    results = []
    for case in CASES:
        case_id, allowed, *_ = case
        folder = output / case_id
        prepare(source, folder, allowed)
        policy = DemoPolicy.model_validate(json.loads((folder / 'policy.json').read_bytes()))
        recorders = []

        def factory(path):
            recorder = RecordingIndex(index_factory(path))
            recorders.append(recorder)
            return recorder

        agent = AMLAgent(mode='demo', engine='template', builder=ContextBuilder(folder, policy),
                         policy=policy, settings=settings, index_factory=factory)
        result = agent.run(INCIDENT)
        calls = [call for recorder in recorders for call in recorder.calls]
        row = evaluate(case, result, calls)
        row['events'] = [{'stage': e.stage, 'code': e.code} for e in agent.last_events]
        row['fixture_sha256'] = hashes(folder)
        results.append(row)
    preserved = before == hashes(source)
    statuses = [row['status'] for row in results]
    status = 'FAIL' if not preserved or 'FAIL' in statuses else 'INCOMPLETE' if 'INCOMPLETE' in statuses else 'PASS'
    return {'status': status, 'measurement_scope': 'single open synthetic fixture, four policy variants',
            'status_meaning': 'Control observation matching, never a product safety gate.',
            'policy_safety': results[2].get('policy_safety', 'INCOMPLETE'),
            'policy_safety_scope': 'Intentionally mutated allowlist only; not the original policy.',
            'embedding_dependency': 'SUPPLIED_BY_CALLER', 'engine': 'template', 'llm': 'NOT_RUN',
            'semantic_relevance': 'NOT_ESTABLISHED', 'aml_expert_acceptance': 'INCOMPLETE',
            'hidden_holdout': 'NOT_ESTABLISHED', 'source_preserved': preserved,
            'source_sha256': before, 'cases': results}
