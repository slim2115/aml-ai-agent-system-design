"""Offline real-embedding ranking and metamorphic QA; no services or LLM calls."""
from contextlib import ExitStack
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import argparse
import json
import math
import os
from pathlib import Path
import platform
import socket
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def write(folder, name, value):
    with (folder / name).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def output_path(value):
    from src.qa_profile import checked_path
    path = Path(value)
    path = checked_path(path if path.is_absolute() else ROOT / path, ROOT)
    parts = path.relative_to(ROOT).parts
    allowed = (len(parts) >= 3 and parts[0] == 'verification' and parts[1].startswith('qa-step-06-')) \
        or (len(parts) >= 4 and parts[:3] == ('var', 'qa', 'retrieval'))
    if not allowed:
        raise ValueError('QA_RETRIEVAL_NEW_OUTPUT_REQUIRED')
    if path.exists():
        raise FileExistsError('QA_EVIDENCE_ALREADY_EXISTS')
    return path


def sources():
    paths = {path for base in ('src', 'qa') for path in (ROOT / base).rglob('*.py')}
    paths.update((ROOT / 'tests/evaluator').glob('test_qa_*.py'))
    for name in ('pyproject.toml', 'qa/datasets/aml-v1/manifest.json',
                 'qa/datasets/retrieval-v1/manifest.json',
                 'var/models/paraphrase-multilingual-MiniLM-L12-v2.manifest.json'):
        paths.add(ROOT / name)
    paths.update(ROOT.glob('*requirements*.txt'))
    paths.update(ROOT.glob('*requirements*.lock'))
    return {p.relative_to(ROOT).as_posix(): sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def environment(output):
    # Only this evaluation process changes; the operator's terminal is untouched.
    for key in list(os.environ):
        if key.startswith('AML_') or key in {'PYTHONPATH', 'PYTHONHOME', 'TRANSFORMERS_CACHE',
                'PYTORCH_TRANSFORMERS_CACHE', 'PYTORCH_PRETRAINED_BERT_CACHE'}:
            os.environ.pop(key, None)
    values = {
        'PYTHON_DOTENV_DISABLED': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
        'ANONYMIZED_TELEMETRY': 'FALSE', 'OTEL_SDK_DISABLED': 'true', 'DO_NOT_TRACK': '1',
        'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false',
        'TOKENIZERS_PARALLELISM': 'false',
        'OLLAMA_BASE_URL': 'http://127.0.0.1:11434', 'LLM_MODEL_NAME': 'qwen2.5:14b',
        'EMBEDDING_MODEL_NAME': 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
        'EMBEDDING_MODEL_PATH': str(ROOT / 'var/models/paraphrase-multilingual-MiniLM-L12-v2'),
        'EMBEDDING_MANIFEST_PATH': str(ROOT / 'var/models/paraphrase-multilingual-MiniLM-L12-v2.manifest.json'),
        'KNOWLEDGE_BASE_COLLECTION': 'qa_retrieval_unused', 'GENERATION_TIMEOUT_SEC': '180',
        'FAITHFULNESS_THRESHOLD': '.90', 'SCHEMA_MATCH_THRESHOLD': '1', 'TRACEABILITY_THRESHOLD': '1',
        'REFUSAL_CORRECTNESS_THRESHOLD': '.95', 'RAG_RECALL_THRESHOLD': '1', 'RAG_PRECISION_THRESHOLD': '1',
    }
    for key, subfolder in {
        'TEMP': 'tmp', 'TMP': 'tmp', 'HF_HOME': 'cache/hf', 'HF_HUB_CACHE': 'cache/hf/hub',
        'HF_ASSETS_CACHE': 'cache/hf/assets', 'HF_MODULES_CACHE': 'cache/hf/modules',
        'SENTENCE_TRANSFORMERS_HOME': 'cache/hf/hub', 'TORCH_HOME': 'cache/torch',
        'XDG_CACHE_HOME': 'cache/xdg', 'MPLCONFIGDIR': 'cache/matplotlib',
        'CHROMA_PERSIST_DIR': 'unused-chroma', 'SYNTHETIC_DATA_DIR': 'unused-sources',
    }.items():
        destination = output / subfolder
        destination.mkdir(parents=True, exist_ok=True)
        values[key] = str(destination)
    os.environ.update(values)


def deny_network(*_args, **_kwargs):
    raise RuntimeError('QA_RETRIEVAL_NETWORK_DISABLED')


class CountingEmbedding:
    def __init__(self, embedding):
        self.embedding = embedding
        self._model = embedding._model
        self.calls = []

    def __call__(self, texts):
        started = time.perf_counter()
        result = self.embedding(texts)
        self.calls.append(dict(inputs=len(texts), input_sha256=[sha256(s.encode()).hexdigest() for s in texts],
                               elapsed_seconds=time.perf_counter() - started))
        return result


def observed(chunks):
    return [dict(citation=c.citation.model_dump(mode='json'), distance=c.distance,
                 text=c.text, char_start=c.char_start, char_end=c.char_end,
                 token_count=c.token_count) for c in chunks]


def scores(rows):
    return [dict(citation=row['citation'], distance=row['distance']) for row in rows]


def assert_inventory(index, corpus):
    from qa.retrieval_checks import citation_identity
    actual = {citation_identity(c.citation.model_dump(mode='json')): c for c in index.chunks}
    expected = {citation_identity(c['citation']): c for c in corpus['inventory']}
    if len(actual) != len(index.chunks) or actual.keys() != expected.keys():
        raise ValueError('QA_ACTUAL_CHUNKS_DISAGREE_WITH_FROZEN_SPANS')
    by_edition = {(r['rule_id'], r['version']): r for r in corpus['records']}
    for identity, chunk in actual.items():
        target = expected[identity]
        edition = by_edition[(chunk.citation.rule_id, chunk.citation.rule_version)]
        if (chunk.char_start != target['char_start'] or chunk.char_end != target['char_end']
                or chunk.source_text_sha256 != target['source_text_sha256']
                or chunk.text != edition['text'][chunk.char_start:chunk.char_end]
                or chunk.effective_from != target['effective_from']
                or chunk.effective_until != target['effective_until']):
            raise ValueError('QA_CHUNK_INVENTORY_MISMATCH')


def run_ranking(output, dataset, embedding, manifest_digest):
    from qa.retrieval_checks import citation_identity, evaluate_ranking
    from src.runtime.retrieval import RuleIndex, _QUERY_TEXT
    if _QUERY_TEXT != dataset['query_text']:
        raise ValueError('QA_FROZEN_QUERY_MAP_CHANGED')
    directory = output / 'ranking'
    directory.mkdir()
    indexes = {}
    for name, corpus in dataset['corpora'].items():
        raw = (ROOT / 'qa/datasets/retrieval-v1' / corpus['path']).read_bytes()
        index = RuleIndex.from_records(corpus['records'], embedding=embedding,
            embedding_manifest_digest=manifest_digest, source_sha256=sha256(raw).hexdigest())
        assert_inventory(index, corpus)
        indexes[name] = index
        write(directory, 'index-' + name + '.json', dict(provenance=index.provenance,
            fingerprint=index.fingerprint, chunks=[dict(citation=c.citation.model_dump(mode='json'),
                text=c.text, char_start=c.char_start, char_end=c.char_end, token_count=c.token_count,
                effective_from=c.effective_from, effective_until=c.effective_until) for c in index.chunks]))
    records = []
    for case in dataset['cases']:
        started = time.perf_counter()
        row = dict(case_id=case['id'], group=case['group'], primary=case['primary'], kind=case['kind'],
                   query=case['query'], as_of=case['as_of'], corpus=case['corpus'], status='ERROR')
        try:
            index = indexes[case['corpus']]
            count = index.active_chunk_count(case['as_of'])
            full = observed(index.search(case['query'], as_of=case['as_of'], k=max(1, count)))
            actual_ids = [citation_identity(c['citation']) for c in full]
            expected_ids = {citation_identity(c) for c in case['expected_active']}
            if count != len(expected_ids) or len(actual_ids) != count or set(actual_ids) != expected_ids:
                raise ValueError('QA_ACTIVE_INVENTORY_MISMATCH')
            measured = {}
            for k in dataset['k_values']:
                top = observed(index.search(case['query'], as_of=case['as_of'], k=k))
                if top != full[:k]:
                    raise ValueError('QA_TOP_K_PREFIX_MISMATCH')
                measured[str(k)] = evaluate_ranking(scores(top), case['relevant'], k=k)
            mrr = evaluate_ranking(scores(full), case['relevant'], k=max(1, count))['reciprocal_rank_at_k']
            row.update(status='PASS', ranked=full, relevant=case['relevant'], metrics=measured,
                       reciprocal_rank_full=mrr, active_chunks=count, ranking_scope='direct_RuleIndex_search')
        except Exception as exc:
            row.update(error_type=type(exc).__name__, error_code=str(exc) if str(exc).startswith('QA_') else 'QA_RANKING_CASE_ERROR')
        row['elapsed_seconds'] = time.perf_counter() - started
        records.append(row)
        write(directory, case['id'] + '.json', row)
    by_id = {row['case_id']: row for row in records}
    relations = []
    for left, right in [('R02', 'R06'), ('R03', 'R07'), ('R04', 'R08'), ('R10', 'R11')]:
        a, b = by_id[left], by_id[right]
        relations.append(dict(left=left, right=right, relation='same_direct_ranking',
            passed=a['status'] == b['status'] == 'PASS' and a['ranked'] == b['ranked']))
    primary = [row for row in records if row['primary']]
    complete = len(primary) == 5 and all(row['status'] == 'PASS' for row in records)
    aggregate = {}
    if complete:
        for k in dataset['k_values']:
            aggregate[str(k)] = {name: math.fsum(row['metrics'][str(k)][name] for row in primary) / len(primary)
                for name in ('precision_at_k', 'recall_at_k', 'reciprocal_rank_at_k')}
        aggregate['mrr_full'] = math.fsum(row['reciprocal_rank_full'] for row in primary) / len(primary)
    result = dict(status='PASS' if complete and all(r['passed'] for r in relations) else 'ERROR',
        metric_mode=dataset['metric_mode'], quality_acceptance='NOT_ESTABLISHED',
        dataset_manifest_sha256=dataset['manifest_sha256'], k_values=dataset['k_values'],
        primary_query_count=len(primary), primary_case_ids=[row['case_id'] for row in primary],
        diagnostic_query_count=len(records) - len(primary), cases=records, relations=relations,
        primary_macro=aggregate,
        interpretation='PASS means complete valid measurement, not a quality threshold. '
            'One tiny synthetic corpus, five positive query families. Aliases, edition variants and negative controls '
            'do not increase the primary denominator. Not M-07, SME approval or legal applicability.')
    write(directory, 'result.json', result)
    return result


def run(output):
    from qa.retrieval_dataset import load_dataset
    from qa.scenarios import load_catalog
    dataset = load_dataset()
    load_catalog()  # Validate frozen goldens before any model load or mutation.
    before = sources()
    output.mkdir(parents=True, exist_ok=False)
    environment(output)
    write(output, 'source-hashes.json', before)
    write(output, 'run.json', dict(status='INCOMPLETE', started_at_utc=datetime.now(timezone.utc).isoformat(),
        dataset_manifest_sha256=dataset['manifest_sha256'], k_values=dataset['k_values'],
        mode='real_embedding_cpu_template', network='disabled_python_sockets_plus_offline_local_model',
        ordinary_services='not_started_or_contacted', seed='no_generated_or_adjusted_goldens'))
    started = time.perf_counter()
    result = dict(status='ERROR', product_acceptance='INCOMPLETE', ranking_quality_acceptance='NOT_ESTABLISHED')
    try:
        with ExitStack() as guards:
            for name in ('connect', 'connect_ex'):
                guards.enter_context(patch.object(socket.socket, name, deny_network))
            guards.enter_context(patch.object(socket, 'create_connection', deny_network))
            from src.config import get_settings
            from src.retrieval.embedding import build_embedding_function, validate_embedding_artifact
            get_settings.cache_clear()
            settings = get_settings()
            manifest_digest = sha256(Path(settings.llm.embedding_manifest_path).read_bytes()).hexdigest()
            loaded = time.perf_counter()
            embedding = CountingEmbedding(build_embedding_function(settings))
            model_load_seconds = time.perf_counter() - loaded
            import torch
            write(output, 'runtime.json', dict(python=sys.version, platform=platform.platform(),
                cpu=platform.processor(), logical_cpus=os.cpu_count(), device='cpu',
                torch_threads=torch.get_num_threads(), torch_interop_threads=torch.get_num_interop_threads(),
                packages={name: version(name) for name in ('torch', 'transformers', 'sentence-transformers', 'tokenizers', 'numpy')},
                model_id=settings.llm.embedding_model_name, embedding_manifest_sha256=manifest_digest,
                model_load_seconds=model_load_seconds, max_seq_length=embedding._model.max_seq_length,
                tokenizer=type(embedding._model.tokenizer).__name__))
            ranking = run_ranking(output, dataset, embedding, manifest_digest)
            from qa.metamorphic_checks import run_metamorphic
            metamorphic = run_metamorphic(output, embedding, manifest_digest, settings)
            if (metamorphic['family_count'] != 5 or len(metamorphic['cases']) != 5
                    or {row['id'] for row in metamorphic['cases']} != {f'MT-{number:02d}' for number in range(1, 6)}
                    or metamorphic['observation_count'] != 10):
                raise ValueError('QA_METAMORPHIC_COLLECTION_INCOMPLETE')
            validate_embedding_artifact(settings)
            if sha256(Path(settings.llm.embedding_manifest_path).read_bytes()).hexdigest() != manifest_digest:
                raise ValueError('QA_MODEL_MANIFEST_CHANGED')
            load_dataset()
            load_catalog()
            unchanged = sources() == before
            write(output, 'embedding-calls.json', dict(calls=len(embedding.calls),
                inputs=sum(call['inputs'] for call in embedding.calls), observations=embedding.calls))
            statuses = {ranking['status'], metamorphic['status']}
            status = 'ERROR' if 'ERROR' in statuses or not unchanged else 'FAIL' if 'FAIL' in statuses else 'PASS'
            result.update(status=status, source_files_unchanged_during_run=unchanged,
                ranking_status=ranking['status'], ranking_cases=len(ranking['cases']),
                primary_query_count=ranking['primary_query_count'], primary_macro=ranking['primary_macro'],
                metamorphic_status=metamorphic['status'], metamorphic_cases=len(metamorphic['cases']),
                metamorphic_family_count=metamorphic['family_count'], embedding_calls=len(embedding.calls),
                model_manifest_sha256=manifest_digest, model_load_seconds=model_load_seconds)
    except Exception as exc:
        result.update(status='ERROR', error_type=type(exc).__name__,
            error_code=str(exc) if str(exc).startswith('QA_') else 'QA_RETRIEVAL_EXECUTION_ERROR')
    result.update(finished_at_utc=datetime.now(timezone.utc).isoformat(), elapsed_seconds=time.perf_counter() - started,
        scope='Real verified offline CPU embedding, direct ranking, frozen synthetic labels, '
              'copied-source metamorphic checks using real context/retrieval/template. No API, Kafka, '
              'PostgreSQL, browser, Ollama generation, Judge, SME approval or M-07 threshold change.')
    write(output, 'result.json', result)
    return result


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    try:
        result = run(output_path(args.output_dir))
    except Exception as exc:
        result = dict(status='ERROR', error_type=type(exc).__name__,
            error_code=str(exc) if str(exc).startswith('QA_') else 'QA_RETRIEVAL_SETUP_ERROR')
    print(json.dumps(result, ensure_ascii=False))
    return {'PASS': 0, 'FAIL': 1, 'ERROR': 2}[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())
