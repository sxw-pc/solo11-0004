"""SHA256 helpers with a bounded, offloaded execution path.

Hash computation runs on a short-lived daemon thread so the main flow is never
blocked by large attachments: callers decide the budget (the intake layer
enforces two seconds) and receive a :class:`HashTimeoutError` when the budget
is exceeded. Abandoned daemon threads cannot delay interpreter shutdown.
"""
from __future__ import annotations
import hashlib
import queue
import threading
from pathlib import Path
from typing import Callable, Iterable

# 1 MiB streaming blocks keep memory flat regardless of file size.
HASH_BLOCK_SIZE = 1024 * 1024


class HashTimeoutError(TimeoutError):
    """Raised when a hash cannot be computed within the caller's budget."""


def sha256_file(path: str | Path) -> str:
    """Stream a file through SHA256 without loading it entirely into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(HASH_BLOCK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_stream(chunks: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


def sha256_file_bounded(
    path: str | Path,
    *,
    timeout_seconds: float = 2.0,
    executor: object | None = None,
    hasher: Callable[[str | Path], str] = sha256_file,
) -> str:
    """Compute a file hash off the calling thread, capped at ``timeout_seconds``.

    The blocking work happens on a daemon worker thread; the calling thread
    only waits up to the budget, so main-flow blocking is bounded. The
    ``executor`` argument is accepted for API symmetry/test injection but the
    implementation deliberately uses a plain daemon thread to guarantee clean
    process shutdown even when a hash overruns its budget.
    """
    result: queue.Queue[tuple[str, BaseException | None]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result.put((hasher(path), None))
        except BaseException as exc:  # propagate to the caller
            result.put(("", exc))

    thread = threading.Thread(target=worker, name="fillbot-hash", daemon=True)
    thread.start()
    try:
        digest, error = result.get(timeout=timeout_seconds)
    except queue.Empty:
        raise HashTimeoutError(
            f"SHA256 computation for {path} exceeded {timeout_seconds:g}s budget"
        ) from None
    if error is not None:
        raise error
    return digest
