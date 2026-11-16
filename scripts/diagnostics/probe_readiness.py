"""Finite isolated readiness probe; no workflow DB, jobs, Ollama or downloads."""
from datetime import datetime, timezone
import faulthandler
from functools import partial
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def traced_bootstrap(directory):
    directory = Path(directory)
    began = time.monotonic()
    def event(stage, **extra):
        row = {'stage': stage, 'elapsed_seconds': round(time.monotonic() - began, 3), **extra}
        with (directory / 'stages.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row) + '\n')
    with (directory / 'startup-stacks.log').open('w', encoding='utf-8') as stack:
        faulthandler.dump_traceback_later(45, repeat=True, file=stack)
        try:
            event('import_runtime_begin')
            from src.workflow import worker
            from src.runtime import retrieval
            from src.retrieval import embedding
            event('import_runtime_done')
            original_provenance = worker._source_provenance
            def provenance(settings):
                event('verify_artifacts_begin')
                result = original_provenance(settings)
                event('verify_artifacts_done')
                return result
            worker._source_provenance = provenance
            def factory(*args, **kwargs):
                event('import_sentence_transformers_begin')
                from sentence_transformers import SentenceTransformer
                event('import_sentence_transformers_done')
                event('load_model_begin')
                model = SentenceTransformer(*args, **kwargs)
                event('load_model_done')
                original_encode = model.encode
                def encode(*args, **kwargs):
                    event('encode_rules_begin')
                    result = original_encode(*args, **kwargs)
                    event('encode_rules_done')
                    return result
                model.encode = encode
                return model
            retrieval.build_embedding_function = lambda settings: embedding.build_embedding_function(settings, model_factory=factory)
            result = worker._bootstrap()
            event('bootstrap_done')
            return result
        except Exception as exc:
            event('bootstrap_failed', exception_type=type(exc).__name__)
            raise
        finally:
            faulthandler.cancel_dump_traceback_later()


def main():
    from src.config import get_settings
    from src.workflow.worker import ManagedWorker
    now = datetime.now(timezone.utc)
    directory = ROOT / 'verification/readiness-2026-09-30' / now.strftime('%Y%m%dT%H%M%SZ')
    directory.mkdir(parents=True, exist_ok=False)
    settings = get_settings()
    model_exists = Path(settings.llm.embedding_model_path).is_dir()
    began = time.monotonic()
    with ManagedWorker(bootstrap=partial(traced_bootstrap, str(directory))) as worker:
        result = worker.start(timeout_seconds=180)
        elapsed = round(time.monotonic() - began, 3)
    report = {'checked_at_utc': now.isoformat(), 'scope': 'Isolated Windows spawn readiness; same local model/index; no jobs or Ollama',
              'elapsed_seconds': elapsed, 'model_directory_exists': model_exists, 'result': result}
    (directory / 'result.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ready': result['ready'], 'code': result['code'], 'elapsed_seconds': elapsed, 'evidence': str(directory)}), flush=True)
    return 0 if result['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
