"""Token-bucket pacing and run identifiers.

The limiter is shared across every worker thread of a run, so all token
accounting happens under one lock: concurrency can never exceed the global
rate even though multiple browsers issue requests simultaneously.
"""
from __future__ import annotations
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable


class RateLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = float(max(1, requests_per_minute))
        self.rate_per_second = self.capacity / 60.0
        self.tokens = self.capacity
        self.updated = clock()
        self.sleeper = sleeper
        self._clock = clock
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate_per_second)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                delay = (1 - self.tokens) / self.rate_per_second
            # Sleep without holding the lock; other workers can spend the
            # tokens that refill in the meantime, then re-check on wake.
            self.sleeper(delay)


def new_run_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
