"""Compatibility import for the corrected v1 runtime.

The former free-text LangGraph path is replaced by the constrained service.
Durable jobs and cancellation belong to the next block.
"""
from typing import TypedDict
from src.runtime.service import AMLAgent

class AgentState(TypedDict):
    incident_id: str
    result: dict

__all__ = ['AMLAgent', 'AgentState']
