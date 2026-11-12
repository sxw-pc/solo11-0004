"""Per-target-site policies: configuration, adaptive pacing and policy events.

Sites are keyed by host, ``host:port``, full URL or wildcard (``*.example.com``).
Each site inherits every field it does not override from the global
:class:`~fillbot.config.BotConfig`; DSL steps may add a one-step override.

The :class:`RequestGovernor` is the object the browser adapter talks to.  It
paces requests with a per-site token bucket, returns the effective timeout and
watches responses so a ``429`` (or a ``503`` carrying ``Retry-After``) halves the
affected site's rate immediately.  Consecutive successes gradually restore it.
Every limit trigger and policy adjustment is emitted as a JSON-serialisable
event; the DSL engine attaches these to the step result and the ledger stores
them in ``steps.policy_events_json`` so a resumed run can replay them.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from fillbot.models import iso_now

POLICY_KEYS = (
    "rate_limit_per_minute",
    "timeout_seconds",
    "max_retries",
    "backoff_base_seconds",
    "backoff_max_seconds",
    "jitter",
)
DEFAULT_PORTS = {"http": 80, "https": 443}
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
_MIN_FACTOR = 0.0625
_RECOVERY_STREAK = 3
_RECOVERY_STEP = 1.25


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {
        "1", "true", "yes", "on", "0", "false", "no", "off"
    }:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return None


@dataclass(frozen=True)
class SitePolicyOverride:
    """A partial policy; ``None`` means "inherit from the layer above"."""

    rate_limit_per_minute: int | None = None
    timeout_seconds: float | None = None
    max_retries: int | None = None
    backoff_base_seconds: float | None = None
    backoff_max_seconds: float | None = None
    jitter: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in POLICY_KEYS if getattr(self, key) is not None}

    def __bool__(self) -> bool:
        return any(getattr(self, key) is not None for key in POLICY_KEYS)


def parse_policy(raw: Any) -> tuple[SitePolicyOverride | None, list[str]]:
    """Parse a partial policy without ever raising.

    Returns ``(override, issues)``.  A non-empty ``issues`` list means the caller
    should ignore the whole entry and fall back to the layer above.
    """
    if not isinstance(raw, Mapping):
        return None, ["policy must be a mapping"]
    unknown = sorted(set(raw) - set(POLICY_KEYS))
    if unknown:
        return None, [f"unknown policy key(s): {', '.join(unknown)}"]
    values: dict[str, Any] = {}
    issues: list[str] = []
    if "rate_limit_per_minute" in raw:
        value = raw["rate_limit_per_minute"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            issues.append("rate_limit_per_minute must be an integer >= 1")
        else:
            values["rate_limit_per_minute"] = value
    if "timeout_seconds" in raw:
        value = raw["timeout_seconds"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            issues.append("timeout_seconds must be a number > 0")
        else:
            values["timeout_seconds"] = float(value)
    if "max_retries" in raw:
        value = raw["max_retries"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            issues.append("max_retries must be an integer >= 0")
        else:
            values["max_retries"] = value
    if "backoff_base_seconds" in raw:
        value = raw["backoff_base_seconds"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            issues.append("backoff_base_seconds must be a number > 0")
        else:
            values["backoff_base_seconds"] = float(value)
    if "backoff_max_seconds" in raw:
        value = raw["backoff_max_seconds"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            issues.append("backoff_max_seconds must be a number > 0")
        else:
            values["backoff_max_seconds"] = float(value)
    if "jitter" in raw:
        value = _as_bool(raw["jitter"])
        if value is None:
            issues.append("jitter must be a boolean")
        else:
            values["jitter"] = value
    base, maximum = values.get("backoff_base_seconds"), values.get("backoff_max_seconds")
    if base is not None and maximum is not None and maximum < base:
        issues.append("backoff_max_seconds must be >= backoff_base_seconds")
    if issues:
        return None, issues
    return SitePolicyOverride(**values), []


def canonical_site(value: Any) -> str | None:
    """Normalise a config key / host / URL into ``host[:port]`` or ``*.host``."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    if text.startswith("*."):
        base = text[2:]
        if not base or "://" in base or "/" in base or ":" in base or not _HOST.fullmatch(base):
            return None
        return "*." + base
    if "://" in text:
        try:
            parsed = urlsplit(text)
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()
        if not host or not _HOST.fullmatch(host):
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        if port is not None:
            if not 1 <= port <= 65535:
                return None
            return f"{host}:{port}"
        if parsed.scheme in DEFAULT_PORTS:
            return f"{host}:{DEFAULT_PORTS[parsed.scheme]}"
        return host
    host, port_text = text, ""
    if ":" in text:
        host, port_text = text.rsplit(":", 1)
    if not _HOST.fullmatch(host) or "/" in text:
        return None
    if port_text:
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            return None
        return f"{host}:{int(port_text)}"
    return host


def site_from_url(url: Any) -> str | None:
    """Return the ``host[:port]`` bucket for a request URL (memoised)."""
    if not isinstance(url, str):
        return None
    cached = _SITE_CACHE.get(url)
    if cached is not None:
        return cached
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        port = DEFAULT_PORTS.get(parsed.scheme)
    site = f"{host}:{port}" if port else host
    if len(_SITE_CACHE) >= 2048:
        _SITE_CACHE.clear()
    _SITE_CACHE[url] = site
    return site


_SITE_CACHE: dict[str, str] = {}


def host_of_site(site: str) -> str:
    if site.startswith("*."):
        return site[2:]
    if ":" in site:
        return site.rsplit(":", 1)[0]
    return site


@dataclass(frozen=True)
class RequestPolicy:
    """A fully resolved policy used by one request."""

    rate_limit_per_minute: int
    timeout_seconds: float
    max_retries: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    jitter: bool
    source: str = "global"

    def apply(self, override: SitePolicyOverride | None, source: str = "step") -> "RequestPolicy":
        if override is None or not override:
            return self
        base = override.backoff_base_seconds if override.backoff_base_seconds is not None else self.backoff_base_seconds
        maximum = override.backoff_max_seconds if override.backoff_max_seconds is not None else self.backoff_max_seconds
        return replace(
            self,
            rate_limit_per_minute=(
                override.rate_limit_per_minute
                if override.rate_limit_per_minute is not None
                else self.rate_limit_per_minute
            ),
            timeout_seconds=(
                override.timeout_seconds if override.timeout_seconds is not None else self.timeout_seconds
            ),
            max_retries=override.max_retries if override.max_retries is not None else self.max_retries,
            backoff_base_seconds=base,
            backoff_max_seconds=max(maximum, base),
            jitter=override.jitter if override.jitter is not None else self.jitter,
            source=source,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "backoff_base_seconds": self.backoff_base_seconds,
            "backoff_max_seconds": self.backoff_max_seconds,
            "jitter": self.jitter,
            "source": self.source,
        }


def global_policy(config: Any) -> RequestPolicy:
    return RequestPolicy(
        int(config.rate_limit_per_minute),
        float(config.timeout_seconds),
        int(config.max_retries),
        float(config.backoff_base_seconds),
        float(config.backoff_max_seconds),
        bool(config.jitter),
        "global",
    )


class SitePolicyRegistry:
    """Resolve URLs to effective policies with a tiny per-site cache."""

    def __init__(self, config: Any) -> None:
        self.global_policy = global_policy(config)
        self._sites: dict[str, SitePolicyOverride] = {}
        self.warnings = list(getattr(config, "site_policy_warnings", ()) or ())
        raw_sites = getattr(config, "site_policies", None) or {}
        if isinstance(raw_sites, Mapping):
            for raw_key, raw_policy in raw_sites.items():
                key = canonical_site(raw_key)
                if key is None:
                    self.warnings.append(f"site policy for {raw_key!r} ignored: invalid site key")
                    continue
                override, issues = parse_policy(raw_policy)
                if override is None or issues:
                    self.warnings.append(
                        f"site policy for {raw_key!r} ignored: {'; '.join(issues) or 'invalid policy'}"
                    )
                    continue
                self._sites[key] = override
        self._wildcards = sorted(
            (key for key in self._sites if key.startswith("*.")), key=len, reverse=True
        )
        self._cache: dict[str, RequestPolicy] = {}

    @property
    def configured_sites(self) -> list[str]:
        return sorted(self._sites)

    def _site_partial(self, site: str) -> SitePolicyOverride | None:
        if site in self._sites:
            return self._sites[site]
        host = host_of_site(site)
        if host in self._sites:
            return self._sites[host]
        for key in self._wildcards:
            base = key[2:]
            if host == base or host.endswith("." + base):
                return self._sites[key]
        return None

    def policy_for_site(self, site: str | None, override: SitePolicyOverride | None = None) -> RequestPolicy:
        if not site:
            return self.global_policy.apply(override)
        base = self._cache.get(site)
        if base is None:
            partial = self._site_partial(site)
            base = self.global_policy.apply(partial, "site") if partial is not None else self.global_policy
            self._cache[site] = base
        return base.apply(override)

    def policy_for(self, url: str, override: SitePolicyOverride | None = None) -> RequestPolicy:
        return self.policy_for_site(site_from_url(url), override)

    def policy_for_site_key(self, site_key: str) -> RequestPolicy:
        site = canonical_site(site_key) or site_key
        return self.policy_for_site(site)


class AdaptiveRateLimiter:
    """Token bucket whose rate can be temporarily reduced after a limit signal.

    Normal (unthrottled) requests take exactly the same code path and the same
    number of clock reads as the original global limiter.
    """

    def __init__(
        self,
        requests_per_minute: int,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_rpm = float(max(1, requests_per_minute))
        self.factor = 1.0
        self.tokens = self.base_rpm
        self.updated = clock()
        self.sleeper = sleeper
        self.clock = clock

    @property
    def effective_rpm(self) -> float:
        return max(1.0, self.base_rpm * self.factor)

    @property
    def capacity(self) -> float:
        return self.base_rpm

    @property
    def rate_per_second(self) -> float:
        return self.base_rpm / 60.0

    def set_factor(self, factor: float, *, drain: bool = False) -> None:
        self.factor = max(_MIN_FACTOR, min(1.0, float(factor)))
        capacity = max(1.0, self.effective_rpm)
        self.tokens = min(self.tokens, capacity)
        if drain:
            self.tokens = min(self.tokens, 0.0)

    def wait(self, rate_limit_per_minute: float | None = None) -> None:
        rate_rpm = max(1.0, self.base_rpm if rate_limit_per_minute is None else float(rate_limit_per_minute))
        rate = max(0.0001, (rate_rpm * self.factor) / 60.0)
        now = self.clock()
        capacity = max(1.0, rate_rpm * self.factor)
        self.tokens = min(capacity, self.tokens + (now - self.updated) * rate)
        self.updated = now
        if self.tokens < 1:
            self.sleeper((1 - self.tokens) / rate)
            self.updated, self.tokens = self.clock(), 0.0
        self.tokens -= 1


@dataclass
class _SiteState:
    policy: RequestPolicy
    limiter: AdaptiveRateLimiter
    successes: int = 0


class RequestGovernor:
    """Request hook used by the browser adapter.

    ``before_request`` performs pacing and returns a policy timeout (``None``
    keeps the browser's configured default).  ``note_response`` observes the
    status code and adapts the site's rate.  Policy events are buffered per
    step and handed back through ``end_step`` for ledger persistence.
    """

    def __init__(
        self,
        registry: SitePolicyRegistry,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        recovery_streak: int = _RECOVERY_STREAK,
    ) -> None:
        self.registry = registry
        self.sleeper = sleeper
        self.clock = clock
        self.recovery_streak = max(1, recovery_streak)
        self._states: dict[str, _SiteState] = {}
        self._global_limiter = AdaptiveRateLimiter(
            self.registry.global_policy.rate_limit_per_minute, sleeper=self.sleeper, clock=self.clock
        )
        self._events: list[dict[str, Any]] = []
        self._step: int | None = None
        self._override: SitePolicyOverride | None = None
        self._pending_restore: list[dict[str, Any]] = []

    # -- state -----------------------------------------------------------
    def state(self, site: str) -> _SiteState:
        found = self._states.get(site)
        if found is None:
            policy = self.registry.policy_for_site(site)
            found = _SiteState(
                policy,
                AdaptiveRateLimiter(policy.rate_limit_per_minute, sleeper=self.sleeper, clock=self.clock),
            )
            self._states[site] = found
        return found

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            site: {
                "rate_factor": round(state.limiter.factor, 4),
                "rate_limit_per_minute": state.policy.rate_limit_per_minute,
                "effective_rate_limit_per_minute": int(state.limiter.effective_rpm),
            }
            for site, state in self._states.items()
        }

    # -- events ----------------------------------------------------------
    def _emit(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload.setdefault("at", iso_now())
        if self._step is not None:
            payload.setdefault("step", self._step)
        self._events.append(payload)

    def begin_step(
        self,
        step_index: int,
        override: SitePolicyOverride | Mapping[str, Any] | None = None,
        issues: tuple[str, ...] | list[str] = (),
    ) -> None:
        self._events = list(self._pending_restore)
        self._pending_restore = []
        self._step = step_index
        self._override = None
        if issues:
            self._emit(
                {
                    "type": "policy_adjusted",
                    "scope": "step",
                    "reason": "invalid_override_ignored",
                    "issues": list(issues),
                }
            )
            return
        if override is None:
            return
        if isinstance(override, SitePolicyOverride):
            parsed, problems = override, []
        else:
            parsed, problems = parse_policy(override)
        if parsed is None or problems:
            self._emit(
                {
                    "type": "policy_adjusted",
                    "scope": "step",
                    "reason": "invalid_override_ignored",
                    "issues": problems or ["invalid policy"],
                }
            )
            return
        self._override = parsed
        self._emit(
            {
                "type": "policy_adjusted",
                "scope": "step",
                "reason": "step_override",
                "override": parsed.as_dict(),
            }
        )

    def end_step(self) -> list[dict[str, Any]]:
        events, self._events = self._events, []
        self._step, self._override = None, None
        return events

    def take_events(self) -> list[dict[str, Any]]:
        events, self._events = self._events, []
        return events

    def note_retry(self, url: str | None, attempt: int, delay: float, error_class: str) -> None:
        """Record how the resolved policy shaped a retry backoff decision."""
        if self._step is None:
            return
        policy = self.current_policy(url)
        self._emit(
            {
                "type": "policy_event",
                "event": "retry_backoff",
                "scope": policy.source,
                "attempt": attempt,
                "delay_seconds": round(float(delay), 4),
                "error_class": error_class,
                "backoff_base_seconds": policy.backoff_base_seconds,
                "backoff_max_seconds": policy.backoff_max_seconds,
                "jitter": policy.jitter,
            }
        )

    # -- request hooks ---------------------------------------------------
    def current_policy(self, url: str | None) -> RequestPolicy:
        """Effective policy for the in-flight step (site + step override)."""
        site = site_from_url(url) if url else None
        if site is None:
            return self.registry.global_policy.apply(self._override)
        state = self._states.get(site)
        if state is None:
            state = self.state(site)
        return state.policy.apply(self._override)

    def before_request(self, url: str) -> float | None:
        """Pace the request and return a policy timeout, or ``None`` for the default."""
        site = site_from_url(url)
        if site is None:
            if self._override is not None:
                self._global_limiter.wait(self.registry.global_policy.apply(self._override).rate_limit_per_minute)
                return self.registry.global_policy.apply(self._override).timeout_seconds
            self._global_limiter.wait()
            return None
        state = self._states.get(site)
        if state is None:
            state = self.state(site)
        if self._override is None and state.limiter.factor >= 1.0:
            # Hot path: no step override and no active throttle. This performs
            # the same single bucket update as the original global limiter.
            state.limiter.wait()
            return state.policy.timeout_seconds if state.policy.source != "global" else None
        effective = state.policy.apply(self._override)
        state.limiter.wait(effective.rate_limit_per_minute)
        return effective.timeout_seconds if effective.source != "global" else None

    def note_response(self, url: str, status: int, retry_after: float | None = None) -> None:
        site = site_from_url(url)
        if site is None:
            return
        state = self.state(site)
        limiter = state.limiter
        if status == 429 or (status == 503 and retry_after is not None):
            before = limiter.factor
            after = max(_MIN_FACTOR, before * 0.5)
            limiter.set_factor(after, drain=True)
            state.successes = 0
            self._emit(
                {
                    "type": "limit_triggered",
                    "scope": "site",
                    "site": site,
                    "status": status,
                    "retry_after": retry_after,
                    "rate_factor": round(after, 4),
                    "rate_limit_per_minute": int(limiter.base_rpm),
                    "effective_rate_limit_per_minute": int(limiter.effective_rpm),
                }
            )
            self._emit(
                {
                    "type": "policy_adjusted",
                    "scope": "site",
                    "site": site,
                    "reason": "limit_response",
                    "trigger_status": status,
                    "before_rate_factor": round(before, 4),
                    "rate_factor": round(after, 4),
                    "rate_limit_per_minute": int(limiter.effective_rpm),
                }
            )
            return
        if 200 <= status < 400 and limiter.factor < 1.0:
            state.successes += 1
            if state.successes >= self.recovery_streak:
                before = limiter.factor
                after = min(1.0, before * _RECOVERY_STEP)
                limiter.set_factor(after)
                state.successes = 0
                self._emit(
                    {
                        "type": "policy_adjusted",
                        "scope": "site",
                        "site": site,
                        "reason": "success_recovery",
                        "before_rate_factor": round(before, 4),
                        "rate_factor": round(after, 4),
                        "rate_limit_per_minute": int(limiter.effective_rpm),
                    }
                )

    # -- resume support --------------------------------------------------
    def restore_from_ledger(self, ledger: Any, run_id: str) -> int:
        """Replay persisted site policy events so a resumed run keeps pacing."""
        try:
            events = ledger.list_step_events(run_id)
        except Exception:  # pragma: no cover - ledger compatibility guard
            return 0
        restored: dict[str, float] = {}
        for event in events:
            if event.get("scope") != "site":
                continue
            if event.get("type") == "policy_adjusted":
                site, factor = event.get("site"), event.get("rate_factor")
                if isinstance(site, str) and isinstance(factor, (int, float)) and not isinstance(factor, bool):
                    restored[site] = float(factor)
            elif event.get("type") == "limit_triggered":
                site = event.get("site")
                if isinstance(site, str):
                    restored.setdefault(site, 0.5)
        for site, factor in restored.items():
            state = self.state(site)
            state.limiter.set_factor(factor)
            state.successes = 0
        if restored:
            self._pending_restore.append(
                {
                    "type": "policy_adjusted",
                    "scope": "site",
                    "reason": "resume_restore",
                    "sites": sorted(restored),
                    "rate_factors": {key: round(value, 4) for key, value in sorted(restored.items())},
                }
            )
        return len(restored)

    def policy_snapshot(self) -> dict[str, Any]:
        return {
            "configured_sites": self.registry.configured_sites,
            "warnings": list(self.registry.warnings),
            "sites": self.snapshot(),
        }
