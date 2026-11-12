from __future__ import annotations
import hashlib
from typing import Any
from fillbot.mocksite.app import create_app
from fillbot.mocksite.uploads import ChunkUploadStore, init_chunk_uploads


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _put(client: Any, upload_id: str, index: int, chunk: bytes, *, total: int, data: bytes, filename: str = "big.pdf") -> Any:
    return client.put(
        f"/__mock/uploads/{upload_id}/chunks/{index}",
        data=chunk,
        headers={
            "X-Chunk-Sha256": _digest(chunk),
            "X-Total-Chunks": str(total),
            "X-Total-Sha256": _digest(data),
            "X-Total-Size": str(len(data)),
            "X-Filename": filename,
        },
    )


def test_chunk_endpoints_assemble_verify_and_report_status() -> None:
    client = create_app(testing=True).test_client()
    data = bytes(range(256)) * 300  # 76.8 KiB -> multi-chunk at 32 KiB
    chunk_size = 32 * 1024
    total = (len(data) + chunk_size - 1) // chunk_size
    assert total >= 3
    upload_id = _digest(data)
    for index in range(total):
        response = _put(client, upload_id, index, data[index * chunk_size : (index + 1) * chunk_size],
                        total=total, data=data)
        assert response.status_code == 200
        body = response.get_json()
        assert body["complete"] is (index == total - 1)
    status = client.get(f"/__mock/uploads/{upload_id}")
    assert status.status_code == 200
    payload = status.get_json()
    assert payload["complete"] is True and payload["sha256"] == upload_id
    assert payload["received_chunks"] == list(range(total)) and payload["filename"] == "big.pdf"


def test_chunk_put_is_idempotent_and_resumable() -> None:
    client = create_app(testing=True).test_client()
    data = b"resume-me" * 5000
    chunk_size, total = 4096, (len(data) + 4095) // 4096
    upload_id = _digest(data)
    first = _put(client, upload_id, 0, data[:chunk_size], total=total, data=data)
    assert first.status_code == 200 and first.get_json()["complete"] is False
    # Re-uploading the identical chunk must be a no-op success (retry safety).
    again = _put(client, upload_id, 0, data[:chunk_size], total=total, data=data)
    assert again.status_code == 200 and again.get_json()["received_chunks"] == [0]
    # A different upload ordering still completes and verifies.
    for index in range(1, total):
        response = _put(client, upload_id, index, data[index * chunk_size : (index + 1) * chunk_size],
                        total=total, data=data)
        assert response.status_code == 200
    assert client.get(f"/__mock/uploads/{upload_id}").get_json()["sha256"] == upload_id


def test_chunk_checksum_and_metadata_mismatch_rejected() -> None:
    client = create_app(testing=True).test_client()
    data = b"integrity" * 100
    bad = client.put(
        f"/__mock/uploads/{_digest(data)}/chunks/0",
        data=data,
        headers={
            "X-Chunk-Sha256": "0" * 64,
            "X-Total-Chunks": "1",
            "X-Total-Sha256": _digest(data),
            "X-Total-Size": str(len(data)),
            "X-Filename": "x.pdf",
        },
    )
    assert bad.status_code == 422 and "checksum" in bad.get_json()["error"]
    missing = client.put("/__mock/uploads/x/chunks/0", data=b"a")
    assert missing.status_code == 400
    # Conflicting metadata for an existing session is rejected.
    ok = _put(client, "session-1", 0, b"a", total=1, data=b"a")
    assert ok.status_code == 200
    conflict = client.put(
        "/__mock/uploads/session-1/chunks/0",
        data=b"a",
        headers={
            "X-Chunk-Sha256": _digest(b"a"),
            "X-Total-Chunks": "2",
            "X-Total-Sha256": _digest(b"a"),
            "X-Total-Size": "1",
            "X-Filename": "x",
        },
    )
    assert conflict.status_code == 409
    assert client.get("/__mock/uploads/unknown").status_code == 404


def test_fault_injection_endpoint_drives_retryable_failures() -> None:
    client = create_app(testing=True).test_client()
    configured = client.post("/__mock/uploads/faults", json={"statuses": [500, 429]})
    assert configured.status_code == 200 and configured.get_json()["configured"] == [500, 429]
    headers = {
        "X-Chunk-Sha256": _digest(b"a"),
        "X-Total-Chunks": "1",
        "X-Total-Sha256": _digest(b"a"),
        "X-Total-Size": "1",
        "X-Filename": "x",
    }
    assert client.put("/__mock/uploads/flaky/chunks/0", data=b"a", headers=headers).status_code == 500
    limited = client.put("/__mock/uploads/flaky/chunks/0", data=b"a", headers=headers)
    assert limited.status_code == 429 and limited.get_json()["retryable"] is True
    # Faults are consumed; the next PUT succeeds.
    assert client.put("/__mock/uploads/flaky/chunks/0", data=b"a", headers=headers).status_code == 200
    bad = client.post("/__mock/uploads/faults", json={"statuses": ["nope"]})
    assert bad.status_code == 400


def test_chunk_store_uses_system_temp_and_cleans_up() -> None:
    import tempfile
    from pathlib import Path
    store = ChunkUploadStore()
    try:
        assert Path(tempfile.gettempdir()) in store.root.parents
        payload, status = store.put_chunk(
            "u1", 0, b"hello",
            chunk_sha256=_digest(b"hello"), total_chunks=1, total_sha256=_digest(b"hello"),
            total_size=5, filename="h.pdf",
        )
        assert status == 200 and payload["complete"] is True
        assert store.root.exists()
    finally:
        store.shutdown()
    assert not store.root.exists()


def test_blueprint_registration_is_additive() -> None:
    from fillbot.mocksite.uploads import ChunkUploadStore
    app = create_app(testing=True)
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    # Core form upload routes remain in place and untouched.
    assert "/report/step3" in rules and "/report/submit" in rules
    assert "/__mock/uploads/<upload_id>/chunks/<int:index>" in rules
    assert isinstance(app.extensions["fillbot_chunk_uploads"], ChunkUploadStore)
    # Re-initialising returns a fresh store wired onto the app.
    store = init_chunk_uploads(app)
    assert app.extensions["fillbot_chunk_uploads"] is store
