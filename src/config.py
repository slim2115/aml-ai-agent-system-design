"""Validated process-local settings; importing this module performs no file I/O.

No dotenv file is searched for or loaded. Relative configured paths are anchored
to this checkout, never to the caller's working directory. Validation happens
when get_settings() is called, before a client or model may be constructed.
"""
from __future__ import annotations

import ipaddress
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

# abspath is lexical: unlike resolve(), it does not inspect the filesystem.
PROJECT_ROOT = Path(os.path.abspath(__file__)).parent.parent
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_EMBEDDING_BASENAME = "paraphrase-multilingual-MiniLM-L12-v2"


def _env_str(key: str, default: str) -> str:
    value = os.environ.get(key, default).strip()
    if not value:
        raise ValueError(f"{key} must not be empty")
    return value


def _env_float(key: str, default: float) -> float:
    try:
        value = float(_env_str(key, str(default)))
    except ValueError:
        raise ValueError(f"{key} must be a finite number") from None
    if not math.isfinite(value):
        raise ValueError(f"{key} must be a finite number")
    return value


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env_str(key, str(default)))
    except ValueError:
        raise ValueError(f"{key} must be an integer") from None


def resolve_project_path(value: str | Path, *, root: Path, key: str) -> str:
    """Resolve a path inside root, rejecting traversal and escaping symlinks.

    No directories are created. The explicit traversal check also rejects a
    redundant '..' that would otherwise normalize back into the checkout.
    """
    raw = str(value).strip()
    windows_path = PureWindowsPath(raw)
    if not raw or "\x00" in raw or ".." in windows_path.parts or ".." in Path(raw).parts:
        raise ValueError(f"{key} must be a path inside the project root without traversal")
    # Refuse Windows drive/UNC spellings on non-Windows hosts rather than
    # accidentally treating an outside Windows path as a relative POSIX path.
    if os.name != "nt" and (windows_path.drive or raw.startswith("\\")):
        raise ValueError(f"{key} must be a path inside the project root")
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        resolved_root = root.resolve()
        resolved = path.resolve()
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        raise ValueError(f"{key} must resolve inside the project root") from None
    return str(resolved)


def validate_loopback_url(value: str) -> str:
    """Accept only literal loopback addresses or localhost; never use DNS."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or any(character.isspace() for character in value)
            or "%" in hostname
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError
        if hostname.lower() != "localhost" and not ipaddress.ip_address(hostname).is_loopback:
            raise ValueError
    except ValueError:
        raise ValueError("OLLAMA_BASE_URL must be an HTTP(S) loopback URL without credentials or extra components") from None
    return value.rstrip("/")


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model_name: str
    embedding_model_name: str
    embedding_model_path: str
    embedding_manifest_path: str

    def __post_init__(self) -> None:
        validate_loopback_url(self.base_url)
        if not self.model_name.strip() or not self.embedding_model_name.strip():
            raise ValueError("Model names must not be empty")


@dataclass(frozen=True)
class VectorStoreConfig:
    persist_dir: str
    collection_name: str


@dataclass(frozen=True)
class DataConfig:
    synthetic_data_dir: str


@dataclass(frozen=True)
class ThresholdsConfig:
    faithfulness: float
    schema_match: float
    traceability: float
    refusal_correctness: float
    rag_recall: float
    rag_precision: float

    def __post_init__(self) -> None:
        floors = {
            "faithfulness": 0.90,
            "schema_match": 1.0,
            "traceability": 1.0,
            "refusal_correctness": 0.95,
            "rag_recall": 1.0,
            "rag_precision": 1.0,
        }
        for name, floor in floors.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or not floor <= value <= 1.0:
                raise ValueError(f"{name} must be finite and in [{floor}, 1.0]")


@dataclass(frozen=True)
class PerformanceConfig:
    generation_timeout_sec: int

    def __post_init__(self) -> None:
        value = self.generation_timeout_sec
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 180:
            raise ValueError("GENERATION_TIMEOUT_SEC must be an integer in [1, 180]")


@dataclass(frozen=True)
class Settings:
    project_root: Path
    llm: LLMConfig
    vector_store: VectorStoreConfig
    data: DataConfig
    thresholds: ThresholdsConfig
    performance: PerformanceConfig


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Validate and cache settings from this process's environment only."""
    from src.qa_profile import checked_path, get_qa_profile
    profile = get_qa_profile()
    root = PROJECT_ROOT.resolve()

    def path(key: str, default: Path) -> str:
        return resolve_project_path(_env_str(key, str(default)), root=root, key=key)

    def mutable_path(key: str, default: Path, qa_path: Path | None) -> str:
        if qa_path is not None:
            checked_path(qa_path, profile.root)
        selected = path(key, qa_path if qa_path is not None else default)
        if qa_path is not None and Path(selected) != qa_path.resolve():
            raise ValueError(f"{key} must use the active QA run path")
        return selected

    return Settings(
        project_root=root,
        llm=LLMConfig(
            base_url=validate_loopback_url(_env_str("OLLAMA_BASE_URL", "http://127.0.0.1:11434")),
            model_name=_env_str("LLM_MODEL_NAME", "qwen2.5:14b"),
            embedding_model_name=_env_str("EMBEDDING_MODEL_NAME", DEFAULT_EMBEDDING_MODEL),
            embedding_model_path=path("EMBEDDING_MODEL_PATH", root / "var" / "models" / _EMBEDDING_BASENAME),
            embedding_manifest_path=path("EMBEDDING_MANIFEST_PATH", root / "var" / "models" / f"{_EMBEDDING_BASENAME}.manifest.json"),
        ),
        vector_store=VectorStoreConfig(
            persist_dir=mutable_path("CHROMA_PERSIST_DIR", root / "var" / "chroma",
                                     profile.chroma_dir if profile else None),
            collection_name=_env_str("KNOWLEDGE_BASE_COLLECTION", "aml_compliance_rules_rebuild"),
        ),
        data=DataConfig(synthetic_data_dir=mutable_path("SYNTHETIC_DATA_DIR", root / "data" / "synthetic",
                                                       profile.seed_dir if profile else None)),
        thresholds=ThresholdsConfig(
            faithfulness=_env_float("FAITHFULNESS_THRESHOLD", 0.90),
            schema_match=_env_float("SCHEMA_MATCH_THRESHOLD", 1.0),
            traceability=_env_float("TRACEABILITY_THRESHOLD", 1.0),
            refusal_correctness=_env_float("REFUSAL_CORRECTNESS_THRESHOLD", 0.95),
            rag_recall=_env_float("RAG_RECALL_THRESHOLD", 1.0),
            rag_precision=_env_float("RAG_PRECISION_THRESHOLD", 1.0),
        ),
        performance=PerformanceConfig(generation_timeout_sec=_env_int("GENERATION_TIMEOUT_SEC", 180)),
    )


def get_demo_paths(root: Path | None = None) -> tuple[Path, Path]:
    """Select one run's policy and frozen sources; an invalid QA profile never falls back."""
    from src.qa_profile import checked_path, get_qa_profile
    profile = get_qa_profile()
    if profile is not None:
        return (checked_path(profile.policy_path, profile.root),
                checked_path(profile.seed_dir, profile.root))
    root = PROJECT_ROOT if root is None else root
    return root / 'config/demo-policy.json', root / 'data/demo-v1'
