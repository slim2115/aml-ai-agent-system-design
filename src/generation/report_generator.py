"""Генератор черновика отчёта через локальную LLM (SR-05..SR-07; ADR-0003).

Оркестрирует готовые слои:
  - системный промпт (prompt_loader, SRS раздел 4);
  - сериализацию InvestigationContext с маскировкой PII (SR-04, NFR-09);
  - RAG-поиск применимых правил (KnowledgeBase, SR-07, ADR-0004);
  - вызов локальной LLM через Ollama REST API (ADR-0005, on-premise);
  - возврат строгого JSON через format="json" (ADR-0003).

Возвращает сырой payload (dict) для последующей проверки guardrails
(SR-08, SR-12, SR-13/14, SR-19). Валидация и отказ — ответственность
вышестоящего слоя (GuardrailPipeline), а не генератора.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from src.config import get_settings
from src.retrieval.knowledge_base import KnowledgeBase, RetrievalResult
from src.schemas import InvestigationContext, Transaction


@dataclass(frozen=True)
class GenerationResult:
    """Результат генерации черновика.

    Attributes:
        payload: распарсованный JSON-ответ LLM (для guardrails).
        raw_response: исходный текст ответа (для отладки/аудита).
        retrieved_rules: правила, извлечённые через RAG (для трассировки).
    """
    payload: dict
    raw_response: str
    retrieved_rules: List[RetrievalResult] = field(default_factory=list)


class OllamaUnavailableError(RuntimeError):
    """Ollama-сервис недоступен или вернул ошибку."""


class ReportGenerator:
    """Генератор черновика отчёта (SR-05..SR-07)."""

    #: Число правил, извлекаемых через RAG для контекста.
    RAG_TOP_K = 3

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        *,
        prompt_version: str | None = None,
    ) -> None:
        # Ленивый импорт разрывает цикл src.agent -> generation -> src.agent.prompt_loader.
        from src.agent.prompt_loader import DEFAULT_PROMPT_VERSION, load_system_prompt

        settings = get_settings()
        self._kb = knowledge_base
        self._prompt_version = prompt_version or DEFAULT_PROMPT_VERSION
        self._load_prompt = load_system_prompt
        self._base_url = settings.llm.base_url.rstrip("/")
        self._model = settings.llm.model_name
        self._timeout = settings.performance.generation_timeout_sec    

    # -------------------------------------------------------------------------
    # Сериализация контекста (SR-04) с маскировкой PII (NFR-09, 152-ФЗ)
    # -------------------------------------------------------------------------

    @staticmethod
    def _serialize_transaction(tx: Transaction) -> str:
        """Сериализует транзакцию в строку для контекста LLM."""
        return (
            f"- {tx.tx_id}: {tx.amount} {tx.currency}, "
            f"контрагент={tx.counterparty}, назначение={tx.purpose}, "
            f"канал={tx.channel.value}, статус={tx.status.value}, "
            f"время={tx.timestamp.isoformat()}"
        )

    def _serialize_context(self, context: InvestigationContext) -> str:
        """Сериализует контекст инцидента в текст для user-сообщения.

        PII (full_name, inn) намеренно НЕ включаются: для анализа паттернов
        они не нужны, а их отсутствие снижает риск утечки (NFR-09, 152-ФЗ,
        минимизация данных в LLM-контур).
        """
        client = context.client
        case = context.case

        lines: List[str] = []
        lines.append(f"Инцидент: {context.incident_id}")
        lines.append(f"Тип алерта: {case.alert_type.value}")
        lines.append(f"Полнота данных: {context.data_completeness.value}")
        lines.append("")
        lines.append("Профиль клиента:")
        lines.append(f"- client_id: {client.client_id}")
        lines.append(f"- категория риска: {client.risk_category.value}")
        lines.append(f"- статус KYC: {client.kyc_status.value}")
        lines.append(f"- клиент с: {client.client_since.isoformat()}")
        lines.append("")
        lines.append(f"Транзакции ({len(context.transactions)}):")
        if context.transactions:
            lines.extend(
                self._serialize_transaction(tx) for tx in context.transactions
            )
        else:
            lines.append("- транзакции отсутствуют")

        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # RAG-поиск применимых правил (SR-07)
    # -------------------------------------------------------------------------

    def _retrieve_rules(self, context: InvestigationContext) -> List[RetrievalResult]:
        """Извлекает применимые правила через RAG по типу алерта.

        Запрос формируется по типу алерта кейса — это определяет категорию
        применимых правил (structuring, threshold, kyc, high_risk).
        """
        query = f"{context.case.alert_type.value} {context.case.alert_type.name}"
        return self._kb.search(query, k=self.RAG_TOP_K)

    @staticmethod
    def _serialize_rules(rules: List[RetrievalResult]) -> str:
        """Сериализует извлечённые правила в текст для user-сообщения."""
        if not rules:
            return "Применимые правила не найдены."
        lines = ["УТВЕРЖДЁННАЯ БАЗА ЗНАНИЙ (используй только эти правила):"]
        for rule in rules:
            lines.append(
                f"- [{rule.regulation_ref}] {rule.section}: {rule.text}"
            )
        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # Формирование сообщения и вызов LLM
    # -------------------------------------------------------------------------

    def _build_user_message(
        self,
        context: InvestigationContext,
        rules: List[RetrievalResult],
    ) -> str:
        """Формирует user-сообщение: данные инцидента + правила + инструкция."""
        return (
            "Проанализируй инцидент и сформируй черновик отчёта строго в формате JSON "
            "согласно схеме AMLInvestigationDraft.\n\n"
            "=== ДАННЫЕ ИНЦИДЕНТА ===\n"
            f"{self._serialize_context(context)}\n\n"
            "=== НОРМАТИВНАЯ БАЗА ===\n"
            f"{self._serialize_rules(rules)}\n\n"
            "=== ИНСТРУКЦИЯ ===\n"
            f"incident_id для ответа: {context.incident_id}\n"
            "Каждый вывод снабди source_ref (ссылка на пункт регламента из базы выше) "
            "и evidence_ref (tx_id + field транзакции). "
            "Если данных недостаточно — верни status=refusal с refusal_reason. "
            "Верни только JSON, без пояснений."
        )

    def _call_llm(self, system_prompt: str, user_message: str) -> str:
        """Вызывает локальную LLM через Ollama REST API (ADR-0005).

        Использует endpoint /api/chat с format="json" для гарантированного
        structured output (ADR-0003).

        Raises:
            OllamaUnavailableError: при недоступности сервиса или ошибке HTTP.
            requests.Timeout: при превышении таймаута генерации (NFR-07).
        """
        url = f"{self._base_url}/api/chat"
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "format": "json",
        }
        try:
            response = requests.post(url, json=payload, timeout=self._timeout)
            response.raise_for_status()
        except requests.ConnectionError as exc:
            raise OllamaUnavailableError(
                f"Ollama недоступен по адресу {self._base_url}. "
                "Проверьте, запущен ли сервис (ollama list)."
            ) from exc
        except requests.HTTPError as exc:
            raise OllamaUnavailableError(
                f"Ollama вернул ошибку HTTP: {exc}"
            ) from exc

        return response.json()["message"]["content"]

    # -------------------------------------------------------------------------
    # Публичный интерфейс
    # -------------------------------------------------------------------------

    def generate(self, context: InvestigationContext) -> GenerationResult:
        """Генерирует черновик отчёта для контекста инцидента.

        Последовательность (UML Sequence, Фаза 3):
          1. Загрузка системного промпта (SRS раздел 4).
          2. RAG-поиск применимых правил (SR-07).
          3. Формирование user-сообщения (контекст + правила).
          4. Вызов LLM и парсинг JSON-ответа (ADR-0003).

        Args:
            context: консолидированный контекст инцидента (SR-04).

        Returns:
            GenerationResult с сырым payload для guardrails.

        Raises:
            OllamaUnavailableError: если LLM-сервис недоступен.
            json.JSONDecodeError: если ответ не является валидным JSON
                (маловероятно при format="json").
        """
        system_prompt = self._load_prompt(self._prompt_version)
        rules = self._retrieve_rules(context)
        user_message = self._build_user_message(context, rules)

        raw_response = self._call_llm(system_prompt, user_message)
        payload = json.loads(raw_response)
        payload = self._normalize_payload(payload, context.incident_id)
        
        return GenerationResult(
            payload=payload,
            raw_response=raw_response,
            retrieved_rules=rules,
        )
    
    def _normalize_payload(self, payload: dict, incident_id: str) -> dict:
        """Очищает типичные артефакты слабой LLM.

        - скобки в source_ref;
        - фиктивная generated_at (подставляется реальная).
        """
        from datetime import datetime, timezone

        payload["incident_id"] = incident_id
        payload["generated_at"] = datetime.now(timezone.utc).isoformat()

        def _strip_brackets(s):
            return s.strip().strip("[]") if isinstance(s, str) else s

        for fact in payload.get("found_facts", []):
            if fact.get("source_ref"):
                fact["source_ref"] = _strip_brackets(fact["source_ref"])

        for pattern in payload.get("suspicious_patterns", []):
            if pattern.get("source_ref"):
                pattern["source_ref"] = _strip_brackets(pattern["source_ref"])

        for rule in payload.get("applicable_rules", []):
            if rule.get("source_ref"):
                rule["source_ref"] = _strip_brackets(rule["source_ref"])
        return payload