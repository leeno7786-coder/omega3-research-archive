"""Batch 3 (15 P3 rows) — failure-log emits per FAILURE_LOG_IMPL_WORKORDER_BATCH3.md."""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from autonomic.cognitive_churn import _content_fingerprints_by_domain
from guardian.telemetry import writer
from hud.server import _attach_image_urls
from memory.api import QuerySpec, read_memory
from memory.consolidation import run_consolidation_once
from memory.knowledge_router import _load_index
from runtime.face.anchor_store import AnchorStore


def _failure_records():
    p = writer.FAILURE_LOG
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _has_failure(
    *, reason: str | None = None, phase: str | None = None, status: str | None = None
) -> bool:
    for rec in _failure_records():
        if reason is not None and rec.get("reason") != reason:
            continue
        if phase is not None and rec.get("phase") != phase:
            continue
        if status is not None and rec.get("status") != status:
            continue
        return True
    return False


# Row 1 — consolidation StrengthIndex flush failure
def test_consolidation_strength_flush_failed_is_logged(monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("flush induced")

    monkeypatch.setattr("memory.strength_index.ensure_strength_index", _boom)
    report = run_consolidation_once(limit_resources=False)
    assert report["status"] == "degraded"
    assert _has_failure(phase="consolidation_strength_flush_failed", status="degraded")


# Row 2 — knowledge Z-index load failure
def test_knowledge_z_index_load_failed_is_logged(monkeypatch, tmp_path):
    idx = tmp_path / "knowledge_z_index.json"
    idx.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr("memory.knowledge_router._INDEX_PATH", idx)
    _load_index()
    assert _has_failure(phase="knowledge_z_index_load_failed")


# Row 3 — history attach per-candidate load_meta failure
def test_history_attach_failed_is_logged(monkeypatch):
    # read_memory now sources candidates from taxonomy.recall (Stage 3), not
    # walker_query — inject the candidate there so it reaches the history-attach
    # post-pass that this test exercises.
    from memory.taxonomy.schemas import RecallPacket
    import memory.taxonomy.recall as _taxrecall

    def _recall(query, **_kw):
        return RecallPacket(
            query=query,
            query_hash="h",
            query_kind="",
            snapshot_hash="s",
            resolved_entities=(),
            results=(
                {
                    "node_id": "mem-hist-1",
                    "node_type": "memory_record",
                    "label": "hist",
                    "score": 0.9,
                    "evidence_event_ids": (),
                    "payload": {"coord": [2, 0, 0]},
                },
            ),
            evidence_event_ids=(),
        )

    def _boom(_path):
        raise RuntimeError("load_meta induced")

    monkeypatch.setattr(_taxrecall, "recall", _recall)
    monkeypatch.setattr("memory.engine.walker.load_coord_index", lambda: None)
    monkeypatch.setattr(
        "memory.engine.walker.COORD_INDEX",
        {"mem-hist-1": {"path": "fake/path.json"}},
    )
    monkeypatch.setattr("memory.engine.walker.load_meta_by_path", _boom)
    read_memory(QuerySpec(cycle_id="cycle-ha", text="history attach"))
    assert _has_failure(phase="history_attach_failed")


# Row 4 — history attach outer failure disables revision history
def test_history_attach_disabled_is_logged(monkeypatch):
    def _walker_query(_q):
        return ([{"id": "m1", "coord": [2, 0, 0], "score": 0.5, "provenance": {}}], {})

    def _boom():
        raise ImportError("history attach induced")

    monkeypatch.setattr("memory.api.walker_query", _walker_query)
    monkeypatch.setattr("memory.engine.walker.load_coord_index", _boom)
    read_memory(QuerySpec(cycle_id="cycle-had", text="history disabled"))
    assert _has_failure(phase="history_attach_disabled")


# Row 7 — anchor store load failure
def test_anchor_store_load_failed_is_logged(monkeypatch, tmp_path):
    bad = tmp_path / "face_anchors.json"
    bad.write_text("{bad", encoding="utf-8")
    store = AnchorStore(path=bad)
    store.names()
    assert _has_failure(phase="anchor_store_load_failed")


# Row 8 — churn content fingerprints read failure
def test_churn_fingerprints_failed_is_logged(monkeypatch):
    import autonomic.cognitive_churn as churn

    def _boom(*_a, **_kw):
        raise RuntimeError("fingerprints induced")

    monkeypatch.setattr("autonomic._history.recent_cycles", _boom)
    _content_fingerprints_by_domain([], cycle_limit=3)
    assert _has_failure(phase="churn_fingerprints")


# Row 9 — image URL resolution failure
def test_image_url_resolution_failed_is_logged(monkeypatch):
    def _boom(_path):
        raise RuntimeError("url induced")

    monkeypatch.setattr("hud.server.Path.resolve", _boom)
    result = _attach_image_urls({"path": "/tmp/fake.png"})
    assert result.get("url", "") == ""
    assert _has_failure(phase="image_url_resolution_failed")


# Row 10 — strength index read failure in memory tree
def test_strength_index_read_failed_is_logged(monkeypatch, tmp_path):
    class BadSI:
        def get_strength(self, _mid):
            raise RuntimeError("strength read induced")

    from fastapi.testclient import TestClient
    from memory.dewey_map import LOBE_MAP, SUBDOMAIN_MAP
    from hud.server import create_app

    sub_path = tmp_path / "memory" / LOBE_MAP[0] / SUBDOMAIN_MAP[(0, 0)]
    sub_path.mkdir(parents=True)
    (sub_path / "strength-probe.json").write_text(
        json.dumps({"id": "strength-probe", "title": "probe"}),
        encoding="utf-8",
    )

    monkeypatch.setattr("hud.server.OMEGA_ROOT", tmp_path)
    monkeypatch.setattr("memory.strength_index.get_strength_index", lambda: BadSI())
    hud_client = TestClient(create_app())
    hud_client.get("/api/memory/lobes?expand=all")
    assert _has_failure(phase="strength_index_read_failed")


@pytest.fixture
def hud_client():
    from fastapi.testclient import TestClient
    from hud.server import create_app

    return TestClient(create_app())


# Row 11 — enrolled operator name read failure
def test_enrolled_operator_name_failed_is_logged(monkeypatch, hud_client):
    def _boom():
        raise RuntimeError("enrolled induced")

    class SS:
        def read(self, _key):
            return ""

    monkeypatch.setattr("sensory.operator_presence.enrolled_operator_name", _boom)
    monkeypatch.setattr(
        "sensory.operator_presence.read_presence_from_shared_state",
        lambda _ss: "PRESENT",
    )
    monkeypatch.setattr(
        "pressure_loop.runtime.try_get_default_runtime",
        lambda: SimpleNamespace(shared_state=SS()),
    )
    hud_client.get("/api/presence/state")
    assert _has_failure(phase="enrolled_operator_name_failed")


# Row 12 — heartbeat read failure
def test_heartbeat_read_failed_is_logged(monkeypatch, hud_client):
    from core.shared_state_keys import PRESENCE_WATCHER_HEARTBEAT

    class BadSS:
        def read(self, key):
            if key == PRESENCE_WATCHER_HEARTBEAT:
                raise RuntimeError("heartbeat induced")
            return "PRESENT"

    monkeypatch.setattr(
        "pressure_loop.runtime.try_get_default_runtime",
        lambda: SimpleNamespace(shared_state=BadSS()),
    )
    monkeypatch.setattr(
        "sensory.operator_presence.read_presence_from_shared_state",
        lambda _ss: "PRESENT",
    )
    monkeypatch.setattr(
        "sensory.operator_presence.enrolled_operator_name",
        lambda: None,
    )
    hud_client.get("/api/presence/state")
    assert _has_failure(phase="heartbeat_read_failed")


# Row 13 — presence staleness check failure
def test_presence_staleness_failed_is_logged(monkeypatch, hud_client):
    class SS:
        def read(self, _key):
            return ""

    monkeypatch.setattr(
        "pressure_loop.runtime.try_get_default_runtime",
        lambda: SimpleNamespace(shared_state=SS()),
    )
    monkeypatch.setattr(
        "sensory.operator_presence.read_presence_from_shared_state",
        lambda _ss: "PRESENT",
    )
    monkeypatch.setattr(
        "sensory.operator_presence.enrolled_operator_name",
        lambda: None,
    )
    monkeypatch.setattr(
        "sensory.operator_presence.presence_is_stale",
        MagicMock(side_effect=RuntimeError("staleness induced")),
    )
    hud_client.get("/api/presence/state")
    assert _has_failure(phase="presence_staleness_failed")


# Row 14 — presence read failure in identity probe
def test_presence_read_failed_is_logged(monkeypatch):
    import hud.telemetry as tel

    monkeypatch.setattr(tel, "_RC", MagicMock(FACE=object()))
    broker_snapshot = {
        tel._RC.FACE: SimpleNamespace(state=SimpleNamespace(value="available"), reason="")
    }

    def _boom(_ss):
        raise RuntimeError("presence induced")

    monkeypatch.setattr("sensory.operator_presence.read_presence_from_shared_state", _boom)
    monkeypatch.setattr(tel, "_get_shared_state", lambda: MagicMock())
    tel._probe_identity(broker_snapshot)
    assert _has_failure(phase="presence_read_failed")


# Row 15 — operator name read failure in identity probe
def test_operator_name_read_failed_is_logged(monkeypatch):
    import hud.telemetry as tel

    monkeypatch.setattr(tel, "_RC", MagicMock(FACE=object()))
    broker_snapshot = {
        tel._RC.FACE: SimpleNamespace(state=SimpleNamespace(value="available"), reason="")
    }
    monkeypatch.setattr(
        "sensory.operator_presence.read_presence_from_shared_state",
        lambda _ss: "PRESENT",
    )
    monkeypatch.setattr(tel, "_get_shared_state", lambda: MagicMock())

    class _BoomStore:
        def names(self):
            raise RuntimeError("operator name induced")

    monkeypatch.setattr(
        "runtime.face.anchor_store.get_anchor_store",
        lambda: _BoomStore(),
    )
    tel._probe_identity(broker_snapshot)
    assert _has_failure(phase="operator_name_read_failed")
