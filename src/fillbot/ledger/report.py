"""Build and export run summaries from the ledger (no evidence dependency)."""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import TextIO

from fillbot.errors import ScriptError
from fillbot.ledger.db import Ledger
from fillbot.models import RunSummary
from fillbot.security.access import AccessPolicy, Principal


def build_summary(
    ledger: Ledger,
    run_id: str,
    *,
    principal: Principal | None = None,
    policy: AccessPolicy | None = None,
) -> RunSummary:
    run = ledger.get_run(run_id)
    if run is None:
        raise ScriptError(f"run not found: {run_id}")
    active_policy = policy or ledger.access_policy()
    if principal is not None and active_policy.enabled():
        # Explicit authorization for the run as a whole; a deny must surface as
        # an error rather than silently rendering an empty report.
        decision = active_policy.authorize(principal, "report.view", details={"run_id": run_id})
        if not decision.allowed:
            raise ScriptError(
                f"report access denied for {principal.identity}: {decision.reason}"
            )
    tasks = ledger.guarded_list_tasks(run_id, principal, policy=active_policy)
    counts = Counter(task.status for task in tasks)
    reasons = Counter(
        task.error_class or "UNKNOWN"
        for task in tasks
        if task.status == "failed" and (task.error_class or task.error_message)
    )
    durations = [task.duration_ms for task in tasks]
    return RunSummary(
        run_id,
        str(run["status"]),
        len(tasks),
        counts.get("success", 0),
        counts.get("failed", 0),
        counts.get("skipped", 0),
        dict(sorted(reasons.items())),
        round(sum(durations) / len(durations), 1) if durations else 0.0,
        [task.receipt_no for task in tasks if task.receipt_no],
        str(run["started_at"]),
        run["finished_at"],
    )


def export_summary(
    ledger: Ledger,
    run_id: str,
    output_dir: str | Path,
    *,
    principal: Principal | None = None,
    policy: AccessPolicy | None = None,
) -> tuple[Path, Path]:
    active_policy = policy or ledger.access_policy()
    decision = active_policy.authorize(principal, "report.export", details={"run_id": run_id})
    if not decision.allowed:
        raise ScriptError(
            f"report export denied for {principal.identity if principal else 'anonymous'}: {decision.reason}"
        )
    summary = build_summary(ledger, run_id, principal=principal, policy=active_policy)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path, csv_path = directory / "summary.json", directory / "summary.csv"
    json_path.write_text(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows: list[tuple[str, object]] = [
        ("tasks_total", summary.tasks_total),
        ("succeeded", summary.succeeded),
        ("failed", summary.failed),
        ("skipped", summary.skipped),
        ("average_duration_ms", summary.average_duration_ms),
        *((f"failure_reason:{key}", count) for key, count in summary.failure_reasons.items()),
        ("receipts", ";".join(summary.receipts)),
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        writer.writerows(rows)
    return json_path, csv_path


def render_summary(
    ledger: Ledger,
    run_id: str,
    *,
    principal: Principal | None = None,
    policy: AccessPolicy | None = None,
) -> str:
    summary = build_summary(ledger, run_id, principal=principal, policy=policy)
    lines = [
        f"run:      {summary.run_id}",
        f"status:   {summary.status}",
        f"tasks:    {summary.tasks_total} total, {summary.succeeded} succeeded, {summary.failed} failed, {summary.skipped} skipped",
        f"avg ms:   {summary.average_duration_ms}",
        f"reasons:  {summary.failure_reasons or '{}'}",
        f"receipts: {', '.join(summary.receipts) or '-'}",
        f"started:  {summary.started_at}",
        f"finished: {summary.finished_at or '-'}",
    ]
    tasks = ledger.guarded_list_tasks(run_id, principal, policy=policy)
    if tasks:
        lines.append("task detail:")
        for task in tasks:
            suffix = f" receipt={task.receipt_no}" if task.receipt_no else ""
            error = f" error={task.error_class}:{task.error_message}" if task.error_class else ""
            lines.append(f"  {task.task_id} [{task.status}] {task.duration_ms}ms{suffix}{error}")
    return "\n".join(lines)


def print_summary(
    ledger: Ledger,
    run_id: str,
    stream: TextIO | None = None,
    *,
    principal: Principal | None = None,
    policy: AccessPolicy | None = None,
) -> None:
    (stream or sys.stdout).write(
        render_summary(ledger, run_id, principal=principal, policy=policy) + "\n"
    )
