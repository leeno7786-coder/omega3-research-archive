"""gather_contributors must not duplicate phase telemetry as emit_data."""

from __future__ import annotations

from unittest.mock import patch

from pressure_loop.contributors import make_contributor
from pressure_loop import pressure as pressure_mod
from pressure_loop.pressure import gather_contributors


class _FakeState:
    def read(self, _key):
        return None


def test_gather_contributors_does_not_emit_data_kind(monkeypatch):
    monkeypatch.setattr(pressure_mod, "_RESEED_STAGING", {})
    monkeypatch.setattr(pressure_mod, "_RESEED_SUPPRESS", {})
    events: list[dict] = []

    def _capture(ev):
        events.append(ev)

    def _reader(_ss):
        return 0.42

    with patch("guardian.terminal_feedback._publish_event", side_effect=_capture):
        with patch(
            "pressure_loop.pressure_field.build_registry", return_value={"operator_intent": _reader}
        ):
            out = gather_contributors(_FakeState())

    assert len(out) == 1
    assert out[0].type == "operator_command"
    data_emits = [
        e for e in events if e.get("kind") == "data" and e.get("phase") == "gather_contributors"
    ]
    assert data_emits == []
