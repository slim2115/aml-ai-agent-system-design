"""Слой RAG-поиска по базе знаний комплаенса (SR-07, ADR-0004)."""
from src.retrieval.knowledge_base import (
    KnowledgeBase,
    RetrievalResult,
    RuleChunk,
)

__all__ = ["KnowledgeBase", "RetrievalResult", "RuleChunk"]