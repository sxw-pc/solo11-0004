"""Per-step HTML snapshots, action journals and retention cleanup."""
from __future__ import annotations
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from fillbot.browser.base import Page
from fillbot.browser.snapshot import is_sensitive, safe_component, sanitize_html
__all__ = ["EvidenceStore", "is_sensitive", "safe_component", "sanitize_html"]
class EvidenceStore:
    def __init__(
        self,
        base_dir: str | Path,
        *,
        mask_fields: Iterable[str] = ("password", "token"),
        keep_runs: int = 10,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.mask_fields = tuple(mask_fields)
        self.keep_runs = max(0, keep_runs)
    def run_dir(self, run_id: str) -> Path:
        path = self.base_dir / safe_component(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path
    def task_dir(self, run_id: str, task_id: str) -> Path:
        path = self.run_dir(run_id) / safe_component(task_id)
        path.mkdir(parents=True, exist_ok=True)
        return path
    def snapshot(
        self,
        run_id: str,
        task_id: str,
        step_index: int,
        page: Page,
        *,
        secret_values: Iterable[str] = (),
        filename: str | None = None,
    ) -> Path:
        path = self.task_dir(run_id, task_id) / (filename or f"step-{step_index:02d}.html")
        path.write_text(sanitize_html(page.text, self.mask_fields, secret_values), encoding="utf-8")
        return path
    def save_receipt(self, run_id: str, task_id: str, page: Page, *, secret_values: Iterable[str] = ()) -> Path:
        return self.snapshot(run_id, task_id, 0, page, secret_values=secret_values, filename="receipt.html")
    def append_action(self, run_id: str, task_id: str, record: dict[str, Any]) -> Path:
        path = self.task_dir(run_id, task_id) / "actions.jsonl"
        payload = dict(record)
        payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return path
    def cleanup(self, keep: int | None = None) -> list[str]:
        retain = self.keep_runs if keep is None else max(0, keep)
        if not self.base_dir.is_dir():
            return []
        candidates = [item for item in self.base_dir.iterdir() if item.is_dir()]
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        removed = []
        for directory in candidates[retain:]:
            shutil.rmtree(directory, ignore_errors=True)
            removed.append(directory.name)
        return removed
