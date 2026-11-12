from __future__ import annotations
import csv
import json
import re
from pathlib import Path
from typing import Any, Callable
import pytest
import yaml
from flask import Flask
from fillbot.cli import main
from fillbot.config import BotConfig, load_config
from fillbot.dsl.schema import load_script
from fillbot.errors import ConfigError, NetworkError, ServerError
from fillbot.intake import load_and_validate_tasks
from fillbot.ledger.db import Ledger
from fillbot.mocksite.chaos import ChaosSettings
from fillbot.runner.scheduler import Runner
ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "quarterly_report.yaml"
VALID_TASK = ROOT / "tests" / "fixtures" / "valid_task.json"
ATTACHMENT = ROOT / "tests" / "fixtures" / "attachment.pdf"
RECEIPT_RE = re.compile(r"^RCP-\d{8}-\d{4}$")
def _load() -> tuple[list[Any], Any]:
    return load_and_validate_tasks(ROOT / "tasks" / "example_batch.json"), load_script(SCRIPT)
def _write_config(tmp_path: Path, base_url: str) -> Path:
    path = tmp_path / "bot.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "base_url": base_url,
                "rate_limit_per_minute": 6000,
                "timeout_seconds": 5,
                "backoff_base_seconds": 0.01,
                "backoff_max_seconds": 0.05,
                "evidence_dir": str(tmp_path / "evidence"),
                "ledger_path": str(tmp_path / "data" / "ledger.db"),
                "runs_dir": str(tmp_path / "runs"),
            }
        )
    )
    return path
def test_mixed_batch_evidence_idempotency_and_summary(serve: Callable[..., Any], make_config: Callable[..., Any]) -> None:
    live = serve(chaos=ChaosSettings(enabled=True, flaky_submit=True, flaky_company="C003"))
    config = make_config(live.base_url)
    tasks, script = _load()
    runner = Runner(config)
    run = runner.run(tasks, script, task_source="tasks/example_batch.json", script_source="scripts/quarterly_report.yaml")
    # C003's final click gets a 500. With no idempotency key registered the
    # task layer refuses to replay the submission, so the task fails without a
    # duplicate POST; only the two unaffected tasks succeed.
    assert (run.status.value, run.succeeded, run.failed, run.skipped) == ("failed", 1, 2, 0)
    # Only the one unaffected task adds a receipt (plus the store's seed).
    assert len(live.receipts) == 2
    rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(run.run_id)}
    assert rows["T-1002"]["error_class"] == "CLIENT" and RECEIPT_RE.fullmatch(rows["T-1001"]["receipt_no"])
    assert rows["T-1003"]["error_class"] == "SERVER" and rows["T-1003"]["receipt_no"] is None
    # The final step ran exactly once: SERVER errors escalate instead of
    # retrying inside the step, and the task layer did not replay.
    final = next(item for item in runner.ledger.list_steps(run.run_id, "T-1003") if item["step_index"] == 26)
    assert final["status"] == "failed" and final["attempts"] == 1 and final["error_class"] == "SERVER"
    bad_step = next(item for item in runner.ledger.list_steps(run.run_id, "T-1002") if item["status"] == "failed")
    assert bad_step["error_class"] == "CLIENT"
    summary_dir = config.runs_dir / run.run_id
    data = json.loads((summary_dir / "summary.json").read_text(encoding="utf-8"))
    assert (data["tasks_total"], data["succeeded"], data["failed"], data["skipped"]) == (3, 1, 2, 0)
    assert data["failure_reasons"] == {"CLIENT": 1, "SERVER": 1}
    with (summary_dir / "summary.csv").open(encoding="utf-8", newline="") as handle:
        csv_values = dict(csv.reader(handle))
    assert csv_values["failure_reason:CLIENT"] == "1" and (summary_dir / "run.log").is_file()
    evidence = config.evidence_dir / run.run_id
    assert (evidence / "T-1001" / "receipt.html").is_file() and not (evidence / "T-1002" / "receipt.html").exists()
    combined = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in evidence.rglob("*") if path.is_file())
    assert "mock-only-beta" not in combined and 'value="***"' in combined
    # A second run against a site whose submit still 500s must keep failing
    # the final step rather than replaying blind (fresh app -> fault re-armed).
    live2 = serve(chaos=ChaosSettings(enabled=True, flaky_submit=True, flaky_company="C003"))
    config2 = make_config(live2.base_url, ledger_path=config.ledger_path, evidence_dir=config.evidence_dir, runs_dir=config.runs_dir)
    second = Runner(config2).run(tasks, script, task_source="tasks/example_batch.json", script_source="scripts/quarterly_report.yaml")
    second_rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(second.run_id)}
    assert [second_rows[key]["status"] for key in ("T-1001", "T-1002", "T-1003")] == ["skipped", "failed", "failed"]
    # The fresh app only carries its own seed receipt; C003 still did not write.
    assert len(live2.receipts) == 1


class _FlakySubmitState:
    """Shared, cross-browser-instance state for the injected submit fault."""

    FINAL_SELECTOR = "form#confirm-form button[type=submit]"

    def __init__(self, error_factory: Callable[[], Any]) -> None:
        self._error_factory = error_factory
        self.submit_posts = 0
        self.unlocked = False

    def click(self, browser: Any, locator: Any, **kwargs: Any) -> Any:
        if getattr(locator, "css", None) == self.FINAL_SELECTOR:
            self.submit_posts += 1
            if not self.unlocked:
                raise self._error_factory()
        return browser.click(locator, **kwargs)


class _FlakySubmitBrowser:
    """Browser wrapper delegating final-submit behaviour to shared state."""

    def __init__(self, inner: Any, state: _FlakySubmitState) -> None:
        self._inner, self._state = inner, state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def click(self, locator: Any, **kwargs: Any) -> Any:
        return self._state.click(self._inner, locator, **kwargs)


def test_final_step_replays_only_with_idempotency_key(serve: Callable[..., Any], make_config: Callable[..., Any]) -> None:
    live = serve()
    config = make_config(live.base_url, backoff_base_seconds=0.01, backoff_max_seconds=0.02, max_retries=1)
    loaded, script = _load()
    c003 = next(task for task in loaded if task.payload["company_id"] == "C003")
    state = _FlakySubmitState(lambda: ServerError("submit unavailable", status=503))
    def factory() -> Any:
        from fillbot.browser.mechanical import MechanicalBrowser
        return _FlakySubmitBrowser(
            MechanicalBrowser(timeout=config.timeout_seconds, follow_redirects=config.follow_redirects),
            state,
        )
    runner = Runner(config, browser_factory=factory)
    seeded = len(live.receipts)
    first = runner.run([c003], script)
    first_rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(first.run_id)}
    assert first_rows["T-1003"]["status"] == "failed"
    assert first_rows["T-1003"]["error_class"] == "SERVER"
    # No new submission was written: the final click never reached the server.
    assert len(live.receipts) == seeded
    # Exactly one submit POST: no step-level SERVER retry and no task replay
    # while the idempotency key is missing.
    assert state.submit_posts == 1
    # Registering the idempotency key (as resume does) unlocks the task-level
    # replay; the injected fault clears and the next POST writes exactly once.
    runner.ledger.register_idempotency_key(c003.biz_key, first.run_id, c003.task_id, "2026-11-12T00:00:00.000+00:00")
    state.unlocked = True
    second = runner.run([c003], script)
    second_rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(second.run_id)}
    assert second_rows["T-1003"]["status"] == "success"
    assert RECEIPT_RE.fullmatch(second_rows["T-1003"]["receipt_no"] or "")
    assert len(live.receipts) == seeded + 1
    final = next(item for item in runner.ledger.list_steps(second.run_id, "T-1003") if item["step_index"] == 26)
    assert final["status"] == "ok" and final["attempts"] == 1
    # One failed POST before the key, one successful POST after: no duplicates.
    assert state.submit_posts == 2


def test_resume_registers_idempotency_keys(
    tmp_path: Path, serve: Callable[..., Any], make_config: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    live = serve(chaos=ChaosSettings(enabled=True, flaky_submit=True, flaky_company="C003"))
    config = make_config(live.base_url, backoff_base_seconds=0.01, backoff_max_seconds=0.02)
    loaded, script = _load()
    tasks = [task for task in loaded if task.task_id in {"T-1001", "T-1003"}]
    runner = Runner(config)
    original, calls = Runner._execute_task, []
    def crash_after_one(self: Runner, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        calls.append(args[1].task_id)
        if len(calls) == 1:
            raise SystemExit(23)
        return result
    monkeypatch.setattr(Runner, "_execute_task", crash_after_one)
    with pytest.raises(SystemExit):
        runner.run(tasks, script)
    monkeypatch.setattr(Runner, "_execute_task", original)
    # Before resume the remaining task's key is absent; resume registers it so
    # the flaky final submission can be safely replayed inside the same run.
    remaining = next(task for task in tasks if task.task_id == "T-1003")
    assert runner.ledger.has_idempotency_key(remaining.biz_key) is False
    run_id = runner.ledger.latest_run_id()
    resumed = runner.resume(run_id, tasks, script)
    assert runner.ledger.has_idempotency_key(remaining.biz_key) is True
    rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(resumed.run_id)}
    assert rows["T-1003"]["status"] == "success"
    # One seeded receipt, one from the interrupted first task, one from the
    # resumed replay; the flaky submit's failed first POST wrote nothing.
    assert len(live.receipts) == 3 and resumed.status.value == "success"


def test_network_timeout_retries_at_step_layer(serve: Callable[..., Any], make_config: Callable[..., Any]) -> None:
    # Inject a deterministic NETWORK failure on the first open() attempt: the
    # step layer retries the transient error per config without a task replay.
    live = serve()
    config = make_config(live.base_url, max_retries=1, backoff_base_seconds=0.01, backoff_max_seconds=0.02, jitter=False)
    loaded, script = _load()
    task = loaded[0]
    flips: dict[str, int] = {"open": 0}
    class FlakyBrowser:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)
        def open(self, url: str, **kwargs: Any) -> Any:
            flips["open"] += 1
            if flips["open"] == 1:
                raise NetworkError("timeout after 0.02s: GET " + url)
            return self._inner.open(url, **kwargs)
    def factory() -> Any:
        from fillbot.browser.mechanical import MechanicalBrowser
        return FlakyBrowser(MechanicalBrowser(timeout=config.timeout_seconds, follow_redirects=config.follow_redirects))
    runner = Runner(config, browser_factory=factory)
    run = runner.run([task], script)
    rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(run.run_id)}
    assert rows[task.task_id]["status"] == "success"
    first_step = next(item for item in runner.ledger.list_steps(run.run_id, task.task_id) if item["step_index"] == 1)
    # One initial attempt plus one step-level retry; the task itself ran once.
    assert first_step["attempts"] == 2 and flips["open"] == 2


def test_non_final_server_failure_replays_without_idempotency_key(
    serve: Callable[..., Any], make_config: Callable[..., Any]
) -> None:
    # A SERVER failure on a non-final step is a recoverable state failure: the
    # task layer replays the whole task even with no idempotency key, because
    # no final submission has been attempted yet.
    live = serve()
    config = make_config(live.base_url, max_retries=1, backoff_base_seconds=0.01, backoff_max_seconds=0.02, jitter=False)
    loaded, script = _load()
    task = loaded[0]
    state = {"logins": 0}
    class FlakyLogin:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)
        def click(self, locator: Any, **kwargs: Any) -> Any:
            if getattr(locator, "css", None) == "form#login-form button[type=submit]":
                state["logins"] += 1
                if state["logins"] == 1:
                    raise ServerError("login 500", status=500)
            return self._inner.click(locator, **kwargs)
    def factory() -> Any:
        from fillbot.browser.mechanical import MechanicalBrowser
        return FlakyLogin(MechanicalBrowser(timeout=config.timeout_seconds, follow_redirects=config.follow_redirects))
    runner = Runner(config, browser_factory=factory)
    assert runner.ledger.has_idempotency_key(task.biz_key) is False
    run = runner.run([task], script)
    rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(run.run_id)}
    assert rows[task.task_id]["status"] == "success"
    # Two browser sessions: the failed first attempt and one full replay.
    assert state["logins"] == 2


def test_final_network_failure_uses_step_layer_not_task_replay(
    serve: Callable[..., Any], make_config: Callable[..., Any]
) -> None:
    # A timeout on the final click is a transient NETWORK error: the step layer
    # retries it in-place (per YAML), and no idempotency key / task replay is
    # involved.
    live = serve()
    config = make_config(live.base_url, max_retries=2, backoff_base_seconds=0.01, backoff_max_seconds=0.02, jitter=False)
    loaded, script = _load()
    task = next(item for item in loaded if item.payload["company_id"] == "C002")
    state = {"final_clicks": 0, "sessions": 0}
    class FlakyFinal:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            state["sessions"] += 1
        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)
        def click(self, locator: Any, **kwargs: Any) -> Any:
            if getattr(locator, "css", None) == "form#confirm-form button[type=submit]":
                state["final_clicks"] += 1
                if state["final_clicks"] == 1:
                    raise NetworkError("timeout on submit")
            return self._inner.click(locator, **kwargs)
    def factory() -> Any:
        from fillbot.browser.mechanical import MechanicalBrowser
        return FlakyFinal(MechanicalBrowser(timeout=config.timeout_seconds, follow_redirects=config.follow_redirects))
    runner = Runner(config, browser_factory=factory)
    run = runner.run([task], script)
    rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(run.run_id)}
    assert rows[task.task_id]["status"] == "success"
    assert state["sessions"] == 1 and state["final_clicks"] == 2
    final = next(item for item in runner.ledger.list_steps(run.run_id, task.task_id) if item["step_index"] == 26)
    assert final["attempts"] == 2


def test_retry_after_honoured_once_across_layers(
    serve: Callable[..., Any], make_config: Callable[..., Any]
) -> None:
    # A 429 carrying Retry-After reaches the task layer (STEP does not retry
    # SERVER). Across all task-level replays of one failure chain the header
    # is slept exactly once; later replays use exponential backoff.
    live = serve()
    config = make_config(
        live.base_url,
        max_retries=2,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.02,
        jitter=False,
    )
    loaded, script = _load()
    task = next(item for item in loaded if item.payload["company_id"] == "C003")
    sleeps: list[float] = []
    state = {"final_clicks": 0}
    class RateLimitedFinal:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)
        def click(self, locator: Any, **kwargs: Any) -> Any:
            if getattr(locator, "css", None) == "form#confirm-form button[type=submit]":
                state["final_clicks"] += 1
                if state["final_clicks"] <= 2:
                    raise ServerError("rate limited", status=429, retry_after=0.05)
            return self._inner.click(locator, **kwargs)
    def factory() -> Any:
        from fillbot.browser.mechanical import MechanicalBrowser
        return RateLimitedFinal(MechanicalBrowser(timeout=config.timeout_seconds, follow_redirects=config.follow_redirects))
    runner = Runner(config, browser_factory=factory, sleeper=sleeps.append)
    # Idempotency key unlocks final-step task replays.
    runner.ledger.register_idempotency_key(task.biz_key, "pre", task.task_id, "2026-11-12T00:00:00.000+00:00")
    run = runner.run([task], script)
    rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(run.run_id)}
    # Three final clicks: two 429s replayed at task level, then success; the
    # single written receipt proves no duplicate submission.
    assert rows[task.task_id]["status"] == "success" and state["final_clicks"] == 3
    # Two task-level replays -> two sleeps: the Retry-After header once, then
    # plain backoff (0.01 base doubling to the 0.02 cap), never the header twice.
    assert sum(1 for value in sleeps if abs(value - 0.05) < 1e-9) == 1
    assert sleeps == [0.05, 0.02]
def test_dry_run_stops_before_final_submit(tmp_path: Path, serve: Callable[..., Any], make_config: Callable[..., Any]) -> None:
    live = serve()
    config = make_config(
        live.base_url,
        ledger_path=str(tmp_path / "dry" / "ledger.db"),
        evidence_dir=str(tmp_path / "dry" / "evidence"),
        runs_dir=str(tmp_path / "dry" / "runs"),
    )
    loaded, script = _load()
    tasks = [loaded[0], loaded[2]]
    run = Runner(config).run(tasks, script, dry_run=True)
    assert run.status.value == "dry_run" and live.receipts == ["RCP-20250115-0001"]
    evidence = config.evidence_dir / run.run_id
    for task in tasks:
        assert (evidence / task.task_id / "step-26.html").is_file() and not (evidence / task.task_id / "receipt.html").exists()
def test_cli_validate_run_and_report(tmp_path: Path, serve: Callable[..., Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", "--tasks", str(VALID_TASK), "--script", str(SCRIPT)]) == 0
    assert "no network requests" in capsys.readouterr().out
    assert main(["validate", "--tasks", str(ROOT / "tests" / "fixtures" / "invalid_tasks.json"), "--script", str(SCRIPT)]) == 2
    assert "task validation error" in capsys.readouterr().err
    assert main(["validate", "--tasks", "/missing/tasks.json", "--script", str(SCRIPT)]) == 2
    assert main(["validate", "--tasks", str(VALID_TASK), "--script", "/missing/script.yaml"]) == 2
    live = serve()
    config = _write_config(tmp_path, live.base_url)
    code = main(["run", "--tasks", str(VALID_TASK), "--script", str(SCRIPT), "--config", str(config), "--rate-limit", "6000"])
    assert code == 0 and "T-9001" in capsys.readouterr().out
    ledger = Ledger(tmp_path / "data" / "ledger.db")
    run_id = ledger.latest_run_id()
    assert run_id is not None and ledger.get_run(run_id)["status"] == "success"
    assert main(["report", run_id, "--config", str(config)]) == 0 and "succeeded" in capsys.readouterr().out
    assert main(["report", "missing", "--config", str(config)]) == 2 and "run not found" in capsys.readouterr().err
    assert main(["run", "--tasks", str(VALID_TASK), "--script", str(SCRIPT), "--config", str(config), "--rate-limit", "0"]) == 2
def test_cli_resume_after_interruption(
    tmp_path: Path, serve: Callable[..., Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path, serve().base_url)
    source = json.loads((ROOT / "tasks" / "example_batch.json").read_text(encoding="utf-8"))
    selected = [source[0], source[2]]
    for task in selected:
        task["attachments"] = [str(ATTACHMENT)]
    task_file = tmp_path / "two_tasks.json"
    task_file.write_text(json.dumps(selected, ensure_ascii=False), encoding="utf-8")
    original, calls = Runner._execute_task, []
    def crash_after_one(self: Runner, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        calls.append(args[1].task_id)
        if len(calls) == 1:
            raise SystemExit(23)
        return result
    monkeypatch.setattr(Runner, "_execute_task", crash_after_one)
    args = ["run", "--tasks", str(task_file), "--script", str(SCRIPT), "--config", str(config)]
    with pytest.raises(SystemExit):
        main(args)
    monkeypatch.setattr(Runner, "_execute_task", original)
    ledger, run_id = Ledger(tmp_path / "data" / "ledger.db"), None
    run_id = ledger.latest_run_id()
    assert run_id is not None and ledger.get_run(run_id)["status"] == "interrupted"
    assert main(["resume", run_id, "--config", str(config)]) == 0 and "succeeded" in capsys.readouterr().out
    assert ledger.get_run(run_id)["status"] == "success"
    assert len(set(ledger.list_task_rows(run_id)[i]["receipt_no"] for i in range(2))) == 2
def test_mock_serve_loopback_and_bad_port(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    called: dict[str, Any] = {}
    monkeypatch.setattr(Flask, "run", lambda self, **kwargs: called.update(kwargs))
    assert main(["mock-serve", "--port", "5099", "--chaos", "--chaos-seed", "2", "--delay-ms", "1"]) == 0
    assert called["host"] == "127.0.0.1" and called["port"] == 5099 and "127.0.0.1" in capsys.readouterr().out
    assert main(["mock-serve", "--port", "0"]) == 2
def test_config_precedence_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("base_url: http://127.0.0.1:1/\ntimeout_seconds: 3\njitter: 'no'\nmask_fields: [password]\n")
    config = load_config(path, overrides={"base_url": "http://127.0.0.1:2", "jitter": "yes"}, environ={"FILLBOT_RATE_LIMIT_PER_MINUTE": "30"})
    assert config.base_url == "http://127.0.0.1:2" and config.rate_limit_per_minute == 30 and config.jitter
    assert config.requests_per_second == 0.5 and config.public_dict()["ledger_path"] == str(config.ledger_path)
    assert config.with_overrides(jitter=None) is config and config.with_overrides(max_retries=1).max_retries == 1
    assert BotConfig().timeout_seconds == 10.0
    for text in ("x", "[1]", "base_url: ftp://x", "timeout_seconds: -1", "jitter: maybe", "unknown: 1", "mask_fields: x"):
        path.write_text(text if "\n" in text else f"{text if ':' in text else 'base_url: ' + text}\n")
        with pytest.raises(ConfigError):
            load_config(path, environ={})
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "none.yaml", environ={})
    with pytest.raises(ConfigError, match="unknown config override"):
        load_config(overrides={"nope": 1}, environ={})
    with pytest.raises(ConfigError, match="cannot exceed"):
        load_config(environ={"FILLBOT_BACKOFF_BASE_SECONDS": "2", "FILLBOT_BACKOFF_MAX_SECONDS": "1"})
