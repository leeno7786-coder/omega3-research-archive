"""Visual intake stays durable until its own pressure-opened AUTO action."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from memory import episode_sram
from memory.encoding import encode_committed_memory as _real_encode_committed_memory
from pressure_loop import cognition_queue


@pytest.fixture(autouse=True)
def clean_buckets(tmp_path, monkeypatch):
    from runtime.face import anchor_store

    isolated_store = anchor_store.AnchorStore(tmp_path / "face_anchors.json")
    monkeypatch.setattr(anchor_store, "_singleton", isolated_store)
    episode_sram.reset()
    yield
    episode_sram.reset()


@pytest.mark.parametrize("paused", [False, True])
def test_chat_queues_all_visual_refs_without_pre_admission_work(monkeypatch, paused):
    from pressure_loop.operator_chat import handle_operator_chat

    calls = []
    monkeypatch.setattr("pressure_loop.operator_vision.ingest_uploaded_image",
                        lambda *a, **k: calls.append(a) or {"ok": False})
    monkeypatch.setattr("pressure_loop.operator_vision.answer_who_is_this_from_upload",
                        lambda *a, **k: calls.append(a))
    monkeypatch.setattr("pressure_loop.pressure_loop_pause.is_pressure_loop_paused",
                        lambda: paused)
    episode_sram.open_bucket("other-browser", pipeline_id=1)
    result = handle_operator_chat("Describe these pictures", upload_refs=["image/a.png", "image/b.jpg"])
    assert result["queued"] is True
    assert calls == [], "image work must wait for this turn's pressure-owned AUTO"
    cognition_queue._reset_cache_for_tests()
    queued = cognition_queue.lease_next_input(0)
    assert queued.metadata["visual_intake"]["upload_refs"] == ["image/a.png", "image/b.jpg"]
    assert queued.metadata["cycle_id"] == result["cycle_id"]
    assert episode_sram.pending("other-browser") == []


def test_face_http_queues_complete_payload_without_memory_or_anchor_writes(monkeypatch):
    from fastapi.testclient import TestClient
    from hud.server import create_app

    episode_sram.open_bucket("other-agent", pipeline_id=2)
    response = TestClient(create_app()).post("/api/vision/remember-face", json={
        "name": "Synthetic Person", "context": "test face",
        "embedding": [0.1] * 512, "bbox": [0, 0, 10, 10],
        "image_b64": base64.b64encode(b"image fixture").decode(),
    })
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["queued"] and body["cycle_id"]
    cognition_queue._reset_cache_for_tests()
    queued = cognition_queue.lease_next_input(0)
    face = queued.metadata["visual_intake"]["face"]
    assert face["image_b64"] and face["embedding"] == [0.1] * 512
    assert face["name"] == "Synthetic Person"
    assert queued.metadata["source_citation"]["source_id"]
    assert episode_sram.pending("other-agent") == []


def _execute_visual(payload, monkeypatch, tmp_path, *, text="Remember this face"):
    from auto_loop._auto_state import MemoryEnvelope
    from auto_loop.promptpack.run import build_prompt_pack
    from auto_loop.execute.run import execute
    from memory.source_ledger.store import record_operator_chat_source

    memory = MemoryEnvelope(False, "skipped", None, (), (), None)
    pack = {"action": "RESPONSE", "execution_inputs": {}, "source_action_goal": {}}
    pressure = {"contributors": [{"id": "operator_response", "detail": {
        "message": text, "visual_intake": payload,
        "cycle_id": "visual-turn", "source_citation": record_operator_chat_source(
            text, cycle_id="visual-turn"),
    }}]}
    prompt = build_prompt_pack(pack, memory, pressure=pressure)
    assert prompt.context_blocks.get("visual_intake") == payload
    episode_sram.open_bucket("visual-episode", pipeline_id=0)
    monkeypatch.setattr("core.persona_router.route_persona", lambda name: (1, 0, "synthetic-person"))
    monkeypatch.setattr("core.persona_router.add_memory_for_person", lambda *a, **k: "1.00.synthetic")
    return execute(prompt, "TEXT_FAST", pack, memory, "visual-episode",
                   opened_by="operator_response", chat_cycle_id="visual-turn", pipeline_id=0)


def test_face_auto_intake_survives_real_canonical_roundtrip(tmp_path, monkeypatch):
    from memory.engine import walker
    from core.face_recall_warmup import warm_anchor_store

    face = {"name": "Synthetic Person", "context": "test face", "embedding": [0.1] * 512,
            "bbox": [0, 0, 10, 10], "image_b64": base64.b64encode(b"fixture").decode()}
    monkeypatch.setattr("sensory.vision_ingest._default_describe", lambda raw: "A person in a room.")
    outcome = _execute_visual({"face": face}, monkeypatch, tmp_path)
    assert outcome["failure_kind"] is None, outcome
    assert "Captured" in outcome["outputs"]["response_text"]
    held = [i.envelope for i in episode_sram.pending("visual-episode") if i.kind == "memory"]
    envelope = next(e for e in held if e.type == "face_memory")
    assert envelope.id and envelope.id.startswith("7.01.face_")
    result_envelope = next(e for e in held if e.source == "operator_vision.auto" and e.type == "episode")
    assert result_envelope.id and result_envelope.id.startswith("6.00.visual_")
    assert envelope.payload["enrichment"]["face_memory"]["embedding"] == face["embedding"]
    assert not [p for p in walker.MEMORY_ROOT.rglob("*.json") if "face_memory" in p.parts]
    episode_sram.close_bucket("visual-episode")
    walker.load_coord_index()
    record = next(walker.load_meta_by_path(walker.resolve_coord_index_path(v["path"]))
                  for v in walker.COORD_INDEX.values() if v.get("type") == "face_memory")
    assert record["enrichment"]["face_memory"]["image_b64"] == face["image_b64"]
    assert record["enrichment"]["face_memory"]["embedding"] == face["embedding"]
    assert not list(walker.MEMORY_ROOT.rglob("*.embedding.json"))
    from runtime.face.anchor_store import AnchorStore

    store = AnchorStore(tmp_path / "recovered-face-anchors.json")
    result = warm_anchor_store(store, root=walker.MEMORY_ROOT.parent)
    assert result["restored"] == 1
    assert store.names() == ["Synthetic Person"]
    assert store.identify([0.1] * 512)["recognized_name"] == "Synthetic Person"


def test_visual_failure_is_an_auto_outcome_with_sram_cause(tmp_path, monkeypatch):
    import cognition_chain
    from pressure_loop import operator_chat_responses

    def broken(*args, **kwargs):
        raise OSError("synthetic visual disk failure")

    monkeypatch.setattr("pressure_loop.operator_vision.ingest_uploaded_image", broken)
    outcome = _execute_visual({"upload_refs": ["image/a.png"]}, monkeypatch, tmp_path)
    assert outcome["failure_kind"] == "visual_intake_failed"
    assert "synthetic visual disk failure" in outcome["failure_reason"]
    assert outcome["outputs"]["visual_intake"]["cycle_id"] == "visual-turn"
    assert any("synthetic visual disk failure" in json.dumps(i.envelope.payload)
               for i in episode_sram.pending("visual-episode") if i.kind == "memory")
    monkeypatch.setattr(operator_chat_responses, "_store_path", lambda: tmp_path / "responses.json")
    operator_chat_responses._reset_cache_for_tests()
    episode = SimpleNamespace(
        episode_id="visual-episode", opened_by="operator_response", pipeline_id=0,
        global_={"intent_decision": {"intent": "RESPOND"}},
        meta={"execution_action_pack": {"action": "RESPONSE"}}, auto=outcome,
        pressure={"contributors": [{"id": "operator_response", "detail": {
            "cycle_id": "visual-turn", "chat_bubble_waiting": True}}]},
    )
    cognition_chain._deliver_chat_reply_at_auto(episode)
    delivered = operator_chat_responses.get_response("visual-turn")
    assert delivered is not None
    assert "synthetic visual disk failure" in delivered["text"]
    assert operator_chat_responses.get_response("visual-episode") is None


@pytest.mark.parametrize("failure", ["identify_raised", "models_unavailable", "persona_unavailable"])
def test_identity_lookup_failure_survives_auto_and_episode(tmp_path, monkeypatch, failure):
    image = tmp_path / "face.png"
    image.write_bytes(b"image fixture")
    monkeypatch.setattr("pressure_loop.operator_vision.resolve_upload_ref", lambda *a, **kw: image)
    monkeypatch.setattr("pressure_loop.operator_vision.ingest_uploaded_image",
                        lambda *a, **kw: {"ok": True, "outcome": "described"})

    def fail(*args, **kwargs):
        raise OSError(failure)

    identify = (fail if failure == "identify_raised" else
                lambda raw: {"ok": False, "degraded": True, "reason": failure}
                if failure == "models_unavailable" else
                {"ok": True, "recognized_name": "Synthetic", "confidence": 0.9})
    monkeypatch.setattr("runtime.face.recognition.identify", identify)
    monkeypatch.setattr("core.persona_router.recall_for_person", fail)
    outcome = _execute_visual({"upload_refs": ["image/face.png"]}, monkeypatch, tmp_path,
                              text="Who is this?")
    assert outcome["failure_kind"] == "visual_intake_failed", outcome
    assert failure in outcome["failure_reason"]
    assert any(failure in json.dumps(item.envelope.payload)
               for item in episode_sram.pending("visual-episode") if item.kind == "memory")


def test_hud_identity_adapter_preserves_explicit_failure(monkeypatch):
    from hud.server import _answer_who_is_this_from_upload

    failure = {"ok": False, "outcome": "face_identification_failed", "error": "model unavailable"}
    monkeypatch.setattr("pressure_loop.operator_vision.answer_who_is_this_from_upload",
                        lambda *a, **kw: failure)
    assert _answer_who_is_this_from_upload("image/face.png", "Who is this?") == failure


def test_anchor_projection_replays_canonical_records_once(tmp_path):
    from runtime.face.anchor_store import AnchorStore

    path = tmp_path / "anchors.json"
    store = AnchorStore(path)
    first = ("2026-01-01", "7.01.first", "Synthetic Person", [0.1] * 512)
    second = ("2026-01-02", "7.01.second", "Synthetic Person", [0.2] * 512)
    store.project_canonical([first])
    assert AnchorStore(path).project_canonical([first]) == {"restored": 0, "skipped": 1}
    assert AnchorStore(path).project_canonical([first, second]) == {"restored": 1, "skipped": 1}
    assert json.loads(path.read_text())["Synthetic Person"]["sample_count"] == 2


def test_anchor_projection_persist_failure_does_not_poison_retry(tmp_path, monkeypatch):
    from runtime.face.anchor_store import AnchorStore

    store = AnchorStore(tmp_path / "anchors.json")
    real_persist = store._persist
    monkeypatch.setattr(store, "_persist", lambda: (_ for _ in ()).throw(OSError("cache disk failed")))
    sample = ("2026-01-01", "7.01.first", "Synthetic Person", [0.1] * 512)
    with pytest.raises(OSError, match="cache disk failed"):
        store.project_canonical([sample])
    monkeypatch.setattr(store, "_persist", real_persist)
    assert store.project_canonical([sample]) == {"restored": 1, "skipped": 0}


def test_incremental_face_projection_matches_cold_rebuild(tmp_path):
    from core.face_recall_warmup import warm_anchor_store
    from runtime.face.anchor_store import AnchorStore

    directory = tmp_path / "memory" / "700_vision" / "face_embeddings" / "face_memory"
    directory.mkdir(parents=True)

    def write_sample(number, vector):
        (directory / f"sample-{number}.json").write_text(json.dumps({
            "id": f"7.01.sample-{number}", "type": "face_memory",
            "timestamp_created": f"2026-01-0{number}T00:00:00Z",
            "enrichment": {"face_memory": {"name": "Synthetic Person", "embedding": vector}},
        }), encoding="utf-8")

    store = AnchorStore(tmp_path / "incremental.json")
    write_sample(1, [0.1] * 512)
    warm_anchor_store(store, root=tmp_path)
    write_sample(2, [0.2, 0.3] * 256)
    assert warm_anchor_store(store, root=tmp_path)["restored"] == 1
    assert warm_anchor_store(store, root=tmp_path)["restored"] == 0
    cold = AnchorStore(tmp_path / "cold.json")
    assert warm_anchor_store(cold, root=tmp_path)["restored"] == 2
    incremental = json.loads((tmp_path / "incremental.json").read_text())["Synthetic Person"]
    rebuilt = json.loads((tmp_path / "cold.json").read_text())["Synthetic Person"]
    assert incremental["sample_count"] == rebuilt["sample_count"] == 2
    assert incremental["embedding"] == rebuilt["embedding"]


def test_out_of_order_face_projection_matches_cold_rebuild(tmp_path):
    from core.face_recall_warmup import warm_anchor_store
    from runtime.face.anchor_store import AnchorStore

    directory = tmp_path / "memory" / "700_vision" / "face_embeddings" / "face_memory"
    directory.mkdir(parents=True)

    def write_sample(number):
        vector = [0.0] * 512
        vector[number] = 1.0
        (directory / f"sample-{number}.json").write_text(json.dumps({
            "id": f"7.01.sample-{number}", "type": "face_memory",
            "timestamp_created": f"2026-01-0{number}T00:00:00Z",
            "enrichment": {"face_memory": {"name": "Synthetic Person", "embedding": vector}},
        }), encoding="utf-8")

    store = AnchorStore(tmp_path / "incremental.json")
    for number in range(2, 9):
        write_sample(number)
    warm_anchor_store(store, root=tmp_path)
    write_sample(1)  # An older capture arrives after seven newer canonical samples.
    assert warm_anchor_store(store, root=tmp_path)["restored"] == 1
    cold = AnchorStore(tmp_path / "cold.json")
    warm_anchor_store(cold, root=tmp_path)
    incremental = json.loads((tmp_path / "incremental.json").read_text())["Synthetic Person"]
    rebuilt = json.loads((tmp_path / "cold.json").read_text())["Synthetic Person"]
    assert incremental["canonical_ids"] == rebuilt["canonical_ids"]
    assert incremental["embedding"] == rebuilt["embedding"]


def test_face_panels_show_queued_and_follow_the_existing_response_rail():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for relative in ("hud/ui-src/core/omega-ui.js", "hud/ui-src/panels/settings-wire.js"):
        source = (root / relative).read_text(encoding="utf-8")
        start = source.index("const r = await fetch('/api/vision/upload-face'")
        handler = source[start:source.index("} catch", start)]
        assert "renderChatResult(result, null)" in handler
        assert "face memory queued" in handler
        assert "face memory saved" not in handler


@pytest.mark.parametrize("lost_ack", [False, True])
def test_face_drain_projects_after_write_and_on_lost_ack_retry(tmp_path, monkeypatch, lost_ack):
    from memory import intake_journal
    from memory.api import intake_memory
    from pressure_loop.operator_vision import build_face_memory_envelope

    # Exercise the real immutable claim receipt, not core's model-free stub.
    monkeypatch.setattr("memory.encoding.encode_committed_memory", _real_encode_committed_memory)
    episode_sram.open_bucket("face-projection", pipeline_id=0)
    envelope = build_face_memory_envelope(name="Synthetic Person", persona_coord=(1, 1, "synthetic"),
                                         embedding=[0.1] * 512, image_b64="Zml4dHVyZQ==")
    intake_memory(envelope, propose=False, bypass_guardian=True, skip_embed=True)
    calls = []
    monkeypatch.setattr("core.face_recall_warmup.warm_anchor_store", lambda: calls.append("project"))
    real_mark = intake_journal.mark_committed
    if lost_ack:
        def fail_ack(*args, **kwargs):
            raise OSError("lost face acknowledgement")

        monkeypatch.setattr(intake_journal, "mark_committed", fail_ack)
        with pytest.raises(OSError, match="lost face acknowledgement"):
            episode_sram.close_bucket("face-projection")
        monkeypatch.setattr(intake_journal, "mark_committed", real_mark)
    episode_sram.close_bucket("face-projection")
    assert calls == ["project"] * (2 if lost_ack else 1)


def test_face_projection_failure_preserves_pending_durable_intake(monkeypatch):
    from memory import intake_journal
    from memory.api import intake_memory
    from pressure_loop.operator_vision import build_face_memory_envelope

    episode_sram.open_bucket("face-projection-failure", pipeline_id=0)
    envelope = build_face_memory_envelope(name="Synthetic Person", persona_coord=(1, 1, "synthetic"),
                                         embedding=[0.1] * 512, image_b64="Zml4dHVyZQ==")
    receipt = intake_memory(envelope, propose=False, bypass_guardian=True, skip_embed=True)

    def fail_projection():
        raise OSError("synthetic anchor cache disk failure")

    monkeypatch.setattr("core.face_recall_warmup.warm_anchor_store", fail_projection)
    with pytest.raises(OSError, match="synthetic anchor cache disk failure"):
        episode_sram.close_bucket("face-projection-failure")
    assert episode_sram.pending("face-projection-failure")
    assert intake_journal.record_by_id(receipt.journal_record_id).status == intake_journal.PENDING


def test_runtime_face_enrollment_queues_without_learning_before_deepsleep(monkeypatch):
    from runtime.face import recognition, anchor_store

    monkeypatch.setattr(recognition, "_identify_impl", lambda raw: {
        "ok": True, "embedding": [0.1] * 512, "bbox": [0, 0, 10, 10]})
    result = recognition.enroll(b"fixture-image", "Synthetic")
    assert result.get("queued") is True
    assert result["cycle_id"]
    assert anchor_store.get_anchor_store().names() == []
    queued = cognition_queue.lease_next_input(0)
    assert queued.metadata["visual_intake"]["face"]["name"] == "Synthetic"


def test_compute_only_face_engine_has_no_direct_enrollment_entry():
    from runtime.face.recognizer import FaceRecognizer

    assert not hasattr(FaceRecognizer, "enroll")


def test_http_enroll_is_the_same_correlated_queue_path(monkeypatch):
    from fastapi.testclient import TestClient
    from hud.server import create_app
    from runtime.face import recognition, anchor_store

    monkeypatch.setattr("hud.server._require_face_runtime", lambda **kw: None)
    monkeypatch.setattr(recognition, "_identify_impl", lambda raw: {
        "ok": True, "embedding": [0.1] * 512, "bbox": [0, 0, 10, 10]})
    response = TestClient(create_app()).post("/api/vision/enroll", json={
        "name": "Synthetic", "image_b64": base64.b64encode(b"fixture").decode()})
    assert response.status_code == 202, response.text
    assert response.json()["queued"]
    assert anchor_store.get_anchor_store().names() == []


def test_presence_enrollment_is_queued_not_a_direct_cache_write(monkeypatch):
    from fastapi.testclient import TestClient
    from hud import presence_test
    from runtime.face import anchor_store

    monkeypatch.setattr(presence_test, "identify", lambda raw: {
        "ok": True, "embedding": [0.1] * 512, "bbox": [0, 0, 10, 10]})
    response = TestClient(presence_test.app).post("/api/presence/enroll", json={
        "name": "Synthetic", "image_b64": base64.b64encode(b"fixture").decode()})
    assert response.status_code == 202, response.text
    assert response.json()["queued"] and response.json()["good"]
    assert anchor_store.get_anchor_store().names() == []


def test_thumbnail_reads_canonical_capture_without_creating_a_file(tmp_path):
    from core.paths import MEMORY_ROOT
    from runtime.face import recognition

    directory = MEMORY_ROOT / "700_vision" / "face_embeddings" / "face_memory"
    directory.mkdir(parents=True)
    (directory / "capture.json").write_text(json.dumps({
        "id": "7.01.synthetic", "type": "face_memory", "timestamp_created": "2026-01-01T00:00:00Z",
        "enrichment": {"face_memory": {"name": "Synthetic", "image_b64": "Zml4dHVyZQ=="}},
    }), encoding="utf-8")
    before = sorted(str(path) for path in MEMORY_ROOT.rglob("*"))
    assert recognition.anchor_thumbnail_bytes("Synthetic") == b"fixture"
    assert sorted(str(path) for path in MEMORY_ROOT.rglob("*")) == before
