"""Reference-aware evidence retention and cross-run reclamation.

Evidence is reclaimed only when it becomes unreachable: no active reference
from a protected or in-window run, and no pin from a review report. Runs that
are unfinished (running/interrupted) or awaiting confirmation (failed) are
never touched, and legacy runs without registered references are kept
conservatively. Cleanup never writes run or task state — only evidence ref
rows — so a failed cleanup cannot affect the runs themselves.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from fillbot.browser.snapshot import safe_component
from fillbot.errors import ScriptError
from fillbot.ledger.db import Ledger
__all__ = [
    "CleanupResult",
    "RetentionManager",
    "PROTECTED_RUN_STATUSES",
    "DELETABLE_RUN_STATUSES",
    "DEFAULT_RETENTION_DAYS",
    "RUN_SOURCE",
    "REPORT_SOURCE",
]
RUN_SOURCE = "run"
REPORT_SOURCE = "report"
DEFAULT_RETENTION_DAYS = 30
# Unfinished (running/interrupted) or awaiting-confirmation (failed) runs are never reclaimed.
PROTECTED_RUN_STATUSES = frozenset({"running", "interrupted", "failed"})
DELETABLE_RUN_STATUSES = frozenset({"success", "dry_run"})
def _norm_path(value: str | Path) -> str:
    return os.path.normpath(os.path.abspath(str(value)))
def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
@dataclass
class CleanupResult:
    """What a cleanup pass did (or would do, when dry_run is set)."""
    dry_run: bool
    retention_days: int
    deleted_files: list[str] = field(default_factory=list)
    deleted_dirs: list[str] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    backfilled_refs: int = 0
    released_refs: int = 0
    @property
    def exit_code(self) -> int:
        return 1 if self.warnings else 0
class RetentionManager:
    """Plans and executes evidence reclamation against the ledger's ref registry."""
    def __init__(
        self,
        ledger: Ledger,
        evidence_dir: str | Path,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        now: datetime | None = None,
    ) -> None:
        if retention_days < 0:
            raise ScriptError(f"retention_days must be >= 0, got {retention_days}")
        self.ledger = ledger
        self.evidence_dir = Path(evidence_dir)
        self.retention_days = retention_days
        self._now = now
    def list_refs(self, **filters: Any) -> list[dict[str, Any]]:
        return self.ledger.list_evidence_refs(**filters)
    def pin(
        self,
        *,
        run_id: str,
        task_id: str | None = None,
        evidence_type: str | None = None,
        source: str = REPORT_SOURCE,
    ) -> int:
        """Pin matching active run evidence under another source (e.g. a review report)."""
        if source == RUN_SOURCE:
            raise ScriptError("pin source must differ from 'run'")
        refs = self.ledger.list_evidence_refs(
            run_id=run_id, task_id=task_id, evidence_type=evidence_type, source=RUN_SOURCE, active_only=True
        )
        for ref in refs:
            self.ledger.register_evidence_ref(
                str(ref["run_id"]), str(ref["task_id"]), str(ref["biz_key"]), str(ref["evidence_type"]), str(ref["path"]), source=source
            )
        return len(refs)
    def unpin(
        self,
        *,
        run_id: str,
        task_id: str | None = None,
        evidence_type: str | None = None,
        source: str | None = None,
    ) -> int:
        """Release non-run references (report pins) matching the filters."""
        refs = self.ledger.list_evidence_refs(
            run_id=run_id, task_id=task_id, evidence_type=evidence_type, source=source, active_only=True
        )
        ids = [int(ref["id"]) for ref in refs if ref["source"] != RUN_SOURCE]
        return self.ledger.release_evidence_refs(ids=ids) if ids else 0
    def cleanup(self, *, dry_run: bool = False) -> CleanupResult:
        """Reclaim unreachable evidence past the retention window.

        The reference backfill runs first; if the migration fails the whole
        pass aborts (ScriptError) before anything is deleted, so a failed
        migration can never cost evidence. Individual deletion problems are
        collected as warnings instead of raising.
        """
        try:
            backfilled = self.ledger.backfill_evidence_refs()
        except Exception as exc:
            raise ScriptError(f"evidence ref migration failed; cleanup aborted, nothing deleted: {exc}") from exc
        result = CleanupResult(dry_run=dry_run, retention_days=self.retention_days, backfilled_refs=backfilled)
        now = self._now or datetime.now(timezone.utc)
        window = timedelta(days=self.retention_days)
        runs = self.ledger.list_runs()
        all_refs = self.ledger.list_evidence_refs()
        active_refs = [ref for ref in all_refs if ref["released_at"] is None]
        active_by_run: dict[str, list[dict[str, Any]]] = {}
        registered_runs: set[str] = set()
        for ref in all_refs:
            registered_runs.add(str(ref["run_id"]))
        for ref in active_refs:
            active_by_run.setdefault(str(ref["run_id"]), []).append(ref)
        eligible = self._eligible_runs(runs, registered_runs, now, window, result)
        # Reachability: a path stays while any active ref belongs to a
        # non-eligible run or to a non-run source (e.g. a review report pin).
        pinned: dict[str, list[str]] = {}
        for ref in active_refs:
            if ref["source"] == RUN_SOURCE and str(ref["run_id"]) in eligible:
                continue
            pinned.setdefault(_norm_path(ref["path"]), []).append(f"{ref['source']}:{ref['run_id']}")
        registered_active = {_norm_path(ref["path"]) for ref in active_refs}
        delete: dict[str, str] = {}
        refs_by_norm: dict[str, list[str]] = {}
        for run_id in sorted(eligible):
            for ref in active_by_run.get(run_id, []):
                if ref["source"] != RUN_SOURCE:
                    continue
                key = _norm_path(ref["path"])
                if key in pinned:
                    result.kept.append((str(ref["path"]), "referenced by " + ", ".join(sorted(pinned[key]))))
                    continue
                delete.setdefault(key, str(ref["path"]))
                refs_by_norm.setdefault(key, []).append(str(ref["path"]))
            run_dir = self.evidence_dir / safe_component(run_id)
            if run_dir.is_dir():
                for path in sorted(run_dir.rglob("*")):
                    if path.is_file() and _norm_path(path) not in registered_active:
                        delete.setdefault(_norm_path(path), str(path))  # unregistered temp file
        self._note_orphan_dirs(runs, result)
        gone = self._delete_files(delete, refs_by_norm, result)
        self._prune_dirs(eligible, gone, result)
        return result
    def _eligible_runs(
        self,
        runs: list[dict[str, Any]],
        registered_runs: set[str],
        now: datetime,
        window: timedelta,
        result: CleanupResult,
    ) -> set[str]:
        eligible: set[str] = set()
        for run in runs:
            run_id, status = str(run["run_id"]), str(run["status"])
            if status in PROTECTED_RUN_STATUSES:
                result.kept.append((run_id, f"run status '{status}' is protected"))
                continue
            if status not in DELETABLE_RUN_STATUSES:
                result.kept.append((run_id, f"run status '{status}' is not reclaimable"))
                continue
            if run_id not in registered_runs:
                result.kept.append((run_id, "no registered evidence refs; kept conservatively"))
                continue
            stamp = _parse_timestamp(run["finished_at"] or run["started_at"])
            if stamp is None:
                result.warnings.append(f"run {run_id}: unparseable timestamp; kept conservatively")
                continue
            if now - stamp < window:
                result.kept.append((run_id, f"within {self.retention_days}d retention window"))
                continue
            eligible.add(run_id)
        return eligible
    def _note_orphan_dirs(self, runs: list[dict[str, Any]], result: CleanupResult) -> None:
        if not self.evidence_dir.is_dir():
            return
        known = {safe_component(str(run["run_id"])) for run in runs}
        for item in sorted(self.evidence_dir.iterdir()):
            if item.is_dir() and item.name not in known:
                result.kept.append((str(item), "no ledger run record; kept conservatively"))
    def _delete_files(
        self,
        delete: dict[str, str],
        refs_by_norm: dict[str, list[str]],
        result: CleanupResult,
    ) -> set[str]:
        """Delete planned files (or plan them in dry-run); returns norms considered gone."""
        gone: set[str] = set()
        for key, display in sorted(delete.items()):
            path = Path(display)
            try:
                existed = path.exists()
                if existed:
                    if not result.dry_run:
                        path.unlink()
                    result.deleted_files.append(display)
                else:
                    result.kept.append((display, "already missing"))
            except OSError as exc:
                result.warnings.append(f"cannot delete {display}: {exc}")
                continue
            gone.add(key)
            if not result.dry_run and refs_by_norm.get(key):
                result.released_refs += self.ledger.release_evidence_refs(paths=refs_by_norm[key])
        return gone
    def _prune_dirs(self, eligible: set[str], gone: set[str], result: CleanupResult) -> None:
        """Remove run/task directories that hold no surviving evidence; never errors out."""
        for run_id in sorted(eligible):
            run_dir = self.evidence_dir / safe_component(run_id)
            if not run_dir.is_dir():
                continue
            prunable: list[Path] = []
            prunable_set: set[Path] = set()
            for root, dirs, files in os.walk(run_dir, topdown=False):
                root_path = Path(root)
                files_gone = all(_norm_path(root_path / name) in gone for name in files)
                dirs_gone = all((root_path / name) in prunable_set for name in dirs)
                if files_gone and dirs_gone:
                    prunable.append(root_path)
                    prunable_set.add(root_path)
            for directory in prunable:
                if result.dry_run:
                    result.deleted_dirs.append(str(directory))
                    continue
                try:
                    directory.rmdir()
                    result.deleted_dirs.append(str(directory))
                except OSError as exc:
                    result.warnings.append(f"cannot remove directory {directory}: {exc}")
