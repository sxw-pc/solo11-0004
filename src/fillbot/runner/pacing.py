"""Token-bucket pacing and run identifiers."""
from __future__ import annotations
import time
import uuid
from datetime import datetime, timezone
from typing import Callable
class RateLimiter:
    def __init__(self, requests_per_minute: int, *, sleeper: Callable[[float], None] = time.sleep) -> None:
        self.capacity = float(max(1, requests_per_minute))
        self.rate_per_second = self.capacity / 60.0
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.sleeper = sleeper
    def wait(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate_per_second)
        self.updated = now
        if self.tokens < 1:
            self.sleeper((1 - self.tokens) / self.rate_per_second)
            self.updated, self.tokens = time.monotonic(), 0.0
        self.tokens -= 1
def new_run_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
