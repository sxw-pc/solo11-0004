"""Offline socket guard and shared local-server/config/task fixtures."""
from __future__ import annotations
import logging
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import pytest
from werkzeug.serving import make_server
from fillbot.config import BotConfig, load_config
from fillbot.mocksite.app import create_app
from fillbot.mocksite.chaos import ChaosSettings
logging.getLogger("werkzeug").setLevel(logging.ERROR)
ACCOUNTS = {
    "C001": ("parkco-a", "mock-only-alpha", "远山数据服务社（虚构）", "沈遥（虚构）"),
    "C002": ("parkco-b", "mock-only-beta", "青岚谷生态科技（虚构）", "林小岚（虚构）"),
    "C003": ("parkco-c", "mock-only-gamma", "溪谷智造工坊（虚构）", "顾青（虚构）"),
}
@dataclass
class LiveServer:
    app: Any
    server: Any
    base_url: str
    thread: threading.Thread
    @property
    def receipts(self) -> list[str]:
        return list(self.app.extensions["fillbot_store"].receipts())
    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=3)
@pytest.fixture(autouse=True)
def offline_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect
    def guarded_connect(sock: socket.socket, address: Any) -> Any:
        if (address[0] if isinstance(address, tuple) else "") != "127.0.0.1":
            raise RuntimeError(f"offline test guard blocked connection to {address!r}")
        return real_connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
@pytest.fixture
def serve() -> Any:
    servers: list[LiveServer] = []
    def start(*, chaos: ChaosSettings | None = None, app: Any | None = None) -> LiveServer:
        application = app or create_app(testing=True, chaos=chaos)
        server = make_server("127.0.0.1", 0, application, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        live = LiveServer(application, server, f"http://127.0.0.1:{server.server_port}", thread)
        servers.append(live)
        return live
    yield start
    for live in reversed(servers):
        live.close()
@pytest.fixture
def make_config(tmp_path: Path) -> Callable[..., BotConfig]:
    def build(base_url: str, **overrides: Any) -> BotConfig:
        values: dict[str, Any] = {
            "base_url": base_url,
            "evidence_dir": str(tmp_path / "evidence"),
            "ledger_path": str(tmp_path / "data" / "ledger.db"),
            "runs_dir": str(tmp_path / "runs"),
            "rate_limit_per_minute": 6000,
            "timeout_seconds": 5,
            "backoff_base_seconds": 0.01,
            "backoff_max_seconds": 0.05,
            "evidence_keep_runs": 10,
        }
        values.update(overrides)
        return load_config(overrides=values)
    return build
@pytest.fixture
def task_payload() -> Callable[..., dict[str, Any]]:
    def build(company_id: str = "C002", **overrides: Any) -> dict[str, Any]:
        username, password, company_name, contact = ACCOUNTS[company_id]
        payload: dict[str, Any] = {
            "username": username,
            "password": password,
            "company_id": company_id,
            "company_name": company_name,
            "period": "2025-Q4",
            "contact_name": contact,
            "report_date": "2025-11-10",
            "electricity": 12800.5,
            "water": 340.2,
            "gas": 88.1,
            "employees": 42,
            "hires": 3,
        }
        payload.update(overrides)
        return payload
    return build
