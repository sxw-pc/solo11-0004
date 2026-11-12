"""Shared retry strategy: one place for attempts, backoff, jitter and Retry-After.

Two layers share this module but never retry the same failure twice:

* ``RetryLayer.STEP`` (DSL engine) retries only transient ``NETWORK`` errors
  (timeouts, connection resets) inside a single step.
* ``RetryLayer.TASK`` (runner scheduler) retries only recoverable state
  failures (``SERVER``: 5xx / 429) by replaying the whole task, subject to the
  final-step idempotency gate enforced by the scheduler.

A :class:`RetryChain` tracks a single failure chain across both layers so that
a ``Retry-After`` header is honoured at most once; every later backoff on the
same chain uses exponential backoff instead.
"""
from __future__ import annotations
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TypeVar
from fillbot.errors import BotError, ErrorClass

T = TypeVar("T")
Sleeper = Callable[[float], None]


class RetryLayer(str, Enum):
    """Which execution layer a policy belongs to."""

    STEP = "step"
    TASK = "task"


# Layer -> error categories that layer is allowed to retry.  The categories are
# disjoint by design: a failure can only be retried at one layer, so there is
# no multiplicative effect when both layers are configured.
LAYER_RETRYABLE: dict[RetryLayer, frozenset[ErrorClass]] = {
    RetryLayer.STEP: frozenset({ErrorClass.NETWORK}),
    RetryLayer.TASK: frozenset({ErrorClass.SERVER}),
}


@dataclass(frozen=True)
class RetrySpec:
    """Validated ``retry`` option from a YAML step (integer or mapping)."""

    attempts: int = 1
    base_seconds: float = 0.0
    max_seconds: float = 8.0


def parse_retry_spec(raw: Any, step_no: int | None = None) -> RetrySpec:
    """Parse the YAML ``retry`` common option.

    Accepted shapes are unchanged: an integer (attempts) or a mapping with
    optional ``attempts`` / ``base_seconds`` / ``max_seconds``.  ``None`` means
    the step does not override the runner-level default policy.
    """
    label = f"step {step_no}: " if step_no is not None else ""
    if raw is None:
        return RetrySpec()
    if isinstance(raw, bool):
        raise ValueError(f"{label}retry must be a mapping or integer")
    if isinstance(raw, int):
        if raw < 1:
            raise ValueError(f"{label}retry.attempts must be >= 1")
        return RetrySpec(attempts=raw)
    if not isinstance(raw, dict):
        raise ValueError(f"{label}retry must be a mapping or integer")
    unknown = set(raw) - {"attempts", "base_seconds", "max_seconds"}
    if unknown:
        raise ValueError(f"{label}unknown retry key(s): {', '.join(sorted(unknown))}")
    attempts = raw.get("attempts", 1)
    base = raw.get("base_seconds", 0.0)
    maximum = raw.get("max_seconds", 8.0)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        raise ValueError(f"{label}retry.attempts must be an integer >= 1")
    if not isinstance(base, (int, float)) or isinstance(base, bool) or base < 0:
        raise ValueError(f"{label}retry.base_seconds must be >= 0")
    if not isinstance(maximum, (int, float)) or isinstance(maximum, bool) or maximum < base:
        raise ValueError(f"{label}retry.max_seconds must be >= base_seconds")
    return RetrySpec(attempts=attempts, base_seconds=float(base), max_seconds=float(maximum))


@dataclass
class RetryChain:
    """Per-failure-chain state shared by the step and task retry loops.

    ``Retry-After`` is a server directive for one specific failed response. Once
    any layer on the chain has slept for it, the directive is consumed; later
    retries on the same chain fall back to exponential backoff so the header is
    never honoured twice (e.g. once at step level and again at task level).
    """

    retry_after_consumed: bool = False

    def take_retry_after(self, error: BotError) -> float | None:
        if error.retry_after is None or self.retry_after_consumed:
            return None
        self.retry_after_consumed = True
        return max(0.0, float(error.retry_after))


@dataclass
class RetryPolicy:
    """Backoff parameters bound to one execution layer."""

    max_attempts: int = 3
    base_seconds: float = 0.4
    max_seconds: float = 8.0
    jitter: bool = True
    layer: RetryLayer = RetryLayer.STEP
    rng: random.Random = field(default_factory=random.Random, repr=False)
    sleeper: Sleeper = field(default=time.sleep, repr=False)

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.base_seconds < 0 or self.max_seconds < self.base_seconds:
            raise ValueError("invalid retry policy")
        if not isinstance(self.layer, RetryLayer):
            self.layer = RetryLayer(self.layer)

    def allows(self, error: BotError) -> bool:
        """Whether this layer is permitted to retry ``error`` at all."""
        return error.category in LAYER_RETRYABLE[self.layer]

    def compute_delay(
        self,
        attempt: int,
        error: BotError | None = None,
        chain: RetryChain | None = None,
    ) -> float:
        if error is not None and error.retry_after is not None:
            if chain is not None:
                directed = chain.take_retry_after(error)
                if directed is not None:
                    return directed
            else:
                # Standalone use without a chain keeps the historical
                # behaviour: a present Retry-After always wins over backoff.
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
    chain: RetryChain | None = None,
) -> RetryOutcome:
    """Run ``operation`` under ``policy``'s layer rules.

    Errors outside the layer's retryable categories (and exhausted attempts)
    propagate untouched; non-:class:`BotError` exceptions are never retried.
    """
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return RetryOutcome(operation(), attempt)
        except BotError as error:
            if not policy.allows(error) or attempt >= policy.max_attempts:
                raise
            delay = policy.compute_delay(attempt, error, chain)
            if on_retry:
                on_retry(attempt, error, delay)
            if delay > 0:
                policy.sleeper(delay)
    raise AssertionError("retry loop exhausted")  # pragma: no cover
