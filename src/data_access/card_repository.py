"""Репозиторий банковских карт (Data Model, раздел 3.2).

Только чтение (BR-07, SR-18). Полный PAN не хранится — только маска.
"""
from __future__ import annotations

from typing import List

from src.data_access.base import BaseJsonRepository
from src.schemas import Card


class CardRepository(BaseJsonRepository[Card]):
    model_type = Card
    file_name = "cards.json"

    def get_by_client(self, client_id: str) -> List[Card]:
        """Возвращает все карты клиента."""
        return [card for card in self._load_all() if card.client_id == client_id]