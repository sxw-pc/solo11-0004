"""Built-in action implementations, independent of any concrete browser."""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from fillbot.browser.base import BrowserSession, Page
from fillbot.dsl import selectors
from fillbot.dsl.schema import Step
from fillbot.errors import AssertionFailure, ScriptError
@dataclass
class ActionContext:
    browser: BrowserSession
    variables: dict[str, Any] = field(default_factory=dict)
    task_context: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    sleeper: Callable[[float], None] = time.sleep
    @property
    def page(self) -> Page:
        return self.browser.page
ActionHandler = Callable[[ActionContext, Step], Any]
def _page(context: ActionContext) -> Page:
    try:
        return context.page
    except Exception as exc:  # pragma: no cover - adapter contract
        raise ScriptError("no page loaded; run an open action first") from exc
def action_open(context: ActionContext, step: Step) -> None:
    context.browser.open(str(step.params["url"]), timeout=step.timeout)
def action_fill(context: ActionContext, step: Step) -> None:
    assert step.locator is not None
    context.browser.fill(step.locator, str(step.params["value"]))
def action_select(context: ActionContext, step: Step) -> None:
    assert step.locator is not None
    context.browser.select(step.locator, str(step.params["value"]))
def action_check(context: ActionContext, step: Step) -> None:
    assert step.locator is not None
    context.browser.check(step.locator, bool(step.params.get("checked", True)))
def action_upload(context: ActionContext, step: Step) -> None:
    assert step.locator is not None
    path = Path(str(step.params["path"]))
    if not path.is_file():
        raise ScriptError(f"upload file not found: {path}")
    context.browser.upload(step.locator, path)
def action_click(context: ActionContext, step: Step) -> None:
    assert step.locator is not None
    context.browser.click(step.locator, timeout=step.timeout)
def action_wait(context: ActionContext, step: Step) -> None:
    seconds = float(step.params["seconds"])
    if seconds > 300:
        raise ScriptError("wait.seconds cannot exceed 300")
    context.sleeper(seconds)
def action_assert_text(context: ActionContext, step: Step) -> None:
    expected = str(step.params["text"])
    if not selectors.text_present(_page(context).text, expected, step.locator):
        raise AssertionFailure(
            f"expected text {expected!r} was not found; page starts with {selectors.normalized_text(_page(context).text)[:160]!r}"
        )
def action_assert_status(context: ActionContext, step: Step) -> None:
    page, expected = _page(context), int(step.params["status"])
    if page.status != expected:
        raise AssertionFailure(f"expected HTTP status {expected}, got {page.status} at {page.url}")
def action_extract(context: ActionContext, step: Step) -> str:
    assert step.locator is not None
    attr = step.params.get("attr")
    return selectors.extract_text(_page(context).text, step.locator, str(attr) if attr else None)
def action_set_var(context: ActionContext, step: Step) -> None:
    context.variables[str(step.params["name"])] = step.params.get("value")
ACTION_HANDLERS: dict[str, ActionHandler] = {
    "open": action_open,
    "fill": action_fill,
    "select": action_select,
    "check": action_check,
    "upload": action_upload,
    "click": action_click,
    "wait": action_wait,
    "assert_text": action_assert_text,
    "assert_status": action_assert_status,
    "extract": action_extract,
    "set_var": action_set_var,
}
