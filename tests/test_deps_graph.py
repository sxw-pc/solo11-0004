from __future__ import annotations

import pytest

from fillbot.deps.graph import (
    detect_cycles,
    effective_timeout,
    parse_plan,
    topological_order,
)
from fillbot.deps.models import CycleDetectedError, DependencyConfigError, DependencyPlan


def _batch(name: str, deps: list[str] | None = None, timeout: int | None = None) -> dict:
    entry = {"name": name, "tasks": f"tasks/{name}.json", "script": "scripts/s.yaml"}
    if deps is not None:
        entry["depends_on"] = deps
    if timeout is not None:
        entry["timeout_seconds"] = timeout
    return entry


def _plan(batches: list[dict], **root: object) -> DependencyPlan:
    raw = {"name": "wf", "batches": batches}
    raw.update(root)
    return parse_plan(raw)


def test_parse_minimal_plan_and_dependencies() -> None:
    plan = _plan([_batch("a"), _batch("b", ["a"]), _batch("c", ["a", "b"])])
    assert plan.names == ("a", "b", "c")
    assert plan.dependencies_of("c") == ("a", "b")
    assert plan.dependents_of()["a"] == ["b", "c"]
    assert topological_order(plan) == ["a", "b", "c"]
    assert detect_cycles(plan) == []


def test_topological_order_is_stable_for_independent_branches() -> None:
    plan = _plan([_batch("zebra"), _batch("alpha"), _batch("mango")])
    assert topological_order(plan) == ["alpha", "mango", "zebra"]


def test_self_dependency_is_rejected() -> None:
    with pytest.raises(DependencyConfigError, match="cannot depend on itself"):
        _plan([_batch("a", ["a"])])


def test_unknown_prerequisite_is_rejected() -> None:
    with pytest.raises(DependencyConfigError, match="unknown batch"):
        _plan([_batch("a", ["ghost"])])


def test_duplicate_batch_name_and_dependency_entries_rejected() -> None:
    with pytest.raises(DependencyConfigError, match="duplicate batch name"):
        _plan([_batch("a"), _batch("a")])
    with pytest.raises(DependencyConfigError, match="duplicate entries"):
        _plan([_batch("a"), _batch("b", ["a", "a"])])


def test_simple_cycle_reports_full_path() -> None:
    raw = {
        "name": "cyclic",
        "batches": [_batch("a", ["c"]), _batch("b", ["a"]), _batch("c", ["b"])],
    }
    with pytest.raises(CycleDetectedError) as exc:
        parse_plan(raw)
    message = str(exc.value)
    assert "cyclic" in message
    # The alert must show the closed loop, not just a boolean.
    assert exc.value.cycles and set(exc.value.cycles[0]) == {"a", "b", "c"}
    assert message.endswith("b -> a")
    assert "a -> c -> b -> a" in message


def test_diamond_is_not_a_cycle() -> None:
    # a -> {b, c} -> d: shared prerequisite is fine.
    plan = _plan([_batch("a"), _batch("b", ["a"]), _batch("c", ["a"]), _batch("d", ["b", "c"])])
    assert topological_order(plan)[0] == "a"
    assert topological_order(plan)[-1] == "d"


def test_multiple_independent_cycles_are_all_reported() -> None:
    raw = {
        "name": "two-cycles",
        "batches": [
            _batch("a", ["b"]), _batch("b", ["a"]),
            _batch("x", ["y"]), _batch("y", ["x"]),
            _batch("root"),
        ],
    }
    with pytest.raises(CycleDetectedError) as exc:
        parse_plan(raw)
    cycles = {"".join(sorted(cycle)) for cycle in exc.value.cycles}
    assert cycles == {"ab", "xy"}


def test_structural_validation_errors() -> None:
    for bad in (
        {"name": "wf"},  # no batches
        {"name": "wf", "batches": []},  # empty
        {"name": "wf", "batches": "not-a-list"},
        {"name": "wf", "batches": [{"name": "bad name!", "tasks": "t", "script": "s"}]},
        {"name": "wf", "batches": [{"name": "a", "script": "s"}]},  # missing tasks
        {"name": "wf", "batches": [{"name": "a", "tasks": "t"}]},  # missing script
        {"name": "wf", "batches": [{"name": "a", "tasks": "t", "script": "s", "bogus": 1}]},
        {"name": "", "batches": [_batch("a")]},
        {"name": "wf", "batches": [_batch("a")], "mystery": 1},
    ):
        with pytest.raises(DependencyConfigError):
            parse_plan(bad)


def test_depends_on_type_and_timeout_validation() -> None:
    with pytest.raises(DependencyConfigError, match="depends_on"):
        _plan([_batch("a"), {"name": "b", "tasks": "t", "script": "s", "depends_on": [1, 2]}])
    with pytest.raises(DependencyConfigError, match="timeout"):
        _plan([_batch("a", timeout=0)])
    with pytest.raises(DependencyConfigError, match="timeout"):
        _plan([_batch("a", timeout="soon")])  # type: ignore[arg-type]
    with pytest.raises(DependencyConfigError, match="poll_interval"):
        _plan([_batch("a")], poll_interval_seconds=0)


def test_effective_timeout_prefers_batch_override() -> None:
    plan = _plan([_batch("a", timeout=30), _batch("b")], default_timeout_seconds=100)
    assert effective_timeout(plan, plan.batch("a")) == 30
    assert effective_timeout(plan, plan.batch("b")) == 100
    bare = _plan([_batch("no-timeout")])
    assert effective_timeout(bare, bare.batch("no-timeout")) is None


def test_load_plan_missing_file(tmp_path) -> None:
    from fillbot.deps.graph import load_plan

    with pytest.raises(DependencyConfigError, match="not found"):
        load_plan(tmp_path / "nope.yaml")


def test_load_plan_rejects_invalid_yaml(tmp_path) -> None:
    from fillbot.deps.graph import load_plan

    path = tmp_path / "bad.yaml"
    path.write_text("name: : :\n  - oops\n", encoding="utf-8")
    with pytest.raises(DependencyConfigError, match="invalid YAML"):
        load_plan(path)
