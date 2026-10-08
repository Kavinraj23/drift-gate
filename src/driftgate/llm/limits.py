"""Token-bucket rate limiter and in-flight concurrency cap."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from driftgate.llm.clock import Clock


class TokenBucket:
    """Reserve-then-wait bucket: acquire() takes tokens immediately and sleeps off any deficit."""

    def __init__(self, capacity: float, refill_per_second: float, clock: Clock) -> None:
        self._capacity = capacity
        self._rate = refill_per_second
        self._clock = clock
        self._level = capacity
        self._last = clock.monotonic()
        self._lock = threading.Lock()

    def acquire(self, amount: float = 1.0) -> float:
        """Block (via the injected clock) until `amount` is available. Returns seconds waited."""
        amount = min(amount, self._capacity)
        with self._lock:
            now = self._clock.monotonic()
            self._level = min(self._capacity, self._level + (now - self._last) * self._rate)
            self._last = now
            self._level -= amount
            wait = -self._level / self._rate if self._level < 0 else 0.0
        if wait > 0:
            self._clock.sleep(wait)
        return wait

    def refund(self, amount: float) -> None:
        with self._lock:
            self._level = min(self._capacity, self._level + amount)


class RateLimiter:
    """Requests-per-minute and tokens-per-minute buckets."""

    def __init__(self, requests_per_minute: int, tokens_per_minute: int, clock: Clock) -> None:
        self._requests = TokenBucket(requests_per_minute, requests_per_minute / 60.0, clock)
        self._tokens = TokenBucket(tokens_per_minute, tokens_per_minute / 60.0, clock)

    def acquire(self, estimated_tokens: int) -> float:
        return self._requests.acquire(1) + self._tokens.acquire(estimated_tokens)

    def refund_tokens(self, amount: int) -> None:
        if amount > 0:
            self._tokens.refund(amount)


class ConcurrencyLimiter:
    def __init__(self, max_in_flight: int) -> None:
        self._sem = threading.BoundedSemaphore(max_in_flight)

    @contextmanager
    def slot(self) -> Iterator[None]:
        self._sem.acquire()
        try:
            yield
        finally:
            self._sem.release()
