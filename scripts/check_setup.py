"""Проверка корректности установки: schemas, config, валидация контракта."""
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path, чтобы импорты src.* работали
# независимо от того, откуда запускается скрипт.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.schemas import validate_draft

# 1. Проверка конфигурации
s = get_settings()
print("config OK:", s.llm.model_name, s.thresholds)

# 2. Проверка контракта AMLInvestigationDraft (пример из SRS, раздел 5)
draft = validate_draft({
    "incident_id": "INC-000123",
    "status": "draft",
    "generated_at": "2026-07-21T10:15:00Z",
    "data_completeness": "full",
    "found_facts": [{
        "fact": "12 транзакций по 95 000 RUB за 3 дня",
        "evidence_ref": {"tx_id": "TX-7781", "field": "amount"},
    }],
    "applicable_rules": [{
        "rule_id": "R-115-002",
        "rule_text": "Контроль операций с признаками структурирования",
        "source_ref": "115-ФЗ, ст.6, п.2",
    }],
    "confidence": 0.87,
})
print("schemas OK; draft valid:", draft.status, draft.incident_id)