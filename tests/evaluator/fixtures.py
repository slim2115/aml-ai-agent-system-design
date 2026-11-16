"""Synthetic mechanical controls, explicitly not an accepted golden dataset."""
from copy import deepcopy
from hashlib import sha256


def bundle(count=5):
    citation = {'rule_id': 'RULE-1', 'rule_version': 'v1', 'source_ref': 'Synthetic rule paragraph 1',
                'chunk_id': 'CHUNK-1', 'content_sha256': sha256(b'Synthetic rule').hexdigest()}
    cases, outcomes = [], []
    for number in range(count):
        category = 'ABCDE'[number % 5]
        incident = f'INC-{number + 1:06d}'
        case_id = f'CASE-{number + 1}'
        window = {'start_inclusive': '2026-01-01T00:00:00Z', 'end_exclusive': '2026-02-01T00:00:00Z',
                  'as_of': '2026-02-01T00:00:00Z'}
        context = {'schema_version': '1.0', 'incident_id': incident, 'client_id': 'CLIENT-1',
                   'snapshot': {'snapshot_id': f'SNAP-{number + 1}', 'captured_at': '2026-02-01T00:00:00Z',
                                'sources': [{'source': 'synthetic', 'version': 'v1', 'content_sha256': 'b' * 64}]},
                   'window': window, 'policy_version': 'v1', 'data_completeness': 'full',
                   'consistency': 'consistent', 'missing_fields': [],
                   'client': {'client_id': 'CLIENT-1', 'risk_category': 'low', 'kyc_status': 'verified',
                              'client_since': '2020-01-01'}, 'cards': [],
                   'transactions': [{'tx_id': 'TX-1', 'client_id': 'CLIENT-1', 'card_id': None, 'amount': '10.00',
                                     'currency': 'RUB', 'counterparty': 'Synthetic counterparty', 'purpose': 'Synthetic transfer',
                                     'channel': 'transfer', 'status': 'completed', 'timestamp': '2026-01-10T00:00:00Z'}]}
        refusal = category in 'CD'
        reason = 'UNTRUSTED_INPUT' if category == 'C' else 'INSUFFICIENT_DATA'
        case = {'id': case_id, 'independence_id': f'SCENARIO-{number + 1}', 'category': category,
                'incident_id': incident, 'expected_status': 'refusal' if refusal else 'draft',
                'forbidden_outputs': ['synthetic-forbidden-output'],
                'expected_retrieval': {'namespace': 'rule_id', 'ids': ['RULE-1']}, 'context': context}
        if refusal:
            case['expected_reason_code'] = reason
            result = {'schema_version': '1.0', 'status': 'refusal', 'incident_id': incident,
                      'generated_at': '2026-02-01T00:00:00Z', 'data_completeness': 'unassessed',
                      'reason_code': reason, 'reason': 'Synthetic refusal control', 'recommended_action': 'manual_review'}
        else:
            result = {'schema_version': '1.0', 'status': 'draft', 'incident_id': incident,
                      'report_id': f'REPORT-{number + 1}', 'report_version': 1, 'generated_at': '2026-02-01T00:00:00Z',
                      'snapshot_id': context['snapshot']['snapshot_id'], 'policy_version': 'v1', 'data_completeness': 'full',
                      'found_facts': [{'fact': 'Synthetic amount field control', 'sources': [deepcopy(citation)],
                                       'evidence': [{'kind': 'transaction', 'snapshot_id': context['snapshot']['snapshot_id'],
                                                     'tx_id': 'TX-1', 'field': 'amount'}]}],
                      'suspicious_patterns': [], 'applicable_rules': [{'citation': deepcopy(citation), 'rule_text': 'Synthetic rule'}],
                      'confidence': .8}
        cases.append(case)
        outcomes.append({'case_id': case_id, 'incident_id': incident, 'result': result, 'latency_seconds': 1.0,
                         'retrieval': {'namespace': 'rule_id', 'ids': ['RULE-1']}})
    return {'schema_version': '1.0', 'dataset': {'id': 'synthetic-control', 'kind': 'synthetic_control'},
            'cases': cases, 'outcomes': outcomes, 'citation_catalog': [citation]}


def external_controls(value=1.0):
    return {metric: {'status': 'measured', 'value': value, 'denominator': 10,
                     'evidence_references': ['synthetic-control://not-product-evidence'], 'method': 'synthetic unit control'}
            for metric in ('M-02', 'M-03', 'M-08')}
