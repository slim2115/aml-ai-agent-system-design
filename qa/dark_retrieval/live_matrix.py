"""Bounded local MiniLM run for retrieval-matrix-1 and offline evidence replay."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qa.dark_retrieval.live import MANIFEST, SOURCES, deny_network, output_path, validate_child_output, write_new
from qa.dark_retrieval.matrix import SELECTORS, digest, measure, score
from qa.dark_retrieval.matrix_cases import cases
from src.qa_profile import checked_path
from src.runtime.transport import strict_json

PROFILE = 'retrieval-matrix-1'
MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
INVENTORY = (*SOURCES, 'qa/dark_retrieval/matrix_cases.py', 'qa/dark_retrieval/matrix.py',
             'qa/dark_retrieval/live_matrix.py', 'src/contracts/v1.py', 'src/qa_profile.py',
             'src/runtime/transport.py')


def hashes():
    result = {}
    for name in INVENTORY:
        path = checked_path(ROOT / name, ROOT)
        result[name] = sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return result


def read_doc(path):
    path = checked_path(path, ROOT)
    if path.stat().st_size > 2_000_000:
        raise ValueError('EVIDENCE_TOO_LARGE')
    doc = strict_json(path.read_bytes())
    if type(doc) is not dict:
        raise ValueError('INVALID_EVIDENCE')
    return doc


def attempt(output, budget):
    return {'profile': PROFILE, 'status': 'STARTED', 'run_id': output.name,
            'timeout_seconds': budget, 'model_id': MODEL, 'catalog_sha256': digest(cases()),
            'planned_cases': len(cases()), 'selectors': SELECTORS, 'source_sha256_before': hashes()}


def verified_attempt(output):
    marker = read_doc(output / 'attempt.json')
    budget = marker.get('timeout_seconds')
    if type(budget) is not int or not 1 <= budget <= 900 or marker != attempt(output, budget):
        raise ValueError('ATTEMPT_OR_SOURCE_MISMATCH')
    if None in marker['source_sha256_before'].values():
        raise ValueError('REQUIRED_SOURCE_UNAVAILABLE')
    return marker


def child(output):
    output = validate_child_output(output)
    marker = verified_attempt(output)
    source_before = marker['source_sha256_before']
    try:
        from qa.tools.run_retrieval import environment
        environment(output)
        with ExitStack() as stack:
            for owner, method in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                                  (socket, 'create_connection'), (socket, 'getaddrinfo')):
                stack.enter_context(patch.object(owner, method, deny_network))
            from src.config import get_settings
            from src.retrieval.embedding import build_embedding_function, validate_embedding_artifact
            settings = get_settings()
            if settings.llm.embedding_model_name != MODEL:
                raise ValueError('MODEL_ID_MISMATCH')
            embedding = build_embedding_function(settings)
            row_digests = {}

            def retain(case_id, raw):
                row = {'run_id': output.name, 'case_id': case_id, 'catalog_sha256': marker['catalog_sha256'],
                       'raw': raw}
                write_new(output / (case_id + '.json'), row)
                row_digests[case_id] = digest(row)

            result = measure(embedding, source_before[MANIFEST], on_observation=retain)
            validate_embedding_artifact(settings)
            source_after = hashes()
            if source_after != source_before:
                raise ValueError('SOURCES_CHANGED')
            write_new(output / 'child-result.json', {
                'profile': PROFILE, 'run_id': output.name, 'model_id': MODEL,
                'dependency': 'real_local_embedding_cpu', 'manifest_sha256': source_before[MANIFEST],
                'packages': {name: version(name) for name in ('sentence-transformers', 'torch', 'transformers')},
                'source_sha256_after': source_after, 'rows_sha256': row_digests,
                'corpus_sha256': result['corpus_sha256'], 'assessment_sha256': digest(result['assessment'])})
        return 0
    except Exception as exc:
        write_new(output / 'child-error.json', {'status': 'INCOMPLETE', 'error_type': type(exc).__name__})
        return 1


def replay(output):
    output = checked_path(output, ROOT)
    parts = output.relative_to(ROOT).parts
    if len(parts) != 4 or parts[:3] != ('var', 'qa', 'dark-retrieval'):
        raise ValueError('INVALID_MATRIX_PATH')
    marker = verified_attempt(output)
    child_result = read_doc(output / 'child-result.json')
    catalog = cases()
    corpora = sorted({digest(c['records']) for c in catalog})
    if (child_result.get('profile') != PROFILE or child_result.get('run_id') != output.name
            or child_result.get('model_id') != MODEL or child_result.get('dependency') != 'real_local_embedding_cpu'
            or child_result.get('manifest_sha256') != marker['source_sha256_before'][MANIFEST]
            or child_result.get('source_sha256_after') != marker['source_sha256_before']
            or child_result.get('corpus_sha256') != corpora
            or type(child_result.get('rows_sha256')) is not dict
            or set(child_result['rows_sha256']) != {c['case_id'] for c in catalog}):
        raise ValueError('CHILD_IDENTITY_MISMATCH')
    observations = {}
    for case in catalog:
        case_id = case['case_id']
        row = read_doc(output / (case_id + '.json'))
        if (set(row) != {'run_id', 'case_id', 'catalog_sha256', 'raw'} or row['run_id'] != output.name
                or row['case_id'] != case_id or row['catalog_sha256'] != marker['catalog_sha256']
                or digest(row) != child_result['rows_sha256'][case_id]):
            raise ValueError('ROW_MISMATCH')
        observations[case_id] = row['raw']
    result = score(observations)
    if result['measurement_status'] != 'PASS' or digest(result) != child_result.get('assessment_sha256'):
        raise ValueError('ASSESSMENT_MISMATCH')
    return result


def run(value, budget):
    if type(budget) is not int or not 1 <= budget <= 900:
        raise ValueError('INVALID_TIMEOUT')
    output = output_path(value)
    output.mkdir(parents=True, exist_ok=False)
    write_new(output / 'attempt.json', attempt(output, budget))
    summary = {'measurement_status': 'INCOMPLETE', 'profile': PROFILE, 'run_id': output.name,
               'product_acceptance': 'INCOMPLETE', 'hidden_holdout': 'NOT_ESTABLISHED'}
    try:
        process = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                                  '--child', str(output)], cwd=ROOT, timeout=budget,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if process.returncode:
            summary['reason'] = 'CHILD_ERROR'
        else:
            summary.update(assessment=replay(output), measurement_status='PASS')
    except subprocess.TimeoutExpired:
        summary['reason'] = 'CHILD_TIMEOUT'
    except (OSError, ValueError, TypeError, KeyError):
        summary['reason'] = 'EVIDENCE_VALIDATION_FAILED'
    write_new(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output-dir')
    group.add_argument('--replay')
    group.add_argument('--child', help=argparse.SUPPRESS)
    parser.add_argument('--timeout-seconds', type=int, default=300)
    args = parser.parse_args()
    try:
        if args.child:
            return child(Path(args.child))
        if args.replay:
            output = checked_path(ROOT / args.replay, ROOT)
            assessment = replay(output)
            saved = read_doc(output / 'summary.json')
            if (saved.get('measurement_status') != 'PASS' or saved.get('run_id') != output.name
                    or saved.get('profile') != PROFILE or saved.get('product_acceptance') != 'INCOMPLETE'
                    or saved.get('hidden_holdout') != 'NOT_ESTABLISHED'
                    or digest(saved.get('assessment')) != digest(assessment)):
                raise ValueError('SUMMARY_MISMATCH')
            print(json.dumps({'replay': 'PASS', 'cases': assessment['observed_cases']}))
            return 0
        summary = run(args.output_dir, args.timeout_seconds)
        print(json.dumps({k: v for k, v in summary.items() if k != 'assessment'}))
        return 0 if summary['measurement_status'] == 'PASS' else 1
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f'INCOMPLETE: {type(exc).__name__}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
