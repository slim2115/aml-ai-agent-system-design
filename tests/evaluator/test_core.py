from copy import deepcopy
import json
import unittest

from src.evaluation import BundleInputError, evaluate_bundle, quality_gates, run_cases
from src.evaluation.gates import (ACCEPTANCE, EVIDENCE, FAITHFULNESS, LATENCY_MAX, LATENCY_P95,
                                  PRECISION, RECALL, REFUSAL, RELEVANCE, SCHEMA, TRACEABILITY)
from tests.evaluator.fixtures import bundle, external_controls


def by_gate(report, name):
    return next(g for g in report['gates'] if g['name'] == name)


class EvaluatorControls(unittest.TestCase):
    def test_empty_is_incomplete_without_perfect_ratios(self):
        result = evaluate_bundle(bundle(0))
        self.assertEqual('INCOMPLETE', result['verdict'])
        for metric in result['metrics'].values():
            self.assertIsNone(metric['value'])
            self.assertEqual(0, metric['denominator'])

    def test_synthetic_baseline_never_proves_acceptance(self):
        data = bundle(30)
        data['external_measurements'] = external_controls()
        result = evaluate_bundle(data)
        self.assertEqual('INCOMPLETE', result['verdict'])
        self.assertEqual('PASS', by_gate(result, 'Dataset Coverage')['status'])
        self.assertEqual('INCOMPLETE', by_gate(result, 'Acceptance Evidence Kind')['status'])

    def test_external_metrics_are_required(self):
        result = evaluate_bundle(bundle())
        for name in (FAITHFULNESS, RELEVANCE, ACCEPTANCE):
            self.assertEqual('INCOMPLETE', by_gate(result, name)['status'])

    def test_external_missing_evidence_or_denominator_is_not_measured(self):
        for field, value in [('evidence_references', []), ('denominator', 0), ('value', None),
                             ('status', 'not_measured'), ('method', None), ('method', '  ')]:
            with self.subTest(field=field):
                data = bundle()
                data['external_measurements'] = external_controls()
                data['external_measurements']['M-02'][field] = value
                self.assertIsNone(evaluate_bundle(data)['metrics'][FAITHFULNESS]['value'])

    def test_external_failure_blocks_even_with_incomplete_coverage(self):
        data = bundle()
        data['external_measurements'] = external_controls(.1)
        self.assertEqual('FAIL', evaluate_bundle(data)['verdict'])

    def test_missing_outcome_cannot_disappear(self):
        data = bundle()
        data['outcomes'].pop()
        result = evaluate_bundle(data)
        self.assertEqual('INCOMPLETE', result['verdict'])
        self.assertEqual(1, result['coverage']['missing_outcome_count'])

    def test_duplicate_and_orphan_outcomes_fail(self):
        for orphan in (False, True):
            with self.subTest(orphan=orphan):
                data = bundle()
                outcome = deepcopy(data['outcomes'][0])
                if orphan:
                    outcome['case_id'] = 'UNREGISTERED'
                data['outcomes'].append(outcome)
                self.assertEqual('FAIL', evaluate_bundle(data)['verdict'])

    def test_status_mismatch_is_blocking(self):
        data = bundle()
        data['outcomes'][0]['result'] = deepcopy(data['outcomes'][2]['result'])
        self.assertEqual('FAIL', evaluate_bundle(data)['verdict'])

    def test_envelope_and_result_incident_must_match_case(self):
        for envelope in (True, False):
            with self.subTest(envelope=envelope):
                data = bundle()
                target = data['outcomes'][0] if envelope else data['outcomes'][0]['result']
                target['incident_id'] = 'INC-999999'
                self.assertEqual('FAIL', evaluate_bundle(data)['verdict'])

    def test_schema_is_checked_for_refusals(self):
        data = bundle()
        data['outcomes'][2]['result']['found_facts'] = []
        result = evaluate_bundle(data)
        self.assertEqual(5, result['metrics'][SCHEMA]['denominator'])
        self.assertEqual(4, result['metrics'][SCHEMA]['numerator'])
        self.assertEqual('FAIL', result['verdict'])

    def test_error_contract_and_expected_code_are_checked(self):
        data = bundle(1)
        data['cases'][0].update(expected_status='error', expected_reason_code='NOT_FOUND')
        data['outcomes'][0]['result'] = {'schema_version': '1.0', 'status': 'error',
            'generated_at': '2026-02-01T00:00:00Z', 'correlation_id': 'CORR-1',
            'code': 'NOT_FOUND', 'message': 'Synthetic error', 'retryable': False}
        self.assertEqual('INCOMPLETE', evaluate_bundle(data)['verdict'])
        data['outcomes'][0]['result']['code'] = 'INTERNAL_ERROR'
        self.assertEqual('FAIL', evaluate_bundle(data)['verdict'])

    def test_raw_invalid_incident_controls_do_not_invent_valid_incidents(self):
        for raw, expected in [(None, 'MISSING_INPUT'), ('', 'MISSING_INPUT'), ('  ', 'MISSING_INPUT'),
                              ('INC-000001\n', 'INVALID_INPUT_FORMAT'), ("'; DROP TABLE incidents;--", 'INVALID_INPUT_FORMAT'),
                              ('BAD-ID', 'INVALID_INPUT_FORMAT'), (True, 'INVALID_INPUT_FORMAT'),
                              (123, 'INVALID_INPUT_FORMAT'), ({'id': 'INC-000001'}, 'INVALID_INPUT_FORMAT')]:
            with self.subTest(raw=raw):
                data = bundle(1)
                data['cases'][0].update(incident_id=raw, expected_status='error', expected_reason_code=expected, context=None)
                data['outcomes'][0].update(incident_id=raw, result={
                    'schema_version': '1.0', 'status': 'error', 'generated_at': '2026-02-01T00:00:00Z',
                    'correlation_id': 'CORR-1', 'code': expected, 'message': 'Input rejected', 'retryable': False})
                result = evaluate_bundle(data)
                self.assertEqual('INCOMPLETE', result['verdict'])
                self.assertEqual('PASS', by_gate(result, 'Behavior Correctness')['status'])
                self.assertEqual([], result['cases'][0]['issues'])

    def test_invalid_incident_must_have_correct_expected_input_error(self):
        for status, reason in [('draft', None), ('refusal', 'UNTRUSTED_INPUT'), ('error', 'NOT_FOUND')]:
            data = bundle(1)
            data['cases'][0].update(incident_id='BAD-ID', expected_status=status, expected_reason_code=reason, context=None)
            with self.subTest(status=status), self.assertRaises(BundleInputError):
                evaluate_bundle(data)

    def test_refusal_and_error_reason_namespaces_cannot_be_mixed(self):
        for status, reason in [('refusal', 'NOT_FOUND'), ('error', 'UNTRUSTED_INPUT'), ('draft', 'INTERNAL_ERROR')]:
            data = bundle(1)
            data['cases'][0].update(expected_status=status, expected_reason_code=reason)
            with self.subTest(status=status), self.assertRaises(BundleInputError):
                evaluate_bundle(data)

    def test_absent_and_different_type_raw_incident_envelopes_fail(self):
        data = bundle(1)
        data['cases'][0].update(incident_id=True, expected_status='error', expected_reason_code='INVALID_INPUT_FORMAT', context=None)
        data['outcomes'][0].update(incident_id=1, result={
            'schema_version': '1.0', 'status': 'error', 'generated_at': '2026-02-01T00:00:00Z',
            'correlation_id': 'CORR-1', 'code': 'INVALID_INPUT_FORMAT', 'message': 'Rejected', 'retryable': False})
        self.assertEqual('FAIL', evaluate_bundle(data)['verdict'])
        data['cases'][0].update(incident_id=None, expected_reason_code='MISSING_INPUT')
        data['outcomes'][0]['result']['code'] = 'MISSING_INPUT'
        del data['outcomes'][0]['incident_id']
        self.assertEqual('FAIL', evaluate_bundle(data)['verdict'])

    def test_wrong_refusal_reason_fails(self):
        data = bundle()
        data['outcomes'][2]['result']['reason_code'] = 'INSUFFICIENT_DATA'
        self.assertEqual('FAIL', evaluate_bundle(data)['verdict'])

    def test_missing_expected_reason_is_incomplete(self):
        data = bundle()
        del data['cases'][2]['expected_reason_code']
        result = evaluate_bundle(data)
        self.assertEqual('INCOMPLETE', by_gate(result, 'Measurement Completeness')['status'])

    def test_forbidden_outputs_checked_in_all_statuses(self):
        for index, field in [(0, 'rule_text'), (2, 'reason')]:
            with self.subTest(index=index):
                data = bundle()
                result = data['outcomes'][index]['result']
                target = result['applicable_rules'][0] if index == 0 else result
                target[field] = 'SYNTHETIC-FORBIDDEN-OUTPUT'
                self.assertEqual('FAIL', evaluate_bundle(data)['verdict'])

    def test_forbidden_output_uses_decoded_text(self):
        data = bundle()
        data['cases'][2]['forbidden_outputs'] = ['BLOCK\nACCOUNT "NOW"']
        data['outcomes'][2]['result']['reason'] = 'Do BLOCK\nACCOUNT "NOW" immediately'
        result = evaluate_bundle(data)
        self.assertIn('FORBIDDEN_OUTPUT', result['cases'][2]['issues'])
        self.assertEqual('FAIL', result['verdict'])

    def test_runner_failure_code_cannot_pass_with_valid_result(self):
        data = bundle()
        data['outcomes'][0]['failure_code'] = 'RUNNER_EXCEPTION'
        self.assertEqual('FAIL', evaluate_bundle(data)['verdict'])

    def test_critical_category_c_failure_is_not_averaged_away(self):
        data = bundle(20)
        for index in range(20):
            refusal = deepcopy(bundle(5)['outcomes'][2]['result'])
            refusal['incident_id'] = data['cases'][index]['incident_id']
            data['cases'][index].update(expected_status='refusal', expected_reason_code='UNTRUSTED_INPUT')
            data['outcomes'][index]['result'] = refusal
        data['outcomes'][2]['result']['reason_code'] = 'INSUFFICIENT_DATA'
        result = evaluate_bundle(data)
        self.assertEqual(.95, result['metrics'][REFUSAL]['value'])
        self.assertEqual('PASS', by_gate(result, REFUSAL)['status'])
        self.assertEqual('FAIL', by_gate(result, 'Critical Category C')['status'])
        self.assertEqual('FAIL', result['verdict'])

    def test_retrieval_is_independent_of_answer_citations(self):
        data = bundle()
        for item in data['outcomes']:
            item['retrieval']['ids'] = []
        result = evaluate_bundle(data)
        self.assertEqual(0, result['metrics'][RECALL]['value'])
        self.assertIsNone(result['metrics'][PRECISION]['value'])
        self.assertEqual(1, result['metrics'][TRACEABILITY]['value'])
        self.assertEqual('FAIL', result['verdict'])

    def test_precision_detects_surplus_retrieval(self):
        data = bundle()
        data['outcomes'][0]['retrieval']['ids'].append('UNRELATED-RULE')
        result = evaluate_bundle(data)
        self.assertEqual(1, result['metrics'][RECALL]['value'])
        self.assertLess(result['metrics'][PRECISION]['value'], 1)
        self.assertEqual('FAIL', result['verdict'])

    def test_retrieval_namespace_mismatch_fails(self):
        data = bundle()
        data['outcomes'][0]['retrieval']['namespace'] = 'source_ref'
        self.assertEqual('FAIL', evaluate_bundle(data)['verdict'])

    def test_source_ref_retrieval_namespace_is_supported(self):
        data = bundle()
        for case, outcome in zip(data['cases'], data['outcomes']):
            for retrieval in (case['expected_retrieval'], outcome['retrieval']):
                retrieval.update(namespace='source_ref', ids=['Synthetic rule paragraph 1'])
        self.assertEqual(1, evaluate_bundle(data)['metrics'][RECALL]['value'])

    def test_missing_retrieval_remains_incomplete(self):
        data = bundle()
        del data['outcomes'][0]['retrieval']
        self.assertEqual('INCOMPLETE', evaluate_bundle(data)['verdict'])

    def test_traceability_checks_patterns_full_citation_and_rule_text(self):
        for target in ('pattern', 'rule_text'):
            with self.subTest(target=target):
                data = bundle()
                report = data['outcomes'][0]['result']
                if target == 'rule_text':
                    report['applicable_rules'][0]['rule_text'] = 'Tampered text with valid citation'
                else:
                    pattern = {'pattern': 'Synthetic pattern', 'evidence': deepcopy(report['found_facts'][0]['evidence']),
                               'sources': deepcopy(report['found_facts'][0]['sources'])}
                    pattern['sources'][0]['content_sha256'] = 'c' * 64
                    report['suspicious_patterns'].append(pattern)
                self.assertEqual('FAIL', evaluate_bundle(data)['verdict'])

    def test_existing_source_ref_does_not_justify_wrong_rule_id(self):
        data = bundle()
        report = data['outcomes'][0]['result']
        report['applicable_rules'][0]['citation']['rule_id'] = 'INVENTED-RULE'
        report['found_facts'][0]['sources'][0]['rule_id'] = 'INVENTED-RULE'
        result = evaluate_bundle(data)
        self.assertLess(result['metrics'][TRACEABILITY]['value'], 1)
        self.assertEqual('FAIL', result['verdict'])

    def test_free_text_grounding_limitation_is_disclosed(self):
        data = bundle()
        data['outcomes'][0]['result']['found_facts'][0]['fact'] = 'Unverified prose claims an amount of 999999 RUB'
        result = evaluate_bundle(data)
        self.assertEqual(1, result['metrics'][EVIDENCE]['value'])
        self.assertTrue(any('Free-text factual grounding remains pending' in item for item in result['limitations']))

    def test_nonnormative_facts_may_omit_rule_sources_per_v1(self):
        data = bundle()
        data['outcomes'][0]['result']['found_facts'][0]['sources'] = []
        self.assertEqual(1, evaluate_bundle(data)['metrics'][TRACEABILITY]['value'])

    def test_unresolved_evidence_and_snapshot_fail(self):
        for key, value in [('tx_id', 'UNKNOWN'), ('snapshot_id', 'WRONG')]:
            with self.subTest(key=key):
                data = bundle()
                data['outcomes'][0]['result']['found_facts'][0]['evidence'][0][key] = value
                result = evaluate_bundle(data)
                self.assertLess(result['metrics'][EVIDENCE]['value'], 1)
                self.assertEqual('FAIL', result['verdict'])

    def test_missing_context_or_catalog_is_incomplete(self):
        for field in ('context', 'citation_catalog'):
            with self.subTest(field=field):
                data = bundle()
                if field == 'context':
                    del data['cases'][0]['context']
                else:
                    data[field] = []
                self.assertEqual('INCOMPLETE', evaluate_bundle(data)['verdict'])

    def test_invalid_evidence_field_is_schema_failure(self):
        data = bundle()
        data['outcomes'][0]['result']['found_facts'][0]['evidence'][0]['field'] = 'secret'
        self.assertEqual('FAIL', evaluate_bundle(data)['verdict'])

    def test_latency_raw_boundary_and_nearest_rank_p95(self):
        data = bundle(20)
        data['outcomes'][-1]['latency_seconds'] = 180.00001
        result = evaluate_bundle(data)
        self.assertEqual(180.00001, result['metrics'][LATENCY_MAX]['value'])
        self.assertEqual(1.0, result['metrics'][LATENCY_P95]['value'])
        self.assertEqual('FAIL', by_gate(result, LATENCY_MAX)['status'])
        self.assertEqual('PASS', by_gate(result, LATENCY_P95)['status'])

    def test_settings_cannot_weaken_plan_thresholds(self):
        data = bundle()
        data['outcomes'][0]['latency_seconds'] = 600.04
        for settings in ({'performance': {'generation_timeout_sec': 600}},
                         {'performance': {'generation_timeout_sec': 0}},
                         {'thresholds': {'rag_recall': .5}}, {'thresholds': {'schema_match': .9}},
                         {'thresholds': {'rag_precision': .5}}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                evaluate_bundle(data, settings)

    def test_missing_latency_not_zero(self):
        data = bundle()
        for outcome in data['outcomes']:
            del outcome['latency_seconds']
        self.assertIsNone(evaluate_bundle(data)['metrics'][LATENCY_MAX]['value'])

    def test_independence_and_category_coverage_are_required(self):
        data = bundle(30)
        for case in data['cases']:
            case['independence_id'] = 'SAME-SCENARIO'
        self.assertEqual('INCOMPLETE', by_gate(evaluate_bundle(data), 'Dataset Coverage')['status'])

    def test_sme_metadata_is_required_and_never_fabricated(self):
        data = bundle(30)
        data['dataset']['kind'] = 'acceptance'
        data['external_measurements'] = external_controls()
        self.assertEqual('INCOMPLETE', by_gate(evaluate_bundle(data), 'SME Dataset Acceptance')['status'])

    def test_no_legacy_or_malformed_bundle_coercion(self):
        for data in [[], {'cases': []}, {'schema_version': '0.1'}, {'cases': [], 'outcomes': [],
                      'schema_version': '1.0', 'dataset': {'id': 'legacy', 'kind': 'synthetic_control'}, 'legacy': True}]:
            with self.subTest(data=data):
                with self.assertRaises(BundleInputError):
                    evaluate_bundle(data)

    def test_nonfinite_and_boolean_measurements_rejected(self):
        for value in (float('nan'), float('inf'), True, -1):
            with self.subTest(value=value):
                data = bundle()
                data['outcomes'][0]['latency_seconds'] = value
                with self.assertRaises(BundleInputError):
                    evaluate_bundle(data)

    def test_public_gates_reject_invalid_measurement_ranges_and_denominators(self):
        for name, value, denominator in [(LATENCY_MAX, -1, 1), (SCHEMA, 2, 1),
                                          (SCHEMA, 1, True), (SCHEMA, 1, .5), (SCHEMA, 1, '1')]:
            with self.subTest(name=name, value=value, denominator=denominator):
                gates = quality_gates({name: {'status': 'measured', 'value': value, 'denominator': denominator}})
                self.assertEqual('INCOMPLETE', next(item for item in gates if item['name'] == name)['status'])

    def test_runner_exception_preserves_case_and_continues_without_sensitive_text(self):
        cases = bundle()['cases']
        called = []
        def runner(case):
            called.append(case['id'])
            if len(called) == 2:
                raise RuntimeError('PRIVATE_TOKEN=do-not-serialize')
            return {'result': None}
        outcomes = run_cases(cases, runner)
        self.assertEqual(len(cases), len(outcomes))
        self.assertEqual(len(cases), len(called))
        self.assertEqual('RUNNER_EXCEPTION', outcomes[1]['failure_code'])
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(outcomes))

    def test_pure_api_does_not_mutate_input_and_shares_gates(self):
        data = bundle()
        before = deepcopy(data)
        result = evaluate_bundle(data)
        self.assertEqual(before, data)
        self.assertEqual(quality_gates(result['metrics']), result['gates'][:11])


if __name__ == '__main__':
    unittest.main()
