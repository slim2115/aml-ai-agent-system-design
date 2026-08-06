"""Репозиторий правил комплаенса / базы знаний (Data Model, раздел 3.5; SR-07).

Только чтение (BR-07, SR-18). Правила являются источником для RAG-поиска
(ADR-0004) и для валидации source_ref (SR-12).
"""
from __future__ import annotations

from typing import Optional

from src.data_access.base import BaseJsonRepository
from src.schemas import Rule


class RuleRepository(BaseJsonRepository[Rule]):
    model_type = Rule
    file_name = "rules.json"

    def get_by_id(self, rule_id: str) -> Optional[Rule]:
        """Возвращает правило по rule_id или None."""
        for rule in self._load_all():
            if rule.rule_id == rule_id:
                return rule
        return None

    def get_known_regulation_refs(self) -> set[str]:
        """Возвращает множество всех существующих regulation_ref.

        Используется для валидации source_ref (SR-12, Traceability Ratio = 100%).
        """
        return {rule.regulation_ref for rule in self._load_all()}