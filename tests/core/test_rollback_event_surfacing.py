"""Change 4b: a drained user-rollback event surfaces as a chat turn (memory.hot_buffer),
so the operator sees it and it flows to memory that the file now differs from before."""

from __future__ import annotations

from types import SimpleNamespace

import cognition_chain as cc
import memory.hot_buffer as hb
from pressure_loop import system_event_queue as seq


def test_rollback_event_surfaces_chat_turn(monkeypatch):
    monkeypatch.setattr(
        seq,
        "drain_rollback_events",
        lambda: (
            {
                "event_id": "e1",
                "file_path": "/ws/a.py",
                "deleted": False,
                "message": "Workspace rollback applied: rolled back /ws/a.py.",
            },
        ),
    )
    turns: list[tuple[str, str]] = []
    monkeypatch.setattr(hb, "append_turn", lambda role, text: turns.append((role, text)))

    ep = SimpleNamespace(meta={}, pressure={}, episode_id="ep")
    cc._attach_pending_system_events(ep)

    assert any("/ws/a.py" in text for _role, text in turns), turns
    assert ep.meta.get("dev_rollback_events")  # still attached to the episode (existing behavior)
