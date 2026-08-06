"""Проверка синтетических данных через Pydantic-модели (итерация 4)."""
import json
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.schemas import Client, Rule, Transaction

data_dir = PROJECT_ROOT / "data" / "synthetic"

clients = [
    Client.model_validate(c)
    for c in json.load(open(data_dir / "clients.json", encoding="utf-8"))
]
txs = [
    Transaction.model_validate(t)
    for t in json.load(open(data_dir / "transactions.json", encoding="utf-8"))
]
rules = [
    Rule.model_validate(r)
    for r in json.load(open(data_dir / "rules.json", encoding="utf-8"))
]

print("validated:", len(clients), "clients,", len(txs), "txs,", len(rules), "rules")