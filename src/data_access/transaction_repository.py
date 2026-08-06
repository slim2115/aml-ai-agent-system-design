"""Репозиторий транзакций (Data Model, раздел 3.3; SR-03).

Только чтение (BR-07, SR-18). Поле purpose — потенциальный вектор
prompt injection (SR-19): репозиторий возвращает его как данные,
обработка — ответственность guardrails.
"""
from __future__ import annotations

from typing import List, Optional

from src.data_access.base import BaseJsonRepository
from src.schemas import Transaction


class TransactionRepository(BaseJsonRepository[Transaction]):
    model_type = Transaction
    file_name = "transactions.json"

    def get_by_client(self, client_id: str) -> List[Transaction]:
        """Возвращает все транзакции клиента (SR-03)."""
        return [tx for tx in self._load_all() if tx.client_id == client_id]

    def get_by_id(self, tx_id: str) -> Optional[Transaction]:
        """Возвращает транзакцию по tx_id (используется для evidence_ref, SR-14)."""
        for tx in self._load_all():
            if tx.tx_id == tx_id:
                return tx
        return None