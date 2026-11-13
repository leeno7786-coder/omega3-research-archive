from __future__ import annotations

from fastapi.testclient import TestClient

from hud.server import create_app


def test_chat_forwards_all_composer_upload_refs_to_visual_ingest(monkeypatch) -> None:
    handled: dict[str, object] = {}

    def _handle(**kwargs: object) -> dict[str, object]:
        handled.update(kwargs)
        return {
            "ok": True,
            "queued": True,
            "cycle_id": kwargs["cycle_id"],
            "message": "",
            "delivery": {"text": "", "rendered_by": "queued"},
        }

    monkeypatch.setattr("pressure_loop.operator_chat.handle_operator_chat", _handle)
    response = TestClient(create_app()).post(
        "/api/chat",
        json={
            "text": "describe these images",
            "upload_ref": "/api/uploads/image/first.jpg",
            "uploads": [
                {"kind": "image", "upload_ref": "/api/uploads/image/first.jpg"},
                {"kind": "image", "upload_ref": "/api/uploads/image/second.png"},
            ],
        },
    )

    assert response.status_code == 200
    assert handled["upload_refs"] == [
        "/api/uploads/image/first.jpg",
        "/api/uploads/image/second.png",
    ]
