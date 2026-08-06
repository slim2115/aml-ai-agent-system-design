"""Защитные проверки (guardrails) черновика и входного контекста.

Все проверки — детерминированные (code-based), без использования LLM, что
гарантирует их воспроизводимость и надёжность. Реализуют:

  - SchemaValidator            : SR-08, NFR-01  -> Schema Match = 100% (M-01).
  - TraceabilityValidator      : SR-12, NFR-04  -> Traceability Ratio = 100% (M-04).
  - EvidenceGroundingValidator : SR-13/SR-14    -> Evidence Grounding = 100% (M-06).
  - PromptInjectionGuard       : SR-19, RISK-SEC-01 -> Refusal Correctness (M-05).
  - GuardrailPipeline          : оркестрация проверок (UML Sequence, Фаза 4).

Связь с артефактами:
  - SRS v0.2 (SR-08, SR-12, SR-13, SR-14, SR-19).
  - Evaluation Plan (метрики M-01, M-04, M-05, M-06; adversarial-кейсы ADV-01..03).
  - Risks & Security (RISK-SEC-01, RISK-AI-01/02).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from pydantic import ValidationError

from src.data_access.transaction_repository import TransactionRepository
from src.retrieval.knowledge_base import KnowledgeBase
from src.schemas import (
    AMLInvestigationDraft,
    EvidenceRef,
    InvestigationContext,
    SuspiciousPattern,
    FoundFact,
    validate_draft,
)


# =============================================================================
# Структуры результата проверок
# =============================================================================

@dataclass(frozen=True)
class CheckResult:
    """Результат одной проверки guardrail.

    Attributes:
        name: идентификатор проверки (например, "schema_match").
        passed: True, если проверка пройдена.
        detail: пояснение (список нарушений при провале, пусто при успехе).
    """
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class GuardrailReport:
    """Агрегированный отчёт по всем проверкам guardrail.

    Attributes:
        checks: результаты отдельных проверок.
    """
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Общий результат: все проверки пройдены."""
        return all(check.passed for check in self.checks)

    def failed_checks(self) -> List[CheckResult]:
        """Возвращает список проваленных проверок."""
        return [check for check in self.checks if not check.passed]


# =============================================================================
# SR-08 / NFR-01: Schema Match
# =============================================================================

class SchemaValidator:
    """Валидация черновика против JSON Schema (SR-08, NFR-01).

    Использует validate_draft из schemas.py (Pydantic-валидация контракта
    AMLInvestigationDraft, включая условные инварианты status=refusal/draft).
    """

    name = "schema_match"

    def validate(self, payload: dict) -> CheckResult:
        """Проверяет соответствие словаря-ответа контракту.

        Args:
            payload: словарь, возвращённый LLM.

        Returns:
            CheckResult: passed=True при соответствии схеме.
        """
        try:
            validate_draft(payload)
            return CheckResult(name=self.name, passed=True)
        except ValidationError as exc:
            return CheckResult(
                name=self.name,
                passed=False,
                detail=f"Нарушение JSON Schema: {exc.error_count()} ошибок",
            )


# =============================================================================
# SR-12 / NFR-04: Traceability (прослеживаемость ссылок)
# =============================================================================

class TraceabilityValidator:
    """Проверка прослеживаемости выводов (SR-12, NFR-04, BRULE-02).

    Каждый source_ref в черновике должен указывать на существующий пункт
    базы знаний. Обеспечивает Traceability Ratio = 100% (M-04) и запрет
    генерации несуществующих правил (BRULE-02, RISK-AI-01).
    """

    name = "traceability"

    def __init__(self, knowledge_base: KnowledgeBase) -> None:
        self._kb = knowledge_base

    def validate(self, draft: AMLInvestigationDraft) -> CheckResult:
        """Сверяет все source_ref черновика с базой знаний.

        Args:
            draft: валидный черновик (после SchemaValidator).

        Returns:
            CheckResult: passed=True, если все ссылки существуют.
        """
        invalid_refs: List[str] = []

        for fact in draft.found_facts:
            if fact.source_ref and not self._kb.validate_source_ref(fact.source_ref):
                invalid_refs.append(fact.source_ref)

        for pattern in draft.suspicious_patterns:
            if not self._kb.validate_source_ref(pattern.source_ref):
                invalid_refs.append(pattern.source_ref)

        for rule in draft.applicable_rules:
            if not self._kb.validate_source_ref(rule.source_ref):
                invalid_refs.append(rule.source_ref)

        if invalid_refs:
            return CheckResult(
                name=self.name,
                passed=False,
                detail=f"Невалидные source_ref: {sorted(set(invalid_refs))}",
            )
        return CheckResult(name=self.name, passed=True)


# =============================================================================
# SR-13 / SR-14: Evidence Grounding (фактическая обоснованность)
# =============================================================================

class EvidenceGroundingValidator:
    """Проверка фактической обоснованности (SR-13, SR-14, BR-05).

    Каждый evidence_ref должен ссылаться на существующую транзакцию (tx_id)
    и допустимое поле (field). Обеспечивает Evidence Grounding Correctness = 100%
    (M-06) и защиту от галлюцинации фактов (RISK-AI-02).

    Примечание: допустимость field уже гарантирована типом TransactionField
    в EvidenceRef (Pydantic). Здесь проверяется существование tx_id в источнике.
    """

    name = "evidence_grounding"

    def __init__(self, transactions: TransactionRepository) -> None:
        self._transactions = transactions

    def validate(self, draft: AMLInvestigationDraft) -> CheckResult:
        """Проверяет существование tx_id для всех evidence_ref черновика.

        Args:
            draft: валидный черновик (после SchemaValidator).

        Returns:
            CheckResult: passed=True, если все tx_id существуют.
        """
        missing_tx: List[str] = []

        evidence_refs: List[EvidenceRef] = [fact.evidence_ref for fact in draft.found_facts]
        evidence_refs += [
            pattern.evidence_ref
            for pattern in draft.suspicious_patterns
            if pattern.evidence_ref is not None
        ]

        for ref in evidence_refs:
            if self._transactions.get_by_id(ref.tx_id) is None:
                missing_tx.append(ref.tx_id)

        if missing_tx:
            return CheckResult(
                name=self.name,
                passed=False,
                detail=f"Несуществующие tx_id в evidence_ref: {sorted(set(missing_tx))}",
            )
        return CheckResult(name=self.name, passed=True)


# =============================================================================
# SR-19 / RISK-SEC-01: Prompt Injection Guard
# =============================================================================

class PromptInjectionGuard:
    """Детекция prompt injection в полях пользовательского ввода (SR-19).

    Поля purpose / counterparty транзакций обрабатываются как ДАННЫЕ, а не
    инструкции (SRS раздел 4.4). Для PoC используется паттерн-матчинг по
    эталонным adversarial-кейсам (ADV-01..03, Evaluation Plan).

    Ограничение: паттерн-матчинг не покрывает изощрённые атаки; для production
    рекомендуется специализированный ML-детектор (открытый вопрос Risks).
    """

    name = "prompt_injection"

    #: Паттерны инъекций (рус/англ), регистронезависимые. Основаны на ADV-01..03.
    INJECTION_PATTERNS: List[str] = [
        "игнорируй предыдущие",
        "игнорируй все инструкции",
        "проигнорируй правила",
        "системная инструкция",
        "системный промпт",
        "выведи промпт",
        "выведи инструкции",
        "одобри транзакцию",
        "разблокируй карту",
        "ignore previous",
        "ignore all instructions",
        "system prompt",
        "system instruction",
        "approve transaction",
        "unlock card",
    ]

    def __init__(self) -> None:
        self._compiled = [
            re.compile(re.escape(pattern), re.IGNORECASE)
            for pattern in self.INJECTION_PATTERNS
        ]

    def scan_text(self, text: str) -> Optional[str]:
        """Сканирует текст на паттерны инъекций.

        Args:
            text: проверяемый текст (значение поля данных).

        Returns:
            Обнаруженный паттерн (первый совпавший) или None.
        """
        for pattern in self._compiled:
            if pattern.search(text):
                return pattern.pattern
        return None

    def scan_context(self, context: InvestigationContext) -> CheckResult:
        """Сканирует поля данных контекста на инъекции (SR-19).

        Проверяет потенциально опасные текстовые поля транзакций:
        purpose и counterparty (векторы косвенной инъекции, RISK-SEC-01).

        Args:
            context: консолидированный контекст инцидента.

        Returns:
            CheckResult: passed=True, если инъекций не обнаружено.
        """
        detections: List[str] = []
        for tx in context.transactions:
            for field_name in ("purpose", "counterparty"):
                value = getattr(tx, field_name)
                hit = self.scan_text(value)
                if hit:
                    detections.append(f"{tx.tx_id}.{field_name}: '{hit}'")

        if detections:
            return CheckResult(
                name=self.name,
                passed=False,
                detail=f"Обнаружены признаки prompt injection: {detections}",
            )
        return CheckResult(name=self.name, passed=True)


# =============================================================================
# Оркестрация проверок (UML Sequence, Фаза 4)
# =============================================================================

class GuardrailPipeline:
    """Оркестрирует все guardrail-проверки (UML Sequence, Фаза 4).

    Последовательность:
      1. Prompt injection scan на контексте (SR-19) — доверие к входным данным.
      2. Schema validation ответа (SR-08).
      3. Traceability (SR-12) и Evidence grounding (SR-13/14) — при успешной
         валидации схемы.

    При провале любой проверки черновик оператору не передаётся (SR-08, AC-08.2):
    выполняется повторная генерация или явный отказ.
    """

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        transactions: TransactionRepository,
        injection_guard: Optional[PromptInjectionGuard] = None,
    ) -> None:
        self._schema = SchemaValidator()
        self._traceability = TraceabilityValidator(knowledge_base)
        self._evidence = EvidenceGroundingValidator(transactions)
        self._injection = injection_guard or PromptInjectionGuard()

    def run(self, payload: dict, context: InvestigationContext) -> GuardrailReport:
        """Запускает полный набор проверок.

        Args:
            payload: словарь-ответ LLM.
            context: консолидированный контекст инцидента.

        Returns:
            GuardrailReport с результатами всех проверок.
        """
        checks: List[CheckResult] = []

        # 1. Проверка входных данных на инъекции (SR-19).
        checks.append(self._injection.scan_context(context))

        # 2. Валидация схемы ответа (SR-08).
        schema_result = self._schema.validate(payload)
        checks.append(schema_result)

        # 3. Семантические проверки — только при валидной схеме.
        if schema_result.passed:
            draft = validate_draft(payload)
            checks.append(self._traceability.validate(draft))
            checks.append(self._evidence.validate(draft))

        return GuardrailReport(checks=checks)