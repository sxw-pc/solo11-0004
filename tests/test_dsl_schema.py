from __future__ import annotations
from pathlib import Path
import pytest
from fillbot.dsl.schema import available_actions, load_script, validate_script
from fillbot.errors import ScriptError, UnregisteredActionError
ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
def _script(*steps: dict) -> dict:
    return {"name": "test", "version": 1, "steps": list(steps)}
def test_example_script_and_common_options() -> None:
    script = load_script(ROOT / "scripts" / "quarterly_report.yaml")
    assert script.name == "quarterly-report" and len(script.steps) == 29
    assert script.steps[25].final is True and script.steps[25].retry.attempts == 3
    assert load_script(FIXTURES / "script_fixture.yaml").steps[0].action == "open"
    assert "extract" in available_actions()
    parsed = validate_script(
        _script(
            {"action": "open", "url": "http://127.0.0.1:1/x"},
            {"action": "fill", "label": "姓名", "value": "", "timeout": 2},
            {"action": "check", "selector": "#ok", "checked": False},
            {"action": "assert_text", "text": "hello", "label": "标题"},
            {"action": "extract", "selector": "#r", "save_as": "r", "attr": "href"},
            {"action": "wait", "seconds": 0, "on_error": "skip", "retry": 2},
            {"action": "click", "selector": "#go", "final": True},
            {"action": "set_var", "name": "x", "value": 1},
        )
    )
    assert parsed.steps[1].timeout == 2 and parsed.steps[4].locator is not None
    assert parsed.steps[5].retry.attempts == 2
    assert validate_script(_script({"action": "assert_text", "text": "x", "attr": "data-x"})).steps[0].params["attr"] == "data-x"
def test_unregistered_action_and_top_level_validation() -> None:
    with pytest.raises(UnregisteredActionError) as raised:
        validate_script(_script({"action": "teleport"}))
    assert "teleport" in str(raised.value) and "fill" in str(raised.value)
    cases = [
        ({"name": "x", "version": 1, "steps": [{"action": "wait", "seconds": 0}], "x": 1}, "unknown top-level"),
        ({"version": 1, "steps": []}, "name"),
        ({"name": "x", "version": 2, "steps": []}, "version"),
        ({"name": "x", "version": 1, "steps": []}, "non-empty"),
        ([], "mapping"),
        (_script([]), "mapping"),
        (_script({"selector": "#a"}), "action is required"),
    ]
    for doc, fragment in cases:
        with pytest.raises(ScriptError, match=fragment):
            validate_script(doc)
@pytest.mark.parametrize(
    "step, fragment",
    [
        ({"action": "fill", "value": "x"}, "exactly one"),
        ({"action": "fill", "selector": "#a", "label": "A", "value": "x"}, "exactly one"),
        ({"action": "open", "url": "http://x", "selector": "#a"}, "does not accept"),
        ({"action": "click", "selector": "#a", "extra": 1}, "unsupported parameter"),
        ({"action": "fill", "selector": "#a"}, "missing required"),
        ({"action": "extract", "selector": "#r"}, "save_as"),
        ({"action": "wait", "seconds": "soon"}, "must be a number"),
        ({"action": "wait", "seconds": -1}, ">= 0"),
        ({"action": "assert_status", "status": True}, "100..599"),
        ({"action": "assert_status", "status": 99}, "100..599"),
        ({"action": "check", "selector": "#c", "checked": "yes"}, "boolean"),
        ({"action": "set_var", "name": "1bad", "value": 1}, "identifier"),
        ({"action": "open", "url": ""}, "non-empty"),
        ({"action": "click", "selector": "", "final": True}, "non-empty"),
        ({"action": "click", "selector": "#a", "on_error": "explode"}, "on_error"),
        ({"action": "click", "selector": "#a", "timeout": 0}, "> 0"),
        ({"action": "click", "selector": "#a", "final": "yes"}, "boolean"),
        ({"action": "fill", "selector": "#a", "value": "x", "final": True}, "only valid"),
        ({"action": "extract", "selector": "#a", "save_as": "x", "attr": ""}, "non-empty"),
    ],
)
def test_step_validation_errors(step: dict, fragment: str) -> None:
    with pytest.raises(ScriptError, match=fragment):
        validate_script(_script(step))
@pytest.mark.parametrize(
    "retry, fragment",
    [
        (True, "mapping or integer"),
        (0, "attempts"),
        ([], "mapping or integer"),
        ({"attempts": 0}, "attempts"),
        ({"attempts": True}, "attempts"),
        ({"attempts": 2, "jitter": 1}, "unknown retry"),
        ({"attempts": 2, "base_seconds": -1}, "base_seconds"),
        ({"attempts": 2, "base_seconds": 2, "max_seconds": 1}, "max_seconds"),
    ],
)
def test_retry_validation(retry: object, fragment: str) -> None:
    with pytest.raises(ScriptError, match=fragment):
        validate_script(_script({"action": "click", "selector": "#a", "retry": retry}))
def test_load_script_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ScriptError, match="not found"):
        load_script(tmp_path / "none.yaml")
    empty, bad = tmp_path / "empty.yaml", tmp_path / "bad.yaml"
    empty.write_text("")
    bad.write_text("name: [")
    with pytest.raises(ScriptError, match="empty"):
        load_script(empty)
    with pytest.raises(ScriptError, match="cannot read"):
        load_script(bad)
