# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
CircuitBreaker and RetryWithBackoff — ModelFungible Core

CircuitBreaker: prevents cascading failures by opening a circuit
when a service is unhealthy. Three states: CLOSED → OPEN → HALF-OPEN → CLOSED.

RetryWithBackoff: retries failed calls with exponential backoff and jitter.
Distinguishes retryable errors (timeout, rate limit) from fatal errors (auth, bad input).

Usage:
    cb = CircuitBreaker(failure_threshold=5, cooldown_seconds=60)
    if not cb.is_call_allowed():
        raise CircuitOpenError("Model temporarily unavailable")
    try:
        result = call_model()
        cb.record(success=True)
    except Exception as e:
        cb.record(success=False)
        if is_retryable_error(e):
            raise  # let RetryWithBackoff handle it
        raise

    rwb = RetryWithBackoff(max_retries=3, base_delay=1.0)
    result = rwb.run(
        fn,
        retryable=is_retryable_error,
    )
"""
from __future__ import annotations
import time
import random
from typing import Callable, Optional
from dataclasses import dataclass


# ─────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────
class CircuitOpenError(Exception):
    """Raised when a circuit breaker is OPEN and rejecting calls."""
    def __init__(self, model_name: str = "", message: str = ""):
        self.model_name = model_name
        self.message = message or "Circuit breaker is OPEN"
        super().__init__(f"[CircuitOpen] {model_name}: {self.message}")


class RetryExhausted(Exception):
    """Raised when all retry attempts are exhausted."""
    def __init__(self, attempts: int, last_error: str):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"All {attempts} attempts exhausted. Last error: {last_error}"
        )


# ─────────────────────────────────────────────────────────────────
# Error classification
# ─────────────────────────────────────────────────────────────────
def is_retryable_error(error: Exception) -> bool:
    """
    Return True if this error type is generally retryable.

    Retryable: timeouts, rate limits, server errors, connection resets.
    Not retryable: auth failures, bad input, not found, forbidden.
    """
    name = type(error).__name__
    msg = str(error).lower()

    # Explicitly NOT retryable
    if any(kw in msg for kw in [
        "auth", "unauthorized", "invalid api key", "403", "forbidden",
        "not found", "404", "bad request", "400", "422",
        "invalid request", "rate_limit_exceeded",
    ]):
        return False

    # Explicitly retryable
    retryable_names = {
        "TimeoutError", "ConnectionError", "HTTPError",
        "RequestException", "SSLError", "ConnectTimeout",
        "ReadTimeout", "GatewayTimeout",
    }
    if name in retryable_names:
        return True

    if "timeout" in msg or "timed out" in msg:
        return True
    if "connection" in msg or "network" in msg:
        return True
    if "reset" in msg or "refused" in msg:
        return True
    if "503" in msg or "502" in msg or "500" in msg:
        return True
    if "rate limit" in msg or "too many requests" in msg:
        return True

    return False


# ─────────────────────────────────────────────────────────────────
# CircuitBreaker
# ─────────────────────────────────────────────────────────────────
class CircuitBreaker:
    """
    Three-state circuit breaker.

    States:
        CLOSED:    Normal operation. Calls pass through. Failures accumulate.
        OPEN:      Service is unhealthy. Calls are rejected immediately.
        HALF-OPEN: Testing the service. One call allowed through.
                   Success → CLOSED. Failure → OPEN again.

    Parameters:
        failure_threshold: consecutive failures to trip the circuit (default 5)
        cooldown_seconds:  seconds to wait before testing recovery (default 60)
        half_open_success_threshold: successes in HALF-OPEN to close (default 1)
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
        half_open_success_threshold: int = 1,
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_success_threshold = half_open_success_threshold

        self._state = "CLOSED"
        self._failure_count = 0
        self._success_in_half_open = 0
        self._opened_at: Optional[float] = None  # unix timestamp

    # ── State ────────────────────────────────────────────────

    def state(self) -> str:
        """Current state: CLOSED | OPEN | HALF-OPEN."""
        self._maybe_transition()
        return self._state

    def is_call_allowed(self) -> bool:
        """Return True if a call is allowed right now."""
        self._maybe_transition()
        return self._state != "OPEN"

    def is_open(self) -> bool:
        return self.state() == "OPEN"

    def is_closed(self) -> bool:
        return self.state() == "CLOSED"

    # ── Recording ─────────────────────────────────────────────

    def record(self, success: bool) -> None:
        """
        Record a call outcome. May trigger state transitions.

        Args:
            success: True if the call succeeded, False if it failed.
        """
        self._maybe_transition()

        if self._state == "HALF-OPEN":
            if success:
                self._success_in_half_open += 1
                if self._success_in_half_open >= self.half_open_success_threshold:
                    self._close()
            else:
                self._open()
            return

        if self._state == "CLOSED":
            if success:
                self._failure_count = 0
            else:
                self._failure_count += 1
                if self._failure_count >= self.failure_threshold:
                    self._open()
            return

        # OPEN state — do nothing, wait for cooldown

    def reset(self) -> None:
        """Manually reset the breaker to CLOSED."""
        self._state = "CLOSED"
        self._failure_count = 0
        self._success_in_half_open = 0
        self._opened_at = None

    # ── Private state transitions ──────────────────────────────

    def _open(self) -> None:
        self._state = "OPEN"
        self._opened_at = time.time()
        self._failure_count = 0
        self._success_in_half_open = 0

    def _close(self) -> None:
        self._state = "CLOSED"
        self._failure_count = 0
        self._success_in_half_open = 0
        self._opened_at = None

    def _to_half_open(self) -> None:
        self._state = "HALF-OPEN"
        self._success_in_half_open = 0

    def _maybe_transition(self) -> None:
        """Check if cooldown has expired and transition OPEN → HALF-OPEN."""
        if self._state != "OPEN":
            return
        if self._opened_at is None:
            return

        elapsed = time.time() - self._opened_at
        if elapsed >= self.cooldown_seconds:
            self._to_half_open()

    # ── Context manager ─────────────────────────────────────

    def __enter__(self):
        if not self.is_call_allowed():
            raise CircuitOpenError(message="Circuit is OPEN")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.record(success=False)
        else:
            self.record(success=True)
        return False  # don't suppress exceptions


# ─────────────────────────────────────────────────────────────────
# RetryWithBackoff
# ─────────────────────────────────────────────────────────────────
class RetryWithBackoff:
    """
    Retry a callable with exponential backoff and optional jitter.

    Usage:
        rwb = RetryWithBackoff(max_retries=3, base_delay=1.0)
        result = rwb.run(
            fn,
            retryable=lambda e: isinstance(e, ConnectionError),
        )

    Tracks attempt count and last error for diagnostics.
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        jitter: bool = True,
        jitter_factor: float = 0.25,
    ):
        """
        Args:
            max_retries:     max number of retry attempts (after initial call)
            base_delay:      initial delay in seconds (doubles each retry)
            max_delay:       cap on delay in seconds
            jitter:           add random jitter to prevent thundering herd
            jitter_factor:   fraction of delay to randomize (±25% default)
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter = jitter
        self.jitter_factor = jitter_factor

        self.attempts: int = 0
        self.last_delay: float = 0.0
        self.last_error: Optional[str] = None

    def run(
        self,
        fn: Callable,
        retryable: Callable[[Exception], bool] = is_retryable_error,
    ):
        """
        Run fn with retries.

        Args:
            fn:       callable to execute
            retryable: fn(exception) → bool, returns True to retry

        Returns:
            Return value of fn on success.

        Raises:
            RetryExhausted: all retries exhausted.
            Any non-retryable exception re-raised immediately.
        """
        self.attempts = 0
        self.last_error = None

        while True:
            self.attempts += 1
            try:
                return fn()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"

                if not retryable(e):
                    raise

                if self.attempts > self.max_retries:
                    raise RetryExhausted(
                        attempts=self.attempts,
                        last_error=self.last_error,
                    )

                # Compute delay
                delay = min(
                    self.base_delay * (2 ** (self.attempts - 2)),
                    self.max_delay,
                ) if self.attempts > 1 else self.base_delay

                if self.jitter:
                    spread = delay * self.jitter_factor
                    delay = delay + random.uniform(-spread, spread)
                    delay = max(0.01, delay)

                self.last_delay = delay
                time.sleep(delay)


# ─────────────────────────────────────────────────────────────────
# Combined: call_with_protection
# ─────────────────────────────────────────────────────────────────
def call_with_protection(
    fn: Callable,
    cb: Optional[CircuitBreaker] = None,
    rwb: Optional[RetryWithBackoff] = None,
    model_name: str = "",
    retryable: Callable[[Exception], bool] = is_retryable_error,
):
    """
    Call a function with circuit breaker + retry protection.

    Usage:
        cb = CircuitBreaker(failure_threshold=3)
        rwb = RetryWithBackoff(max_retries=3)
        try:
            result = call_with_protection(
                fn,
                cb=cb,
                rwb=rwb,
                model_name="claude",
            )
        except CircuitOpenError:
            print("Model unavailable — circuit is open")
        except RetryExhausted as e:
            print(f"All attempts failed: {e.last_error}")

    Args:
        fn:         callable to execute
        cb:         optional CircuitBreaker for cascade failure protection
        rwb:        optional RetryWithBackoff for transient error retries
        model_name:  name for error messages
        retryable:   fn(e) → bool for retryable errors
    """
    if cb is not None and not cb.is_call_allowed():
        raise CircuitOpenError(model_name=model_name)

    def _call():
        if cb is not None:
            try:
                result = fn()
                cb.record(success=True)
                return result
            except Exception as e:
                cb.record(success=False)
                raise
        return fn()

    if rwb is not None:
        return rwb.run(_call, retryable=retryable)

    return _call()


__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "RetryWithBackoff",
    "RetryExhausted",
    "is_retryable_error",
    "call_with_protection",
]
