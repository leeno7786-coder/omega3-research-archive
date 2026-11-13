"""Chat-attachment who-is-this (vision spec §3, live-E2E gap 2026-07-18).

Pins: intent gating (no who-is-this text → cognition), strict upload-ref
containment (traversal refuses), non-image fall-through, and the composed
deterministic answer (recognized / unknown-face / no-face) with persona
facts — all with the face runtime injected."""

from __future__ import annotations

import hud.server as hs
from pressure_loop.operator_vision import WHO_IS_THIS_RE


def test_who_is_this_regex_shapes():
    # The pattern moved out of hud.server when the who-is-this flow was pulled
    # into pressure_loop.operator_vision (hud/server.py:346 now delegates to
    # answer_who_is_this_from_upload). The test kept asserting on the old
    # private name and had been failing on AttributeError ever since, which is
    # why the intent gating below was effectively unguarded.
    assert WHO_IS_THIS_RE.search("Who is this?")
    assert WHO_IS_THIS_RE.search("who's that")
    assert WHO_IS_THIS_RE.search("who is in the photo")
    assert not WHO_IS_THIS_RE.search("remember this person")
    assert not WHO_IS_THIS_RE.search("what is this")


def test_upload_ref_containment(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "_UPLOAD_ROOT", tmp_path)
    (tmp_path / "image").mkdir()
    (tmp_path / "image" / "shot.jpg").write_bytes(b"jpg")
    assert hs._resolve_upload_ref("/api/uploads/image/shot.jpg") is not None
    assert hs._resolve_upload_ref("image/shot.jpg") is not None
    assert hs._resolve_upload_ref("shot.jpg") is not None
    assert hs._resolve_upload_ref("../../guardian/state/vault.key") is None
    assert hs._resolve_upload_ref("..\\..\\secrets.txt") is None
    assert hs._resolve_upload_ref("") is None


def test_uploaded_image_runs_the_visual_memory_ingest_pipeline(tmp_path, monkeypatch):
    import pressure_loop.operator_vision as operator_vision

    monkeypatch.setattr(operator_vision, "UPLOAD_ROOT", tmp_path)
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    image_path = image_dir / "scene.jpg"
    image_path.write_bytes(b"jpeg-bytes")
    captured = {}

    def _ingest(image_bytes, **kwargs):
        captured["image_bytes"] = image_bytes
        captured.update(kwargs)
        return {"ok": True, "outcome": "ingested", "cycle_id": "vision-desc-test"}

    monkeypatch.setattr("sensory.vision_ingest.ingest_image_description", _ingest)

    result = operator_vision.ingest_uploaded_image(
        "image/scene.jpg",
        cycle_id="chat-cycle-1",
        upload_root=tmp_path,
    )

    assert result == {"ok": True, "outcome": "ingested", "cycle_id": "vision-desc-test"}
    assert captured == {
        "image_bytes": b"jpeg-bytes",
        "origin": "upload",
        "image_path": "image/scene.jpg",
        "episode_id": "chat-cycle-1",
    }


def test_non_matching_text_falls_through(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "_UPLOAD_ROOT", tmp_path)
    assert hs._answer_who_is_this_from_upload("image/x.jpg", "summarize this") is None


def _arm(tmp_path, monkeypatch, identify_result, facts=()):
    monkeypatch.setattr(hs, "_UPLOAD_ROOT", tmp_path)
    (tmp_path / "image").mkdir(exist_ok=True)
    (tmp_path / "image" / "shot.jpg").write_bytes(b"jpg-bytes")
    monkeypatch.setattr("runtime.face.recognition.identify", lambda _b: identify_result)
    monkeypatch.setattr(
        "core.persona_router.recall_for_person",
        lambda _n: [{"payload": {"text": f}} for f in facts],
    )
    monkeypatch.setattr("memory.hot_buffer.append_turn", lambda *a, **k: None)


def test_recognized_face_answers_with_persona_facts(tmp_path, monkeypatch):
    _arm(
        tmp_path,
        monkeypatch,
        {"recognized_name": "Noah Lee", "confidence": 0.72, "face_count": 1},
        facts=("The creator.", "First encounter with Noah Lee."),
    )
    out = hs._answer_who_is_this_from_upload("image/shot.jpg", "Who is this?")
    assert out is not None and out["ok"]
    assert out["recognized_name"] == "Noah Lee"
    assert "That's Noah Lee (72% match)." in out["message"]
    assert "The creator." in out["message"]
    assert out["route"]["action"] == "VISION_IDENTIFY"


def test_unknown_face_offers_enrollment(tmp_path, monkeypatch):
    _arm(tmp_path, monkeypatch, {"face_count": 1, "embedding": []})
    out = hs._answer_who_is_this_from_upload("image/shot.jpg", "who is this")
    assert out is not None
    assert "don't recognize this face yet" in out["message"]


def test_no_face_is_honest(tmp_path, monkeypatch):
    _arm(tmp_path, monkeypatch, {"face_count": 0})
    out = hs._answer_who_is_this_from_upload("image/shot.jpg", "who is this?")
    assert out is not None
    assert out["message"] == "I don't see a face in that image."
