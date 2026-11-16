"""Second open development corpus for the three pre-existing distance cutoffs."""
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
sys.path.insert(0, str(ROOT))
from qa.dark_retrieval import live, live_matrix
from qa.dark_retrieval.assessment import assess_run
from qa.dark_retrieval.matrix import SELECTORS, digest, validate_raw
from qa.dark_retrieval.matrix_cases import cases, citation
from src.qa_profile import checked_path

TEXTS = {
    'structuring': 'В течение дня клиент разделил один платёж на серию переводов одинакового назначения.',
    'threshold': 'Сравните рублёвую сумму отдельного перевода с контрольной границей размера платежа.',
    'kyc': 'Сверьте удостоверение личности и дату актуализации клиентской анкеты до оценки операции.',
    'high_risk': 'Проверьте географический риск страны назначения независимо от суммы платежа.',
    'reporting': 'Составьте сообщение о проверенной операции и зарегистрируйте дату его передачи адресату.',
}


def catalog():
    selected = [c for c in cases() if c['slice'] in ('new-active', 'topic-absent')]
    for case in selected:
        case['case_id'] = case['case_id'].replace('MX-', 'ALT-')
        for r in case['records']:
            r['text'] = TEXTS[r['category']] + (' Редакция A.' if r['version'] == 'v1' else ' Редакция B.')
        case['relevant'] = [citation(r) for r in case['records'] if
                            r['category'] == case['query'] and r['version'] == 'v2']
    return selected


def metrics(rows):
    cases_ = catalog()
    if set(rows) != {c['case_id'] for c in cases_}:
        raise ValueError('INCOMPLETE_CASE_MATRIX')
    for c in cases_:
        validate_raw(c, rows[c['case_id']])
    table = {}
    for name, cutoff in SELECTORS.items():
        sim = {}
        for c in cases_:
            raw = rows[c['case_id']]
            selected = [raw[0]['citation']] if raw and (cutoff is None or raw[0]['distance'] <= cutoff) else []
            sim[c['case_id']] = {'raw': raw, 'selected': selected}
        evaluated = assess_run(cases_, sim)
        table[name] = {k: evaluated[k] for k in ('status', 'false_accept', 'false_reject', 'failures')}
        table[name]['wrong_on_positive'] = sum(bool(r['decision']['unsupported_selected_count'])
                for r in evaluated['rows'] if r['decision']['relevant_count'])
    return table


def binding():
    sources = live_matrix.hashes()
    sources['qa/dark_retrieval/threshold_challenge.py'] = sha256(Path(__file__).read_bytes()).hexdigest()
    if None in sources.values():
        raise ValueError('INCOMPLETE_INPUTS')
    return {'catalog': digest(catalog()), 'selectors': digest(SELECTORS), 'sources': sources}


def child(folder):
    from qa.tools.run_retrieval import environment
    marker = live_matrix.read_doc(folder / 'attempt.json')
    if (marker != binding() or any(p.name != 'attempt.json' for p in folder.iterdir())):
        raise ValueError('USED_OR_CHANGED_ATTEMPT')
    environment(folder)
    with ExitStack() as guards:
        for owner, method in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                              (socket, 'create_connection'), (socket, 'getaddrinfo')):
            guards.enter_context(patch.object(owner, method, live.deny_network))
        from src.config import get_settings
        from src.retrieval.embedding import build_embedding_function, validate_embedding_artifact
        settings = get_settings()
        if settings.llm.embedding_model_name != live_matrix.MODEL:
            raise ValueError('MODEL_MISMATCH')
        embedding = build_embedding_function(settings)
        measured = live.measure(catalog(), embedding, marker['sources'][live.MANIFEST], dependency='real_local_embedding_cpu')
        rows = {r['case_id']: r['raw'] for r in measured['cases']}
        live.write_new(folder / 'observations.json', rows)
        validate_embedding_artifact(settings)
        if marker != binding():
            raise ValueError('SOURCE_CHANGED')
        live.write_new(folder / 'verified.json', {'observations_sha256': digest(rows), 'run_id': folder.name})


def replay(folder):
    folder = checked_path(folder, ROOT)
    parts = folder.relative_to(ROOT).parts
    if len(parts) != 4 or parts[:3] != ('var', 'qa', 'dark-retrieval'):
        raise ValueError('INVALID_OUTPUT')
    marker = live_matrix.read_doc(folder / 'attempt.json')
    rows = live_matrix.read_doc(folder / 'observations.json')
    if (marker != binding() or live_matrix.read_doc(folder / 'verified.json') !=
            {'observations_sha256': digest(rows), 'run_id': folder.name}):
        raise ValueError('UNVERIFIED_ROWS')
    return {'measurement_status': 'PASS', 'cases': len(rows), 'model': live_matrix.MODEL,
            'selectors': metrics(rows), 'production_threshold': None, 'expert_acceptance': 'INCOMPLETE'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output-dir')
    group.add_argument('--replay')
    group.add_argument('--child', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        folder = checked_path(ROOT / args.child, ROOT)
        if folder.relative_to(ROOT).parts[:-1] != ('var', 'qa', 'dark-retrieval'):
            raise ValueError('INVALID_OUTPUT')
        child(folder)
        return 0
    if args.replay:
        folder = ROOT / args.replay
        return 0 if replay(folder) == live_matrix.read_doc(folder / 'summary.json') else 2
    folder = live.output_path(args.output_dir)
    folder.mkdir(parents=True, exist_ok=False)
    result = {'measurement_status': 'INCOMPLETE'}
    try:
        live.write_new(folder / 'attempt.json', binding())
        subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--child', str(folder)],
                       cwd=ROOT, check=True, timeout=300)
        result = replay(folder)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        result['reason'] = type(exc).__name__
    live.write_new(folder / 'summary.json', result)
    print(json.dumps(result))
    return 0 if result['measurement_status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
