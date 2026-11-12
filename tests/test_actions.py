from __future__ import annotations
from pathlib import Path
import pytest
from fillbot.browser.base import BrowserSession, Locator, Page
from fillbot.dsl.actions import ACTION_HANDLERS, ActionContext, action_assert_status, action_assert_text
from fillbot.dsl.actions import action_extract, action_set_var, action_upload, action_wait
from fillbot.dsl.schema import ACTION_REGISTRY, Step
from fillbot.dsl.selectors import extract_text, normalized_text, parse_html, resolve_clickable, resolve_control, text_content
from fillbot.dsl.selectors import text_present
from fillbot.errors import AssertionFailure, ScriptError
HTML = """
<html><body>
<label for="name">姓名</label><input id="name" name="user" value="Ada">
<label>邮箱 <input id="email" name="mail"></label><input id="phone" aria-label="电话">
<label for="a">选项</label><input id="a"><label for="b">选项</label><input id="b">
<label for="orphan">孤立</label><button id="go" type="submit">提交</button>
<a id="link" href="/receipt/1">回执链接</a><span id="receipt">RCP-20260101-0001</span>
<span id="empty"></span><div id="box">你好 世界</div><label for="send">发送</label><button id="send">发送</button>
</body></html>
"""
class MinimalBrowser(BrowserSession):
    def __init__(self, page: Page | None) -> None:
        self._page = page
    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("no page")
        return self._page
    def open(self, url: str, **kwargs: object) -> Page:
        assert self._page is not None
        return self._page
    def fill(self, locator: Locator, value: str) -> None:
        pass
    def select(self, locator: Locator, value: str) -> None:
        pass
    def check(self, locator: Locator, checked: bool = True) -> None:
        pass
    def upload(self, locator: Locator, path: Path) -> None:
        pass
    def click(self, locator: Locator, **kwargs: object) -> Page:
        assert self._page is not None
        return self._page
    def close(self) -> None:
        pass
def _step(action: str, params: dict, locator: Locator | None = None) -> Step:
    return Step(index=1, action=action, params=params, locator=locator)
def test_selectors() -> None:
    soup = parse_html(HTML)
    assert resolve_control(soup, Locator(css="#name"))["name"] == "user"
    assert resolve_control(soup, Locator(label="姓名"))["id"] == "name"
    assert resolve_control(soup, Locator(label="邮箱"))["id"] == "email"
    assert resolve_control(soup, Locator(label="电话"))["id"] == "phone"
    for locator, fragment in [
        (Locator(css="#missing"), "selector not found"),
        (Locator(label="不存在"), "label not found"),
        (Locator(label="选项"), "ambiguous"),
        (Locator(label="孤立"), "no associated"),
    ]:
        with pytest.raises(ScriptError, match=fragment):
            resolve_control(soup, locator)
    assert resolve_clickable(soup, Locator(css="#go"))["id"] == "go"
    assert [resolve_clickable(soup, Locator(label=label))["id"] for label in ("回执链接", "提交", "发送")] == [
        "link",
        "go",
        "send",
    ]
    for locator, fragment in [
        (Locator(css="#none"), "not found"),
        (Locator(label="看不见"), "not found"),
        (Locator(css="button"), "expected one"),
    ]:
        with pytest.raises(ScriptError, match=fragment):
            resolve_clickable(soup, locator)
    assert extract_text(HTML, Locator(css="#receipt")) == "RCP-20260101-0001"
    assert extract_text(HTML, Locator(css="#link"), "href") == "/receipt/1"
    assert extract_text(HTML, Locator(css="#name")) == "Ada"
    for locator, attr, fragment in [
        (Locator(css="#receipt"), "href", "attribute"),
        (Locator(css="#empty"), None, "empty"),
        (Locator(css="label"), None, "ambiguous"),
    ]:
        with pytest.raises(ScriptError, match=fragment):
            extract_text(HTML, locator, attr)
    assert text_present(HTML, "你好") and text_present(HTML, "世界", Locator(css="#box"))
    assert not text_present(HTML, "不存在") and normalized_text(" a \n  b ") == "a b"
    assert text_content(parse_html('<input value="x">').find("input")) == "x"
def test_actions_and_errors(tmp_path: Path) -> None:
    assert set(ACTION_HANDLERS) == set(ACTION_REGISTRY)
    browser = MinimalBrowser(Page(200, "http://x", {}, '<div id="box">你好 <span id="r">R-1</span></div>'))
    context = ActionContext(browser=browser)
    action_assert_text(context, _step("assert_text", {"text": "你好"}, Locator(css="#box")))
    action_assert_status(context, _step("assert_status", {"status": 200}))
    assert action_extract(context, _step("extract", {}, Locator(css="#r"))) == "R-1"
    for call, fragment in [
        (lambda: action_assert_text(context, _step("assert_text", {"text": "absent"})), "not found"),
        (lambda: action_assert_status(context, _step("assert_status", {"status": 201})), "expected HTTP"),
        (lambda: action_extract(context, _step("extract", {}, Locator(css="#none"))), "not found"),
        (lambda: action_upload(context, _step("upload", {"path": str(tmp_path / "nope")}, Locator(css="#f"))), "not found"),
        (lambda: action_wait(context, _step("wait", {"seconds": 301})), "300"),
        (lambda: action_assert_status(ActionContext(browser=MinimalBrowser(None)), _step("assert_status", {})), "no page"),
    ]:
        with pytest.raises((AssertionFailure, ScriptError), match=fragment):
            call()
    sleeps: list[float] = []
    context.sleeper = sleeps.append
    action_wait(context, _step("wait", {"seconds": 0.5}))
    action_set_var(context, _step("set_var", {"name": "x", "value": {"a": 1}}))
    assert sleeps == [0.5] and context.variables["x"] == {"a": 1}
