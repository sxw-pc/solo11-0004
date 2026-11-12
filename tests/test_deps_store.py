from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fillbot.deps.config import DepConfig
from fillbot.deps.models import BatchState, BatchStatus, WorkflowRecord, WorkflowStatus
from fillbot.deps.store import DependencyStore
from fillbot.errors import ConfigError


def _workflow(workflow_id: str = "WF-1") -> WorkflowRecord:
    return WorkflowRecord(
        workflow_id=workflow_id,
        plan_name="plan",
        status=WorkflowStatus.RUNNING,
        source="/tmp/plan.yaml",
        default_timeout_seconds=60,
    )


def test_store_schema_is_separate_from_task_ledger(tmp_path: Path) -> None:
    store = DependencyStore(tmp_path / "deps.db")
    with sqlite3.connect(store.path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables == {"workflows", "batch_states"}
    # The task ledger tables must never be created in the dependency store.
    assert "runs" not in tables and "tasks" not in tables


def test_workflow_and_batch_lifecycle_persistence(tmp_path: Path) -> None:
    store = DependencyStore(tmp_path / "deps.db")
    store.create_workflow(_workflow())
    assert store.get_workflow("WF-1")["status"] == "running"
    assert store.latest_workflow_id() == "WF-1"
    assert store.get_workflow("missing") is None and store.latest_workflow_id() is not None

    store.upsert_batch(BatchState("WF-1", "a", BatchStatus.READY))
    store.upsert_batch(
        BatchState("WF-1", "b", BatchStatus.SUSPENDED, depends_on=("a",), timeout_deadline="2026-01-01T00:00:00+00:00")
    )
    # Upsert updates rather than duplicating.
    store.upsert_batch(BatchState("WF-1", "a", BatchStatus.RUNNING, run_id="R-1"))
    rows = {state.name: state for state in store.list_batches("WF-1")}
    assert rows["a"].status == BatchStatus.RUNNING and rows["a"].run_id == "R-1"
    assert rows["b"].depends_on == ("a",) and rows["b"].timeout_deadline.startswith("2026")
    assert store.status_map("WF-1") == {"a": BatchStatus.RUNNING, "b": BatchStatus.SUSPENDED}
    assert store.get_batch("WF-1", "a") is not None and store.get_batch("WF-1", "x") is None

    store.update_workflow_status("WF-1", WorkflowStatus.SUCCESS, "2026-01-02T00:00:00+00:00")
    finished = store.get_workflow("WF-1")
    assert finished["status"] == "success" and finished["finished_at"] is not None


def test_blocked_by_tuple_round_trips(tmp_path: Path) -> None:
    store = DependencyStore(tmp_path / "deps.db")
    store.create_workflow(_workflow())
    store.upsert_batch(
        BatchState("WF-1", "d", BatchStatus.BLOCKED, blocked_by=("a", "c"), reason="upstream failed")
    )
    state = store.get_batch("WF-1", "d")
    assert state is not None and state.blocked_by == ("a", "c") and state.reason == "upstream failed"


def test_multiple_workflows_are_isolated(tmp_path: Path) -> None:
    store = DependencyStore(tmp_path / "deps.db")
    store.create_workflow(_workflow("WF-1"))
    store.create_workflow(_workflow("WF-2"))
    store.upsert_batch(BatchState("WF-1", "a", BatchStatus.SUCCESS))
    store.upsert_batch(BatchState("WF-2", "a", BatchStatus.FAILED))
    assert store.get_batch("WF-1", "a").status == BatchStatus.SUCCESS
    assert store.get_batch("WF-2", "a").status == BatchStatus.FAILED
    assert [row["workflow_id"] for row in store.list_workflows()] == ["WF-1", "WF-2"]


def test_dep_config_defaults_and_env_overrides() -> None:
    config = DepConfig.from_mapping(environ={})
    assert config.store_path.name == "dependencies.db"
    assert config.poll_interval_seconds == 1.0 and config.default_timeout_seconds is None

    env = {
        "FILLBOT_DEPS_STORE": "/tmp/x/deps.db",
        "FILLBOT_DEPS_POLL_INTERVAL": "0.25",
        "FILLBOT_DEPS_TIMEOUT": "120",
    }
    overridden = DepConfig.from_mapping(environ=env)
    assert str(overridden.store_path) == "/tmp/x/deps.db"
    assert overridden.poll_interval_seconds == 0.25 and overridden.default_timeout_seconds == 120


def test_dep_config_validation() -> None:
    with pytest.raises(ConfigError, match="unknown"):
        DepConfig.from_mapping({"nope": 1}, environ={})
    with pytest.raises(ConfigError, match="positive number"):
        DepConfig.from_mapping({"poll_interval_seconds": 0}, environ={})
    with pytest.raises(ConfigError, match="positive integer"):
        DepConfig.from_mapping({"default_timeout_seconds": -5}, environ={})
    with pytest.raises(ConfigError, match="numeric"):
        DepConfig.from_mapping(environ={"FILLBOT_DEPS_POLL_INTERVAL": "fast"})


def test_model_serialization_and_lookups() -> None:
    from fillbot.deps.models import (
        BatchSpec,
        BatchState,
        CycleDetectedError,
        DependencyConfigError,
        DependencyError,
        DependencyPlan,
        WorkflowRecord,
        WorkflowStatus,
    )

    spec_a = BatchSpec("a", "ta.json", "s.yaml")
    spec = BatchSpec("b", "t.json", "s.yaml", ("a",), 30)
    assert spec.as_dict()["depends_on"] == ("a",)
    plan = DependencyPlan("wf", (spec_a, spec), 100, 0.5)
    assert plan.names == ("a", "b") and plan.batch("b") is spec
    with pytest.raises(KeyError):
        plan.batch("missing")
    assert plan.dependents_of() == {"a": ["b"], "b": []}

    state = BatchState("WF", "b", BatchStatus.BLOCKED, depends_on=("a",), blocked_by=("a",))
    serialized = state.as_dict()
    assert serialized["status"] == "blocked"
    assert serialized["depends_on"] == ["a"] and serialized["blocked_by"] == ["a"]

    record = WorkflowRecord("WF", "wf", WorkflowStatus.RUNNING, "p.yaml")
    assert record.as_dict()["status"] == "running"

    cycle = CycleDetectedError([["a", "b"], ["x", "y"]])
    assert isinstance(cycle, DependencyError) and cycle.cycles == [["a", "b"], ["x", "y"]]
    assert "a -> b -> a" in str(cycle)
    assert isinstance(DependencyConfigError("x"), DependencyError)
