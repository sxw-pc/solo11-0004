"""Flask app factory for the fictional, loopback-only reporting platform."""
from __future__ import annotations
import os
import secrets
import time
from functools import wraps
from typing import Any, Callable
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from fillbot.mocksite.chaos import ChaosController, ChaosSettings
from fillbot.mocksite.forms_def import validate_step1, validate_step2, validate_step3
from fillbot.mocksite.seed import Account, MockStore
def create_app(
    *,
    testing: bool = False,
    chaos: ChaosSettings | None = None,
    store: MockStore | None = None,
    secret_key: str | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config.update(
        TESTING=testing,
        SECRET_KEY=secret_key or os.environ.get("FILLBOT_MOCK_SECRET") or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=128 * 1024,
    )
    mock_store = store or MockStore()
    chaos_controller = ChaosController(chaos or ChaosSettings.from_env())
    app.extensions["fillbot_store"], app.extensions["fillbot_chaos"] = mock_store, chaos_controller
    def token() -> str:
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(16)
        return str(session["csrf_token"])
    def csrf_ok() -> bool:
        expected = session.get("csrf_token", "")
        return bool(expected) and secrets.compare_digest(request.form.get("csrf_token", ""), expected)
    def current_account() -> Account | None:
        return mock_store.accounts.get(session.get("username")) if session.get("username") else None
    def report() -> dict[str, Any]:
        value = session.get("report")
        return value if isinstance(value, dict) else {}
    def save_report(value: dict[str, Any]) -> None:
        session["report"], session.modified = value, True
    def login_required(view: Callable[..., Response | str]) -> Callable[..., Response | str]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Response | str:
            return redirect(url_for("login")) if current_account() is None else view(*args, **kwargs)
        return wrapped
    def error_page(
        messages: list[str] | dict[str, list[str]], status: int = 400, title: str = "请求未完成"
    ) -> tuple[str, int]:
        flat = (
            [f"{field}: {message}" for field, items in messages.items() for message in items]
            if isinstance(messages, dict)
            else messages
        )
        return render_template("error.html", messages=flat, title=title), status
    @app.before_request
    def inject_chaos() -> Response | None:
        decision = chaos_controller.decide(request.method, request.path, request.form.get("company_id"))
        chaos_controller.apply_delay(decision)
        if decision.status is None:
            return None
        response = app.make_response(
            render_template("error.html", messages=["模拟故障注入：请稍后重试"], title="服务暂时不可用")
        )
        response.status_code = decision.status
        if decision.retry_after is not None:
            response.headers["Retry-After"] = str(decision.retry_after)
        return response
    @app.get("/")
    def index() -> Response | str:
        return redirect(url_for("login") if current_account() is None else url_for("step1"))
    @app.route("/login", methods=["GET", "POST"])
    def login() -> Response | str | tuple[str, int]:
        if request.method == "GET":
            return render_template("login.html", csrf_token=token(), error=None)
        if not csrf_ok():
            return error_page(["CSRF 校验失败，请刷新页面后重试"])
        account = mock_store.authenticate(request.form.get("username", "").strip(), request.form.get("password", ""))
        if account is None:
            return render_template("login.html", csrf_token=token(), error="账号或口令不正确"), 401
        session.clear()
        session["username"] = account.username
        token()
        return redirect(url_for("step1"))
    @app.get("/report/step1")
    @login_required
    def step1() -> str:
        account = current_account()
        assert account is not None
        values = report().get("step1") or {
            "company_id": account.company_id,
            "company_name": account.company_name,
            "period": "2025-Q4",
            "contact_name": account.contact_name,
            "report_date": "2025-11-10",
        }
        return render_template("step1.html", csrf_token=token(), values=values, errors={})
    @app.post("/report/step1")
    @login_required
    def step1_post() -> Response | str | tuple[str, int]:
        account = current_account()
        assert account is not None
        if not csrf_ok():
            return error_page(["CSRF 校验失败"])
        values, errors = validate_step1(request.form, account)
        if errors:
            return render_template("step1.html", csrf_token=token(), values=values, errors=errors), 400
        save_report({"step1": values})
        return redirect(url_for("step2"))
    @app.get("/report/step2")
    @login_required
    def step2() -> str | tuple[str, int]:
        if not report().get("step1"):
            return error_page(["请先完成第一步（步骤顺序错误）"])
        values = report().get("step2") or {"electricity": "", "water": "", "gas": ""}
        return render_template("step2.html", csrf_token=token(), values=values, errors={})
    @app.post("/report/step2")
    @login_required
    def step2_post() -> Response | str | tuple[str, int]:
        data = report()
        if not data.get("step1"):
            return error_page(["请先完成第一步（步骤顺序错误）"])
        if not csrf_ok():
            return error_page(["CSRF 校验失败"])
        values, errors = validate_step2(request.form)
        if errors:
            return render_template("step2.html", csrf_token=token(), values=values, errors=errors), 400
        data["step2"] = values
        data.pop("step3", None)
        save_report(data)
        return redirect(url_for("step3"))
    @app.get("/report/step3")
    @login_required
    def step3() -> str | tuple[str, int]:
        if not report().get("step2"):
            return error_page(["请先完成第二步（步骤顺序错误）"])
        values = report().get("step3") or {"employees": "", "hires": ""}
        return render_template("step3.html", csrf_token=token(), values=values, errors={})
    @app.post("/report/step3")
    @login_required
    def step3_post() -> Response | str | tuple[str, int]:
        data = report()
        if not data.get("step2"):
            return error_page(["请先完成第二步（步骤顺序错误）"])
        if not csrf_ok():
            return error_page(["CSRF 校验失败"])
        values, errors = validate_step3(request.form, request.files.get("attachment"))
        if errors:
            return render_template("step3.html", csrf_token=token(), values=values, errors=errors), 400
        data["step3"] = values
        save_report(data)
        return redirect(url_for("confirm"))
    @app.get("/report/confirm")
    @login_required
    def confirm() -> str | tuple[str, int]:
        data = report()
        if not data.get("step3"):
            return error_page(["请先完成第三步（步骤顺序错误）"])
        return render_template("confirm.html", csrf_token=token(), report=data)
    @app.post("/report/submit")
    @login_required
    def submit() -> Response | str | tuple[str, int]:
        data = report()
        if not data.get("step3"):
            return error_page(["请先完成所有步骤（步骤顺序错误）"])
        if not csrf_ok():
            return error_page(["CSRF 校验失败"])
        account = current_account()
        assert account is not None
        period = str(data.get("step1", {}).get("period", ""))
        submission = mock_store.submit_if_absent(
            account.company_id, period, {key: data.get(key) for key in ("step1", "step2", "step3")}
        )
        if submission is None:
            existing = mock_store.find_submission(account.company_id, period)
            return error_page(
                [f"该企业本季度已报送，回执号 {existing.receipt_no if existing else ''}"],
                status=409,
                title="重复报送",
            )
        session["last_receipt"] = submission.receipt_no
        return render_template("receipt.html", receipt=submission, submission=submission)
    @app.get("/receipt/<receipt_no>")
    @login_required
    def receipt_page(receipt_no: str) -> Response | str | tuple[str, int]:
        item = mock_store.find_receipt(receipt_no)
        if item is not None:
            return render_template("receipt.html", receipt=item, submission=item)
        return error_page(["回执不存在"], status=404, title="回执不存在")
    @app.get("/__mock/receipts")
    def mock_receipts() -> Response:
        return jsonify({"count": mock_store.submission_count(), "receipts": mock_store.receipts()})
    @app.get("/__mock/slow")
    def mock_slow() -> Response:
        try:
            millis = max(0, min(2000, int(request.args.get("ms", "100"))))
        except ValueError:
            millis = 100
        time.sleep(millis / 1000)
        return jsonify({"slept_ms": millis})
    @app.get("/healthz")
    def healthz() -> Response:
        return jsonify({"ok": True, "service": "fictional-reporting-mock"})
    return app
