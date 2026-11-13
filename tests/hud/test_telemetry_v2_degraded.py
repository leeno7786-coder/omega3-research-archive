"""B0 — v2 telemetry adapters return clean degraded JSON, never raise.

Locks the HUD_PANELS_VISUAL_PACKET §5.1 contract: missing runtime /
capabilities degrade to shape-correct payloads (HTTP 200 + JSON), not
exceptions or 500s.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from guardian import terminal_feedback as tf
from hud import telemetry
from hud.server import create_app


@pytest.fixture
def hud_client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _patch_runtime_capability_snapshot(monkeypatch):
    """runtime_capability_snapshot can hang without a live broker; patch it
    for the telemetry panel tests that exercise memory / autoloop panels."""
    try:
        import runtime.runtime_capability_broker as broker
    except Exception:
        pass
    else:
        monkeypatch.setattr(broker, "runtime_capability_snapshot", lambda: {})
    # _probe_voice also probes runtime TTS/STT which can block in tests.
    try:
        import hud.telemetry as _tel
    except Exception:
        pass
    else:
        monkeypatch.setattr(
            _tel,
            "_probe_voice",
            lambda: {
                "state": "down",
                "label": "voice",
                "detail": "test-mocked",
            },
        )


def test_last_pressure_tick_snapshot_never_raises():
    result = telemetry.last_pressure_tick_snapshot()
    assert result is None or isinstance(result, dict)


def test_presence_state_fail_closed_200(hud_client: TestClient):
    r = hud_client.get("/api/presence/state")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] in ("PRESENT", "ABSENT", "UNKNOWN")
    assert "enrolled_operator_name" in body
    assert "watcher_stale" in body


def test_cuedesk_last_tick_fail_closed_200(hud_client: TestClient):
    r = hud_client.get("/api/cuedesk/last_tick")
    assert r.status_code == 200
    body = r.json()
    assert "tick" in body


def test_response_state_reports_current_event_driven_delivery_rails(
    hud_client: TestClient, monkeypatch
):
    import pressure_loop.operator_chat_responses as operator_responses
    import pressure_loop.proactive_chat_messages as proactive_responses

    monkeypatch.setattr(operator_responses, "list_pending", lambda: [{"cycle_id": "cyc-1"}])
    monkeypatch.setattr(
        proactive_responses,
        "list_pending",
        lambda: [{"message_id": "msg-1"}, {"message_id": "msg-2"}],
    )

    response = hud_client.get("/api/response/state")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "degraded": False,
        "rail": "event_driven",
        "gate": "ready",
        "pending_operator_responses": 1,
        "pending_proactive_responses": 2,
        "pending_total": 3,
    }


def test_pressure_tick_snapshot_carries_sanitized_cognition_visuals(monkeypatch):
    monkeypatch.setattr(telemetry, "_shared_state_read", lambda key, default=None: 14)
    monkeypatch.setattr(
        telemetry,
        "_audit_events",
        lambda limit=800, **_: [
            {
                "kind": "data",
                "loop": "pressure",
                "phase": "episode_ignition",
                "ts": "2026-06-30T17:00:00Z",
                "opened_by": "learning_debt",
                "aggregate_pressure": 0.82,
                "trajectory_snapshot": {
                    "target_domain": "memory_taxonomy",
                    "progress": 0.42,
                },
            }
        ],
    )

    import pressure_loop.episode_journal as episode_journal

    monkeypatch.setattr(
        episode_journal,
        "journal_window",
        lambda n=1: [SimpleNamespace(intent="USE_SKILL", action="USE_SKILL")],
    )

    snapshot = telemetry.last_pressure_tick_snapshot()
    assert snapshot is not None
    visuals = snapshot["cognition_visuals"]
    assert visuals["winner"] == {
        "label": "learning",
        "level": 0.82,
        "state": "active",
    }
    assert visuals["path"] == {
        "label": "active",
        "level": 0.42,
        "state": "active",
    }
    assert visuals["motion"] == {
        "label": "act",
        "state": "selected",
    }
    assert "USE_SKILL" not in json.dumps(visuals)
    assert "learning_debt" not in json.dumps(visuals)
    assert "memory_taxonomy" not in json.dumps(visuals)


def test_panel_adapters_never_raise():
    for fn_name in (
        "panel_brain",
        "panel_memory",
        "panel_omega",
        "panel_autoloop",
        "panel_deepsleep",
        "panel_identity",
    ):
        getattr(telemetry, fn_name)()


def test_panel_brain_cycle_wheel_shape():
    data = telemetry.panel_brain()
    for key in (
        "cycle",
        "layers",
        "cycle_layers",
        "cycle_active_index",
        "contributors",
        "suppress_reason",
        "conversation",
        "dashboard",
    ):
        assert key in data
    dash = data["dashboard"]
    for key in (
        "cognitive_load",
        "neural_series",
        "activity_rings",
        "throughput",
        "heatmap",
        "pressure_channels",
        "recent_phases",
    ):
        assert key in dash, f"missing dashboard key: {key}"


def test_telemetry_brain_snapshot_shape():
    snap = telemetry.telemetry_snapshot()
    brain = snap.get("brain") or {}
    for key in (
        "cycle_layers",
        "cycle_active_index",
        "contributors",
        "suppress_reason",
        "conversation",
    ):
        assert key in brain, f"missing brain key: {key}"


def test_telemetry_cognition_snapshot_shape():
    snap = telemetry.telemetry_snapshot()
    cognition = snap.get("cognition") or {}
    for key in ("cascade", "ignition", "tool"):
        assert key in cognition, f"missing cognition key: {key}"
    assert "cycle" in cognition["cascade"]
    assert "winner" in cognition["ignition"]
    assert "learned_link" in cognition["tool"]


def test_cognition_autonomy_contract_is_event_driven(monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "_active_episode_snapshot",
        lambda: {"id": "ep-live", "state": "active", "active": True},
        raising=False,
    )
    monkeypatch.setattr(
        telemetry,
        "_idle_learning_snapshot",
        lambda active: {
            "state": "held",
            "seconds_remaining": None,
            "label": "held while episode active",
            "enabled": True,
        },
        raising=False,
    )
    monkeypatch.setattr(
        telemetry,
        "_latest_cascade_phase",
        lambda: {
            "cycle": "meta_a",
            "loop": "meta",
            "phase": "action",
            "status": "enter",
            "episode_id": "ep-live",
        },
    )
    monkeypatch.setattr(telemetry, "last_pressure_tick_snapshot", lambda: {})
    monkeypatch.setattr(telemetry, "_latest_tool_provenance", lambda: {})

    data = telemetry._telemetry_cognition()

    assert data["episode"]["id"] == "ep-live"
    assert data["autonomy"]["state"] == "cascade_active"
    assert data["autonomy"]["time_driven"] is False
    assert data["idle_learning"]["seconds_remaining"] is None


def test_idle_learning_is_the_only_timed_eligibility(monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "_active_episode_snapshot",
        lambda: {"id": "", "state": "resting", "active": False},
        raising=False,
    )
    monkeypatch.setattr(
        telemetry,
        "_idle_learning_snapshot",
        lambda active: {
            "state": "resting",
            "seconds_remaining": 42,
            "label": "eligible in 00:42",
            "enabled": True,
        },
        raising=False,
    )
    monkeypatch.setattr(
        telemetry,
        "_latest_cascade_phase",
        lambda: {
            "cycle": "deepsleep",
            "loop": "deepsleep",
            "phase": "closure",
            "status": "ok",
            "episode_id": "ep-closed",
        },
    )
    monkeypatch.setattr(telemetry, "last_pressure_tick_snapshot", lambda: {})
    monkeypatch.setattr(telemetry, "_latest_tool_provenance", lambda: {})

    data = telemetry._telemetry_cognition()

    assert data["autonomy"]["state"] == "resting"
    assert data["idle_learning"]["seconds_remaining"] == 42
    assert "next_transition" not in data
    assert "eta" not in data["autonomy"]


def test_telemetry_deepsleep_snapshot_shape():
    snap = telemetry.telemetry_snapshot()
    ds = snap.get("deepsleep") or {}
    for key in ("phase_states", "dashboard", "cycle"):
        assert key in ds, f"missing deepsleep key: {key}"
    assert len(ds.get("phase_states") or []) == 4


def test_telemetry_identity_snapshot_shape():
    snap = telemetry.telemetry_snapshot()
    ident = snap.get("identity") or {}
    assert "dashboard" in ident
    dash = ident.get("dashboard") or {}
    for key in ("live", "confidence", "drift", "vector_count"):
        assert key in dash, f"missing identity dashboard key: {key}"


def test_panel_memory_recall_bloom_shape():
    data = telemetry.panel_memory()
    for key in ("recallTrace", "lobe_activity", "recent_writes", "recall_mode", "dashboard"):
        assert key in data
    dash = data.get("dashboard") or {}
    assert "live" in dash
    assert "write_series" in dash


def test_panel_omega_trajectory_shape():
    data = telemetry.panel_omega()
    for key in (
        "recent_epochs",
        "contradiction",
        "coherence",
        "traits",
        "tool_synthesis",
        "dashboard",
    ):
        assert key in data
    dash = data.get("dashboard") or {}
    for key in (
        "live",
        "coherence",
        "epoch_series",
        "epochs",
        "trait_bars",
        "tool_queue_bars",
        "gap_bars",
        "journal_bars",
        "trajectory_bars",
    ):
        assert key in dash, f"missing omega dashboard key: {key}"


def test_omega_live_snapshot_reads_outcome_pack_events():
    tf._reset_recent_events_ring_for_tests()
    try:
        tf.emit_data(
            "omega",
            "omega_outcome_pack",
            episode_id="win-42",
            trajectory_diff="FOLLOWING",
            trajectory_update={
                "target_domain": "attention",
                "target_capability": "focus",
                "confidence": 0.72,
            },
            pressure_feedback={
                "contributors_to_raise": [{"type": "curiosity", "weight": 0.4}],
                "contributors_to_lower": [{"type": "noise", "weight": 0.2}],
                "contributors_to_create": [],
            },
            coherence=0.72,
        )

        data = telemetry.panel_omega()
        epochs = data.get("recent_epochs") or []
        assert epochs
        assert epochs[-1].get("window_id") == "win-42"
        assert epochs[-1].get("direction") == "stable"

        dash = data.get("dashboard") or {}
        assert dash.get("live") is True
        assert dash.get("epoch_series")
        assert dash.get("epochs")
    finally:
        tf._reset_recent_events_ring_for_tests()


def test_panel_autoloop_respiratory_shape():
    data = telemetry.panel_autoloop()
    for key in ("autonomic", "suppress_histogram", "pause_state", "pressure", "dashboard"):
        assert key in data
    dash = data.get("dashboard") or {}
    assert "live" in dash
    assert "rings" in dash


def test_pressure_live_snapshot_reads_evaluate_agg():
    tf._reset_recent_events_ring_for_tests()
    try:
        tf.emit_phase(
            "pressure",
            "gather_contributors",
            status="ok",
            weights={"autonomous_drive": 0.42, "operator_message": 0.18},
        )
        tf.emit_phase(
            "pressure",
            "evaluate",
            status="ok",
            agg=0.55,
            dominant="autonomous_drive",
        )
        tf.emit_phase("pressure", "tick", status="ok", tick=7)

        snap = telemetry._pressure_live_snapshot()
        assert snap.get("pressure_aggregate") == 0.55
        assert snap.get("dominant") == "autonomous_drive"
        assert snap.get("weights", {}).get("autonomous_drive") == 0.42

        brain = telemetry.panel_brain()
        dash = brain.get("dashboard") or {}
        assert dash.get("pressure") == 0.55
        assert dash.get("neural_series")
    finally:
        tf._reset_recent_events_ring_for_tests()


def test_memory_recent_writes_reads_live_memory_data_events():
    tf._reset_recent_events_ring_for_tests()
    try:
        tf.emit_data(
            "memory",
            "write_memory",
            validation_status="committed",
            coord=[9, 2, 0],
            canonical_ref="mem-123",
            summary="status=committed",
        )

        data = telemetry.panel_memory()

        assert data["recent_writes"]
        assert data["recent_writes"][0]["outcome"] == "committed"
        assert data["lobe_activity"].get("9.2") == 1
    finally:
        tf._reset_recent_events_ring_for_tests()


def test_autoloop_respiratory_reads_live_neuromod_data_events(monkeypatch):
    tf._reset_recent_events_ring_for_tests()
    monkeypatch.setattr(telemetry, "_get_cached_scheduler", lambda: None)
    try:
        tf.emit_data(
            "autonomic",
            "neuromod_compute",
            dopamine=0.25,
            plasticity=0.5,
            strength=0.75,
            stress=0.1,
        )

        data = telemetry.panel_autoloop()

        assert data["autonomic"]["dopamine"][-1]["v"] == 0.25
        assert data["autonomic"]["plasticity"][-1]["v"] == 0.5
        assert data["autonomic"]["strength"][-1]["v"] == 0.75
        assert data["autonomic"]["stress"][-1]["v"] == 0.1
    finally:
        tf._reset_recent_events_ring_for_tests()


def test_panel_autoloop_refreshes_neuromod_when_snapshot_cache_warm(monkeypatch):
    """Ring-backed autonomic series must not lag the snapshot TTL."""
    tf._reset_recent_events_ring_for_tests()
    monkeypatch.setattr(telemetry, "_get_cached_scheduler", lambda: None)
    try:
        telemetry.telemetry_snapshot()
        assert not telemetry.panel_autoloop()["autonomic"]["dopamine"]

        tf.emit_data(
            "autonomic",
            "neuromod_compute",
            dopamine=0.42,
            plasticity=0.11,
            strength=0.22,
            stress=0.33,
        )

        data = telemetry.panel_autoloop()
        assert data["autonomic"]["dopamine"][-1]["v"] == 0.42
        assert data["autonomic"]["plasticity"][-1]["v"] == 0.11
    finally:
        tf._reset_recent_events_ring_for_tests()
        telemetry._SNAPSHOT_CACHE = None
        telemetry._SNAPSHOT_CACHE_TS = 0.0


def test_panel_deepsleep_consolidation_shape():
    data = telemetry.panel_deepsleep()
    for key in ("phase_states", "current_phase", "consolidation", "dashboard"):
        assert key in data


def test_panel_identity_dashboard_shape():
    data = telemetry.panel_identity()
    assert "dashboard" in data
    assert "confidence" in data["dashboard"]


def test_memory_lobes_expand_tree_shape(hud_client: TestClient):
    r = hud_client.get("/api/memory/lobes?expand=all")
    assert r.status_code == 200
    tree = r.json()
    assert tree.get("id") == "root"
    assert isinstance(tree.get("children"), list)
    if tree["children"]:
        lobe = tree["children"][0]
        assert "id" in lobe and "children" in lobe


def _walk_memory_files(node: dict):
    if node.get("node_kind") == "file":
        yield node
    for child in node.get("children") or []:
        yield from _walk_memory_files(child)


def _walk_memory_nodes(node: dict):
    yield node
    for child in node.get("children") or []:
        yield from _walk_memory_nodes(child)


def test_memory_read_roundtrip_from_expand_tree(hud_client: TestClient):
    """Settings memory graph opens leaf nodes via /api/memory/read."""
    r = hud_client.get("/api/memory/lobes?expand=all")
    assert r.status_code == 200
    tree = r.json()
    sample = next(_walk_memory_files(tree), None)
    if sample is None:
        pytest.skip("no memory files on disk for read roundtrip")
    rr = hud_client.get(
        "/api/memory/read",
        params={
            "lobe": sample["lobe"],
            "subdomain": sample["subdomain"],
            "subdomain_dir": sample.get("subdomain_dir") or "",
            "path": sample.get("read_path") or sample.get("path"),
        },
    )
    assert rr.status_code == 200
    assert rr.text


def test_memory_graph_tracks_actual_and_dynamic_subdomains(monkeypatch, tmp_path):
    """Settings graph reflects the actual memory store, not just Dewey static Y."""
    import hud.server as hud_server

    monkeypatch.setattr(hud_server, "OMEGA_ROOT", tmp_path)

    memory_file = tmp_path / "memory" / "200_knowledge" / "quantum" / "general" / "2.00.abc123.json"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text(
        """
{
  "id": "2.00.abc123",
  "coord": [2, 0, 0],
  "title": "quantum entanglement",
  "summary": "stored in a live dynamic folder"
}
""".strip(),
        encoding="utf-8",
    )

    idx = tmp_path / "guardian" / "state" / "dynamic_y_index.json"
    idx.parent.mkdir(parents=True)
    idx.write_text(
        """
{
  "schema_version": 1,
  "lobes": {
    "2": {
      "next_y": 6,
      "subdomains": {
        "robotics_controls": {
          "y": 5,
          "created_at": "2026-06-29T00:00:00+00:00",
          "parent": "robotics"
        }
      }
    }
  }
}
""".strip(),
        encoding="utf-8",
    )

    client = TestClient(hud_server.create_app())
    r = client.get("/api/memory/lobes?expand=all")
    assert r.status_code == 200
    tree = r.json()

    assert not any(
        node.get("node_kind") == "subdomain" and node.get("subdomain_dir") == "astronomy"
        for node in _walk_memory_nodes(tree)
    )

    quantum = next(
        node
        for node in _walk_memory_nodes(tree)
        if node.get("node_kind") == "subdomain" and node.get("subdomain_dir") == "quantum"
    )
    assert quantum["label"] == "quantum"
    assert quantum["count"] == 1

    nested_dir = next(
        node
        for node in _walk_memory_nodes(tree)
        if node.get("node_kind") == "directory"
        and node.get("subdomain_dir") == "quantum"
        and node.get("path") == "general"
    )
    assert nested_dir["label"] == "general"
    assert nested_dir["count"] == 1

    seeded = next(
        node
        for node in _walk_memory_nodes(tree)
        if node.get("node_kind") == "subdomain" and node.get("subdomain_dir") == "robotics_controls"
    )
    assert seeded["dynamic"] is True
    assert seeded["count"] == 0

    leaf = next(node for node in _walk_memory_files(tree) if node.get("memory_id") == "2.00.abc123")
    assert leaf["label"] == "quantum entanglement"
    assert leaf["subdomain_dir"] == "quantum"

    rr = client.get(
        "/api/memory/read",
        params={
            "lobe": leaf["lobe"],
            "subdomain": leaf["subdomain"],
            "subdomain_dir": leaf["subdomain_dir"],
            "path": leaf["read_path"],
        },
    )
    assert rr.status_code == 200
    assert "quantum entanglement" in rr.text
