"""Deterministic, opt-in fault injection for resilience testing."""
from __future__ import annotations
import os
import random
import time
from dataclasses import dataclass
from typing import Mapping
def _flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
def _value(env: Mapping[str, str], name: str, default: str, cast):
    try:
        return cast(env.get(name, default))
    except (TypeError, ValueError):
        return cast(default)
@dataclass(frozen=True)
class ChaosSettings:
    enabled: bool = False
    seed: int = 7
    failure_rate: float = 0.0
    delay_ms: int = 0
    retry_after: int = 1
    flaky_submit: bool = False
    flaky_company: str = "C003"
    expire_session: bool = False
    invalid_csrf_once: bool = False
    login_failure: bool = False
    attachment_limit_once: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ChaosSettings":
        env = os.environ if environ is None else environ
        flaky = _flag(env.get("FILLBOT_CHAOS_FLAKY_SUBMIT", "0"))
        consequence_flags = (
            "FILLBOT_CHAOS_EXPIRE_SESSION",
            "FILLBOT_CHAOS_INVALID_CSRF",
            "FILLBOT_CHAOS_LOGIN_FAILURE",
            "FILLBOT_CHAOS_ATTACHMENT_LIMIT",
        )
        return cls(
            enabled=_flag(env.get("FILLBOT_CHAOS", "0"))
            or flaky
            or any(_flag(env.get(name, "0")) for name in consequence_flags),
            seed=_value(env, "FILLBOT_CHAOS_SEED", "7", int),
            failure_rate=max(0.0, min(1.0, _value(env, "FILLBOT_CHAOS_RATE", "0.0", float))),
            delay_ms=max(0, _value(env, "FILLBOT_CHAOS_DELAY_MS", "0", int)),
            retry_after=max(0, _value(env, "FILLBOT_CHAOS_RETRY_AFTER", "1", int)),
            flaky_submit=flaky,
            flaky_company=env.get("FILLBOT_CHAOS_FLAKY_COMPANY", "C003").strip(),
            expire_session=_flag(env.get("FILLBOT_CHAOS_EXPIRE_SESSION", "0")),
            invalid_csrf_once=_flag(env.get("FILLBOT_CHAOS_INVALID_CSRF", "0")),
            login_failure=_flag(env.get("FILLBOT_CHAOS_LOGIN_FAILURE", "0")),
            attachment_limit_once=_flag(env.get("FILLBOT_CHAOS_ATTACHMENT_LIMIT", "0")),
        )


@dataclass(frozen=True)
class ChaosDecision:
    status: int | None = None
    retry_after: int | None = None
    delay_seconds: float = 0.0


class ChaosController:
    def __init__(self, settings: ChaosSettings) -> None:
        self.settings = settings
        self.rng = random.Random(settings.seed)
        self.flaky_fired = False
        self.csrf_fired = False
        self.attachment_fired = False

    def decide(self, method: str, path: str, company_id: str | None = None) -> ChaosDecision:
        if not self.settings.enabled or path.startswith(("/__mock", "/static")):
            return ChaosDecision()
        delay = self.rng.randint(0, self.settings.delay_ms) / 1000.0 if self.settings.delay_ms else 0.0
        company_match = not self.settings.flaky_company or self.settings.flaky_company == (company_id or "")
        if (
            self.settings.flaky_submit
            and company_match
            and not self.flaky_fired
            and method.upper() == "POST"
            and path == "/report/submit"
        ):
            self.flaky_fired = True
            return ChaosDecision(status=500, delay_seconds=delay)
        if self.settings.failure_rate > 0 and self.rng.random() < self.settings.failure_rate:
            status = self.rng.choice((500, 429))
            return ChaosDecision(status, self.settings.retry_after if status == 429 else None, delay)
        return ChaosDecision(delay_seconds=delay)

    def apply_delay(self, decision: ChaosDecision) -> None:
        if decision.delay_seconds > 0:
            time.sleep(decision.delay_seconds)

    def should_expire_session(self, method: str, path: str) -> bool:
        return (
            self.settings.enabled
            and self.settings.expire_session
            and method.upper() == "POST"
            and path in {"/report/step2", "/report/step3", "/report/submit"}
        )

    def should_break_csrf(self, path: str = "") -> bool:
        if not (self.settings.enabled and self.settings.invalid_csrf_once) or self.csrf_fired:
            return False
        if path and path not in {"/report/step1", "/report/step2", "/report/step3", "/report/submit"}:
            return False
        self.csrf_fired = True
        return True

    def should_fail_login(self) -> bool:
        return self.settings.enabled and self.settings.login_failure

    def should_reject_attachment(self) -> bool:
        if not (self.settings.enabled and self.settings.attachment_limit_once) or self.attachment_fired:
            return False
        self.attachment_fired = True
        return True
