"""Action-script execution engine with interpolation, retry and on_error."""
from __future__ import annotations
import os
import re
import time
from dataclasses import replace
from typing import Any, Callable, Mapping
from fillbot.browser.base import BrowserSession, Locator, Page
from fillbot.dsl.actions import ACTION_HANDLERS, ActionContext
from fillbot.dsl.schema import Script, Step, available_actions
from fillbot.errors import BotError, ScriptError, UndefinedVariableError, UnregisteredActionError
from fillbot.models import EngineResult, StepResult, StepStatus, TaskStatus, iso_now
from fillbot.runner.retry import RetryPolicy, run_with_retry
_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)\s*\}\}")
class DSLExecutor:
    def __init__(
        self,
        script: Script,
        browser: BrowserSession,
        *,
        task_context: Mapping[str, Any],
        run_id: str,
        env: Mapping[str, str] | None = None,
        default_retry: RetryPolicy | None = None,
        snapshot_cb: Callable[[Step, Page, StepStatus], None] | None = None,
        action_log_cb: Callable[[dict[str, Any]], None] | None = None,
        retry_cb: Callable[[Step, int, BotError, float], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        governor: Any | None = None,
    ) -> None:
        self.script, self.browser = script, browser
        self.task_context, self.run_id = dict(task_context), run_id
        self.env = dict(os.environ if env is None else env)
        self.default_retry = default_retry or RetryPolicy()
        self.snapshot_cb, self.action_log_cb, self.retry_cb, self.sleeper = (
            snapshot_cb,
            action_log_cb,
            retry_cb,
            sleeper,
        )
        self.governor = governor
        self.variables: dict[str, Any] = {}
    def _lookup(self, expression: str, step_no: int) -> Any:
        parts = expression.split(".")
        sources: dict[str, Any] = {
            "task": self.task_context,
            "env": self.env,
            "var": self.variables,
            "run": {"id": self.run_id},
        }
        if parts[0] not in sources:
            raise UndefinedVariableError(
                f"step {step_no}: undefined variable namespace {parts[0]!r} in {{{{{expression}}}}}"
            )
        source = sources[parts[0]]
        for part in parts[1:]:
            if isinstance(source, Mapping) and part in source:
                source = source[part]
            else:
                raise UndefinedVariableError(f"step {step_no}: undefined variable {{{{{expression}}}}}")
        return source
    def interpolate(self, value: Any, step_no: int) -> Any:
        if isinstance(value, str):
            whole = _VAR.fullmatch(value.strip())
            if whole:
                return self._lookup(whole.group(1), step_no)
            return _VAR.sub(lambda match: str(self._lookup(match.group(1), step_no)), value)
        if isinstance(value, list):
            return [self.interpolate(item, step_no) for item in value]
        if isinstance(value, dict):
            return {key: self.interpolate(item, step_no) for key, item in value.items()}
        return value
    def _resolved_step(self, step: Step) -> Step:
        params = self.interpolate(step.params, step.index)
        locator = None
        if step.locator is not None:
            locator = Locator(
                css=self.interpolate(step.locator.css, step.index) if step.locator.css else None,
                label=self.interpolate(step.locator.label, step.index) if step.locator.label else None,
            )
        return replace(step, params=params, locator=locator, timeout=self._resolved_timeout(step))

    def _resolved_timeout(self, step: Step) -> float | None:
        """Effective step timeout: explicit ``timeout`` > step policy > site policy."""
        if step.timeout_explicit:
            return step.timeout
        policy = step.policy.get("timeout_seconds") if step.policy else None
        if policy is not None:
            return float(policy)
        if self.governor is None:
            return None
        site = self.governor.current_policy(self._step_url(step))
        return site.timeout_seconds if site.source != "global" else None

    def _step_url(self, step: Step) -> str | None:
        """The URL a step will most likely talk to (used for site resolution)."""
        target = step.params.get("url") if step.action == "open" else None
        if isinstance(target, str):
            return target
        page = self._safe_page()
        return page.url if page is not None else None

    def _policy_for(self, step: Step) -> RetryPolicy:
        base = self.default_retry
        governor = self.governor
        if governor is not None:
            # Resolve the step's target site so site and step overrides
            # contribute backoff/attempts before any retry runs.
            site = governor.current_policy(self._step_url(step))
            if not step.retry_explicit:
                return RetryPolicy(
                    max(1, site.max_retries + 1),
                    site.backoff_base_seconds,
                    max(site.backoff_max_seconds, site.backoff_base_seconds),
                    site.jitter,
                    base.rng,
                    base.sleeper,
                )
        if not step.retry_explicit:
            return base
        jitter = base.jitter
        if self.governor is not None:
            jitter = self.governor.current_policy(self._step_url(step)).jitter
        return RetryPolicy(
            step.retry.attempts,
            step.retry.base_seconds,
            max(step.retry.max_seconds, step.retry.base_seconds),
            jitter,
            base.rng,
            base.sleeper,
        )

    def _retry_delay(self, policy: RetryPolicy, step: Step) -> Callable[[int, BotError], float] | None:
        """Per-site jitter for explicit step retry specs."""
        governor = self.governor
        if governor is None or not step.retry_explicit:
            return None

        def compute(attempt: int, error: BotError) -> float:
            site = governor.current_policy(self._step_url(step))
            return policy.compute_delay(attempt, error, jitter=site.jitter)

        return compute
    def _safe_page(self, error: BotError | None = None) -> Page | None:
        if error is not None and error.page is not None:
            return error.page
        try:
            return self.browser.page
        except Exception:
            return None
    def _record(
        self,
        step: Step,
        status: StepStatus,
        started_at: str,
        clock: float,
        attempts: int,
        error: BotError | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> StepResult:
        duration = int((time.monotonic() - clock) * 1000)
        result = StepResult(
            step.index,
            step.action,
            status,
            started_at,
            iso_now(),
            duration,
            attempts,
            error.category.value if error else None,
            error.message if error else None,
            policy_events=list(events or []),
        )
        page = self._safe_page(error)
        if page is not None and self.snapshot_cb:
            self.snapshot_cb(step, page, status)
        if self.action_log_cb:
            self.action_log_cb(
                {
                    "step": step.index,
                    "action": step.action,
                    "locator": step.locator.describe() if step.locator else "",
                    "result": status.value,
                    "started_at": started_at,
                    "duration_ms": duration,
                    "attempts": attempts,
                    "error_class": error.category.value if error else None,
                    "error": error.message if error else None,
                }
            )
        return result

    def _step_events(self) -> list[dict[str, Any]]:
        return self.governor.end_step() if self.governor is not None else []

    def _run_step(self, step: Step) -> tuple[StepStatus, None, int]:
        if step.action not in ACTION_HANDLERS:
            raise UnregisteredActionError(
                f"step {step.index}: no handler for action {step.action!r}; available actions: {', '.join(available_actions())}"
            )
        handler = ACTION_HANDLERS[step.action]
        context = ActionContext(self.browser, self.variables, self.task_context, self.run_id, self.sleeper)
        attempts = [0]
        def invoke() -> Any:
            attempts[0] += 1
            return handler(context, step)
        def callback(attempt: int, error: BotError, delay: float) -> None:
            if self.governor is not None:
                self.governor.note_retry(self._step_url(step), attempt, delay, error.category.value)
            if self.retry_cb:
                self.retry_cb(step, attempt, error, delay)
        policy = self._policy_for(step)
        try:
            outcome = run_with_retry(
                invoke,
                policy,
                on_retry=callback,
                delay_for=self._retry_delay(policy, step),
            )
        except BotError as error:
            error.details.setdefault("attempts", attempts[0])
            raise
        if step.action == "extract":
            self.variables[str(step.params["save_as"])] = outcome.value
        return StepStatus.OK, None, outcome.attempts
    def execute(self, *, dry_run: bool = False) -> EngineResult:
        results: list[StepResult] = []
        for original in self.script.steps:
            started_at, clock = iso_now(), time.monotonic()
            try:
                step = self._resolved_step(original)
            except BotError as error:
                results.append(self._record(original, StepStatus.FAILED, started_at, clock, 0, error))
                return EngineResult(
                    TaskStatus.FAILED, dict(self.variables), results, error.message, error.category.value
                )
            if self.governor is not None:
                self.governor.begin_step(step.index, step.policy, step.policy_issues)
            if dry_run and step.final:
                results.append(
                    self._record(step, StepStatus.SKIPPED, started_at, clock, 0, events=self._step_events())
                )
                return EngineResult(TaskStatus.DRY_RUN, dict(self.variables), results)
            try:
                status, _, attempts = self._run_step(step)
            except BotError as caught:
                attempts = int(caught.details.get("attempts", 1))
                chosen = StepStatus.SKIPPED if step.on_error == "skip" else StepStatus.FAILED
                results.append(self._record(step, chosen, started_at, clock, attempts, caught, events=self._step_events()))
                if step.on_error in {"skip", "continue"}:
                    continue
                return EngineResult(
                    TaskStatus.FAILED, dict(self.variables), results, caught.message, caught.category.value
                )
            results.append(self._record(step, status, started_at, clock, attempts, events=self._step_events()))
        receipt = self.variables.get("receipt_no")
        return EngineResult(
            TaskStatus.SUCCESS, dict(self.variables), results, receipt_no=str(receipt) if receipt is not None else None
        )
