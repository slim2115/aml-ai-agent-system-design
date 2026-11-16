"""Qualify frozen synthetic scenarios with explicit model doubles, offline."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def deny_network(*_args, **_kwargs):
    raise RuntimeError('NETWORK_DISABLED_IN_SCENARIO_QUALIFICATION')


def output_path(value):
    from src.qa_profile import checked_path
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    path = checked_path(path, ROOT)
    relative = path.relative_to(ROOT)
    if (len(relative.parts) < 2 or relative.parts[0] not in {'verification', 'var'}
            or not relative.parts[1].startswith('qa-step-03-')):
        raise ValueError('OUTPUT_REQUIRES_NEW_QA_STEP_03_DIRECTORY')
    if path.exists():
        raise FileExistsError('QA_EVIDENCE_ALREADY_EXISTS')
    return path


def write(directory, name, value):
    with (directory / name).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def qualify(directory, split):
    from qa.scenarios import load_catalog
    from qa.scenario_checks import check_observation, run_scenario
    from src.evaluation import evaluate_bundle
    from src.evaluation.models import ReplayBundle
    dataset_dir = ROOT / 'qa/datasets/aml-v1'
    catalog = load_catalog(dataset_dir)
    selected = [case for case in catalog.cases if split == 'all' or case.split == split]
    if not selected:
        raise ValueError('NO_SELECTED_CASES')
    citations = {}
    for scenario in selected:
        if scenario.golden:
            for rule in scenario.golden.rules:
                citation = rule.citation
                citations[(citation.rule_id, citation.rule_version, citation.chunk_id)] = citation.model_dump(mode='json')
    replay = {'schema_version': '1.0', 'dataset': catalog.dataset.model_dump(mode='json'),
              'cases': [scenario.case.model_dump(mode='json') for scenario in selected],
              'outcomes': [], 'citation_catalog': list(citations.values()), 'external_measurements': {}}
    ReplayBundle.model_validate(replay)
    directory.mkdir(parents=True, exist_ok=False)
    write(directory, 'replay-template.json', replay)
    rows = []
    for scenario in selected:
        try:
            observed = run_scenario(scenario, dataset_dir / scenario.fixture)
            issues = check_observation(scenario, observed)
            outcome = {'case_id': scenario.case.id, 'incident_id': scenario.case.incident_id,
                       'result': observed.result, 'latency_seconds': observed.latency_seconds}
            if scenario.case.expected_retrieval is not None:
                outcome['retrieval'] = {'namespace': 'rule_id', 'ids': sorted(set(observed.selected_rule_ids))}
            replay['outcomes'].append(outcome)
            rows.append({'case_id': scenario.case.id, 'split': scenario.split,
                         'status': 'FAIL' if issues else 'PASS', 'issues': issues,
                         'model_double_calls': len(observed.model_prompts), 'events': observed.events})
        except Exception as exc:
            replay['outcomes'].append({'case_id': scenario.case.id, 'incident_id': scenario.case.incident_id,
                                       'failure_code': 'RUNNER_EXCEPTION'})
            rows.append({'case_id': scenario.case.id, 'split': scenario.split, 'status': 'ERROR',
                         'issues': ['RUNNER_EXCEPTION'], 'error_type': type(exc).__name__})
    ReplayBundle.model_validate(replay)
    write(directory, 'replay.json', replay)
    evaluation = evaluate_bundle(replay)
    write(directory, 'evaluation.json', evaluation)
    qualification_issues = ['EVALUATOR_MANDATORY_GATE_FAILED'] if evaluation['verdict'] == 'FAIL' else []
    from hashlib import sha256
    result = {
        'checked_at_utc': datetime.now(timezone.utc).isoformat(),
        'status': 'ERROR' if any(row['status'] == 'ERROR' for row in rows) else
                  'FAIL' if any(row['status'] == 'FAIL' for row in rows) or qualification_issues else 'PASS',
        'scope': 'Frozen scenario qualification: real context/contracts/editions/chunks/agent/template; '
                 'fake whitespace tokenizer, equal fake vectors and scripted model replies. '
                 'No browser, services or real models.',
        'dataset_id': catalog.dataset.id, 'dataset_version': catalog.dataset_version,
        'dataset_manifest_sha256': sha256((dataset_dir / 'manifest.json').read_bytes()).hexdigest(),
        'split': split, 'case_count': len(rows),
        'independence_families': len({case.case.independence_id for case in selected}),
        'real_model_calls': 0, 'retrieval_scope': 'Observed policy-selected rule IDs at as_applicable; not semantic ranking quality.',
        'product_acceptance': evaluation['verdict'], 'qualification_issues': qualification_issues, 'cases': rows,
        'heldout_policy': 'Contract smoke may run both splits; no Judge rubric/model tuning performed. '
                          'Heldout in an accessible local repository is organizational, not blind.',
    }
    write(directory, 'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--split', choices=['all', 'calibration', 'heldout'], default='all')
    args = parser.parse_args()
    try:
        directory = output_path(args.output_dir)
        with ExitStack() as guards:
            for name in ('connect', 'connect_ex'):
                guards.enter_context(patch.object(socket.socket, name, deny_network))
            guards.enter_context(patch.object(socket, 'create_connection', deny_network))
            result = qualify(directory, args.split)
        print(json.dumps({key: result[key] for key in ('status', 'scope', 'case_count',
                                                      'independence_families', 'product_acceptance')}, ensure_ascii=False))
        return {'PASS': 0, 'FAIL': 1, 'ERROR': 2}[result['status']]
    except Exception as exc:
        print(json.dumps({'status': 'ERROR', 'error_type': type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
