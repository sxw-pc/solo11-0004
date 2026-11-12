"""Resumable, chunked upload support for the requests-based browser adapter.

The manager materialises chunks as temporary files under the system temp
directory, uploads them one at a time over the mock site's additive staging
protocol, and persists a checkpoint after every successful chunk. On retry or
process restart it resumes from the checkpoint: already-uploaded chunks are
never retransmitted. When the server confirms assembly, its returned SHA256 is
compared with the locally computed digest before the form is submitted.
"""
from __future__ import annotations
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
import requests
from fillbot.errors import ClientError, NetworkError, ScriptError, ServerError
from fillbot.hashing import sha256_bytes, sha256_file

STAGING_PATH = "/__mock/uploads/{upload_id}/chunks/{index}"
STATUS_PATH = "/__mock/uploads/{upload_id}"


class CheckpointStore(Protocol):
    def save_upload_state(self, run_id: str, task_id: str, state: dict[str, Any]) -> None: ...
    def get_upload_state(self, run_id: str, task_id: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class StagedUpload:
    """Result of staging a file through the resumable chunk protocol."""

    path: Path
    upload_id: str
    sha256: str
    size: int
    chunk_size: int
    total_chunks: int
    chunks_sent: int          # chunks actually transmitted on this invocation
    resumed: bool             # a previous checkpoint was reused


class ChunkUploadManager:
    def __init__(
        self,
        session: requests.Session,
        base_url: str,
        *,
        chunk_size: int,
        threshold: int,
        run_id: str,
        task_id: str,
        expected_hashes: dict[str, str] | None = None,
        checkpoint_store: CheckpointStore | None = None,
        temp_root: str | Path | None = None,
        limiter: Callable[[], None] | None = None,
    ) -> None:
        if chunk_size < 1:
            raise ScriptError("upload chunk size must be >= 1")
        self._session, self._base_url = session, base_url.rstrip("/")
        self.chunk_size, self.threshold = chunk_size, threshold
        self.run_id, self.task_id = run_id, task_id
        self._expected_hashes = expected_hashes or {}
        self._checkpoints = checkpoint_store
        # Collect leftovers from prior hard-killed processes before claiming
        # our own directory, so the sweep never removes the active temp parent.
        self._sweep_stale_parents(temp_root)
        # One temp parent per (run, task); every file gets its own subdirectory.
        self._temp_parent = Path(
            tempfile.mkdtemp(prefix=f"fillbot-upload-{run_id}-{task_id}-", dir=temp_root)
        )
        self._limiter = limiter
        self._staged: dict[str, StagedUpload] = {}

    @staticmethod
    def _sweep_stale_parents(temp_root: str | Path | None) -> None:
        """Best-effort removal of temp dirs orphaned by a hard-killed process.

        A graceful run cleans its own directory in :meth:`cleanup`; this only
        collects leftovers from prior crashes so the system temp dir cannot
        grow without bound. Failures are ignored: cleanup must never break an
        upload, and the files are inside the OS-managed temp area anyway.
        """
        search_root = Path(temp_root) if temp_root else Path(tempfile.gettempdir())
        try:
            for orphan in search_root.glob("fillbot-upload-*"):
                if orphan.is_dir():
                    shutil.rmtree(orphan, ignore_errors=True)
        except OSError:
            pass

    # ------------------------------------------------------------------
    def should_chunk(self, path: Path) -> bool:
        return path.stat().st_size > self.threshold

    def _load_checkpoint(self, key: str) -> dict[str, Any] | None:
        if self._checkpoints is None:
            return None
        state = self._checkpoints.get_upload_state(self.run_id, self.task_id)
        if not state:
            return None
        return state.get("files", {}).get(key)

    def _save_checkpoint(self, key: str, entry: dict[str, Any]) -> None:
        if self._checkpoints is None:
            return
        current = self._checkpoints.get_upload_state(self.run_id, self.task_id) or {}
        files = dict(current.get("files", {}))
        files[key] = entry
        self._checkpoints.save_upload_state(
            self.run_id, self.task_id, {"version": 1, "temp_dir": str(self._temp_parent), "files": files}
        )

    def _put_chunk(
        self, url: str, body: bytes, headers: dict[str, str]
    ) -> dict[str, Any]:
        if self._limiter:
            self._limiter()
        try:
            response = self._session.put(url, data=body, headers=headers, timeout=None)
        except requests.RequestException as exc:
            raise NetworkError(f"chunk upload network error: {url}: {exc}") from exc
        if response.status_code == 429:
            retry_after = None
            if "Retry-After" in response.headers:
                try:
                    retry_after = float(response.headers["Retry-After"])
                except ValueError:
                    retry_after = None
            raise ServerError(f"rate limited (429) at {url}", status=429, retry_after=retry_after)
        if response.status_code >= 500:
            raise ServerError(f"chunk server error {response.status_code} at {url}", status=response.status_code)
        if response.status_code >= 400:
            raise ClientError(f"chunk client error {response.status_code} at {url}: {response.text[:160]}",
                              status=response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise ServerError(f"chunk endpoint returned non-JSON response at {url}") from exc

    def stage_file(self, path: Path) -> StagedUpload:
        """Upload ``path`` in chunks, resuming from any prior checkpoint."""
        resolved = path.resolve()
        key = str(resolved)
        if key in self._staged:
            return self._staged[key]
        size = resolved.stat().st_size
        local_hash = self._expected_hashes.get(key) or sha256_file(resolved)
        total_chunks = (size + self.chunk_size - 1) // self.chunk_size
        # Content-addressed session id: identical content resumes the same
        # server-side staging session even after a local temp dir is rebuilt.
        upload_id = local_hash
        chunk_dir = self._temp_parent / upload_id
        chunk_dir.mkdir(parents=True, exist_ok=True)

        checkpoint = self._load_checkpoint(key)
        server_received: set[int] = set()
        resumed = False
        if (
            checkpoint
            and checkpoint.get("upload_id") == upload_id
            and checkpoint.get("size") == size
            and checkpoint.get("chunk_size") == self.chunk_size
        ):
            server_received = set(checkpoint.get("received_chunks", ()))
            resumed = bool(server_received)

        # Reconcile with the server's view when resuming: the server remains
        # the source of truth for which chunks it actually holds.
        if resumed:
            try:
                status = self._session.get(
                    self._base_url + STATUS_PATH.format(upload_id=upload_id), timeout=None
                )
                if status.status_code == 200:
                    server_received = set(status.json().get("received_chunks", ()))
            except requests.RequestException:
                # Offline/failed probe: fall back to the local checkpoint.
                pass

        sent = 0
        with resolved.open("rb") as handle:
            for index in range(total_chunks):
                if index in server_received:
                    # Skip past bytes belonging to an already-uploaded chunk.
                    handle.seek((index + 1) * self.chunk_size)
                    continue
                handle.seek(index * self.chunk_size)
                body = handle.read(self.chunk_size)
                part = chunk_dir / f"chunk-{index:06d}.part"
                if not part.exists():
                    part.write_bytes(body)
                headers = {
                    "Content-Type": "application/octet-stream",
                    "X-Chunk-Sha256": sha256_bytes(body),
                    "X-Total-Chunks": str(total_chunks),
                    "X-Total-Sha256": local_hash,
                    "X-Total-Size": str(size),
                    "X-Filename": resolved.name,
                }
                payload = self._put_chunk(
                    self._base_url + STAGING_PATH.format(upload_id=upload_id, index=index), body, headers
                )
                sent += 1
                server_received.add(index)
                self._save_checkpoint(
                    key,
                    {
                        "upload_id": upload_id,
                        "path": key,
                        "filename": resolved.name,
                        "size": size,
                        "sha256": local_hash,
                        "chunk_size": self.chunk_size,
                        "total_chunks": total_chunks,
                        "received_chunks": sorted(server_received),
                        "complete": bool(payload.get("complete")),
                    },
                )

        # Requirement 5: the target site must confirm the assembled digest.
        final = self._session.get(self._base_url + STATUS_PATH.format(upload_id=upload_id), timeout=None)
        if final.status_code != 200:
            raise ServerError(f"cannot confirm upload status for {resolved.name}: HTTP {final.status_code}",
                              status=final.status_code)
        info = final.json()
        if not info.get("complete") or info.get("sha256") != local_hash:
            raise ClientError(
                f"server hash verification failed for {resolved.name}: "
                f"local={local_hash} server={info.get('sha256')} complete={info.get('complete')}",
                status=422,
            )
        staged = StagedUpload(resolved, upload_id, local_hash, size, self.chunk_size, total_chunks, sent, resumed)
        self._staged[key] = staged
        return staged

    def cleanup(self) -> None:
        """Remove every temporary chunk directory created by this manager."""
        shutil.rmtree(self._temp_parent, ignore_errors=True)
