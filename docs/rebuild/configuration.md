# Block 3: local configuration and offline embedding

`src/config.py` reads process environment only when `get_settings()` is called.
Importing it does not load `.env`, inspect paths, create directories, connect to a
service, or load a model. The root is derived lexically from this module, then
resolved during settings validation. Settings are immutable and cached; tests
may explicitly clear `get_settings.cache_clear()` between environments.

## Defaults and validation

| Setting | Default / rule |
| --- | --- |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434`; HTTP(S), literal loopback IP or `localhost` only |
| `LLM_MODEL_NAME` | `qwen2.5:14b` |
| `EMBEDDING_MODEL_NAME` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` |
| `EMBEDDING_MODEL_PATH` | `<root>/var/models/paraphrase-multilingual-MiniLM-L12-v2` |
| `EMBEDDING_MANIFEST_PATH` | `<root>/var/models/paraphrase-multilingual-MiniLM-L12-v2.manifest.json` |
| `CHROMA_PERSIST_DIR` | `<root>/var/chroma` |
| `SYNTHETIC_DATA_DIR` | `<root>/data/synthetic` |
| `KNOWLEDGE_BASE_COLLECTION` | `aml_compliance_rules_rebuild` |
| `FAITHFULNESS_THRESHOLD` | `0.90`, allowed `[0.90, 1.0]` |
| `SCHEMA_MATCH_THRESHOLD` | `1.0`, fixed |
| `TRACEABILITY_THRESHOLD` | `1.0`, fixed |
| `REFUSAL_CORRECTNESS_THRESHOLD` | `0.95`, allowed `[0.95, 1.0]` |
| `RAG_RECALL_THRESHOLD` | `1.0`, fixed |
| `RAG_PRECISION_THRESHOLD` | `1.0`, fixed |
| `GENERATION_TIMEOUT_SEC` | `180`, integer `[1, 180]` |

All four configurable filesystem paths resolve inside this project's computed
root. Relative paths are anchored to the project, independently of the current
directory. Explicit `..` traversal and paths whose symlink resolution escapes
the root are rejected before client initialization. Validation does not create
the target directories. Loopback URL validation uses numeric IP parsing, never
DNS, and rejects credentials, non-root paths, queries and fragments.

The technical choice for environment overrides is **fail closed**: invalid,
nonfinite, out-of-range or weaker thresholds raise `ValueError`; values are not
clamped and do not fall back to defaults. Empty values also fail. Stronger
faithfulness/refusal thresholds and shorter positive timeouts are accepted.
This keeps accidental environment values from weakening release acceptance.
Timeout configuration itself is not an implementation of an end-to-end job
deadline; execution/deadline handling belongs to its later block.

`.env.example` is documentation only. Dot-source
`scripts/rebuild/Enter-LocalEnvironment.ps1` for explicit process-local settings,
absolute paths, offline flags and cache locations in this checkout. The launcher
does not start a service, download an artifact, or load a model.

## Offline artifact contract

Before invoking SentenceTransformer, `src/retrieval/embedding.py` requires a
local artifact directory and an adjacent manifest. The manifest has this shape:

```json
{
  "schema_version": 1,
  "model_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
  "files": [
    {"path": "config.json", "sha256": "<64 hexadecimal SHA-256 characters>"}
  ]
}
```

The example entry is illustrative: the real manifest inventories **every file**
under the artifact directory, including nested files. `model_id` must exactly
match the configured `EMBEDDING_MODEL_NAME`. Relative manifest paths use `/` and
must be unique, including case-insensitive comparison. Missing files, extra
files, checksum mismatches, malformed manifests, traversal, absolute paths and
symlinks inside the artifact are rejected. This manifest checks integrity and
configuration consistency; it is not a cryptographic publisher signature.

An explicitly staged artifact may be copied from the previously audited,
read-only local cache; staging must not execute the model or download files.
The manifest stays outside the inventoried directory to avoid a self-hash.
Changing the model requires a matching separately staged artifact and manifest;
merely setting an identifier cannot trigger a download or select a built-in
fallback. Later index lifecycle work must handle embedding-space changes.

The embedding factory loads the validated directory with `device="cpu"`,
`local_files_only=True` and `trust_remote_code=False`. The configured model
identity is checked against the manifest and the configured directory is passed
to the loader. There is no Chroma default embedding fallback. Knowledge-base
and embedding imports are lazy with respect to Chroma, SentenceTransformers
and schema dependencies; importing modules cannot load an artifact.

Absent, invalid or unloadable artifacts raise `EmbeddingNotReadyError`, with
stable code `EMBEDDING_NOT_READY` and a safe message without raw loader errors,
paths or secrets. The original exception is suppressed. The adapter also
sanitizes encode failures. No artifact success is inferred from mock tests.

## Hermetic checks

`python -B -m unittest discover -s tests/configuration -v` uses only stdlib and
synthetic files under `var/tmp`. Injectable model factories verify loader
arguments without importing Chroma, Torch or SentenceTransformers, performing
network requests or executing a model. The suite covers defaults, import side
effects, CWD independence, path escape, loopback-only URLs, strict thresholds,
timeout bounds, artifact inventory/integrity and safe error behavior.
