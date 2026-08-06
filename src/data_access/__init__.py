"""Слой доступа к данным (read-only, BR-07/SR-18).

Публичный интерфейс: репозитории сущностей и сборщик контекста.
"""
from src.data_access.case_repository import CaseRepository
from src.data_access.client_repository import ClientRepository
from src.data_access.card_repository import CardRepository
from src.data_access.rule_repository import RuleRepository
from src.data_access.context_builder import InvestigationContextBuilder
from src.data_access.exceptions import (
    DataAccessError,
    EntityNotFoundError,
    InvalidInputError,
)
from src.data_access.transaction_repository import TransactionRepository

__all__ = [
    "CaseRepository",
    "ClientRepository",
    "CardRepository",
    "TransactionRepository",
    "InvestigationContextBuilder",
    "DataAccessError",
    "EntityNotFoundError",
    "InvalidInputError",
]