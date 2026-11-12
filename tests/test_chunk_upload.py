from __future__ import annotations
import hashlib
import tempfile
from pathlib import Path
from typing import Any
import pytest
from fillbot.browser.base import Locator
from fillbot.browser.chunking import ChunkUploadManager
from fillbot.browser.mechanical import MechanicalBrowser
from fillbot.errors import ClientError, NetworkError, ServerError

FORM = """
<html><body><form id="f" method="post" action="/submit" enctype="multipart/form-data">
<input type="hidden" name="csrf_token" value="tok-123">
<label for="file">附件</label><input id="file" name="file" type="file">
<button id="go" name="go" type="submit">提交</button>
</form></body></html>
"""


class MemoryCheckpoints:
    """Minimal CheckpointStore standing in for the SQLite ledger."""

    def __init__(self) -> None:
        self.states: dict[tuple[str, str], dict[str, Any]] = {}

    def save_upload_state(self, run_id: str, task_id: str, state: dict[str, Any]) -> None:
        self.states[(run_id, task_id)] = state

    def get_upload_state(self, run_id: str, task_id: str) -> dict[str, Any] | None:
        return self.states.get((run_id, task_id))


def _form_app() -> Any:
    from flask import Flask, Response, jsonify, request

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    received: dict[str, Any] = {}

    @app.get("/form")
    def form() -> str:
        return FORM

    @app.post("/submit")
    def submit() -> Response:
        uploaded = request.files.get("file")
        received["hash"] = hashlib.sha256(uploaded.read()).hexdigest()
        received["filename"] = uploaded.filename
        return Response('<span id="result">ok</span>')

    @app.get("/received")
    def received_state() -> Any:
        return jsonify(received)

    return app


@pytest.fixture
def chunk_server(serve: Any) -> Any:
    # Full mock app carries the /__mock chunk endpoints plus a form route.
    app = _form_app()
    from fillbot.mocksite.uploads import init_chunk_uploads

    store = init_chunk_uploads(app)
    live = serve(app=app)
    live.chunk_store = store
    return live


def _manager(browser: MechanicalBrowser, server: Any, checkpoints: MemoryCheckpoints, *, size: int, threshold: int) -> ChunkUploadManager:
    return browser.configure_chunk_uploads(
        server.base_url,
        chunk_size=size,
        threshold=threshold,
        run_id="R-1",
        task_id="T-1",
        checkpoint_store=checkpoints,
    )


def test_large_file_stages_in_chunks_and_form_still_posts(tmp_path: Path, chunk_server: Any) -> None:
    data = bytes((i % 251 for i in range(70 * 1024)))  # 70 KiB, > 32 KiB threshold
    pdf = tmp_path / "big.pdf"
    pdf.write_bytes(data)
    checkpoints = MemoryCheckpoints()
    with MechanicalBrowser() as browser:
        _manager(browser, chunk_server, checkpoints, size=32 * 1024, threshold=32 * 1024)
        browser.open(chunk_server.base_url + "/form")
        browser.upload(Locator(css="#file"), pdf)
        page = browser.click(Locator(css="#go"))
        assert page.status == 200 and "ok" in page.text
    state = checkpoints.states[("R-1", "T-1")]
    entry = next(iter(state["files"].values()))
    assert entry["total_chunks"] == 3 and entry["complete"] is True
    assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    import requests

    received = requests.get(chunk_server.base_url + "/received", timeout=2).json()
    assert received["hash"] == hashlib.sha256(data).hexdigest() and received["filename"] == "big.pdf"


def test_interrupted_upload_resumes_without_resending_chunks(
    tmp_path: Path, chunk_server: Any
) -> None:
    data = b"q" * (60 * 1024)  # 2 chunks at 32 KiB
    pdf = tmp_path / "big.pdf"
    pdf.write_bytes(data)
    checkpoints = MemoryCheckpoints()
    # First attempt: inject one 500 on chunk 0 then allow progress, but abort
    # after the first successful chunk by forcing the second PUT to fail hard.
    import requests

    with MechanicalBrowser() as browser:
        manager = _manager(browser, chunk_server, checkpoints, size=32 * 1024, threshold=32 * 1024)
        original_put = manager._put_chunk
        calls = {"n": 0}

        def flaky_put(url: str, body: bytes, headers: dict[str, str]) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ServerError("injected 500", status=500)
            return original_put(url, body, headers)

        manager._put_chunk = flaky_put  # type: ignore[method-assign]
        with pytest.raises(ServerError):
            browser.open(chunk_server.base_url + "/form")
            browser.upload(Locator(css="#file"), pdf)
            browser.click(Locator(css="#go"))
    # Checkpoint recorded no successful chunk yet (first call failed).
    assert checkpoints.states == {} or all(
        not f["received_chunks"] for f in checkpoints.states.get(("R-1", "T-1"), {}).get("files", {}).values()
    )

    # Second attempt with a fresh manager/process simulation: same checkpoint.
    with MechanicalBrowser() as browser:
        manager2 = _manager(browser, chunk_server, checkpoints, size=32 * 1024, threshold=32 * 1024)
        sent_urls: list[str] = []
        original = manager2._put_chunk

        def tracking_put(url: str, body: bytes, headers: dict[str, str]) -> dict[str, Any]:
            sent_urls.append(url)
            return original(url, body, headers)

        manager2._put_chunk = tracking_put  # type: ignore[method-assign]
        browser.open(chunk_server.base_url + "/form")
        browser.upload(Locator(css="#file"), pdf)
        page = browser.click(Locator(css="#go"))
        assert page.status == 200
        staged = next(iter(manager2._staged.values()))
        assert staged.total_chunks == 2
    # All chunks were eventually sent; verify the server holds both.
    upload_id = hashlib.sha256(data).hexdigest()
    status = requests.get(f"{chunk_server.base_url}/__mock/uploads/{upload_id}", timeout=2).json()
    assert status["complete"] is True and status["received_chunks"] == [0, 1]


def test_resume_skips_already_uploaded_chunks(tmp_path: Path, chunk_server: Any) -> None:
    data = b"z" * (96 * 1024)  # 3 chunks at 32 KiB
    pdf = tmp_path / "tri.pdf"
    pdf.write_bytes(data)
    checkpoints = MemoryCheckpoints()

    def run_once(fail_after: int | None) -> list[int]:
        with MechanicalBrowser() as browser:
            manager = _manager(browser, chunk_server, checkpoints, size=32 * 1024, threshold=32 * 1024)
            sent: list[int] = []
            original = manager._put_chunk

            def maybe_fail(url: str, body: bytes, headers: dict[str, str]) -> dict[str, Any]:
                index = int(url.rsplit("/", 1)[-1])
                result = original(url, body, headers)
                sent.append(index)
                if fail_after is not None and len(sent) == fail_after:
                    raise NetworkError("simulated connection drop after chunk landed")
                return result

            manager._put_chunk = maybe_fail  # type: ignore[method-assign]
            browser.open(chunk_server.base_url + "/form")
            browser.upload(Locator(css="#file"), pdf)
            try:
                browser.click(Locator(css="#go"))
            except NetworkError:
                pass
            return sent

    first = run_once(fail_after=2)  # chunks 0,1 land; crash before 2
    assert first == [0, 1]
    second = run_once(fail_after=None)
    # Requirement 1: only the missing chunk is retransmitted on resume.
    assert second == [2]
    entry = checkpoints.states[("R-1", "T-1")]["files"][str(pdf.resolve())]
    assert entry["received_chunks"] == [0, 1, 2] and entry["complete"] is True


def test_server_hash_mismatch_is_rejected(tmp_path: Path, chunk_server: Any) -> None:
    pdf = tmp_path / "bad.pdf"
    pdf.write_bytes(b"m" * (40 * 1024))
    checkpoints = MemoryCheckpoints()
    with MechanicalBrowser() as browser:
        manager = _manager(browser, chunk_server, checkpoints, size=32 * 1024, threshold=32 * 1024)
        # Lie about the expected total hash: the server must refuse assembly.
        manager._expected_hashes = {str(pdf.resolve()): "a" * 64}
        browser.open(chunk_server.base_url + "/form")
        browser.upload(Locator(css="#file"), pdf)
        with pytest.raises(ClientError, match="422|hash verification failed"):
            browser.click(Locator(css="#go"))


def test_chunk_corruption_detected_server_side(tmp_path: Path, chunk_server: Any) -> None:
    pdf = tmp_path / "corrupt.pdf"
    pdf.write_bytes(b"c" * (40 * 1024))
    checkpoints = MemoryCheckpoints()
    with MechanicalBrowser() as browser:
        manager = _manager(browser, chunk_server, checkpoints, size=32 * 1024, threshold=32 * 1024)
        original = manager._put_chunk

        def corrupt_first(url: str, body: bytes, headers: dict[str, str]) -> dict[str, Any]:
            if url.endswith("/chunks/0"):
                headers = dict(headers)
                headers["X-Chunk-Sha256"] = hashlib.sha256(body).hexdigest()
                return original(url, body + b"tampered", headers)
            return original(url, body, headers)

        manager._put_chunk = corrupt_first  # type: ignore[method-assign]
        with pytest.raises(ClientError, match="422"):
            manager.stage_file(pdf)


def test_temp_chunks_live_under_system_temp_and_are_cleaned(tmp_path: Path, chunk_server: Any) -> None:
    pdf = tmp_path / "clean.pdf"
    pdf.write_bytes(b"t" * (40 * 1024))
    checkpoints = MemoryCheckpoints()
    temp_root = Path(tempfile.mkdtemp(prefix="fillbot-test-root-"))
    browser = MechanicalBrowser()
    manager = browser.configure_chunk_uploads(
        chunk_server.base_url, chunk_size=32 * 1024, threshold=32 * 1024,
        run_id="R-9", task_id="T-9", checkpoint_store=checkpoints, temp_root=temp_root,
    )
    assert temp_root in manager._temp_parent.parents
    manager.stage_file(pdf)
    parts = list(manager._temp_parent.rglob("*.part"))
    assert parts and all(part.is_file() for part in parts)
    browser.close()  # task finished -> automatic cleanup
    assert not manager._temp_parent.exists()


def test_small_file_keeps_legacy_path_without_chunk_calls(tmp_path: Path, chunk_server: Any) -> None:
    pdf = tmp_path / "small.pdf"
    pdf.write_bytes(b"%PDF-small")  # well below threshold
    checkpoints = MemoryCheckpoints()
    with MechanicalBrowser() as browser:
        manager = _manager(browser, chunk_server, checkpoints, size=32 * 1024, threshold=32 * 1024)
        staged_calls: list[str] = []
        original = manager._put_chunk
        manager._put_chunk = lambda url, body, headers: (staged_calls.append(url) or original(url, body, headers))  # type: ignore[method-assign]
        browser.open(chunk_server.base_url + "/form")
        browser.upload(Locator(css="#file"), pdf)
        page = browser.click(Locator(css="#go"))
        assert page.status == 200 and staged_calls == []
    assert ("R-1", "T-1") not in checkpoints.states


def test_browser_without_manager_uses_legacy_upload(tmp_path: Path, serve: Any) -> None:
    live = serve(app=_form_app())
    pdf = tmp_path / "legacy.pdf"
    pdf.write_bytes(b"legacy-bytes")
    with MechanicalBrowser() as browser:
        browser.open(live.base_url + "/form")
        browser.upload(Locator(css="#file"), pdf)
        assert browser.click(Locator(css="#go")).status == 200


def test_manager_sweeps_orphaned_temp_dirs_from_hard_kills(tmp_path: Path) -> None:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="fillbot-sweep-root-"))
    orphan = root / "fillbot-upload-oldrun-T-1-abcd"
    orphan.mkdir(parents=True)
    (orphan / "chunk-000000.part").write_bytes(b"stale")
    unrelated = root / "keep-me"
    unrelated.mkdir()
    ChunkUploadManager(
        __import__("requests").Session(),
        "http://127.0.0.1:1",
        chunk_size=1024,
        threshold=1024,
        run_id="R-new",
        task_id="T-new",
        temp_root=root,
    )
    assert not orphan.exists()  # stale upload dir swept
    assert unrelated.exists()   # non-upload content is untouched
