"""Efficiency guard: small attachments must stay on the single-request path.

The hard requirement is that small-attachment upload efficiency degrades by no
more than 10%. Since the design routes files at or below the chunk threshold
through the unchanged legacy multipart path (zero extra HTTP calls), the
strongest and least flaky assertion is structural: no chunk endpoint is ever
contacted for a small file. A latency-based sanity check complements it with a
generous, CI-safe margin.
"""
from __future__ import annotations
import statistics
import time
from pathlib import Path
from typing import Any
import pytest
from fillbot.browser.base import Locator
from fillbot.browser.mechanical import MechanicalBrowser


def _app() -> Any:
    from flask import Flask, Response, request

    app = Flask(__name__)
    calls: dict[str, int] = {"form": 0, "submit": 0, "chunk": 0}

    @app.get("/form")
    def form() -> str:
        calls["form"] += 1
        return (
            '<html><body><form id="f" method="post" action="/submit" enctype="multipart/form-data">'
            '<input id="file" name="file" type="file">'
            '<button id="go" type="submit">go</button></form></body></html>'
        )

    @app.post("/submit")
    def submit() -> Response:
        calls["submit"] += 1
        uploaded = request.files.get("file")
        if uploaded:
            uploaded.read()
        return Response("ok")

    @app.put("/__mock/uploads/<upload_id>/chunks/<int:index>")
    def chunk(upload_id: str, index: int) -> Any:  # pragma: no cover - must never fire
        calls["chunk"] += 1
        return Response(status=500)

    app.test_calls = calls  # type: ignore[attr-defined]
    return app


class _Checkpoints:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str], dict[str, Any]] = {}

    def save_upload_state(self, run_id: str, task_id: str, state: dict[str, Any]) -> None:
        self.states[(run_id, task_id)] = state

    def get_upload_state(self, run_id: str, task_id: str) -> dict[str, Any] | None:
        return self.states.get((run_id, task_id))


@pytest.mark.parametrize("size", [133, 1024, 8 * 1024, 32 * 1024])
def test_small_files_never_invoke_chunk_endpoints(tmp_path: Path, serve: Any, size: int) -> None:
    live = serve(app=_app())
    pdf = tmp_path / "small.pdf"
    pdf.write_bytes(b"a" * size)
    with MechanicalBrowser() as browser:
        browser.configure_chunk_uploads(
            live.base_url,
            chunk_size=32 * 1024,
            threshold=32 * 1024,
            run_id="R",
            task_id="T",
            checkpoint_store=_Checkpoints(),
        )
        browser.open(live.base_url + "/form")
        browser.upload(Locator(css="#file"), pdf)
        assert browser.click(Locator(css="#go")).status == 200
    assert live.app.test_calls["chunk"] == 0  # type: ignore[attr-defined]
    assert live.app.test_calls["submit"] == 1  # type: ignore[attr-defined]


def test_configured_browser_keeps_small_file_latency_within_budget(tmp_path: Path, serve: Any) -> None:
    live = serve(app=_app())
    pdf = tmp_path / "note.pdf"
    pdf.write_bytes(b"a" * 4096)

    def one_round(configured: bool) -> float:
        browser = MechanicalBrowser()
        if configured:
            browser.configure_chunk_uploads(
                live.base_url, chunk_size=32 * 1024, threshold=32 * 1024,
                run_id="R", task_id="T", checkpoint_store=_Checkpoints(),
            )
        start = time.perf_counter()
        browser.open(live.base_url + "/form")
        browser.upload(Locator(css="#file"), pdf)
        browser.click(Locator(css="#go"))
        elapsed = (time.perf_counter() - start) * 1000
        browser.close()
        return elapsed

    one_round(False)
    one_round(True)
    baseline = [one_round(False) for _ in range(25)]
    configured = [one_round(True) for _ in range(25)]
    # Same code path => medians must be within a generous 10% envelope on
    # loopback; this guards against accidental extra requests on small files.
    base_median, configured_median = statistics.median(baseline), statistics.median(configured)
    assert (configured_median - base_median) / base_median <= 0.10
