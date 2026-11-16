"""Paired template-agent runs with one intentional allowlist defect on synthetic data."""
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

from qa.dark_downstream import live_decision as previous
from qa.dark_downstream.matrix_decision import ALLOW, INCIDENT, SOURCE, assess, encoded, fixture, selected_cases
from qa.dark_downstream.probe import RecordingIndex
from qa.dark_retrieval.live_matrix import MODEL, read_doc
from qa.dark_retrieval.matrix import digest
from qa.dark_retrieval.matrix_cases import citation
from src.qa_profile import checked_path
from src.runtime.context import ContextBuilder
from src.runtime.policy import DemoPolicy
from src.runtime.service import AMLAgent

PROFILE = 'structuring-policy-pair-v1'
EXTRA = 'MATRIX-02'
ARMS = ('baseline', 'candidate')


def inputs(anchor):
    path = previous.existing_output(anchor)
    verified = previous.replay(path)
    if (digest(read_doc(path / 'summary.json')) != digest(verified) or
            verified['measurement_status'] != 'PASS' or verified['decision_status'] != 'PASS'):
        raise ValueError('BASELINE_NOT_QUALIFIED')
    seal = read_doc(path / 'child-result.json')
    docs = {}
    for case in selected_cases():
        key = case['case_id']
        doc = read_doc(path / (key + '.json'))
        if digest(doc) != seal['row_sha256'][key]:
            raise ValueError('BASELINE_ROW_CHANGED')
        docs[key] = doc
    return {'anchor': path.relative_to(ROOT).as_posix(), 'anchor_seal_sha256': digest(seal),
            'evaluator_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
            'extra': EXTRA, 'profile': PROFILE}, docs


def variant_files(source, case, arm):
    files = fixture(source, case)
    policy = json.loads(files['policy.json'])
    if arm not in ARMS or policy['applicability']['structuring'] != ALLOW:
        raise ValueError('UNEXPECTED_CONTROL_POLICY')
    if arm == 'candidate':
        policy['applicability']['structuring'].append(EXTRA)
        files['policy.json'] = encoded(policy)
    return files


def observe(output, case, arm, settings, index_factory):
    key = case['case_id']
    folder = output / arm / key
    folder.mkdir(parents=True)
    for name, raw in variant_files(ROOT / SOURCE, case, arm).items():
        with checked_path(folder / name, ROOT).open('xb') as stream:
            stream.write(raw)
    policy = DemoPolicy.model_validate_json((folder / 'policy.json').read_bytes())
    recorders = []

    def factory(path):
        recorder = RecordingIndex(index_factory(path))
        recorders.append(recorder)
        return recorder

    agent = AMLAgent(mode='demo', engine='template', builder=ContextBuilder(folder, policy),
                     policy=policy, settings=settings, index_factory=factory)
    result = agent.run(INCIDENT)
    return {'run_id': output.name, 'arm': arm, 'case_id': key, 'result': result,
            'calls': [call for recorder in recorders for call in recorder.calls]}


def classify(case, row, anchor):
    reference = anchor['calls'][0]['raw']
    result = assess(case, row, reference[:3])
    reported = result['reported_citations']
    extras = [c for c in reported if c not in case['relevant']]
    missing = [c for c in case['relevant'] if c not in reported]
    texts = {digest(citation(r)): r['text'] for r in case['records']}
    quotes_match = all(texts.get(digest(r['citation'])) == r['rule_text']
                       for r in row['result'].get('applicable_rules', []))
    ranks_agree = result['trace_status'] == 'PASS' and [r['citation'] for r in row['calls'][0]['raw']] == [
        r['citation'] for r in reference]
    return {'agent_status': result['agent_status'], 'reason': result['reason'], 'decision': result['decision_status'],
            'trace': result['trace_status'], 'same_order_as_anchor': ranks_agree, 'reported': reported,
            'reported_ranks': result['reported_ranks'], 'quotes_match_corpus': quotes_match,
            'extra': extras, 'missing': missing}


def compare_pair(case, arms, same_search):
    base, candidate = arms['baseline'], arms['candidate']
    qualified = (same_search and all(r.get('same_order_as_anchor') and r.get('trace') == 'PASS'
                 and r['decision'] != 'INCOMPLETE' for r in (base, candidate)))
    if qualified:
        if case['relevant']:
            actual_extras = [r['rule_id'] for r in candidate['extra']]
            detected = (base['decision'] == 'PASS' and candidate['decision'] == 'FAIL' and
                        actual_extras == [EXTRA] and len(candidate['reported']) == len(case['relevant']) + 1 and
                        not candidate['missing'] and None not in candidate['reported_ranks'] and
                        candidate['quotes_match_corpus'])
            control = 'PASS' if detected else 'FAIL'
        else:
            control = 'PASS' if base['decision'] == candidate['decision'] == 'PASS' else 'FAIL'
    else:
        control = 'INCOMPLETE'
    return {'case_id': case['case_id'], **arms, 'same_full_search': same_search,
            'measurement_status': 'PASS' if qualified else 'INCOMPLETE', 'mutation_check': control}


def rollup(statuses):
    if 'FAIL' in statuses:
        return 'FAIL'
    if 'INCOMPLETE' in statuses:
        return 'INCOMPLETE'
    return 'PASS'


def checked_inputs(output):
    marker = read_doc(output / 'attempt.json')
    binding, docs = inputs(marker['inputs']['anchor'])
    if marker.get('run_id') != output.name or binding != marker['inputs']:
        raise ValueError('ATTEMPT_OR_SOURCE_CHANGED')
    return binding, docs


def worker(output):
    binding, _ = checked_inputs(output)
    if any(p.name != 'attempt.json' for p in output.iterdir()):
        raise ValueError('ATTEMPT_ALREADY_USED')
    from qa.tools.run_retrieval import environment
    environment(output)
    with ExitStack() as stack:
        for owner, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                            (socket, 'create_connection'), (socket, 'getaddrinfo')):
            stack.enter_context(patch.object(owner, name, previous.deny_network))
        from src.config import get_settings
        from src.retrieval.embedding import build_embedding_function, validate_embedding_artifact
        from src.runtime.retrieval import RuleIndex
        settings = get_settings()
        if settings.llm.embedding_model_name != MODEL:
            raise ValueError('MODEL_MISMATCH')
        embedding = build_embedding_function(settings)
        def index_factory(path):
            return RuleIndex.from_file(path, settings=settings, embedding_factory=lambda _: embedding)

        hashes = {}
        for case in selected_cases():
            for arm in ARMS:
                doc = observe(output, case, arm, settings, index_factory)
                key = arm + '/' + case['case_id'] + '.json'
                previous.write_new(output / key, doc)
                hashes[key] = digest(doc)
        validate_embedding_artifact(settings)
        checked_inputs(output)
        previous.write_new(output / 'child-result.json', {'profile': PROFILE, 'run_id': output.name,
                           'inputs_sha256': digest(binding), 'model': MODEL, 'engine': 'template', 'rows': hashes})


def replay(output):
    binding, anchors = checked_inputs(output)
    seal = read_doc(output / 'child-result.json')
    planned_keys = {arm + '/' + c['case_id'] + '.json' for arm in ARMS for c in selected_cases()}
    if (seal.get('profile') != PROFILE or seal.get('run_id') != output.name or seal.get('model') != MODEL
            or seal.get('engine') != 'template' or seal.get('inputs_sha256') != digest(binding)
            or type(seal.get('rows')) is not dict or set(seal['rows']) - planned_keys):
        raise ValueError('INVALID_PAIR_SEAL')
    pairs = []
    for case in selected_cases():
        name = case['case_id']
        arms, searches = {}, {}
        for arm in ARMS:
            key = arm + '/' + name + '.json'
            try:
                doc = read_doc(output / key)
                if (doc.get('run_id') != output.name or doc.get('arm') != arm
                        or digest(doc) != seal['rows'].get(key)):
                    raise ValueError('ROW_MISMATCH')
                for member, expected in variant_files(ROOT / SOURCE, case, arm).items():
                    if checked_path(output / arm / name / member, ROOT).read_bytes() != expected:
                        raise ValueError('FIXTURE_CHANGED')
                arms[arm] = classify(case, doc, anchors[name])
                searches[arm] = digest(doc['calls'])
            except (OSError, KeyError, ValueError, TypeError):
                arms[arm] = {'decision': 'INCOMPLETE', 'trace': 'INCOMPLETE'}
        paired = len(searches) == 2 and searches['baseline'] == searches['candidate']
        pairs.append(compare_pair(case, arms, paired))
    return {'profile': PROFILE, 'run_id': output.name, 'pairs': pairs,
            'baseline_status': rollup([p['baseline']['decision'] for p in pairs]),
            'candidate_status': rollup([p['candidate']['decision'] for p in pairs]),
            'measurement_status': rollup([p['measurement_status'] for p in pairs]),
            'mutation_check': rollup([p['mutation_check'] for p in pairs]), 'product_acceptance': 'INCOMPLETE'}


def run(value, anchor, budget):
    if not 1 <= budget <= 900:
        raise ValueError('INVALID_TIMEOUT')
    output = previous.output_path(value)
    binding, _ = inputs(anchor)
    output.mkdir(parents=True, exist_ok=False)
    previous.write_new(output / 'attempt.json', {'run_id': output.name, 'inputs': binding, 'timeout_seconds': budget})
    summary = {'run_id': output.name, 'profile': PROFILE, 'measurement_status': 'INCOMPLETE',
               'candidate_status': 'INCOMPLETE', 'mutation_check': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE'}
    try:
        proc = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--worker', str(output)],
                              timeout=budget, cwd=ROOT, check=False)
        if proc.returncode:
            summary['reason'] = 'WORKER_ERROR'
        else:
            summary = replay(output)
    except subprocess.TimeoutExpired:
        summary['reason'] = 'WORKER_TIMEOUT'
    except (OSError, KeyError, TypeError, ValueError):
        summary['reason'] = 'UNVERIFIED_EVIDENCE'
    previous.write_new(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output-dir')
    group.add_argument('--replay')
    group.add_argument('--worker', help=argparse.SUPPRESS)
    parser.add_argument('--baseline-run')
    parser.add_argument('--timeout-seconds', type=int, default=300)
    args = parser.parse_args()
    try:
        if args.worker:
            worker(previous.existing_output(args.worker))
            return 0
        if args.replay:
            output = previous.existing_output(args.replay)
            result = replay(output)
            if digest(result) != digest(read_doc(output / 'summary.json')):
                raise ValueError('SUMMARY_CHANGED')
        else:
            if not args.baseline_run:
                parser.error('--baseline-run is required')
            result = run(args.output_dir, args.baseline_run, args.timeout_seconds)
        print(json.dumps({k: v for k, v in result.items() if k != 'pairs'}))
        if result['candidate_status'] == 'FAIL' or result['mutation_check'] == 'FAIL':
            return 1
        return 0 if result['measurement_status'] == result['mutation_check'] == 'PASS' else 2
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f'INCOMPLETE: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
