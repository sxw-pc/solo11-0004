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
def build_summary(ledger: Ledger, run_id: str) -> RunSummary:
    run = ledger.get_run(run_id)
    if run is None:
        raise ScriptError(f"run not found: {run_id}")
    tasks = ledger.list_tasks(run_id)
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
        counts.get("abandoned", 0),
        counts.get("awaiting_confirmation", 0),
    )
def export_summary(ledger: Ledger, run_id: str, output_dir: str | Path) -> tuple[Path, Path]:
    summary = build_summary(ledger, run_id)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path, csv_path = directory / "summary.json", directory / "summary.csv"
    json_path.write_text(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows: list[tuple[str, object]] = [
        ("tasks_total", summary.tasks_total),
        ("succeeded", summary.succeeded),
        ("failed", summary.failed),
        ("skipped", summary.skipped),
        ("abandoned", summary.abandoned),
        ("awaiting_confirmation", summary.awaiting_confirmation),
        ("average_duration_ms", summary.average_duration_ms),
        *((f"failure_reason:{key}", count) for key, count in summary.failure_reasons.items()),
        ("receipts", ";".join(summary.receipts)),
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        writer.writerows(rows)
    return json_path, csv_path
def render_summary(ledger: Ledger, run_id: str) -> str:
    summary = build_summary(ledger, run_id)
    lines = [
        f"run:      {summary.run_id}",
        f"status:   {summary.status}",
        f"tasks:    {summary.tasks_total} total, {summary.succeeded} succeeded, {summary.failed} failed, {summary.skipped} skipped, {summary.abandoned} human-abandoned, {summary.awaiting_confirmation} awaiting confirmation",
        f"avg ms:   {summary.average_duration_ms}",
        f"reasons:  {summary.failure_reasons or '{}'}",
        f"receipts: {', '.join(summary.receipts) or '-'}",
        f"started:  {summary.started_at}",
        f"finished: {summary.finished_at or '-'}",
    ]
    tasks = ledger.list_tasks(run_id)
    if tasks:
        lines.append("task detail:")
        for task in tasks:
            suffix = f" receipt={task.receipt_no}" if task.receipt_no else ""
            if task.status == "abandoned":
                detail = f" {task.error_message}" if task.error_message else ""
                lines.append(f"  {task.task_id} [abandoned] human-abandoned{detail}")
                continue
            error = f" error={task.error_class}:{task.error_message}" if task.error_class else ""
            lines.append(f"  {task.task_id} [{task.status}] {task.duration_ms}ms{suffix}{error}")
    return "\n".join(lines)
def print_summary(ledger: Ledger, run_id: str, stream: TextIO | None = None) -> None:
    (stream or sys.stdout).write(render_summary(ledger, run_id) + "\n")
