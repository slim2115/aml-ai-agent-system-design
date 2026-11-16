"""Capture an offline QA baseline without importing or starting the application.

Run with the project's Python: python -I -B qa/tools/capture_baseline.py
Outputs are new, exclusively created JSON files in a verification subdirectory.
Only the explicit source allowlist and the two named model manifests are read.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import sys
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRECTORIES = (
    "src", "tests", "scripts", "requirements", ".github", "config",
    "data/demo-v1", "contracts", "prompts",
)
SOURCE_FILES = (
    "pyproject.toml", "requirements.txt", "requirements-contracts.txt",
    ".rebuild/origin.json",
)
EXCLUDED_NAMES = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git",
    ".venv", "node_modules", "credentials", "secrets", ".env",
})
PACKAGES = (
    "pip", "pydantic", "requests", "chromadb", "langgraph", "streamlit",
    "sentence-transformers", "transformers", "torch", "numpy", "tokenizers",
    "huggingface-hub", "safetensors", "fastapi", "uvicorn", "psycopg",
    "psycopg-binary", "confluent-kafka", "httpx", "httpx2", "pytest",
    "pytest-subtests", "pytest-playwright", "playwright", "pytest-asyncio",
    "jsonschema", "rfc3339-validator", "PyYAML",
)
HISTORICAL_DIRECTORY = ROOT / "verification/kafka-aml-2026-10-04"
EMBED_DIRECTORY = ROOT / "var/models/paraphrase-multilingual-MiniLM-L12-v2"
EMBED_MANIFEST = EMBED_DIRECTORY.with_name(EMBED_DIRECTORY.name + ".manifest.json")
EMBED_FILES = frozenset({
    "1_Pooling/config.json", "config_sentence_transformers.json", "config.json",
    "model.safetensors", "modules.json", "README.md", "sentence_bert_config.json",
    "special_tokens_map.json", "tokenizer_config.json", "tokenizer.json",
})
QWEN_MANIFEST = (
    Path.home() / ".ollama/models/manifests/registry.ollama.ai/library/qwen2.5/14b"
)


def sha256_file(path: Path) -> tuple[str, int]:
    """Stream potentially large model files and reject changes during hashing."""
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("A baseline input changed while being hashed; rerun.")
    return digest.hexdigest(), after.st_size


def safe_source_file(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if any(part in EXCLUDED_NAMES or part.startswith(".env") for part in relative.parts):
        return False
    if path.suffix.lower() in {".pyc", ".pyo"}:
        return False
    if path.is_symlink() or path.resolve() != path.absolute():
        raise RuntimeError("Symlink or junction found within the source allowlist.")
    return path.is_file()


def source_manifest() -> dict:
    files: set[Path] = set()
    missing_scope = []
    for name in SOURCE_DIRECTORIES:
        directory = ROOT / name
        if not directory.is_dir():
            missing_scope.append(name)
            continue
        files.update(path for path in directory.rglob("*") if safe_source_file(path))
    for name in SOURCE_FILES:
        path = ROOT / name
        if safe_source_file(path):
            files.add(path)
        else:
            missing_scope.append(name)
    entries = []
    for path in sorted(files, key=lambda item: item.relative_to(ROOT).as_posix()):
        digest, size = sha256_file(path)
        entries.append({"path": path.relative_to(ROOT).as_posix(), "sha256": digest, "bytes": size})
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": 1,
        "algorithm": "sha256",
        "directories": list(SOURCE_DIRECTORIES),
        "individual_files": list(SOURCE_FILES),
        "missing_scope": missing_scope,
        "exclusions": sorted(EXCLUDED_NAMES) + [".env*", "*.pyc", "*.pyo"],
        "file_count": len(entries),
        "entries_sha256": hashlib.sha256(canonical).hexdigest(),
        "files": entries,
    }


def historical_comparison(manifest: dict) -> dict:
    path = HISTORICAL_DIRECTORY / "final-files.json"
    evidence = json.loads(path.read_text(encoding="utf-8-sig"))
    old: dict[str, str] = {}
    source_root = PureWindowsPath(str(ROOT))
    ignored = 0
    for entry in evidence["files"]:
        item = PureWindowsPath(entry["path"])
        if not item.is_absolute():
            item = source_root / item
        try:
            relative = item.relative_to(source_root).as_posix()
        except ValueError:
            ignored += 1
            continue
        if PurePosixPath(relative).parts[0] not in {"src", "tests", "scripts"}:
            ignored += 1
            continue
        if relative in old:
            raise RuntimeError("Duplicate path in historical manifest.")
        old[relative] = entry["sha256"].lower()
    current = {
        entry["path"]: entry["sha256"] for entry in manifest["files"]
        if PurePosixPath(entry["path"]).parts[0] in {"src", "tests", "scripts"}
    }
    common = set(old) & set(current)
    changed = [
        {"path": name, "historical_sha256": old[name], "current_sha256": current[name]}
        for name in sorted(common) if old[name] != current[name]
    ]
    missing = sorted(set(old) - set(current))
    added = sorted(set(current) - set(old))
    return {
        "reference": path.relative_to(ROOT).as_posix(),
        "reference_sha256": sha256_file(path)[0],
        "reference_recorded_at_utc": evidence.get("recorded_at_utc"),
        "scope": "Only src/, tests/, scripts/; historical manifest is not assumed complete.",
        "historical_entries_compared": len(old),
        "historical_entries_outside_scope": ignored,
        "unchanged_count": sum(old[name] == current[name] for name in common),
        "changed": changed,
        "missing_now": missing,
        "current_files_absent_from_historical_manifest": added,
        "recorded_files_match": bool(old) and not changed and not missing,
        "interpretation": (
            "Hash comparison only. Files absent from the old manifest may have existed then; "
            "absence is not evidence they were added later. This does not rerun or renew test results."
        ),
    }


def embedding_snapshot() -> dict:
    if not EMBED_MANIFEST.is_file():
        return {"status": "manifest_missing", "model_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"}
    declared = json.loads(EMBED_MANIFEST.read_text(encoding="utf-8-sig"))
    names = [entry["path"] for entry in declared["files"]]
    if len(names) != len(EMBED_FILES) or set(names) != EMBED_FILES:
        raise RuntimeError("Embedding manifest differs from the explicit ten-file allowlist.")
    entries = []
    for item in sorted(declared["files"], key=lambda row: row["path"]):
        path = EMBED_DIRECTORY / item["path"]
        if path.resolve() != path.absolute():
            raise RuntimeError("Symlink or junction in embedding model paths.")
        expected = item["sha256"].lower()
        if not path.is_file():
            entries.append({"path": item["path"], "expected_sha256": expected, "status": "missing"})
            continue
        digest, size = sha256_file(path)
        entries.append({
            "path": item["path"], "expected_sha256": expected, "sha256": digest,
            "bytes": size, "status": "match" if digest == expected else "mismatch",
        })
    return {
        "model_id": declared["model_id"],
        "manifest": EMBED_MANIFEST.relative_to(ROOT).as_posix(),
        "manifest_sha256": sha256_file(EMBED_MANIFEST)[0],
        "status": "all_declared_files_match" if all(e["status"] == "match" for e in entries) else "incomplete_or_mismatch",
        "files_verified": len(entries),
        "files": entries,
        "model_loaded": False,
        "interpretation": "Integrity against the existing local manifest, not model quality or upstream authenticity.",
    }


def qwen_snapshot() -> dict:
    result = {
        "model_id": "qwen2.5:14b",
        "manifest_location": "<user-home>/.ollama/models/manifests/registry.ollama.ai/library/qwen2.5/14b",
        "blobs_verified": False,
        "blobs_read": False,
        "model_loaded": False,
        "interpretation": "Manifest digest and declared layer identities only; blob presence and content are not verified.",
    }
    if not QWEN_MANIFEST.is_file():
        return {**result, "status": "manifest_missing"}
    if QWEN_MANIFEST.stat().st_size > 1024 * 1024:
        raise RuntimeError("Unexpectedly large Ollama manifest; not read.")
    declared = json.loads(QWEN_MANIFEST.read_text(encoding="utf-8-sig"))
    def identity(item: dict) -> dict:
        return {key: item.get(key) for key in ("mediaType", "digest", "size")}
    return {
        **result,
        "status": "manifest_present_blobs_not_verified",
        "manifest_sha256": sha256_file(QWEN_MANIFEST)[0],
        "schema_version": declared.get("schemaVersion"),
        "config": identity(declared.get("config", {})),
        "layers": [identity(item) for item in declared.get("layers", [])],
    }


def historical_test_results() -> dict:
    unit_path = HISTORICAL_DIRECTORY / "full/unit-verification.json"
    ui_path = HISTORICAL_DIRECTORY / "ui-cli-qualification.json"
    unit = json.loads(unit_path.read_text(encoding="utf-8-sig"))
    ui = json.loads(ui_path.read_text(encoding="utf-8-sig"))
    return {
        "status": "historical_only_not_rerun",
        "regression": {
            "evidence": unit_path.relative_to(ROOT).as_posix(),
            "evidence_sha256": sha256_file(unit_path)[0],
            **{key: unit.get(key) for key in (
                "checked_at_utc", "status", "collected_tests", "additional_subtests",
                "tests", "failures", "errors", "skipped", "scope",
            )},
        },
        "ui_cli": {
            "evidence": ui_path.relative_to(ROOT).as_posix(),
            "evidence_sha256": sha256_file(ui_path)[0],
            **{key: ui.get(key) for key in ("checked_at_utc", "status", "scope")},
            "checks": len(ui["checks"]),
            "passed": sum(item.get("passed") is True for item in ui["checks"]),
        },
        "interpretation": "Existing reports are evidence of earlier runs only. This collector executes no tests.",
    }


def package_versions() -> dict:
    versions = {}
    for package in PACKAGES:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    return {"source": "importlib.metadata; packages not imported", "null_means": "not_installed_in_this_interpreter", "packages": versions}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="verification/qa-step-01-2026-10-07")
    args = parser.parse_args()
    output = Path(args.output_dir)
    if not output.is_absolute():
        output = ROOT / output
    output = output.absolute()
    verification = (ROOT / "verification").resolve()
    if output.resolve() != output or not output.is_relative_to(verification) or output == verification:
        parser.error("Output must be a normal subdirectory of this project's verification/.")
    targets = [output / "file-manifest.json", output / "baseline.json"]
    if any(path.exists() for path in targets):
        parser.error("Baseline files already exist; choose a new verification subdirectory. Nothing was overwritten.")
    started_at = datetime.now(timezone.utc).isoformat()
    manifest = source_manifest()
    origin_path = ROOT / ".rebuild/origin.json"
    origin = json.loads(origin_path.read_text(encoding="utf-8-sig"))
    baseline = {
        "schema_version": 1,
        "capture_started_at_utc": started_at,
        "report_date_local": datetime.fromisoformat(started_at).astimezone(timezone(timedelta(hours=3))).date().isoformat(),
        "report_timezone": "Europe/Moscow",
        "report_utc_offset": "+03:00",
        "purpose": "Read-only local QA baseline; no runtime or service qualification.",
        "collector": {"path": "qa/tools/capture_baseline.py", "sha256": sha256_file(Path(__file__))[0]},
        "safety": {
            "application_imported": False, "models_loaded": False, "services_started": False,
            "tests_executed": False, "network_used": False, "environment_dumped": False,
            "credentials_read": False, "git_executed": False,
        },
        "runtime": {
            "python": platform.python_version(), "implementation": platform.python_implementation(),
            "interpreter": sys.executable, "system": platform.system(),
            "release": platform.release(), "machine": platform.machine(),
            "package_versions": package_versions(),
        },
        "provenance": {
            "source_commit": origin["source_commit"],
            "source_archive_sha256": origin["archive_sha256"],
            "copied_at_utc": origin["created_at_utc"],
            "git_directory_present": (ROOT / ".git").exists(),
            "interpretation": "The archived source commit identifies initial provenance, not the current rebuilt code. Current files are identified by file-manifest.json.",
        },
        "source_snapshot": {
            "manifest": "file-manifest.json", "file_count": manifest["file_count"],
            "entries_sha256": manifest["entries_sha256"], "missing_scope": manifest["missing_scope"],
        },
        "historical_source_comparison": historical_comparison(manifest),
        "historical_test_results": historical_test_results(),
        "models": {"embedding": embedding_snapshot(), "generator": qwen_snapshot()},
    }
    baseline["capture_completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    baseline["source_snapshot"]["manifest_file_sha256"] = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    with targets[0].open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(manifest_text)
    with targets[1].open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(baseline, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({
        "status": "captured", "output": output.relative_to(ROOT).as_posix(),
        "source_files": manifest["file_count"],
        "historical_recorded_files_match": baseline["historical_source_comparison"]["recorded_files_match"],
        "embedding": baseline["models"]["embedding"]["status"],
        "qwen": baseline["models"]["generator"]["status"],
        "tests_executed": False,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
