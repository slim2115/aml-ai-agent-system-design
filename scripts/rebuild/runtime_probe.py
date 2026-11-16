"""Bounded offline constructor/embedding smoke; never calls agent.run or Ollama."""
import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import importlib.metadata
import json
import math
from pathlib import Path
import socket
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='verification/block-03/runtime-probe.json')
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    output.relative_to(ROOT)
    output.parent.mkdir(parents=True, exist_ok=True)
    checks = []
    network_attempts = []

    def deny_network(*args, **kwargs):
        network_attempts.append('PYTHON_NETWORK_ATTEMPT_BLOCKED')
        raise RuntimeError('NETWORK_DISABLED_IN_RUNTIME_PROBE')

    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.create_connection = deny_network
    started = time.perf_counter()
    import os
    os.environ['CHROMA_PERSIST_DIR'] = str(ROOT / 'var/tmp' / ('probe-chroma-' + uuid.uuid4().hex))
    os.environ['KNOWLEDGE_BASE_COLLECTION'] = 'rebuild_probe'
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    from src.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    from src.retrieval.embedding import EmbeddingNotReadyError, build_embedding_function
    missing_settings = replace(settings, llm=replace(settings.llm, embedding_model_path=str(ROOT / 'var/models/not-staged')))
    try:
        build_embedding_function(missing_settings)
        checks.append({'name': 'cold_missing_artifact', 'passed': False})
    except EmbeddingNotReadyError as error:
        checks.append({'name': 'cold_missing_artifact', 'passed': error.code == 'EMBEDDING_NOT_READY'})
    try:
        embedding = build_embedding_function(settings)
        values = embedding(['Синтетическая операция для проверки.', 'Synthetic offline embedding check.'])
        valid = len(values) == 2 and all(len(vector) == 384 and all(math.isfinite(value) for value in vector) for vector in values)
        checks.append({'name': 'real_offline_embedding', 'passed': valid, 'vectors': len(values), 'dimensions': len(values[0])})
        del embedding
        from src.agent import AMLAgent
        agent = AMLAgent()
        checks.append({'name': 'legacy_agent_constructor_with_rebuilt_dependencies', 'passed': agent._graph is not None})
        hits = agent._kb.search('Синтетическая проверка правил мониторинга операций.', k=1)
        checks.append({'name': 'local_chroma_query_adapter', 'passed': len(hits) == 1,
                       'scope': 'API/runtime compatibility only, not retrieval relevance'})
    except Exception as error:
        checks.append({'name': 'runtime_initialization', 'passed': False, 'error_type': type(error).__name__, 'diagnostic': str(error)[:500]})
    checks.append({'name': 'no_python_network_attempts', 'passed': not network_attempts, 'attempt_count': len(network_attempts)})
    passed = all(check['passed'] for check in checks)
    packages = {distribution.metadata['Name'].lower().replace('_', '-'): distribution.version for distribution in importlib.metadata.distributions()}
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'PASS' if passed else 'FAIL',
              'python': sys.version.split()[0], 'executable': sys.executable, 'checks': checks,
              'elapsed_seconds': time.perf_counter() - started, 'settings': asdict(settings), 'packages': packages,
              'scope': 'Synthetic embedding and graph constructor only. No report generation, Ollama, HTTP API or PostgreSQL. Python sockets blocked; not an OS firewall test.'}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key not in ('packages', 'settings')}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
