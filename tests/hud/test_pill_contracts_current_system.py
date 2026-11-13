"""HUD pill contract tests — current system alignment.

Validates that `panel_status()` and `telemetry_snapshot()` expose the
contracted keys and shapes the HUD frontend depends on.
"""

from types import SimpleNamespace

import pytest

from hud import telemetry


# ─────────────────────────────────────────────────────────────────────
# /api/status contract (status glyph row)
# ─────────────────────────────────────────────────────────────────────

PILL_IDS = (
    "voice",
    "identity",
    "memory",
    "l0",
    "presence",
    "pressure_loop",
    "embed",
    "classify",
    "face",
    "coding_model",
)


def test_panel_status_has_all_pills():
    status = telemetry.panel_status()
    for pill_id in PILL_IDS:
        assert pill_id in status, f"missing pill: {pill_id}"


def test_panel_status_pill_shape():
    status = telemetry.panel_status()
    for pill_id in PILL_IDS:
        cell = status[pill_id]
        assert set(("state", "label", "detail")).issubset(cell.keys())
        assert cell["state"] in ("live", "loading", "degraded", "down")


def test_memory_pill_degrades_when_encoding_journal_is_malformed(monkeypatch):
    import memory.encoding_journal as journal
    import memory.strength_index as strength_index

    monkeypatch.setattr(
        telemetry,
        "_probe_retina",
        lambda: {"state": "live", "label": "ettin", "detail": "resident · 1 codes"},
    )
    monkeypatch.setattr(
        strength_index,
        "ensure_strength_index",
        lambda: SimpleNamespace(_records={"memory-1": {}}),
    )
    monkeypatch.setattr(journal, "find_unclosed_entries", lambda: [])
    monkeypatch.setattr(
        journal,
        "journal_status",
        lambda: {
            "clean": False,
            "unclosed": [],
            "unclosed_count": 0,
            "malformed_count": 1,
            "malformed_lines": [2878],
        },
    )

    pill = telemetry._probe_memory(
        {"embed": SimpleNamespace(state=SimpleNamespace(value="available"), reason="")}
    )

    assert pill["state"] == "degraded"
    assert "MALFORMED" in pill["detail"]
    assert "2878" in pill["detail"]


def test_semantic_core_and_embed_pills_report_resident_ettin(monkeypatch):
    import memory.retina as retina

    monkeypatch.setattr(
        retina,
        "stats",
        lambda: {"encoder_resident": True, "loaded": True, "codes": 42},
    )

    embed = telemetry._probe_retina()
    semantic_core = telemetry._probe_l0()

    assert embed == {"state": "live", "label": "ettin", "detail": "resident · 42 codes"}
    assert semantic_core["state"] == "live"
    assert "Ettin" in semantic_core["detail"]


def test_memory_pill_stays_loading_until_ettin_codes_are_mapped(monkeypatch):
    import memory.encoding_journal as journal
    import memory.strength_index as strength_index

    monkeypatch.setattr(
        telemetry,
        "_probe_retina",
        lambda: {
            "state": "loading",
            "label": "ettin",
            "detail": "resident · projection codes pending",
        },
    )
    monkeypatch.setattr(
        strength_index,
        "ensure_strength_index",
        lambda: SimpleNamespace(_records={"memory-1": {}}),
    )
    monkeypatch.setattr(
        journal,
        "journal_status",
        lambda: {
            "clean": True,
            "unclosed_count": 0,
            "malformed_count": 0,
        },
    )

    pill = telemetry._probe_memory({})

    assert pill["state"] == "loading"
    assert "projection codes pending" in pill["detail"]


# ─────────────────────────────────────────────────────────────────────
# /api/telemetry/snapshot contract — subtree keys
# ─────────────────────────────────────────────────────────────────────


def test_telemetry_snapshot_has_all_subtrees():
    # Only assert top-level keys; per-subtree shapes tested below.
    snap = telemetry.telemetry_snapshot()
    for key in (
        "cycle",
        "memory",
        "omega",
        "meta",
        "global",
        "autoloop",
        "deepsleep",
        "identity",
        "system",
        "layers",
        "brain",
    ):
        assert key in snap, f"missing telemetry subtree: {key}"


def test_telemetry_snapshot_panel_dashboards_present():
    snap = telemetry.telemetry_snapshot()
    for subtree in ("brain", "memory", "omega", "autoloop", "deepsleep", "identity"):
        block = snap.get(subtree) or {}
        dash = block.get("dashboard")
        assert isinstance(dash, dict), subtree
        assert "live" in dash, subtree


# ─────────────────────────────────────────────────────────────────────
# Omega subtree contract (reflection / trajectory / epochs)
# ─────────────────────────────────────────────────────────────────────


def test_telemetry_omega_has_current_system_keys():
    omega = telemetry._telemetry_omega()
    for key in ("epoch", "reflection", "traits", "contradiction"):
        assert key in omega, f"missing omega key: {key}"


def test_telemetry_omega_has_tool_synthesis_queue():
    omega = telemetry._telemetry_omega()
    ts = omega.get("tool_synthesis") or {}
    assert "queue" in ts, "missing omega.tool_synthesis.queue"
    assert isinstance(ts["queue"], list)


# ─────────────────────────────────────────────────────────────────────
# System / v2 badge contract
# ─────────────────────────────────────────────────────────────────────


def test_telemetry_system_has_v2():
    system = telemetry._telemetry_system()
    assert "v2" in system, "missing system.v2 subtree for v2 badges"
    v2 = system["v2"]
    for key in ("presence", "pressure_loop"):
        assert key in v2, f"missing system.v2 key: {key}"
    assert "state" in v2["presence"]
    assert "paused" in v2["pressure_loop"]
    assert "owners" in v2["pressure_loop"]


def test_panel_status_spa_substrate_maps_glyph_strings():
    pills = telemetry.panel_status()
    substrate = telemetry.panel_status_spa_substrate(pills)
    assert set(substrate.keys()) == set(telemetry._SPA_STATUS_PILL_KEYS)
    for value in substrate.values():
        assert isinstance(value, str)
        assert value
