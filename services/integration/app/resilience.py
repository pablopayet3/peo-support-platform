"""Timeouts, retries and a circuit breaker for calls to other systems.

Why: an intake must never hang because a downstream system is slow or down. When the core platform
fails repeatedly, the breaker opens and callers get "unavailable" at once, so the AI agent can end the
intake as Incomplete and the ticket is still routed (the rule from the case study, section 3).
"""
import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

T = TypeVar("T")


class Unavailable(Exception):
    """A downstream system did not answer in time, or the breaker is open."""


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 5, reset_after_s: float = 30.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_after_s = reset_after_s
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if time.monotonic() - self.opened_at >= self.reset_after_s:
            return "half_open"
        return "open"

    def before_call(self) -> None:
        if self.state == "open":
            raise Unavailable(f"{self.name} circuit is open")

    def on_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def on_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold or self.state == "half_open":
            self.opened_at = time.monotonic()


RETRYABLE_STATUS = {429, 502, 503, 504}


async def call_with_retries(fn: Callable[[], Awaitable[httpx.Response]], breaker: CircuitBreaker, *,
                            attempts: int = 3, base_delay_s: float = 0.2) -> httpx.Response:
    """Retry transient failures with exponential backoff and jitter. Honours Retry-After on 429/503."""
    breaker.before_call()
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = await fn()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
        else:
            if resp.status_code not in RETRYABLE_STATUS:
                if resp.status_code >= 500:
                    breaker.on_failure()
                else:
                    breaker.on_success()
                return resp
            last_exc = None
            if attempt == attempts:
                breaker.on_failure()
                raise Unavailable(f"{breaker.name} answered {resp.status_code}")
            retry_after = resp.headers.get("retry-after")
            if retry_after and retry_after.isdigit() and int(retry_after) <= 10:
                await asyncio.sleep(int(retry_after))
                continue
        if attempt < attempts:
            await asyncio.sleep(base_delay_s * 2 ** (attempt - 1) + random.uniform(0, base_delay_s))
    breaker.on_failure()
    raise Unavailable(f"{breaker.name} did not answer: {last_exc!r}")
