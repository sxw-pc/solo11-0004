from __future__ import annotations
import io
import re
from typing import Any
import pytest
from fillbot.mocksite.app import create_app
from fillbot.mocksite.chaos import ChaosController, ChaosSettings
from fillbot.mocksite.seed import ACCOUNTS, MockStore
TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
RECEIPT_RE = re.compile(r"RCP-\d{8}-\d{4}")
def _token(html: str) -> str:
    match = TOKEN_RE.search(html)
    assert match is not None
    return match.group(1)
def _login(client: Any, company_id: str = "C002") -> str:
    account = next(item for item in ACCOUNTS if item["company_id"] == company_id)
    html = client.get("/login").get_data(as_text=True)
    response = client.post(
        "/login",
        data={"csrf_token": _token(html), "username": account["username"], "password": account["password"]},
        follow_redirects=True,
    )
    return response.get_data(as_text=True)
def _to_confirm(client: Any, company_id: str = "C002", *, filename: str = "note.pdf", content: bytes = b"%PDF-fixture") -> str:
    account = next(item for item in ACCOUNTS if item["company_id"] == company_id)
    html = _login(client, company_id)
    response = client.post(
        "/report/step1",
        data={
            "csrf_token": _token(html),
            "company_id": company_id,
            "company_name": account["company_name"],
            "period": "2025-Q4",
            "contact_name": account["contact_name"],
            "report_date": "2025-11-10",
        },
    )
    assert response.status_code == 302
    html = client.get("/report/step2").get_data(as_text=True)
    assert client.post(
        "/report/step2", data={"csrf_token": _token(html), "electricity": "100.5", "water": "20", "gas": "5"}
    ).status_code == 302
    html = client.get("/report/step3").get_data(as_text=True)
    assert client.post(
        "/report/step3",
        data={
            "csrf_token": _token(html),
            "employees": "42",
            "hires": "2",
            "confirmed": "on",
            "attachment": (io.BytesIO(content), filename),
        },
        content_type="multipart/form-data",
    ).status_code == 302
    return client.get("/report/confirm").get_data(as_text=True)
def test_login_csrf_order_and_validation() -> None:
    app = create_app(testing=True)
    client = app.test_client()
    assert client.get("/").status_code == 302 and client.get("/report/step1").status_code == 302
    assert client.post("/login", data={"username": "parkco-b", "password": "x"}).status_code == 400
    html = client.get("/login").get_data(as_text=True)
    wrong = client.post("/login", data={"csrf_token": _token(html), "username": "parkco-b", "password": "wrong"})
    assert wrong.status_code == 401 and "不正确" in wrong.get_data(as_text=True)
    _login(client)
    assert client.get("/").status_code == 302 and client.get("/report/step2").status_code == 400
    assert client.post("/report/step2", data={}).status_code == 400
    assert "请先完成第二步" in client.get("/report/step3").get_data(as_text=True)
    assert client.post("/report/step1", data={"csrf_token": "bad"}).status_code == 400
    html = client.get("/report/step1").get_data(as_text=True)
    invalid = client.post(
        "/report/step1",
        data={
            "csrf_token": _token(html),
            "company_id": "C002",
            "company_name": "x",
            "period": "2025-Q9",
            "contact_name": "",
            "report_date": "not-a-date",
        },
    )
    assert invalid.status_code == 400 and "period" in invalid.get_data(as_text=True)
    assert "report_date" in invalid.get_data(as_text=True)
def test_step2_and_step3_server_validation() -> None:
    client = create_app(testing=True).test_client()
    html = _login(client)
    confirm = client.post(
        "/report/step1",
        data={
            "csrf_token": _token(html),
            "company_id": "C002",
            "company_name": "青岚谷生态科技（虚构）",
            "period": "2025-Q4",
            "contact_name": "林小岚（虚构）",
            "report_date": "2025-11-10",
        },
    )
    assert confirm.status_code == 302
    step2 = client.get("/report/step2").get_data(as_text=True)
    invalid = client.post(
        "/report/step2", data={"csrf_token": _token(step2), "electricity": "-1", "water": "x", "gas": "2"}
    )
    assert invalid.status_code == 400 and "electricity" in invalid.get_data(as_text=True)
    client.post("/report/step2", data={"csrf_token": _token(step2), "electricity": "1", "water": "2", "gas": "3"})
    step3 = client.get("/report/step3").get_data(as_text=True)
    high = client.post(
        "/report/step3",
        data={"csrf_token": _token(step3), "employees": "12000", "hires": "0", "confirmed": "on", "attachment": (io.BytesIO(b"%PDF"), "note.pdf")},
        content_type="multipart/form-data",
    )
    assert high.status_code == 400 and "10000" in high.get_data(as_text=True)
    step3 = client.get("/report/step3").get_data(as_text=True)
    bad = client.post(
        "/report/step3",
        data={"csrf_token": _token(step3), "employees": "1", "hires": "0", "confirmed": "on", "attachment": (io.BytesIO(b"text"), "note.txt")},
        content_type="multipart/form-data",
    )
    assert bad.status_code == 400 and "PDF" in bad.get_data(as_text=True)
def test_full_flow_duplicate_receipt_store_and_health() -> None:
    client = create_app(testing=True).test_client()
    confirm = _to_confirm(client)
    response = client.post("/report/submit", data={"csrf_token": _token(confirm), "company_id": "C002"})
    body = response.get_data(as_text=True)
    assert response.status_code == 200 and "报送回执" in body
    receipt = RECEIPT_RE.search(body)
    assert receipt is not None and receipt.group(0) != "RCP-20250115-0001"
    assert client.get(f"/receipt/{receipt.group(0)}").status_code == 200
    assert client.get("/receipt/RCP-20260101-9999").status_code == 404
    confirm = client.get("/report/confirm").get_data(as_text=True)
    duplicate = client.post("/report/submit", data={"csrf_token": _token(confirm), "company_id": "C002"})
    assert duplicate.status_code == 409 and "重复报送" in duplicate.get_data(as_text=True)
    assert client.get("/__mock/receipts").get_json()["count"] == 2
    assert client.get("/healthz").get_json()["ok"] is True
    assert client.get("/__mock/slow?ms=0").get_json()["slept_ms"] == 0
    store = MockStore()
    assert store.authenticate("parkco-a", "mock-only-alpha") is not None and store.authenticate("parkco-a", "wrong") is None
    assert store.find_submission("C001", "2025-Q3") is not None
    with pytest.raises(ValueError, match="duplicate"):
        store.add_submission("C001", "2025-Q3", {})
    created = store.add_submission("C002", "2025-Q4", {})
    assert RECEIPT_RE.fullmatch(created.receipt_no) and len(store.receipts()) == 2
def test_chaos_and_flaky_submit_retry() -> None:
    disabled = ChaosController(ChaosSettings())
    assert disabled.decide("GET", "/x").status is None
    settings = ChaosSettings(enabled=True, seed=9, failure_rate=1.0, retry_after=3)
    first, second = ChaosController(settings), ChaosController(settings)
    sequence = lambda controller: [(controller.decide("GET", "/x").status, controller.decide("GET", "/x").retry_after) for _ in range(2)]
    assert sequence(first) == sequence(second)
    flaky = ChaosController(ChaosSettings(enabled=True, flaky_submit=True, flaky_company="C003"))
    assert flaky.decide("POST", "/report/submit", "C002").status is None
    assert flaky.decide("POST", "/report/submit", "C003").status == 500
    assert flaky.decide("POST", "/report/submit", "C003").status is None
    assert flaky.decide("GET", "/__mock/receipts").status is None
    env = ChaosSettings.from_env(
        {
            "FILLBOT_CHAOS": "1",
            "FILLBOT_CHAOS_SEED": "3",
            "FILLBOT_CHAOS_RATE": "0.5",
            "FILLBOT_CHAOS_DELAY_MS": "5",
            "FILLBOT_CHAOS_FLAKY_SUBMIT": "1",
            "FILLBOT_CHAOS_FLAKY_COMPANY": "C003",
        }
    )
    assert env.enabled and env.flaky_submit and env.flaky_company == "C003"
    client = create_app(testing=True, chaos=ChaosSettings(enabled=True, flaky_submit=True, flaky_company="C003")).test_client()
    confirm = _to_confirm(client, "C003")
    token = _token(confirm)
    assert client.post("/report/submit", data={"csrf_token": token, "company_id": "C003"}).status_code == 500
    assert client.post("/report/submit", data={"csrf_token": token, "company_id": "C003"}).status_code == 200
