"""Проверка слоя RAG-поиска (итерация 6): индексация, поиск, валидация source_ref."""
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data_access.rule_repository import RuleRepository
from src.retrieval import KnowledgeBase

rules = RuleRepository().find_all()
kb = KnowledgeBase(rules)
n = kb.index()
print("indexed chunks:", n)

# Семантический поиск (SR-07): запрос про структурирование
results = kb.search("разбиение суммы на операции ниже порога контроля", k=2)
for r in results:
    print(" -", r.rule_id, "|", r.regulation_ref, "| dist=", round(r.distance, 3))

# Валидация source_ref (SR-12)
print("valid ref  :", kb.validate_source_ref("115-ФЗ, ст.6, п.2"))
print("invalid ref:", kb.validate_source_ref("999-ФЗ, ст.99, п.1"))