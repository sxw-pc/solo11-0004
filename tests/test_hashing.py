from __future__ import annotations
import hashlib
import time
from pathlib import Path
import pytest
from fillbot.hashing import (
    HashTimeoutError,
    sha256_bytes,
    sha256_file,
    sha256_file_bounded,
    sha256_stream,
)


def test_sha256_helpers_match_hashlib(tmp_path: Path) -> None:
    payload = b"fillbot-hash-check" * 1000
    path = tmp_path / "a.bin"
    path.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    assert sha256_bytes(payload) == expected
    assert sha256_file(path) == expected
    doubled = iter([payload, payload])
    assert sha256_stream(doubled) == hashlib.sha256(payload * 2).hexdigest()
    # Streaming in small blocks gives the same digest as one-shot hashing.
    blocks = [payload[i : i + 7] for i in range(0, len(payload), 7)]
    assert sha256_stream(iter(blocks)) == expected


def test_bounded_hash_runs_off_main_thread_and_returns_digest(tmp_path: Path) -> None:
    path = tmp_path / "small.bin"
    path.write_bytes(b"abc")
    assert sha256_file_bounded(path, timeout_seconds=2.0) == hashlib.sha256(b"abc").hexdigest()


def test_bounded_hash_enforces_timeout_without_long_main_block(tmp_path: Path) -> None:
    path = tmp_path / "big.bin"
    path.write_bytes(b"0" * 4096)

    def slow_hasher(target: str | Path) -> str:
        time.sleep(0.6)
        return "digest"

    start = time.monotonic()
    with pytest.raises(HashTimeoutError, match="exceeded"):
        sha256_file_bounded(path, timeout_seconds=0.05, hasher=slow_hasher)
    elapsed = time.monotonic() - start
    # The main flow must not wait for the slow worker beyond the small budget.
    assert elapsed < 0.5, elapsed


def test_bounded_hash_propagates_worker_errors(tmp_path: Path) -> None:
    missing = tmp_path / "missing.bin"
    with pytest.raises(OSError):
        sha256_file_bounded(missing, timeout_seconds=1.0)
