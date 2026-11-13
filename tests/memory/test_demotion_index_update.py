"""Demotion must patch coord_index when lifecycle type changes."""

from __future__ import annotations

from memory.demotion import _persist_document


def test_persist_document_enables_index_update(monkeypatch):
    captured: dict[str, bool] = {}

    def fake_atomic_write(doc, update_index=False):
        captured["update_index"] = update_index
        return "/fake/path"

    monkeypatch.setattr(
        "memory.engine.validate_write.atomic_write",
        fake_atomic_write,
    )

    ok = _persist_document({"id": "2.00.idx", "type": "concept"})
    assert ok is True
    assert captured["update_index"] is True
