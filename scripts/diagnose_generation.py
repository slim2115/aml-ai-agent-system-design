"""Диагностика генерации: показывает сырой ответ LLM и ошибку валидации."""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data_access import InvestigationContextBuilder
from src.data_access.rule_repository import RuleRepository
from src.retrieval import KnowledgeBase
from src.generation import ReportGenerator
from src.schemas import validate_draft
from pydantic import ValidationError

kb = KnowledgeBase(RuleRepository().find_all())
kb.index()
gen = ReportGenerator(kb)
ctx = InvestigationContextBuilder().build("INC-000123")

# Шаг 1: промпт + RAG + user message
system_prompt = gen._load_prompt(gen._prompt_version)
rules = gen._retrieve_rules(ctx)
user_message = gen._build_user_message(ctx, rules)

# Шаг 2: вызов LLM
print("=== Вызов LLM (может занять 1-2 минуты) ===")
raw = gen._call_llm(system_prompt, user_message)

print("\n=== СЫРОЙ ответ LLM ===")
print(raw)

# Шаг 3: парсинг JSON
print("\n=== Парсинг JSON ===")
try:
    payload = json.loads(raw)
    print("JSON распарсен успешно")
except json.JSONDecodeError as e:
    print(f"ОШИБКА парсинга JSON: {e}")
    sys.exit(1)

# Шаг 4: нормализация
print("\n=== После нормализации ===")
try:
    payload = gen._normalize_payload(payload, ctx.incident_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
except Exception as e:
    print(f"ОШИБКА в нормализаторе: {type(e).__name__}: {e}")

# Шаг 5: валидация Pydantic
print("\n=== Валидация Pydantic ===")
try:
    draft = validate_draft(payload)
    print("✅ Валидация пройдена:", draft.status, draft.incident_id)
except ValidationError as e:
    print("❌ ОШИБКА валидации:")
    for err in e.errors():
        print(f"  - поле: {err['loc']}, ошибка: {err['msg']}, значение: {err.get('input')}")