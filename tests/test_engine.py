from __future__ import annotations
import random
from pathlib import Path
from typing import Any
import pytest
from fillbot.browser.base import BrowserSession, Locator, Page
from fillbot.dsl.engine import DSLExecutor
from fillbot.dsl.schema import Script, Step, validate_script
from fillbot.errors import ClientError, NetworkError, ScriptError, ServerError
from fillbot.models import StepStatus, TaskStatus
from fillbot.runner.retry import RetryChain, RetryLayer, RetryPolicy
FORM = """
<html><body><h1>填报表单</h1><label for="full_name">姓名</label><input id="full_name" name="full_name">
<select id="color" name="color"><option value="green">绿</option></select>
<label for="agree">同意</label><input id="agree" type="checkbox"><input id="file" type="file">
<button id="submit" type="submit">提交</button></body></html>
"""
RECEIPT = '<html><body><h1>报送回执</h1><span id="receipt">RCP-20260101-0001</span></body></html>'
class FakeBrowser(BrowserSession):
    def __init__(self, *, fail_clicks: int = 0, client_error: bool = False, network_error: bool = False) -> None:
        self.current = Page(200, "http://start/", {}, "<html>start</html>")
        self.fills: list[tuple[Locator, str]] = []
        self.selects: list[tuple[Locator, str]] = []
        self.checks: list[tuple[Locator, bool]] = []
        self.uploads: list[tuple[Locator, Path]] = []
        self.opened: list[str] = []
        self.clicks, self.fail_clicks, self.client_error, self.closed = 0, fail_clicks, client_error, False
        self.network_error = network_error
    @property
    def page(self) -> Page:
        return self.current
    def open(self, url: str, **kwargs: object) -> Page:
        self.opened.append(url)
        self.current = Page(200, url, {}, FORM)
        return self.current
    def fill(self, locator: Locator, value: str) -> None:
        self.fills.append((locator, value))
    def select(self, locator: Locator, value: str) -> None:
        self.selects.append((locator, value))
    def check(self, locator: Locator, checked: bool = True) -> None:
        self.checks.append((locator, checked))
    def upload(self, locator: Locator, path: Path) -> None:
        self.uploads.append((locator, path))
    def click(self, locator: Locator, **kwargs: object) -> Page:
        self.clicks += 1
        if self.clicks <= self.fail_clicks:
            if self.client_error:
                raise ClientError("bad request", status=400)
            if self.network_error:
                raise NetworkError("timeout after 0.01s: POST http://start/submit")
            raise ServerError("transient", status=500)
        self.current = Page(200, "http://start/receipt", {}, RECEIPT)
        return self.current
    def close(self) -> None:
        self.closed = True
def _execute(steps: list[dict[str, Any]], **options: Any) -> tuple[Any, FakeBrowser, list[float], list[dict]]:
    browser = options.get("browser") or FakeBrowser()
    sleeps, logs = [], []
    executor = DSLExecutor(
        validate_script({"name": "engine-test", "version": 1, "steps": steps}),
        browser,
        task_context=options.get("context", {"name": "Ada", "password": "mock-only-secret"}),
        run_id="RUN-123",
        env=options.get("env", {"BASE": "http://start"}),
        default_retry=RetryPolicy(3, 0.01, 0.02, False, rng=random.Random(4), sleeper=sleeps.append),
        snapshot_cb=lambda step, page, status: (options["snaps"] if "snaps" in options else []).append(status),
        action_log_cb=(options["sink"] if "sink" in options else logs).append,
        sleeper=sleeps.append,
    )
    return executor.execute(dry_run=options.get("dry_run", False)), browser, sleeps, options.get("sink") or logs
def _all_actions(tmp_path: Path) -> list[dict[str, Any]]:
    attachment = tmp_path / "upload.pdf"
    attachment.write_bytes(b"%PDF-fixture")
    return [
        {"action": "open", "url": "{{env.BASE}}/{{run.id}}"},
        {"action": "fill", "label": "姓名", "value": "{{task.name}}"},
        {"action": "select", "selector": "#color", "value": "green"},
        {"action": "check", "label": "同意", "checked": True},
        {"action": "upload", "selector": "#file", "path": str(attachment)},
        {"action": "click", "selector": "#submit"},
        {"action": "wait", "seconds": 0},
        {"action": "assert_text", "text": "报送回执"},
        {"action": "assert_status", "status": 200},
        {"action": "extract", "selector": "#receipt", "save_as": "receipt_no"},
        {"action": "set_var", "name": "note", "value": "done {{task.name}}"},
    ]
def test_every_action_and_interpolation(tmp_path: Path) -> None:
    snaps: list[StepStatus] = []
    result, browser, sleeps, logs = _execute(_all_actions(tmp_path), snaps=snaps)
    assert result.status == TaskStatus.SUCCESS and len(result.steps) == len(logs) == 11
    assert browser.opened == ["http://start/RUN-123"] and browser.fills[0][1] == "Ada"
    assert browser.selects[0][1] == "green" and browser.checks[0][1] is True and browser.clicks == 1
    assert browser.uploads[0][1].name == "upload.pdf" and sleeps == [0]
    assert result.variables["receipt_no"] == "RCP-20260101-0001" and result.variables["note"] == "done Ada"
    assert all(step.status == StepStatus.OK for step in result.steps) and snaps == [StepStatus.OK] * 11
    assert all("fill" not in str(item.get("action", "")) or "value" not in item for item in logs)
    typed, typed_browser, _, _ = _execute(
        [
            {"action": "set_var", "name": "number", "value": "{{task.employees}}"},
            {"action": "fill", "selector": "#n", "value": "{{var.number}}"},
            {"action": "fill", "selector": "#m", "value": "count={{task.employees}}"},
        ],
        context={"name": "Ada", "employees": 42},
    )
    assert typed.status == TaskStatus.SUCCESS and isinstance(typed.variables["number"], int)
    assert [value for _, value in typed_browser.fills] == ["42", "count=42"]
def test_interpolation_errors() -> None:
    cases = [
        ([{"action": "open", "url": "http://x"}, {"action": "fill", "selector": "#a", "value": "{{task.nope}}"}], "task.nope"),
        ([{"action": "fill", "selector": "#a", "value": "{{wat.x}}"}], "namespace"),
        ([{"action": "fill", "selector": "#a", "value": "{{env.MISSING}}"}], "env.MISSING"),
    ]
    for steps, fragment in cases:
        result, _, _, _ = _execute(steps, env={})
        assert result.status == TaskStatus.FAILED and result.error_class == "SCRIPT" and fragment in (result.error or "")
    assert "step 2" in (cases[0] and _execute(cases[0][0])[0].error or "")
@pytest.mark.parametrize(
    "policy, expected_status, expected_step",
    [("fail", TaskStatus.FAILED, StepStatus.FAILED), ("skip", TaskStatus.SUCCESS, StepStatus.SKIPPED), ("continue", TaskStatus.SUCCESS, StepStatus.FAILED)],
)
def test_on_error_strategies(policy: str, expected_status: TaskStatus, expected_step: StepStatus) -> None:
    class Browser(FakeBrowser):
        def fill(self, locator: Locator, value: str) -> None:
            if value == "boom":
                raise ScriptError("boom failure")
            super().fill(locator, value)
    result, browser, _, _ = _execute(
        [{"action": "fill", "selector": "#a", "value": "boom", "on_error": policy}, {"action": "wait", "seconds": 0}],
        browser=Browser(),
    )
    assert result.status == expected_status and result.steps[0].status == expected_step
    assert (browser.clicks, len(browser.fills)) == (0, 0)
def test_unregistered_handler_dry_run_and_retry() -> None:
    bad = DSLExecutor(
        Script(name="bad", version=1, steps=(Step(index=1, action="bogus"),)), FakeBrowser(), task_context={}, run_id="x"
    )
    result = bad.execute()
    assert result.status == TaskStatus.FAILED and "available actions" in (result.error or "")
    dry, fake, _, _ = _execute(
        [{"action": "open", "url": "http://x"}, {"action": "click", "selector": "#submit", "final": True}, {"action": "wait", "seconds": 0}],
        dry_run=True,
    )
    assert dry.status == TaskStatus.DRY_RUN and fake.clicks == 0 and len(dry.steps) == 2
    assert dry.steps[-1].status == StepStatus.SKIPPED
    # Step layer retries transient NETWORK errors per the YAML retry option.
    retried, retry_browser, sleeps, _ = _execute(
        [
            {"action": "open", "url": "http://x"},
            {"action": "click", "selector": "#submit", "retry": {"attempts": 3, "base_seconds": 0.01}},
        ],
        browser=FakeBrowser(fail_clicks=1, network_error=True),
    )
    assert retried.status == TaskStatus.SUCCESS and retried.steps[1].attempts == 2 and retry_browser.clicks == 2
    assert len(sleeps) == 1
    # Integer retry form stays valid; exhausted step attempts surface the error.
    exhausted, _, _, _ = _execute(
        [{"action": "click", "selector": "#submit", "retry": 2}],
        browser=FakeBrowser(fail_clicks=5, network_error=True),
    )
    assert exhausted.status == TaskStatus.FAILED and exhausted.steps[0].attempts == 2
    assert exhausted.error_class == "NETWORK"
    # SERVER failures are not retried at step level any more: they escalate to
    # the task layer, so the click happens exactly once and no delay is slept.
    escalated, server_browser, server_sleeps, _ = _execute(
        [{"action": "open", "url": "http://x"}, {"action": "click", "selector": "#submit", "retry": 3}],
        browser=FakeBrowser(fail_clicks=5),
    )
    assert escalated.status == TaskStatus.FAILED and escalated.error_class == "SERVER"
    assert escalated.steps[1].attempts == 1 and server_browser.clicks == 1 and server_sleeps == []
    # A failing final click is flagged so the scheduler can gate task replays.
    final_fail, _, _, _ = _execute(
        [
            {"action": "open", "url": "http://x"},
            {"action": "click", "selector": "#submit", "final": True, "retry": 3},
        ],
        browser=FakeBrowser(fail_clicks=5),
    )
    assert final_fail.status == TaskStatus.FAILED and final_fail.final_failure is True
    client, _, client_sleeps, _ = _execute(
        [{"action": "click", "selector": "#submit"}], browser=FakeBrowser(fail_clicks=5, client_error=True)
    )
    assert client.status == TaskStatus.FAILED and client.error_class == "CLIENT" and client_sleeps == []


def test_retry_chain_honours_retry_after_once() -> None:
    chain = RetryChain()
    step_policy = RetryPolicy(3, 0.1, 1.0, jitter=False, layer=RetryLayer.STEP, sleeper=lambda _: None)
    task_policy = RetryPolicy(3, 0.1, 1.0, jitter=False, layer=RetryLayer.TASK, sleeper=lambda _: None)
    limited = ServerError("rate limited", status=429, retry_after=3.0)
    # First encounter on the shared chain consumes Retry-After (3.0s)...
    assert step_policy.compute_delay(1, limited, chain) == 3.0
    # ...and a later layer on the same chain must not wait it again.
    later = task_policy.compute_delay(1, limited, chain)
    assert later == 0.1
    # Without a chain the standalone historical behaviour is preserved.
    assert step_policy.compute_delay(1, limited) == 3.0
    # Categories are disjoint: STEP declines SERVER, TASK declines NETWORK.
    assert step_policy.allows(NetworkError("timeout")) is True
    assert step_policy.allows(limited) is False
    assert task_policy.allows(limited) is True
    assert task_policy.allows(NetworkError("timeout")) is False
