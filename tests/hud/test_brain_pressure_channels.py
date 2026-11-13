"""Brain panel System Pressure channels prefer ignition contributor roster."""

from __future__ import annotations

from unittest.mock import patch

from hud import telemetry


def _audit_fixture():
    return [
        {
            "kind": "data",
            "loop": "pressure",
            "phase": "episode_ignition",
            "ts": "2026-06-19T12:00:00Z",
            "opened_by": "learning_debt",
            "aggregate_pressure": 0.82,
            "contributors": [
                {"type": "learning_debt", "weight": 0.82, "source": "autonomous"},
                {"type": "curiosity_gap", "weight": 0.41, "source": "autonomous"},
                {"type": "operator_query", "weight": 0.28, "source": "operator"},
                {"type": "contradiction", "weight": 0.15, "source": "autonomous"},
            ],
        },
        {
            "kind": "phase",
            "loop": "pressure",
            "phase": "gather_contributors",
            "status": "ok",
            "ts": "2026-06-19T12:00:01Z",
            "weights": {"learning_debt": 0.52},
        },
    ]


def test_contributors_from_audit_prefers_ignition_roster(monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "_audit_events",
        lambda limit=800, **_: list(reversed(_audit_fixture()))[:limit],
    )
    rows = telemetry._contributors_from_audit()
    assert len(rows) == 4
    assert rows[0]["name"] == "learning debt"
    assert rows[0]["score"] == 0.82
    assert rows[1]["name"] == "curiosity gap"
    assert rows[2]["name"] == "operator query"
    assert rows[3]["name"] == "contradiction"


def test_pressure_channels_show_four_ignition_scores(monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "_audit_events",
        lambda limit=800, **_: list(reversed(_audit_fixture()))[:limit],
    )
    rows = telemetry._contributors_from_audit()
    channels = telemetry._pressure_channels_from_contributors(rows)
    assert len(channels) == 4
    assert channels[0]["label"] == "learning debt"
    assert channels[0]["score"] == 0.82
    assert "score 0.820" in channels[0]["detail"]
    assert channels[1]["label"] == "curiosity gap"
    assert channels[2]["label"] == "operator query"
    assert channels[3]["label"] == "contradiction"
    assert all("awaiting tick" not in c["detail"] for c in channels)
