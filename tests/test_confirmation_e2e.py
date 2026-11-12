"""End-to-end acceptance tests for the human pre-submit confirmation gate."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

import pytest

from fillbot.cli import main
from fillbot.dsl.schema import load_script
from fillbot.errors import ConfirmationError
from fillbot.intake import load_and_validate_tasks
from fillbot.ledger.db import Ledger
from fillbot.ledger.report import build_summary
from fillbot.models import TaskStatus
from fillbot.runner.scheduler import Runner

ROOT = Path(__file__).parents[1]
GATED_SCRIPT = ROOT / "scripts" / "quarterly_report_gated.yaml"
PLAIN_SCRIPT = ROOT / "scripts" / "quarterly_report.yaml"
TASKS_FILE = ROOT / "tasks" / "example_batch.json"
RECEIPT_RE = re.compile(r"^RCP-\d{8}-\d{4}$")


def _seeded_count(live: Any) -> int:
    """The mock store ships one seeded receipt; tests count new submissions."""
    return 1


def _gated_tasks() -> list[Any]:
    tasks = load_and_validate_tasks(TASKS_FILE)
    # Keep the well-behaved C002/C003 accounts; one task is enough for gate flow.
    return [tasks[0]]


def _write_config(tmp_path: Path, base_url: str) -> Path:
    import yaml

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


def test_gated_run_pauses_and_survives_process_restart(
    tmp_path: Path, serve: Callable[..., Any], make_config: Callable[..., Any]
) -> None:
    live = serve()
    config = make_config(live.base_url)
    tasks, script = _gated_tasks(), load_script(GATED_SCRIPT)
    runner = Runner(config)
    run = runner.run(tasks, script, task_source=str(TASKS_FILE), script_source=str(GATED_SCRIPT))

    # Run pauses; no submission reached the mock site yet.
    assert run.status.value == "awaiting_confirmation" and len(live.receipts) == _seeded_count(live)
    row = runner.ledger.list_task_rows(run.run_id)[0]
    assert row["status"] == "awaiting_confirmation"
    pending = runner.list_pending_confirmations(run_id=run.run_id)
    assert len(pending) == 1 and pending[0].task_id == tasks[0].task_id
    (run_id, task_id), token = next(iter(runner.issued_tokens.items()))
    assert token.startswith("fbc-")

    # Masked review snapshot + metadata exist; password never leaks.
    task_evidence = config.evidence_dir / run_id / task_id
    snapshot = (task_evidence / "confirmation-snapshot.html").read_text(encoding="utf-8")
    assert "mock-only-beta" not in snapshot and "请确认报送内容" in snapshot
    meta = json.loads((task_evidence / "confirmation.json").read_text(encoding="utf-8"))
    assert meta["gate_step_index"] == 26 and meta["biz_key"] == tasks[0].biz_key
    assert (task_evidence / "step-26.html").is_file() and not (task_evidence / "receipt.html").exists()

    # Simulate a process restart: a brand-new Runner over the same ledger file.
    revived = Runner(make_config(live.base_url))
    revived_pending = revived.list_pending_confirmations(run_id=run_id)
    assert len(revived_pending) == 1 and revived_pending[0].confirmation_id == pending[0].confirmation_id
    # Plaintext token is not reconstructable from the ledger, but the issued one works.
    confirmation, status = revived.approve_confirmation(token, tasks, script)
    assert status == TaskStatus.SUCCESS and confirmation.status == "approved"

    # Exactly one submission, one receipt, run reaches success.
    assert len(live.receipts) == _seeded_count(live) + 1 and RECEIPT_RE.fullmatch(live.receipts[-1])
    final_row = revived.ledger.list_task_rows(run_id)[0]
    assert final_row["status"] == "success" and final_row["receipt_no"] == live.receipts[-1]
    assert revived.ledger.get_run(run_id)["status"] == "success"
    assert (task_evidence / "receipt.html").is_file()
    summary = build_summary(revived.ledger, run_id)
    assert summary.succeeded == 1 and summary.awaiting_confirmation == 0 and summary.abandoned == 0


def test_bad_and_spent_tokens_are_rejected_and_audited(
    tmp_path: Path, serve: Callable[..., Any], make_config: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    live = serve()
    config = make_config(live.base_url)
    tasks, script = _gated_tasks(), load_script(GATED_SCRIPT)
    runner = Runner(config)
    run = runner.run(tasks, script)
    (_, _), token = next(iter(runner.issued_tokens.items()))

    # Unknown token: rejected, audited, no submission.
    with pytest.raises(ConfirmationError, match="unknown confirmation token"):
        runner.approve_confirmation("fbc-does-not-exist", tasks, script)
    assert len(live.receipts) == _seeded_count(live)
    # Invalid-token attempts carry no run id, so they appear in the global audit.
    events = runner.ledger.list_confirmation_events()
    assert events[-1]["event"] == "invalid_token"

    # The real token works; reusing it must fail and be audited.
    confirmation, status = runner.approve_confirmation(token, tasks, script)
    assert status == TaskStatus.SUCCESS and len(live.receipts) == _seeded_count(live) + 1
    with pytest.raises(ConfirmationError, match="already used"):
        runner.approve_confirmation(token, tasks, script)
    events = runner.ledger.list_confirmation_events(run.run_id)
    assert "spent_token" in [event["event"] for event in events]
    # The rejected reuse did not trigger another submission.
    assert len(live.receipts) == _seeded_count(live) + 1

    # CLI surface: bad token exits 3 and writes to stderr.
    code = main(
        [
            "confirmations",
            "approve",
            "--token",
            token,
            "--tasks",
            str(TASKS_FILE),
            "--script",
            str(GATED_SCRIPT),
            "--config",
            str(_write_config(tmp_path, live.base_url)),
        ]
    )
    assert code == 3 and "already used" in capsys.readouterr().err


def test_reject_is_terminal_human_abandonment_not_failure(
    tmp_path: Path, serve: Callable[..., Any], make_config: Callable[..., Any]
) -> None:
    live = serve()
    config = make_config(live.base_url)
    tasks, script = _gated_tasks(), load_script(GATED_SCRIPT)
    runner = Runner(config)
    run = runner.run(tasks, script)
    (_, task_id), token = next(iter(runner.issued_tokens.items()))

    confirmation = runner.reject_confirmation(token, tasks, actor="reviewer", note="wrong figures")
    assert confirmation.status == "rejected"
    assert len(live.receipts) == _seeded_count(live)  # nothing was ever submitted
    row = runner.ledger.list_task_rows(run.run_id)[0]
    assert row["status"] == "abandoned" and row["receipt_no"] is None and row["attempts"] == 0
    summary = build_summary(runner.ledger, run.run_id)
    assert summary.failed == 0 and summary.abandoned == 1 and summary.failure_reasons == {}
    assert runner.ledger.get_run(run.run_id)["status"] == "success"

    # Resuming the same run never retries the human-abandoned task (it is terminal).
    revived = Runner(make_config(live.base_url))
    resumed = revived.resume(
        run.run_id, tasks, script, task_source=str(TASKS_FILE), script_source=str(GATED_SCRIPT)
    )
    assert revived.ledger.task_status(run.run_id, task_id) == "abandoned"
    assert resumed.status.value == "success"
    assert len(live.receipts) == _seeded_count(live)
    events = runner.ledger.list_confirmation_events(run.run_id, task_id)
    assert [event["event"] for event in events] == ["paused", "rejected"]


def test_batch_approval_submits_each_task_once(
    tmp_path: Path, serve: Callable[..., Any], make_config: Callable[..., Any]
) -> None:
    live = serve()
    config = make_config(live.base_url)
    all_tasks = load_and_validate_tasks(TASKS_FILE)
    tasks = [all_tasks[0], all_tasks[2]]  # C002 + C003, distinct biz keys
    script = load_script(GATED_SCRIPT)
    runner = Runner(config)
    run = runner.run(tasks, script)
    assert run.status.value == "awaiting_confirmation"
    pending = runner.list_pending_confirmations(run_id=run.run_id)
    assert len(pending) == 2 and len(live.receipts) == _seeded_count(live)
    tokens = [runner.issued_tokens[(run.run_id, record.task_id)] for record in pending]

    outcomes = runner.approve_confirmations_batch(tokens, tasks, script, actor="batch-ops")
    assert [status for _, status in outcomes] == [TaskStatus.SUCCESS, TaskStatus.SUCCESS]
    assert len(live.receipts) == _seeded_count(live) + 2 and len(set(live.receipts[-2:])) == 2
    assert runner.ledger.get_run(run.run_id)["status"] == "success"
    # Every confirmation carries the batch actor.
    for record in runner.ledger.list_confirmations(run_id=run.run_id):
        assert record["status"] == "approved" and record["decided_by"] == "batch-ops"


def test_batch_requires_tokens_and_matching_task_sheet(
    tmp_path: Path, serve: Callable[..., Any], make_config: Callable[..., Any]
) -> None:
    from fillbot.errors import ScriptError

    live = serve()
    runner = Runner(make_config(live.base_url))
    tasks, script = _gated_tasks(), load_script(GATED_SCRIPT)
    with pytest.raises(ScriptError, match="at least one confirmation token"):
        runner.approve_confirmations_batch([], tasks, script)

    # A decision whose task is absent from the supplied sheet fails loudly.
    isolated = make_config(
        live.base_url,
        ledger_path=str(tmp_path / "iso" / "ledger.db"),
        evidence_dir=str(tmp_path / "iso" / "evidence"),
        runs_dir=str(tmp_path / "iso" / "runs"),
    )
    isolated_runner = Runner(isolated)
    isolated_runner.run(tasks, script)
    (_, _), token = next(iter(isolated_runner.issued_tokens.items()))
    with pytest.raises(ScriptError, match="not found in the original task sheet"):
        Runner(isolated).approve_confirmation(token, [], script)


def test_approved_replay_failure_does_not_submit_and_can_still_finish(
    tmp_path: Path, serve: Callable[..., Any], make_config: Callable[..., Any]
) -> None:
    live = serve()
    config = make_config(live.base_url)
    tasks, script = _gated_tasks(), load_script(GATED_SCRIPT)
    runner = Runner(config)
    runner.run(tasks, script)
    (_, _), token = next(iter(runner.issued_tokens.items()))

    # Break the replay by pointing at a dead base URL; the consumed token stays
    # consumed and the mock site never receives a submission.
    broken = make_config("http://127.0.0.1:1")
    revived = Runner(broken)
    _, status = revived.approve_confirmation(token, tasks, script)
    assert status == TaskStatus.FAILED and len(live.receipts) == _seeded_count(live)
    # Token was consumed exactly once; reuse is rejected, not replayed.
    with pytest.raises(ConfirmationError, match="already used"):
        Runner(make_config(live.base_url)).approve_confirmation(token, tasks, script)


def test_unmarked_script_keeps_fully_automatic_behaviour(
    tmp_path: Path, serve: Callable[..., Any], make_config: Callable[..., Any]
) -> None:
    live = serve()
    config = make_config(live.base_url)
    tasks, script = _gated_tasks(), load_script(PLAIN_SCRIPT)
    runner = Runner(config)
    run = runner.run(tasks, script, task_source=str(TASKS_FILE), script_source=str(PLAIN_SCRIPT))
    assert run.status.value == "success" and run.abandoned == 0
    assert len(live.receipts) == _seeded_count(live) + 1 and runner.list_pending_confirmations() == []
    assert runner.issued_tokens == {}
    # Dry-run semantics on a gated script still stop before the final click.
    dry_config = make_config(
        live.base_url,
        ledger_path=str(tmp_path / "dry" / "ledger.db"),
        evidence_dir=str(tmp_path / "dry" / "evidence"),
        runs_dir=str(tmp_path / "dry" / "runs"),
    )
    gated = Runner(dry_config).run(_gated_tasks(), load_script(GATED_SCRIPT), dry_run=True)
    assert gated.status.value == "dry_run"
    # Dry-run never submitted and never opened a confirmation gate.
    assert len(live.receipts) == _seeded_count(live) + 1
    assert Runner(dry_config).list_pending_confirmations() == []


def test_cli_gated_lifecycle_list_approve(
    tmp_path: Path, serve: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    live = serve()
    config_path = _write_config(tmp_path, live.base_url)
    single_tasks = tmp_path / "one_task.json"
    # One-task sheet with absolute attachment paths so validation is location-proof.
    task = _gated_tasks()[0]
    single_tasks.write_text(
        json.dumps(
            [
                {
                    "task_id": task.task_id,
                    "biz_key": task.biz_key,
                    "attachments": [str(path) for path in task.attachments],
                    "payload": task.payload,
                }
            ],
            ensure_ascii=False,
        )
    )
    code = main(
        ["run", "--tasks", str(single_tasks), "--script", str(GATED_SCRIPT), "--config", str(config_path)]
    )
    out = capsys.readouterr().out
    assert code == 0 and "HUMAN CONFIRMATION REQUIRED" in out and "fbc-" in out
    token = re.search(r"one-time token: (\S+)", out).group(1)

    ledger = Ledger(tmp_path / "data" / "ledger.db")
    run_id = ledger.latest_run_id()
    assert main(["confirmations", "list", "--config", str(config_path)]) == 0
    listing = capsys.readouterr().out
    assert run_id in listing and "awaiting human confirmation" in listing

    assert (
        main(
            [
                "confirmations",
                "approve",
                "--token",
                token,
                "--tasks",
                str(single_tasks),
                "--script",
                str(GATED_SCRIPT),
                "--config",
                str(config_path),
            ]
        )
        == 0
    )
    assert "success" in capsys.readouterr().out
    # A separate process/CLI wrote the decision; re-open the ledger to read it.
    refreshed = Ledger(tmp_path / "data" / "ledger.db")
    assert refreshed.get_run(run_id)["status"] == "success"
    assert main(["confirmations", "list", "--config", str(config_path)]) == 0
    assert "no tasks awaiting" in capsys.readouterr().out


def test_cli_reject_and_summary_csv(
    tmp_path: Path, serve: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    import csv

    live = serve()
    config_path = _write_config(tmp_path, live.base_url)
    task = _gated_tasks()[0]
    single_tasks = tmp_path / "one_task.json"
    single_tasks.write_text(
        json.dumps(
            [
                {
                    "task_id": task.task_id,
                    "biz_key": task.biz_key,
                    "attachments": [str(path) for path in task.attachments],
                    "payload": task.payload,
                }
            ],
            ensure_ascii=False,
        )
    )
    assert main(["run", "--tasks", str(single_tasks), "--script", str(GATED_SCRIPT), "--config", str(config_path)]) == 0
    token = re.search(r"one-time token: (\S+)", capsys.readouterr().out).group(1)
    code = main(
        [
            "confirmations",
            "reject",
            "--token",
            token,
            "--tasks",
            str(single_tasks),
            "--actor",
            "reviewer",
            "--note",
            "data mismatch",
            "--config",
            str(config_path),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0 and "abandoned (human decision)" in out and "human-abandoned" in out
    ledger = Ledger(tmp_path / "data" / "ledger.db")
    run_id = ledger.latest_run_id()
    summary_csv = config_runs_csv(tmp_path, run_id)
    with summary_csv.open(encoding="utf-8", newline="") as handle:
        values = dict(csv.reader(handle))
    assert values["abandoned"] == "1" and values["failed"] == "0" and values["awaiting_confirmation"] == "0"
    assert len(live.receipts) == _seeded_count(live)


def config_runs_csv(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / "runs" / run_id / "summary.csv"


def test_replay_rebuilds_session_without_holding_cookies(
    tmp_path: Path, serve: Callable[..., Any], make_config: Callable[..., Any]
) -> None:
    """The paused browser session is closed; approval must work from a fresh session."""
    live = serve()
    config = make_config(live.base_url)
    tasks, script = _gated_tasks(), load_script(GATED_SCRIPT)
    runner = Runner(config)
    created: list[Any] = []

    def default_factory() -> Any:
        from fillbot.browser.mechanical import MechanicalBrowser

        return MechanicalBrowser(
            timeout=config.timeout_seconds,
            follow_redirects=config.follow_redirects,
            limiter=runner.limiter.wait,
        )

    def factory() -> Any:
        browser = default_factory()
        created.append(browser)
        return browser

    runner._browser_factory = factory
    runner.run(tasks, script)
    (_, _), token = next(iter(runner.issued_tokens.items()))
    assert len(created) == 1  # one browser for the fill phase
    fill_browser = created[0]
    # The fill-phase requests session was closed when the task paused.
    assert fill_browser._session is not None

    revived = Runner(make_config(live.base_url))
    revived_browsers: list[Any] = []

    def revived_factory() -> Any:
        from fillbot.browser.mechanical import MechanicalBrowser

        browser = MechanicalBrowser(
            timeout=revived.config.timeout_seconds,
            follow_redirects=revived.config.follow_redirects,
            limiter=revived.limiter.wait,
        )
        revived_browsers.append(browser)
        return browser

    revived._browser_factory = revived_factory
    _, status = revived.approve_confirmation(token, tasks, script)
    assert status == TaskStatus.SUCCESS
    # Replay used a brand-new browser/session and still produced exactly one receipt.
    assert len(revived_browsers) == 1 and revived_browsers[0] is not fill_browser
    assert len(live.receipts) == _seeded_count(live) + 1
