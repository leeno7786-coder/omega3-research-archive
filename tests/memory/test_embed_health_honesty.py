"""Memory health reports the resident Ettin semantic retina honestly."""

from __future__ import annotations

import pytest


@pytest.fixture
def substrate_state():
    """Save and restore the process-global semantic-substrate verdict."""
    from memory import api

    saved = dict(api._embedding_substrate_state)
    yield api
    api._embedding_substrate_state.update(saved)


def test_health_reports_unseated_ettin_as_degraded(substrate_state, monkeypatch):
    api = substrate_state
    api.set_embedding_substrate_state("ok", "Ettin seated at boot")
    monkeypatch.setattr(
        "memory.retina.stats",
        lambda: {"encoder_resident": False, "model": "tasksource/ettin-17m-embed"},
    )

    health = api.memory_health()

    assert health.embedding_substrate == "degraded"
    assert "Ettin encoder not seated" in health.embedding_substrate_detail
    assert "Ettin seated at boot" in health.embedding_substrate_detail
    assert any("embedding substrate degraded" in issue for issue in health.issues)


def test_health_reports_ok_while_ettin_is_resident(substrate_state, monkeypatch):
    api = substrate_state
    api.set_embedding_substrate_state(
        "ok", "Ettin resident (tasksource/ettin-17m-embed); codes=42"
    )
    monkeypatch.setattr(
        "memory.retina.stats",
        lambda: {"encoder_resident": True, "model": "tasksource/ettin-17m-embed"},
    )

    health = api.memory_health()

    assert health.embedding_substrate == "ok"
    assert "Ettin" in health.embedding_substrate_detail


def test_live_probe_never_upgrades_a_failed_boot_verdict(substrate_state, monkeypatch):
    api = substrate_state
    api.set_embedding_substrate_state("failed", "Ettin snapshot verification failed")
    monkeypatch.setattr(
        "memory.retina.stats",
        lambda: {"encoder_resident": True, "model": "tasksource/ettin-17m-embed"},
    )

    health = api.memory_health()

    assert health.embedding_substrate == "failed"
    assert "snapshot verification failed" in health.embedding_substrate_detail


def test_unknown_health_lazy_seats_ettin(substrate_state, monkeypatch):
    api = substrate_state
    api.set_embedding_substrate_state("unknown", "boot has not reported L0 state yet")
    seated: list[bool] = []

    def _seat():
        seated.append(True)
        return object()

    monkeypatch.setattr("memory.retina.encoder", _seat)
    monkeypatch.setattr(
        "memory.retina.stats",
        lambda: {"encoder_resident": bool(seated), "model": "tasksource/ettin-17m-embed"},
    )

    health = api.memory_health()

    assert seated == [True]
    assert health.embedding_substrate == "ok"
    assert health.embedding_substrate_detail == "probed via memory_health"
