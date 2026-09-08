"""Token-bucket throttling plus hard per-run ceilings.

Two independent guards. The bucket smooths request pacing so we stay polite to
free endpoints; the budget is an absolute cap so a bug cannot burn a whole
daily quota in one run.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Classic token bucket. Thread-safe so the scraper can use a pool."""

    def __init__(self, per_minute: int, burst: int | None = None):
        self.rate = max(per_minute, 1) / 60.0
        self.capacity = float(burst if burst is not None else max(per_minute, 1))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until tokens are available. Returns seconds spent waiting."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._last) * self.rate
                )
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                sleep_for = deficit / self.rate
            time.sleep(min(sleep_for, 5.0))
            waited += min(sleep_for, 5.0)


class Budget:
    """A hard ceiling on calls for a single run."""

    def __init__(self, limit: int, name: str = "calls"):
        self.limit = limit
        self.name = name
        self.used = 0
        self._lock = threading.Lock()

    def take(self, n: int = 1) -> bool:
        with self._lock:
            if self.used + n > self.limit:
                return False
            self.used += n
            return True

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def __str__(self) -> str:
        return f"{self.name}: {self.used}/{self.limit}"
