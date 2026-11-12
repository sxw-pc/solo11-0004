"""Tests for per-site rate policies, adaptive throttling and policy events."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml
from flask import Flask, Response, jsonify
from werkzeug.serving import make_server

from fillbot.browser.mechanical import MechanicalBrowser
from fillbot.cli import main
from fillbot.config import load_config
from fillbot.dsl.engine import DSLExecutor
from fillbot.dsl.schema import load_script, validate_script
from fillbot.errors import ConfigError
from fillbot.intake import load_and_validate_tasks
from fillbot.ledger.db import Ledger
from fillbot.models import RunRecord, StepResult, StepStatus, TaskRecord, iso_now
from fillbot.runner.retry import RetryPolicy
from fillbot.runner.scheduler import Runner
from fillbot.sites import (
    AdaptiveRateLimiter,
    RequestGovernor,
    SitePolicyRegistry,
    canonical_site,
    host_of_site,
    parse_policy,
    site_from_url,
)

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "quarterly_report.yaml"
ATTACHMENT = ROOT / "tests" / "fixtures" / "attachment.pdf"


def _ticker() -> tuple[Callable[[], float], list[float]]:
    """Deterministic monotonic clock; slept time advances it."""
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += max(0.0, seconds)

    sleeps: list[float] = []
    return clock, sleeps


def test_site_keys_and_lenient_policy_parsing() -> None:
    assert canonical_site("https://Reports.Example.com:8443/x") == "reports.example.com:8443"
    assert canonical_site("https://reports.example.com/x") == "reports.example.com:443"
    assert canonical_site("http://reports.example.com") == "reports.example.com:80"
    assert canonical_site("reports.example.com") == "reports.example.com"
    assert canonical_site("*.example.com") == "*.example.com"
    assert canonical_site("*.example.com:443") is None
    assert canonical_site("ftp://x") == "x"
    assert canonical_site(42) is None and canonical_site("") is None
    for bad in (
        "http://",
        "https://bad host/x",
        "out of scope!",
        "example.com/path",
        "example.com:99999",
        "example.com:abc",
        "*.example.com:443",
        "*.bad host",
        "*.a.b/c",
        "http://example.com:bad/x",
        "http://example.com:0/x",
        "http://example.com:99999/x",
    ):
        assert canonical_site(bad) is None, bad
    assert canonical_site("*.a.b.example.com") == "*.a.b.example.com"
    assert site_from_url("http://127.0.0.1:5000/a") == "127.0.0.1:5000"
    assert site_from_url("not a url") is None
    assert site_from_url(123) is None
    assert site_from_url("http:///nohost") is None
    assert site_from_url("https://example.com/x") == "example.com:443"
    assert site_from_url("ftp://example.com/x") == "example.com"
    # Request URLs are lenient: an unparsable port falls back to the scheme default.
    assert site_from_url("http://example.com:bad/x") == "example.com:80"
    assert host_of_site("*.example.com") == "example.com"
    assert host_of_site("example.com:8080") == "example.com"
    assert host_of_site("example.com") == "example.com"

    good, issues = parse_policy({"rate_limit_per_minute": 30, "timeout_seconds": 4, "jitter": "no"})
    assert issues == [] and good is not None
    assert good.as_dict() == {"rate_limit_per_minute": 30, "timeout_seconds": 4.0, "jitter": False}
    for bad, fragment in (
        ({"rate_limit_per_minute": 0}, "rate_limit"),
        ({"rate_limit_per_minute": True}, "rate_limit"),
        ({"timeout_seconds": -1}, "timeout_seconds"),
        ({"max_retries": 1.5}, "max_retries"),
        ({"jitter": "maybe"}, "jitter"),
        ({"unknown": 1}, "unknown policy"),
        ({"backoff_base_seconds": 0}, "backoff_base"),
        ({"backoff_max_seconds": 0}, "backoff_max"),
        ({"backoff_base_seconds": 9, "backoff_max_seconds": 1}, "backoff"),
        ([], "mapping"),
    ):
        parsed, problems = parse_policy(bad)
        assert parsed is None and any(fragment in issue for issue in problems), bad
    for truthy in ("yes", "ON", "1", 1):
        assert parse_policy({"jitter": truthy})[0].jitter is True
    for falsy in ("no", "off", "0", 0):
        assert parse_policy({"jitter": falsy})[0].jitter is False
    parsed, problems = parse_policy({})
    assert parsed is not None and not problems and not parsed


def test_registry_inheritance_wildcard_and_invalid_fallback() -> None:
    config = load_config(
        overrides={
            "rate_limit_per_minute": 60,
            "timeout_seconds": 10,
            "max_retries": 2,
            "site_policies": {
                "reports.example.com": {"rate_limit_per_minute": 12, "timeout_seconds": 3},
                "*.partner.example.com": {"rate_limit_per_minute": 5},
                "legacy.example.com": {"rate_limit_per_minute": "fast"},
                "bad key!": {"rate_limit_per_minute": 1},
            },
        },
        environ={},
    )
    registry = SitePolicyRegistry(config)
    assert registry.configured_sites == ["*.partner.example.com", "reports.example.com"]
    assert len(registry.warnings) == 2
    policy = registry.policy_for("https://reports.example.com/x")
    assert (policy.rate_limit_per_minute, policy.timeout_seconds) == (12, 3)
    assert policy.max_retries == 2 and policy.backoff_base_seconds == 0.4 and policy.source == "site"
    wild = registry.policy_for("https://api.partner.example.com/y")
    assert wild.rate_limit_per_minute == 5 and wild.timeout_seconds == 10
    fallback = registry.policy_for("https://legacy.example.com/z")
    assert fallback.rate_limit_per_minute == 60 and fallback.source == "global"
    unknown = registry.policy_for("https://other.example.com/z")
    assert unknown.rate_limit_per_minute == 60
    step = registry.policy_for(
        "https://reports.example.com/x",
        parse_policy({"rate_limit_per_minute": 99, "max_retries": 0})[0],
    )
    assert (step.rate_limit_per_minute, step.max_retries, step.source) == (99, 0, "step")
    assert registry.policy_for_site(None).rate_limit_per_minute == 60
    assert registry.policy_for_site_key("reports.example.com:443").rate_limit_per_minute == 12


def test_site_policies_from_yaml_env_and_invalid_entries(tmp_path: Path) -> None:
    path = tmp_path / "bot.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://127.0.0.1:1",
                "rate_limit_per_minute": 90,
                "site_policies": {
                    "127.0.0.1": {"timeout_seconds": 2},
                    "bad host!": {"timeout_seconds": 1},
                    "127.0.0.2": "not-a-map",
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_config(path, environ={})
    assert config.site_policies == {"127.0.0.1": {"timeout_seconds": 2.0}}
    assert len(config.site_policy_warnings) == 2
    assert config.public_dict()["site_policies"] == {"127.0.0.1": {"timeout_seconds": 2.0}}
    json_env = load_config(
        environ={"FILLBOT_SITE_POLICIES": json.dumps({"127.0.0.1:5000": {"max_retries": 5}})}
    )
    assert json_env.site_policies == {"127.0.0.1:5000": {"max_retries": 5}}
    assert load_config(environ={"FILLBOT_SITE_POLICIES": "not json"}).site_policies == {}
    assert load_config(environ={"FILLBOT_SITE_POLICIES": "[1]"}).site_policies == {}
    # Old config files must keep loading unchanged.
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text("base_url: http://127.0.0.1:1\nrate_limit_per_minute: 30\n", encoding="utf-8")
    old = load_config(legacy, environ={})
    assert old.rate_limit_per_minute == 30 and old.site_policies == {}
    # Explicit unknown top-level keys are still strict.
    (tmp_path / "unknown.yaml").write_text("base_url: http://127.0.0.1:1\nnope: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown config key"):
        load_config(tmp_path / "unknown.yaml", environ={})


def test_adaptive_limiter_normal_path_and_throttle() -> None:
    clock, sleeps = _ticker()
    limiter = AdaptiveRateLimiter(6000, sleeper=sleeps.append, clock=clock)
    # ~166 requests fit in the initial bucket without sleeping at all.
    for _ in range(160):
        limiter.wait()
    assert sleeps == [] and limiter.factor == 1.0
    limiter.set_factor(0.5, drain=True)
    assert limiter.factor == 0.5 and limiter.effective_rpm == 3000
    limiter.wait()
    assert sleeps and sleeps[-1] > 0
    limiter.set_factor(0.0)
    assert limiter.factor >= 0.0625
    limiter.set_factor(4.0)
    assert limiter.factor == 1.0
    assert limiter.capacity == 6000 and limiter.rate_per_second == 100.0


def _governor(**policies: Any) -> tuple[RequestGovernor, list[float], Any]:
    config = load_config(
        overrides={
            "rate_limit_per_minute": 6000,
            "timeout_seconds": 10,
            "max_retries": 2,
            "site_policies": policies.get("site_policies", {}),
        },
        environ={},
    )
    clock, sleeps = _ticker()
    governor = RequestGovernor(SitePolicyRegistry(config), sleeper=sleeps.append, clock=clock)
    return governor, sleeps, governor


def test_governor_applies_site_and_step_policy_and_records_events() -> None:
    governor, sleeps, _ = _governor(
        site_policies={"slow.example.com": {"rate_limit_per_minute": 6, "timeout_seconds": 30}}
    )
    governor.begin_step(1)
    assert governor.before_request("https://slow.example.com/a") == 30
    assert governor.before_request("https://slow.example.com/a") == 30
    # Two calls at 6 rpm exhausts 6 tokens? No: capacity 6 is drained by 2 only.
    assert sleeps == []
    governor.note_response("https://slow.example.com/a", 200)
    assert governor.end_step() == []

    step = parse_policy({"timeout_seconds": 0.5, "rate_limit_per_minute": 1})[0]
    governor.begin_step(2, step)
    assert governor.before_request("https://slow.example.com/a") == 0.5
    events = governor.end_step()
    assert events and events[0]["scope"] == "step" and events[0]["reason"] == "step_override"
    assert events[0]["step"] == 2 and events[0]["override"] == step.as_dict()

    governor.begin_step(3, None, ("rate_limit_per_minute must be an integer >= 1",))
    events = governor.end_step()
    assert events[0]["reason"] == "invalid_override_ignored" and events[0]["issues"]

    # Unparseable URL uses the global policy and keeps the browser default.
    governor.begin_step(4)
    assert governor.before_request("not a url") is None
    assert governor.end_step() == []


def test_governor_throttles_on_429_and_recovers_without_extra_cost() -> None:
    governor, sleeps, _ = _governor(site_policies={"limited.example.com": {"rate_limit_per_minute": 600}})
    governor.begin_step(1)
    governor.before_request("https://limited.example.com/x")
    governor.note_response("https://limited.example.com/x", 429, 1.5)
    events = governor.end_step()
    types = [event["type"] for event in events]
    assert types == ["limit_triggered", "policy_adjusted"]
    assert events[0]["site"] == "limited.example.com:443" and events[0]["retry_after"] == 1.5
    assert events[0]["rate_factor"] == 0.5 and events[1]["reason"] == "limit_response"
    governor.begin_step(2)
    governor.before_request("https://limited.example.com/x")
    assert sleeps and sleeps[-1] > 0  # throttled request must actually wait
    assert governor.end_step() == []

    # Three consecutive successes recover one recovery step of the rate factor.
    for index in range(3):
        governor.begin_step(3 + index)
        governor.note_response("https://limited.example.com/x", 200)
        recovered = governor.end_step()
    assert recovered[-1]["reason"] == "success_recovery" and recovered[-1]["rate_factor"] == 0.625

    # Recover to full rate with enough successes.
    for index in range(12):
        governor.begin_step(20 + index)
        governor.note_response("https://limited.example.com/x", 200)
        governor.end_step()
    assert governor.state("limited.example.com:443").limiter.factor == 1.0

    # A 503 carrying Retry-After is also treated as a rate-limit signal.
    governor.begin_step(40)
    governor.note_response("https://limited.example.com/x", 503, 2.0)
    events = governor.end_step()
    assert events[0]["type"] == "limit_triggered" and events[0]["status"] == 503
    assert governor.state("limited.example.com:443").limiter.factor == 0.5

    # A normal site never pays for recovery bookkeeping or throttling.
    governor.begin_step(10)
    for _ in range(20):
        governor.note_response("https://fast.example.com/x", 200)
    assert governor.end_step() == []
    governor.begin_step(11)
    for _ in range(20):
        governor.before_request("https://fast.example.com/x")
    assert governor.end_step() == [] and len(sleeps) == 1


def test_governor_restore_from_ledger_replays_site_factor() -> None:
    class StubLedger:
        def __init__(self, events: list[dict[str, Any]]) -> None:
            self.events = events

        def list_step_events(self, run_id: str) -> list[dict[str, Any]]:
            return list(self.events)

    governor, _, _ = _governor(
        site_policies={"slow.example.com": {"rate_limit_per_minute": 12}}
    )
    ledger = StubLedger(
        [
            {"scope": "site", "type": "limit_triggered", "site": "slow.example.com:443"},
            {
                "scope": "site",
                "type": "policy_adjusted",
                "site": "slow.example.com:443",
                "rate_factor": 0.25,
            },
            {"scope": "step", "type": "policy_adjusted", "rate_factor": 0.1},
        ]
    )
    assert governor.restore_from_ledger(ledger, "R") == 1
    assert governor.state("slow.example.com:443").limiter.factor == 0.25
    assert governor.state("slow.example.com:443").policy.rate_limit_per_minute == 12

    class Broken:
        def list_step_events(self, run_id: str) -> list[dict[str, Any]]:
            raise RuntimeError("old ledger")

    assert governor.restore_from_ledger(Broken(), "R") == 0

    # The restore is audited once, on the next step that actually runs.
    governor.begin_step(1)
    events = governor.end_step()
    assert events and events[0]["reason"] == "resume_restore"
    assert events[0]["rate_factors"]["slow.example.com:443"] == 0.25
    governor.begin_step(2)
    assert governor.end_step() == []


def test_step_policy_schema_is_lenient_and_backwards_compatible() -> None:
    script = validate_script(
        {
            "name": "policy",
            "version": 1,
            "steps": [
                {"action": "open", "url": "http://127.0.0.1:1/x"},
                {
                    "action": "click",
                    "selector": "#submit",
                    "policy": {"rate_limit_per_minute": 4, "timeout_seconds": 0.5},
                },
                {
                    "action": "wait",
                    "seconds": 0,
                    "policy": {"rate_limit_per_minute": "bad", "nope": 1},
                },
            ],
        }
    )
    assert script.steps[1].policy == {"rate_limit_per_minute": 4, "timeout_seconds": 0.5}
    assert script.steps[2].policy is None and script.steps[2].policy_issues
    legacy = validate_script(
        {"name": "legacy", "version": 1, "steps": [{"action": "open", "url": "http://127.0.0.1:1/x"}]}
    )
    assert legacy.steps[0].policy is None and legacy.steps[0].policy_issues == ()


def test_engine_step_policy_drives_timeout_and_retry_backoff() -> None:
    app = Flask(__name__)
    attempts = {"count": 0}

    @app.get("/boom")
    def boom() -> Response:
        attempts["count"] += 1
        return Response("boom", status=503)

    @app.get("/ok")
    def ok() -> str:
        return "<html><body>ok</body></html>"

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    config = load_config(
        overrides={
            "base_url": base,
            "rate_limit_per_minute": 6000,
            "timeout_seconds": 10,
            "site_policies": {f"127.0.0.1:{server.server_port}": {"max_retries": 0, "timeout_seconds": 10}},
        },
        environ={},
    )
    clock, sleeps = _ticker()
    governor = RequestGovernor(SitePolicyRegistry(config), sleeper=sleeps.append, clock=clock)
    browser = MechanicalBrowser(
        request_hook=governor.before_request, response_hook=governor.note_response
    )
    record: list[float | None] = []
    session = browser._session
    original_session_request = session.request

    def recording_session_request(method, url, **kwargs):  # type: ignore[no-untyped-def]
        record.append(kwargs.get("timeout"))
        return original_session_request(method, url, **kwargs)

    session.request = recording_session_request  # type: ignore[method-assign]
    try:
        script = validate_script(
            {
                "name": "retry",
                "version": 1,
                "steps": [
                    {
                        "action": "open",
                        "url": f"{base}/boom",
                        "retry": {"attempts": 2, "base_seconds": 0.01},
                        "policy": {"timeout_seconds": 0.25, "jitter": False},
                    }
                ],
            }
        )
        executor = DSLExecutor(
            script,
            browser,
            task_context={},
            run_id="R",
            governor=governor,
            default_retry=RetryPolicy(3, 0.01, 0.02, False, sleeper=sleeps.append),
            sleeper=sleeps.append,
        )
        result = executor.execute()
        assert result.status.value == "failed" and attempts["count"] == 2
        # Step policy timeout reaches the browser; retry delay is the explicit base.
        assert record == [0.25, 0.25]
        assert 0.01 in sleeps
        events = result.steps[0].policy_events
        assert any(event["scope"] == "step" for event in events)
        assert any(
            event.get("event") == "retry_backoff" and event["attempt"] == 1 for event in events
        )
    finally:
        browser.close()
        server.shutdown()
        thread.join(timeout=3)


def test_scheduler_multi_site_throttle_and_events_in_ledger(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path
) -> None:
    def limited_app() -> Flask:
        app = Flask("limited")
        state = {"hit": 0}

        @app.get("/x")
        def x() -> Response:
            state["hit"] += 1
            if state["hit"] == 1:
                return Response("slow down", status=429, headers={"Retry-After": "0.2"})
            return "<html><body><span id='ok'>ok</span></body></html>"

        @app.get("/state")
        def get_state() -> Any:
            return jsonify(state)

        return app

    fast = serve()
    slow = serve(app=limited_app())
    config = make_config(
        fast.base_url,
        site_policies={
            f"127.0.0.1:{slow.server.server_port}": {
                "rate_limit_per_minute": 30,
                "timeout_seconds": 2,
                "max_retries": 1,
                "backoff_base_seconds": 0.01,
                "backoff_max_seconds": 0.05,
            },
            f"127.0.0.1:{fast.server.server_port}": {"rate_limit_per_minute": 6000},
        },
    )
    script = validate_script(
        {
            "name": "multi-site",
            "version": 1,
            "steps": [
                {"action": "open", "url": f"{fast.base_url}/healthz"},
                {
                    "action": "open",
                    "url": f"{slow.base_url}/x",
                    "policy": {"rate_limit_per_minute": 6000},
                },
                {"action": "assert_text", "text": "ok"},
                {"action": "set_var", "name": "receipt_no", "value": "RCP-20260101-0009"},
            ],
        }
    )
    task = TaskRecord(
        "T-MULTI",
        "C002:MULTI",
        {},
        (ATTACHMENT,),
    )
    runner = Runner(config)
    run = runner.run([task], script)
    assert run.status.value == "success"

    steps = runner.ledger.list_steps(run.run_id, "T-MULTI")
    events = runner.ledger.list_step_events(run.run_id, "T-MULTI")
    site_events = [event for event in events if event.get("scope") == "site"]
    assert any(event["type"] == "limit_triggered" and event["status"] == 429 for event in site_events)
    assert any(event.get("reason") == "limit_response" for event in site_events)
    assert any(event["scope"] == "step" for event in events)
    for step in steps:
        if step["policy_events_json"]:
            assert json.loads(step["policy_events_json"])
    # Raw SQL confirms the transitional ledger column is present and populated.
    with sqlite3.connect(runner.ledger.path) as connection:
        rows = connection.execute(
            "SELECT step_index, policy_events_json FROM steps WHERE run_id = ? AND policy_events_json IS NOT NULL",
            (run.run_id,),
        ).fetchall()
    assert rows

    # Recovery: rebuild a governor from the ledger and check the site is still slowed.
    rebuilt = RequestGovernor(SitePolicyRegistry(config))
    restored = rebuilt.restore_from_ledger(runner.ledger, run.run_id)
    assert restored >= 1
    slowed = rebuilt.snapshot()
    site_key = next(iter(slowed))
    assert slowed[site_key]["rate_factor"] < 1.0


def test_resume_restores_site_policy_state(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path
) -> None:
    live = serve()
    config = make_config(live.base_url)
    tasks = load_and_validate_tasks(ROOT / "tasks" / "example_batch.json")
    script = load_script(SCRIPT)

    ledger = Ledger(config.ledger_path)
    ledger.create_run(
        RunRecord(
            "R-RESUME",
            iso_now(),
            tasks_total=2,
            task_source="tasks.json",
            script_source=str(SCRIPT),
            base_url=config.base_url,
        )
    )
    # Simulate a previous (interrupted) task that got throttled.
    ledger.start_task("R-RESUME", tasks[0], iso_now(), str(config.evidence_dir))
    prior = StepResult(
        1,
        "open",
        StepStatus.OK,
        iso_now(),
        iso_now(),
        3,
        policy_events=[
            {
                "type": "policy_adjusted",
                "scope": "site",
                "site": "127.0.0.1:80",
                "rate_factor": 0.25,
                "reason": "limit_response",
            }
        ],
    )
    ledger.record_step("R-RESUME", tasks[0].task_id, prior)
    ledger.finish_task(
        "R-RESUME",
        tasks[0].task_id,
        status="failed",
        finished_at=iso_now(),
        duration_ms=1,
        attempts=1,
        error_class="SERVER",
        error_message="interrupted",
    )
    runner = Runner(config, ledger=ledger)
    runner.resume("R-RESUME", [tasks[0], tasks[2]], script)

    # The persisted 0.25 factor was replayed onto the pre-existing site bucket.
    restored = runner.governor.snapshot()["127.0.0.1:80"]
    assert restored["rate_factor"] == 0.25
    assert ledger.get_run("R-RESUME")["status"] == "success"
    restore_events = [
        event
        for event in ledger.list_step_events("R-RESUME")
        if event.get("reason") == "resume_restore"
    ]
    assert restore_events and restore_events[0]["rate_factors"]["127.0.0.1:80"] == 0.25
    assert [row["task_id"] for row in ledger.list_task_rows("R-RESUME")] == ["T-1001", "T-1003"]
    assert ledger.task_status("R-RESUME", "T-1003") == "success"


def test_legacy_ledger_and_task_data_run_without_migration(
    tmp_path: Path, serve: Callable[..., Any], make_config: Callable[..., Any]
) -> None:
    """A ledger created by the previous version (no policy column) keeps working."""
    db_path = tmp_path / "legacy" / "ledger.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
              status TEXT NOT NULL, tasks_total INTEGER NOT NULL DEFAULT 0, succeeded INTEGER NOT NULL DEFAULT 0,
              failed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
              task_source TEXT NOT NULL DEFAULT '', script_source TEXT NOT NULL DEFAULT '',
              dry_run INTEGER NOT NULL DEFAULT 0, base_url TEXT NOT NULL DEFAULT '',
              evidence_dir TEXT NOT NULL DEFAULT '', options_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
              biz_key TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT, finished_at TEXT,
              duration_ms INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
              error_class TEXT, error_message TEXT, receipt_no TEXT, evidence_dir TEXT NOT NULL DEFAULT '',
              UNIQUE(run_id, task_id));
            CREATE TABLE steps (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
              step_index INTEGER NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT,
              finished_at TEXT, duration_ms INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
              error_class TEXT, error_message TEXT, snapshot_path TEXT,
              UNIQUE(run_id, task_id, step_index));
            INSERT INTO runs (run_id, started_at, status, tasks_total) VALUES ('LEGACY', '2025-01-01T00:00:00Z', 'failed', 1);
            INSERT INTO steps (run_id, task_id, step_index, action, status)
              VALUES ('LEGACY', 'T-OLD', 1, 'open', 'ok');
            """
        )
    ledger = Ledger(db_path)
    assert ledger.list_steps("LEGACY", "T-OLD")[0]["action"] == "open"
    assert ledger.list_step_events("LEGACY") == []
    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(steps)")}
    assert "policy_events_json" in columns

    live = serve()
    config = make_config(live.base_url, ledger_path=str(db_path))
    tasks, script = _legacy_batch()
    run = Runner(config).run(tasks, script)
    assert run.status.value in {"success", "failed"}
    assert ledger.get_run(run.run_id)["status"] == run.status.value


def _legacy_batch() -> tuple[list[TaskRecord], Any]:
    tasks = [
        TaskRecord(
            "T-LEGACY",
            "C002:2025-Q4",
            {
                "username": "parkco-b",
                "password": "mock-only-beta",
                "company_id": "C002",
                "company_name": "青岚谷生态科技（虚构）",
                "period": "2025-Q4",
                "contact_name": "林小岚（虚构）",
                "report_date": "2025-11-10",
                "electricity": 1,
                "water": 1,
                "gas": 1,
                "employees": 1,
                "hires": 1,
            },
            (ATTACHMENT,),
        )
    ]
    return tasks, load_script(SCRIPT)


def test_legacy_limiter_parameter_still_works(serve: Callable[..., Any]) -> None:
    calls: list[int] = []
    with MechanicalBrowser(limiter=lambda: calls.append(1)) as browser:
        assert browser.open(serve().base_url + "/healthz").status == 200
    assert calls == [1]


def test_ledger_migration_is_idempotent_and_helpers(tmp_path: Path) -> None:
    path = tmp_path / "l.db"
    ledger = Ledger(path)
    Ledger(path).initialize()  # second open must not fail on the existing column
    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(steps)")]
    assert columns.count("policy_events_json") == 1
    ledger.create_run(RunRecord("R", iso_now(), tasks_total=1))
    step = StepResult(
        1,
        "open",
        StepStatus.OK,
        iso_now(),
        iso_now(),
        1,
        policy_events=[{"type": "limit_triggered", "scope": "site", "site": "x:1"}],
    )
    ledger.start_task("R", TaskRecord("T", "K:V", {}), iso_now(), "e")
    ledger.record_step("R", "T", step)
    events = ledger.list_step_events("R", "T")
    assert events[0]["task_id"] == "T" and events[0]["step"] == 1
    # Corrupted JSON is ignored, not fatal.
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE steps SET policy_events_json = 'not json' WHERE run_id='R' AND task_id='T'"
        )
    assert ledger.list_step_events("R", "T") == []


def test_invalid_step_policy_is_ignored_and_audited() -> None:
    """A bad step policy must not abort the run; it degrades to the site policy."""
    from fillbot.browser.base import Page

    class OkBrowser:
        def __init__(self) -> None:
            self.current = Page(200, "https://example.com/", {}, "<html>ok</html>")

        @property
        def page(self) -> Page:
            return self.current

        def open(self, url: str, **kwargs: Any) -> Page:
            self.current = Page(200, url, {}, "<html>ok</html>")
            return self.current

        def close(self) -> None:
            return None

    config = load_config(
        overrides={"site_policies": {"example.com": {"rate_limit_per_minute": 7}}, "rate_limit_per_minute": 6000},
        environ={},
    )
    governor = RequestGovernor(SitePolicyRegistry(config))
    executor = DSLExecutor(
        validate_script(
            {
                "name": "bad-policy",
                "version": 1,
                "steps": [
                    {
                        "action": "open",
                        "url": "https://example.com/ok",
                        "policy": {"timeout_seconds": -3},
                    },
                    {"action": "set_var", "name": "receipt_no", "value": "RCP-20260101-0008"},
                ],
            }
        ),
        OkBrowser(),
        task_context={},
        run_id="R",
        governor=governor,
    )
    result = executor.execute()
    assert result.status.value == "success"
    events = result.steps[0].policy_events
    assert events[0]["reason"] == "invalid_override_ignored" and events[0]["issues"]
    # The site policy is still in force; the invalid override did not change it.
    assert governor.state("example.com:443").policy.rate_limit_per_minute == 7


def test_registry_and_governor_defensive_paths() -> None:
    class RawConfig:
        rate_limit_per_minute = 60
        timeout_seconds = 10
        max_retries = 2
        backoff_base_seconds = 0.4
        backoff_max_seconds = 8.0
        jitter = True
        site_policies = {
            "bad host!": {"timeout_seconds": 1},
            "ok.example.com": {"rate_limit_per_minute": "fast"},
        }

    registry = SitePolicyRegistry(RawConfig())
    assert registry.configured_sites == [] and len(registry.warnings) == 2

    config = load_config(overrides={"rate_limit_per_minute": 6000}, environ={})
    governor = RequestGovernor(SitePolicyRegistry(config))
    assert governor.take_events() == []
    governor.begin_step(1, {"timeout_seconds": True})
    events = governor.end_step()
    assert events[0]["reason"] == "invalid_override_ignored"
    governor.begin_step(2, parse_policy({"max_retries": 0})[0])
    governor.end_step()
    governor.note_retry(None, 1, 0.2, "SERVER")  # no active step -> ignored
    assert governor.take_events() == []
    governor.begin_step(4, {"timeout_seconds": 3})
    assert governor.before_request("not a url") == 3  # global policy + step override
    assert governor.end_step()[0]["reason"] == "step_override"
    governor.note_response("not a url", 200)
    governor.note_response("https://fine.example.com/x", 500)
    snapshot = governor.policy_snapshot()
    assert snapshot["configured_sites"] == [] and snapshot["sites"]
    governor.begin_step(3, None)
    governor.note_retry("https://fine.example.com/x", 2, 0.5, "SERVER")
    retry_events = governor.end_step()
    assert retry_events[0]["event"] == "retry_backoff" and retry_events[0]["scope"] == "global"


def test_cli_accepts_site_policy_config(tmp_path: Path, serve: Callable[..., Any], capsys: pytest.CaptureFixture[str]) -> None:
    live = serve()
    path = tmp_path / "bot.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "base_url": live.base_url,
                "rate_limit_per_minute": 6000,
                "timeout_seconds": 5,
                "backoff_base_seconds": 0.01,
                "backoff_max_seconds": 0.05,
                "evidence_dir": str(tmp_path / "evidence"),
                "ledger_path": str(tmp_path / "data" / "ledger.db"),
                "runs_dir": str(tmp_path / "runs"),
                "site_policies": {
                    f"127.0.0.1:{live.server.server_port}": {"rate_limit_per_minute": 6000},
                    "bogus host!": {"timeout_seconds": 1},
                },
            }
        ),
        encoding="utf-8",
    )
    task = ROOT / "tests" / "fixtures" / "valid_task.json"
    assert main(["validate", "--tasks", str(task), "--script", str(SCRIPT), "--config", str(path)]) == 0
    assert "bogus host!" in capsys.readouterr().err
    code = main(["run", "--tasks", str(task), "--script", str(SCRIPT), "--config", str(path)])
    assert code == 0
    assert "bogus host!" in capsys.readouterr().err
    ledger = Ledger(tmp_path / "data" / "ledger.db")
    run_id = ledger.latest_run_id()
    assert run_id is not None and ledger.get_run(run_id)["status"] == "success"
    log = (tmp_path / "runs" / run_id / "run.log").read_text(encoding="utf-8")
    assert "bogus host!" in log
    stored = ledger.get_run(run_id)["options_json"]
    assert f"127.0.0.1:{live.server.server_port}" in stored


def test_no_extra_clock_cost_on_normal_requests() -> None:
    """Counting clock reads proves the happy path matches the old limiter cost."""
    counts = {"clock": 0, "sleep": 0}

    def clock() -> float:
        counts["clock"] += 1
        return counts["clock"] / 1000.0

    def sleep(seconds: float) -> None:
        counts["sleep"] += 1

    config = load_config(overrides={"rate_limit_per_minute": 6000}, environ={})
    governor = RequestGovernor(SitePolicyRegistry(config), sleeper=sleep, clock=clock)
    governor.begin_step(1)
    for _ in range(50):
        governor.before_request("http://127.0.0.1:5000/x")
    governor.end_step()
    # Two clock reads per request: update timestamp and post-sleep reset (skipped here).
    assert counts["clock"] <= 2 * 50 + 2
    assert counts["sleep"] == 0
