"""Error-class-driven retry with exponential backoff, jitter and Retry-After."""
from __future__ import annotations
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar
from fillbot.errors import BotError
T = TypeVar("T")
Sleeper = Callable[[float], None]
@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_seconds: float = 0.4
    max_seconds: float = 8.0
    jitter: bool = True
    rng: random.Random = field(default_factory=random.Random, repr=False)
    sleeper: Sleeper = field(default=time.sleep, repr=False)
    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.base_seconds < 0 or self.max_seconds < self.base_seconds:
            raise ValueError("invalid retry policy")
    def compute_delay(self, attempt: int, error: BotError | None = None) -> float:
        if error is not None and error.retry_after is not None:
            return max(0.0, float(error.retry_after))
        raw = min(self.base_seconds * (2 ** max(0, attempt - 1)), self.max_seconds)
        if self.jitter and raw > 0:
            raw *= self.rng.uniform(0.75, 1.25)
        return max(0.0, min(raw, self.max_seconds))
@dataclass
class RetryOutcome:
    value: Any
    attempts: int
def run_with_retry(
    operation: Callable[[], T],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[int, BotError, float], None] | None = None,
) -> RetryOutcome:
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return RetryOutcome(operation(), attempt)
        except BotError as error:
            if not error.retryable or attempt >= policy.max_attempts:
                raise
            delay = policy.compute_delay(attempt, error)
            if on_retry:
                on_retry(attempt, error, delay)
            if delay > 0:
                policy.sleeper(delay)
    raise AssertionError("retry loop exhausted")  # pragma: no cover
