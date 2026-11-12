"""Serial run orchestration with evidence, ledger, idempotency and resume."""
from __future__ import annotations
import logging
import os
import re
import time
from collections import Counter
from typing import Callable, Sequence
from fillbot.browser.base import BrowserSession
from fillbot.browser.mechanical import MechanicalBrowser
from fillbot.config import BotConfig
from fillbot.dsl.engine import DSLExecutor
from fillbot.dsl.schema import Script
from fillbot.errors import BotError, ScriptError
from fillbot.evidence.store import EvidenceStore, is_sensitive
from fillbot.ledger.db import Ledger
from fillbot.ledger.report import export_summary
from fillbot.models import (
    ConfirmationRecord,
    RunRecord,
    RunStatus,
    TaskRecord,
    TaskStatus,
    elapsed_ms,
    iso_now,
    utcnow,
)
from fillbot.runner.gateway import ConfirmationGateway
from fillbot.runner.pacing import RateLimiter, new_run_id
from fillbot.runner.retry import RetryPolicy
from fillbot.runner.state import TERMINAL_TASK_STATUSES, plan_resume
RECEIPT_RE = re.compile(r"^RCP-\d{8}-\d{4}$")
BrowserFactory = Callable[[], BrowserSession]
# Task rows the run loop must never re-execute (human-gate states included).
_SKIP_ON_PLAN = TERMINAL_TASK_STATUSES | frozenset({"awaiting_confirmation"})
class Runner:
    def __init__(
        self,
        config: BotConfig,
        *,
        ledger: Ledger | None = None,
        evidence: EvidenceStore | None = None,
        browser_factory: BrowserFactory | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.ledger = ledger or Ledger(config.ledger_path)
        self.evidence = evidence or EvidenceStore(
            config.evidence_dir, mask_fields=config.mask_fields, keep_runs=config.evidence_keep_runs
        )
        self.sleeper = sleeper
        self.limiter = RateLimiter(config.rate_limit_per_minute, sleeper=sleeper)
        self._browser_factory = browser_factory
        self.gateway = ConfirmationGateway(self.ledger, self.evidence, clock=iso_now)
        # Plaintext tokens of tasks paused by this process (printed once by the CLI).
        self.issued_tokens: dict[tuple[str, str], str] = {}
        self._logger = logger
        self._file_handler: logging.FileHandler | None = None
    def _factory(self) -> BrowserSession:
        if self._browser_factory is not None:
            return self._browser_factory()
        return MechanicalBrowser(timeout=self.config.timeout_seconds, follow_redirects=self.config.follow_redirects, limiter=self.limiter.wait)
    def _setup_logging(self, run_id: str) -> logging.Logger:
        run_dir = self.config.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        logger = self._logger or logging.getLogger(f"fillbot.run.{run_id}")
        self._logger = logger
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        file_handler = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
        for handler in (logging.StreamHandler(), file_handler):
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        self._file_handler = file_handler
        return logger
    def _teardown_logging(self) -> None:
        if self._logger is not None and self._file_handler is not None:
            for handler in list(self._logger.handlers):
                self._logger.removeHandler(handler)
                handler.close()
            self._file_handler = None
    def _secrets(self, task: TaskRecord) -> list[str]:
        return [str(value) for key, value in task.payload.items() if value and is_sensitive(key, self.config.mask_fields)]
    def _task_context(self, task: TaskRecord) -> dict[str, object]:
        context: dict[str, object] = dict(task.payload)
        context.update({"task_id": task.task_id, "biz_key": task.biz_key, "attachments": [str(path) for path in task.attachments], "attachment": str(task.attachment) if task.attachment else ""})
        return context
    def _retry_policy(self) -> RetryPolicy:
        return RetryPolicy(self.config.max_retries + 1, self.config.backoff_base_seconds, self.config.backoff_max_seconds, self.config.jitter, sleeper=self.sleeper)
    def _filled_fields(self, task: TaskRecord) -> dict[str, object]:
        # Value-free inventory: only field names, never payload contents.
        return {key: True for key in sorted(task.payload) if not is_sensitive(key, self.config.mask_fields)}
    def _execute_task(
        self,
        run_id: str,
        task: TaskRecord,
        script: Script,
        logger: logging.Logger,
        dry_run: bool,
        *,
        gate_mode: str = "pause",
    ) -> tuple[TaskStatus, str | None, str | None, str | None, int, str, list]:
        started, started_at = utcnow(), iso_now()
        evidence_dir = str(self.evidence.task_dir(run_id, task.task_id))
        self.ledger.start_task(run_id, task, started_at, evidence_dir)
        logger.info("task %s started biz_key=%s gate_mode=%s", task.task_id, task.biz_key, gate_mode)
        secrets, snapshots = self._secrets(task), {}
        browser = self._factory()
        env = {**os.environ, "FILLBOT_BASE_URL": self.config.base_url}
        def snapshot_cb(step, page, status):
            snapshots[step.index] = str(
                self.evidence.snapshot(run_id, task.task_id, step.index, page, secret_values=secrets)
            )
        def action_cb(record):
            self.evidence.append_action(run_id, task.task_id, record)
        def retry_cb(step, attempt, error, delay):
            logger.warning(
                "task %s step %s action=%s retry=%s delay=%.3fs error=%s",
                task.task_id,
                step.index,
                step.action,
                attempt,
                delay,
                error.category.value,
            )
        def gate_cb(step):
            logger.info("task %s reached confirmation gate at step %s", task.task_id, step.index)
        try:
            executor = DSLExecutor(
                script,
                browser,
                task_context=self._task_context(task),
                run_id=run_id,
                env=env,
                default_retry=self._retry_policy(),
                snapshot_cb=snapshot_cb,
                action_log_cb=action_cb,
                retry_cb=retry_cb,
                gate_cb=gate_cb,
                gate_mode=gate_mode,
                sleeper=self.sleeper,
            )
            result = executor.execute(dry_run=dry_run)
            for step in result.steps:
                step.snapshot_path = snapshots.get(step.step_index)
                self.ledger.record_step(run_id, task.task_id, step)
            attempts = max((step.attempts for step in result.steps), default=1)
            receipt, status = result.receipt_no, result.status
            error_class, error_message = result.error_class, result.error
            if status == TaskStatus.AWAITING_CONFIRMATION:
                # The confirm page itself is already snapshotted by the step callback;
                # persist the durable gate state, masked review snapshot and token,
                # then release the server session while the human reviews.
                paused = self.gateway.pause(
                    run_id=run_id,
                    task_id=task.task_id,
                    biz_key=task.biz_key,
                    gate_step_index=result.gate_step_index or 0,
                    page=browser.page,
                    filled_fields=self._filled_fields(task),
                    mask_fields=self.config.mask_fields,
                    secret_values=secrets,
                )
                self.issued_tokens[(run_id, task.task_id)] = paused.token
                duration = elapsed_ms(started)
                self.ledger.finish_task(
                    run_id,
                    task.task_id,
                    status=TaskStatus.AWAITING_CONFIRMATION.value,
                    finished_at=iso_now(),
                    duration_ms=duration,
                    attempts=attempts,
                    evidence_dir=evidence_dir,
                )
                logger.info(
                    "task %s paused awaiting human confirmation id=%s; session closed",
                    task.task_id,
                    paused.confirmation.confirmation_id,
                )
                return TaskStatus.AWAITING_CONFIRMATION, None, None, None, attempts, evidence_dir, result.steps
            if status == TaskStatus.SUCCESS:
                problem = (
                    "script completed without extracting receipt_no"
                    if not receipt
                    else (
                        f"malformed receipt number: {receipt!r}"
                        if not RECEIPT_RE.fullmatch(receipt)
                        else f"duplicate receipt number: {receipt}" if self.ledger.receipt_exists(receipt) else None
                    )
                )
                if problem:
                    status, error_class, error_message, receipt = TaskStatus.FAILED, "ASSERT", problem, None
                else:
                    self.evidence.save_receipt(run_id, task.task_id, browser.page, secret_values=secrets)
            elif status == TaskStatus.DRY_RUN:
                receipt = None
            duration = elapsed_ms(started)
            self.ledger.finish_task(run_id, task.task_id, status=status.value, finished_at=iso_now(), duration_ms=duration, attempts=attempts, error_class=error_class, error_message=error_message, receipt_no=receipt if status == TaskStatus.SUCCESS else None, evidence_dir=evidence_dir)
            logger.info(
                "task %s finished status=%s class=%s receipt=%s",
                task.task_id,
                status.value,
                error_class or "-",
                receipt or "-",
            )
            return status, receipt, error_class, error_message, attempts, evidence_dir, result.steps
        except Exception as exc:
            error = exc if isinstance(exc, BotError) else ScriptError(str(exc))
            duration = elapsed_ms(started)
            self.ledger.finish_task(
                run_id,
                task.task_id,
                status=TaskStatus.FAILED.value,
                finished_at=iso_now(),
                duration_ms=duration,
                attempts=1,
                error_class=error.category.value,
                error_message=error.message,
                receipt_no=None,
                evidence_dir=evidence_dir,
            )
            logger.error("task %s crashed class=%s message=%s", task.task_id, error.category.value, error.message)
            return TaskStatus.FAILED, None, error.category.value, error.message, 1, evidence_dir, []
        finally:
            # Pause or finish: never hold a server-side session open across the wait.
            browser.close()
    def _finalize(self, run: RunRecord, tasks: Sequence[TaskRecord], *, interrupted: bool = False) -> RunRecord:
        rows = self.ledger.list_task_rows(run.run_id)
        counts = Counter(str(row["status"]) for row in rows)
        run.tasks_total = len(tasks)
        run.succeeded, run.failed, run.skipped = (
            counts.get("success", 0),
            counts.get("failed", 0),
            counts.get("skipped", 0),
        )
        run.abandoned = counts.get("abandoned", 0)
        awaiting = counts.get("awaiting_confirmation", 0)
        run.finished_at, run.evidence_dir = iso_now(), str(self.evidence.run_dir(run.run_id))
        if interrupted:
            run.status = RunStatus.INTERRUPTED
        elif awaiting:
            run.status = RunStatus.AWAITING_CONFIRMATION
            run.finished_at = None
        elif counts.get("failed", 0):
            run.status = RunStatus.FAILED
        elif counts.get("dry_run", 0):
            run.status = RunStatus.DRY_RUN
        else:
            run.status = RunStatus.SUCCESS
        self.ledger.finish_run(run)
        export_summary(self.ledger, run.run_id, self.config.runs_dir / run.run_id)
        # Keep review snapshots of runs still waiting on a human decision.
        self.evidence.cleanup(protect=self.ledger.runs_with_pending_confirmations())
        return run
    def run(self, tasks: Sequence[TaskRecord], script: Script, *, task_source: str = "", script_source: str = "", dry_run: bool = False, run_id: str | None = None, resume: bool = False, options: dict | None = None) -> RunRecord:
        if not tasks:
            raise ScriptError("task batch is empty")
        rid = run_id or new_run_id()
        logger = self._setup_logging(rid)
        if resume:
            existing = self.ledger.get_run(rid)
            if existing is None:
                raise ScriptError(f"run not found: {rid}")
            self.ledger.reopen_run(rid)
            run = RunRecord(
                rid,
                str(existing["started_at"]),
                task_source=str(existing["task_source"]),
                script_source=str(existing["script_source"]),
                dry_run=dry_run,
                base_url=self.config.base_url,
                options=options or {},
            )
            logger.info("resume run=%s remaining planned=%s", rid, len(tasks))
        else:
            run = RunRecord(
                rid,
                iso_now(),
                tasks_total=len(tasks),
                task_source=task_source,
                script_source=script_source,
                dry_run=dry_run,
                base_url=self.config.base_url,
                options=options or {},
            )
            self.ledger.create_run(run)
            logger.info("start run=%s tasks=%s dry_run=%s", rid, len(tasks), dry_run)
        try:
            completed = set(plan_resume(self.ledger, rid, tasks).completed_task_ids) if resume else set()
            current_rows = {str(row["task_id"]): str(row["status"]) for row in self.ledger.list_task_rows(rid)}
            for task in tasks:
                if task.task_id in completed:
                    logger.info("task %s already complete; skipped on resume", task.task_id)
                    continue
                if current_rows.get(task.task_id) in _SKIP_ON_PLAN:
                    # Awaiting human review or a human/terminal outcome: never auto-retry.
                    logger.info(
                        "task %s held at status=%s; not executed by the batch loop",
                        task.task_id,
                        current_rows.get(task.task_id),
                    )
                    continue
                if self.ledger.has_successful_biz_key(task.biz_key):
                    evidence_dir = str(self.evidence.run_dir(rid))
                    self.ledger.start_task(rid, task, iso_now(), evidence_dir)
                    self.ledger.finish_task(rid, task.task_id, status=TaskStatus.SKIPPED.value, finished_at=iso_now(), duration_ms=0, attempts=0, error_message="idempotent skip: biz_key already succeeded", receipt_no=None, evidence_dir=evidence_dir)
                    logger.info("task %s skipped idempotently", task.task_id)
                    continue
                self._execute_task(rid, task, script, logger, dry_run)
            return self._finalize(run, tasks)
        except BaseException:
            self._finalize(run, tasks, interrupted=True)
            raise
        finally:
            self._teardown_logging()
    def resume(self, run_id: str, tasks: Sequence[TaskRecord], script: Script, *, task_source: str = "", script_source: str = "", dry_run: bool = False) -> RunRecord:
        return self.run(tasks, script, task_source=task_source, script_source=script_source, dry_run=dry_run, run_id=run_id, resume=True, options={"resumed": True})
    # ------------------------------------------------------------------
    # Human confirmation gate: approve (session rebuild + safe replay) / reject
    # ------------------------------------------------------------------
    def _task_by_id(self, tasks: Sequence[TaskRecord], task_id: str) -> TaskRecord:
        for task in tasks:
            if task.task_id == task_id:
                return task
        raise ScriptError(f"task {task_id!r} not found in the original task sheet")
    def _run_logger(self, run_id: str) -> logging.Logger:
        existing = self._logger
        if existing is not None and getattr(existing, "name", "").endswith(run_id):
            return existing
        return self._setup_logging(run_id)
    def _refresh_run_summary(self, run_id: str, tasks: Sequence[TaskRecord]) -> RunRecord:
        row = self.ledger.get_run(run_id)
        assert row is not None
        run = RunRecord(
            run_id,
            str(row["started_at"]),
            task_source=str(row["task_source"]),
            script_source=str(row["script_source"]),
            dry_run=bool(row["dry_run"]),
            base_url=str(row["base_url"]),
        )
        return self._finalize(run, tasks)
    def list_pending_confirmations(self, *, run_id: str | None = None) -> list[ConfirmationRecord]:
        return self.gateway.list_pending(run_id=run_id)
    def approve_confirmation(
        self, token: str, tasks: Sequence[TaskRecord], script: Script, *, actor: str = "operator", note: str = ""
    ) -> tuple[ConfirmationRecord, TaskStatus]:
        """Consume the one-time token, rebuild the session and replay to the gate.

        The gate click never ran during the paused attempt, so replaying every
        step in a fresh session submits exactly once.
        """
        confirmation = self.gateway.decide(token, approve=True, actor=actor, note=note)
        run_id = confirmation.run_id
        logger = self._run_logger(run_id)
        task = self._task_by_id(tasks, confirmation.task_id)
        logger.info(
            "confirmation %s approved by %s; rebuilding session and replaying task %s",
            confirmation.confirmation_id,
            actor,
            task.task_id,
        )
        try:
            status, _, error_class, error_message, _, _, _ = self._execute_task(
                run_id, task, script, logger, dry_run=False, gate_mode="approve"
            )
        finally:
            self._refresh_run_summary(run_id, tasks)
            self._teardown_logging()
        if status != TaskStatus.SUCCESS:
            logger.error(
                "approved task %s failed after replay: %s %s", task.task_id, error_class or "-", error_message or ""
            )
        return confirmation, status
    def reject_confirmation(
        self, token: str, tasks: Sequence[TaskRecord], *, actor: str = "operator", note: str = ""
    ) -> ConfirmationRecord:
        """Record a human abandonment; the task becomes a terminal non-failure."""
        confirmation = self.gateway.decide(token, approve=False, actor=actor, note=note)
        run_id = confirmation.run_id
        logger = self._run_logger(run_id)
        self.ledger.finish_task(
            run_id,
            confirmation.task_id,
            status=TaskStatus.ABANDONED.value,
            finished_at=iso_now(),
            duration_ms=0,
            attempts=0,
            error_class=None,
            error_message=f"human-abandoned: {note}" if note else "human-abandoned by operator",
            receipt_no=None,
            evidence_dir=confirmation.evidence_dir,
        )
        task = self._task_by_id(tasks, confirmation.task_id)
        logger.info("confirmation %s rejected; task %s marked abandoned", confirmation.confirmation_id, task.task_id)
        self._refresh_run_summary(run_id, tasks)
        self._teardown_logging()
        return confirmation
    def approve_confirmations_batch(
        self,
        tokens: Sequence[str],
        tasks: Sequence[TaskRecord],
        script: Script,
        *,
        actor: str = "operator",
        note: str = "",
    ) -> list[tuple[ConfirmationRecord, TaskStatus]]:
        """Approve several one-time tokens in order; each still submits at most once."""
        if not tokens:
            raise ScriptError("at least one confirmation token is required for batch approval")
        results: list[tuple[ConfirmationRecord, TaskStatus]] = []
        for token in tokens:
            results.append(self.approve_confirmation(token, tasks, script, actor=actor, note=note))
        return results
