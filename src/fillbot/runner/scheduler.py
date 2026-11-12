"""Batch orchestration with evidence, ledger, idempotency and resume.

Execution model: tasks within one batch may run concurrently up to the
configured ``max_concurrency`` (default 1, which preserves the original
serial behaviour exactly). Every worker gets its own browser session, so
cookies/CSRF state never cross enterprise boundaries; the rate limiter,
ledger and evidence store are shared and individually thread-safe.
Idempotency, retry and resume semantics are identical to the serial path.
"""
from __future__ import annotations
import logging
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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
RECEIPT_RE = re.compile(r"^RCP-\d{8}-\d{4}$")
BrowserFactory = Callable[[], BrowserSession]


class _BizKeyGate:
    """Serialise tasks that share the same idempotency key.

    Different ``biz_key`` values get independent locks, so unrelated
    enterprises keep running fully in parallel. Tasks with the same key run
    one at a time; while holding the key lock each task re-reads the durable
    ledger state, so a task only skips when a *successful* record actually
    exists. A failed predecessor therefore never suppresses a successor the
    way a purely in-memory "claimed" flag would. A worker ever holds a single
    key lock, so the locks cannot deadlock.
    """

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}

    def lock_for(self, biz_key: str) -> threading.Lock:
        with self._registry_lock:
            lock = self._key_locks.get(biz_key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[biz_key] = lock
            return lock

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
        # One shared limiter for the whole run: the global rate holds under
        # any concurrency level because token accounting is locked internally.
        self.limiter = RateLimiter(config.rate_limit_per_minute, sleeper=sleeper)
        self._browser_factory = browser_factory
        self._logger = logger
        self._file_handler: logging.FileHandler | None = None
    def _factory(self) -> BrowserSession:
        # A fresh session per task guarantees cookie/CSRF isolation between
        # different enterprises; sessions are never shared across threads.
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
    def _execute_task(self, run_id: str, task: TaskRecord, script: Script, logger: logging.Logger, dry_run: bool) -> tuple[TaskStatus, str | None, str | None, str | None, int, str, list]:
        started, started_at = utcnow(), iso_now()
        evidence_dir = str(self.evidence.task_dir(run_id, task.task_id))
        self.ledger.start_task(run_id, task, started_at, evidence_dir)
        logger.info("task %s started biz_key=%s", task.task_id, task.biz_key)
        secrets, snapshots = self._secrets(task), {}
        browser: BrowserSession | None = None
        try:
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
            if browser is not None:
                browser.close()
    def _idempotent_skip(self, run_id: str, task: TaskRecord, logger: logging.Logger) -> None:
        evidence_dir = str(self.evidence.run_dir(run_id))
        self.ledger.start_task(run_id, task, iso_now(), evidence_dir)
        self.ledger.finish_task(run_id, task.task_id, status=TaskStatus.SKIPPED.value, finished_at=iso_now(), duration_ms=0, attempts=0, error_message="idempotent skip: biz_key already succeeded", receipt_no=None, evidence_dir=evidence_dir)
        logger.info("task %s skipped idempotently", task.task_id)
    def _dispatch(self, run_id: str, task: TaskRecord, script: Script, logger: logging.Logger, dry_run: bool, gate: _BizKeyGate) -> None:
        # Hold the per-biz_key lock across the whole task so concurrent tasks
        # sharing an idempotency key cannot both submit. The skip decision is
        # made against the ledger while the lock is held, matching the serial
        # runner's check-then-act ordering exactly.
        with gate.lock_for(task.biz_key):
            if self.ledger.has_successful_biz_key(task.biz_key):
                self._idempotent_skip(run_id, task, logger)
                return
            self._execute_task(run_id, task, script, logger, dry_run)
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
        try:
            completed = set(plan_resume(self.ledger, rid, tasks).completed_task_ids) if resume else set()
            pending = [task for task in tasks if task.task_id not in completed]
            # Pre-register every task in batch order. Rows keep this rowid
            # ordering for reports even when workers finish out of order.
            for task in pending:
                self.evidence.task_dir(rid, task.task_id)
                self.ledger.register_task(rid, task, str(self.evidence.task_dir(rid, task.task_id)))
            gate = _BizKeyGate()
            workers = max(1, int(self.config.max_concurrency))
            logger.info("dispatch run=%s tasks=%s concurrency=%s", rid, len(pending), workers)
            if workers == 1:
                # Serial path: identical control flow to the original runner,
                # including immediate propagation of interruptions.
                for task in pending:
                    self._dispatch(rid, task, script, logger, dry_run, gate)
            else:
                pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"fillbot-{rid}")
                try:
                    futures = [
                        pool.submit(self._dispatch, rid, task, script, logger, dry_run, gate)
                        for task in pending
                    ]
                    for future in futures:
                        # Ordinary task failures are already recorded as a
                        # terminal status inside _execute_task. A BaseException
                        # (e.g. SystemExit/KeyboardInterrupt) escapes the worker
                        # and is re-raised here; the handler below cancels work
                        # that never started while letting in-flight tasks
                        # settle, then the outer handler marks the run
                        # interrupted and keeps it resumable.
                        future.result()
                except BaseException:
                    # cancel_futures drops only queued (not-yet-started) tasks;
                    # wait=True still joins the tasks already in flight so their
                    # ledger rows reach a terminal, auditable state.
                    pool.shutdown(wait=True, cancel_futures=True)
                    raise
                else:
                    pool.shutdown(wait=True)
            return self._finalize(run, tasks)
        except BaseException:
            self._finalize(run, tasks, interrupted=True)
            raise
        finally:
            self._teardown_logging()
    def resume(self, run_id: str, tasks: Sequence[TaskRecord], script: Script, *, task_source: str = "", script_source: str = "", dry_run: bool = False) -> RunRecord:
        return self.run(tasks, script, task_source=task_source, script_source=script_source, dry_run=dry_run, run_id=run_id, resume=True, options={"resumed": True})
