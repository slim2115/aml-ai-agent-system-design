"""A shared monotonic I/O budget; expiry never authorizes a fresh cleanup call."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import math
import time
from typing import Callable, Iterator


class BudgetExpired(RuntimeError):
    """Safe local scheduler outcome, not a workflow rejection or remote timeout."""

    def __init__(self):
        super().__init__('SCHEDULER_BUDGET_EXHAUSTED')


@dataclass(frozen=True)
class Deadline:
    expires_at: float
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def after(cls, seconds: float, *, clock=time.monotonic) -> 'Deadline':
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('INVALID_IO_BUDGET')
        return cls(clock() + seconds, clock)

    def remaining(self) -> float:
        remaining = self.expires_at - self.clock()
        if remaining <= 0:
            raise BudgetExpired()
        return remaining

    def check(self) -> None:
        self.remaining()


_CURRENT: ContextVar[Deadline | None] = ContextVar('aml_io_budget', default=None)


def current_budget() -> Deadline | None:
    return _CURRENT.get()


def check_budget() -> None:
    if (budget := current_budget()) is not None:
        budget.check()


@contextmanager
def use_budget(budget: Deadline) -> Iterator[Deadline]:
    """Propagate through existing synchronous store/HTTP calls without new keys."""
    token = _CURRENT.set(budget)
    try:
        budget.check()
        yield budget
    finally:
        _CURRENT.reset(token)
