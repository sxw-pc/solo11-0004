"""Domain records for cross-batch dependency orchestration.

The dependency layer sits *above* the existing serial :class:`~fillbot.runner.scheduler.Runner`.
A *batch* is one existing unit of work (a task sheet plus a DSL script); a *workflow*
is a named set of batches whose ordering is governed by explicit ``depends_on`` rules.

Nothing here changes the on-disk task-sheet format or the ``run``/``resume`` entry
points.  Batches without dependencies keep running exactly as before.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from fillbot.models import iso_now


class BatchStatus(str, Enum):
    """Lifecycle of one batch inside a workflow.

    ``blocked`` is a terminal cascade state inherited from a failed/timed-out
    prerequisite; ``suspended`` is the live "waiting on prerequisites" state that
    consumes no execution resource (no browser, no worker, no network).
    """

    PENDING = "pending"        # declared, never evaluated
    SUSPENDED = "suspended"    # waiting for prerequisites (event-driven, no resource held)
    READY = "ready"            # prerequisites satisfied, eligible to run
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"        # cascade: an upstream dependency did not succeed
    TIMEOUT = "timeout"        # deadline elapsed before prerequisites were satisfied


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


# Statuses from which a batch may still start executing.
WAITING_STATUSES = frozenset({BatchStatus.PENDING, BatchStatus.SUSPENDED, BatchStatus.READY})
# Terminal statuses that never change again.
BATCH_TERMINAL = frozenset(
    {BatchStatus.SUCCESS, BatchStatus.FAILED, BatchStatus.BLOCKED, BatchStatus.TIMEOUT}
)
# A batch reaching any of these statuses cascades a block to its dependents.
CASCADE_BLOCK_STATUSES = frozenset({BatchStatus.FAILED, BatchStatus.BLOCKED, BatchStatus.TIMEOUT})

BATCH_TRANSITIONS = {
    BatchStatus.PENDING: frozenset(
        {BatchStatus.SUSPENDED, BatchStatus.READY, BatchStatus.RUNNING, BatchStatus.BLOCKED}
    ),
    BatchStatus.SUSPENDED: frozenset(
        {BatchStatus.READY, BatchStatus.RUNNING, BatchStatus.BLOCKED, BatchStatus.TIMEOUT}
    ),
    BatchStatus.READY: frozenset(
        {BatchStatus.RUNNING, BatchStatus.SUSPENDED, BatchStatus.BLOCKED, BatchStatus.TIMEOUT}
    ),
    BatchStatus.RUNNING: frozenset(
        {BatchStatus.SUCCESS, BatchStatus.FAILED, BatchStatus.SUSPENDED}
    ),
    BatchStatus.SUCCESS: frozenset(),
    BatchStatus.FAILED: frozenset(),
    BatchStatus.BLOCKED: frozenset(),
    BatchStatus.TIMEOUT: frozenset(),
}

WORKFLOW_TRANSITIONS = {
    WorkflowStatus.PENDING: frozenset({WorkflowStatus.RUNNING}),
    WorkflowStatus.RUNNING: frozenset(
        {WorkflowStatus.SUCCESS, WorkflowStatus.FAILED, WorkflowStatus.INTERRUPTED}
    ),
    WorkflowStatus.INTERRUPTED: frozenset(
        {WorkflowStatus.RUNNING, WorkflowStatus.SUCCESS, WorkflowStatus.FAILED}
    ),
    WorkflowStatus.SUCCESS: frozenset(),
    WorkflowStatus.FAILED: frozenset(),
}


@dataclass(frozen=True)
class BatchSpec:
    """One declared batch: a name plus the existing task-sheet / script paths."""

    name: str
    tasks: str
    script: str
    depends_on: tuple[str, ...] = ()
    timeout_seconds: int | None = None  # per-batch override of the workflow default

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DependencyPlan:
    """Parsed, validated dependency configuration for one workflow."""

    name: str
    batches: tuple[BatchSpec, ...]
    default_timeout_seconds: int | None = None
    poll_interval_seconds: float = 1.0

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(batch.name for batch in self.batches)

    def batch(self, name: str) -> BatchSpec:
        for spec in self.batches:
            if spec.name == name:
                return spec
        raise KeyError(name)

    def dependencies_of(self, name: str) -> tuple[str, ...]:
        return self.batch(name).depends_on

    def dependents_of(self) -> dict[str, list[str]]:
        """Reverse adjacency: prerequisite name -> batches that depend on it."""
        reverse: dict[str, list[str]] = {name: [] for name in self.names}
        for spec in self.batches:
            for prerequisite in spec.depends_on:
                reverse[prerequisite].append(spec.name)
        return reverse


@dataclass
class BatchState:
    """Persisted runtime state of a batch (one row in the dependency store)."""

    workflow_id: str
    name: str
    status: BatchStatus
    depends_on: tuple[str, ...] = ()
    run_id: str | None = None
    timeout_deadline: str | None = None
    reason: str | None = None
    blocked_by: tuple[str, ...] = ()
    updated_at: str = field(default_factory=iso_now)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["depends_on"] = list(self.depends_on)
        data["blocked_by"] = list(self.blocked_by)
        return data


@dataclass
class WorkflowRecord:
    workflow_id: str
    plan_name: str
    status: WorkflowStatus
    source: str
    started_at: str = field(default_factory=iso_now)
    finished_at: str | None = None
    default_timeout_seconds: int | None = None
    poll_interval_seconds: float = 1.0
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


class DependencyError(Exception):
    """Base class for dependency-layer errors with clear, operator-facing messages."""


class CycleDetectedError(DependencyError):
    """Raised at configuration/validation time; carries every detected cycle."""

    def __init__(self, cycles: list[list[str]]) -> None:
        self.cycles = cycles
        rendered = "; ".join(" -> ".join([*cycle, cycle[0]]) for cycle in cycles)
        super().__init__(f"cyclic batch dependency detected: {rendered}")


class DependencyConfigError(DependencyError):
    """Missing batch, bad path/timeout or structurally invalid dependency YAML."""
