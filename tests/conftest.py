"""Общие фикстуры для тестов (unit + evaluation).

Дорогие ресурсы (агент, база знаний) создаются один раз на сессию.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agent import AMLAgent
from src.data_access import TransactionRepository
from src.data_access.rule_repository import RuleRepository
from src.retrieval import KnowledgeBase

GOLDEN_DATASET_PATH = Path(__file__).parent / "golden_dataset.json"


@pytest.fixture(scope="session")
def golden_dataset() -> list[dict]:
    """Загружает Golden Dataset (Evaluation Plan, раздел 2)."""
    if not GOLDEN_DATASET_PATH.exists():
        pytest.skip(f"Golden Dataset не найден: {GOLDEN_DATASET_PATH}")
    return json.loads(GOLDEN_DATASET_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def knowledge_base() -> KnowledgeBase:
    """Индексированная база знаний комплаенса (SR-07)."""
    kb = KnowledgeBase(RuleRepository().find_all())
    kb.index()
    return kb


@pytest.fixture(scope="session")
def transaction_repository() -> TransactionRepository:
    """Репозиторий транзакций (для проверки evidence grounding, SR-14)."""
    return TransactionRepository()


@pytest.fixture(scope="session")
def agent() -> AMLAgent:
    """Скомпилированный агент (LangGraph). Инициализация не требует Ollama."""
    return AMLAgent()