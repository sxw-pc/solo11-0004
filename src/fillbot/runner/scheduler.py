"""Serial run orchestration with evidence, ledger, idempotency and resume."""
from __future__ import annotations
import logging
import os
import re
import time
from collections import Counter
from typing import Any, Callable, Sequence
from fillbot.browser.base import BrowserSession
from fillbot.browser.mechanical import MechanicalBrowser
from fillbot.config import BotConfig
from fillbot.diagnostics.collector import attach_evidence, classify_error, classify_failure_fields
from fillbot.diagnostics.models import Diagnosis
from fillbot.diagnostics.rules import STAGE_RECEIPT, STAGE_RUNTIME, STAGE_SUBMIT
from fillbot.dsl.engine import DSLExecutor
from fillbot.dsl.schema import Script
from fillbot.errors import BotError, ScriptError
from fillbot.evidence.store import EvidenceStore, is_sensitive
from fillbot.ledger.db import Ledger
from fillbot.ledger.report import export_summary
from fillbot.models import RunRecord, RunStatus, StepStatus, TaskRecord, TaskStatus, elapsed_ms, iso_now, utcnow
from fillbot.runner.pacing import RateLimiter, new_run_id
from fillbot.runner.retry import RetryPolicy
from fillbot.runner.state import plan_resume
RECEIPT_RE = re.compile(r"^RCP-\d{8}-\d{4}$")
BrowserFactory = Callable[[], BrowserSession]
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

    def _diagnose_task_error(
        self,
        error: BotError,
        *,
        task: TaskRecord,
        run_id: str,
        evidence_dir: str,
        browser: BrowserSession,
        snapshot_path: str | None,
        stage: str = STAGE_RUNTIME,
    ) -> Diagnosis | None:
        """Classify a crash-level failure and attach evidence pointers (inert on error)."""
        started = time.perf_counter()
        try:
            page = getattr(error, "page", None)
            if page is None:
                try:
                    page = browser.page
                except Exception:
                    page = None
            _, diagnosis = classify_error(
                error,
                stage=stage,
                resources=[str(path) for path in task.attachments],
                page=page,
            )
            attach_evidence(
                diagnosis,
                evidence_dir=evidence_dir,
                snapshot_path=snapshot_path,
                action_log_path=str(self.evidence.action_log_path(run_id, task.task_id)),
                receipt_path=str(self.evidence.receipt_path(run_id, task.task_id)),
            )
            diagnosis.overhead_ms = round((time.perf_counter() - started) * 1000, 3)
            return diagnosis
        except Exception:  # pragma: no cover - diagnostics must be inert
            return None

    def _execute_task(self, run_id: str, task: TaskRecord, script: Script, logger: logging.Logger, dry_run: bool) -> tuple[TaskStatus, str | None, str | None, str | None, int, str, list]:
        started, started_at = utcnow(), iso_now()
        evidence_dir = str(self.evidence.task_dir(run_id, task.task_id))
        self.ledger.start_task(run_id, task, started_at, evidence_dir)
        logger.info("task %s started biz_key=%s", task.task_id, task.biz_key)
        secrets, snapshots = self._secrets(task), {}
        browser = self._factory()
        env = {**os.environ, "FILLBOT_BASE_URL": self.config.base_url}
        def snapshot_cb(step, page, status):
            snapshots[step.index] = str(
                self.evidence.snapshot(run_id, task.task_id, step.index, page, secret_values=secrets)
            )
        def action_cb(record):
            self.evidence.append_action(run_id, task.task_id, record)
        def exchange_cb(record):
            self.evidence.append_exchange(run_id, task.task_id, record)
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
                exchange_cb=exchange_cb,
                sleeper=self.sleeper,
            )
            result = executor.execute(dry_run=dry_run)
            for step in result.steps:
                step.snapshot_path = snapshots.get(step.step_index)
                self.ledger.record_step(run_id, task.task_id, step)
            attempts = max((step.attempts for step in result.steps), default=1)
            receipt, status = result.receipt_no, result.status
            error_class, error_message = result.error_class, result.error
            diagnosis: Diagnosis | None = getattr(result, "diagnosis", None)
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
                    diagnosis = self._receipt_diagnosis(
                        problem,
                        error_class="ASSERT",
                        task=task,
                        run_id=run_id,
                        browser=browser,
                        snapshots=snapshots,
                        evidence_dir=evidence_dir,
                    )
                else:
                    self.evidence.save_receipt(run_id, task.task_id, browser.page, secret_values=secrets)
            elif status == TaskStatus.DRY_RUN:
                receipt = None
            if status == TaskStatus.FAILED:
                failure_step = next(
                    (step for step in reversed(result.steps) if step.status == StepStatus.FAILED), None
                )
                failure_snapshot = (
                    failure_step.snapshot_path or snapshots.get(failure_step.step_index)
                    if failure_step is not None
                    else (snapshots.get(max(snapshots)) if snapshots else None)
                )
                if diagnosis is not None:
                    attach_evidence(
                        diagnosis,
                        evidence_dir=evidence_dir,
                        snapshot_path=failure_snapshot,
                        action_log_path=str(self.evidence.action_log_path(run_id, task.task_id)),
                        receipt_path=str(self.evidence.receipt_path(run_id, task.task_id)),
                    )
                else:
                    diagnosis = self._runtime_diagnosis(
                        task=task,
                        run_id=run_id,
                        browser=browser,
                        evidence_dir=evidence_dir,
                        snapshots=snapshots,
                        result=result,
                    )
                if failure_step is not None and diagnosis is not None and failure_step.root_cause is None:
                    failure_step.root_cause = diagnosis.root_cause.value
                    self.ledger.record_step(run_id, task.task_id, failure_step)
            duration = elapsed_ms(started)
            self.ledger.finish_task(run_id, task.task_id, status=status.value, finished_at=iso_now(), duration_ms=duration, attempts=attempts, error_class=error_class, error_message=error_message, receipt_no=receipt if status == TaskStatus.SUCCESS else None, evidence_dir=evidence_dir, diagnosis=diagnosis)
            logger.info(
                "task %s finished status=%s class=%s receipt=%s root_cause=%s",
                task.task_id,
                status.value,
                error_class or "-",
                receipt or "-",
                getattr(getattr(diagnosis, "root_cause", None), "value", None) or "-",
            )
            return status, receipt, error_class, error_message, attempts, evidence_dir, result.steps
        except Exception as exc:
            error = exc if isinstance(exc, BotError) else ScriptError(str(exc))
            duration = elapsed_ms(started)
            diagnosis = self._diagnose_task_error(
                error,
                task=task,
                run_id=run_id,
                evidence_dir=evidence_dir,
                browser=browser,
                snapshot_path=max(snapshots.values(), key=lambda path: path) if snapshots else None,
            )
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
                diagnosis=diagnosis,
            )
            logger.error("task %s crashed class=%s message=%s", task.task_id, error.category.value, error.message)
            return TaskStatus.FAILED, None, error.category.value, error.message, 1, evidence_dir, []
        finally:
            browser.close()

    def _receipt_diagnosis(
        self,
        problem: str,
        *,
        error_class: str,
        task: TaskRecord,
        run_id: str,
        browser: BrowserSession,
        snapshots: dict[int, str],
        evidence_dir: str,
    ) -> Diagnosis:
        """Classify failures detected after a nominally successful submission."""
        try:
            page = browser.page
        except Exception:  # pragma: no cover - adapter contract
            page = None
        from fillbot.diagnostics.collector import page_features

        hints = page_features(page)
        diagnosis = classify_failure_fields(
            stage=STAGE_RECEIPT,
            error_class=error_class,
            message=problem,
            receipt=None,
            resources=[str(path) for path in task.attachments],
            page_url=hints.get("page_url", ""),
            page_text=hints.get("page_text", ""),
            page_title=hints.get("page_title", ""),
        )
        attach_evidence(
            diagnosis,
            evidence_dir=evidence_dir,
            snapshot_path=snapshots.get(max(snapshots)) if snapshots else None,
            action_log_path=str(self.evidence.action_log_path(run_id, task.task_id)),
            receipt_path=str(self.evidence.receipt_path(run_id, task.task_id)),
        )
        return diagnosis

    def _runtime_diagnosis(
        self,
        *,
        task: TaskRecord,
        run_id: str,
        browser: BrowserSession,
        evidence_dir: str,
        snapshots: dict[int, str],
        result: Any,
    ) -> Diagnosis:
        """Build a diagnosis for a failure whose exception did not carry one."""
        try:
            page = browser.page
        except Exception:  # pragma: no cover - adapter contract
            page = None
        from fillbot.diagnostics.collector import page_features

        hints = page_features(page)
        diagnosis = classify_failure_fields(
            stage=STAGE_SUBMIT,
            error_class=result.error_class,
            message=str(result.error or "task failed without a classified exception"),
            resources=[str(path) for path in task.attachments],
            page_url=hints.get("page_url", ""),
            page_text=hints.get("page_text", ""),
            page_title=hints.get("page_title", ""),
        )
        failure_step = next((step for step in reversed(result.steps) if step.status == StepStatus.FAILED), None)
        attach_evidence(
            diagnosis,
            evidence_dir=evidence_dir,
            snapshot_path=failure_step.snapshot_path if failure_step is not None else None,
            action_log_path=str(self.evidence.action_log_path(run_id, task.task_id)),
            receipt_path=str(self.evidence.receipt_path(run_id, task.task_id)),
        )
        return diagnosis
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
                sleeper=self.sleeper,
            )
            result = executor.execute(dry_run=dry_run)
            for step in result.steps:
                step.snapshot_path = snapshots.get(step.step_index)
                self.ledger.record_step(run_id, task.task_id, step)
            attempts = max((step.attempts for step in result.steps), default=1)
            receipt, status = result.receipt_no, result.status
            error_class, error_message = result.error_class, result.error
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
            browser.close()
    def _finalize(self, run: RunRecord, tasks: Sequence[TaskRecord], *, interrupted: bool = False) -> RunRecord:
        counts = Counter(str(row["status"]) for row in self.ledger.list_task_rows(run.run_id))
        run.tasks_total = len(tasks)
        run.succeeded, run.failed, run.skipped = (
            counts.get("success", 0),
            counts.get("failed", 0),
            counts.get("skipped", 0),
        )
        run.finished_at, run.evidence_dir = iso_now(), str(self.evidence.run_dir(run.run_id))
        if interrupted:
            run.status = RunStatus.INTERRUPTED
        elif counts.get("failed", 0):
            run.status = RunStatus.FAILED
        elif counts.get("dry_run", 0):
            run.status = RunStatus.DRY_RUN
        else:
            run.status = RunStatus.SUCCESS
        self.ledger.finish_run(run)
        # Summary export is advisory; a legacy/missing evidence layout must never
        # turn a finished run into a crash, so keep diagnostics strictly additive.
        try:
            export_summary(self.ledger, run.run_id, self.config.runs_dir / run.run_id)
        except Exception as exc:  # pragma: no cover - defensive
            if self._logger is not None:
                self._logger.warning("summary export failed for run %s: %s", run.run_id, exc)
        self.evidence.cleanup()
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
            for task in tasks:
                if task.task_id in completed:
                    logger.info("task %s already complete; skipped on resume", task.task_id)
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
