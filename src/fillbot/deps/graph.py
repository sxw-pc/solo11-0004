"""Pure graph logic and YAML parsing for explicit batch dependencies.

Only *explicit* dependency declarations are supported: the robot performs write
operations whose ordering must be auditable, so dependencies are never guessed
from naming conventions.  Everything here is side-effect free (apart from reading
the YAML file) and unit tested in isolation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from fillbot.deps.models import BatchSpec, CycleDetectedError, DependencyConfigError, DependencyPlan

_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _require_mapping(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DependencyConfigError(f"{label} must be a mapping, got {type(raw).__name__}")
    return raw


def _coerce_names(value: Any, batch_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise DependencyConfigError(
            f"batch {batch_name!r}: depends_on must be a string or a list of strings"
        )
    names: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise DependencyConfigError(
                f"batch {batch_name!r}: depends_on entries must be non-empty strings"
            )
        names.append(item.strip())
    if len(set(names)) != len(names):
        raise DependencyConfigError(f"batch {batch_name!r}: duplicate entries in depends_on")
    return tuple(names)


def _timeout(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DependencyConfigError(f"{label} must be an integer number of seconds")
    if value <= 0:
        raise DependencyConfigError(f"{label} must be > 0 seconds")
    return value


def _poll_interval(value: Any) -> float:
    if value is None:
        return 1.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DependencyConfigError("poll_interval_seconds must be a number")
    interval = float(value)
    if interval <= 0:
        raise DependencyConfigError("poll_interval_seconds must be > 0")
    return interval


def parse_plan(raw: Any, *, source: str = "<memory>") -> DependencyPlan:
    """Validate a raw mapping (as loaded from YAML) into a :class:`DependencyPlan`."""
    root = _require_mapping(raw, "dependency plan")
    name = root.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DependencyConfigError("plan.name is required and must be a non-empty string")
    raw_batches = root.get("batches")
    if not isinstance(raw_batches, list) or not raw_batches:
        raise DependencyConfigError("plan.batches must be a non-empty list")

    specs: list[BatchSpec] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_batches, 1):
        batch = _require_mapping(entry, f"batches[{index}]")
        batch_name = batch.get("name")
        if not isinstance(batch_name, str) or not _NAME_RE.fullmatch(batch_name.strip()):
            raise DependencyConfigError(
                f"batches[{index}].name must match {_NAME_RE.pattern}, got {batch_name!r}"
            )
        batch_name = batch_name.strip()
        if batch_name in seen:
            raise DependencyConfigError(f"duplicate batch name: {batch_name!r}")
        seen.add(batch_name)

        tasks_path = batch.get("tasks")
        script_path = batch.get("script")
        if not isinstance(tasks_path, str) or not tasks_path.strip():
            raise DependencyConfigError(f"batch {batch_name!r}: tasks path is required")
        if not isinstance(script_path, str) or not script_path.strip():
            raise DependencyConfigError(f"batch {batch_name!r}: script path is required")

        unknown = set(batch) - {"name", "tasks", "script", "depends_on", "timeout_seconds"}
        if unknown:
            raise DependencyConfigError(
                f"batch {batch_name!r}: unknown key(s): {', '.join(sorted(unknown))}"
            )
        specs.append(
            BatchSpec(
                name=batch_name,
                tasks=tasks_path.strip(),
                script=script_path.strip(),
                depends_on=_coerce_names(batch.get("depends_on"), batch_name),
                timeout_seconds=_timeout(batch.get("timeout_seconds"), f"batch {batch_name!r}.timeout_seconds"),
            )
        )

    declared = {spec.name for spec in specs}
    for spec in specs:
        for prerequisite in spec.depends_on:
            if prerequisite not in declared:
                raise DependencyConfigError(
                    f"batch {spec.name!r} depends on unknown batch {prerequisite!r}"
                )
            if prerequisite == spec.name:
                raise DependencyConfigError(f"batch {spec.name!r} cannot depend on itself")

    unknown_top = set(root) - {
        "name", "batches", "default_timeout_seconds", "poll_interval_seconds",
    }
    if unknown_top:
        raise DependencyConfigError(f"unknown plan key(s): {', '.join(sorted(unknown_top))}")

    plan = DependencyPlan(
        name=name.strip(),
        batches=tuple(specs),
        default_timeout_seconds=_timeout(root.get("default_timeout_seconds"), "default_timeout_seconds"),
        poll_interval_seconds=_poll_interval(root.get("poll_interval_seconds")),
    )
    detect_cycles(plan)  # fail fast, before any work is scheduled
    return plan


def load_plan(path: str | Path) -> DependencyPlan:
    """Read and validate a dependency YAML file."""
    file_path = Path(path)
    if not file_path.is_file():
        raise DependencyConfigError(f"dependency file not found: {file_path}")
    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DependencyConfigError(f"invalid YAML in {file_path}: {exc}") from exc
    return parse_plan(raw, source=str(file_path))


def adjacency(plan: DependencyPlan) -> dict[str, tuple[str, ...]]:
    return {spec.name: spec.depends_on for spec in plan.batches}


def detect_cycles(plan: DependencyPlan) -> list[list[str]]:
    """Three-colour DFS returning every elementary cycle path found.

    White (unvisited) -> grey (on the current DFS stack) -> black (fully explored).
    An edge reaching a grey node closes a cycle; the stack slice reconstructs the
    exact path so the operator gets an actionable alert instead of a bare boolean.
    """
    graph = adjacency(plan)
    white, grey, black = 0, 1, 2
    colour: dict[str, int] = {name: white for name in graph}
    stack: list[str] = []
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        colour[node] = grey
        stack.append(node)
        for prerequisite in graph[node]:
            if colour[prerequisite] == white:
                visit(prerequisite)
            elif colour[prerequisite] == grey:
                start = stack.index(prerequisite)
                cycle = stack[start:]
                if cycle not in cycles:
                    cycles.append(cycle)
        stack.pop()
        colour[node] = black

    for node in graph:
        if colour[node] == white:
            visit(node)
    if cycles:
        raise CycleDetectedError(cycles)
    return []


def topological_order(plan: DependencyPlan) -> list[str]:
    """Kahn's algorithm; raises :class:`CycleDetectedError` on a cycle."""
    graph = adjacency(plan)
    indegree = {name: len(edges) for name, edges in graph.items()}
    # Deterministic order: process names alphabetically when several are ready.
    ready = sorted(name for name, degree in indegree.items() if degree == 0)
    order: list[str] = []
    dependents = plan.dependents_of()
    while ready:
        node = ready.pop(0)
        order.append(node)
        for dependent in sorted(dependents[node]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()
    if len(order) != len(graph):
        detect_cycles(plan)  # surfaces the concrete cycle paths
    return order


def effective_timeout(plan: DependencyPlan, spec: BatchSpec) -> int | None:
    """Per-batch override wins; otherwise the workflow default applies."""
    return spec.timeout_seconds if spec.timeout_seconds is not None else plan.default_timeout_seconds
