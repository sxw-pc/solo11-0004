"""Cross-batch dependency orchestration (opt-in, additive).

Public entry points:

* :func:`load_plan` / :func:`parse_plan` -- read and validate explicit dependency YAML
* :class:`CycleDetectedError` -- raised with the full cycle path on circular rules
* :class:`DependencyStore` -- separate SQLite store of workflow/batch state
* :class:`Orchestrator` -- event-driven scheduling on top of the unchanged Runner

Batches without a dependency plan never touch this package; the existing
``fillbot run`` / ``resume`` flow is unchanged.
"""
from fillbot.deps.graph import (
    adjacency,
    detect_cycles,
    effective_timeout,
    load_plan,
    parse_plan,
    topological_order,
)
from fillbot.deps.models import (
    BATCH_TERMINAL,
    CASCADE_BLOCK_STATUSES,
    WAITING_STATUSES,
    BatchSpec,
    BatchState,
    BatchStatus,
    CycleDetectedError,
    DependencyConfigError,
    DependencyError,
    DependencyPlan,
    WorkflowRecord,
    WorkflowStatus,
)
from fillbot.deps.orchestrator import Orchestrator
from fillbot.deps.store import DependencyStore

__all__ = [
    "adjacency",
    "detect_cycles",
    "effective_timeout",
    "load_plan",
    "parse_plan",
    "topological_order",
    "CycleDetectedError",
    "DependencyConfigError",
    "DependencyError",
    "DependencyPlan",
    "BatchSpec",
    "BatchState",
    "BatchStatus",
    "WorkflowRecord",
    "WorkflowStatus",
    "BATCH_TERMINAL",
    "CASCADE_BLOCK_STATUSES",
    "WAITING_STATUSES",
    "DependencyStore",
    "Orchestrator",
]
