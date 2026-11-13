"""HUD standalone boots substrate paused for visual inspection."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reset_pause():
    from pressure_loop.pressure_loop_pause import _reset_pressure_loop_pause_for_tests
    import pressure_loop.runtime as _rt

    _rt._default_runtime = None  # no stale runtime bleeds in from a prior test (order-independence)
    _reset_pressure_loop_pause_for_tests()
    yield
    _reset_pressure_loop_pause_for_tests()
    _rt._default_runtime = None


def test_boot_paused_for_hud_registers_runtime_and_holds_pause(monkeypatch):
    import boot as boot_mod
    from pressure_loop.pressure_loop_pause import pressure_loop_pause_state
    from pressure_loop.runtime import try_get_default_runtime

    monkeypatch.setattr(boot_mod, "boot", lambda: {"summary": "ok", "memory": "up"})

    report = boot_mod.boot_paused_for_hud(pause_owner="test-hud")

    assert report["pressure_loop"] == "paused"
    assert report["mode"] == "hud_paused"
    assert try_get_default_runtime() is None  # boot() was stubbed — pause still set

    pause = pressure_loop_pause_state()
    assert pause["paused"] is True
    assert "test-hud" in pause["owners"]


def test_boot_paused_for_hud_with_real_boot(monkeypatch):
    import boot as boot_mod
    from memory.projection import generation as projection
    from pressure_loop.pressure_loop_pause import pressure_loop_pause_state
    from pressure_loop.runtime import try_get_default_runtime

    mapped_views = []
    monkeypatch.setattr(
        "guardian.telemetry.emit_boot_marker",
        lambda: "test-run",
    )
    monkeypatch.setattr(
        "runtime.client.get_runtime_client",
        lambda: type("C", (), {"health": lambda self: True, "base_url": "http://x"})(),
    )
    monkeypatch.setattr(
        "runtime.supervisor.ensure_runtime_process",
        lambda **_kwargs: {"ok": True, "spawned": False},
    )
    monkeypatch.setattr("memory.api.set_embedding_substrate_state", lambda *_args: None)
    monkeypatch.setattr("memory.engine.walker.load_coord_index", lambda: None)
    monkeypatch.setattr("memory.strength_index.ensure_strength_index", lambda: None)
    monkeypatch.setattr("memory.engine.walker.COORD_INDEX", {})
    monkeypatch.setattr(
        "memory.taxonomy.compile_trigger.compile_on_boot",
        lambda: SimpleNamespace(node_count=0, snapshot_hash="0" * 64),
    )
    monkeypatch.setattr("memory.encoding_journal.recover_unclosed", lambda _rollback: [])
    monkeypatch.setattr("memory.queues.compact_reflection_eligible", lambda: 0)
    monkeypatch.setattr("memory.retina.encoder", lambda: SimpleNamespace(snapshot="test"))

    def verify_bound_view():
        view = projection.current_view()
        assert view is not None
        assert view.closed is False
        mapped_views.append(view)
        return {"state": "empty", "generation_id": view.generation_id, "codes": 0}

    monkeypatch.setattr("memory.retina.bound_generation", verify_bound_view)
    monkeypatch.setattr("global_loop.intent.router.get_router", lambda: SimpleNamespace(bundle="test"))
    monkeypatch.setattr("guardian.response_classes.reload", lambda: None)
    monkeypatch.setattr(
        "guardian.response_classes.status",
        lambda: {"total_classes": 0, "total_triggers": 0},
    )
    monkeypatch.setattr("guardian.terminal_console.install", lambda: None)
    monkeypatch.setattr("pressure_loop.pressure_field.reset_pressure_accumulator", lambda: None)

    report = boot_mod.boot_paused_for_hud()

    assert try_get_default_runtime() is not None
    assert report["pressure_loop"] == "paused"
    assert pressure_loop_pause_state()["paused"] is True
    assert len(mapped_views) == 1
    assert mapped_views[0].closed is True


def test_standalone_boot_substrate_paused_delegates(monkeypatch):
    import hud.standalone as standalone

    calls: list[str] = []

    def _fake():
        calls.append("boot_paused")
        return {"pressure_loop": "paused"}

    monkeypatch.setattr(standalone, "boot_substrate_paused", _fake)
    assert standalone.boot_substrate_paused()["pressure_loop"] == "paused"
    assert calls == ["boot_paused"]


def test_standalone_keeps_plugins_available_when_substrate_boot_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hud.standalone as standalone

    events: list[str] = []

    def degraded_boot() -> dict[str, object]:
        events.append("boot")
        raise RuntimeError("inference runtime unavailable")

    def start_mcp() -> object:
        events.append("mcp_start")
        return object()

    def stop_mcp() -> dict[str, int]:
        events.append("mcp_stop")
        return {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}

    def run(_app_target: str, **_kwargs: object) -> None:
        events.append("uvicorn")

    monkeypatch.setattr(standalone, "boot_substrate_paused", degraded_boot)
    monkeypatch.setattr(standalone, "_port_in_use", lambda _host, _port: False)
    monkeypatch.setattr("mcp_platform.service.start_default_mcp_host", start_mcp)
    monkeypatch.setattr("mcp_platform.service.shutdown_default_mcp_host", stop_mcp)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=run))

    assert standalone.main(["--port", "13311"]) == 0
    assert events == ["boot", "mcp_start", "uvicorn", "mcp_stop"]


@pytest.mark.parametrize(
    ("argv", "expected_host", "expected_port"),
    (
        (["--no-boot", "--host", "localhost", "--port", "13311"], "localhost", "13311"),
        (["--no-boot"], "127.0.0.1", "13310"),
        (["--no-boot", "--host", "0.0.0.0", "--port", "14444"], "0.0.0.0", "14444"),
    ),
)
def test_standalone_synchronizes_listener_authority_before_probe_and_app_load(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected_host: str,
    expected_port: str,
) -> None:
    import hud.standalone as standalone

    observed: list[tuple[str, str, str]] = []
    monkeypatch.setenv("OMEGA_HUD_HOST", "stale.example")
    monkeypatch.setenv("OMEGA_HUD_PORT", "65500")

    def probe(host: str, port: int) -> bool:
        observed.append(
            ("probe", os.environ["OMEGA_HUD_HOST"], os.environ["OMEGA_HUD_PORT"])
        )
        assert (host, port) == (expected_host, int(expected_port))
        return False

    def run(app_target: str, **kwargs: object) -> None:
        observed.append(
            ("uvicorn", os.environ["OMEGA_HUD_HOST"], os.environ["OMEGA_HUD_PORT"])
        )
        assert app_target == "hud.server:app"
        assert kwargs["host"] == expected_host
        assert kwargs["port"] == int(expected_port)

    monkeypatch.setattr(standalone, "_port_in_use", probe)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=run))

    assert standalone.main(argv) == 0
    assert observed == [
        ("probe", expected_host, expected_port),
        ("uvicorn", expected_host, expected_port),
    ]
