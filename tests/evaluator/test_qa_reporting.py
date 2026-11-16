"""Evidence reports must preserve failures, provenance gaps and safe rendering."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from qa import reporting
from qa.tools import report_ci


def save(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return reporting.pin(root, name)


def spec(root, identifier, kind, members):
    return {'id': identifier, 'kind': kind, 'files': {
        role: save(root, 'verification/fixture/' + identifier + '/' + role.replace(':', '-')
                   + ('.xml' if role == 'junit' else '.json'), value)
        for role, value in members.items()}}


def index(*profiles, scope='historical'):
    return {'schema_version': '1.0', 'scope': scope, 'profiles': list(profiles)}


def unit_result(**updates):
    result = {'status': 'PASS', 'pytest_exit_code': 0, 'tests': 3, 'collected_tests': 2,
              'additional_subtests': 1, 'failures': 0, 'errors': 0, 'skipped': 0,
              'contract_artifacts_match': [{'name': 'openapi', 'passed': True}]}
    result.update(updates)
    return result


JUNIT = b'<testsuites tests="999"><testsuite tests="999"><testcase name="first"/><testcase name="second"/><testcase name="second-subcase"/></testsuite></testsuites>'


def unit_profile(root, **updates):
    return spec(root, 'regression', 'unit', {'result': unit_result(**updates), 'junit': JUNIT})


def retrieval_result(**updates):
    result = {'status': 'PASS', 'ranking_cases': 13, 'primary_query_count': 5,
              'metamorphic_cases': 5, 'metamorphic_family_count': 3,
              'embedding_calls': 29, 'primary_macro': {'recall_at_3': 0.8},
              'ranking_status': 'PASS', 'metamorphic_status': 'PASS'}
    result.update(updates)
    return result


def judge_result():
    return {'status': 'FAIL', 'model_identity_stable': False,
            'summary': {'execution_status': 'FAIL', 'known_challenge_status': 'INCOMPLETE',
                        'human_calibration': {'supplied_reviews': 0},
                        'overall': {'planned': 20, 'valid_responses': 11, 'missing': 0,
                                    'error': 3, 'timeout': 0, 'invalid_response': 6,
                                    'all_planned_accuracy': {'numerator': 11, 'denominator': 20,
                                                             'rate': 0.55}}}}


def pipeline_result():
    return {'status': 'PASS', 'pytest_exit_code': 0, 'testcases': 1, 'failure': 0, 'error': 0, 'skipped': 0,
            'expected_testcases': 1, 'collection_complete': True}


def test_unit_pass_requires_matching_cases_contracts_and_subtest_accounting(tmp_path):
    report = reporting.build_report(index(unit_profile(tmp_path)), root=tmp_path)
    assert report['status'] == report['evidence_integrity'] == 'PASS'
    metrics = report['profiles'][0]['metrics']
    assert (metrics['tests'], metrics['collected_tests'], metrics['additional_subtests']) == (3, 2, 1)
    assert report['product_acceptance'] == 'INCOMPLETE'
    assert report['remote_ci'] == 'NOT_VERIFIED_BY_THIS_REPORT'
    assert reporting.junit(JUNIT)['tests'] == 3  # Do not trust forged XML summary attributes.


@pytest.mark.parametrize('contracts', [None, [], [{'passed': False}], [{'passed': 'true'}]])
def test_unit_cannot_pass_without_verified_contracts(tmp_path, contracts):
    report = reporting.build_report(index(unit_profile(tmp_path, contract_artifacts_match=contracts)), root=tmp_path)
    assert report['status'] in {'FAIL', 'ERROR'}


@pytest.mark.parametrize('updates', [
    {'tests': None}, {'tests': True}, {'tests': -1}, {'tests': 3.0}, {'tests': '3'},
    {'collected_tests': None}, {'additional_subtests': True}, {'additional_subtests': 2},
    {'status': 'UNKNOWN'},
])
def test_unknown_or_inconsistent_counts_are_error(tmp_path, updates):
    report = reporting.build_report(index(unit_profile(tmp_path, **updates)), root=tmp_path)
    assert report['status'] == 'ERROR'


def test_missing_junit_cannot_be_green(tmp_path):
    profile = unit_profile(tmp_path)
    del profile['files']['junit']
    report = reporting.build_report(index(profile), root=tmp_path)
    assert report['status'] == 'INCOMPLETE'
    assert 'junit_missing' in report['profiles'][0]['issues']


@pytest.mark.parametrize('total,failures,expected', [(3, 0, 'PASS'), (999, 0, 'ERROR'), (3, 1, 'ERROR')])
def test_pytest_successful_subtests_use_consistent_suite_totals(tmp_path, total, failures, expected):
    raw = f'<testsuites><testsuite name="pytest" tests="{total}" failures="{failures}" errors="0" skipped="0"><testcase name="one"/><testcase name="two"/></testsuite></testsuites>'.encode()
    profile = spec(tmp_path, 'subtests', 'unit', {'result': unit_result(), 'junit': raw})
    report = reporting.build_report(index(profile), root=tmp_path)
    assert report['status'] == expected
    if expected == 'PASS':
        assert 'no individual XML nodes' in report['profiles'][0]['metrics']['subtests_accounting']


@pytest.mark.parametrize('exit_code, expected', [(None, 'INCOMPLETE'), (1, 'FAIL'), (5, 'FAIL')])
def test_nonzero_or_unknown_pytest_exit_cannot_be_green(tmp_path, exit_code, expected):
    profile = unit_profile(tmp_path, pytest_exit_code=exit_code)
    assert reporting.build_report(index(profile), root=tmp_path)['status'] == expected


def test_empty_suite_is_incomplete_even_when_it_claims_pass(tmp_path):
    profile = spec(tmp_path, 'empty', 'unit', {
        'result': unit_result(tests=0, collected_tests=0, additional_subtests=0),
        'junit': b'<testsuite tests="0"/>'})
    assert reporting.build_report(index(profile), root=tmp_path)['status'] == 'INCOMPLETE'


def test_skipped_cases_are_not_counted_as_success(tmp_path):
    profile = spec(tmp_path, 'skipped', 'unit', {
        'result': unit_result(skipped=1),
        'junit': b'<testsuite><testcase/><testcase/><testcase><skipped/></testcase></testsuite>'})
    assert reporting.build_report(index(profile), root=tmp_path)['status'] == 'FAIL'


def test_junit_disagreement_cannot_be_hidden_by_declared_pass(tmp_path):
    profile = spec(tmp_path, 'disagreement', 'unit', {
        'result': unit_result(),
        'junit': b'<testsuite><testcase/><testcase/><testcase><failure/></testcase></testsuite>'})
    report = reporting.build_report(index(profile), root=tmp_path)
    assert report['status'] == 'ERROR'
    assert 'junit_counts_disagree' in report['profiles'][0]['issues']


def test_fail_is_not_overridden_by_other_passes_or_missing_human_reviews(tmp_path):
    failed = spec(tmp_path, 'judge', 'judge', {'result': judge_result()})
    human = {'id': 'human-review', 'kind': 'human', 'files': {}}
    report = reporting.build_report(index(unit_profile(tmp_path), failed, human), root=tmp_path)
    assert report['status'] == 'FAIL'
    assert [p['status'] for p in report['profiles']] == ['PASS', 'FAIL', 'INCOMPLETE']
    metrics = report['profiles'][1]['metrics']
    assert metrics['valid_responses'] == 11 and metrics['planned'] == 20
    assert metrics['all_planned_accuracy']['rate'] == 0.55
    assert metrics['model_identity_stable'] is False
    assert metrics['human_reviews'] == 0 and metrics['suitability'] == 'NOT_ESTABLISHED'
    assert report['profiles'][2]['metrics']['calibration'] == 'PENDING'


@pytest.mark.parametrize('child', ['execution_status', 'known_challenge_status'])
def test_judge_nested_failure_overrides_claimed_pass(tmp_path, child):
    value = judge_result()
    value.update(status='PASS', model_identity_stable=True)
    value['summary'].update(execution_status='PASS', known_challenge_status='PASS',
                            human_calibration={'supplied_reviews': 20})
    value['summary']['overall'].update(valid_responses=20, error=0, invalid_response=0)
    value['summary'][child] = 'FAIL'
    report = reporting.build_report(index(spec(tmp_path, 'judge', 'judge', {'result': value})), root=tmp_path)
    assert report['status'] == 'FAIL'


@pytest.mark.parametrize('child', ['ranking_status', 'metamorphic_status'])
def test_retrieval_nested_failure_remains_failure(tmp_path, child):
    profile = spec(tmp_path, 'retrieval', 'retrieval', {'result': retrieval_result(**{child: 'FAIL'})})
    assert reporting.build_report(index(profile), root=tmp_path)['status'] == 'FAIL'


def test_incomplete_pipeline_collection_never_passes(tmp_path):
    value = pipeline_result()
    value.update(expected_testcases=5, collection_complete=False)
    profile = spec(tmp_path, 'pipeline', 'pipeline', {'result': value, 'junit': b'<testsuite><testcase/></testsuite>'})
    assert reporting.build_report(index(profile), root=tmp_path)['status'] == 'INCOMPLETE'


def test_measured_source_change_is_a_failure(tmp_path):
    profile = unit_profile(tmp_path, source_files_unchanged_during_run=False)
    report = reporting.build_report(index(profile), root=tmp_path)
    assert report['status'] == 'FAIL'
    assert 'measured_sources_changed' in report['profiles'][0]['issues']


@pytest.mark.parametrize('raw', [b'{"status":"PASS","status":"FAIL"}',
                               b'{"status":"PASS","value":NaN}',
                               b'{"status":"PASS","value":Infinity}',
                               b'{"status":"PASS","value":-Infinity}',
                               b'{"status":"PASS","value":1e999}',
                               b'{', b'\xff'])
def test_malformed_or_ambiguous_json_is_integrity_error(tmp_path, raw):
    profile = spec(tmp_path, 'broken', 'checks', {'result': raw})
    report = reporting.build_report(index(profile), root=tmp_path)
    assert report['status'] == report['evidence_integrity'] == 'ERROR'


def test_xml_entities_are_rejected_before_parsing(tmp_path):
    raw = b'<!DOCTYPE foo [<!ENTITY local SYSTEM "file:///sensitive">]><testsuite><testcase name="&local;"/></testsuite>'
    profile = spec(tmp_path, 'entities', 'unit', {'result': unit_result(), 'junit': raw})
    assert reporting.build_report(index(profile), root=tmp_path)['evidence_integrity'] == 'ERROR'


@pytest.mark.parametrize('malformed', [
    {'status': 'PASS', 'checks': [None]},
    {'status': 'PASS', 'checks': ['passed']},
])
def test_malformed_nested_evidence_is_reported_without_crashing(tmp_path, malformed):
    profile = spec(tmp_path, 'malformed', 'checks', {'result': malformed})
    report = reporting.build_report(index(profile), root=tmp_path)
    assert report['status'] == 'ERROR'
    assert 'malformed_evidence' in report['profiles'][0]['issues']


def test_tampered_pinned_bytes_are_not_loaded_or_displayed(tmp_path):
    profile = unit_profile(tmp_path)
    path = tmp_path / profile['files']['result']['path']
    path.write_text('{"status":"PASS","secret":"TAMPERED-SOURCE-MARKER"}', encoding='utf-8')
    report = reporting.build_report(index(profile), root=tmp_path)
    assert report['status'] == 'ERROR'
    assert 'TAMPERED-SOURCE-MARKER' not in json.dumps(report)
    assert report['profiles'][0]['evidence'][0]['integrity'] == 'ERROR'


def test_missing_pinned_file_stays_incomplete(tmp_path):
    profile = unit_profile(tmp_path)
    (tmp_path / profile['files']['result']['path']).unlink()
    report = reporting.build_report(index(profile), root=tmp_path)
    assert report['status'] == 'INCOMPLETE'
    assert report['evidence_integrity'] == 'INCOMPLETE'


@pytest.mark.parametrize('name', ['../outside.json', 'verification/../outside.json',
                                 '/outside.json', 'C:/outside.json', 'verification\\file.json'])
def test_input_path_escape_is_rejected(tmp_path, name):
    profile = {'id': 'escape', 'kind': 'checks',
               'files': {'result': {'path': name, 'sha256': None}}}
    with pytest.raises(ValueError):
        reporting.build_report(index(profile), root=tmp_path)


@pytest.mark.parametrize('name', ['src/result.json', 'scripts/result.json'])
def test_input_scope_does_not_include_product_sources(tmp_path, name):
    profile = {'id': 'scope', 'kind': 'checks', 'files': {'result': {'path': name, 'sha256': None}}}
    with pytest.raises(ValueError, match='REPORT_INPUT_SCOPE'):
        reporting.build_report(index(profile), root=tmp_path)


def test_output_is_exclusive_and_does_not_overwrite_previous_report(tmp_path):
    plan = index(unit_profile(tmp_path))
    report = reporting.build_report(plan, root=tmp_path)
    output = 'var/report/first'
    assert reporting.write_report(report, plan, output, root=tmp_path) == 0
    before = {p.name: sha256(p.read_bytes()).hexdigest() for p in (tmp_path / output).iterdir()}
    assert set(before) == {'report.json', 'index.json', 'report.html', 'summary.md'}
    with pytest.raises(FileExistsError, match='REPORT_OUTPUT_EXISTS'):
        reporting.write_report(report, plan, output, root=tmp_path)
    assert before == {p.name: sha256(p.read_bytes()).hexdigest() for p in (tmp_path / output).iterdir()}


@pytest.mark.parametrize('output', ['../report/new', 'src/report/new', 'var/only'])
def test_output_escape_or_unscoped_directory_is_rejected(tmp_path, output):
    plan = index(unit_profile(tmp_path))
    report = reporting.build_report(plan, root=tmp_path)
    with pytest.raises(ValueError):
        reporting.write_report(report, plan, output, root=tmp_path)


def test_nested_untrusted_markup_is_inert_in_html_and_markdown(tmp_path):
    markup = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    profile = spec(tmp_path, 'retrieval', 'retrieval', {
        'result': retrieval_result(primary_macro={'nested': {'untrusted': markup}})})
    plan = index(profile)
    report = reporting.build_report(plan, root=tmp_path)
    reporting.write_report(report, plan, 'var/report/escaped', root=tmp_path)
    for name in ('report.html', 'summary.md'):
        content = (tmp_path / 'var/report/escaped' / name).read_text(encoding='utf-8')
        assert '<img' not in content and '<script' not in content


def test_renderer_escapes_nested_markup_even_if_a_future_adapter_admits_text(tmp_path):
    plan = index(unit_profile(tmp_path))
    report = reporting.build_report(plan, root=tmp_path)
    report['profiles'][0]['metrics']['future_metric'] = {'message': '<script>alert(2)</script>'}
    reporting.write_report(report, plan, 'var/report/renderer-defense', root=tmp_path)
    for name in ('report.html', 'summary.md'):
        content = (tmp_path / 'var/report/renderer-defense' / name).read_text(encoding='utf-8')
        assert '<script>' not in content
        assert '&lt;script&gt;' in content


def test_raw_prompt_response_and_credentials_are_not_copied_to_outputs(tmp_path):
    # Credential-shaped fixture only; this is not an actual token.
    secret = 'Bearer ' + 'syntheticfixture' * 3
    value = unit_result(raw_prompt=secret, response_body={'api_key': secret},
                        error_message='RAW-SOURCE-ERROR-MARKER')
    profile = spec(tmp_path, 'private', 'unit', {'result': value, 'junit': JUNIT})
    plan = index(profile)
    report = reporting.build_report(plan, root=tmp_path)
    reporting.write_report(report, plan, 'var/report/private', root=tmp_path)
    for path in (tmp_path / 'var/report/private').iterdir():
        text = path.read_text(encoding='utf-8')
        assert secret not in text
        assert 'RAW-SOURCE-ERROR-MARKER' not in text
        assert 'raw_prompt' not in text and 'response_body' not in text


def test_credential_shaped_metric_is_rejected_and_excluded_from_outputs(tmp_path):
    secret = 'ghp_' + 'syntheticfixture' * 3
    profile = spec(tmp_path, 'secret-metric', 'retrieval', {
        'result': retrieval_result(primary_macro={'nested': secret})})
    plan = index(profile)
    report = reporting.build_report(plan, root=tmp_path)
    assert report['status'] == 'ERROR'
    assert reporting.write_report(report, plan, 'var/report/rejected-metric', root=tmp_path) == 1
    assert all(secret not in path.read_text(encoding='utf-8')
               for path in (tmp_path / 'var/report/rejected-metric').iterdir())


def test_credential_shaped_metadata_is_blocked_before_any_output_is_written(tmp_path):
    secret = 'ghp_' + 'syntheticfixture' * 3
    profile = spec(tmp_path, 'secret-metadata', 'unit', {
        'result': unit_result(), 'junit': JUNIT, 'runtime': {'model': secret}})
    plan = index(profile)
    report = reporting.build_report(plan, root=tmp_path)
    with pytest.raises(ValueError, match='REPORT_SECRET_PATTERN_DETECTED'):
        reporting.write_report(report, plan, 'var/report/blocked', root=tmp_path)
    assert not (tmp_path / 'var/report/blocked').exists()


def test_shared_run_and_case_ids_do_not_merge_distinct_pipeline_evidence(tmp_path):
    profiles = []
    for number in (1, 2):
        case = {'run_id': 'same-run', 'case_id': 'QA-P01', 'observations': [
            {'event_id': 'event-' + str(number),
             'source': {'topic': 'same-topic', 'partition': 2, 'offset': number},
             'consistency': {'job_id': 'job-' + str(number), 'report_id': 'report-' + str(number),
                             'report_version': 1, 'content_sha256': str(number) * 64}}]}
        profiles.append(spec(tmp_path, 'pipeline-' + str(number), 'pipeline', {
            'result': pipeline_result(), 'junit': b'<testsuite><testcase/></testsuite>', 'case:QA-P01': case}))
    report = reporting.build_report(index(*profiles), root=tmp_path)
    rows = report['correlation_rows']
    assert len(rows) == 2
    assert rows[0]['qa_run_id'] == rows[1]['qa_run_id'] == 'same-run'
    assert rows[0]['case_id'] == rows[1]['case_id'] == 'QA-P01'
    assert rows[0]['evidence'] != rows[1]['evidence']
    assert [(r['event_id'], r['job_id'], r['report_id']) for r in rows] == [
        ('event-1', 'job-1', 'report-1'), ('event-2', 'job-2', 'report-2')]
    assert all(r['ui_opened_job_id'] is None and r['judge_case_id'] is None for r in rows)
    assert all(r['missing_links_are_not_inferred'] is True for r in rows)


def test_judge_source_case_relation_does_not_invent_operational_links(tmp_path):
    profile = spec(tmp_path, 'judge', 'judge', {
        'result': judge_result(), 'case:J-001': {'observation': {'case_id': 'J-001'}},
        'dataset': {'cases': [{'id': 'J-001', 'source_case_id': 'QA-S01'}]}})
    row = reporting.build_report(index(profile), root=tmp_path)['correlation_rows'][0]
    assert row['judge_case_id'] == 'J-001' and row['judge_source_case_id'] == 'QA-S01'
    assert row['event_id'] is None and row['job_id'] is None and row['ui_opened_job_id'] is None


@pytest.mark.parametrize('profile', ['hermetic', 'browser'])
def test_ci_missing_inputs_are_incomplete_and_never_green(tmp_path, profile):
    plan = report_ci.ci_index(profile, 'var/ci/missing', root=tmp_path)
    report = reporting.build_report(plan, root=tmp_path)
    assert report['scope'] == 'ci-' + profile
    assert report['status'] == 'INCOMPLETE'
    assert all(p['status'] == 'INCOMPLETE' for p in report['profiles'])
    assert reporting.write_report(report, plan, 'var/report/ci-' + profile, root=tmp_path) == 3


def test_ci_browser_does_not_follow_a_result_path_outside_qa_runs(tmp_path):
    save(tmp_path, 'var/ci/browser/result.json', {'status': 'PASS', 'browser_result_path': '../outside/result.json'})
    plan = report_ci.ci_index('browser', 'var/ci/browser', root=tmp_path)
    browser = next(p for p in plan['profiles'] if p['id'] == 'chromium-smoke')
    assert browser['files']['result']['path'] == 'var/ci/browser/missing-browser/result.json'
    assert reporting.build_report(plan, root=tmp_path)['status'] != 'PASS'


@pytest.mark.parametrize('cleanup, expected', [('PASS', 'PASS'), ('FAIL', 'FAIL'), ('NOT_STARTED', 'INCOMPLETE')])
def test_ci_browser_requires_qualified_run_and_cleanup(tmp_path, cleanup, expected):
    source = 'var/qa/runs/owned/result.json'
    value = {'status': 'PASS', 'pytest_exit_code': 0, 'testcases': 1,
             'failure': 0, 'error': 0, 'skipped': 0}
    save(tmp_path, 'var/ci/browser/result.json', dict(value, cleanup_status=cleanup, browser_result_path=source))
    save(tmp_path, source, value)
    save(tmp_path, 'var/qa/runs/owned/tests.xml', b'<testsuite><testcase/></testsuite>')
    save(tmp_path, 'var/qa/runs/owned/browser.json', {'engine': 'chromium', 'version': '153.test'})
    plan = report_ci.ci_index('browser', 'var/ci/browser', root=tmp_path)
    report = reporting.build_report(plan, root=tmp_path)
    assert report['status'] == expected
    assert report['remote_ci'] == 'NOT_VERIFIED_BY_THIS_REPORT'


def test_ci_hermetic_success_requires_all_three_stage_evidences(tmp_path):
    checks = {'status': 'PASS', 'checks': [{'passed': True}]}
    save(tmp_path, 'var/ci/hermetic/ci-inputs.json', checks)
    save(tmp_path, 'var/ci/hermetic/unit-verification.json', unit_result())
    save(tmp_path, 'var/ci/hermetic/unit-tests.xml', JUNIT)
    save(tmp_path, 'var/ci/hermetic/ui/ui-cli-qualification.json', checks)
    plan = report_ci.ci_index('hermetic', 'var/ci/hermetic', root=tmp_path)
    assert reporting.build_report(plan, root=tmp_path)['status'] == 'PASS'
    (tmp_path / 'var/ci/hermetic/ui/ui-cli-qualification.json').unlink()
    refreshed_plan = report_ci.ci_index('hermetic', 'var/ci/hermetic', root=tmp_path)
    assert reporting.build_report(refreshed_plan, root=tmp_path)['status'] == 'INCOMPLETE'


def test_ci_main_returns_incomplete_code_for_unavailable_stages(tmp_path, monkeypatch, capsys):
    original_index = report_ci.ci_index
    monkeypatch.setattr(report_ci, 'ci_index', lambda profile, name: original_index(profile, name, root=tmp_path))
    monkeypatch.setattr(report_ci, 'build_report', lambda plan: reporting.build_report(plan, root=tmp_path))
    monkeypatch.setattr(report_ci, 'write_report', lambda report, plan, output: reporting.write_report(report, plan, output, root=tmp_path))
    monkeypatch.setattr(report_ci.sys, 'argv', ['report_ci', '--profile', 'hermetic',
                                              '--input-dir', 'var/ci/missing', '--output-dir', 'var/report/main'])
    assert report_ci.main() == 3
    assert 'CI profile: INCOMPLETE' in capsys.readouterr().out


@pytest.mark.parametrize('scope', ['current', 'release', ''])
def test_unsupported_scope_is_not_silently_accepted(tmp_path, scope):
    with pytest.raises(ValueError, match='REPORT_INDEX_INVALID'):
        reporting.build_report(index(unit_profile(tmp_path), scope=scope), root=tmp_path)


def test_duplicate_profile_ids_are_rejected(tmp_path):
    profile = unit_profile(tmp_path)
    with pytest.raises(ValueError, match='REPORT_PROFILE_INVALID'):
        reporting.build_report(index(profile, deepcopy(profile)), root=tmp_path)
