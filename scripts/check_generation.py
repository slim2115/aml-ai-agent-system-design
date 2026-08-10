"""Проверка генератора отчётов (итерация 8): вызов LLM через Ollama.

ТРЕБОВАНИЕ: запущенная Ollama с моделью llama3.1 (или моделью из .env).
Если Ollama недоступна, вы увидите OllamaUnavailableError.
"""
import json
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data_access import InvestigationContextBuilder
from src.data_access.rule_repository import RuleRepository
from src.retrieval import KnowledgeBase
from src.generation import ReportGenerator

# Подготовка зависимостей
print("Инициализация базы знаний (первый запуск может скачивать embedding-модель)...")
kb = KnowledgeBase(RuleRepository().find_all())
kb.index()

gen = ReportGenerator(kb)
ctx = InvestigationContextBuilder().build("INC-000123")

print(f"\nКонтекст собран: {ctx.incident_id}, транзакций={len(ctx.transactions)}")
print("Генерация черновика (вызов Ollama, может занять 1-2 минуты)...\n")

try:
    result = gen.generate(ctx)
    print(json.dumps(result.payload, ensure_ascii=False, indent=2))
except Exception as exc:
    print(f"ОШИБКА: {type(exc).__name__}: {exc}")
    print("\nУбедитесь, что:")
    print("  1. Ollama запущена (ollama list)")
    print("  2. Модель скачана (ollama pull llama3.1)")
    print("  3. В .env указано LLM_MODEL_NAME=llama3.1")