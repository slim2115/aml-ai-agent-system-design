"""Historical adapter tests use exclusively temporary synthetic evidence."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from qa.dark_compare import historical


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def write(root, relative, value):
    data = encoded(value)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {'path': relative, 'bytes': len(data), 'sha256': sha256(data).hexdigest()}


def synthetic(root, *, invalid_first=None, bad_family=False):
    scenarios = [{'case': {'id': cid, 'independence_id': ('BAD' if bad_family and i == 0 else family)},
                  'fixture': f'fixtures/{i}'}
                 for i, (cid, family) in enumerate(historical.CASES.items())]
    case_path = historical.CATALOG + '/cases.json'
    case_entry = write(root, case_path, {'cases': scenarios})
    case_entry['path'] = 'cases.json'
    sources = []
    def add(path, value):
        sources.append(write(root, path, value))
    add(historical.CATALOG + '/manifest.json', {'files': [case_entry]})
    add(historical.PILOT + '/qualification.json', {'status': 'PASS', 'collection_status': 'PASS',
        'product_acceptance': 'INCOMPLETE', 'checks': [{'status': 'PASS'}], 'check_count': 1, 'failed_checks': []})
    add(historical.PILOT + '/run.json', {'model': 'qwen2.5:14b', 'model_digest': historical.EXPECTED_MODEL,
                                         'selected_case_ids': list(historical.CASES)})
    add(historical.PILOT + '/result.json', {'status': 'PASS', 'product_acceptance': 'INCOMPLETE'})
    add(historical.PILOT + '/source-hashes.json', {'src/runtime/generation.py': 'a' * 64})
    for cid, family in historical.CASES.items():
        base = f'{historical.PILOT}/cases/{cid}'
        add(base + '/template.json', {'issues': [], 'observation': {'result': {'status': 'draft'}}})
        valid = cid != invalid_first
        user = {'facts': [{'fact_id': 'f'}], 'rule_keys': ['r']}
        model_response = {'schema_version': '1.0', 'fact_order': ['f' if valid else 'invalid'], 'rule_order': ['r']}
        request = {'messages': [{'role': 'system', 'content': 'system'},
                                {'role': 'user', 'content': json.dumps(user)}]}
        attempt = {'http_status': 200, 'request_body': json.dumps(request),
                   'response_json': {'done': True, 'message': {'role': 'assistant', 'content': json.dumps(model_response)}},
                   'completion': {'valid_complete_selection': valid}}
        add(base + '/repeat-01.json', {'evaluation': {'case_id': cid, 'independence_id': family,
            'repeat_index': 1, 'status': 'PASS', 'issues': [], 'fact_issues': []},
            'transport_outcome': 'completed', 'http_attempts': [attempt]})
    manifest = {'files': sources}
    manifest_data = encoded(manifest)
    write(root, historical.EVIDENCE + '/file-manifest.json', manifest)
    write(root, historical.EVIDENCE + '/artifact-integrity.json', {'status': 'PASS',
        'manifest_sha256': sha256(manifest_data).hexdigest()})


def test_valid_synthetic_saved_observations_are_compared_without_raw_data(tmp_path):
    synthetic(tmp_path)
    report = historical.build(tmp_path)
    assert report['status'] == 'PASS'
    assert report['planned_cases'] == report['measured_pairs'] == 5
    assert report['distinct_families_descriptive'] == 3
    assert report['independent_cases'] is None
    assert report['template_model'] == 'NOT_APPLICABLE'
    assert report['baseline_metadata']['model_sha256'] is None
    assert report['baseline_metadata']['prompt_sha256'] is None
    assert report['product_acceptance'] == report['human_acceptance'] == 'INCOMPLETE'
    assert 'request_body' not in json.dumps(report)


def test_invalid_first_response_not_erased_by_successful_final_draft(tmp_path):
    synthetic(tmp_path, invalid_first='QA-S01')
    report = historical.build(tmp_path)
    assert report['status'] == 'FAIL'
    assert report['transitions']['regression'] == 1
    assert next(x for x in report['cases'] if x['case_id'] == 'QA-S01')['candidate_first'] == 'FAIL'


def test_saved_file_tampering_and_missing_evidence_are_incomplete(tmp_path):
    synthetic(tmp_path)
    path = tmp_path / f'{historical.PILOT}/cases/QA-S01/template.json'
    path.write_bytes(path.read_bytes() + b' ')
    assert historical.build(tmp_path)['status'] == 'INCOMPLETE'
    path.unlink()
    assert historical.build(tmp_path)['status'] == 'INCOMPLETE'


def test_wrong_family_not_promoted(tmp_path):
    synthetic(tmp_path, bad_family=True)
    assert historical.build(tmp_path)['status'] == 'INCOMPLETE'


def test_unknown_first_attempt_is_not_a_pass():
    record = {'evaluation': {'case_id': 'QA-S01', 'independence_id': 'FAMILY-STRUCTURING',
             'repeat_index': 1, 'status': 'PASS', 'issues': [], 'fact_issues': []}, 'http_attempts': []}
    assert historical._first_model_status(record, 'QA-S01', 'FAMILY-STRUCTURING') == 'INCOMPLETE'


def test_observed_failure_is_not_hidden_by_missing_http_evidence():
    record = {'evaluation': {'case_id': 'QA-S01', 'independence_id': 'FAMILY-STRUCTURING',
             'repeat_index': 1, 'status': 'FAIL', 'issues': ['WRONG_SUM'], 'fact_issues': []}, 'http_attempts': []}
    assert historical._first_model_status(record, 'QA-S01', 'FAMILY-STRUCTURING') == 'FAIL'
