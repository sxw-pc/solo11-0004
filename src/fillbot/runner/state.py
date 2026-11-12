"""Explicit state machines and resume planning for runs and tasks."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Sequence
from fillbot.errors import ScriptError
from fillbot.ledger.db import Ledger
from fillbot.models import TaskRecord, TaskStatus
TASK_TRANSITIONS = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.SKIPPED, TaskStatus.FAILED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.SUCCESS,
            TaskStatus.FAILED,
            TaskStatus.SKIPPED,
            TaskStatus.DRY_RUN,
            TaskStatus.AWAITING_CONFIRMATION,
        }
    ),
    TaskStatus.FAILED: frozenset({TaskStatus.RUNNING}),
    TaskStatus.AWAITING_CONFIRMATION: frozenset(
        {TaskStatus.RUNNING, TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.ABANDONED}
    ),
    TaskStatus.SUCCESS: frozenset(),
    TaskStatus.SKIPPED: frozenset(),
    TaskStatus.DRY_RUN: frozenset(),
    TaskStatus.ABANDONED: frozenset(),
}
# Terminal task statuses: a resumed run never re-executes these.
TERMINAL_TASK_STATUSES = frozenset({"success", "skipped", "abandoned", "dry_run"})
RUN_TRANSITIONS = {
    "running": frozenset({"success", "failed", "interrupted", "dry_run", "awaiting_confirmation"}),
    "interrupted": frozenset({"running", "success", "failed", "awaiting_confirmation"}),
    "awaiting_confirmation": frozenset({"running", "success", "failed", "interrupted"}),
    "failed": frozenset({"running", "success", "failed", "awaiting_confirmation"}),
    "success": frozenset(),
    "dry_run": frozenset(),
}
class _StateMachine:
    transitions: Any
    label = "state"
    def __init__(self, initial: Any) -> None:
        if initial not in self.transitions:
            raise ScriptError(f"invalid initial state: {initial}")
        self.status = initial
        self.history = [initial]
    def transition(self, target: Any) -> Any:
        if target not in self.transitions[self.status]:
            raise ScriptError(f"invalid {self.label} transition: {self.status} -> {target}")
        self.status = target
        self.history.append(target)
        return target
class TaskStateMachine(_StateMachine):
    transitions, label = TASK_TRANSITIONS, "task"
    def __init__(self, initial: Any = TaskStatus.PENDING) -> None:
        super().__init__(initial)
class RunStateMachine(_StateMachine):
    transitions, label = RUN_TRANSITIONS, "run"
    def __init__(self, initial: str = "running") -> None:
        super().__init__(initial)
@dataclass
class ResumePlan:
    run: dict[str, Any]
    remaining: list[TaskRecord]
    completed_task_ids: list[str]
    @property
    def run_id(self) -> str:
        return str(self.run["run_id"])
    @property
    def is_complete(self) -> bool:
        return not self.remaining
def plan_resume(ledger: Ledger, run_id: str, tasks: Sequence[TaskRecord]) -> ResumePlan:
    run = ledger.get_run(run_id)
    if run is None:
        raise ScriptError(f"run not found: {run_id}")
    completed = {
        str(row["task_id"])
        for row in ledger.list_task_rows(run_id)
        if row["status"] in TERMINAL_TASK_STATUSES
    }
    return ResumePlan(run, [task for task in tasks if task.task_id not in completed], sorted(completed))
