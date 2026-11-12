"""Build and export run summaries from the ledger (no evidence dependency)."""
from __future__ import annotations
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, TextIO
from fillbot.errors import ScriptError
from fillbot.ledger.db import Ledger
from fillbot.models import RunSummary, TaskSummary


def _root_cause_values(tasks: Iterable[TaskSummary]) -> list[TaskSummary]:
    return [task for task in tasks if task.status == "failed"]


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
    root_causes = Counter(
        task.root_cause or "UNKNOWN"
        for task in _root_cause_values(tasks)
        if task.error_class or task.error_message or task.root_cause
    )
    durations = [task.duration_ms for task in tasks]
    overhead = _diagnosis_overhead(ledger, run_id, tasks)
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
        dict(sorted(root_causes.items())),
        overhead,
    )


def _diagnosis_overhead(ledger: Ledger, run_id: str, tasks: Iterable[TaskSummary]) -> float:
    """Total/summary diagnostics wall time; falls back to zero for legacy rows."""
    total = 0.0
    for row in ledger.list_task_rows(run_id):
        try:
            total += float(row.get("diagnosis_ms") or 0.0)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
    return round(total, 3)


def root_cause_details(tasks: Iterable[TaskSummary]) -> dict[str, dict[str, Any]]:
    """Per-root-cause aggregation with labels, advice and evidence pointers."""
    grouped: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if task.status != "failed":
            continue
        key = task.root_cause or "UNKNOWN"
        entry = grouped.setdefault(
            key,
            {
                "label": task.root_cause_label or key,
                "suggestion": task.diagnosis_suggestion or "",
                "count": 0,
                "task_ids": [],
                "evidence_path": task.evidence_path or task.evidence_dir,
            },
        )
        entry["count"] += 1
        entry["task_ids"].append(task.task_id)
        if task.diagnosis_suggestion and not entry["suggestion"]:
            entry["suggestion"] = task.diagnosis_suggestion
        if task.evidence_path and not entry["evidence_path"]:
            entry["evidence_path"] = task.evidence_path
    return grouped


def export_summary(ledger: Ledger, run_id: str, output_dir: str | Path) -> tuple[Path, Path]:
    summary = build_summary(ledger, run_id)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path, csv_path = directory / "summary.json", directory / "summary.csv"
    tasks = ledger.list_tasks(run_id)
    payload = summary.as_dict()
    payload["root_cause_details"] = root_cause_details(tasks)
    payload["diagnosed_failures"] = [
        {
            "task_id": task.task_id,
            "root_cause": task.root_cause or "UNKNOWN",
            "label": task.root_cause_label or "",
            "suggestion": task.diagnosis_suggestion or "",
            "error_class": task.error_class,
            "error_message": task.error_message,
            "evidence_path": task.evidence_path or task.evidence_dir,
            "evidence": task.evidence_links,
        }
        for task in tasks
        if task.status == "failed"
    ]
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows: list[tuple[str, object]] = [
        ("tasks_total", summary.tasks_total),
        ("succeeded", summary.succeeded),
        ("failed", summary.failed),
        ("skipped", summary.skipped),
        ("average_duration_ms", summary.average_duration_ms),
        *((f"failure_reason:{key}", count) for key, count in summary.failure_reasons.items()),
        *((f"failure_root_cause:{key}", count) for key, count in summary.failure_root_causes.items()),
        *((f"root_cause_label:{key}", entry["label"]) for key, entry in root_cause_details(tasks).items()),
        ("diagnosis_overhead_ms", summary.diagnosis_overhead_ms),
        ("receipts", ";".join(summary.receipts)),
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        writer.writerows(rows)
    return json_path, csv_path


def render_summary(ledger: Ledger, run_id: str) -> str:
    summary = build_summary(ledger, run_id)
    tasks = ledger.list_tasks(run_id)
    lines = [
        f"run:      {summary.run_id}",
        f"status:   {summary.status}",
        f"tasks:    {summary.tasks_total} total, {summary.succeeded} succeeded, {summary.failed} failed, {summary.skipped} skipped",
        f"avg ms:   {summary.average_duration_ms}",
        f"reasons:  {summary.failure_reasons or '{}'}",
        f"roots:    {summary.failure_root_causes or '{}'}",
        f"receipts: {', '.join(summary.receipts) or '-'}",
        f"started:  {summary.started_at}",
        f"finished: {summary.finished_at or '-'}",
    ]
    if summary.failed:
        lines.append("failure diagnosis:")
        for task in tasks:
            if task.status != "failed":
                continue
            label = task.root_cause_label or task.root_cause or "UNKNOWN"
            lines.append(f"  {task.task_id} [{task.root_cause or 'UNKNOWN'}] {label}")
            if task.diagnosis_suggestion:
                lines.append(f"    advice: {task.diagnosis_suggestion}")
            if task.evidence_path:
                lines.append(f"    evidence: {task.evidence_path}")
            for link in task.evidence_links:
                if link.get("relative_path"):
                    lines.append(f"      {link.get('kind')}: {link['relative_path']}")
    if tasks:
        lines.append("task detail:")
        for task in tasks:
            suffix = f" receipt={task.receipt_no}" if task.receipt_no else ""
            error = f" error={task.error_class}:{task.error_message}" if task.error_class else ""
            root = f" root={task.root_cause}" if task.root_cause else ""
            lines.append(f"  {task.task_id} [{task.status}] {task.duration_ms}ms{suffix}{error}{root}")
    return "\n".join(lines)


def print_summary(ledger: Ledger, run_id: str, stream: TextIO | None = None) -> None:
    (stream or sys.stdout).write(render_summary(ledger, run_id) + "\n")
