"""Оркестрация агента AML / Anti-Fraud Copilot на LangGraph (ADR-0001).

Связывает функциональные слои в единый pipeline по UML Sequence (Фазы 1-7):

  build_context      -> Фаза 1: агрегация данных (SR-04, data_access)
  [route]            -> Фаза 2: проверка полноты (SR-20, BRULE-03)
  generate           -> Фаза 3: RAG + генерация (SR-05..07, retrieval + generation)
  run_guardrails     -> Фаза 4: защитные проверки (SR-08/12/13/14/19, guardrails)
  [route]            -> решение: draft или refusal
  finalize_draft     -> Фаза 5: возврат черновика
  finalize_refusal   -> Фаза 2/4: явный отказ (BRULE-03)

Использован стабильный core API LangGraph (StateGraph, START, END,
conditional edges). Human-in-the-Loop (Фаза 6) и автоэкспорт (Фаза 7)
выполняются вне агента — агент read-only и не меняет статусы кейсов (BR-07).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from src.data_access import InvestigationContextBuilder
from src.data_access.exceptions import (
    EntityNotFoundError,
    InvalidInputError,
)
from src.data_access.rule_repository import RuleRepository
from src.data_access.transaction_repository import TransactionRepository
from src.generation import ReportGenerator
from src.guardrails import GuardrailPipeline
from src.retrieval import KnowledgeBase
from src.schemas import DataCompleteness, InvestigationContext


# =============================================================================
# Состояние графа
# =============================================================================

class AgentState(TypedDict, total=False):
    """Состояние агента, передаваемое между узлами графа.

    Поля (total=False — все опциональны, заполняются по ходу выполнения):
        incident_id: входной идентификатор инцидента.
        context: консолидированный контекст (SR-04).
        payload: сырой JSON-ответ LLM (для guardrails).
        guardrail_passed: результат защитных проверок.
        final_draft: итоговый ответ (draft / refusal / error).
        status: текущий статус обработки.
        error: текст ошибки (при наличии).
        reasoning_trace: цепочка рассуждений для аудита (NFR-11).
    """
    incident_id: str
    context: Optional[InvestigationContext]
    payload: Optional[dict]
    guardrail_passed: bool
    final_draft: Optional[dict]
    status: str
    error: Optional[str]
    reasoning_trace: List[str]
    generation_attempts: int
    failed_guards: List[str]

# =============================================================================
# Агент
# =============================================================================

class AMLAgent:
    """Агент первичного расследования AML (LangGraph, ADR-0001).

    Агент функционирует строго в режиме read-only (BR-07): результат работы —
    черновик отчёта (draft) или явный отказ (refusal). Изменение статусов кейсов
    и автоэкспорт выполняются внешней системой после HITL-одобрения (SR-22).
    """
    
    MAX_GENERATION_ATTEMPTS = 3

    def __init__(self) -> None:
        """Инициализирует зависимости и компилирует граф."""
        self._kb = KnowledgeBase(RuleRepository().find_all())
        self._kb.index()
        self._builder = InvestigationContextBuilder()
        self._generator = ReportGenerator(self._kb)
        self._guardrails = GuardrailPipeline(self._kb, TransactionRepository())
        self._graph = self._build_graph()

    # -------------------------------------------------------------------------
    # Построение графа
    # -------------------------------------------------------------------------

    def _build_graph(self):
        """Строит и компилирует граф состояний LangGraph."""
        graph = StateGraph(AgentState)

        graph.add_node("build_context", self._node_build_context)
        graph.add_node("generate", self._node_generate)
        graph.add_node("run_guardrails", self._node_run_guardrails)
        graph.add_node("finalize_draft", self._node_finalize_draft)
        graph.add_node("finalize_refusal", self._node_finalize_refusal)

        graph.add_edge(START, "build_context")

        # После сборки контекста: generate (полные данные) или refusal (неполные/ошибка)
        graph.add_conditional_edges(
            "build_context",
            self._route_after_context,
            {"generate": "generate", "refusal": "finalize_refusal"},
        )

        graph.add_edge("generate", "run_guardrails")

        # После guardrails: draft (успех), refusal (провал/лимит), retry (повтор генерации)
        graph.add_conditional_edges(
            "run_guardrails",
            self._route_after_guardrails,
            {
                "draft": "finalize_draft",
                "refusal": "finalize_refusal",
                "retry": "generate",
            },
        )

        graph.add_edge("finalize_draft", END)
        graph.add_edge("finalize_refusal", END)

        return graph.compile()

    # -------------------------------------------------------------------------
    # Узлы графа
    # -------------------------------------------------------------------------

    def _node_build_context(self, state: AgentState) -> dict:
        """Фаза 1: агрегация данных инцидента (SR-04)."""
        incident_id = state["incident_id"]
        trace = list(state.get("reasoning_trace", []))
        try:
            context = self._builder.build(incident_id)
            trace.append(
                f"Контекст собран: {incident_id}, "
                f"транзакций={len(context.transactions)}, "
                f"полнота={context.data_completeness.value}"
            )
            return {
                "context": context,
                "status": "processing",
                "reasoning_trace": trace,
            }
        except (InvalidInputError, EntityNotFoundError) as exc:
            trace.append(f"Ошибка сбора контекста: {exc}")
            return {
                "context": None,
                "error": str(exc),
                "status": "error",
                "reasoning_trace": trace,
            }

    def _node_generate(self, state: AgentState) -> dict:
        """Фаза 3: RAG-поиск правил и генерация черновика (SR-05..07).

        При повторной генерации (retry) инкрементирует счётчик попыток.
        """
        context = state["context"]
        trace = list(state.get("reasoning_trace", []))
        attempts = state.get("generation_attempts", 0) + 1

        try:
            result = self._generator.generate(context)
            trace.append(
                f"Черновик сгенерирован (попытка {attempts}/"
                f"{self.MAX_GENERATION_ATTEMPTS}); "
                f"извлечено правил: {len(result.retrieved_rules)}"
            )
            return {
                "payload": result.payload,
                "generation_attempts": attempts,
                "reasoning_trace": trace,
            }
        except Exception as exc:  # noqa: BLE001
            trace.append(f"Ошибка генерации (попытка {attempts}): {exc}")
            return {
                "payload": None,
                "generation_attempts": attempts,
                "error": str(exc),
                "status": "error",
                "reasoning_trace": trace,
            }

    def _node_run_guardrails(self, state: AgentState) -> dict:
        """Фаза 4: защитные проверки (SR-08/12/13/14/19)."""
        context = state["context"]
        payload = state.get("payload")
        trace = list(state.get("reasoning_trace", []))

        if payload is None:
            trace.append("Guardrails пропущены: отсутствует payload (ошибка генерации)")
            return {"guardrail_passed": False, "failed_guards": failed, "reasoning_trace": trace}

        report = self._guardrails.run(payload, context)
        failed = [check.name for check in report.failed_checks()]
        trace.append(f"Guardrails: passed={report.passed}, failed={failed}")
        return {"guardrail_passed": report.passed, "reasoning_trace": trace}

    def _node_finalize_draft(self, state: AgentState) -> dict:
        """Фаза 5: возврат валидного черновика (status=draft)."""
        payload = dict(state["payload"])
        trace = list(state.get("reasoning_trace", []))
        payload["reasoning_trace"] = trace
        return {"final_draft": payload, "status": "draft"}

    def _node_finalize_refusal(self, state: AgentState) -> dict:
        """Фаза 2/4: явный отказ (BRULE-03) или ошибка входа.

        Формирует:
          - валидный refusal (AMLInvestigationDraft) — если incident_id валиден
            и отказ вызван недостатком данных или провалом guardrails;
          - error-ответ — если ошибка на входе (невалидный incident_id,
            кейс не найден), т.к. валидный черновик невозможен.
        """
        incident_id = state["incident_id"]
        trace = list(state.get("reasoning_trace", []))
        context = state.get("context")
        error = state.get("error")

        # Ошибка входа (нет контекста) -> error-ответ (не черновик).
        if context is None:
            return {
                "final_draft": {
                    "incident_id": incident_id,
                    "status": "error",
                    "error": error or "Неизвестная ошибка обработки",
                    "reasoning_trace": trace,
                },
                "status": "error",
            }

        # Определение причины отказа.
        if context.data_completeness is not DataCompleteness.FULL:
            reason = (
                "Недостаточно данных для формирования заключения "
                "(отсутствует история транзакций или идентификационные данные). "
                "Кейс передан в очередь ручной обработки."
            )
        elif not state.get("guardrail_passed", True):
            reason = (
                "Черновик не прошёл защитные проверки: нарушена прослеживаемость "
                "выводов, фактическая обоснованность или обнаружена попытка "
                "манипуляции (prompt injection). Кейс передан в ручную обработку."
            )
        else:
            reason = "Невозможно сформировать заключение. Кейс передан в ручную обработку."

        refusal = {
            "incident_id": incident_id,
            "status": "refusal",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_completeness": context.data_completeness.value,
            "refusal_reason": reason,
            "reasoning_trace": trace,
            "confidence": 0.0,
        }
        return {"final_draft": refusal, "status": "refusal"}

    # -------------------------------------------------------------------------
    # Маршрутизация (conditional edges)
    # -------------------------------------------------------------------------

    @staticmethod
    def _route_after_context(state: AgentState) -> str:
        """Фаза 2: решение после сборки контекста (SR-20, BRULE-03).

        - ошибка входа (нет контекста)      -> refusal (error-ответ);
        - данные неполные (нет транзакций)  -> refusal (BRULE-03);
        - данные полные                     -> generate.
        """
        context = state.get("context")
        if context is None:
            return "refusal"
        if context.data_completeness is not DataCompleteness.FULL:
            return "refusal"
        return "generate"

    @staticmethod
    def _route_after_guardrails(state: AgentState) -> str:
        """Решение после guardrails: draft, retry или refusal.

        Оптимизация: если среди проваленных проверок есть prompt_injection,
        retry бесполезен — инъекция находится в исходных данных и не будет
        устранена повторной генерацией. В этом случае сразу отказ.
        """
        if state.get("payload") is None:
            return "refusal"
        if state.get("guardrail_passed"):
            return "draft"

        # Инъекция в данных не исправляется повторной генерацией — сразу отказ
        failed = state.get("failed_guards", [])
        if "prompt_injection" in failed:
            return "refusal"

        # Остальные провалы (schema, traceability) — случайные артефакты,
        # пробуем повторить генерацию
        attempts = state.get("generation_attempts", 0)
        if attempts < AMLAgent.MAX_GENERATION_ATTEMPTS:
            return "retry"
        return "refusal"

    # -------------------------------------------------------------------------
    # Публичный интерфейс
    # -------------------------------------------------------------------------

    def run(self, incident_id: str) -> dict:
        """Запускает полный цикл расследования для инцидента.

        Args:
            incident_id: внешний идентификатор инцидента (INC-NNNNNN).

        Returns:
            dict: итоговый ответ — draft (status=draft), refusal (status=refusal)
            или error (status=error). Содержит reasoning_trace для аудита (NFR-11).
        """
        initial_state: AgentState = {
            "incident_id": incident_id,
            "reasoning_trace": [],
            "generation_attempts": 0,
        }
        final_state = self._graph.invoke(initial_state)
        return final_state["final_draft"]