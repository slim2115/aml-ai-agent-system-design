"""Проверка guardrails (итерация 7): schema, traceability, evidence, injection."""
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data_access import InvestigationContextBuilder, TransactionRepository
from src.data_access.rule_repository import RuleRepository
from src.retrieval import KnowledgeBase
from src.guardrails import GuardrailPipeline

# Подготовка зависимостей
kb = KnowledgeBase(RuleRepository().find_all())
kb.index()
builder = InvestigationContextBuilder()
pipeline = GuardrailPipeline(kb, TransactionRepository())

# Кейс 1: валидный черновик + чистый контекст (категория A) -> passed
ctx_a = builder.build("INC-000123")
good_draft = {
    "incident_id": "INC-000123",
    "status": "draft",
    "generated_at": "2026-07-21T10:15:00Z",
    "data_completeness": "full",
    "found_facts": [
        {"fact": "12 транзакций по 95000 RUB",
         "evidence_ref": {"tx_id": "TX-7700", "field": "amount"}}
    ],
    "applicable_rules": [
        {"rule_id": "R-115-002", "rule_text": "Контроль структурирования",
         "source_ref": "115-ФЗ, ст.6, п.2"}
    ],
    "confidence": 0.87,
}
r1 = pipeline.run(good_draft, ctx_a)
print("case A passed:", r1.passed, "| checks:", [(c.name, c.passed) for c in r1.checks])

# Кейс 2: черновик с невалидным source_ref (галлюцинация правила) -> failed (traceability)
bad_draft = dict(good_draft)
bad_draft["applicable_rules"] = [
    {"rule_id": "R-FAKE", "rule_text": "x", "source_ref": "999-ФЗ, ст.99"}
]
r2 = pipeline.run(bad_draft, ctx_a)
print("case bad-ref passed:", r2.passed, "| failed:", [c.name for c in r2.failed_checks()])

# Кейс 3: контекст с prompt injection в purpose (TX-9001) -> failed (injection)
ctx_c = builder.build("INC-000307")
r3 = pipeline.run({**good_draft, "incident_id": "INC-000307"}, ctx_c)
print("case injection passed:", r3.passed, "| failed:", [c.name for c in r3.failed_checks()])