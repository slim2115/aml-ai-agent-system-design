"""Диагностика маршрутизации: какой путь выбирает _route_after_context."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.agent import AMLAgent

agent = AMLAgent()

# Имитируем начальное состояние
state = {
    "incident_id": "INC-000123",
    "reasoning_trace": [],
    "generation_attempts": 0,
}

# Шаг 1: выполняем build_context
result = agent._node_build_context(state)
print("=== После build_context ===")
print("status:", result.get("status"))
print("context is None:", result.get("context") is None)
if result.get("context"):
    ctx = result["context"]
    print("data_completeness:", ctx.data_completeness)
    print("data_completeness type:", type(ctx.data_completeness))

# Шаг 2: проверяем маршрутизацию
print("\n=== Маршрутизация ===")
route = agent._route_after_context(result)
print("_route_after_context вернул:", repr(route))