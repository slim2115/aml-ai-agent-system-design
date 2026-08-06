"""Конфигурация AML / Anti-Fraud Copilot.

Загружает параметры из переменных окружения (.env) и предоставляет типизированный
иммутабельный объект настроек. Секреты в репозиторий не попадают: используется
.env.example как шаблон, реальный .env — в .gitignore (QA.md, раздел 5.3).

Связь с артефактами:
- ADR-0005: on-premise LLM (Ollama) — параметры LLMConfig.
- Evaluation Plan, раздел 3: пороги метрик — ThresholdsConfig.
- NFR-07: таймаут генерации — PerformanceConfig.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# Загрузка .env из корня проекта (если файл существует).
load_dotenv()

# Корень проекта: src/config.py -> parent (src) -> parent (корень).
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


# =============================================================================
# Вспомогательные функции чтения переменных окружения
# =============================================================================

def _env_str(key: str, default: str) -> str:
    """Читает строковую переменную окружения с дефолтом."""
    return os.getenv(key, default).strip()


def _env_float(key: str, default: float) -> float:
    """Читает вещественную переменную окружения с дефолтом."""
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(key: str, default: int) -> int:
    """Читает целочисленную переменную окружения с дефолтом."""
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


# =============================================================================
# Секции конфигурации (иммутабельные)
# =============================================================================

@dataclass(frozen=True)
class LLMConfig:
    """Параметры локальной LLM (ADR-0005, on-premise)."""
    base_url: str
    model_name: str
    embedding_model_name: str


@dataclass(frozen=True)
class VectorStoreConfig:
    """Параметры векторной БД ChromaDB (ADR-0004, ADR-0005)."""
    persist_dir: str
    collection_name: str


@dataclass(frozen=True)
class DataConfig:
    """Пути к синтетическим данным (дисклеймер: data/README.md)."""
    synthetic_data_dir: str


@dataclass(frozen=True)
class ThresholdsConfig:
    """Пороги метрик качества (Evaluation Plan, раздел 3).

    Значения соответствуют NFR:
      faithfulness       -> NFR-02 (PoC >= 0.90);
      schema_match       -> NFR-01 (= 1.0);
      traceability       -> NFR-04 (= 1.0);
      refusal_correctness-> NFR-05 (>= 0.95).
    """
    faithfulness: float
    schema_match: float
    traceability: float
    refusal_correctness: float

    def __post_init__(self) -> None:
        """Контроль диапазонов порогов [0..1]."""
        for name in ("faithfulness", "schema_match", "traceability", "refusal_correctness"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"Порог {name}={value} вне диапазона [0..1]")


@dataclass(frozen=True)
class PerformanceConfig:
    """Параметры производительности (NFR-07)."""
    generation_timeout_sec: int


@dataclass(frozen=True)
class Settings:
    """Агрегированная конфигурация приложения."""
    project_root: Path
    llm: LLMConfig
    vector_store: VectorStoreConfig
    data: DataConfig
    thresholds: ThresholdsConfig
    performance: PerformanceConfig


# =============================================================================
# Фабрика конфигурации
# =============================================================================

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Создаёт (и кэширует) объект конфигурации из переменных окружения.

    Дефолты соответствуют .env.example. Вызывается один раз; повторные вызовы
    возвращают закэшированный экземпляр.

    Returns:
        Settings: иммутабельная конфигурация приложения.
    """
    return Settings(
        project_root=PROJECT_ROOT,
        llm=LLMConfig(
            base_url=_env_str("OLLAMA_BASE_URL", "http://localhost:11434"),
            model_name=_env_str("LLM_MODEL_NAME", "llama3"),
            embedding_model_name=_env_str("EMBEDDING_MODEL_NAME", "nomic-embed-text"),
        ),
        vector_store=VectorStoreConfig(
            persist_dir=_env_str("CHROMA_PERSIST_DIR", "./data/chroma"),
            collection_name=_env_str("KNOWLEDGE_BASE_COLLECTION", "aml_compliance_rules"),
        ),
        data=DataConfig(
            synthetic_data_dir=_env_str("SYNTHETIC_DATA_DIR", "./data/synthetic"),
        ),
        thresholds=ThresholdsConfig(
            faithfulness=_env_float("FAITHFULNESS_THRESHOLD", 0.90),
            schema_match=_env_float("SCHEMA_MATCH_THRESHOLD", 1.0),
            traceability=_env_float("TRACEABILITY_THRESHOLD", 1.0),
            refusal_correctness=_env_float("REFUSAL_CORRECTNESS_THRESHOLD", 0.95),
        ),
        performance=PerformanceConfig(
            generation_timeout_sec=_env_int("GENERATION_TIMEOUT_SEC", 180),
        ),
    )