"""Загрузчик версионируемых системных промптов.

Промпт является требованием и версионируется (QA.md P0; SRS v0.2, раздел 4).
Файлы промптов хранятся в ``prompts/system_prompt_v{version}.md`` и загружаются
исключительно через этот модуль — хардкод промптов в коде запрещён.

Связь с артефактами:
- SRS v0.2, раздел 4: спецификация системного промпта.
- ADR-0003: structured output (формат вывода описан в промпте).
- SR-19 / RISK-SEC-01: защита от prompt injection (раздел промпта).

Используется ``string.Template`` (синтаксис $var) вместо ``str.format`` намеренно:
промпт содержит фигурные скобки (описание JSON), которые ``str.format`` ошибочно
трактует как плейсхолдеры. ``string.Template`` к ним нечувствителен.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Dict, List, Optional

from src.config import get_settings

# Имя директории с промптами относительно корня проекта.
PROMPTS_DIR_NAME = "prompts"

# Шаблон имени файла промпта: system_prompt_v{version}.md
PROMPT_FILE_TEMPLATE = "system_prompt_v{version}.md"

# Версия промпта по умолчанию.
DEFAULT_PROMPT_VERSION = "0.5"

# Регулярное выражение для извлечения версии из имени файла.
_VERSION_PATTERN = re.compile(r"^system_prompt_v(?P<version>\d+\.\d+)\.md$")


class PromptNotFoundError(FileNotFoundError):
    """Выбрасывается, когда файл промпта запрошенной версии не найден."""


class EmptyPromptError(ValueError):
    """Выбрасывается, когда файл промпта пуст или содержит только пробелы."""


def _resolve_prompts_dir() -> Path:
    """Возвращает абсолютный путь к директории с промптами.

    Директория располагается в корне проекта (рядом с docs/, src/).
    Путь берётся из конфигурации (project_root), что обеспечивает
    работоспособность независимо от текущей рабочей директории.

    Returns:
        Path: абсолютный путь к директории prompts/.
    """
    settings = get_settings()
    return settings.project_root / PROMPTS_DIR_NAME


def get_prompt_path(version: str = DEFAULT_PROMPT_VERSION) -> Path:
    """Строит путь к файлу промпта заданной версии.

    Args:
        version: версия промпта в формате "MAJOR.MINOR" (например, "0.2").

    Returns:
        Path: путь к файлу system_prompt_v{version}.md.
    """
    filename = PROMPT_FILE_TEMPLATE.format(version=version)
    return _resolve_prompts_dir() / filename


def load_system_prompt(
    version: str = DEFAULT_PROMPT_VERSION,
    *,
    variables: Optional[Dict[str, str]] = None,
) -> str:
    """Загружает системный промпт заданной версии.

    Читает файл промпта, валидирует его непустоту и опционально подставляет
    переменные через string.Template (безопасно для фигурных скобок JSON).

    Args:
        version: версия промпта (по умолчанию DEFAULT_PROMPT_VERSION).
        variables: словарь переменных для подстановки ($key). Если None,
            промпт возвращается как есть.

    Returns:
        str: текст системного промпта, готовый к передаче в LLM.

    Raises:
        PromptNotFoundError: если файл промпта не существует.
        EmptyPromptError: если файл пуст или содержит только пробелы.
    """
    path = get_prompt_path(version)
    if not path.is_file():
        raise PromptNotFoundError(
            f"Файл промпта не найден: {path}. "
            f"Доступные версии: {list_available_versions()}"
        )

    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise EmptyPromptError(f"Файл промпта пуст: {path}")

    if variables:
        content = Template(content).safe_substitute(variables)

    return content


@lru_cache(maxsize=1)
def list_available_versions() -> List[str]:
    """Возвращает список доступных версий промптов (отсортирован).

    Сканирует директорию prompts/ и извлекает версии из имён файлов
    вида system_prompt_v{version}.md.

    Returns:
        List[str]: список версий, например ["0.1", "0.2"].
    """
    prompts_dir = _resolve_prompts_dir()
    if not prompts_dir.is_dir():
        return []

    versions: List[str] = []
    for file_path in prompts_dir.iterdir():
        match = _VERSION_PATTERN.match(file_path.name)
        if match:
            versions.append(match.group("version"))
    return sorted(versions)


def assert_prompt_integrity(version: str = DEFAULT_PROMPT_VERSION) -> None:
    """Проверяет целостность промпта: наличие обязательных секций.

    Легковесный guardrail: гарантирует, что критичные ограничения не были
    случайно удалены из промпта при редактировании. Проверяет наличие
    ключевых маркеров (READ-ONLY, Refusal Policy, prompt injection).

    Args:
        version: версия промпта для проверки.

    Raises:
        PromptNotFoundError: если файл не найден.
        ValueError: если отсутствует хотя бы одна обязательная секция.
    """
    prompt = load_system_prompt(version)
    required_markers = (
        "READ-ONLY",
        "ONLY APPROVED SOURCES",
        "NO HALLUCINATION",
        "CITATION MANDATORY",
        "Refusal Policy",
        "prompt injection",
    )
    missing = [marker for marker in required_markers if marker not in prompt]
    if missing:
        raise ValueError(
            f"Нарушена целостность промпта v{version}: "
            f"отсутствуют обязательные секции {missing}"
        )