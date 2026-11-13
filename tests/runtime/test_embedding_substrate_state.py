"""The compatibility L0 health surface is backed by the in-brain Ettin retina."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import boot
from core.required_substrate import RequiredSubstrateError
from memory import api, retina


@pytest.fixture(autouse=True)
def restore_substrate_state():
    saved = dict(api._embedding_substrate_state)
    yield
    api._embedding_substrate_state.update(saved)


def test_public_state_surface_accepts_only_known_statuses():
    assert "embedding_substrate" in api.MemoryHealth.__dataclass_fields__
    assert "embedding_substrate_detail" in api.MemoryHealth.__dataclass_fields__

    for status in ("ok", "degraded", "failed", "unknown"):
        api.set_embedding_substrate_state(status, f"test {status}")
        assert api._embedding_substrate_state["status"] == status

    with pytest.raises(ValueError):
        api.set_embedding_substrate_state("mostly-fine")


def test_boot_reports_semantic_health_only_after_ettin_is_seated(monkeypatch):
    import core.paths as core_paths
    from memory.projection import generation as projection
    from tests.memory.projection._fixtures import clock, publish

    # Boot's Program 0 path serves a published generation. Ensure this direct
    # boot-stage regression exercises the generation-backed adapter, too.
    publish(core_paths.MEMORY_ROOT, clock(1))
    reports: list[tuple[str, str]] = []
    mapped_views = []
    monkeypatch.setattr(retina, "encoder", lambda: SimpleNamespace(snapshot="pinned-test"))

    def verify_bound_view():
        view = projection.current_view()
        assert view is not None
        assert view.closed is False
        mapped_views.append(view)
        return {"state": "mapped", "generation_id": view.generation_id, "codes": 42}

    monkeypatch.setattr(retina, "bound_generation", verify_bound_view)
    monkeypatch.setattr(
        api,
        "set_embedding_substrate_state",
        lambda status, detail="": reports.append((status, detail)),
    )

    report: dict[str, object] = {}
    boot._boot_semantic_retina(report)

    assert report["retina"] == "up"
    assert reports == [
        (
            "ok",
            "Ettin resident (tasksource/ettin-17m-embed); "
            f"generation={mapped_views[0].generation_id[:12]}; codes=42",
        )
    ]
    assert len(mapped_views) == 1
    assert mapped_views[0].closed is True


def test_boot_fails_closed_when_ettin_health_cannot_be_recorded(monkeypatch):
    monkeypatch.setattr(retina, "encoder", lambda: SimpleNamespace(snapshot="pinned-test"))
    monkeypatch.setattr(
        retina,
        "bound_generation",
        lambda: {"state": "empty", "generation_id": "0" * 64, "codes": 0},
    )

    def fail(*_args):
        raise RuntimeError("state sink unavailable")

    monkeypatch.setattr(api, "set_embedding_substrate_state", fail)

    with pytest.raises(RequiredSubstrateError) as raised:
        boot._boot_semantic_retina({})

    assert raised.value.component == "memory"
    assert raised.value.operation == "embedding_substrate_state"
