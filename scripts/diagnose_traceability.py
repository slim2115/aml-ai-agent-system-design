"""Диагностика traceability: показывает payload и деталь проваленных проверок."""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data_access import InvestigationContextBuilder, TransactionRepository
from src.data_access.rule_repository import RuleRepository
from src.retrieval import KnowledgeBase
from src.generation import ReportGenerator
from src.guardrails import GuardrailPipeline

kb = KnowledgeBase(RuleRepository().find_all())
kb.index()
gen = ReportGenerator(kb)
pipeline = GuardrailPipeline(kb, TransactionRepository())
ctx = InvestigationContextBuilder().build("INC-000123")

# Генерация через полный generate() (как в агенте)
print("=== Генерация (1-2 минуты) ===")
result = gen.generate(ctx)
payload = result.payload

print("\n=== Payload после нормализации (только source_ref / rule_id) ===")
for i, fact in enumerate(payload.get("found_facts", [])):
    print(f"found_facts[{i}].source_ref = {fact.get('source_ref')!r}")
for i, pat in enumerate(payload.get("suspicious_patterns", [])):
    print(f"suspicious_patterns[{i}].source_ref = {pat.get('source_ref')!r}")
for i, rule in enumerate(payload.get("applicable_rules", [])):
    print(f"applicable_rules[{i}].rule_id = {rule.get('rule_id')!r}, "
          f"source_ref = {rule.get('source_ref')!r}")

print("\n=== Известные в базе ===")
print("regulation_refs:", sorted(kb.get_known_regulation_refs()))
print("rule_ids:", sorted(r.rule_id for r in RuleRepository().find_all()))

print("\n=== Результат guardrails (с деталями) ===")
report = pipeline.run(payload, ctx)
print("passed:", report.passed)
for c in report.checks:
    mark = "OK " if c.passed else "FAIL"
    print(f"[{mark}] {c.name}: {c.detail}")