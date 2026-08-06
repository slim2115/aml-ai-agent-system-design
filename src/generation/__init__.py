"""Слой генерации черновика отчёта через локальную LLM (SR-05..07, ADR-0003)."""
from src.generation.report_generator import (
    GenerationResult,
    OllamaUnavailableError,
    ReportGenerator,
)

__all__ = ["ReportGenerator", "GenerationResult", "OllamaUnavailableError"]