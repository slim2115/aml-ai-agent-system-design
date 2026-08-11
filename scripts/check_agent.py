"""Проверка полного цикла агента (итерация 9): LangGraph-оркестрация.

ТРЕБОВАНИЕ: запущенная Ollama с моделью qwen2.5:14b (или моделью из .env).
"""
import json
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.agent import AMLAgent

agent = AMLAgent()

# Кейс D: нет транзакций -> refusal (BRULE-03)
print("=== Кейс INC-000999 (нет транзакций, ожидаемый отказ) ===")
result = agent.run("INC-000999")
print(json.dumps(result, ensure_ascii=False, indent=2))

# Невалидный incident_id -> error (SR-01)
print("\n=== Невалидный incident_id BAD-ID (ожидаемая ошибка) ===")
result = agent.run("BAD-ID")
print("status:", result.get("status"))
print("error:", result.get("error"))