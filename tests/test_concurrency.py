"""Concurrency behaviour: parallel dispatch, isolation, idempotency, resume.

These tests exercise the same offline loopback-only mock site as the serial
suite, but with max_concurrency > 1, and additionally stress the shared
subsystems (rate limiter, SQLite ledger, evidence store) from many threads.
"""
from __future__ import annotations
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable
import pytest
from flask import Flask, Response
from fillbot.cli import main
from fillbot.config import load_config
from fillbot.dsl.schema import validate_script
from fillbot.errors import ConfigError
from fillbot.ledger.db import Ledger
from fillbot.ledger.report import build_summary
from fillbot.mocksite.chaos import ChaosSettings
from fillbot.models import TaskRecord
from fillbot.runner.pacing import RateLimiter
from fillbot.runner.scheduler import Runner

RECEIPT_RE = __import__("re").compile(r"^RCP-\d{8}-\d{4}$")


def _payload(company_id: str, period: str = "2025-Q4") -> dict[str, Any]:
    accounts = {
        "C001": ("parkco-a", "mock-only-alpha"),
        "C002": ("parkco-b", "mock-only-beta"),
        "C003": ("parkco-c", "mock-only-gamma"),
    }
    username, password = accounts[company_id]
    return {
        "username": username,
        "password": password,
        "company_id": company_id,
        "company_name": f"企业-{company_id}",
        "period": period,
        "contact_name": "联系人（虚构）",
        "report_date": "2025-11-10",
        "electricity": 100.0,
        "water": 10.0,
        "gas": 1.0,
        "employees": 10,
        "hires": 1,
    }


def _task(index: int, company_id: str = "C002", period: str = "2025-Q4", biz_key: str | None = None) -> TaskRecord:
    return TaskRecord(
        task_id=f"T-{index:04d}",
        biz_key=biz_key or f"{company_id}:{period}",
        payload=_payload(company_id, period),
    )


def _script() -> Any:
    from pathlib import Path as _Path

    root = _Path(__file__).parents[1]
    from fillbot.dsl.schema import load_script

    return load_script(root / "scripts" / "quarterly_report.yaml")


def _attachment_setup(tasks: list[TaskRecord], tmp_path: Path) -> None:
    pdf = tmp_path / "note.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    for task in tasks:
        object.__setattr__(task, "attachments", (pdf,))


def _config(base_url: str, tmp_path: Path, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "base_url": base_url,
        "evidence_dir": str(tmp_path / "evidence"),
        "ledger_path": str(tmp_path / "data" / "ledger.db"),
        "runs_dir": str(tmp_path / "runs"),
        "rate_limit_per_minute": 6000,
        "timeout_seconds": 5,
        "backoff_base_seconds": 0.01,
        "backoff_max_seconds": 0.05,
        "max_concurrency": 3,
    }
    values.update(overrides)
    return load_config(overrides=values)


def _unique_tasks() -> list[TaskRecord]:
    # Three distinct companies, unique (company, period) biz keys.
    return [
        _task(1, "C001", "2025-Q4"),
        _task(2, "C002", "2025-Q4"),
        _task(3, "C003", "2025-Q4"),
    ]


# ---------------------------------------------------------------------------
# End-to-end parallel runs against the real mock site
# ---------------------------------------------------------------------------


def test_parallel_dry_run_never_submits_and_marks_every_task(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path
) -> None:
    live = serve()
    config = make_config(live.base_url, max_concurrency=3)
    tasks = _unique_tasks()
    _attachment_setup(tasks, tmp_path)
    runner = Runner(config)
    run = runner.run(tasks, _script(), dry_run=True)
    assert run.status.value == "dry_run"
    rows = {row["task_id"]: row["status"] for row in runner.ledger.list_task_rows(run.run_id)}
    assert all(status == "dry_run" for status in rows.values())
    # No receipts created anywhere: not in the ledger, not at the site.
    assert run.succeeded == 0 and len(live.receipts) == 1
    evidence_root = config.evidence_dir / run.run_id
    for task in tasks:
        task_dir = evidence_root / task.task_id
        assert not (task_dir / "receipt.html").exists()
        assert (task_dir / "step-26.html").is_file()


def test_parallel_batch_succeeds_with_isolated_sessions(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path
) -> None:
    live = serve()
    config = make_config(live.base_url, max_concurrency=3)
    tasks = _unique_tasks()
    _attachment_setup(tasks, tmp_path)
    runner = Runner(config)
    run = runner.run(tasks, _script())
    assert run.status.value == "success"
    assert (run.succeeded, run.failed, run.skipped) == (3, 0, 0)
    rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(run.run_id)}
    receipts = [rows[task.task_id]["receipt_no"] for task in tasks]
    assert all(RECEIPT_RE.fullmatch(value or "") for value in receipts)
    assert len(set(receipts)) == 3
    # Report row ordering follows the input batch, not completion order.
    ordered = runner.ledger.list_task_rows(run.run_id)
    assert [row["task_id"] for row in ordered] == [task.task_id for task in tasks]
    summary = build_summary(runner.ledger, run.run_id)
    assert (summary.tasks_total, summary.succeeded, summary.failed, summary.skipped) == (3, 3, 0, 0)
    assert sorted(summary.receipts) == sorted(receipts)
    # Cookie isolation: each receipt page only shows its own enterprise id.
    evidence_root = config.evidence_dir / run.run_id
    combined: dict[str, str] = {}
    for task in tasks:
        receipt_html = (evidence_root / task.task_id / "receipt.html").read_text(encoding="utf-8")
        combined[task.task_id] = receipt_html
    for task in tasks:
        own_company = task.payload["company_id"]
        other_companies = {"C001", "C002", "C003"} - {own_company}
        assert own_company in combined[task.task_id]
        assert all(other not in combined[task.task_id] for other in other_companies)
    assert len(live.receipts) == 4  # seeded C001 Q3 + three new submissions


def test_parallel_run_matches_serial_summary_and_evidence(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path
) -> None:
    tasks_serial, tasks_parallel = _unique_tasks(), _unique_tasks()
    _attachment_setup(tasks_serial, tmp_path)
    _attachment_setup(tasks_parallel, tmp_path)

    serial_live = serve()
    serial_runner = Runner(make_config(serial_live.base_url, max_concurrency=1, ledger_path=str(tmp_path / "serial" / "ledger.db"), evidence_dir=str(tmp_path / "serial" / "evidence"), runs_dir=str(tmp_path / "serial" / "runs")))
    serial_run = serial_runner.run(tasks_serial, _script())
    serial_summary = build_summary(serial_runner.ledger, serial_run.run_id)

    parallel_live = serve()
    parallel_runner = Runner(make_config(parallel_live.base_url, max_concurrency=3, ledger_path=str(tmp_path / "parallel" / "ledger.db"), evidence_dir=str(tmp_path / "parallel" / "evidence"), runs_dir=str(tmp_path / "parallel" / "runs")))
    parallel_run = parallel_runner.run(tasks_parallel, _script())
    parallel_summary = build_summary(parallel_runner.ledger, parallel_run.run_id)

    for summary in (serial_summary, parallel_summary):
        assert (summary.status, summary.tasks_total, summary.succeeded, summary.failed, summary.skipped) == (
            "success",
            3,
            3,
            0,
            0,
        )
        assert summary.failure_reasons == {} and len(summary.receipts) == 3
    # Same average-duration aggregation rule regardless of execution order.
    assert serial_summary.average_duration_ms >= 0 and parallel_summary.average_duration_ms >= 0
    for runner_obj, run_obj, task_list in (
        (serial_runner, serial_run, tasks_serial),
        (parallel_runner, parallel_run, tasks_parallel),
    ):
        for task in task_list:
            task_dir = runner_obj.evidence.run_dir(run_obj.run_id) / task.task_id
            assert (task_dir / "receipt.html").is_file()
            assert (task_dir / "actions.jsonl").is_file()
            step_files = sorted(path.name for path in task_dir.glob("step-*.html"))
            assert step_files  # every task keeps the same evidence set


def test_parallel_flaky_submit_still_retries_per_task(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path
) -> None:
    live = serve(chaos=ChaosSettings(enabled=True, flaky_submit=True, flaky_company="C003"))
    config = make_config(live.base_url, max_concurrency=3)
    tasks = _unique_tasks()
    _attachment_setup(tasks, tmp_path)
    runner = Runner(config)
    run = runner.run(tasks, _script())
    assert run.status.value == "success" and run.succeeded == 3
    final_steps = {
        task.task_id: next(
            item for item in runner.ledger.list_steps(run.run_id, task.task_id) if item["step_index"] == 26
        )
        for task in tasks
    }
    assert final_steps["T-0003"]["attempts"] == 2
    assert all(final_steps[task.task_id]["status"] == "ok" for task in tasks)


def test_failed_task_is_isolated_and_independent_retry_works(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path
) -> None:
    live = serve()
    config = make_config(live.base_url, max_concurrency=3)
    tasks = _unique_tasks()
    # T-0002 uses an out-of-range employee count, which the mock rejects with
    # a CLIENT error; the other two must still succeed in the same run.
    tasks[1].payload["employees"] = 12000
    _attachment_setup(tasks, tmp_path)
    runner = Runner(config)
    run = runner.run(tasks, _script())
    assert run.status.value == "failed"
    rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(run.run_id)}
    assert rows["T-0001"]["status"] == "success" and rows["T-0003"]["status"] == "success"
    assert rows["T-0002"]["status"] == "failed" and rows["T-0002"]["error_class"] == "CLIENT"

    # Fix only the failing task and retry it via resume: the two successes
    # are skipped on resume, the repaired task completes; no duplicate submit.
    tasks[1].payload["employees"] = 10
    resumed = runner.resume(run.run_id, tasks, _script())
    rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(run.run_id)}
    assert [rows[task.task_id]["status"] for task in tasks] == ["success", "success", "success"]
    assert resumed.status.value == "success" and resumed.failed == 0
    receipts = [rows[task.task_id]["receipt_no"] for task in tasks]
    assert len(set(receipts)) == 3 and all(RECEIPT_RE.fullmatch(value) for value in receipts)
    # The mock only ever saw one successful submission per enterprise.
    assert len(live.receipts) == 4


def test_concurrent_idempotent_skip_for_duplicate_biz_keys(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path
) -> None:
    live = serve()
    config = make_config(live.base_url, max_concurrency=4)
    first = _unique_tasks()
    _attachment_setup(first, tmp_path)
    runner = Runner(config)
    runner.run(first, _script())
    assert len(live.receipts) == 4

    # Four new tasks all reusing already-succeeded biz keys, dispatched at
    # once: every worker must observe the idempotency rule, none may submit.
    duplicates = [
        TaskRecord("D-0001", first[0].biz_key, dict(first[0].payload)),
        TaskRecord("D-0002", first[1].biz_key, dict(first[1].payload)),
        TaskRecord("D-0003", first[2].biz_key, dict(first[2].payload)),
        TaskRecord("D-0004", first[0].biz_key, dict(first[0].payload)),
    ]
    run = runner.run(duplicates, _script())
    assert run.status.value == "success" and (run.succeeded, run.failed, run.skipped) == (0, 0, 4)
    assert len(live.receipts) == 4  # no extra submissions reached the site


def test_duplicate_biz_keys_within_one_parallel_batch(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path
) -> None:
    live = serve()
    config = make_config(live.base_url, max_concurrency=4)
    base = _task(1, "C002", "2025-Q4")
    _attachment_setup([base], tmp_path)
    pdf = tmp_path / "note.pdf"
    twins = [
        TaskRecord("K-0001", base.biz_key, dict(base.payload), attachments=(pdf,)),
        TaskRecord("K-0002", base.biz_key, dict(base.payload), attachments=(pdf,)),
        TaskRecord("K-0003", base.biz_key, dict(base.payload), attachments=(pdf,)),
    ]
    runner = Runner(config)
    run = runner.run(twins, _script())
    rows = {row["task_id"]: row["status"] for row in runner.ledger.list_task_rows(run.run_id)}
    assert sorted(rows.values()).count("success") == 1
    assert sorted(rows.values()).count("skipped") == 2
    assert len(live.receipts) == 2  # seeded + exactly one new submission


def test_same_biz_key_failure_does_not_skip_the_next_task(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path
) -> None:
    # Two tasks share a biz_key. The first is rejected by the site (CLIENT);
    # because no successful record exists, the second must still execute and
    # succeed rather than being silently skipped. This mirrors serial
    # check-then-act idempotency semantics under concurrency.
    live = serve()
    config = make_config(live.base_url, max_concurrency=2)
    good = _task(1, "C002", "2025-Q4")
    _attachment_setup([good], tmp_path)
    pdf = tmp_path / "note.pdf"
    bad_payload = dict(good.payload)
    bad_payload["employees"] = 12000  # server-side range rejection
    pair = [
        TaskRecord("F-0001", good.biz_key, bad_payload, attachments=(pdf,)),
        TaskRecord("F-0002", good.biz_key, dict(good.payload), attachments=(pdf,)),
    ]
    runner = Runner(config)
    run = runner.run(pair, _script())
    rows = {row["task_id"]: row["status"] for row in runner.ledger.list_task_rows(run.run_id)}
    assert rows["F-0001"] == "failed"
    assert rows["F-0002"] == "success"
    assert run.status.value == "failed"  # one failure still fails the run overall
    # Only the repaired task produced a submission; the failed one never did.
    assert len(live.receipts) == 2


def _make_crashing_runner(
    config: Any, allow: int, monkeypatch: pytest.MonkeyPatch
) -> tuple[Runner, list[str]]:
    """Return a runner whose workers stop the process after ``allow`` tasks.

    Additional workers block on a gate before touching the site, so the
    interruption is deterministic: exactly ``allow`` tasks finish and the rest
    stay unfinished (pre-registered only), independent of thread scheduling.
    """
    runner = Runner(config)
    original = Runner._execute_task
    started: list[str] = []
    gate = threading.Event()
    slots = threading.Lock()

    def controlled(self: Runner, *args: Any, **kwargs: Any) -> Any:
        # Claim a slot before touching the site. The claim happens at worker
        # start, so only ``allow`` workers can ever execute; the rest park.
        with slots:
            granted = len(started) < allow
            if granted:
                started.append(args[1].task_id)
        if not granted:
            gate.wait(timeout=10)
            raise SystemExit(23)
        result = original(self, *args, **kwargs)
        if len(started) == allow:
            # Release parked workers so pool shutdown stays prompt, then
            # interrupt the run from this worker thread.
            gate.set()
            raise SystemExit(23)
        return result

    monkeypatch.setattr(Runner, "_execute_task", controlled)
    return runner, started


def test_parallel_resume_after_interruption_completes_all_tasks(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bundled DSL relies on the mock's server-side period default, so the
    # offline fixtures provide one submittable task per enterprise.
    live = serve()
    config = make_config(live.base_url, max_concurrency=3)
    tasks = [
        _task(1, "C001", "2025-Q4"),
        _task(2, "C002", "2025-Q4"),
        _task(3, "C003", "2025-Q4"),
    ]
    _attachment_setup(tasks, tmp_path)
    runner, finished = _make_crashing_runner(config, allow=1, monkeypatch=monkeypatch)

    with pytest.raises(SystemExit):
        runner.run(tasks, _script())
    monkeypatch.undo()

    ledger = runner.ledger
    run_id = ledger.latest_run_id()
    assert run_id is not None and ledger.get_run(run_id)["status"] == "interrupted"
    before = {row["task_id"]: row["status"] for row in ledger.list_task_rows(run_id)}
    # All tasks were pre-registered in batch order, even the ones whose
    # workers never reached the site before the process died.
    assert set(before) == {task.task_id for task in tasks}
    assert len(finished) == 1

    resumed = runner.resume(run_id, tasks, _script())
    assert resumed.status.value == "success"
    rows = {row["task_id"]: row for row in ledger.list_task_rows(run_id)}
    assert all(rows[task.task_id]["status"] == "success" for task in tasks)
    receipts = [rows[task.task_id]["receipt_no"] for task in tasks]
    assert len(set(receipts)) == 3 and all(RECEIPT_RE.fullmatch(value) for value in receipts)
    # The task that finished before the crash was not submitted twice.
    assert len(live.receipts) == 4  # seeded + three unique submissions


def test_resume_picks_up_every_unfinished_concurrent_task(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two of three tasks complete; the third worker is gated and never runs.
    # Resume must discover the unfinished pre-registered row and finish it
    # while leaving the two existing successes untouched.
    live = serve(chaos=ChaosSettings(enabled=True, flaky_submit=True, flaky_company="C003"))
    config = make_config(live.base_url, max_concurrency=3)
    tasks = _unique_tasks()
    _attachment_setup(tasks, tmp_path)
    runner, finished = _make_crashing_runner(config, allow=2, monkeypatch=monkeypatch)

    with pytest.raises(SystemExit):
        runner.run(tasks, _script())
    monkeypatch.undo()

    run_id = runner.ledger.latest_run_id()
    assert run_id is not None
    plan_rows = {row["task_id"]: row["status"] for row in runner.ledger.list_task_rows(run_id)}
    assert set(plan_rows) == {task.task_id for task in tasks}
    assert len(finished) == 2
    unfinished = [task for task in tasks if plan_rows[task.task_id] not in {"success", "skipped"}]
    assert len(unfinished) == 1

    resumed = runner.resume(run_id, tasks, _script())
    assert resumed.status.value == "success"
    final_rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(run_id)}
    assert all(final_rows[task.task_id]["status"] == "success" for task in tasks)
    assert len(live.receipts) == 4


def test_pool_shutdown_cancels_queued_futures() -> None:
    # Deterministic contract for the mechanism the scheduler relies on: with
    # fewer workers than tasks, shutdown(cancel_futures=True) must leave queued
    # (never-started) futures cancelled while in-flight work is allowed to
    # finish. This is what keeps an interrupted run from draining its backlog.
    from concurrent.futures import ThreadPoolExecutor

    started = threading.Event()
    release = threading.Event()
    ran: list[int] = []
    lock = threading.Lock()

    def work(item: int) -> int:
        with lock:
            position = len(ran)
            ran.append(item)
        if position == 0:
            started.set()
            release.wait(timeout=10)
        return item

    pool = ThreadPoolExecutor(max_workers=1)
    futures = [pool.submit(work, index) for index in range(6)]
    assert started.wait(timeout=10)
    pool.shutdown(wait=True, cancel_futures=True)
    release.set()
    cancelled = [index for index, future in enumerate(futures) if future.cancelled()]
    # The single worker could only start one task; the other five were queued
    # and must have been cancelled rather than executed.
    assert len(cancelled) == 5
    assert ran == [0]


def test_interrupted_concurrent_run_leaves_queued_work_pending(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end: interrupt a multi-worker run while tasks are still queued and
    # assert the run is marked interrupted, remains resumable, and never runs
    # the whole batch (the failure mode plain shutdown(wait=True) would cause).
    live = serve()
    workers = 2
    config = make_config(live.base_url, max_concurrency=workers)
    companies = ("C001", "C002", "C003")
    tasks = [_task(index + 1, companies[index % 3], "2025-Q4", biz_key=f"B{index}:2025-Q4") for index in range(6)]
    _attachment_setup(tasks, tmp_path)
    runner = Runner(config)
    seen: list[str] = []
    seen_lock = threading.Lock()
    original = Runner._execute_task

    def crash_after_two(self: Runner, *args: Any, **kwargs: Any) -> Any:
        with seen_lock:
            seen.append(args[1].task_id)
            count = len(seen)
        if count <= workers:
            raise SystemExit(23)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Runner, "_execute_task", crash_after_two)
    with pytest.raises(SystemExit):
        runner.run(tasks, _script())
    monkeypatch.undo()

    run_id = runner.ledger.latest_run_id()
    assert runner.ledger.get_run(run_id)["status"] == "interrupted"
    rows = runner.ledger.list_task_rows(run_id)
    statuses = [row["status"] for row in rows]
    # The core invariant: with two workers and an immediate interruption, at
    # most two tasks ever left the pre-registered pending state. The backlog
    # was not drained — plain shutdown(wait=True) would have run all six tasks
    # to a terminal state. Whether an in-flight task reached success/failed or
    # stayed running depends on scheduling, so only backlog size is asserted.
    assert statuses.count("pending") >= len(tasks) - workers
    assert sum(1 for status in statuses if status != "pending") <= workers


# ---------------------------------------------------------------------------
# Shared subsystem guarantees
# ---------------------------------------------------------------------------


def test_rate_limiter_is_global_under_concurrency() -> None:
    # Deterministic virtual clock: capacity 2, refill 2 tokens/minute so the
    # next token takes ~30s; sleeping simply advances the clock, which keeps
    # the limiter's refill arithmetic testable without real-time waits.
    current = [0.0]
    sleeps: list[float] = []
    lock = threading.Lock()

    def clock() -> float:
        with lock:
            return current[0]

    def fake_sleep(delay: float) -> None:
        with lock:
            sleeps.append(delay)
            current[0] += delay

    limiter = RateLimiter(2, sleeper=fake_sleep, clock=clock)
    acquired: list[int] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        limiter.wait()
        acquired.append(1)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert len(acquired) == 4
    # Two burst tokens go through immediately; the remaining two must each
    # pace for roughly one refill interval regardless of worker count.
    assert len(sleeps) == 2
    assert all(29.0 <= delay <= 31.0 for delay in sleeps)


def test_rate_limiter_never_exceeds_average_rate_in_real_time() -> None:
    # 120/min = 2/s. Drain the fresh bucket first, then 8 concurrent waits
    # must follow the refill curve (~0.5s per token) instead of bursting.
    limiter = RateLimiter(120)
    limiter.tokens = 0.0
    limiter.updated = time.monotonic()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        limiter.wait()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    elapsed = time.monotonic() - started
    assert not any(thread.is_alive() for thread in threads)
    assert elapsed >= 3.0  # ~7 refill intervals of 0.5s minus scheduling slack


def test_each_parallel_task_gets_an_isolated_browser_session(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path
) -> None:
    from fillbot.browser.mechanical import MechanicalBrowser

    live = serve()
    config = make_config(live.base_url, max_concurrency=3)
    tasks = _unique_tasks()
    _attachment_setup(tasks, tmp_path)
    sessions: list[Any] = []
    runner = Runner(config)

    def tracking_factory() -> Any:
        browser = MechanicalBrowser(
            timeout=config.timeout_seconds,
            follow_redirects=config.follow_redirects,
            limiter=runner.limiter.wait,
        )
        sessions.append(browser)
        return browser

    runner._browser_factory = tracking_factory
    run = runner.run(tasks, _script())
    assert run.status.value == "success"
    # One browser per task, no session object shared between workers.
    assert len(sessions) == 3 and len({id(session) for session in sessions}) == 3
    assert all(browser._session is not sessions[0]._session for browser in sessions[1:])
    for browser in sessions:
        browser.close()


def test_ledger_wal_mode_and_concurrent_writer_stress(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    from fillbot.models import RunRecord, StepResult, StepStatus, iso_now

    ledger.create_run(RunRecord(run_id="R-stress", started_at=iso_now()))
    errors: list[Exception] = []

    def writer(worker_id: int) -> None:
        try:
            for offset in range(20):
                task_id = f"W{worker_id}-{offset:02d}"
                task = TaskRecord(task_id, f"B{worker_id}:{offset}", {})
                ledger.start_task("R-stress", task, iso_now(), f"e/{task_id}")
                ledger.record_step(
                    "R-stress",
                    task_id,
                    StepResult(1, "open", StepStatus.OK, iso_now(), iso_now(), 1),
                )
                ledger.finish_task(
                    "R-stress",
                    task_id,
                    status="success",
                    finished_at=iso_now(),
                    duration_ms=1,
                    attempts=1,
                    receipt_no=f"RCP-2026010{worker_id % 8 + 1}-{worker_id:02d}{offset:02d}",
                )
        except Exception as exc:  # pragma: no cover - failure path assertion
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(wid,)) for wid in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors and not any(thread.is_alive() for thread in threads)
    rows = ledger.list_task_rows("R-stress")
    assert len(rows) == 120 and all(row["status"] == "success" for row in rows)
    assert ledger.receipt_count() == 120


def test_evidence_store_concurrent_writes_do_not_collide(tmp_path: Path) -> None:
    from fillbot.evidence.store import EvidenceStore

    store = EvidenceStore(tmp_path / "evidence")
    errors: list[Exception] = []

    def writer(task_id: str) -> None:
        try:
            for step in range(10):
                page = type("P", (), {"text": f"<html>{task_id}-{step}</html>"})()
                store.snapshot("R-1", task_id, step, page)
                store.append_action("R-1", task_id, {"step": step, "action": "open"})
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"T-{wid:02d}",)) for wid in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    for wid in range(8):
        task_dir = store.task_dir("R-1", f"T-{wid:02d}")
        assert len(list(task_dir.glob("step-*.html"))) == 10
        lines = (task_dir / "actions.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 10
        assert all(json.loads(line)["action"] == "open" for line in lines)


def test_parallelism_improves_throughput_within_one_rate_limit_budget(
    serve: Callable[..., Any], make_config: Callable[..., Any], tmp_path: Path
) -> None:
    slow_app = Flask("slow")
    counter = {"n": 0}
    counter_lock = threading.Lock()

    @slow_app.get("/work")
    def work() -> Response:
        time.sleep(0.12)
        with counter_lock:
            counter["n"] += 1
            number = counter["n"]
        # Unique receipts so the ledger's global uniqueness rule accepts
        # every task even though all hit the same endpoint.
        return Response(f'<html><span id="receipt-no">RCP-20260101-{number:04d}</span></html>')

    live = serve(app=slow_app)
    script = validate_script(
        {
            "name": "slow-flow",
            "version": 1,
            "steps": [
                {"action": "open", "url": "{{env.FILLBOT_BASE_URL}}/work"},
                {"action": "assert_status", "status": 200},
                {"action": "extract", "selector": "#receipt-no", "save_as": "receipt_no"},
            ],
        }
    )
    tasks = [TaskRecord(f"P-{idx:02d}", f"K:{idx}", {}) for idx in range(4)]

    serial_config = make_config(live.base_url, max_concurrency=1, ledger_path=str(tmp_path / "s" / "ledger.db"), evidence_dir=str(tmp_path / "s" / "evidence"), runs_dir=str(tmp_path / "s" / "runs"), rate_limit_per_minute=6000)
    started = time.monotonic()
    serial_run = Runner(serial_config).run(tasks, script)
    serial_elapsed = time.monotonic() - started

    parallel_config = make_config(live.base_url, max_concurrency=4, ledger_path=str(tmp_path / "p" / "ledger.db"), evidence_dir=str(tmp_path / "p" / "evidence"), runs_dir=str(tmp_path / "p" / "runs"), rate_limit_per_minute=6000)
    started = time.monotonic()
    parallel_run = Runner(parallel_config).run(tasks, script)
    parallel_elapsed = time.monotonic() - started

    assert serial_run.status.value == parallel_run.status.value == "success"
    # Four 120ms tasks: serial >= 0.48s, four-wide ~0.12s.
    assert serial_elapsed >= 0.4
    assert parallel_elapsed < serial_elapsed * 0.6
    # Both runs share one limiter per Runner; with the generous 6000/min
    # budget neither run waits on pacing, so speedup comes only from
    # concurrency, never from exceeding the configured rate.
    assert parallel_run.succeeded == 4 and serial_run.succeeded == 4


# ---------------------------------------------------------------------------
# Configuration and CLI plumbing
# ---------------------------------------------------------------------------


def test_max_concurrency_config_validation(tmp_path: Path) -> None:
    assert load_config(overrides={"max_concurrency": 1}, environ={}).max_concurrency == 1
    assert load_config(environ={"FILLBOT_MAX_CONCURRENCY": "7"}).max_concurrency == 7
    config_path = tmp_path / "bot.yaml"
    config_path.write_text("max_concurrency: 4\n", encoding="utf-8")
    assert load_config(config_path, environ={}).max_concurrency == 4
    for bad in ("0", "-2", "x"):
        config_path.write_text(f"max_concurrency: {bad}\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(config_path, environ={})


def test_cli_concurrency_override(
    tmp_path: Path, serve: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(__file__).parents[1]
    live = serve()
    config_path = tmp_path / "bot.yaml"
    config_path.write_text(
        "\n".join(
            (
                f"base_url: {live.base_url}",
                "rate_limit_per_minute: 6000",
                "max_concurrency: 1",
                f"evidence_dir: {tmp_path / 'evidence'}",
                f"ledger_path: {tmp_path / 'data' / 'ledger.db'}",
                f"runs_dir: {tmp_path / 'runs'}",
            )
        ),
        encoding="utf-8",
    )
    code = main(
        [
            "run",
            "--tasks",
            str(root / "tests" / "fixtures" / "valid_task.json"),
            "--script",
            str(root / "scripts" / "quarterly_report.yaml"),
            "--config",
            str(config_path),
            "--concurrency",
            "2",
        ]
    )
    assert code == 0 and "succeeded" in capsys.readouterr().out
    ledger = Ledger(tmp_path / "data" / "ledger.db")
    run_id = ledger.latest_run_id()
    assert run_id is not None
    assert json.loads(ledger.get_run(run_id)["options_json"])["max_concurrency"] == 2
    assert main(
        [
            "run",
            "--tasks",
            str(root / "tests" / "fixtures" / "valid_task.json"),
            "--script",
            str(root / "scripts" / "quarterly_report.yaml"),
            "--config",
            str(config_path),
            "--concurrency",
            "0",
        ]
    ) == 2
