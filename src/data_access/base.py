"""Базовый read-only репозиторий над JSON-файлами синтетических данных.

Реализует общий паттерн: однократная загрузка JSON, валидация каждой записи
через Pydantic-модель (Data Model v0.1), кэширование в памяти.

Архитектурная гарантия read-only (BR-07, SR-18): базовый класс и наследники
предоставляют только операции чтения. Методы записи/изменения отсутствуют.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Generic, List, Optional, Type, TypeVar

from pydantic import BaseModel

from src.config import get_settings

T = TypeVar("T", bound=BaseModel)


class BaseJsonRepository(Generic[T]):
    """Read-only репозиторий над JSON-файлом синтетических данных.

    Наследники задают ``model_type`` (Pydantic-модель записи) и ``file_name``
    (имя JSON-файла в data/synthetic/).
    """

    #: Pydantic-модель записи (переопределяется в наследниках).
    model_type: Type[T]
    #: Имя JSON-файла источника (переопределяется в наследниках).
    file_name: str

    def __init__(self) -> None:
        self._cache: Optional[List[T]] = None

    def _data_path(self) -> Path:
        """Абсолютный путь к JSON-файлу источника."""
        settings = get_settings()
        return settings.project_root / settings.data.synthetic_data_dir / self.file_name

    def _load_all(self) -> List[T]:
        """Загружает и валидирует все записи (с кэшированием).

        Raises:
            FileNotFoundError: если файл источника отсутствует.
            pydantic.ValidationError: если запись не соответствует модели.
        """
        if self._cache is None:
            path = self._data_path()
            if not path.is_file():
                raise FileNotFoundError(f"Файл данных не найден: {path}")
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._cache = [self.model_type.model_validate(item) for item in raw]
        return self._cache

    def find_all(self) -> List[T]:
        """Возвращает все записи источника (копия списка)."""
        return list(self._load_all())