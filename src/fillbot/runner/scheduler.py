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
from fillbot.models import RunRecord, RunStatus, TaskRecord, TaskStatus, elapsed_ms, iso_now, utcnow
from fillbot.runner.pacing import RateLimiter, new_run_id
from fillbot.runner.retry import RetryPolicy
from fillbot.runner.state import plan_resume
from fillbot.security.levels import SecurityLevel
from fillbot.security.masking import MaskingPolicy

RECEIPT_RE = re.compile(r"^RCP-\d{8}-\d{4}$")
BrowserFactory = Callable[[], BrowserSession]


def build_masking_policy(
    script: Script,
    tasks: Sequence[TaskRecord],
    mask_fields: Sequence[str],
) -> MaskingPolicy:
    """Merge DSL security config with per-task field grades.

    Task-sheet grades take precedence over script-level field defaults.
    With no grading anywhere the returned policy is a legacy policy, so the
    evidence output stays identical to the pre-grading release.
    """
    security = script.security
    dsl_fields = {name: level for name, level in security.field_levels.items()}
    merged: dict[str, SecurityLevel] = dict(dsl_fields)
    for task in tasks:
        for name, raw_level in (task.field_security or {}).items():
            try:
                merged[name] = SecurityLevel.parse(raw_level)
            except ValueError:
                # Bad intake data should have been rejected earlier; fail closed.
                merged[name] = SecurityLevel.L4
    if not security.enabled and not merged:
        return MaskingPolicy.legacy_policy(mask_fields)
    defaults = MaskingPolicy.create(mask_fields=mask_fields).rules
    policy = MaskingPolicy(
        rules={**defaults, **dict(security.rules)},
        field_levels=merged,
        default_level=security.default_level,
        mask_fields=tuple(mask_fields),
        legacy=False,
    )
    return policy
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
        # Share the ledger authorizer/audit table with the evidence store.
        self.evidence.bind_ledger(self.ledger)
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
        """Values that must never appear verbatim in evidence.

        In legacy mode only the fixed mask-field list applies (unchanged
        behaviour). In graded mode every graded payload field is treated as a
        secret so free-text occurrences are redacted even outside form inputs.
        """
        return [value for _, value in self._named_secrets(task)]

    def _named_secrets(self, task: TaskRecord) -> list[tuple[str, str]]:
        """``(field_name, value)`` pairs, enabling per-grade free-text masking."""
        policy = self.evidence.policy
        if policy.legacy:
            return [
                (key, str(value))
                for key, value in task.payload.items()
                if value and is_sensitive(key, self.config.mask_fields)
            ]
        secret_keys = set(policy.field_levels)
        if policy.default_level is not None:
            secret_keys.update(task.payload.keys())
        else:
            secret_keys.update(
                key for key in task.payload if is_sensitive(key, self.config.mask_fields)
            )
        return [
            (key, str(value))
            for key, value in task.payload.items()
            if value and key in secret_keys
        ]
    def _task_context(self, task: TaskRecord) -> dict[str, object]:
        context: dict[str, object] = dict(task.payload)
        context.update({"task_id": task.task_id, "biz_key": task.biz_key, "attachments": [str(path) for path in task.attachments], "attachment": str(task.attachment) if task.attachment else ""})
        return context
    def _retry_policy(self) -> RetryPolicy:
        return RetryPolicy(self.config.max_retries + 1, self.config.backoff_base_seconds, self.config.backoff_max_seconds, self.config.jitter, sleeper=self.sleeper)
    def _execute_task(self, run_id: str, task: TaskRecord, script: Script, logger: logging.Logger, dry_run: bool) -> tuple[TaskStatus, str | None, str | None, str | None, int, str, list]:
        started, started_at = utcnow(), iso_now()
        evidence_dir = str(self.evidence.task_dir(run_id, task.task_id))
        self.ledger.start_task(run_id, task, started_at, evidence_dir)
        logger.info("task %s started biz_key=%s", task.task_id, task.biz_key)
        named_secrets = self._named_secrets(task)
        secrets, snapshots = [value for _, value in named_secrets], {}
        browser = self._factory()
        env = {**os.environ, "FILLBOT_BASE_URL": self.config.base_url}
        def snapshot_cb(step, page, status):
            snapshots[step.index] = str(
                self.evidence.snapshot(
                    run_id, task.task_id, step.index, page,
                    secret_values=secrets, named_secrets=named_secrets,
                )
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
                    self.evidence.save_receipt(
                        run_id, task.task_id, browser.page,
                        secret_values=secrets, named_secrets=named_secrets,
                    )
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
        export_summary(self.ledger, run.run_id, self.config.runs_dir / run.run_id)
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
        self.evidence.policy = build_masking_policy(script, tasks, self.config.mask_fields)
        logger.info(
            "masking mode=%s graded_fields=%d",
            "legacy" if self.evidence.policy.legacy else "graded",
            len(self.evidence.policy.field_levels),
        )
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
