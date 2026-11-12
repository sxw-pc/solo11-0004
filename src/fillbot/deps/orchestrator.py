"""Event-driven dependency orchestrator built on top of the existing Runner.

Resource model
--------------
A suspended batch holds **no** browser, network connection or worker: it is a row
in ``DependencyStore`` plus an in-memory status.  The orchestrator loop wakes on a
short polling interval, recomputes which batches became ready, and only then hands
a batch to the unchanged serial :class:`~fillbot.runner.scheduler.Runner`.  Waiting
therefore costs one cheap SQLite read per tick regardless of how many batches wait.

Failure handling
----------------
When a batch ends in ``failed`` (or times out / is itself blocked), every still-waiting
transitive dependent is moved to the terminal ``blocked`` state with a ``blocked_by``
audit trail, so a prerequisite can never silently fail while downstream work starts.

Recovery
--------
All decisions are persisted before they are acted on.  On restart, ``resume`` loads
the workflow and batch rows; ``running`` batches left by a killed process are treated
as interrupted and re-driven through the normal Runner resume path, while suspended
batches simply continue waiting.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

from fillbot.config import BotConfig
from fillbot.deps.graph import DependencyPlan, effective_timeout
from fillbot.deps.models import (
    BATCH_TERMINAL,
    CASCADE_BLOCK_STATUSES,
    WAITING_STATUSES,
    BatchSpec,
    BatchState,
    BatchStatus,
    DependencyError,
    WorkflowRecord,
    WorkflowStatus,
)
from fillbot.deps.store import DependencyStore
from fillbot.dsl.schema import Script
from fillbot.intake import load_and_validate_tasks
from fillbot.models import RunRecord, TaskRecord, iso_now
from fillbot.runner.pacing import new_run_id
from fillbot.runner.scheduler import Runner

RunnerFactory = Callable[[], Runner]
Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Orchestrator:
    def __init__(
        self,
        config: BotConfig,
        store: DependencyStore,
        *,
        runner_factory: RunnerFactory | None = None,
        sleeper: Sleeper = __import__("time").sleep,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self._runner_factory = runner_factory or (lambda: Runner(config))
        self.sleeper = sleeper
        self.clock: Clock = clock or (lambda: datetime.now(timezone.utc))
        self.logger = logger or logging.getLogger("fillbot.deps")

    # ------------------------------------------------------------------ run
    def run(
        self,
        plan: DependencyPlan,
        *,
        source: str,
        dry_run: bool = False,
        workflow_id: str | None = None,
    ) -> WorkflowRecord:
        workflow_id = workflow_id or new_run_id()
        record = WorkflowRecord(
            workflow_id=workflow_id,
            plan_name=plan.name,
            status=WorkflowStatus.RUNNING,
            source=source,
            default_timeout_seconds=plan.default_timeout_seconds,
            poll_interval_seconds=plan.poll_interval_seconds,
            dry_run=dry_run,
        )
        self.store.create_workflow(record)
        for spec in plan.batches:
            self.store.upsert_batch(
                BatchState(
                    workflow_id=workflow_id,
                    name=spec.name,
                    status=BatchStatus.PENDING,
                    depends_on=spec.depends_on,
                )
            )
        self.logger.info("workflow %s started batches=%d dry_run=%s", workflow_id, len(plan.batches), dry_run)
        return self._drive(plan, record, dry_run=dry_run)

    def resume(self, workflow_id: str, *, dry_run: bool | None = None) -> WorkflowRecord:
        row = self.store.get_workflow(workflow_id)
        if row is None:
            raise DependencyError(f"workflow not found: {workflow_id}")
        plan = self._reload_plan(row)
        record = WorkflowRecord(
            workflow_id=workflow_id,
            plan_name=str(row["plan_name"]),
            status=WorkflowStatus.RUNNING,
            source=str(row["source"]),
            started_at=str(row["started_at"]),
            default_timeout_seconds=row["default_timeout_seconds"],
            poll_interval_seconds=float(row["poll_interval_seconds"]),
            dry_run=bool(row["dry_run"]) if dry_run is None else dry_run,
        )
        self.store.update_workflow_status(workflow_id, WorkflowStatus.RUNNING)
        self._reconcile_after_restart(plan, record)
        self.logger.info("workflow %s resumed", workflow_id)
        return self._drive(plan, record, dry_run=record.dry_run)

    def _reload_plan(self, row: dict) -> DependencyPlan:
        # Imported lazily to keep the graph module free of store concerns.
        from fillbot.deps.graph import load_plan

        return load_plan(row["source"])

    # ------------------------------------------------------------ recovery
    def _reconcile_after_restart(self, plan: DependencyPlan, record: WorkflowRecord) -> None:
        """Rebuild a consistent in-store state from whatever survived on disk."""
        known = {state.name: state for state in self.store.list_batches(record.workflow_id)}
        for spec in plan.batches:
            state = known.get(spec.name)
            if state is None:  # plan gained a batch after the first start
                self.store.upsert_batch(
                    BatchState(record.workflow_id, spec.name, BatchStatus.PENDING, spec.depends_on)
                )
                continue
            if state.status == BatchStatus.RUNNING:
                # Process died mid-batch: the underlying Runner run is interrupted and
                # will be resumed (or restarted) when the batch becomes due again.
                self._mark(
                    record.workflow_id,
                    spec,
                    BatchStatus.SUSPENDED,
                    reason="recovered after restart; underlying run interrupted",
                )

    # ------------------------------------------------------------- driving
    def _drive(self, plan: DependencyPlan, record: WorkflowRecord, *, dry_run: bool) -> WorkflowRecord:
        try:
            while True:
                states = {state.name: state for state in self.store.list_batches(record.workflow_id)}
                if all(state.status in BATCH_TERMINAL for state in states.values()):
                    break
                # Re-evaluate every waiting batch.  This is a pure status calculation
                # backed by SQLite, so it holds no browser/worker resource.
                progressed = self._evaluate_waiting(plan, record, states)
                progressed |= self._apply_timeouts(plan, record, states)
                progressed |= self._apply_cascade(plan, record, states)
                ready = self._ready_batches(plan, record.workflow_id)
                if not ready:
                    if progressed:
                        continue
                    # Nothing runnable and nothing changed: wait without holding any
                    # execution resource, then re-evaluate on the next tick.
                    self.sleeper(plan.poll_interval_seconds)
                    continue
                for spec in ready:
                    self._execute_batch(plan, record, spec, dry_run=dry_run)
            return self._finalize(record)
        except BaseException:
            self.store.update_workflow_status(record.workflow_id, WorkflowStatus.INTERRUPTED, iso_now())
            raise

    def _evaluate_waiting(
        self, plan: DependencyPlan, record: WorkflowRecord, states: dict[str, BatchState]
    ) -> bool:
        """Move waiting batches between pending/suspended/ready based on prerequisites."""
        changed = False
        for spec in plan.batches:
            state = states[spec.name]
            if state.status not in WAITING_STATUSES:
                continue
            self._ensure_deadline(plan, record, spec)
            if not spec.depends_on:
                target = BatchStatus.READY
            elif all(states[name].status == BatchStatus.SUCCESS for name in spec.depends_on):
                target = BatchStatus.READY
            else:
                target = BatchStatus.SUSPENDED
            if state.status != target:
                self._mark(record.workflow_id, spec, target)
                states[spec.name] = self.store.get_batch(record.workflow_id, spec.name)  # type: ignore[assignment]
                changed = True
        return changed

    def _ready_batches(self, plan: DependencyPlan, workflow_id: str) -> list[BatchSpec]:
        statuses = self.store.status_map(workflow_id)
        return [spec for spec in plan.batches if statuses.get(spec.name) == BatchStatus.READY]

    # ------------------------------------------------------------- timeouts
    def _apply_timeouts(
        self, plan: DependencyPlan, record: WorkflowRecord, states: dict[str, BatchState]
    ) -> bool:
        now = self.clock()
        changed = False
        for spec in plan.batches:
            state = states[spec.name]
            if state.status not in WAITING_STATUSES or not state.timeout_deadline:
                continue
            if now >= _parse_iso(state.timeout_deadline):
                self._mark(
                    record.workflow_id,
                    spec,
                    BatchStatus.TIMEOUT,
                    reason=(
                        f"dependency timeout after {effective_timeout(plan, spec)}s: prerequisites "
                        f"{list(spec.depends_on)} not all successful before deadline"
                    ),
                )
                states[spec.name] = self.store.get_batch(record.workflow_id, spec.name)  # type: ignore[assignment]
                changed = True
        return changed

    def _ensure_deadline(self, plan: DependencyPlan, record: WorkflowRecord, spec: BatchSpec) -> None:
        """Persist a timeout deadline when a batch first starts waiting."""
        state = self.store.get_batch(record.workflow_id, spec.name)
        if state is None or state.timeout_deadline is not None:
            return
        timeout = effective_timeout(plan, spec)
        if timeout is None:
            return
        deadline = (self.clock() + timedelta(seconds=timeout)).isoformat(timespec="milliseconds")
        state.timeout_deadline = deadline
        self.store.upsert_batch(state)

    # -------------------------------------------------------------- cascade
    def _apply_cascade(
        self, plan: DependencyPlan, record: WorkflowRecord, states: dict[str, BatchState]
    ) -> bool:
        """Propagate failure/block/timeout to every still-waiting transitive dependent."""
        changed = False
        dependents = plan.dependents_of()
        for upstream in plan.names:
            if states[upstream].status not in CASCADE_BLOCK_STATUSES:
                continue
            for dependent in dependents[upstream]:
                state = states[dependent]
                if state.status in WAITING_STATUSES:
                    blockers = self._blocking_prerequisites(record.workflow_id, plan, dependent)
                    self._mark(
                        record.workflow_id,
                        plan.batch(dependent),
                        BatchStatus.BLOCKED,
                        reason=f"blocked: prerequisite(s) {blockers} did not succeed",
                        blocked_by=tuple(blockers),
                    )
                    states[dependent] = self.store.get_batch(record.workflow_id, dependent)  # type: ignore[assignment]
                    changed = True
        return changed

    def _blocking_prerequisites(
        self, workflow_id: str, plan: DependencyPlan, name: str
    ) -> list[str]:
        statuses = self.store.status_map(workflow_id)
        return [
            prerequisite
            for prerequisite in plan.dependencies_of(name)
            if statuses.get(prerequisite) in CASCADE_BLOCK_STATUSES
        ]

    # ------------------------------------------------------------- execution
    def _execute_batch(
        self, plan: DependencyPlan, record: WorkflowRecord, spec: BatchSpec, *, dry_run: bool
    ) -> None:
        tasks: Sequence[TaskRecord] = load_and_validate_tasks(spec.tasks)
        from fillbot.dsl.schema import load_script

        script: Script = load_script(spec.script)
        prior = self.store.get_batch(record.workflow_id, spec.name)
        assert prior is not None
        runner = self._runner_factory()

        if prior.run_id:
            # Restart path: the run id survived a crash, so continue the exact
            # interrupted Runner run via its own ledger rather than start a new one.
            run_id = prior.run_id
            self._mark_running(record.workflow_id, spec, run_id)
            run: RunRecord = runner.resume(
                run_id,
                tasks,
                script,
                task_source=spec.tasks,
                script_source=spec.script,
                dry_run=dry_run,
            )
        else:
            # Pre-allocate the run id and persist (RUNNING, run_id) before any work.
            # A crash at any point leaves a resumable row instead of an orphan RUNNING.
            run_id = new_run_id()
            self._mark_running(record.workflow_id, spec, run_id)
            run = runner.run(
                tasks,
                script,
                task_source=spec.tasks,
                script_source=spec.script,
                dry_run=dry_run,
                run_id=run_id,
                options={"workflow_id": record.workflow_id, "batch": spec.name},
            )

        succeeded = run.status.value in {"success", "dry_run"}
        self._mark(
            record.workflow_id,
            spec,
            BatchStatus.SUCCESS if succeeded else BatchStatus.FAILED,
            reason=None if succeeded else f"batch run {run.run_id} ended with status {run.status.value}",
        )
        self.logger.info(
            "batch %s finished status=%s run_id=%s", spec.name, run.status.value, run.run_id
        )

    def _mark_running(self, workflow_id: str, spec: BatchSpec, run_id: str) -> None:
        state = self.store.get_batch(workflow_id, spec.name)
        assert state is not None
        state.status = BatchStatus.RUNNING
        state.run_id = run_id
        state.updated_at = iso_now()
        self.store.upsert_batch(state)

    # -------------------------------------------------------------- helpers
    def _mark(
        self,
        workflow_id: str,
        spec: BatchSpec,
        status: BatchStatus,
        *,
        reason: str | None = None,
        blocked_by: tuple[str, ...] = (),
    ) -> None:
        state = self.store.get_batch(workflow_id, spec.name)
        assert state is not None
        state.status = status
        state.reason = reason
        if blocked_by:
            state.blocked_by = blocked_by
        state.updated_at = iso_now()
        self.store.upsert_batch(state)

    def _finalize(self, record: WorkflowRecord) -> WorkflowRecord:
        states = self.store.list_batches(record.workflow_id)
        has_failure = any(
            state.status in (BatchStatus.FAILED, BatchStatus.BLOCKED, BatchStatus.TIMEOUT)
            for state in states
        )
        record.status = WorkflowStatus.FAILED if has_failure else WorkflowStatus.SUCCESS
        record.finished_at = iso_now()
        self.store.update_workflow_status(record.workflow_id, record.status, record.finished_at)
        self.logger.info("workflow %s finalized status=%s", record.workflow_id, record.status.value)
        return record
