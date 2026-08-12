"""Unit-тесты слоя доступа к данным (детерминированные, без LLM).

Проверяют: валидацию incident_id (SR-01), извлечение клиента/транзакций
(SR-02/03), консолидацию контекста и оценку полноты (SR-04/20), read-only (BR-07).
"""
from __future__ import annotations

import pytest

from src.data_access import (
    CaseRepository,
    ClientRepository,
    InvestigationContextBuilder,
    TransactionRepository,
)
from src.data_access.exceptions import EntityNotFoundError, InvalidInputError
from src.schemas import DataCompleteness


def test_incident_id_validation():
    """Невалидный incident_id отклоняется (SR-01, AC-01.2)."""
    with pytest.raises(InvalidInputError):
        CaseRepository().get_by_incident_id("BAD-ID")


def test_client_lookup():
    """Извлечение клиента по ID (SR-02)."""
    client = ClientRepository().get_by_id("C-00045")
    assert client is not None
    assert client.client_id == "C-00045"


def test_transactions_by_client():
    """Извлечение транзакций клиента (SR-03)."""
    txs = TransactionRepository().get_by_client("C-00045")
    assert len(txs) > 0
    assert all(tx.client_id == "C-00045" for tx in txs)


def test_context_full_completeness():
    """Кейс с транзакциями -> полнота FULL (SR-04/20)."""
    context = InvestigationContextBuilder().build("INC-000123")
    assert context.data_completeness is DataCompleteness.FULL
    assert len(context.transactions) > 0


def test_context_partial_no_transactions():
    """Кейс без транзакций -> полнота PARTIAL, триггер отказа (SR-20, BRULE-03)."""
    context = InvestigationContextBuilder().build("INC-000999")
    assert context.data_completeness is DataCompleteness.PARTIAL
    assert len(context.transactions) == 0


def test_case_not_found():
    """Несуществующий incident_id (валидный формат) -> EntityNotFoundError."""
    with pytest.raises(EntityNotFoundError):
        InvestigationContextBuilder().build("INC-999999")


def test_repositories_are_read_only():
    """Репозитории не имеют методов записи (BR-07, SR-18)."""
    for repo in (ClientRepository(), TransactionRepository(), CaseRepository()):
        write_methods = [
            m for m in dir(repo)
            if m.startswith(("create", "update", "delete", "save", "insert", "write"))
        ]
        assert write_methods == [], f"Найдены write-методы: {write_methods}"