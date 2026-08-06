"""Проверка слоя доступа к данным (итерация 5): контекст, полнота, валидация ID."""
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data_access import InvestigationContextBuilder, InvalidInputError

builder = InvestigationContextBuilder()

# Кейс A: структурирование, есть транзакции -> FULL
ctx = builder.build("INC-000123")
print("A:", ctx.incident_id, ctx.client.client_id,
      "txs=", len(ctx.transactions), ctx.data_completeness)

# Кейс D: нет транзакций -> PARTIAL (триггер отказа)
ctx = builder.build("INC-000999")
print("D:", ctx.incident_id, ctx.client.client_id,
      "txs=", len(ctx.transactions), ctx.data_completeness)

# Невалидный incident_id -> InvalidInputError (SR-01)
try:
    builder.build("BAD-ID")
except InvalidInputError as e:
    print("expected:", type(e).__name__)