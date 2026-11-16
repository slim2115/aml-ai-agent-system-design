"""Join verified retrieval-matrix evidence to fresh template-agent decisions."""
import argparse
from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qa.dark_downstream.live import deny_network, output_path, write_new
from qa.dark_downstream.matrix_decision import DATA, SOURCE, assess, fixture, observe, selected_cases
from qa.dark_retrieval import live_matrix as matrix
from qa.dark_retrieval.matrix import digest
from src.qa_profile import checked_path

PROFILE = 'matrix-agent-decisions-1'
SOURCES = (*matrix.INVENTORY, 'qa/dark_downstream/live_decision.py', 'qa/dark_downstream/matrix_decision.py',
           'qa/dark_downstream/probe.py', 'qa/dark_downstream/live.py', 'src/runtime/context.py',
           'src/runtime/service.py', 'src/runtime/policy.py', 'src/runtime/generation.py',
           'src/runtime/events.py', 'src/contracts/semantics.py', *(SOURCE + '/' + n for n in DATA))


def bound_inputs(reference):
    path = checked_path(ROOT / reference, ROOT)
    original = matrix.replay(path)
    summary = matrix.read_doc(path / 'summary.json')
    if (summary.get('run_id') != path.name or summary.get('profile') != matrix.PROFILE
            or summary.get('measurement_status') != 'PASS' or digest(summary.get('assessment')) != digest(original)):
        raise ValueError('MATRIX_SUMMARY_MISMATCH')
    child = matrix.read_doc(path / 'child-result.json')
    rows = {}
    for case in selected_cases():
        key = case['case_id']
        doc = matrix.read_doc(path / (key + '.json'))
        if digest(doc) != child['rows_sha256'][key]:
            raise ValueError('MATRIX_ROW_MISMATCH')
        rows[key] = doc['raw']
    binding = {'matrix_run': path.relative_to(ROOT).as_posix(), 'matrix_summary_sha256': digest(summary),
               'source_sha256': {n: sha256(checked_path(ROOT / n, ROOT).read_bytes()).hexdigest() for n in SOURCES}}
    return binding, rows


def existing_output(value):
    path = checked_path(ROOT / value, ROOT)
    parts = path.relative_to(ROOT).parts
    if len(parts) != 4 or parts[:3] != ('var', 'qa', 'dark-downstream') or not path.is_dir():
        raise ValueError('INVALID_DECISION_PATH')
    return path


def inputs_for(output):
    marker = matrix.read_doc(output / 'attempt.json')
    if marker.get('profile') != PROFILE or marker.get('run_id') != output.name:
        raise ValueError('ATTEMPT_MISMATCH')
    binding, rows = bound_inputs(marker['inputs']['matrix_run'])
    if marker['inputs'] != binding:
        raise ValueError('INPUTS_CHANGED')
    return binding, rows


def child(output):
    binding, _ = inputs_for(output)
    if any(p.name != 'attempt.json' for p in output.iterdir()):
        raise ValueError('ATTEMPT_ALREADY_USED')
    try:
        from qa.tools.run_retrieval import environment
        environment(output)
        with ExitStack() as stack:
            for owner, method in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                                  (socket, 'create_connection'), (socket, 'getaddrinfo')):
                stack.enter_context(patch.object(owner, method, deny_network))
            from src.config import get_settings
            from src.retrieval.embedding import build_embedding_function, validate_embedding_artifact
            from src.runtime.retrieval import RuleIndex
            settings = get_settings()
            if settings.llm.embedding_model_name != matrix.MODEL:
                raise ValueError('MODEL_MISMATCH')
            embedding = build_embedding_function(settings)
            def factory(path):
                return RuleIndex.from_file(path, settings=settings, embedding_factory=lambda _: embedding)

            row_hashes = {}
            for case in selected_cases():
                name = case['case_id']
                doc = observe(ROOT / SOURCE, output / name, case, settings, factory)
                doc['run_id'] = output.name
                write_new(output / (name + '.json'), doc)
                row_hashes[name] = digest(doc)
            validate_embedding_artifact(settings)
            inputs_for(output)
            write_new(output / 'child-result.json', {'profile': PROFILE, 'run_id': output.name,
                      'inputs_sha256': digest(binding), 'model_id': matrix.MODEL, 'engine': 'template',
                      'dependency': 'real_local_embedding_cpu', 'row_sha256': row_hashes})
        return 0
    except Exception as exc:
        write_new(output / 'child-error.json', {'status': 'INCOMPLETE', 'error_type': type(exc).__name__})
        return 2


def replay(output):
    binding, reference = inputs_for(output)
    seal = matrix.read_doc(output / 'child-result.json')
    catalog = selected_cases()
    if (seal.get('profile') != PROFILE or seal.get('run_id') != output.name
            or seal.get('inputs_sha256') != digest(binding) or seal.get('model_id') != matrix.MODEL
            or seal.get('engine') != 'template' or seal.get('dependency') != 'real_local_embedding_cpu'
            or set(seal['row_sha256']) != {c['case_id'] for c in catalog}):
        raise ValueError('INVALID_CHILD_EVIDENCE')
    rows = []
    for case in catalog:
        name = case['case_id']
        try:
            doc = matrix.read_doc(output / (name + '.json'))
            if doc.get('run_id') != output.name or digest(doc) != seal['row_sha256'][name]:
                raise ValueError('DECISION_EVIDENCE_MISMATCH')
            for member, expected in fixture(ROOT / SOURCE, case).items():
                if checked_path(output / name / member, ROOT).read_bytes() != expected:
                    raise ValueError('FIXTURE_MISMATCH')
            rows.append(assess(case, doc, reference[name]))
        except (KeyError, TypeError, ValueError, OSError) as exc:
            rows.append({'case_id': name, 'decision_status': 'INCOMPLETE', 'trace_status': 'INCOMPLETE',
                         'error_type': type(exc).__name__})
    complete = all(r['trace_status'] == 'PASS' and r['decision_status'] != 'INCOMPLETE' for r in rows)
    outcome = 'FAIL' if any(r['decision_status'] == 'FAIL' for r in rows) else 'PASS' if complete else 'INCOMPLETE'
    return {'profile': PROFILE, 'run_id': output.name, 'cases': rows, 'decision_status': outcome,
            'measurement_status': 'PASS' if complete else 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE'}


def run(value, reference, budget):
    if not 1 <= budget <= 900:
        raise ValueError('INVALID_TIMEOUT')
    output = output_path(value)
    binding, _ = bound_inputs(reference)
    output.mkdir(parents=True, exist_ok=False)
    write_new(output / 'attempt.json', {'profile': PROFILE, 'run_id': output.name, 'timeout_seconds': budget,
                                        'inputs': binding})
    summary = {'profile': PROFILE, 'run_id': output.name, 'decision_status': 'INCOMPLETE',
               'measurement_status': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE'}
    try:
        completed = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--child', str(output)],
                                   cwd=ROOT, timeout=budget, check=False)
        if completed.returncode:
            summary['reason'] = 'CHILD_ERROR'
        else:
            summary = replay(output)
    except subprocess.TimeoutExpired:
        summary['reason'] = 'CHILD_TIMEOUT'
    except (OSError, ValueError, KeyError, TypeError):
        summary['reason'] = 'EVIDENCE_INCOMPLETE'
    write_new(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output-dir')
    group.add_argument('--replay')
    group.add_argument('--child', help=argparse.SUPPRESS)
    parser.add_argument('--matrix-run')
    parser.add_argument('--timeout-seconds', type=int, default=300)
    args = parser.parse_args()
    try:
        if args.child:
            return child(existing_output(args.child))
        if args.replay:
            output = existing_output(args.replay)
            result = replay(output)
            if digest(matrix.read_doc(output / 'summary.json')) != digest(result):
                raise ValueError('SUMMARY_MISMATCH')
        else:
            if not args.matrix_run:
                parser.error('--matrix-run is required when starting a run')
            result = run(args.output_dir, args.matrix_run, args.timeout_seconds)
        print(json.dumps({k: v for k, v in result.items() if k != 'cases'}))
        if result['decision_status'] == 'FAIL':
            return 1
        return 0 if result['measurement_status'] == 'PASS' else 2
    except (KeyError, TypeError, ValueError, OSError) as exc:
        print(f'INCOMPLETE: {type(exc).__name__}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
