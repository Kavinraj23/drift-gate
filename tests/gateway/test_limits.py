from __future__ import annotations

import threading

from driftgate.llm.limits import ConcurrencyLimiter, RateLimiter, TokenBucket

from .helpers import FakeClock


def test_bucket_allows_burst_then_waits_for_refill(clock: FakeClock) -> None:
    bucket = TokenBucket(capacity=2, refill_per_second=1.0, clock=clock)
    assert bucket.acquire() == 0
    assert bucket.acquire() == 0
    assert bucket.acquire() == 1.0  # deficit of one token at 1/s
    assert clock.sleeps == [1.0]


def test_bucket_refills_over_time_without_sleeping(clock: FakeClock) -> None:
    bucket = TokenBucket(capacity=2, refill_per_second=1.0, clock=clock)
    bucket.acquire(2)
    clock.now += 5
    assert bucket.acquire(2) == 0
    assert clock.sleeps == []


def test_oversized_request_is_clamped_to_capacity(clock: FakeClock) -> None:
    bucket = TokenBucket(capacity=10, refill_per_second=1.0, clock=clock)
    assert bucket.acquire(1000) == 0  # would otherwise wait forever


def test_rate_limiter_enforces_requests_and_tokens(clock: FakeClock) -> None:
    limiter = RateLimiter(requests_per_minute=60, tokens_per_minute=600, clock=clock)
    assert limiter.acquire(600) == 0
    waited = limiter.acquire(300)  # tokens refill at 10/s -> 30s
    assert waited == 30.0


def test_concurrency_cap_never_exceeded() -> None:
    cap = 2
    limiter = ConcurrencyLimiter(cap)
    lock = threading.Lock()
    release = threading.Event()
    at_cap = threading.Event()
    state = {"in_flight": 0, "max": 0}

    def worker() -> None:
        with limiter.slot():
            with lock:
                state["in_flight"] += 1
                state["max"] = max(state["max"], state["in_flight"])
                if state["in_flight"] == cap:
                    at_cap.set()
            release.wait(timeout=5)
            with lock:
                state["in_flight"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    assert at_cap.wait(timeout=5)
    threads[-1].join(timeout=0.05)  # blocked waiters cannot enter while two hold slots
    assert state["in_flight"] == cap
    release.set()
    for t in threads:
        t.join(timeout=5)
    assert state["max"] == cap
