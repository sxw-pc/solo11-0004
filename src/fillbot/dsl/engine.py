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
from fillbot.runner.retry import RetryChain, RetryLayer, RetryPolicy, run_with_retry
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
        retry_chain: RetryChain | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.script, self.browser = script, browser
        self.task_context, self.run_id = dict(task_context), run_id
        self.env = dict(os.environ if env is None else env)
        self.default_retry = default_retry or RetryPolicy(layer=RetryLayer.STEP)
        self.snapshot_cb, self.action_log_cb, self.retry_cb, self.sleeper = (
            snapshot_cb,
            action_log_cb,
            retry_cb,
            sleeper,
        )
        # One chain per task execution: a Retry-After header honoured by a step
        # retry is not honoured again when the task-level layer replays.
        self.retry_chain = retry_chain or RetryChain()
        self.variables: dict[str, Any] = {}
        # Set when a final click step is the failing step; the scheduler uses
        # this to gate whole-task replays behind the ledger idempotency key.
        self.final_failure = False
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
        return replace(step, params=params, locator=locator)
    def _policy_for(self, step: Step) -> RetryPolicy:
        base = self.default_retry
        layer = RetryLayer.STEP
        if not step.retry_explicit:
            return RetryPolicy(
                base.max_attempts,
                base.base_seconds,
                base.max_seconds,
                base.jitter,
                layer,
                base.rng,
                base.sleeper,
            )
        return RetryPolicy(
            step.retry.attempts,
            step.retry.base_seconds,
            max(step.retry.max_seconds, step.retry.base_seconds),
            base.jitter,
            layer,
            base.rng,
            base.sleeper,
        )
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
            if self.retry_cb:
                self.retry_cb(step, attempt, error, delay)
        try:
            outcome = run_with_retry(invoke, self._policy_for(step), on_retry=callback, chain=self.retry_chain)
        except BotError as error:
            error.details.setdefault("attempts", attempts[0])
            if step.final:
                self.final_failure = True
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
            if dry_run and step.final:
                results.append(self._record(step, StepStatus.SKIPPED, started_at, clock, 0))
                return EngineResult(TaskStatus.DRY_RUN, dict(self.variables), results)
            try:
                status, _, attempts = self._run_step(step)
            except BotError as caught:
                attempts = int(caught.details.get("attempts", 1))
                chosen = StepStatus.SKIPPED if step.on_error == "skip" else StepStatus.FAILED
                results.append(self._record(step, chosen, started_at, clock, attempts, caught))
                if step.on_error in {"skip", "continue"}:
                    continue
                return EngineResult(
                    TaskStatus.FAILED,
                    dict(self.variables),
                    results,
                    caught.message,
                    caught.category.value,
                    final_failure=self.final_failure,
                    retry_after=caught.retry_after,
                )
            results.append(self._record(step, status, started_at, clock, attempts))
        receipt = self.variables.get("receipt_no")
        return EngineResult(
            TaskStatus.SUCCESS, dict(self.variables), results, receipt_no=str(receipt) if receipt is not None else None
        )
