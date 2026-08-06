"""Схемы данных и контракты AML / Anti-Fraud Copilot.

Модуль реализует два слоя моделей (Pydantic v2):

1. Модели данных (Data Model v0.1, раздел 3):
   Client, Card, Transaction, Case, Rule.
   Используются для типизации read-only доступа (SR-02, SR-03) и консолидации
   контекста (SR-04).

2. Контракт ответа агента (SRS v0.2, раздел 5 — JSON Schema AMLInvestigationDraft):
   EvidenceRef (SR-14), FoundFact, SuspiciousPattern, ApplicableRule,
   AMLInvestigationDraft. Используются для валидации Schema Match = 100%
   (SR-08, NFR-01) и трассировки выводов (SR-11, BR-04/BR-05).

Все условные требования JSON Schema (allOf) воспроизведены через model_validator.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# =============================================================================
# Перечисления (Data Model v0.1)
# =============================================================================

class RiskCategory(str, Enum):
    """Категория риска клиента (Data Model, Client.risk_category)."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class KycStatus(str, Enum):
    """Статус процедуры KYC (Data Model, Client.kyc_status)."""
    VERIFIED = "verified"
    PENDING = "pending"
    EXPIRED = "expired"


class CardStatus(str, Enum):
    """Статус карты (Data Model, Card.card_status)."""
    ACTIVE = "active"
    BLOCKED = "blocked"
    CLOSED = "closed"


class TransactionChannel(str, Enum):
    """Канал операции (Data Model, Transaction.channel)."""
    ATM = "atm"
    POS = "pos"
    ONLINE = "online"
    TRANSFER = "transfer"


class TransactionStatus(str, Enum):
    """Статус транзакции (Data Model, Transaction.status)."""
    COMPLETED = "completed"
    PENDING = "pending"
    DECLINED = "declined"


class TransactionField(str, Enum):
    """Допустимые значения evidence_ref.field (Data Model, раздел 5.2; SR-14).

    Фиксированный перечень полей транзакции, на которые может ссылаться
    утверждение отчёта. Используется для валидации grounding (M-06).
    """
    AMOUNT = "amount"
    CURRENCY = "currency"
    COUNTERPARTY = "counterparty"
    PURPOSE = "purpose"
    CHANNEL = "channel"
    STATUS = "status"
    TIMESTAMP = "timestamp"


class CaseStatus(str, Enum):
    """Статус кейса (Data Model, Case.status).

    PENDING_MANUAL — результат явного отказа агента (SR-21, BRULE-03).
    """
    NEW = "new"
    IN_PROGRESS = "in_progress"
    PENDING_MANUAL = "pending_manual"
    CLOSED = "closed"
    ESCALATED = "escalated"


class AlertType(str, Enum):
    """Тип сработавшего алерта (Data Model, Case.alert_type)."""
    STRUCTURING = "structuring"
    HIGH_RISK_COUNTRY = "high_risk_country"
    VELOCITY = "velocity"
    THRESHOLD_BREACH = "threshold_breach"
    KYC_MISMATCH = "kyc_mismatch"


class RuleCategory(str, Enum):
    """Категория регуляторного правила (Data Model, Rule.category)."""
    STRUCTURING = "structuring"
    KYC = "kyc"
    THRESHOLD = "threshold"
    HIGH_RISK = "high_risk"
    REPORTING = "reporting"


class DraftStatus(str, Enum):
    """Статус черновика отчёта (SRS раздел 5, AMLInvestigationDraft.status)."""
    DRAFT = "draft"
    REFUSAL = "refusal"


class DataCompleteness(str, Enum):
    """Полнота данных (SRS раздел 5; SR-20).

    INSUFFICIENT — триггер для явного отказа (BRULE-03).
    """
    FULL = "full"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


# =============================================================================
# Модели данных (Data Model v0.1, раздел 3)
# =============================================================================

class Client(BaseModel):
    """Профиль клиента (Data Model, раздел 3.1; SR-02).

    Поля full_name и inn являются PII и маскируются в логах (NFR-09, 152-ФЗ).
    """
    model_config = ConfigDict(frozen=True)

    client_id: str = Field(..., description="Уникальный идентификатор клиента, напр. C-00045")
    full_name: str = Field(..., description="ФИО клиента (PII, маскируется)")
    risk_category: RiskCategory
    kyc_status: KycStatus
    client_since: date
    inn: Optional[str] = Field(None, description="ИНН клиента (PII, маскируется)")


class Card(BaseModel):
    """Банковская карта (Data Model, раздел 3.2).

    Полный PAN не хранится в PoC-контуре — только маскированный номер.
    """
    model_config = ConfigDict(frozen=True)

    card_id: str
    client_id: str = Field(..., description="FK -> Client")
    pan_masked: str = Field(..., description="Маскированный PAN, напр. **** **** **** 7781")
    card_status: CardStatus
    issue_date: date


class Transaction(BaseModel):
    """Транзакция (Data Model, раздел 3.3; SR-03).

    Источник данных для evidence_ref (SR-14). Поле purpose — потенциальный
    вектор prompt injection (SR-19): обрабатывается как данные, не как инструкция.
    """
    model_config = ConfigDict(frozen=True)

    tx_id: str = Field(..., description="Уникальный идентификатор транзакции, напр. TX-7781")
    client_id: str = Field(..., description="FK -> Client")
    card_id: Optional[str] = Field(None, description="FK -> Card")
    amount: Decimal
    currency: str = Field(..., description="ISO 4217, напр. RUB")
    counterparty: str
    purpose: str = Field(..., description="Назначение платежа (injection-risk, SR-19)")
    channel: TransactionChannel
    status: TransactionStatus
    timestamp: datetime


class Case(BaseModel):
    """Кейс / инцидент (Data Model, раздел 3.4; SR-01, SR-22)."""
    model_config = ConfigDict(frozen=True)

    case_id: str
    incident_id: str = Field(..., pattern=r"^INC-\d{6}$", description="Внешний ID инцидента (SR-01)")
    client_id: str = Field(..., description="FK -> Client")
    alert_type: AlertType
    status: CaseStatus
    created_at: datetime
    closed_at: Optional[datetime] = None
    assigned_operator: Optional[str] = None


class Rule(BaseModel):
    """Правило базы знаний комплаенса (Data Model, раздел 3.5; SR-07, SR-11).

    Источник для source_ref и RAG-поиска. regulation_ref используется
    для валидации трассируемости (M-04, Traceability Ratio = 100%).
    """
    model_config = ConfigDict(frozen=True)

    rule_id: str = Field(..., description="Идентификатор правила, напр. R-115-002")
    title: str
    regulation_ref: str = Field(..., description="Ссылка на НПА, напр. 115-ФЗ, ст.6, п.2")
    category: RuleCategory
    text: str
    effective_from: date
    version: str

# =============================================================================
# Консолидированный контекст расследования (SR-04)
# =============================================================================

class InvestigationContext(BaseModel):
    """Единый контекст инцидента для передачи в LLM (SR-04).

    Собирается InvestigationContextBuilder из данных кейса, клиента,
    карт и транзакций. Поле data_completeness определяет применимость
    политики явного отказа (SR-20, BRULE-03).
    """
    model_config = ConfigDict(frozen=True)

    incident_id: str = Field(..., pattern=r"^INC-\d{6}$")
    case: Case
    client: Client
    cards: List[Card] = Field(default_factory=list)
    transactions: List[Transaction] = Field(default_factory=list)
    data_completeness: DataCompleteness = DataCompleteness.FULL

# =============================================================================
# Контракт ответа агента (SRS v0.2, раздел 5 — JSON Schema)
# =============================================================================

class EvidenceRef(BaseModel):
    """Ссылка на доказательство (SRS раздел 5; SR-13, SR-14; BR-05).

    Обеспечивает фактическую обоснованность (grounding): привязку утверждения
    к конкретной транзакции и её реквизиту.
    """
    model_config = ConfigDict(frozen=True)

    tx_id: str = Field(..., description="Идентификатор транзакции-первоисточника (FK -> Transaction)")
    field: TransactionField = Field(..., description="Имя поля транзакции (фиксированный перечень, SR-14)")


class FoundFact(BaseModel):
    """Найденный факт (SRS раздел 5, found_facts[]; SR-05).

    source_ref опционален: факт может не иметь нормативного обоснования,
    но всегда должен иметь фактическое (evidence_ref).
    """
    model_config = ConfigDict(frozen=True)

    fact: str
    evidence_ref: EvidenceRef
    source_ref: Optional[str] = Field(
        None, description="Ссылка на абзац/статью регламента (BR-04), если применимо"
    )


class SuspiciousPattern(BaseModel):
    """Подозрительный паттерн (SRS раздел 5, suspicious_patterns[]; SR-06).

    source_ref обязателен: паттерн всегда обосновывается правилом (BRULE-02).
    """
    model_config = ConfigDict(frozen=True)

    pattern: str
    source_ref: str = Field(..., description="Ссылка на пункт регламента (обязательно)")
    evidence_ref: Optional[EvidenceRef] = None


class ApplicableRule(BaseModel):
    """Применимое правило (SRS раздел 5, applicable_rules[]; SR-07)."""
    model_config = ConfigDict(frozen=True)

    rule_id: str
    rule_text: str
    source_ref: str = Field(..., description="Ссылка на пункт регламента (обязательно)")


class AMLInvestigationDraft(BaseModel):
    """Черновик расследования (SRS v0.2, раздел 5 — JSON Schema AMLInvestigationDraft).

    Контракт ответа агента. Валидируется перед передачей оператору (SR-08).
    Условные требования JSON Schema (allOf) воспроизведены в model_validator:
      - status=refusal  -> обязателен refusal_reason;
      - status=draft    -> обязательны found_facts и applicable_rules.
    """
    model_config = ConfigDict(frozen=True)

    incident_id: str = Field(..., pattern=r"^INC-\d{6}$")
    status: DraftStatus
    generated_at: datetime
    data_completeness: DataCompleteness = DataCompleteness.FULL

    found_facts: List[FoundFact] = Field(default_factory=list)
    suspicious_patterns: List[SuspiciousPattern] = Field(default_factory=list)
    applicable_rules: List[ApplicableRule] = Field(default_factory=list)

    refusal_reason: Optional[str] = None
    reasoning_trace: List[str] = Field(
        default_factory=list, description="Цепочка рассуждений для аудита (NFR-11)"
    )
    confidence: float = Field(..., ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_status_invariants(self) -> "AMLInvestigationDraft":
        """Проверка условных требований JSON Schema (SRS раздел 5, allOf)."""
        if self.status is DraftStatus.REFUSAL and not self.refusal_reason:
            raise ValueError("status=refusal требует непустой refusal_reason")
        if self.status is DraftStatus.DRAFT:
            if not self.found_facts:
                raise ValueError("status=draft требует непустой found_facts")
            if not self.applicable_rules:
                raise ValueError("status=draft требует непустой applicable_rules")
        return self


# =============================================================================
# Валидация контракта (SR-08, NFR-01 Schema Match)
# =============================================================================

def validate_draft(payload: dict) -> AMLInvestigationDraft:
    """Валидирует словарь-ответ агента против контракта AMLInvestigationDraft.

    Используется как код-бейзд проверка Schema Match (M-01, NFR-01).
    При несоответствии выбрасывает pydantic.ValidationError — черновик
    оператору не передаётся (SR-08, AC-08.2).

    Args:
        payload: словарь, возвращённый LLM (ожидается строгий JSON).

    Returns:
        Валидная модель AMLInvestigationDraft.

    Raises:
        pydantic.ValidationError: при нарушении схемы или условных инвариантов.
    """
    return AMLInvestigationDraft.model_validate(payload)