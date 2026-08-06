"""Репозиторий профилей клиентов (Data Model, раздел 3.1; SR-02).

Только чтение (BR-07, SR-18). PII-поля (full_name, inn) маскируются
на уровне логирования (NFR-09), а не в самом репозитории.
"""
from __future__ import annotations

from typing import Optional

from src.data_access.base import BaseJsonRepository
from src.data_access.exceptions import EntityNotFoundError
from src.schemas import Client


class ClientRepository(BaseJsonRepository[Client]):
    model_type = Client
    file_name = "clients.json"

    def get_by_id(self, client_id: str) -> Optional[Client]:
        """Возвращает клиента по ID или None, если не найден."""
        for client in self._load_all():
            if client.client_id == client_id:
                return client
        return None

    def get_by_id_or_raise(self, client_id: str) -> Client:
        """Возвращает клиента или выбрасывает EntityNotFoundError."""
        client = self.get_by_id(client_id)
        if client is None:
            raise EntityNotFoundError(f"Клиент не найден: {client_id}")
        return client