"""Compare retained template and first LLM observations; no new model calls.

All checks concern locally saved synthetic evidence. A local manifest is not an
independent signature, a secret holdout, or expert/product acceptance.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from qa.dark_compare.comparison import compare
from qa.dark_factory.baseline import _read_member, _strict_json, write_new

EVIDENCE = 'verification/qa-step-07-2026-10-20'
PILOT = EVIDENCE + '/pilot-first'
CATALOG = 'qa/datasets/aml-v1'
CASES = {'QA-S01': 'FAMILY-STRUCTURING', 'QA-S02': 'FAMILY-STRUCTURING',
         'QA-S03': 'FAMILY-HIGH-RISK', 'QA-S04': 'FAMILY-THRESHOLD',
         'QA-S14': 'FAMILY-HIGH-RISK'}
EXPECTED_MODEL = '7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6'
_NO_MODEL = sha256(b'comparison-sentinel:template-has-no-llm').hexdigest()
_NO_PROMPT = sha256(b'comparison-sentinel:template-has-no-llm-prompt').hexdigest()


def _json(root, path):
    data = _read_member(root, path)
    value = _strict_json(data)
    if type(value) is not dict:
        raise ValueError('INVALID_OBJECT')
    return value, data


def _entries(document):
    rows = document.get('files')
    if type(rows) is not list or not rows:
        raise ValueError('INVALID_MANIFEST')
    result = {}
    for item in rows:
        if (type(item) is not dict or type(item.get('path')) is not str
                or item['path'] in result or type(item.get('bytes')) is not int
                or item['bytes'] < 0 or type(item.get('sha256')) is not str):
            raise ValueError('INVALID_MANIFEST')
        result[item['path']] = item
    return result


def _checked(root, path, manifest, *, manifest_name=None):
    data = _read_member(root, path)
    key = path if manifest_name is None else manifest_name
    entry = manifest.get(key)
    if not entry or entry['bytes'] != len(data) or entry['sha256'] != sha256(data).hexdigest():
        raise ValueError('EVIDENCE_HASH_MISMATCH')
    parsed = _strict_json(data)
    if type(parsed) is not dict:
        raise ValueError('INVALID_OBJECT')
    return parsed, data


def _first_model_status(record, case_id, family):
    evaluation = record['evaluation']
    if (type(evaluation) is not dict or evaluation.get('case_id') != case_id
            or evaluation.get('independence_id') != family or evaluation.get('repeat_index') != 1
            or type(evaluation.get('issues')) is not list or type(evaluation.get('fact_issues')) is not list
            or evaluation.get('status') not in {'PASS', 'FAIL', 'INCOMPLETE'}):
        raise ValueError('INVALID_EVALUATION')
    if evaluation['status'] == 'FAIL' or evaluation['issues'] or evaluation['fact_issues']:
        return 'FAIL'
    attempts = record.get('http_attempts')
    if type(attempts) is not list or not attempts:
        return 'INCOMPLETE'
    first = attempts[0]
    if type(first) is not dict or type(first.get('completion')) is not dict:
        return 'INCOMPLETE'
    stored = first['completion'].get('valid_complete_selection')
    if type(stored) is not bool or type(first.get('response_json')) is not dict or type(first.get('request_body')) is not str:
        return 'INCOMPLETE'
    # Recompute rather than trusting a saved Boolean. Raw content is not emitted.
    from qa.tools.run_llm import completion_metadata
    rebuilt = completion_metadata(first)
    if type(rebuilt.get('valid_complete_selection')) is not bool or rebuilt['valid_complete_selection'] != stored:
        raise ValueError('INCONSISTENT_FIRST_ATTEMPT')
    if not stored:
        return 'FAIL'
    if evaluation['status'] != 'PASS' or record.get('transport_outcome') != 'completed':
        return 'INCOMPLETE'
    return 'PASS'


def build(root: Path = ROOT) -> dict:
    """Build an aggregate or return INCOMPLETE on missing/invalid evidence."""
    root = Path(root).resolve(strict=True)
    incomplete = {'status': 'INCOMPLETE', 'human_acceptance': 'INCOMPLETE',
                  'product_acceptance': 'INCOMPLETE', 'hidden_holdout': 'NOT_ESTABLISHED',
                  'scope': 'Historical synthetic evidence only; no new model calls.'}
    try:
        integrity, _ = _json(root, EVIDENCE + '/artifact-integrity.json')
        manifest, manifest_raw = _json(root, EVIDENCE + '/file-manifest.json')
        if (integrity.get('status') != 'PASS'
                or integrity.get('manifest_sha256') != sha256(manifest_raw).hexdigest()):
            raise ValueError('INVALID_SAVED_MANIFEST')
        inventory = _entries(manifest)
        qualification, _ = _checked(root, PILOT + '/qualification.json', inventory)
        checks = qualification.get('checks')
        if (qualification.get('status') != 'PASS' or qualification.get('collection_status') != 'PASS'
                or qualification.get('product_acceptance') != 'INCOMPLETE'
                or type(checks) is not list or not checks or qualification.get('check_count') != len(checks)
                or qualification.get('failed_checks') != []
                or any(type(x) is not dict or x.get('status') != 'PASS' for x in checks)):
            raise ValueError('UNQUALIFIED_SOURCE')
        run, _ = _checked(root, PILOT + '/run.json', inventory)
        result, _ = _checked(root, PILOT + '/result.json', inventory)
        source_hashes, source_raw = _checked(root, PILOT + '/source-hashes.json', inventory)
        catalog_manifest, catalog_raw = _checked(root, CATALOG + '/manifest.json', inventory)
        catalog_members = _entries(catalog_manifest)
        catalog, _ = _checked(root, CATALOG + '/cases.json', catalog_members, manifest_name='cases.json')
        if (run.get('model') != 'qwen2.5:14b' or run.get('model_digest') != EXPECTED_MODEL
                or not set(CASES).issubset(set(run.get('selected_case_ids', [])))
                or result.get('product_acceptance') != 'INCOMPLETE'
                or result.get('status') not in {'PASS', 'FAIL', 'INCOMPLETE'}):
            raise ValueError('INVALID_RUN')
        code_hash = sha256(source_raw).hexdigest()  # Hash of measured source-hash inventory.
        prompt_source_hash = source_hashes['src/runtime/generation.py']
        if type(prompt_source_hash) is not str or len(prompt_source_hash) != 64:
            raise ValueError('INVALID_PROMPT_SOURCE_ID')
        scenarios = catalog.get('cases')
        if type(scenarios) is not list:
            raise ValueError('INVALID_CATALOG')
        indexed = {}
        for row in scenarios:
            if type(row) is not dict or type(row.get('case')) is not dict:
                raise ValueError('INVALID_CASE')
            cid = row['case'].get('id')
            if cid in indexed:
                raise ValueError('DUPLICATE_CASE')
            indexed[cid] = row
        if not set(CASES).issubset(indexed):
            raise ValueError('MISSING_CASE')
        spec = {'catalog_sha256': sha256(catalog_raw).hexdigest(), 'cases': []}
        base_attempts, model_attempts = [], []
        for cid, family in CASES.items():
            scenario = indexed[cid]
            if scenario['case'].get('independence_id') != family:
                raise ValueError('FAMILY_MISMATCH')
            identity = {'case_id': cid, 'family': family,
                        'case_sha256': sha256(json.dumps(scenario, sort_keys=True, ensure_ascii=False,
                                                         separators=(',', ':'), allow_nan=False).encode()).hexdigest()}
            spec['cases'].append(identity)
            template, _ = _checked(root, f'{PILOT}/cases/{cid}/template.json', inventory)
            repeat, _ = _checked(root, f'{PILOT}/cases/{cid}/repeat-01.json', inventory)
            issues = template.get('issues')
            observation = template.get('observation')
            if type(issues) is not list or type(observation) is not dict or type(observation.get('result')) is not dict:
                raise ValueError('INVALID_TEMPLATE')
            base_status = 'FAIL' if issues or observation['result'].get('status') != 'draft' else 'PASS'
            base_attempts.append({**identity, 'attempt': 1, 'status': base_status})
            model_attempts.append({**identity, 'attempt': 1,
                                   'status': _first_model_status(repeat, cid, family)})
        base = {'run_id': 'historical-template-first', 'catalog_sha256': spec['catalog_sha256'],
                'code_sha256': code_hash, 'model_sha256': _NO_MODEL, 'prompt_sha256': _NO_PROMPT,
                'attempts': base_attempts}
        model = {'run_id': 'historical-llm-first', 'catalog_sha256': spec['catalog_sha256'],
                 'code_sha256': code_hash, 'model_sha256': EXPECTED_MODEL,
                 'prompt_sha256': prompt_source_hash, 'attempts': model_attempts}
        report = compare(spec, base, model)
        # Sentinels satisfy the comparator contract but are not model identities.
        report['baseline_metadata']['model_sha256'] = None
        report['baseline_metadata']['prompt_sha256'] = None
        failed = result['status'] == 'FAIL' or any(row['baseline_first'] == 'FAIL' or row['candidate_first'] == 'FAIL'
                                                   for row in report['cases'])
        report.update(status='FAIL' if failed else 'PASS' if report['comparison_status'] == 'MEASURED'
                      and result['status'] == 'PASS' else 'INCOMPLETE',
                      product_acceptance='INCOMPLETE', human_acceptance='INCOMPLETE',
                      historical_collection_status=result['status'],
                      scope='Historical synthetic first repeats only; no new inference or independent holdout.',
                      identity_note='code_sha256 hashes the retained source-hash inventory; prompt_sha256 identifies the generation source, not an individual prompt.',
                      evidence_note='Hashes check retained local records against a locally held manifest, not an independent signature.',
                      template_model='NOT_APPLICABLE', model_name='qwen2.5:14b')
        return report
    except (OSError, ValueError, TypeError, KeyError, AttributeError, StopIteration, RecursionError):
        incomplete['reason'] = 'MISSING_OR_INVALID_SAVED_EVIDENCE'
        return incomplete


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    result = build()
    try:
        output = write_new(result, args.output)
    except (OSError, ValueError):
        print('Invalid or existing output', file=sys.stderr)
        return 2
    print(json.dumps({'status': result['status'], 'output': output.relative_to(ROOT).as_posix(),
                      'product_acceptance': result['product_acceptance']}))
    return 0 if result['status'] == 'PASS' else 1 if result['status'] == 'FAIL' else 2


if __name__ == '__main__':
    raise SystemExit(main())
