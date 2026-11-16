"""Bounded local-embedding measurement; no downstream decision is inferred.

Output is append-only to a new owned path. Python socket guards and offline
model mode are defense in depth, not a process/OS network sandbox.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qa.dark_retrieval.assessment import assess_run
from qa.dark_retrieval.scenarios import development_cases
from src.qa_profile import checked_path

MANIFEST = 'var/models/paraphrase-multilingual-MiniLM-L12-v2.manifest.json'
SOURCES = ('qa/dark_retrieval/live.py', 'qa/dark_retrieval/scenarios.py',
           'qa/dark_retrieval/assessment.py', 'qa/retrieval_checks.py',
           'qa/tools/run_retrieval.py', 'src/runtime/retrieval.py',
           'src/retrieval/embedding.py', 'src/config.py', MANIFEST)


def output_path(value: str) -> Path:
    candidate = Path(value)
    path = checked_path(candidate if candidate.is_absolute() else ROOT / candidate, ROOT)
    parts = path.relative_to(ROOT).parts
    if len(parts) != 4 or parts[:3] != ('var', 'qa', 'dark-retrieval'):
        raise ValueError('NEW_DARK_RETRIEVAL_OUTPUT_REQUIRED')
    if path.exists():
        raise FileExistsError('OUTPUT_ALREADY_EXISTS')
    return path


def source_hashes() -> dict:
    result = {}
    for name in SOURCES:
        path = checked_path(ROOT / name, ROOT)
        result[name] = sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return result


def write_new(path: Path, data: dict) -> None:
    checked_path(path, ROOT)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def deny_network(*_args, **_kwargs):
    raise RuntimeError('NETWORK_DISABLED')


def measure(cases: list[dict], embedding, manifest_digest: str, *, index_factory=None,
            dependency: str = 'unknown') -> dict:
    """Observe raw retrieval only; selection is explicitly never supplied."""
    if index_factory is None:
        from src.runtime.retrieval import RuleIndex
        index_factory = RuleIndex.from_records
    observations, evidence = {}, []
    for case in cases:
        index = index_factory(case['records'], embedding=embedding,
                              embedding_manifest_digest=manifest_digest)
        chunks = index.search(case['query'], as_of=case['as_of'], k=3)
        raw = [{'citation': c.citation.model_dump(mode='json'), 'distance': c.distance} for c in chunks]
        observations[case['case_id']] = {'raw': raw, 'selected': None}
        evidence.append({'case_id': case['case_id'], 'family': case['family'],
                         'slice': case['slice'], 'query': case['query'], 'as_of': case['as_of'],
                         'raw': raw})
    assessment = assess_run(cases, observations)
    return {'measurement_status': 'PASS', 'product_quality': 'INCOMPLETE',
            'expert_acceptance': 'INCOMPLETE', 'secret_holdout': False,
            'dependency': dependency, 'dependency_validation': 'INCOMPLETE',
            'network': 'not_established_by_measure',
            'network_guard_limit': 'not_an_os_firewall', 'independent_families_claimed': False,
            'selection_observed': False, 'cases': evidence, 'assessment': assessment}


def validate_child_output(output: Path) -> Path:
    output = checked_path(output, ROOT)
    parts = output.relative_to(ROOT).parts
    if len(parts) != 4 or parts[:3] != ('var', 'qa', 'dark-retrieval') or not output.is_dir():
        raise ValueError('INVALID_CHILD_OUTPUT')
    marker = checked_path(output / 'attempt.json', ROOT)
    if not marker.is_file():
        raise ValueError('MISSING_ATTEMPT_MARKER')
    if marker.stat().st_size > 65536:
        raise ValueError('INVALID_ATTEMPT_MARKER')
    value = json.loads(marker.read_text(encoding='utf-8'))
    timeout = value.get('timeout_seconds') if type(value) is dict else None
    if (type(value) is not dict or value.get('status') != 'STARTED' or type(timeout) is not int
            or not 1 <= timeout <= 900 or type(value.get('source_sha256_before')) is not dict):
        raise ValueError('INVALID_ATTEMPT_MARKER')
    for name in ('child-result.json', 'child-error.json', 'summary.json'):
        path = checked_path(output / name, ROOT)
        if path.exists():
            raise FileExistsError('CHILD_OUTPUT_EXISTS')
    return output


def child(output: Path) -> int:
    """The parent creates the directory and attempt marker first."""
    output = validate_child_output(output)
    try:
        from qa.tools.run_retrieval import environment
        environment(output)
        with patch.object(socket.socket, 'connect', deny_network), \
             patch.object(socket.socket, 'connect_ex', deny_network), \
             patch.object(socket, 'create_connection', deny_network), \
             patch.object(socket, 'getaddrinfo', deny_network):
            from src.config import get_settings
            from src.retrieval.embedding import build_embedding_function, validate_embedding_artifact
            settings = get_settings()
            validate_embedding_artifact(settings)
            before = source_hashes()
            if any(value is None for value in before.values()):
                raise RuntimeError('MISSING_REQUIRED_SOURCE')
            embedding = build_embedding_function(settings)
            result = measure(development_cases(), embedding, before[MANIFEST], dependency='real_local_embedding_cpu')
            validate_embedding_artifact(settings)
            after = source_hashes()
            if before != after:
                raise RuntimeError('SOURCE_CHANGED')
            result['source_sha256_before'] = before
            result['source_sha256_after'] = after
            result['model_id'] = settings.llm.embedding_model_name
            result['dependency_validation'] = 'PASS'
            result['network'] = 'offline_model_and_python_socket_guards'
            write_new(output / 'child-result.json', result)
        return 0
    except Exception as exc:
        write_new(output / 'child-error.json', {'status': 'INCOMPLETE', 'error_type': type(exc).__name__,
                                                'message': 'Measurement did not complete; inspect local prerequisites.'})
        return 1


def validate_result(result: dict, before: dict, after: dict) -> None:
    """Recompute the published assessment from raw rows, never trust a child verdict."""
    if (type(result) is not dict or result.get('measurement_status') != 'PASS' or before != after
            or result.get('source_sha256_before') != before or result.get('source_sha256_after') != after
            or result.get('dependency') != 'real_local_embedding_cpu'
            or result.get('dependency_validation') != 'PASS' or result.get('selection_observed') is not False
            or result.get('product_quality') != 'INCOMPLETE' or result.get('expert_acceptance') != 'INCOMPLETE'
            or result.get('secret_holdout') is not False):
        raise ValueError('RESULT_OR_SOURCE_MISMATCH')
    expected = development_cases()
    rows = result.get('cases')
    if type(rows) is not list or len(rows) != len(expected):
        raise ValueError('RESULT_OR_SOURCE_MISMATCH')
    observations = {}
    for case, row in zip(expected, rows):
        if (type(row) is not dict or set(row) != {'case_id', 'family', 'slice', 'query', 'as_of', 'raw'}
                or any(row[key] != case[key] for key in ('case_id', 'family', 'slice', 'query', 'as_of'))):
            raise ValueError('RESULT_OR_SOURCE_MISMATCH')
        observations[case['case_id']] = {'raw': row['raw'], 'selected': None}
    recomputed = assess_run(expected, observations)
    if result.get('assessment') != recomputed or recomputed['status'] != 'INCOMPLETE':
        raise ValueError('RESULT_OR_SOURCE_MISMATCH')


def run(value: str, timeout: int = 300, *, process_run=subprocess.run) -> dict:
    if type(timeout) is not int or not 1 <= timeout <= 900:
        raise ValueError('INVALID_TIMEOUT')
    output = output_path(value)
    output.mkdir(parents=True, exist_ok=False)
    before = source_hashes()
    write_new(output / 'attempt.json', {'status': 'STARTED', 'timeout_seconds': timeout,
                                       'source_sha256_before': before})
    summary = {'status': 'INCOMPLETE', 'measurement_status': 'INCOMPLETE',
               'product_quality': 'INCOMPLETE', 'expert_acceptance': 'INCOMPLETE',
               'secret_holdout': False, 'reason': None, 'timeout_seconds': timeout}
    try:
        process = process_run([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                               '--child', str(output)], cwd=ROOT, timeout=timeout,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        result_path = checked_path(output / 'child-result.json', ROOT)
        if process.returncode != 0:
            summary['reason'] = 'CHILD_ERROR'
        elif not result_path.is_file():
            summary['reason'] = 'MISSING_CHILD_RESULT'
        else:
            if result_path.stat().st_size > 1024 * 1024:
                raise ValueError('RESULT_TOO_LARGE')
            from src.runtime.transport import strict_json
            result = strict_json(result_path.read_bytes())
            after = source_hashes()
            try:
                validate_result(result, before, after)
            except (ValueError, TypeError, KeyError):
                summary['reason'] = 'RESULT_OR_SOURCE_MISMATCH'
            else:
                summary['measurement_status'] = 'PASS'
                summary['reason'] = 'DOWNSTREAM_SELECTION_NOT_MEASURED'
            summary['source_sha256_after'] = after
    except subprocess.TimeoutExpired:
        summary['reason'] = 'CHILD_TIMEOUT'
    except (OSError, ValueError, TypeError, AttributeError):
        summary['reason'] = 'SUPERVISOR_ERROR'
    write_new(output / 'summary.json', summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir')
    parser.add_argument('--timeout-seconds', type=int, default=300)
    parser.add_argument('--child', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child(Path(args.child))
    if not args.output_dir:
        parser.error('--output-dir is required')
    result = run(args.output_dir, args.timeout_seconds)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['measurement_status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
