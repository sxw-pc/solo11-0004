"""Additive, loopback-only resumable chunk-upload endpoints for the mock site.

These routes live under the ``/__mock`` test-support namespace and do **not**
change the site's core form-upload interface (``/report/step3`` etc.): they give
the local client a staging area that speaks a resumable protocol so the bot's
breakpoint-continuation logic can be exercised entirely on loopback.

Protocol
--------
``PUT /__mock/uploads/<upload_id>/chunks/<index>``
    headers: ``X-Chunk-Sha256``, ``X-Total-Chunks``, ``X-Total-Sha256``,
    ``X-Total-Size``, ``X-Filename``; body: raw chunk bytes.
    Idempotent: re-uploading an identical chunk returns 200 without side
    effects. When the final chunk lands, the server assembles the whole file,
    verifies its SHA256 and returns the digest alongside ``complete=true``.

``GET /__mock/uploads/<upload_id>``
    Returns progress (received chunk indexes, completeness, assembled digest).

Fault injection for tests: ``POST /__mock/uploads/faults`` configures the
store to fail the next N chunk PUTs with a given HTTP status.
"""
from __future__ import annotations
import threading
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from flask import Blueprint, current_app, jsonify, request
from fillbot.hashing import sha256_bytes, sha256_stream
from fillbot.errors import ErrorClass

bp = Blueprint("mock_uploads", __name__, url_prefix="/__mock/uploads")


@dataclass
class ChunkUpload:
    upload_id: str
    total_chunks: int
    total_size: int
    total_sha256: str
    filename: str
    chunk_dir: Path
    received: set[int] = field(default_factory=set)
    complete: bool = False
    assembled_sha256: str | None = None


class ChunkUploadStore:
    """In-memory, thread-safe staging store backed by temporary files."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._uploads: dict[str, ChunkUpload] = {}
        self._tempdir = TemporaryDirectory(prefix="fillbot-mock-chunks-")
        # Deterministic fault injection: list of statuses to return on next PUTs.
        self.faults: list[int] = []

    @property
    def root(self) -> Path:
        return Path(self._tempdir.name)

    def _chunk_path(self, upload: ChunkUpload, index: int) -> Path:
        return upload.chunk_dir / f"chunk-{index:06d}.part"

    def put_chunk(
        self,
        upload_id: str,
        index: int,
        body: bytes,
        *,
        chunk_sha256: str,
        total_chunks: int,
        total_sha256: str,
        total_size: int,
        filename: str,
    ) -> tuple[dict[str, Any], int]:
        with self._lock:
            if self.faults:
                status = self.faults.pop(0)
                return {"error": "injected fault", "retryable": status >= 500 or status == 429}, status
            upload = self._uploads.get(upload_id)
            if upload is None:
                if not (0 <= index < total_chunks and total_chunks >= 1):
                    return {"error": "invalid chunk index"}, 400
                upload = ChunkUpload(
                    upload_id=upload_id,
                    total_chunks=total_chunks,
                    total_size=total_size,
                    total_sha256=total_sha256,
                    filename=filename,
                    chunk_dir=self.root / upload_id,
                )
                upload.chunk_dir.mkdir(parents=True, exist_ok=True)
                self._uploads[upload_id] = upload
            elif (
                upload.total_chunks != total_chunks
                or upload.total_sha256 != total_sha256
                or upload.total_size != total_size
            ):
                return {"error": "upload metadata does not match the existing session"}, 409
            if not (0 <= index < upload.total_chunks):
                return {"error": "chunk index out of range"}, 400
            if not upload.complete and sha256_bytes(body) != chunk_sha256:
                return {"error": "chunk checksum mismatch"}, 422
            target = self._chunk_path(upload, index)
            if index not in upload.received:
                # Atomic write so a failed request never leaves a partial chunk.
                temporary = target.with_suffix(".tmp")
                temporary.write_bytes(body)
                temporary.replace(target)
                upload.received.add(index)
            if len(upload.received) == upload.total_chunks and not upload.complete:
                assembled = sha256_stream(
                    self._chunk_path(upload, idx).read_bytes() for idx in range(upload.total_chunks)
                )
                if assembled != upload.total_sha256:
                    return {"error": "assembled file checksum mismatch", "expected": upload.total_sha256,
                            "actual": assembled}, 422
                upload.assembled_sha256 = assembled
                upload.complete = True
            return self._public(upload), 200

    def status(self, upload_id: str) -> tuple[dict[str, Any], int]:
        with self._lock:
            upload = self._uploads.get(upload_id)
            if upload is None:
                return {"error": "upload session not found"}, 404
            return self._public(upload), 200

    def _public(self, upload: ChunkUpload) -> dict[str, Any]:
        return {
            "upload_id": upload.upload_id,
            "received_chunks": sorted(upload.received),
            "total_chunks": upload.total_chunks,
            "complete": upload.complete,
            "sha256": upload.assembled_sha256,
            "filename": upload.filename,
        }

    def reset_faults(self, statuses: list[int]) -> None:
        with self._lock:
            self.faults = list(statuses)

    def shutdown(self) -> None:
        self._tempdir.cleanup()


def _store() -> ChunkUploadStore:
    store = current_app.extensions.get("fillbot_chunk_uploads")
    if store is None:  # pragma: no cover - always wired by create_app
        store = ChunkUploadStore()
        current_app.extensions["fillbot_chunk_uploads"] = store
    return store


def _int_header(name: str) -> int | None:
    raw = request.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@bp.put("/<upload_id>/chunks/<int:index>")
def put_chunk(upload_id: str, index: int) -> Any:
    total_chunks = _int_header("X-Total-Chunks")
    total_size = _int_header("X-Total-Size")
    chunk_sha256 = request.headers.get("X-Chunk-Sha256", "").strip().lower()
    total_sha256 = request.headers.get("X-Total-Sha256", "").strip().lower()
    filename = request.headers.get("X-Filename", "")
    if total_chunks is None or total_size is None or not chunk_sha256 or not total_sha256:
        return jsonify({"error": "missing required chunk headers"}), 400
    body = request.get_data(cache=False)
    if total_size is not None and len(body) > total_size:
        return jsonify({"error": "chunk larger than declared total size"}), 400
    payload, status = _store().put_chunk(
        upload_id,
        index,
        body,
        chunk_sha256=chunk_sha256,
        total_chunks=total_chunks,
        total_sha256=total_sha256,
        total_size=total_size,
        filename=filename,
    )
    return jsonify(payload), status


@bp.get("/<upload_id>")
def upload_status(upload_id: str) -> Any:
    payload, status = _store().status(upload_id)
    return jsonify(payload), status


@bp.post("/faults")
def configure_faults() -> Any:
    data = request.get_json(silent=True) or {}
    statuses = data.get("statuses", [])
    if not isinstance(statuses, list) or not all(isinstance(item, int) for item in statuses):
        return jsonify({"error": "statuses must be a list of HTTP status integers"}), 400
    _store().reset_faults(statuses)
    return jsonify({"configured": statuses, "error_class": ErrorClass.SERVER.value}), 200


def init_chunk_uploads(app: Any) -> ChunkUploadStore:
    existing = app.extensions.get("fillbot_chunk_uploads")
    if isinstance(existing, ChunkUploadStore):
        return existing
    store = ChunkUploadStore()
    app.extensions["fillbot_chunk_uploads"] = store
    app.register_blueprint(bp)
    return store
