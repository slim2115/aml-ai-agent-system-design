"""Cooperative admission ledger for a predeclared call, token and time budget.

Every admitted attempt consumes one call immediately. Tokens are reserved before
invocation; missing usage retains that reservation rather than pretending zero
cost. This ledger does not invoke a provider, interrupt a hung callback, enforce
provider token limits, or establish model quality. Its owner must supply a real
transport deadline/process supervisor and an honest conservative reservation.
"""
from copy import deepcopy
import math
from threading import Lock
import time


class BudgetError(ValueError):
    """Fixed diagnostic code for an invalid budget input or ledger state."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class BudgetStop(BudgetError):
    """The ledger declined admission; the caller must not invoke its provider."""


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _finite(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_limits(limits):
    """Validate the entire fixed limit contract and return a detached copy."""
    if (type(limits) is not dict
            or set(limits) != {'max_attempts', 'max_tokens', 'wall_seconds'}
            or not _integer(limits['max_attempts'], 1, 1000)
            or not _integer(limits['max_tokens'], 1, 10_000_000)
            or not _finite(limits['wall_seconds'])
            or not 0 < limits['wall_seconds'] <= 3600):
        raise BudgetError('BUDGET_INVALID_LIMITS')
    return deepcopy(limits)


class BudgetLedger:
    """One active reservation; limits and any stop/breach remain sticky.

    ``reserve`` accepts a worst-case total input+output token reservation and
    returns its local ID. ``finish`` settles exactly that reservation with the
    measured total usage, or None when unavailable. Provider errors/timeouts
    still consume the call; they are not refunded or retried by this object.

    PASS describes complete budget accounting without an observed breach, never
    a successful model response or completed experiment. A denied next call is
    reported separately as admission_status=STOPPED. Planned slots not executed
    must remain in the experiment's denominator outside this ledger.
    """

    def __init__(self, limits, clock=time.monotonic):
        self._limits = validate_limits(limits)
        if not callable(clock):
            raise BudgetError('BUDGET_INVALID_CLOCK')
        self._clock = clock
        self._lock = Lock()
        self._started = self._last = None
        self._elapsed = 0.0
        self._attempts = []
        self._active = None
        self._stop_reason = None
        self._error_code = None
        self._breaches = []
        self._now()
        self._started = self._last

    def _fail_clock(self, code):
        self._error_code = self._error_code or code
        self._stop_reason = self._stop_reason or code
        raise BudgetError(self._error_code)

    def _now(self):
        if self._error_code:
            raise BudgetError(self._error_code)
        try:
            now = self._clock()
        except Exception:
            self._fail_clock('BUDGET_CLOCK_ERROR')
        if not _finite(now):
            self._fail_clock('BUDGET_INVALID_CLOCK')
        if self._last is not None and now < self._last:
            self._fail_clock('BUDGET_CLOCK_ROLLBACK')
        if self._started is not None:
            elapsed = now - self._started
            if not _finite(elapsed):
                self._fail_clock('BUDGET_INVALID_CLOCK')
            self._elapsed = float(elapsed)
        self._last = now
        return now

    def _charged(self):
        return sum(row['charged_tokens'] for row in self._attempts)

    def _stop(self, code):
        self._stop_reason = self._stop_reason or code
        raise BudgetStop(self._stop_reason)

    def reserve(self, tokens):
        """Charge before invocation, or reject without creating a new attempt."""
        with self._lock:
            if type(tokens) is not int or tokens <= 0:
                raise BudgetError('BUDGET_INVALID_RESERVATION')
            if self._error_code:
                raise BudgetError(self._error_code)
            if self._stop_reason:
                raise BudgetStop(self._stop_reason)
            if self._active is not None:
                raise BudgetError('BUDGET_ACTIVE_RESERVATION')
            self._now()
            if self._elapsed >= self._limits['wall_seconds']:
                self._stop('BUDGET_WALL_LIMIT_REACHED')
            if len(self._attempts) >= self._limits['max_attempts']:
                self._stop('BUDGET_ATTEMPT_LIMIT_REACHED')
            if self._charged() + tokens > self._limits['max_tokens']:
                self._stop('BUDGET_TOKEN_LIMIT_REACHED')
            identity = f'attempt-{len(self._attempts) + 1:04d}'
            self._attempts.append({
                'reservation_id': identity, 'reserved_tokens': tokens,
                'charged_tokens': tokens, 'known_tokens': None, 'outcome': 'active',
                'started_elapsed_seconds': self._elapsed, 'finished_elapsed_seconds': None,
                'elapsed_seconds': None,
            })
            self._active = identity
            return identity

    def finish(self, reservation_id, *, outcome, tokens):
        """Settle once; a late or over-reservation result closes future admission."""
        with self._lock:
            if (type(reservation_id) is not str or not reservation_id
                    or reservation_id != self._active):
                raise BudgetError('BUDGET_UNKNOWN_OR_SETTLED_RESERVATION')
            if type(outcome) is not str or outcome not in ('completed', 'timeout', 'error'):
                raise BudgetError('BUDGET_INVALID_OUTCOME')
            if tokens is not None and (type(tokens) is not int or tokens < 0):
                raise BudgetError('BUDGET_INVALID_USAGE')
            self._now()
            row = self._attempts[-1]
            row.update(outcome=outcome, known_tokens=tokens,
                       charged_tokens=row['reserved_tokens'] if tokens is None else tokens,
                       finished_elapsed_seconds=self._elapsed,
                       elapsed_seconds=self._elapsed - row['started_elapsed_seconds'])
            self._active = None
            if tokens is not None and tokens > row['reserved_tokens']:
                self._breaches.append('BUDGET_RESERVATION_EXCEEDED')
            if self._charged() > self._limits['max_tokens']:
                self._breaches.append('BUDGET_TOKEN_LIMIT_EXCEEDED')
            if self._elapsed > self._limits['wall_seconds']:
                self._breaches.append('BUDGET_WALL_LIMIT_EXCEEDED')
            if self._breaches:
                self._stop_reason = self._stop_reason or self._breaches[0]
            return self._snapshot()

    def _snapshot(self):
        unknown = sum(row['known_tokens'] is None and row['outcome'] != 'active'
                      for row in self._attempts)
        known = sum(row['known_tokens'] for row in self._attempts if row['known_tokens'] is not None)
        status = ('ERROR' if self._error_code else 'FAIL' if self._breaches else
                  'INCOMPLETE' if self._active is not None or unknown else 'PASS')
        return {
            'status': status, 'limits': deepcopy(self._limits),
            'attempted_calls': len(self._attempts), 'charged_tokens': self._charged(),
            'known_tokens': known, 'unknown_usage_attempts': unknown,
            'total_tokens': known if self._active is None and unknown == 0 else None,
            'active_reservation': self._active, 'elapsed_seconds': self._elapsed,
            'remaining_tokens': max(0, self._limits['max_tokens'] - self._charged()),
            'remaining_attempts': max(0, self._limits['max_attempts'] - len(self._attempts)),
            'remaining_wall_seconds': max(0.0, self._limits['wall_seconds'] - self._elapsed),
            'stop_reason': self._stop_reason, 'error_code': self._error_code,
            'breaches': list(dict.fromkeys(self._breaches)),
            'admission_status': ('STOPPED' if self._stop_reason else 'ACTIVE' if self._active else
                                 'LIMIT_REACHED' if self._elapsed >= self._limits['wall_seconds']
                                 or len(self._attempts) >= self._limits['max_attempts']
                                 or self._charged() >= self._limits['max_tokens'] else 'OPEN'),
            'attempts': deepcopy(self._attempts),
            'scope': 'Cooperative admission and accounting only; no hard interruption, provider enforcement or quality acceptance',
        }

    def snapshot(self):
        """Return a detached report; clock failures stay visible and terminal."""
        with self._lock:
            try:
                self._now()
            except BudgetError:
                pass
            return self._snapshot()
