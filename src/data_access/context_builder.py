"""Сборка консолидированного контекста расследования (SR-04).

Оркестрирует репозитории для сборки InvestigationContext и оценивает
полноту данных (SR-20), что является входом для политики явного отказа
(BRULE-03) на уровне guardrails/агента.

Разделение ответственности: builder только объективно оценивает полноту
(full/partial/insufficient); решение об отказе принимает вышестоящий слой.
"""
from __future__ import annotations

from src.data_access.card_repository import CardRepository
from src.data_access.case_repository import CaseRepository
from src.data_access.client_repository import ClientRepository
from src.data_access.transaction_repository import TransactionRepository
from src.schemas import (
    Case,
    Client,
    DataCompleteness,
    InvestigationContext,
    Transaction,
)


class InvestigationContextBuilder:
    """Собирает InvestigationContext по incident_id (SR-04)."""

    def __init__(
        self,
        cases: CaseRepository | None = None,
        clients: ClientRepository | None = None,
        cards: CardRepository | None = None,
        transactions: TransactionRepository | None = None,
    ) -> None:
        self._cases = cases or CaseRepository()
        self._clients = clients or ClientRepository()
        self._cards = cards or CardRepository()
        self._transactions = transactions or TransactionRepository()

    def build(self, incident_id: str) -> InvestigationContext:
        """Строит контекст инцидента.

        Последовательность (соответствует UML Sequence, Фаза 1):
          1. Поиск кейса по incident_id (SR-01).
          2. Извлечение профиля клиента (SR-02).
          3. Извлечение карт и транзакций клиента (SR-03).
          4. Оценка полноты данных (SR-20).

        Args:
            incident_id: внешний идентификатор инцидента (INC-NNNNNN).

        Returns:
            InvestigationContext с заполненными данными и data_completeness.

        Raises:
            InvalidInputError: при неверном формате incident_id.
            EntityNotFoundError: если кейс не найден.
        """
        case: Case = self._cases.get_by_incident_id_or_raise(incident_id)
        client: Client = self._clients.get_by_id_or_raise(case.client_id)
        cards = self._cards.get_by_client(client.client_id)
        transactions = self._transactions.get_by_client(client.client_id)

        completeness = self._assess_completeness(transactions)

        return InvestigationContext(
            incident_id=incident_id,
            case=case,
            client=client,
            cards=cards,
            transactions=transactions,
            data_completeness=completeness,
        )

    @staticmethod
    def _assess_completeness(transactions: list[Transaction]) -> DataCompleteness:
        """Оценивает полноту данных (SR-20).

        Правила (соответствует AC-03.2 и BRULE-03):
          - нет транзакций      -> PARTIAL (неполные данные; триггер для SR-20);
          - транзакции есть     -> FULL.

        Примечание: клиент уже гарантированно присутствует (иначе
        get_by_id_or_raise выбросил бы EntityNotFoundError -> трактуется
        как INSUFFICIENT на уровне guardrails).
        """
        if not transactions:
            return DataCompleteness.PARTIAL
        return DataCompleteness.FULL