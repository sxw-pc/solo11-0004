"""Human-readable rendering of workflow and batch dependency state."""
from __future__ import annotations

from typing import TextIO

from fillbot.deps.models import BatchStatus
from fillbot.deps.store import DependencyStore

_STATUS_GLYPH = {
    BatchStatus.PENDING: "·",
    BatchStatus.SUSPENDED: "⏸",
    BatchStatus.READY: "▶",
    BatchStatus.RUNNING: "●",
    BatchStatus.SUCCESS: "✓",
    BatchStatus.FAILED: "✗",
    BatchStatus.BLOCKED: "⊘",
    BatchStatus.TIMEOUT: "⏱",
}


def render_workflow(store: DependencyStore, workflow_id: str) -> str:
    workflow = store.get_workflow(workflow_id)
    if workflow is None:
        raise KeyError(workflow_id)
    lines = [
        f"workflow: {workflow_id} ({workflow['plan_name']})",
        f"status:   {workflow['status']}",
        f"started:  {workflow['started_at']}",
        f"finished: {workflow['finished_at'] or '-'}",
        f"dry_run:  {bool(workflow['dry_run'])}",
        "batches:",
    ]
    for state in store.list_batches(workflow_id):
        glyph = _STATUS_GLYPH.get(state.status, "?")
        deps = f" depends_on={list(state.depends_on)}" if state.depends_on else ""
        run = f" run_id={state.run_id}" if state.run_id else ""
        lines.append(f"  {glyph} {state.name} [{state.status.value}]{deps}{run}")
        if state.reason:
            lines.append(f"      reason: {state.reason}")
        if state.blocked_by:
            lines.append(f"      blocked_by: {list(state.blocked_by)}")
        if state.timeout_deadline and state.status in (BatchStatus.SUSPENDED, BatchStatus.READY):
            lines.append(f"      timeout_deadline: {state.timeout_deadline}")
    return "\n".join(lines)


def print_workflow(store: DependencyStore, workflow_id: str, stream: TextIO | None = None) -> None:
    import sys

    (stream or sys.stdout).write(render_workflow(store, workflow_id) + "\n")
