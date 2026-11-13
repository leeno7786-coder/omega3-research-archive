from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from deepsleep_loop.identity_store import Trait, _trait_envelope
from pressure_loop.operator_vision import build_face_memory_envelope
from hud.settings import build_operator_profile_envelope
import memory.coding_project as coding_project
import memory.taxonomy.canonical_capture as canonical_capture
import pressure_loop.temporal_store as temporal_store

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


def _fake_receipt(path: str = "memory/test.json") -> SimpleNamespace:
    return SimpleNamespace(validation_status="committed", path=path, staging_ref=None)


def test_operator_profile_writer_targets_operator_persona_lobe():
    envelope, _ = build_operator_profile_envelope(
        {
            "name": "Noah",
            "preferred_name": "Lee",
            "role": "creator",
        }
    )

    assert envelope.coord == [1, 0, 0]
    assert envelope.payload["domain"] == "operator"
    assert envelope.payload["subtopic"] == "operator_profile"


def test_identity_trait_writer_targets_identity_traits_lobe():
    envelope = _trait_envelope(
        Trait(name="curious", strength=0.75, evidence_count=3),
        cycle_id="ep-trait",
    )

    assert envelope.coord == [0, 1, 0]
    assert envelope.payload["subtopic"] == "emergent_trait"


def test_face_memory_writer_targets_vision_lobe_face_memory():
    envelope = build_face_memory_envelope(
        name="Alice",
        persona_coord=(1, 1, "alice"),
        embedding=[0.1, 0.2, 0.3],
        context="operator",
        bbox=[1, 2, 3, 4],
        image_b64="data:image/png;base64,abc",
    )

    assert envelope.coord == [7, 1, 0]
    assert envelope.type == "face_memory"
    assert envelope.payload["subtopic"] == "face_memory"
    assert envelope.payload["enrichment"]["face_memory"]["person_id"] == "alice"


def test_temporal_writers_target_temporal_subdomains(monkeypatch):
    calls: list[tuple[str, list[int], str, str]] = []

    def _fake_write(env, *, propose=True, bypass_guardian=False, skip_embed=False):
        assert propose is False
        assert bypass_guardian is True
        calls.append((env.type, list(env.coord), str(env.payload.get("subtopic") or "")))
        return _fake_receipt()

    monkeypatch.setattr("memory.api.write_memory", _fake_write)

    temporal_store.write_episode_to_temporal(
        {
            "episode_id": "ep-temporal",
            "closed_at": "2026-06-29T00:00:00+00:00",
            "cascade_status": "completed",
        }
    )
    temporal_store.write_consolidation_to_temporal(
        {"episode_id": "ep-temporal", "event_kind": "deepsleep_summary"}
    )
    temporal_store.write_capability_to_temporal(
        {"episode_id": "ep-temporal", "event_kind": "capability_reinforced"}
    )
    temporal_store.write_trajectory({"target_domain": "python", "target_capability": "coding"})

    assert ("episode", [9, 1, 0], "episode") in calls
    assert ("consolidation", [9, 3, 0], "consolidation") in calls
    assert ("capability", [9, 4, 0], "capability") in calls
    assert ("trajectory", [9, 2, 0], "trajectory") in calls


def test_persona_writers_target_persona_memory(monkeypatch):
    from core import persona_router as pr

    calls: list[tuple[str, list[int], str]] = []

    def _fake_write(env, *, propose=True, bypass_guardian=False, **kwargs):
        assert propose is False
        assert bypass_guardian is True
        calls.append(
            (
                env.type,
                list(env.coord),
                str(env.payload.get("subtopic") or ""),
                str(env.payload.get("text") or ""),
            )
        )
        return _fake_receipt("1.01.persona")

    monkeypatch.setattr("memory.api.intake_memory", _fake_write)

    pr._write_profile_root("alice-smith", "Alice Smith")
    monkeypatch.setattr(pr, "route_persona", lambda _name: (1, 1, "alice-smith"))
    result = pr.add_memory_for_person(
        "Alice Smith",
        {"text": "Alice likes hiking"},
        type="persona_note",
        cycle_id="persona-cycle",
    )

    assert result == "1.01.persona"
    assert ("persona_name", [1, 1], "persona_name", "Alice Smith is a known person.") in calls
    assert ("persona_note", [1, 1], "persona_note", "Alice likes hiking") in calls


def test_canonical_capture_routes_temporal_and_situational_writers(monkeypatch):
    class _FakeEvent:
        def __init__(self, event_kind: str, event_id: str):
            self.event_kind = event_kind
            self.event_id = event_id
            self.episode_id = "ep-capture"
            self.source = "test.capture"
            self.payload = {"status": "success"}

        def to_dict(self):
            return {
                "event_kind": self.event_kind,
                "event_id": self.event_id,
                "episode_id": self.episode_id,
                "source": self.source,
                "payload": dict(self.payload),
            }

    situational: list[tuple[list[int], str]] = []
    temporal_episode: list[dict] = []
    temporal_consolidation: list[dict] = []
    temporal_capability: list[dict] = []

    def _fake_write(env, *, propose=True, bypass_guardian=False, skip_embed=False):
        assert propose is False
        assert bypass_guardian is True
        assert skip_embed is False
        situational.append((list(env.coord), str(env.payload.get("subtopic") or "")))
        return _fake_receipt()

    monkeypatch.setattr(
        canonical_capture.adapters,
        "events_from_episode",
        lambda _episode, _created_at: [
            _FakeEvent("tool_result", "evt-situational"),
            _FakeEvent("deepsleep_summary", "evt-consolidation"),
            _FakeEvent("capability_reinforced", "evt-capability"),
        ],
    )
    monkeypatch.setattr(
        canonical_capture,
        "write_episode_to_temporal",
        lambda episode: temporal_episode.append(dict(episode)),
    )
    monkeypatch.setattr(
        canonical_capture,
        "write_consolidation_to_temporal",
        lambda record: temporal_consolidation.append(dict(record)),
    )
    monkeypatch.setattr(
        canonical_capture,
        "write_capability_to_temporal",
        lambda record: temporal_capability.append(dict(record)),
    )
    monkeypatch.setattr("memory.api.write_memory", _fake_write)

    report = canonical_capture.capture_episode_to_canonical(
        {"episode_id": "ep-capture"},
        created_at="2026-06-29T00:00:00+00:00",
        cycle_id="ep-capture",
    )

    assert report["errors"] == []
    assert len(temporal_episode) == 1
    assert len(temporal_consolidation) == 1
    assert len(temporal_capability) == 1
    assert ([4, 6, 0], "tool_result") in situational


def test_coding_project_snapshot_artifacts_default_to_dev_lobe(monkeypatch, tmp_path):
    omega_root = tmp_path / "omega-root"
    project = tmp_path / "workspace" / "Demo Project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "main.py").write_text("print('hello')\n", encoding="utf-8")

    monkeypatch.setattr(coding_project, "OMEGA_ROOT", omega_root)

    manifest = coding_project.scan_project_tree(project, project_name="Demo Project")
    snapshot = coding_project.snapshot_project_tree(
        project,
        project_name="Demo Project",
        snapshot_id="snap-001",
    )
    manifest_env = coding_project.build_project_manifest_envelope(manifest, "cycle-demo")
    snapshot_env = coding_project.build_project_snapshot_envelope(snapshot, "cycle-demo")

    expected_root = omega_root / "memory" / "1100_dev" / "artifacts" / "demo-project" / "snap-001"
    assert Path(snapshot.snapshot_root) == expected_root
    assert Path(snapshot.tree_path) == expected_root / "tree" / "demo-project"
    assert manifest_env.coord[0] == 8
    assert snapshot_env.coord[0] == 8
