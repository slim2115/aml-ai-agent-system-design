"""Confirm absent, broken and failed evidence cannot become a product PASS."""
import json
from copy import deepcopy
from hashlib import sha256

import pytest

from qa.dark_factory.report import build_report, read_input
from qa.dark_factory.baseline import SOURCES
from qa.tools.run_llm import EXPECTED_DIGEST


def examples():
    digest = 'a' * 64
    baseline = {'schema_version': '1.0.0', 'capture_status': 'PASS', 'current_product_status': 'INCOMPLETE',
                'source_count_expected': len(SOURCES), 'source_count_captured': len(SOURCES),
                'sources': {name: {'path': path, 'sha256': digest, 'bytes': 1} for name, path in SOURCES.items()}, 'issues': [],
                'historical_observations': {'judge': {'status': 'FAIL'},
                    'human_calibration': {'status': 'INCOMPLETE'}, 'product_acceptance': {'status': 'INCOMPLETE'}}}
    fake = {'status': 'PASS', 'scope': 'Synthetic fields, real boundary, fake embedding and fake transport.',
            'live_model': 'NOT_RUN', 'human_acceptance': 'INCOMPLETE',
            'hidden_holdout': 'NOT_AVAILABLE', 'source_preserved': True,
            'cases': [{'case_id': x, 'status': 'PASS', 'issues': [], 'fake_model_calls': 1, 'payload_sha256': digest}
                      for x in ('purpose_role', 'counterparty_role', 'purpose_unicode', 'counterparty_instructions')]}
    live = {'status': 'PASS', 'human_acceptance': 'INCOMPLETE', 'hidden_holdout': 'NOT_AVAILABLE',
            'issues': [], 'source_preserved': True, 'model_input_marker_observed': False,
            'marker_observed_in_output': False, 'prompt_count': 1, 'http_attempt_count': 1,
            'attempts_started': 1, 'model_digest_expected': EXPECTED_DIGEST, 'model_digest_observed': EXPECTED_DIGEST,
            'model_digest_after': EXPECTED_DIGEST, 'embedding_manifest_sha256': digest,
            'evidence_schema': 'dark-adversarial-live-v2', 'case_id': 'purpose_role',
            'transport_outcome': 'completed', 'wire_capture_status': 'COMPLETE',
            'projection_leak_ids': [], 'wire_leak_ids': [], 'output_leak_ids': [],
            'checked_fragment_ids': ['instruction_1', 'marker', 'payload'],
            'projection_scan_status': 'COMPLETE', 'output_scan_status': 'COMPLETE',
            'first_attempt_status': 'PASS', 'repair_outcome': 'NOT_APPLICABLE',
            'attempts': [{'index': 1, 'wire_capture_status': 'COMPLETE',
                          'request_sha256': digest, 'response_status': 'PASS'}]}
    regression = {'status': 'PASS', 'tests': 7, 'collected_tests': 5, 'additional_subtests': 2,
                  'failures': 0, 'errors': 0, 'skipped': 0, 'pytest_exit_code': 0,
                  'testpaths': ['tests/dark_factory', 'tests/dark_factory_cases', 'tests/dark_guards',
                                'tests/dark_retrieval', 'tests/dark_compare', 'tests/dark_adversarial'],
                  'contract_artifacts_match': [{'passed': True}]}
    return baseline, fake, live, regression


def test_machine_scopes_are_separate_and_judge_failure_is_retained():
    baseline, fake, live, regression = examples()
    result = build_report(baseline, fake, live, regression)
    assert [result[k] for k in ('baseline_capture', 'adversarial_fake', 'adversarial_live',
             'deterministic_regression', 'historical_judge', 'human_acceptance')] == [
                 'PASS', 'INCOMPLETE', 'PASS', 'PASS', 'FAIL', 'INCOMPLETE']
    assert result['hidden_holdout'] == 'NOT_ESTABLISHED' and result['product_acceptance'] == 'INCOMPLETE'


def test_missing_and_malformed_inputs_never_pass():
    result = build_report(None, {'status': 'PASS'}, {}, {'status': 'PASS'})
    for scope in ('baseline_capture', 'historical_judge', 'adversarial_fake', 'adversarial_live', 'deterministic_regression'):
        assert result[scope] == 'INCOMPLETE'


def test_baseline_failure_retained_even_if_capture_broken():
    baseline, fake, live, regression = examples()
    baseline['capture_status'] = 'INCOMPLETE'
    result = build_report(baseline, fake, live, regression)
    assert result['baseline_capture'] == 'INCOMPLETE'
    assert result['historical_judge'] == 'FAIL'


def test_contradictory_fake_live_and_skips_are_failure():
    baseline, fake, live, regression = examples()
    fake['cases'][0]['status'] = 'FAIL'
    live['model_input_marker_observed'] = True
    regression['skipped'] = 1
    result = build_report(baseline, fake, live, regression)
    assert [result[k] for k in ('adversarial_fake', 'adversarial_live', 'deterministic_regression')] == ['FAIL'] * 3


def test_fake_v2_requires_full_fragment_and_surface_evidence():
    from qa.dark_adversarial.probe import ATTACKS, fragments
    baseline, fake, live, regression = examples()
    assert build_report(baseline, fake, live, regression)['adversarial_fake'] == 'INCOMPLETE'
    fake['evidence_schema'] = 'dark-adversarial-fake-v2'
    for row, (case_id, field, payload) in zip(fake['cases'], ATTACKS):
        assert row['case_id'] == case_id
        row.update(field=field, payload_sha256=sha256(payload.encode()).hexdigest(),
                   checked_fragment_ids=sorted(fragments(case_id)),
                   input_scan_status='COMPLETE', output_scan_status='COMPLETE')
    assert build_report(baseline, fake, live, regression)['adversarial_fake'] == 'PASS'
    fake['cases'][0]['checked_fragment_ids'].remove('instruction_1')
    assert build_report(baseline, fake, live, regression)['adversarial_fake'] == 'INCOMPLETE'
    fake['cases'][0]['checked_fragment_ids'] = sorted(fragments('purpose_role'))
    fake['cases'][0]['output_scan_status'] = 'INCOMPLETE'
    assert build_report(baseline, fake, live, regression)['adversarial_fake'] == 'INCOMPLETE'
    fake['cases'][0]['status'] = 'FAIL'
    assert build_report(baseline, fake, live, regression)['adversarial_fake'] == 'FAIL'


def test_live_missing_identity_cannot_pass_and_no_raw_content_is_returned():
    baseline, fake, live, regression = examples()
    live['model_digest_after'] = None
    live['private_untrusted'] = 'raw prompt must not enter report'
    result = build_report(baseline, fake, live, regression)
    assert result['adversarial_live'] == 'INCOMPLETE'
    assert 'raw prompt' not in json.dumps(result)


def test_live_attempt_budget_and_record_consistency_are_enforced():
    baseline, fake, live, regression = examples()
    live['prompt_count'] = live['attempts_started'] = 3
    assert build_report(baseline, fake, live, regression)['adversarial_live'] == 'FAIL'
    live['prompt_count'], live['attempts_started'] = 1, 2
    assert build_report(baseline, fake, live, regression)['adversarial_live'] == 'FAIL'


def test_unqualified_historical_live_evidence_is_incomplete_but_leaks_still_fail():
    baseline, fake, live, regression = examples()
    live.pop('evidence_schema')
    assert build_report(baseline, fake, live, regression)['adversarial_live'] == 'INCOMPLETE'
    live['model_input_marker_observed'] = True
    assert build_report(baseline, fake, live, regression)['adversarial_live'] == 'FAIL'


def test_forged_model_identity_wire_and_wrong_case_cannot_pass():
    baseline, fake, live, regression = examples()
    live['model_digest_expected'] = live['model_digest_observed'] = live['model_digest_after'] = 'b' * 64
    assert build_report(baseline, fake, live, regression)['adversarial_live'] == 'FAIL'
    _, _, live, _ = examples()
    live['case_id'] = 'another-case'
    assert build_report(baseline, fake, live, regression)['adversarial_live'] == 'FAIL'
    _, _, live, _ = examples()
    live['attempts'][0]['wire_capture_status'] = 'INCOMPLETE'
    assert build_report(baseline, fake, live, regression)['adversarial_live'] == 'INCOMPLETE'
    live['attempts'][0]['wire_capture_status'] = 'COMPLETE'
    live['wire_leak_ids'] = ['marker']
    assert build_report(baseline, fake, live, regression)['adversarial_live'] == 'FAIL'


def test_live_requires_every_fragment_and_both_scan_surfaces():
    baseline, fake, live, regression = examples()
    live['checked_fragment_ids'].remove('instruction_1')
    assert build_report(baseline, fake, live, regression)['adversarial_live'] == 'INCOMPLETE'
    live['checked_fragment_ids'].append('instruction_1')
    live['checked_fragment_ids'].sort()
    live['output_scan_status'] = 'INCOMPLETE'
    assert build_report(baseline, fake, live, regression)['adversarial_live'] == 'INCOMPLETE'
    live['status'] = 'INCOMPLETE'
    live['wire_leak_ids'] = ['instruction_1']
    assert build_report(baseline, fake, live, regression)['adversarial_live'] == 'FAIL'


def test_failed_first_attempt_cannot_be_hidden_by_successful_repair():
    baseline, fake, live, regression = examples()
    live['prompt_count'] = live['attempts_started'] = live['http_attempt_count'] = 2
    live['first_attempt_status'] = 'FAIL'
    live['repair_outcome'] = 'SUCCESS'
    live['attempts'].append({**live['attempts'][0], 'index': 2})
    assert build_report(baseline, fake, live, regression)['adversarial_live'] == 'FAIL'


@pytest.mark.parametrize('field', ['case_id', 'transport_outcome', 'first_attempt_status', 'repair_outcome'])
def test_missing_live_measurements_remain_incomplete(field):
    baseline, fake, live, regression = examples()
    live[field] = 'INCOMPLETE' if field in {'first_attempt_status', 'repair_outcome'} else None
    assert build_report(baseline, fake, live, regression)['adversarial_live'] == 'INCOMPLETE'


def retrieval_examples(monkeypatch):
    from qa.dark_retrieval import live as retrieval
    from qa.dark_retrieval.assessment import assess_run
    cases = retrieval.development_cases()
    hashes = {name: 'a' * 64 for name in retrieval.SOURCES}
    monkeypatch.setattr(retrieval, 'source_hashes', lambda: dict(hashes))
    rows, observations = [], {}
    for case in cases:
        cite = case['relevant'][0] if case['relevant'] else cases[1]['relevant'][0]
        raw = [{'citation': cite, 'distance': 0.2}]
        rows.append({**{key: case[key] for key in ('case_id', 'family', 'slice', 'query', 'as_of')}, 'raw': raw})
        observations[case['case_id']] = {'raw': raw, 'selected': None}
    child = {'measurement_status': 'PASS', 'source_sha256_before': hashes, 'source_sha256_after': hashes,
             'dependency': 'real_local_embedding_cpu', 'dependency_validation': 'PASS',
             'selection_observed': False, 'product_quality': 'INCOMPLETE', 'expert_acceptance': 'INCOMPLETE',
             'secret_holdout': False, 'cases': rows, 'assessment': assess_run(cases, observations)}
    summary = {'status': 'INCOMPLETE', 'measurement_status': 'PASS', 'source_sha256_after': hashes,
               'product_quality': 'INCOMPLETE', 'expert_acceptance': 'INCOMPLETE', 'secret_holdout': False,
               'reason': 'DOWNSTREAM_SELECTION_NOT_MEASURED'}
    attempt = {'status': 'STARTED', 'source_sha256_before': hashes}
    return deepcopy(summary), deepcopy(child), deepcopy(attempt)


def test_retrieval_recomputed_but_downstream_and_expert_remain_incomplete(monkeypatch):
    summary, child, attempt = retrieval_examples(monkeypatch)
    result = build_report(retrieval_summary=summary, retrieval_child=child, retrieval_attempt=attempt)
    assert result['retrieval_measurement'] == 'PASS'
    assert result['retrieval_selection'] == result['retrieval_product_quality'] == 'INCOMPLETE'
    assert result['product_acceptance'] == 'INCOMPLETE'


def test_retrieval_missing_tampered_or_changed_sources_cannot_pass(monkeypatch):
    from qa.dark_retrieval import live as retrieval
    summary, child, attempt = retrieval_examples(monkeypatch)
    assert build_report(retrieval_summary=summary)['retrieval_measurement'] == 'INCOMPLETE'
    child['assessment']['rows'][0]['ranking']['hit_count'] = 999
    assert build_report(retrieval_summary=summary, retrieval_child=child,
                        retrieval_attempt=attempt)['retrieval_measurement'] == 'FAIL'
    summary, child, attempt = retrieval_examples(monkeypatch)
    summary['source_sha256_after']['qa/dark_retrieval/live.py'] = 'b' * 64
    assert build_report(retrieval_summary=summary, retrieval_child=child,
                        retrieval_attempt=attempt)['retrieval_measurement'] == 'FAIL'
    summary, child, attempt = retrieval_examples(monkeypatch)
    monkeypatch.setattr(retrieval, 'source_hashes', lambda: {'changed': 'b' * 64})
    assert build_report(retrieval_summary=summary, retrieval_child=child,
                        retrieval_attempt=attempt)['retrieval_measurement'] == 'INCOMPLETE'


def test_retrieval_forged_expert_approval_cannot_pass(monkeypatch):
    summary, child, attempt = retrieval_examples(monkeypatch)
    summary['expert_acceptance'] = 'PASS'
    assert build_report(retrieval_summary=summary, retrieval_child=child,
                        retrieval_attempt=attempt)['retrieval_measurement'] == 'INCOMPLETE'
    summary['status'] = 'FAIL'
    assert build_report(retrieval_summary=summary)['retrieval_measurement'] == 'FAIL'


def audit_examples():
    baseline, _, _, _ = examples()
    baseline['historical_observations']['judge'].update(planned=20, valid_responses=19,
                                                        correct_known_labels=16, false_passes=3)
    metrics = {'planned': 20, 'valid_responses': 19, 'correct_known_labels': 16, 'expected_defective': 14,
               'false_passes': 3, 'historical_judge_status': 'FAIL', 'human_calibration': 'INCOMPLETE',
               'result_sha256': 'a' * 64}
    audit = {'schema_version': '1.0', 'audit_status': 'PASS', 'judge_status': 'FAIL',
             'human_calibration': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE', 'metrics': metrics}
    return baseline, audit


def test_judge_audit_validates_counts_and_matches_baseline_failure():
    baseline, audit = audit_examples()
    report = build_report(baseline=baseline, judge_audit=audit)
    assert report['judge_audit'] == 'PASS' and report['historical_judge'] == 'FAIL'
    assert report['judge_audit_metrics']['false_passes'] == 3
    assert report['judge_audit_metrics']['expected_defective'] == 14
    assert report['human_acceptance'] == 'INCOMPLETE'


def test_judge_audit_cannot_hide_false_pass_or_fake_counts():
    baseline, audit = audit_examples()
    audit['judge_status'] = audit['metrics']['historical_judge_status'] = 'PASS'
    report = build_report(baseline=baseline, judge_audit=audit)
    assert report['judge_audit'] == 'FAIL' and report['historical_judge'] == 'FAIL'
    baseline, audit = audit_examples()
    audit['metrics']['false_passes'] = 15
    assert build_report(baseline=baseline, judge_audit=audit)['judge_audit'] == 'FAIL'
    baseline, audit = audit_examples()
    audit['metrics']['result_sha256'] = 'b' * 64
    report = build_report(baseline=baseline, judge_audit=audit)
    assert report['judge_audit'] == 'INCOMPLETE' and report['historical_judge'] == 'FAIL'


@pytest.mark.parametrize('kind,path', [('baseline', '../secret.json'), ('fake', 'var/qa/../secret.json'),
                                       ('live', 'D:/secret.json'), ('regression', 'var/checks/../x.json')])
def test_invalid_input_path_never_read(tmp_path, kind, path):
    with pytest.raises(ValueError, match='INVALID_INPUT_PATH'):
        read_input(kind, path, root=tmp_path)


def test_missing_input_is_distinct_and_duplicate_keys_rejected(tmp_path):
    folder = tmp_path / 'var/qa/dark-factory'
    folder.mkdir(parents=True)
    assert read_input('baseline', 'var/qa/dark-factory/missing.json', root=tmp_path) == (None, None)
    (folder / 'bad.json').write_text('{"status":"PASS","status":"FAIL"}', encoding='utf-8')
    with pytest.raises(ValueError, match='DUPLICATE_KEY'):
        read_input('baseline', 'var/qa/dark-factory/bad.json', root=tmp_path)


def test_cli_disallows_mixing_retrieval_run_directories(monkeypatch, capsys):
    import qa.dark_factory.report as module
    captured = {}
    monkeypatch.setattr(module, 'read_input', lambda kind, path: ({'kind': kind}, 'a' * 64))
    def save(result, path):
        captured.update(result)
    monkeypatch.setattr(module, 'write_new', save)
    code = module.main(['--baseline', 'b', '--fake', 'f', '--live', 'l', '--regression', 'r',
                        '--retrieval-summary', 'var/qa/dark-retrieval/run-a/summary.json',
                        '--retrieval-child', 'var/qa/dark-retrieval/run-b/child-result.json',
                        '--retrieval-attempt', 'var/qa/dark-retrieval/run-a/attempt.json',
                        '--output', 'ignored'])
    assert code == 1
    assert captured['retrieval_measurement'] == 'INCOMPLETE'
    assert captured['input_sha256']['retrieval_summary'] is None
    assert captured['input_sha256']['retrieval_child'] is None


@pytest.mark.parametrize('kind,path', [
    ('retrieval_summary', 'var/qa/dark-retrieval/../escape/summary.json'),
    ('retrieval_child', 'D:/outside/child-result.json'),
    ('retrieval_attempt', 'var/qa/dark-retrieval/a/../../attempt.json'),
    ('judge_audit', 'var/qa/dark-factory/../secret.json'),
])
def test_new_input_paths_cannot_escape(tmp_path, kind, path):
    with pytest.raises(ValueError, match='INVALID_INPUT_PATH'):
        read_input(kind, path, root=tmp_path)
