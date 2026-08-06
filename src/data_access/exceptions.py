"""Доменные исключения слоя доступа к данным.

Исключений достаточно для различения ситуаций, критичных для политики
отказа (SR-20, BRULE-03): неверный вход (SR-01) и отсутствие сущности.
"""
from __future__ import annotations


class DataAccessError(Exception):
    """Базовое исключение слоя доступа к данным."""


class InvalidInputError(DataAccessError):
    """Невалидный входной параметр (например, формат incident_id, SR-01)."""


class EntityNotFoundError(DataAccessError):
    """Запрошенная сущность не найдена в источнике данных."""