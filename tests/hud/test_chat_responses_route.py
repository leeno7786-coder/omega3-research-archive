"""GET /api/chat/responses — the HUD polls this on the queued cycle_id to
resolve the 'thinking' bubble into the system's async answer."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hud.server import create_app  # noqa: E402
import pressure_loop.operator_chat_responses as ocr  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "_store_path", lambda: tmp_path / "ocr.json")
    monkeypatch.setattr(ocr, "_lane_updates_path", lambda: tmp_path / "lane-updates.json")
    ocr._reset_cache_for_tests()


def test_poll_returns_answer_and_marks_delivered(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ocr.append_response(
        cycle_id="cyc-1",
        episode_id="ep",
        text="Hello Noah",
        prompt_pack={"prompt_type": "RESPONSE_QUESTION"},
        thinking={"memory": {"entries": [{"label": "Creator", "preview": "Built by Noah"}]}},
    )

    client = TestClient(create_app())
    r = client.get("/api/chat/responses", params={"cycle_id": "cyc-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["message"] == "Hello Noah"
    assert body["delivery"]["text"] == "Hello Noah"
    assert body["delivery"]["rendered_by"] == "omega_async"
    assert body["prompt_pack"]["prompt_type"] == "RESPONSE_QUESTION"
    assert body["thinking"]["memory"]["entries"][0]["label"] == "Creator"
    # answered once: the record is now delivered
    assert ocr.get_response("cyc-1")["status"] == "delivered"


def test_specialist_lane_update_endpoint_returns_durable_correlated_events(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ocr.append_response(
        cycle_id="cyc-lane",
        episode_id="chat-ep",
        text="Queued for Browser.",
        lane_work={
            "handoff_id": "h" * 64,
            "lane": "browser",
            "original_cycle_id": "cyc-lane",
            "thread_id": "thread-1",
        },
    )
    ocr.append_lane_update(
        cycle_id="cyc-lane",
        episode_id="browser-ep",
        lane="browser",
        handoff_id="h" * 64,
        status="completed",
        text="Found two matching pages.",
        update_id="browser-ep:completed",
    )

    client = TestClient(create_app())
    body = client.get(
        "/api/chat/updates", params={"cycle_id": "cyc-lane", "after_seq": 0}
    ).json()

    assert body["cycle_id"] == "cyc-lane"
    assert body["lane"] == "browser"
    assert body["status"] == "completed"
    assert body["done"] is True
    assert body["updates"][0]["text"] == "Found two matching pages."
    assert body["updates"][0]["episode_id"] == "browser-ep"


def test_active_lane_endpoint_recovers_a_reply_already_delivered(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ocr.append_response(
        cycle_id="cyc-active",
        episode_id="chat-ep",
        text="Queued for Coding Agent.",
        lane_work={
            "handoff_id": "a" * 64,
            "lane": "agent",
            "original_cycle_id": "cyc-active",
            "thread_id": "thread-2",
        },
    )
    ocr.mark_delivered("cyc-active")
    body = TestClient(create_app()).get("/api/chat/active-lanes").json()

    assert body["lanes"] == [{
        "cycle_id": "cyc-active", "lane": "agent", "status": "queued", "last_seq": 0,
        "acknowledged_seq": 0, "acknowledged": True,
    }]


def test_chat_update_ack_endpoint_persists_the_rendered_cursor(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ocr.append_lane_update(
        cycle_id="cyc-ack", episode_id="browser-ep", lane="browser",
        handoff_id="a" * 64, status="completed", text="Browser done.",
        update_id="browser-ep:completed",
    )

    response = TestClient(create_app()).post(
        "/api/chat/updates/ack",
        json={"cycle_id": "cyc-ack", "through_seq": 1},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "acknowledged_seq": 1}
    assert ocr.list_active_lane_work() == []


def test_poll_marks_a_failed_cascade_reply_as_a_failed_terminal_result(
    tmp_path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    ocr.append_response(
        cycle_id="cyc-failed",
        episode_id="ep-failed",
        text="I could not complete this turn: Global A produced no intent decision.",
        delivery={
            "text": "I could not complete this turn: Global A produced no intent decision.",
            "rendered_by": "omega_async",
            "failed": True,
        },
    )

    body = TestClient(create_app()).get(
        "/api/chat/responses", params={"cycle_id": "cyc-failed"}
    ).json()

    assert body["found"] is True
    assert body["ok"] is False
    assert body["delivery"]["failed"] is True


def test_poll_preserves_assigned_physical_correlation(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ocr.append_response(
        cycle_id="cyc-owned",
        episode_id="ep-owned",
        text="owned answer",
        pipeline_id=2,
        job_id="job-owned",
        work_kind="operator_turn",
    )

    body = TestClient(create_app()).get(
        "/api/chat/responses", params={"cycle_id": "cyc-owned"}
    ).json()

    assert body["pipeline_id"] == 2
    assert body["job_id"] == "job-owned"
    assert body["work_kind"] == "operator_turn"


def test_poll_omits_unknown_physical_correlation(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ocr.append_response(cycle_id="cyc-unassigned", episode_id="ep", text="answer")

    body = TestClient(create_app()).get(
        "/api/chat/responses", params={"cycle_id": "cyc-unassigned"}
    ).json()

    assert "pipeline_id" not in body
    assert "job_id" not in body
    assert "work_kind" not in body


def test_orphan_recovery_preserves_assigned_physical_correlation(monkeypatch):
    record = {
        "cycle_id": "cyc-orphan-owned",
        "episode_id": "ep-owned",
        "text": "recovered",
        "pipeline_id": 1,
        "job_id": "job-orphan",
        "work_kind": "operator_turn",
    }
    monkeypatch.setattr(ocr, "list_undelivered", lambda min_age_s: [record])
    monkeypatch.setattr(ocr, "mark_delivered", lambda cycle_id: record)

    body = TestClient(create_app()).get("/api/chat/responses/orphans").json()

    assert body["responses"][0]["pipeline_id"] == 1
    assert body["responses"][0]["job_id"] == "job-orphan"
    assert body["responses"][0]["work_kind"] == "operator_turn"


def test_orphan_recovery_omits_unknown_physical_correlation(monkeypatch):
    record = {
        "cycle_id": "cyc-orphan-unassigned",
        "episode_id": "ep-unassigned",
        "text": "recovered",
    }
    monkeypatch.setattr(ocr, "list_undelivered", lambda min_age_s: [record])
    monkeypatch.setattr(ocr, "mark_delivered", lambda cycle_id: record)

    recovered = TestClient(create_app()).get(
        "/api/chat/responses/orphans"
    ).json()["responses"][0]

    assert "pipeline_id" not in recovered
    assert "job_id" not in recovered
    assert "work_kind" not in recovered


def test_orphan_recovery_preserves_failed_terminal_state(monkeypatch):
    record = {
        "cycle_id": "cyc-orphan-failed",
        "episode_id": "ep-failed",
        "text": "I could not complete this turn.",
        "delivery": {"text": "I could not complete this turn.", "failed": True},
    }
    monkeypatch.setattr(ocr, "list_undelivered", lambda min_age_s: [record])
    monkeypatch.setattr(ocr, "mark_delivered", lambda cycle_id: record)

    recovered = TestClient(create_app()).get(
        "/api/chat/responses/orphans"
    ).json()["responses"][0]

    assert recovered["ok"] is False
    assert recovered["delivery"]["failed"] is True


def test_poll_returns_queued_artifact_metadata(tmp_path, monkeypatch):
    import hud.server as server

    image_root = tmp_path / "images"
    document_root = tmp_path / "documents"
    image_path = image_root / "2026-07-26" / "img-1.png"
    document_path = document_root / "2026-07-26" / "doc-1.md"
    monkeypatch.setattr(server, "_IMAGE_ROOT", image_root)
    monkeypatch.setattr(server, "_DOCUMENT_ROOT", document_root)
    monkeypatch.setattr(
        ocr,
        "get_response",
        lambda _cid: {
            "cycle_id": "cyc-image",
            "episode_id": "ep-image",
            "status": "pending",
            "text": "",
            "image": {"artifact_id": "img-1", "path": str(image_path)},
            "document": {"artifact_id": "doc-1", "path": str(document_path)},
        },
    )
    monkeypatch.setattr(ocr, "mark_delivered", lambda _cid: None)

    client = TestClient(create_app())
    body = client.get("/api/chat/responses", params={"cycle_id": "cyc-image"}).json()

    assert body["found"] is True
    assert body["message"] == ""
    assert body["image"]["artifact_id"] == "img-1"
    assert body["image"]["url"] == "/api/images/2026-07-26/img-1.png"
    assert body["document"]["url"] == "/api/documents/2026-07-26/doc-1.md"


def test_poll_forwards_persisted_plugin_permission_hud_dict(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    hud = {
        "request_id": "a" * 48,
        "revision": "b" * 64,
        "display": {
            "plugin_apps": [
                {
                    "plugin_id": "google.workspace.drive",
                    "plugin_name": "Workspace Drive",
                    "app_id": "google.workspace.drive.mcp",
                    "app_name": "Workspace Drive Mcp",
                }
            ],
            "effect_summary": "Create data",
            "ordered_operations": ["Create file"],
            "audience": "{}",
            "destination": "{}",
            "crosses_providers": False,
            "field_names": ["name"],
            "risks": ["write"],
        },
    }
    stored = ocr.append_response(
        cycle_id="cyc-perm",
        episode_id="ep-perm",
        text="Permission required",
        plugin_permission_request={
            **hud,
            "access_token": "ya29.secret",
            "plan": {"arguments": {"body": "secret-argument-value"}},
        },
    )
    assert stored is not None

    client = TestClient(create_app())
    body = client.get("/api/chat/responses", params={"cycle_id": "cyc-perm"}).json()

    assert body["found"] is True
    assert body["plugin_permission_request"] == hud
    serialized = str(body).lower()
    assert "ya29.secret" not in serialized
    assert "secret-argument-value" not in serialized


def test_poll_unknown_cycle_returns_found_false_not_500(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    client = TestClient(create_app())
    r = client.get("/api/chat/responses", params={"cycle_id": "nope"})
    assert r.status_code == 200
    assert r.json()["found"] is False


def test_poll_blank_cycle_returns_found_false(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    client = TestClient(create_app())
    r = client.get("/api/chat/responses")
    assert r.status_code == 200
    assert r.json()["found"] is False
