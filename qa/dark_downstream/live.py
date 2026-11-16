"""Bounded local-embedding downstream measurement; new output directory only."""
from __future__ import annotations

import argparse
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
from qa.dark_downstream.probe import CASES, FILES, evaluate, run as probe
from src.qa_profile import checked_path

SOURCE = 'qa/datasets/aml-v1/fixtures/S03'
MANIFEST = 'var/models/paraphrase-multilingual-MiniLM-L12-v2.manifest.json'
SOURCES = ('qa/dark_downstream/live.py', 'qa/dark_downstream/probe.py',
           'qa/tools/run_retrieval.py', 'src/runtime/service.py', 'src/runtime/context.py',
           'src/runtime/policy.py', 'src/runtime/retrieval.py', 'src/runtime/generation.py',
           'src/runtime/events.py', 'src/retrieval/embedding.py', 'src/contracts/v1.py',
           'src/contracts/semantics.py', 'src/config.py', MANIFEST,
           *(SOURCE + '/' + name for name in FILES))


def source_hashes():
    return {name: sha256(checked_path(ROOT / name, ROOT).read_bytes()).hexdigest() for name in SOURCES}


def write_new(path, value):
    checked_path(path, ROOT)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def output_path(value):
    candidate = Path(value)
    path = checked_path(candidate if candidate.is_absolute() else ROOT / candidate, ROOT)
    parts = path.relative_to(ROOT).parts
    if len(parts) != 4 or parts[:3] != ('var', 'qa', 'dark-downstream') or path.exists():
        raise ValueError('NEW_DOWNSTREAM_DIRECTORY_REQUIRED')
    return path


def deny_network(*_args, **_kwargs):
    raise RuntimeError('NETWORK_DISABLED')


def child(output):
    from qa.tools.run_retrieval import environment
    from qa.scenarios import load_catalog
    from src.config import get_settings
    from src.retrieval.embedding import build_embedding_function, validate_embedding_artifact
    from src.runtime.retrieval import RuleIndex
    try:
        marker = json.loads((output / 'attempt.json').read_text(encoding='utf-8'))
        if marker.get('status') != 'STARTED' or marker.get('source_sha256_before') != source_hashes():
            raise ValueError('SOURCE_OR_ATTEMPT_CHANGED')
        environment(output)
        with patch.object(socket.socket, 'connect', deny_network), \
             patch.object(socket.socket, 'connect_ex', deny_network), \
             patch.object(socket, 'create_connection', deny_network), \
             patch.object(socket, 'getaddrinfo', deny_network):
            load_catalog()
            settings = get_settings()
            validate_embedding_artifact(settings)
            embedding = build_embedding_function(settings)
            factory = lambda path: RuleIndex.from_file(path, settings=settings,
                                                      embedding_factory=lambda _settings: embedding)
            result = probe(ROOT / SOURCE, output, settings, factory)
            validate_embedding_artifact(settings)
            after = source_hashes()
            if after != marker['source_sha256_before']:
                raise ValueError('SOURCE_CHANGED')
            result['embedding_dependency'] = 'real_local_embedding_cpu'
            result['embedding_artifact_validation'] = 'PASS'
            result['model_id'] = settings.llm.embedding_model_name
            result['network'] = 'offline_model_and_python_socket_guards_not_os_firewall'
            result['source_sha256_before'] = marker['source_sha256_before']
            result['source_sha256_after'] = after
            write_new(output / 'child-result.json', result)
        return 0
    except Exception as exc:
        write_new(output / 'child-error.json', {'status': 'INCOMPLETE', 'error_type': type(exc).__name__,
                                                 'diagnostic': 'Child did not complete; check local prerequisites.'})
        return 1


def validate_result(result, before, after):
    if (type(result) is not dict or before != after or result.get('source_sha256_before') != before
            or result.get('source_sha256_after') != after
            or result.get('embedding_dependency') != 'real_local_embedding_cpu'
            or result.get('embedding_artifact_validation') != 'PASS'
            or result.get('semantic_relevance') != 'NOT_ESTABLISHED'
            or result.get('llm') != 'NOT_RUN' or result.get('source_preserved') is not True
            or result.get('aml_expert_acceptance') != 'INCOMPLETE'
            or result.get('hidden_holdout') != 'NOT_ESTABLISHED'):
        raise ValueError('INVALID_CHILD_RESULT')
    rows = result.get('cases')
    if type(rows) is not list or len(rows) != len(CASES):
        raise ValueError('INVALID_CASE_MATRIX')
    statuses = []
    for case, row in zip(CASES, rows):
        if type(row) is not dict or row.get('case_id') != case[0]:
            raise ValueError('INVALID_CASE_IDENTITY')
        if row.get('status') == 'INCOMPLETE':
            statuses.append('INCOMPLETE')
            continue
        calls = [row['search']] if row.get('search_observed') is True else []
        draft = {'status': row['agent_status'], 'reason_code': row['reason_code'],
                 'applicable_rules': [{'citation': c} for c in row['reported_citations']]}
        recomputed = evaluate(case, draft, calls)
        for key, value in recomputed.items():
            if row.get(key) != value:
                raise ValueError('INCONSISTENT_CASE_RESULT')
        statuses.append(recomputed['status'])
    expected = 'FAIL' if 'FAIL' in statuses else 'INCOMPLETE' if 'INCOMPLETE' in statuses else 'PASS'
    if (result.get('status') != expected
            or result.get('policy_safety') != rows[2].get('policy_safety', 'INCOMPLETE')):
        raise ValueError('INCONSISTENT_SUMMARY')
    return expected


def incorporate(summary, result, before, after):
    """Expose the controlled safety failure only after the evidence validates."""
    measurement = validate_result(result, before, after)
    summary['status'] = measurement
    summary['policy_safety'] = result['policy_safety']
    summary['reason'] = 'CONTROL_OBSERVATIONS_MATCH' if measurement == 'PASS' else 'CHECK_CHILD_RESULT'


def run(value, timeout=300):
    if type(timeout) is not int or not 1 <= timeout <= 900:
        raise ValueError('INVALID_TIMEOUT')
    output = output_path(value)
    output.mkdir(parents=True, exist_ok=False)
    before = source_hashes()
    write_new(output / 'attempt.json', {'status': 'STARTED', 'timeout_seconds': timeout,
                                        'source_sha256_before': before})
    summary = {'status': 'INCOMPLETE', 'semantic_relevance': 'NOT_ESTABLISHED',
               'product_quality': 'INCOMPLETE', 'human_acceptance': 'INCOMPLETE',
               'hidden_holdout': 'NOT_ESTABLISHED', 'policy_safety': 'INCOMPLETE',
               'policy_safety_scope': 'Intentionally mutated allowlist only; not the original policy.'}
    try:
        completed = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                                    '--child', str(output)], cwd=ROOT, timeout=timeout,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        child_path = checked_path(output / 'child-result.json', ROOT)
        if completed.returncode != 0 or not child_path.is_file():
            summary['reason'] = 'CHILD_ERROR_OR_MISSING'
        else:
            result = json.loads(child_path.read_text(encoding='utf-8'))
            incorporate(summary, result, before, source_hashes())
    except subprocess.TimeoutExpired:
        summary['reason'] = 'CHILD_TIMEOUT'
    except (OSError, ValueError, TypeError, KeyError):
        summary['reason'] = 'SUPERVISOR_OR_VALIDATION_ERROR'
    write_new(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir')
    parser.add_argument('--timeout-seconds', type=int, default=300)
    parser.add_argument('--child', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        path = checked_path(Path(args.child), ROOT)
        if len(path.relative_to(ROOT).parts) != 4 or path.relative_to(ROOT).parts[:3] != ('var', 'qa', 'dark-downstream'):
            return 2
        return child(path)
    if not args.output_dir:
        parser.error('--output-dir is required')
    result = run(args.output_dir, args.timeout_seconds)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
