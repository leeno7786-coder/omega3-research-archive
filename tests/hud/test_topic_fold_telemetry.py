"""HUD surfaces last_topic_fold from shared state on the omega panel."""

from __future__ import annotations


def test_omega_telemetry_includes_topic_fold(monkeypatch):
    from hud import telemetry

    def _read(key: str):
        if key == "last_topic_fold":
            return {
                "active": True,
                "target_domain": "attention mechanisms",
                "question_id": "q-1",
                "question_text": "how do attention heads combine?",
                "scoped_topic": "attention mechanisms: how do attention heads combine?",
                "at": "2026-06-10T12:00:00+00:00",
            }
        if key == "last_omega_trajectory":
            return None
        if key == "recent_omega_epochs":
            return []
        return None

    monkeypatch.setattr(telemetry, "_shared_state_read", _read)

    out = telemetry._telemetry_omega()
    fold = out.get("topic_fold") or {}
    assert fold.get("active") is True
    assert fold.get("target_domain") == "attention mechanisms"
    assert "attention heads" in fold.get("question_text", "")
