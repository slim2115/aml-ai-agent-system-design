"""Follow three open structuring searches through the template AML agent."""
from hashlib import sha256
import json

from qa.dark_downstream.probe import RecordingIndex
from qa.dark_retrieval.matrix_cases import cases, citation
from qa.retrieval_checks import citation_identity, evaluate_ranking
from src.contracts.v1 import ADAPTERS
from src.runtime.context import ContextBuilder
from src.runtime.policy import DemoPolicy
from src.runtime.service import AMLAgent

INCIDENT = 'INC-000123'
SOURCE = 'qa/datasets/aml-v1/fixtures/S01'
DATA = ('cases.json', 'clients.json', 'cards.json', 'transactions.json')
ALLOW = ['MATRIX-01']


def selected_cases():
    return [case for case in cases() if case['family'] == 'structuring']


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def fixture(source, case):
    """Keep the S01 incident data and bind a fresh manifest to the matrix corpus."""
    files = {name: (source / name).read_bytes() for name in DATA}
    files['rules.json'] = encoded(case['records'])
    fingerprint = lambda raw: sha256(raw).hexdigest()
    descriptors = [{'name': name, 'row_count': len(json.loads(raw)), 'content_sha256': fingerprint(raw)}
                   for name, raw in files.items()]
    window = {'start_inclusive': '2026-07-01T00:00:00Z', 'end_exclusive': case['as_of'], 'as_of': case['as_of']}
    manifest = {'schema_version': '1.0', 'snapshot_id': 'snapshot-' + case['case_id'],
                'policy_id': 'policy-' + case['case_id'], 'captured_at': case['as_of'],
                'purpose': 'synthetic_learning_only', 'assumed_naive_timezone': 'UTC', 'window': window,
                'original_sources': [{'source': d['name'], 'version': 'matrix-fixture-v1',
                                      'content_sha256': d['content_sha256']} for d in descriptors],
                'files': descriptors}
    files['manifest.json'] = encoded(manifest)
    policy = {'schema_version': '1.0', 'policy_id': manifest['policy_id'], 'snapshot_id': manifest['snapshot_id'],
              'snapshot_manifest_sha256': fingerprint(files['manifest.json']), 'window': window,
              'purpose': 'synthetic_learning_only', 'expert_approved': False, 'assumed_naive_timezone': 'UTC',
              'max_snapshot_age_seconds': 0, 'currency_scales': {'RUB': 2, 'USD': 2},
              'applicability': {'structuring': ALLOW}}
    files['policy.json'] = encoded(policy)
    return files


def observe(source, folder, case, settings, index_factory):
    folder.mkdir()
    for name, raw in fixture(source, case).items():
        with (folder / name).open('xb') as f:
            f.write(raw)
    policy = DemoPolicy.model_validate_json((folder / 'policy.json').read_bytes())
    recorders = []

    def factory(path):
        recorder = RecordingIndex(index_factory(path))
        recorders.append(recorder)
        return recorder

    agent = AMLAgent(mode='demo', engine='template', builder=ContextBuilder(folder, policy),
                     policy=policy, settings=settings, index_factory=factory)
    result = agent.run(INCIDENT)
    return {'case_id': case['case_id'], 'result': result,
            'calls': [call for recorder in recorders for call in recorder.calls]}


def assess(case, observation, reference):
    if observation.get('case_id') != case['case_id']:
        raise ValueError('CASE_MISMATCH')
    result = ADAPTERS['InvestigationResult'].validate_python(observation['result']).model_dump(mode='json')
    if result['incident_id'] != INCIDENT:
        raise ValueError('INCIDENT_MISMATCH')
    status = result['status']
    rules = result.get('applicable_rules', [])
    expected = [{'citation': c, 'rule_text': next(r['text'] for r in case['records'] if citation(r) == c)}
                for c in case['relevant']]
    if status == 'draft' and (result['snapshot_id'] != 'snapshot-' + case['case_id']
                              or result['policy_version'] != 'policy-' + case['case_id']):
        raise ValueError('DRAFT_CONTEXT_MISMATCH')
    correct = (status == 'draft' and rules == expected if expected else
               status == 'refusal' and result.get('reason_code') == 'NO_APPLICABLE_RULE')
    row = {'case_id': case['case_id'], 'agent_status': status, 'reason': result.get('reason_code') or result.get('code'),
           'allowed_ids': list(ALLOW), 'reported_citations': [r['citation'] for r in rules],
           'decision_status': 'INCOMPLETE' if status == 'error' else 'PASS' if correct else 'FAIL',
           'trace_status': 'INCOMPLETE', 'same_first_three': None, 'reported_ranks': []}
    calls = observation.get('calls')
    if not isinstance(calls, list) or len(calls) != 1:
        return row
    call = calls[0]
    day = case['as_of'][:10]
    active = {citation_identity(citation(r)) for r in case['records']
              if r['effective_from'] <= day < r.get('effective_until', '9999-12-31')}
    if (type(call) is not dict or call.get('query') != case['query'] or call.get('as_of') != case['as_of']
            or type(call.get('k')) is not int or call['k'] != len(active)):
        return row
    raw = call.get('raw')
    try:
        evaluate_ranking(raw, case['relevant'], k=len(active))
        ids = [citation_identity(item['citation']) for item in raw]
        if set(ids) != active or any(not 0 <= item['distance'] <= 2 for item in raw):
            return row
    except (KeyError, TypeError, ValueError):
        return row
    citations = [item['citation'] for item in raw]
    ranks = [citations.index(c) + 1 if c in citations else None for c in row['reported_citations']]
    row.update(trace_status='PASS', queried_k=call['k'], first_citation=citations[0], reported_ranks=ranks,
               same_first_three=citations[:3] == [item['citation'] for item in reference])
    if None in ranks:
        row['decision_status'] = 'FAIL'
    return row
