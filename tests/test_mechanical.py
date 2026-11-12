from __future__ import annotations
import time
from typing import Any
import pytest
import requests
from flask import Flask, Response, jsonify, request
from fillbot.browser.base import Locator
from fillbot.browser.mechanical import MechanicalBrowser
from fillbot.errors import ClientError, NetworkError, ScriptError, ServerError
FORM = """
<html><body><form id="f" method="post" action="/submit" enctype="multipart/form-data">
<input type="hidden" name="csrf_token" value="tok-123"><label for="name">姓名</label><input id="name" name="name">
<label for="color">颜色</label><select id="color" name="color"><option value="red">红</option><option value="green">绿</option></select>
<label for="agree">同意</label><input id="agree" name="agree" type="checkbox" value="yes">
<label for="file">附件</label><input id="file" name="file" type="file"><button id="go" name="go" type="submit">提交</button>
</form></body></html>
"""
def _app() -> Flask:
    app = Flask(__name__)
    state: dict[str, Any] = {"submissions": [], "flaky": 0}
    @app.get("/form")
    def form() -> str:
        return FORM
    @app.post("/submit")
    def submit() -> Response | tuple[str, int]:
        if request.form.get("csrf_token") != "tok-123":
            return "csrf failed", 400
        uploaded = request.files.get("file")
        state["submissions"].append(
            {
                "form": request.form.to_dict(),
                "file": uploaded.read().decode("utf-8", "replace") if uploaded else "",
                "filename": uploaded.filename if uploaded else "",
            }
        )
        return '<html><body><span id="result">ok</span></body></html>'
    @app.get("/state")
    def get_state() -> Response:
        return jsonify(state)
    @app.get("/redirect")
    def redirect_route() -> Response:
        return Response("", status=302, headers={"Location": "/form"})
    @app.get("/slow")
    def slow() -> str:
        time.sleep(0.2)
        return "slow"
    @app.get("/missing")
    def missing() -> tuple[str, int]:
        return "missing", 404
    @app.get("/boom")
    def boom() -> tuple[str, int]:
        return "boom", 500
    @app.get("/limited")
    def limited() -> Response:
        return Response("wait", status=429, headers={"Retry-After": "2"})
    @app.get("/noform")
    def noform() -> str:
        return "<button id='lonely'>孤立</button>"
    @app.get("/flaky-form")
    def flaky_form() -> str:
        return FORM.replace('action="/submit"', 'action="/flaky-submit"')
    @app.post("/flaky-submit")
    def flaky_submit() -> Response:
        state["flaky"] += 1
        return Response("temporary", status=500) if state["flaky"] == 1 else Response("ok")
    @app.get("/cookie/set")
    def cookie_set() -> Response:
        response = Response("set")
        response.set_cookie("session_id", "abc")
        return response
    @app.get("/cookie/check")
    def cookie_check() -> tuple[str, int]:
        return ("yes", 200) if request.cookies.get("session_id") == "abc" else ("no", 400)
    return app
@pytest.fixture
def browser_server(serve: Any) -> Any:
    return serve(app=_app())
def test_submit_upload_redirect_and_cookies(browser_server: Any, tmp_path: Any) -> None:
    file_path = tmp_path / "note.pdf"
    file_path.write_bytes(b"%PDF-local")
    with MechanicalBrowser() as browser:
        assert browser.open(browser_server.base_url + "/form").status == 200
        browser.fill(Locator(label="姓名"), "Ada")
        browser.select(Locator(css="#color"), "绿")
        browser.check(Locator(label="同意"), True)
        browser.upload(Locator(css="#file"), file_path)
        result = browser.click(Locator(css="#go"))
        browser.open(browser_server.base_url + "/cookie/set")
        assert browser.open(browser_server.base_url + "/cookie/check").status == 200
    assert result.status == 200 and "ok" in result.text
    submission = requests.get(browser_server.base_url + "/state", timeout=2).json()["submissions"][0]
    form = submission["form"]
    assert {key: form[key] for key in ("csrf_token", "name", "color", "agree")} == {
        "csrf_token": "tok-123",
        "name": "Ada",
        "color": "green",
        "agree": "yes",
    }
    assert (submission["filename"], submission["file"]) == ("note.pdf", "%PDF-local")
    with MechanicalBrowser(follow_redirects=False) as browser:
        assert browser.open(browser_server.base_url + "/redirect").status == 302
    with MechanicalBrowser(follow_redirects=True) as browser:
        page = browser.open(browser_server.base_url + "/redirect")
        assert page.status == 200 and "form" in page.url
def test_error_classification_and_network(browser_server: Any) -> None:
    with MechanicalBrowser() as browser:
        with pytest.raises(ClientError) as client:
            browser.open(browser_server.base_url + "/missing")
        assert client.value.status == 404 and client.value.page is not None
        with pytest.raises(ServerError) as server:
            browser.open(browser_server.base_url + "/boom")
        assert server.value.status == 500
        with pytest.raises(ServerError) as limited:
            browser.open(browser_server.base_url + "/limited")
        assert limited.value.retry_after == 2.0
        with pytest.raises(NetworkError, match="timeout"):
            browser.open(browser_server.base_url + "/slow", timeout=0.02)
        with pytest.raises(NetworkError, match="NETWORK"):
            browser.open("http://127.0.0.1:1/unreachable", timeout=0.2)
def test_staging_errors_and_replay(browser_server: Any, tmp_path: Any) -> None:
    browser = MechanicalBrowser()
    with pytest.raises(ScriptError, match="no page loaded"):
        _ = browser.page
    browser.open(browser_server.base_url + "/form")
    missing = tmp_path / "missing.pdf"
    calls = [
        (lambda: browser.fill(Locator(css="#file"), "x"), "text field"),
        (lambda: browser.select(Locator(css="#name"), "x"), "not a select"),
        (lambda: browser.select(Locator(css="#color"), "purple"), "option"),
        (lambda: browser.check(Locator(css="#name")), "checkable"),
        (lambda: browser.upload(Locator(css="#name"), missing), "file input"),
        (lambda: browser.upload(Locator(css="#file"), missing), "not found"),
    ]
    for call, fragment in calls:
        with pytest.raises(ScriptError, match=fragment):
            call()
    browser.close()
    with MechanicalBrowser() as browser:
        browser.open(browser_server.base_url + "/noform")
        with pytest.raises(ScriptError, match="not inside a form"):
            browser.click(Locator(css="#lonely"))
        browser.open(browser_server.base_url + "/flaky-form")
        browser.fill(Locator(css="#name"), "Retry")
        with pytest.raises(ServerError):
            browser.click(Locator(css="#go"))
        assert browser.click(Locator(css="#go")).status == 200
