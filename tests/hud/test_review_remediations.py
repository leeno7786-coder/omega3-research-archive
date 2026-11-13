from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from hud import server as hs
from hud import telemetry
from pressure_loop import runtime as pressure_runtime
from runtime import document_store, image_store


def test_generated_images_with_identical_timestamp_and_prompt_do_not_overwrite(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(image_store, "IMAGE_ROOT", tmp_path / "images")
    when = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)

    first = image_store.persist_generated_image(b"first", "same prompt", when=when)
    second = image_store.persist_generated_image(b"second", "same prompt", when=when)

    assert first != second
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"


def test_documents_with_identical_timestamp_and_title_do_not_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(document_store, "DOCUMENT_ROOT", tmp_path / "documents")
    when = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)

    first = document_store.persist_document("first", "same title", "md", when=when)
    second = document_store.persist_document("second", "same title", "md", when=when)

    assert first != second
    assert first.read_text(encoding="utf-8") == "first"
    assert second.read_text(encoding="utf-8") == "second"


def test_uploads_with_identical_source_names_remain_distinct(tmp_path, monkeypatch):
    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(hs, "_UPLOAD_ROOT", upload_root)
    client = TestClient(hs.create_app())

    response = client.post(
        "/api/upload",
        files=[
            ("files", ("same.txt", b"first", "text/plain")),
            ("files", ("same.txt", b"second", "text/plain")),
        ],
    )

    assert response.status_code == 200
    rows = response.json()["files"]
    assert rows[0]["stored_as"] != rows[1]["stored_as"]
    assert (upload_root / "doc" / rows[0]["stored_as"]).read_bytes() == b"first"
    assert (upload_root / "doc" / rows[1]["stored_as"]).read_bytes() == b"second"


def test_active_episode_snapshot_reads_registered_pressure_runtime(monkeypatch):
    episode = SimpleNamespace(episode_id="ep-live", state="executing")
    runtime = SimpleNamespace(episode_store=SimpleNamespace(current=lambda: episode))
    monkeypatch.setattr(
        pressure_runtime,
        "try_get_default_runtime",
        lambda: runtime,
    )

    assert telemetry._active_episode_snapshot() == {
        "id": "ep-live",
        "state": "executing",
        "active": True,
    }


def test_chat_rejects_non_numeric_weight_as_client_error():
    client = TestClient(hs.create_app(), raise_server_exceptions=False)

    response = client.post(
        "/api/chat",
        json={"text": "hello", "weight": "not-a-number"},
    )

    assert response.status_code == 422


def test_chat_rejects_non_finite_weight_as_client_error():
    client = TestClient(hs.create_app(), raise_server_exceptions=False)

    response = client.post(
        "/api/chat",
        json={"text": "hello", "weight": "Infinity"},
    )

    assert response.status_code == 422


def test_idle_learning_rejects_string_boolean(monkeypatch):
    from pressure_loop import idle_wake

    monkeypatch.setattr(
        idle_wake,
        "set_operator_idle_learning",
        lambda enabled: {"enabled": enabled},
    )
    client = TestClient(hs.create_app())

    response = client.post("/api/idle-learning", json={"enabled": "false"})

    assert response.status_code == 422


def test_idle_learning_accepts_boolean(monkeypatch):
    from pressure_loop import idle_wake

    received: list[bool] = []

    def _set(enabled: bool):
        received.append(enabled)
        return {"enabled": enabled}

    monkeypatch.setattr(idle_wake, "set_operator_idle_learning", _set)
    client = TestClient(hs.create_app())

    response = client.post("/api/idle-learning", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert received == [False]


def test_gallery_routes_reject_non_positive_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(image_store, "IMAGE_ROOT", tmp_path / "images")
    monkeypatch.setattr(document_store, "DOCUMENT_ROOT", tmp_path / "documents")
    client = TestClient(hs.create_app())

    assert client.get("/api/images?limit=0").status_code == 422
    assert client.get("/api/documents?limit=-1").status_code == 422


def test_store_list_functions_return_empty_for_non_positive_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(image_store, "IMAGE_ROOT", tmp_path / "images")
    monkeypatch.setattr(document_store, "DOCUMENT_ROOT", tmp_path / "documents")
    when = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    image_store.persist_generated_image(b"image", "item", when=when)
    document_store.persist_document("document", "item", "md", when=when)

    assert image_store.list_generated_images(limit=0) == []
    assert document_store.list_documents(limit=-1) == []
