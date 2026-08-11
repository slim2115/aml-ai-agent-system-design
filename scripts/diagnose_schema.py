"""Диагностика schema_match: показывает ключевые поля и точную ошибку валидации."""
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

print("=== Генерация (1-2 минуты) ===")
result = gen.generate(ctx)
payload = result.payload

print("\n=== Ключевые поля payload ===")
print("status:", payload.get("status"))
print("found_facts count:", len(payload.get("found_facts", [])))
print("suspicious_patterns count:", len(payload.get("suspicious_patterns", [])))
print("applicable_rules count:", len(payload.get("applicable_rules", [])))
print("confidence:", payload.get("confidence"))

print("\n=== applicable_rules содержимое ===")
print(json.dumps(payload.get("applicable_rules", []), ensure_ascii=False, indent=2))

print("\n=== Валидация Pydantic (точная ошибка) ===")
try:
    draft = validate_draft(payload)
    print("OK:", draft.status)
except ValidationError as e:
    for err in e.errors():
        print(f"поле: {err['loc']}, ошибка: {err['msg']}")