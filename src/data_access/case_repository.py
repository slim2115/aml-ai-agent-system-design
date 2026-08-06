"""Репозиторий кейсов / инцидентов (Data Model, раздел 3.4; SR-01).

Только чтение (BR-07, SR-18). Валидирует формат incident_id (SR-01)
до обращения к данным.
"""
from __future__ import annotations

import re
from typing import Optional

from src.data_access.base import BaseJsonRepository
from src.data_access.exceptions import EntityNotFoundError, InvalidInputError
from src.schemas import Case

#: Формат внешнего идентификатора инцидента (SR-01).
INCIDENT_ID_PATTERN = re.compile(r"^INC-\d{6}$")


class CaseRepository(BaseJsonRepository[Case]):
    model_type = Case
    file_name = "cases.json"

    @staticmethod
    def validate_incident_id(incident_id: str) -> None:
        """Проверяет формат incident_id (SR-01, AC-01.2).

        Raises:
            InvalidInputError: при несоответствии формату INC-NNNNNN.
        """
        if not INCIDENT_ID_PATTERN.match(incident_id):
            raise InvalidInputError(
                f"Неверный формат incident_id: {incident_id!r} (ожидается INC-NNNNNN)"
            )

    def get_by_incident_id(self, incident_id: str) -> Optional[Case]:
        """Возвращает кейс по incident_id или None (SR-01)."""
        self.validate_incident_id(incident_id)
        for case in self._load_all():
            if case.incident_id == incident_id:
                return case
        return None

    def get_by_incident_id_or_raise(self, incident_id: str) -> Case:
        """Возвращает кейс или выбрасывает EntityNotFoundError."""
        case = self.get_by_incident_id(incident_id)
        if case is None:
            raise EntityNotFoundError(f"Кейс не найден для incident_id: {incident_id}")
        return case