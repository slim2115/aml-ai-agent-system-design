"""Verified offline sentence-transformer adapter, with no import-time I/O."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from src.config import Settings, get_settings, resolve_project_path


class EmbeddingNotReadyError(RuntimeError):
    """Stable public failure; paths and underlying exception details stay private."""

    code = "EMBEDDING_NOT_READY"

    def __init__(self) -> None:
        super().__init__("EMBEDDING_NOT_READY: verified local embedding artifact is unavailable")


def _verified_artifact(settings: Settings) -> Path:
    root = settings.project_root
    model_dir = Path(resolve_project_path(settings.llm.embedding_model_path, root=root, key="EMBEDDING_MODEL_PATH"))
    manifest_path = Path(resolve_project_path(settings.llm.embedding_manifest_path, root=root, key="EMBEDDING_MANIFEST_PATH"))
    if not model_dir.is_dir() or not manifest_path.is_file():
        raise ValueError("Missing offline artifact")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or manifest.get("model_id") != settings.llm.embedding_model_name
        or not isinstance(manifest.get("files"), list)
        or not manifest["files"]
    ):
        raise ValueError("Invalid manifest")
    expected: set[str] = set()
    casefolded: set[str] = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            raise ValueError("Invalid manifest entry")
        raw = entry.get("path")
        digest = entry.get("sha256")
        if (
            not isinstance(raw, str)
            or not raw
            or "\\" in raw
            or ":" in raw
            or "\x00" in raw
            or any(part in {"", ".", ".."} for part in raw.split("/"))
            or PurePosixPath(raw).is_absolute()
            or raw.casefold() in casefolded
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None
        ):
            raise ValueError("Invalid file inventory")
        expected.add(raw)
        casefolded.add(raw.casefold())
        file_path = model_dir.joinpath(*PurePosixPath(raw).parts)
        resolved = file_path.resolve(strict=True)
        resolved.relative_to(model_dir)
        if not file_path.is_file() or file_path.is_symlink():
            raise ValueError("Invalid artifact file")
        checksum = hashlib.sha256()
        with file_path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(block)
        if checksum.hexdigest() != digest.lower():
            raise ValueError("Artifact checksum mismatch")
    actual: set[str] = set()
    for candidate in model_dir.rglob("*"):
        # Reject links, including linked directories that rglob may not traverse.
        if candidate.is_symlink():
            raise ValueError("Artifact links are not supported")
        candidate.resolve(strict=True).relative_to(model_dir)
        if candidate.is_file():
            actual.add(candidate.relative_to(model_dir).as_posix())
    if actual != expected:
        raise ValueError("Artifact inventory mismatch")
    return model_dir


def validate_embedding_artifact(settings: Settings | None = None) -> Path:
    """Verify manifest identity, complete file inventory and SHA-256 checksums."""
    try:
        return _verified_artifact(settings or get_settings())
    except Exception:
        raise EmbeddingNotReadyError() from None


class LocalSentenceTransformerEmbedding:
    """Chroma's callable interface, backed only by a verified local CPU model."""

    def __init__(self, model: Any, settings: Settings) -> None:
        self._model = model
        self._settings = settings

    @staticmethod
    def name() -> str:
        return "rebuild_local_sentence_transformer"

    def is_legacy(self) -> bool:
        return False

    def get_config(self) -> dict[str, str]:
        return {
            "model_name": self._settings.llm.embedding_model_name,
            "model_path": self._settings.llm.embedding_model_path,
            "manifest_path": self._settings.llm.embedding_manifest_path,
        }

    @staticmethod
    def build_from_config(config: dict[str, str]) -> "LocalSentenceTransformerEmbedding":
        settings = get_settings()
        expected = {
            "model_name": settings.llm.embedding_model_name,
            "model_path": settings.llm.embedding_model_path,
            "manifest_path": settings.llm.embedding_manifest_path,
        }
        if config != expected:
            raise EmbeddingNotReadyError()
        return build_embedding_function(settings)

    def __call__(self, input: list[str]) -> list[list[float]]:
        try:
            result = self._model.encode(list(input), convert_to_numpy=True, normalize_embeddings=False, show_progress_bar=False)
            return result.tolist()
        except Exception:
            raise EmbeddingNotReadyError() from None

    def embed_query(self, input: list[str]) -> list[list[float]]:
        return self(input)


def build_embedding_function(
    settings: Settings | None = None,
    *,
    model_factory: Callable[..., Any] | None = None,
) -> LocalSentenceTransformerEmbedding:
    """Load the configured offline artifact; never download or fall back.

    model_factory permits hermetic tests without importing the model runtime.
    Even injected factories run only after the full artifact verification.
    """
    selected = settings or get_settings()
    model_dir = validate_embedding_artifact(selected)
    try:
        if model_factory is None:
            from sentence_transformers import SentenceTransformer

            model_factory = SentenceTransformer
        model = model_factory(str(model_dir), device="cpu", local_files_only=True, trust_remote_code=False)
        return LocalSentenceTransformerEmbedding(model, selected)
    except Exception:
        raise EmbeddingNotReadyError() from None
