from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from fillbot.deps import orchestrator as orchestrator_module
from fillbot.deps.graph import parse_plan
from fillbot.deps.models import (
    BatchStatus,
    DependencyError,
    WorkflowRecord,
    WorkflowStatus,
)
from fillbot.deps.orchestrator import Orchestrator
from fillbot.deps.store import DependencyStore
from fillbot.models import RunRecord, RunStatus, iso_now
from fillbot.runner.pacing import new_run_id


@pytest.fixture(autouse=True)
def stub_batch_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    """The orchestrator loads real task sheets before invoking the runner.

    Unit tests inject a FakeRunner, so stub the file-level loaders; the real
    intake/DSL loading is covered by the end-to-end CLI tests.
    """
    monkeypatch.setattr(orchestrator_module, "load_and_validate_tasks", lambda path: [object()])
    monkeypatch.setattr("fillbot.dsl.schema.load_script", lambda path: object())
    # resume() reloads the plan from the recorded source; return an in-memory plan instead.
    monkeypatch.setattr(
        "fillbot.deps.graph.load_plan",
        lambda path: parse_plan({"name": "wf", "batches": [
            {"name": "a", "tasks": "tasks/a.json", "script": "scripts/s.yaml"},
            {"name": "b", "tasks": "tasks/b.json", "script": "scripts/s.yaml", "depends_on": ["a"]},
        ], "poll_interval_seconds": 0.001}),
    )


def _spec_plan(edges: dict[str, list[str]], *, timeouts: dict[str, int] | None = None,
               default_timeout: int | None = None, poll: float = 0.001) -> Any:
    batches = []
    for name in edges:
        entry: dict[str, Any] = {"name": name, "tasks": f"tasks/{name}.json", "script": "scripts/s.yaml"}
        if edges[name]:
            entry["depends_on"] = edges[name]
        if timeouts and name in timeouts:
            entry["timeout_seconds"] = timeouts[name]
        batches.append(entry)
    raw: dict[str, Any] = {"name": "wf", "batches": batches, "poll_interval_seconds": poll}
    if default_timeout is not None:
        raw["default_timeout_seconds"] = default_timeout
    return parse_plan(raw)


class FakeRunner:
    """Stand-in for the real Runner: records calls and returns scripted run results."""

    def __init__(self, outcomes: dict[str, str], calls: list[str], *, crash: str | None = None) -> None:
        self.outcomes = outcomes
        self.calls = calls
        self.crash = crash
        self.ledgers: dict[str, list[str]] = {}

    def run(self, tasks: Any, script: Any, **kwargs: Any) -> RunRecord:
        batch = kwargs["options"]["batch"]
        self.calls.append(batch)
        if self.crash == batch:
            raise SystemExit(23)
        return self._record(batch, kwargs.get("run_id"))

    def resume(self, run_id: str, tasks: Any, script: Any, **kwargs: Any) -> RunRecord:
        self.calls.append(f"resume:{run_id}")
        return self._record(self._batch_for_run(run_id), run_id)

    def _batch_for_run(self, run_id: str) -> str:
        for batch, ids in self.ledgers.items():
            if run_id in ids:
                return batch
        return run_id

    def _record(self, batch: str, run_id: str | None = None) -> RunRecord:
        run_id = run_id or new_run_id()
        self.ledgers.setdefault(batch, []).append(run_id)
        status_value = self.outcomes.get(batch, "success")
        return RunRecord(
            run_id=run_id,
            started_at=iso_now(),
            status=RunStatus(status_value),
            finished_at=iso_now(),
            tasks_total=1,
            succeeded=1 if status_value == "success" else 0,
            failed=0 if status_value == "success" else 1,
        )


def _orchestrator(tmp_path: Path, fake: FakeRunner, *, clock: Callable[[], datetime] | None = None,
                  sleeps: list[float] | None = None) -> Orchestrator:
    store = DependencyStore(tmp_path / "deps.db")
    return Orchestrator(
        config=object(),  # type: ignore[arg-type]
        store=store,
        runner_factory=lambda: fake,
        sleeper=(sleeps or []).append if sleeps is not None else lambda _: None,
        clock=clock,
    )


def test_happy_path_runs_in_topological_order(tmp_path: Path) -> None:
    plan = _spec_plan({"accounting": [], "energy": ["accounting"], "summary": ["energy"]})
    calls: list[str] = []
    orch = _orchestrator(tmp_path, FakeRunner({}, calls))
    record = orch.run(plan, source="plan.yaml")
    assert record.status == WorkflowStatus.SUCCESS
    assert calls == ["accounting", "energy", "summary"]
    statuses = orch.store.status_map(record.workflow_id)
    assert set(statuses.values()) == {BatchStatus.SUCCESS}
    # Every batch got a persisted run id (auditable back to the task ledger).
    assert all(state.run_id for state in orch.store.list_batches(record.workflow_id))


def test_independent_root_batches_both_run(tmp_path: Path) -> None:
    plan = _spec_plan({"a": [], "b": [], "c": ["a"]})
    calls: list[str] = []
    orch = _orchestrator(tmp_path, FakeRunner({}, calls))
    orch.run(plan, source="plan.yaml")
    assert set(calls[:2]) == {"a", "b"} and calls[-1] == "c"


def test_failed_prerequisite_cascades_block_to_dependents(tmp_path: Path) -> None:
    plan = _spec_plan({"a": [], "b": ["a"], "c": ["b"], "side": ["a"]})
    calls: list[str] = []
    fake = FakeRunner({"a": "failed"}, calls)
    orch = _orchestrator(tmp_path, fake)
    record = orch.run(plan, source="plan.yaml")
    # Only 'a' ever touched a runner; b, c and side were never executed.
    assert calls == ["a"]
    assert record.status == WorkflowStatus.FAILED
    states = {s.name: s for s in orch.store.list_batches(record.workflow_id)}
    assert states["a"].status == BatchStatus.FAILED
    for blocked in ("b", "c", "side"):
        assert states[blocked].status == BatchStatus.BLOCKED
        assert states[blocked].reason and "did not succeed" in states[blocked].reason
    # Transitive chain keeps the full audit trail.
    assert states["c"].blocked_by == ("b",)
    assert states["b"].blocked_by == ("a",)


def test_cascade_reaches_end_of_long_chain(tmp_path: Path) -> None:
    names = ["a", "b", "c", "d", "e"]
    edges = {name: ([prev] if index else []) for index, (prev, name) in enumerate(zip([None, *names], names))}
    plan = _spec_plan(edges)
    calls: list[str] = []
    orch = _orchestrator(tmp_path, FakeRunner({"a": "failed"}, calls))
    record = orch.run(plan, source="plan.yaml")
    states = {s.name: s for s in orch.store.list_batches(record.workflow_id)}
    assert calls == ["a"]  # nothing downstream ever consumed a runner
    for name in names[1:]:
        assert states[name].status == BatchStatus.BLOCKED
    # Each blocked node points at its immediate failed/blocked prerequisite.
    assert states["b"].blocked_by == ("a",)
    assert states["c"].blocked_by == ("b",)
    assert states["d"].blocked_by == ("c",)
    assert states["e"].blocked_by == ("d",)


def test_diamond_failure_blocks_all_branches_with_full_audit(tmp_path: Path) -> None:
    plan = _spec_plan({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]})
    calls: list[str] = []
    orch = _orchestrator(tmp_path, FakeRunner({"a": "failed"}, calls))
    record = orch.run(plan, source="plan.yaml")
    states = {s.name: s for s in orch.store.list_batches(record.workflow_id)}
    assert calls == ["a"]
    assert states["b"].status == states["c"].status == BatchStatus.BLOCKED
    assert states["d"].status == BatchStatus.BLOCKED
    assert set(states["d"].blocked_by) == {"b", "c"}


def test_suspended_batch_holds_no_resource_and_polls(tmp_path: Path) -> None:
    plan = _spec_plan({"a": [], "b": ["a"]}, poll=0.01)
    calls: list[str] = []
    sleeps: list[float] = []
    constructed: list[int] = []

    class CountingRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__({}, calls)
            constructed.append(1)

    store = DependencyStore(tmp_path / "deps.db")
    orch = Orchestrator(
        config=object(),  # type: ignore[arg-type]
        store=store,
        runner_factory=CountingRunner,
        sleeper=sleeps.append,
    )
    orch.run(plan, source="plan.yaml")
    # Exactly two runners were ever built: one per executed batch, none while waiting.
    assert len(constructed) == 2
    assert calls == ["a", "b"]


def test_timeout_terminates_waiting_batch_and_cascades(tmp_path: Path) -> None:
    plan = _spec_plan({"a": [], "b": ["a"], "c": ["b"]}, timeouts={"b": 100, "c": 100})
    current = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}

    def clock() -> datetime:
        return current["now"]

    class SlowFailingRunner(FakeRunner):
        def run(self, tasks: Any, script: Any, **kwargs: Any) -> RunRecord:
            batch = kwargs["options"]["batch"]
            self.calls.append(batch)
            assert batch == "a", "dependents must never execute"
            # a runs longer than b/c's wait deadline, then fails.
            current["now"] += timedelta(seconds=200)
            return RunRecord(
                run_id=new_run_id(), started_at=iso_now(), status=RunStatus.FAILED,
                finished_at=iso_now(), tasks_total=1, failed=1,
            )

    calls: list[str] = []
    store = DependencyStore(tmp_path / "deps.db")
    orch = Orchestrator(
        config=object(), store=store,  # type: ignore[arg-type]
        runner_factory=lambda: SlowFailingRunner({}, calls),
        sleeper=lambda _: None, clock=clock,
    )
    record = orch.run(plan, source="plan.yaml")
    states = {s.name: s for s in store.list_batches(record.workflow_id)}
    assert calls == ["a"]  # only the root batch ever consumed an execution resource
    assert states["a"].status == BatchStatus.FAILED
    # b and c are terminal and never ran; each carries an explicit reason.
    for name in ("b", "c"):
        assert states[name].status in (BatchStatus.TIMEOUT, BatchStatus.BLOCKED)
        assert states[name].reason
    # At least one timeout must have fired.
    assert any(s.status == BatchStatus.TIMEOUT for s in states.values())


def test_deadline_only_counts_wait_time(tmp_path: Path) -> None:
    plan = _spec_plan({"a": [], "b": ["a"]}, timeouts={"b": 60})
    current = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}

    def clock() -> datetime:
        return current["now"]

    class SlowRunner(FakeRunner):
        def run(self, tasks: Any, script: Any, **kwargs: Any) -> RunRecord:
            batch = kwargs["options"]["batch"]
            self.calls.append(batch)
            current["now"] += timedelta(seconds=30)  # a takes 30s; b has only waited 0s at readiness
            return self._record(batch)

    calls: list[str] = []
    store = DependencyStore(tmp_path / "deps.db")
    orch = Orchestrator(config=object(), store=store,  # type: ignore[arg-type]
                        runner_factory=lambda: SlowRunner({}, calls), sleeper=lambda _: None, clock=clock)
    record = orch.run(plan, source="plan.yaml")
    states = {s.name: s for s in store.list_batches(record.workflow_id)}
    # b became ready the moment a succeeded; its 60s wait budget was not consumed by a's runtime.
    assert states["b"].status == BatchStatus.SUCCESS and calls == ["a", "b"]


def test_restart_recovers_interrupted_batch_and_resumes(tmp_path: Path) -> None:
    plan = _spec_plan({"a": [], "b": ["a"]})
    store = DependencyStore(tmp_path / "deps.db")

    # First process: crash (SystemExit) while batch 'a' is running. The orchestrator
    # marks the workflow interrupted and leaves 'a' persisted as RUNNING.
    crash_calls: list[str] = []
    crash_orch = Orchestrator(
        config=object(), store=store,  # type: ignore[arg-type]
        runner_factory=lambda: FakeRunner({}, crash_calls, crash="a"),
        sleeper=lambda _: None,
    )
    with pytest.raises(SystemExit):
        crash_orch.run(plan, source="plan.yaml")
    wid = store.latest_workflow_id()
    assert wid is not None
    assert store.get_workflow(wid)["status"] == "interrupted"
    assert store.get_batch(wid, "a").status == BatchStatus.RUNNING

    # Simulate a fresh process: a brand-new orchestrator on the same on-disk store.
    calls2: list[str] = []
    fake2 = FakeRunner({}, calls2)
    orch2 = Orchestrator(
        config=object(),  # type: ignore[arg-type]
        store=DependencyStore(tmp_path / "deps.db"),
        runner_factory=lambda: fake2,
        sleeper=lambda _: None,
    )
    resumed = orch2.resume(wid)
    assert resumed.status == WorkflowStatus.SUCCESS
    # a was resumed via its persisted run id (not started fresh); b then ran normally.
    persisted_run = orch2.store.get_batch(wid, "a").run_id
    assert f"resume:{persisted_run}" in calls2
    assert "b" in calls2
    assert all(s.status == BatchStatus.SUCCESS for s in orch2.store.list_batches(wid))


def test_drive_sleeps_when_nothing_is_runnable_defensive(tmp_path: Path) -> None:
    # Defensive coverage: a non-terminal SUSPENDED batch whose prerequisite is not yet
    # SUCCESS makes the loop sleep (resource-free polling) instead of busy-spinning.
    plan = _spec_plan({"a": [], "b": ["a"]}, poll=0.02)
    store = DependencyStore(tmp_path / "deps.db")
    orch = Orchestrator(
        config=object(), store=store,  # type: ignore[arg-type]
        runner_factory=lambda: FakeRunner({}, []),
        sleeper=lambda _: None,
    )
    record = WorkflowRecord(
        workflow_id="WF-def", plan_name="wf", status=WorkflowStatus.RUNNING, source="plan.yaml"
    )
    store.create_workflow(record)
    from fillbot.deps.models import BatchState

    # 'a' is already SUCCESS; 'b' stays SUSPENDED until the one-shot sleep releases it.
    store.upsert_batch(BatchState("WF-def", "a", BatchStatus.SUCCESS))
    store.upsert_batch(BatchState("WF-def", "b", BatchStatus.SUSPENDED, depends_on=("a",)))

    sleeps: list[float] = []

    def one_shot(interval: float) -> None:
        sleeps.append(interval)

    orch.sleeper = one_shot

    # Neutralize evaluation so 'b' is not auto-promoted to READY before we can observe
    # the sleep path; then restore normal behaviour for convergence on the next tick.
    calls = {"n": 0}
    original_evaluate = orch._evaluate_waiting

    def gated_evaluate(plan_arg: Any, rec: Any, states: Any) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            return False  # pretend nothing changed: force the sleep branch
        return original_evaluate(plan_arg, rec, states)

    orch._evaluate_waiting = gated_evaluate  # type: ignore[method-assign]
    orch._drive(plan, record, dry_run=False)
    assert sleeps == [0.02]
    final = {s.name: s.status for s in store.list_batches("WF-def")}
    assert final["a"] == BatchStatus.SUCCESS and final["b"] == BatchStatus.SUCCESS


def test_resume_unknown_workflow_raises(tmp_path: Path) -> None:
    store = DependencyStore(tmp_path / "deps.db")
    orch = Orchestrator(config=object(), store=store, runner_factory=lambda: None,  # type: ignore[arg-type]
                        sleeper=lambda _: None)
    with pytest.raises(DependencyError, match="workflow not found"):
        orch.resume("nope")


def test_recovery_adopts_batch_added_to_plan_after_first_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First run completes a one-batch plan.
    plan_v1 = _spec_plan({"a": []})
    calls: list[str] = []
    store = DependencyStore(tmp_path / "deps.db")
    orch = Orchestrator(config=object(), store=store,  # type: ignore[arg-type]
                        runner_factory=lambda: FakeRunner({}, calls), sleeper=lambda _: None)
    record = orch.run(plan_v1, source="plan.yaml")

    # The plan file later gains batch 'b'. On resume it must be adopted, not crash.
    plan_v2 = _spec_plan({"a": [], "b": ["a"]})
    import fillbot.deps.graph as graph_module

    monkeypatch.setattr(graph_module, "load_plan", lambda path: plan_v2)
    calls2: list[str] = []
    fresh = Orchestrator(config=object(),  # type: ignore[arg-type]
                         store=DependencyStore(tmp_path / "deps.db"),
                         runner_factory=lambda: FakeRunner({}, calls2), sleeper=lambda _: None)
    fresh.resume(record.workflow_id)
    states = {s.name: s for s in fresh.store.list_batches(record.workflow_id)}
    assert set(states) == {"a", "b"}
    assert states["a"].status == BatchStatus.SUCCESS
    assert states["b"].status == BatchStatus.SUCCESS
    assert "b" in calls2


def test_recovery_marks_orphaned_running_as_suspended(tmp_path: Path) -> None:
    plan = _spec_plan({"a": [], "b": ["a"]})
    calls: list[str] = []
    store = DependencyStore(tmp_path / "deps.db")
    orch = _orchestrator(tmp_path, FakeRunner({}, calls))
    record = orch.run(plan, source="plan.yaml")
    run_id_of_b = store.get_batch(record.workflow_id, "b").run_id
    # Corrupt the store to mimic a process killed exactly between RUNNING persist and finish.
    state = store.get_batch(record.workflow_id, "b")
    assert state is not None
    state.status = BatchStatus.RUNNING
    state.reason = None
    store.upsert_batch(state)
    store.update_workflow_status(record.workflow_id, WorkflowStatus.INTERRUPTED)

    resume_calls: list[str] = []
    fresh = Orchestrator(
        config=object(),  # type: ignore[arg-type]
        store=DependencyStore(tmp_path / "deps.db"),
        runner_factory=lambda: FakeRunner({}, resume_calls),
        sleeper=lambda _: None,
    )
    fresh.resume(record.workflow_id)
    recovered = fresh.store.get_batch(record.workflow_id, "b")
    assert recovered is not None and recovered.status == BatchStatus.SUCCESS
    assert recovered.run_id == run_id_of_b  # same underlying Runner run was continued
    assert f"resume:{run_id_of_b}" in resume_calls
