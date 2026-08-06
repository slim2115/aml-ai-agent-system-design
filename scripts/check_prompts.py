"""Проверка загрузчика промптов (итерация 3)."""
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.agent.prompt_loader import (
    PromptNotFoundError,
    assert_prompt_integrity,
    list_available_versions,
    load_system_prompt,
)

# 1. Загрузка промпта по умолчанию (v0.2)
p = load_system_prompt()
print("prompt loaded, chars:", len(p))

# 2. Список доступных версий
print("versions:", list_available_versions())

# 3. Проверка целостности (обязательные секции безопасности)
assert_prompt_integrity()
print("integrity OK")

# 4. Обработка отсутствующей версии
try:
    load_system_prompt("9.9")
except PromptNotFoundError as e:
    print("expected error:", type(e).__name__)