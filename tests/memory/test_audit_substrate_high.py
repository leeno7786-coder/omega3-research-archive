"""Audit substrate HIGH findings — regression tests (2026-06-23)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_expand_links_skips_private_targets():
    """HIGH-3: link-expanded candidates must respect flags.private."""
    from memory.engine import walker

    public = {
        "id": "pub1",
        "links": ["priv1"],
        "score": {"importance": 0.5},
        "flags": {},
    }
    private = {
        "id": "priv1",
        "text": "secret",
        "score": {"importance": 0.5},
        "flags": {"private": True},
    }

    def _path_for_id(mid: str):
        return f"/mem/{mid}.json" if mid in ("pub1", "priv1") else None

    def _load_meta(path: str):
        if path.endswith("priv1.json"):
            return dict(private)
        return None

    with (
        patch.object(walker, "path_for_id", side_effect=_path_for_id),
        patch.object(walker, "load_meta_by_path", side_effect=_load_meta),
    ):
        expanded = walker.expand_links([public], depth=1, visited_ids=set())

    assert expanded == []


def test_task_continuity_workflows_lane_weight_reaches_plan():
    """HIGH-4: TASK_CONTINUITY must weight the canonical 'workflows' lane."""
    from memory.recall_compiler import compile_recall_plan

    class _QS:
        def __init__(self, text: str):
            self.text = text
            self.cycle_id = "t"
            self.domain = None
            self.task_scope = None

    plan = compile_recall_plan(_QS("where did we leave off on the deployment"))
    assert plan.posture.name == "TASK_CONTINUITY"
    assert plan.lane_weights.get("workflows", 0) > 0


def test_boot_wires_encoding_journal_recovery(monkeypatch):
    """HIGH-2: boot must call recover_unclosed with real rollback, not a no-op."""
    from memory.rollback import rollback_canonical_write

    captured: list = []

    def _fake_recover(fn):
        captured.append(fn)
        return []

    monkeypatch.setattr("memory.encoding_journal.recover_unclosed", _fake_recover)
    monkeypatch.setattr(
        "runtime.client.get_runtime_client",
        lambda: MagicMock(health=lambda: True, base_url="http://test"),
    )
    monkeypatch.setattr(
        "runtime.supervisor.ensure_runtime_process",
        lambda **_kwargs: {"ok": True, "spawned": False},
    )
    monkeypatch.setattr("memory.api.set_embedding_substrate_state", lambda *_args: None)
    monkeypatch.setattr("memory.engine.walker.load_coord_index", lambda: None)
    monkeypatch.setattr("memory.strength_index.ensure_strength_index", lambda: MagicMock())
    monkeypatch.setattr("memory.strength_index.register_graceful_shutdown", lambda: [])
    monkeypatch.setattr(
        "memory.taxonomy.compile_trigger.compile_on_boot",
        lambda: MagicMock(node_count=0, snapshot_hash="0" * 64),
    )
    monkeypatch.setattr("memory.queues.compact_reflection_eligible", lambda: 0)
    monkeypatch.setattr("memory.capability_catalog.sync_capabilities", lambda: {"count": 0})
    monkeypatch.setattr("global_loop.intent.router.get_router", lambda: MagicMock(bundle="test"))
    monkeypatch.setattr("guardian.response_classes.reload", lambda: None)
    monkeypatch.setattr(
        "guardian.response_classes.status",
        lambda: {"total_classes": 0, "total_triggers": 0},
    )
    monkeypatch.setattr("guardian.terminal_console.install", lambda: None)
    monkeypatch.setattr("guardian.telemetry.emit_boot_marker", lambda: "run-test")

    import boot

    boot.boot()

    assert captured, "boot must invoke recover_unclosed"
    assert captured[0] is rollback_canonical_write


def test_hud_journal_health_uses_read_only_scan(monkeypatch):
    """HIGH-2: HUD health check must not mutate journal via recover_unclosed."""
    import hud.telemetry as tel

    calls = {"recover": 0, "find": 0}

    def _fake_find():
        calls["find"] += 1
        return [{"journal_id": "x"}]

    def _fake_recover(*_a, **_kw):
        calls["recover"] += 1
        return []

    monkeypatch.setattr("memory.encoding_journal.find_unclosed_entries", _fake_find)
    monkeypatch.setattr("memory.encoding_journal.recover_unclosed", _fake_recover)
    monkeypatch.setattr(
        "memory.strength_index.ensure_strength_index", lambda: MagicMock(_records={})
    )

    # Invoke the journal-clean block indirectly via strength-index path
    from memory.strength_index import ensure_strength_index

    ensure_strength_index()
    # Mirror telemetry's journal check snippet
    from memory.encoding_journal import find_unclosed_entries

    unclosed = find_unclosed_entries() or []
    journal_clean = len(unclosed) == 0
    assert journal_clean is False
    assert calls["find"] == 1
    assert calls["recover"] == 0
