"""boot.py must spawn the in-process HUD before the pressure tick loop."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import boot  # noqa: E402
from core.required_substrate import RequiredSubstrateError  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_boot_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.boot_lock.acquire", lambda: {"pid": 1})
    # Ordering tests must not launch real background services or a browser.
    monkeypatch.setattr("mcp_platform.service.start_default_mcp_host", lambda: object())
    monkeypatch.setattr(
        "runtime.supervisor.start_gateway_watchdog", lambda: {"started": False}
    )
    monkeypatch.setattr("webbrowser.open", lambda *_args: False)


@pytest.fixture(autouse=True)
def _stub_pressure_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pressure_loop.policy.assert_pressure_boot_allowed",
        lambda: None,
    )


@pytest.fixture(autouse=True)
def _stub_default_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pressure_loop.runtime.get_default_runtime",
        lambda: object(),
    )


@pytest.fixture(autouse=True)
def _stub_cognition_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests exercise HUD spawn ordering, not the cognition runtime gate;
    # satisfy the gate so run_continuous reaches the HUD/loop. Boot probes
    # reachability only and loads NO model (2026-07-30 residency policy), so this
    # stubs the probe rather than the loader.
    monkeypatch.setattr(
        "runtime.model_control.probe_cognition_runtime",
        lambda **_k: {"ok": True, "model": "gemma_e4b", "loaded": False},
    )


@pytest.fixture(autouse=True)
def _stub_hud_edge_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "browser_agent.lifecycle.prepare_hud_browser",
        lambda **_kwargs: {"edge_started": False},
    )
    monkeypatch.setattr("browser_agent.lifecycle.open_hud_window", lambda _url: None)


def test_run_continuous_calls_spawn_hud_before_tick_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    shutdown_calls: list[int] = []

    monkeypatch.setattr(boot, "boot", lambda: order.append("boot") or {})

    def fake_spawn() -> str:
        order.append("spawn")
        return "http://127.0.0.1:13310"

    def fake_shutdown() -> None:
        shutdown_calls.append(1)

    monkeypatch.setattr("hud.spawner.spawn_hud_in_thread", fake_spawn)
    shutdown_steps: list[str] = []

    def fake_shutdown_system(**_kwargs) -> dict:
        shutdown_steps.append("shutdown_system")
        fake_shutdown()
        return {}

    monkeypatch.setattr(
        "core.boot_lifecycle.shutdown_system",
        fake_shutdown_system,
    )
    monkeypatch.setattr(
        "core.boot_lifecycle.restore_substrate_state",
        lambda _rt: {},
    )
    monkeypatch.setattr("core.boot_lifecycle.record_started", lambda _c: None)
    monkeypatch.setattr("core.boot_lifecycle.reset_lifecycle_state", lambda: None)

    def fake_loop(_runtime) -> None:
        order.append("tick_loop")
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        "pressure_loop.loop.run_pressure_tick_loop",
        fake_loop,
    )

    boot.run_continuous()

    assert order == ["boot", "spawn", "tick_loop"]
    assert shutdown_calls == [1]
    assert shutdown_steps == ["shutdown_system"]


def test_run_continuous_passes_hud_url_to_the_single_debug_edge_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []
    monkeypatch.delenv("OMEGA_HUD_OPEN_BROWSER", raising=False)
    monkeypatch.setattr(boot, "boot", lambda: {})
    monkeypatch.setattr(
        "browser_agent.lifecycle.prepare_hud_browser",
        lambda **kwargs: events.append(("prepare", kwargs))
        or {"initial_url_opened": True},
    )
    monkeypatch.setattr(
        "browser_agent.lifecycle.open_hud_window",
        lambda url: events.append(("open", url)),
    )
    monkeypatch.setattr(
        "hud.spawner.spawn_hud_in_thread",
        lambda: events.append(("spawn", None)) or "http://127.0.0.1:13310",
    )
    monkeypatch.setattr("core.boot_lifecycle.shutdown_system", lambda **_kwargs: {})
    monkeypatch.setattr("core.boot_lifecycle.restore_substrate_state", lambda _rt: {})
    monkeypatch.setattr("core.boot_lifecycle.record_started", lambda _component: None)
    monkeypatch.setattr("core.boot_lifecycle.reset_lifecycle_state", lambda: None)
    monkeypatch.setattr("pressure_loop.loop.run_pressure_tick_loop", lambda _rt: None)

    boot.run_continuous()

    assert events == [
        ("spawn", None),
        (
            "prepare",
            {"initial_url": "http://127.0.0.1:13310"},
        ),
    ]


def test_run_continuous_calls_shutdown_hud_on_unexpected_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown_calls: list[int] = []

    monkeypatch.setattr(boot, "boot", lambda: {})
    monkeypatch.setattr(
        "hud.spawner.spawn_hud_in_thread",
        lambda: "http://127.0.0.1:13310",
    )
    monkeypatch.setattr(
        "hud.spawner.shutdown_hud",
        lambda: shutdown_calls.append(1),
    )
    monkeypatch.setattr(
        "core.boot_lifecycle.shutdown_system",
        lambda **_k: shutdown_calls.append(1) or {},
    )
    monkeypatch.setattr(
        "core.boot_lifecycle.restore_substrate_state",
        lambda _rt: {},
    )
    monkeypatch.setattr("core.boot_lifecycle.record_started", lambda _c: None)
    monkeypatch.setattr("core.boot_lifecycle.reset_lifecycle_state", lambda: None)

    def fake_loop(_runtime) -> None:
        raise RuntimeError("tick loop died")

    monkeypatch.setattr(
        "pressure_loop.loop.run_pressure_tick_loop",
        fake_loop,
    )

    with pytest.raises(RuntimeError, match="tick loop died"):
        boot.run_continuous()

    assert shutdown_calls == [1]


def test_run_continuous_omega_hud_disable_still_boots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMEGA_HUD_DISABLE", "1")
    spawn_calls: list[int] = []

    monkeypatch.setattr(boot, "boot", lambda: {})
    monkeypatch.setattr(
        "hud.spawner.spawn_hud_in_thread",
        lambda: spawn_calls.append(1) or None,
    )
    monkeypatch.setattr("hud.spawner.shutdown_hud", lambda: None)
    monkeypatch.setattr("core.boot_lifecycle.shutdown_system", lambda **_k: {})
    monkeypatch.setattr(
        "core.boot_lifecycle.restore_substrate_state",
        lambda _rt: {},
    )
    monkeypatch.setattr("core.boot_lifecycle.record_started", lambda _c: None)
    monkeypatch.setattr("core.boot_lifecycle.reset_lifecycle_state", lambda: None)

    def fake_loop(_runtime) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        "pressure_loop.loop.run_pressure_tick_loop",
        fake_loop,
    )

    boot.run_continuous()

    assert spawn_calls == [1]


def test_run_continuous_reverses_started_work_when_substrate_boot_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = RequiredSubstrateError(
        "memory",
        "warmup",
        reason="invalid_index",
    )
    shutdown_calls: list[dict] = []
    spawn_calls: list[int] = []

    monkeypatch.setattr(boot, "boot", lambda: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(
        "core.boot_lifecycle.shutdown_system",
        lambda **kwargs: shutdown_calls.append(dict(kwargs)) or {},
    )
    monkeypatch.setattr("core.boot_lifecycle.record_started", lambda _component: None)
    monkeypatch.setattr("core.boot_lifecycle.reset_lifecycle_state", lambda: None)
    monkeypatch.setattr(
        "hud.spawner.spawn_hud_in_thread",
        lambda: spawn_calls.append(1) or "http://127.0.0.1:13310",
    )

    with pytest.raises(RequiredSubstrateError) as raised:
        boot.run_continuous()

    assert raised.value is failure
    assert len(shutdown_calls) == 1
    assert shutdown_calls[0]["reason"] == "substrate_start_failure"
    assert spawn_calls == []


def test_run_continuous_reverses_started_work_when_state_restore_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = RequiredSubstrateError(
        "substrate_state",
        "restore",
        reason="snapshot_unreadable",
    )
    shutdown_calls: list[dict] = []

    monkeypatch.setattr(boot, "boot", lambda: {})
    monkeypatch.setattr(
        "core.boot_lifecycle.restore_substrate_state",
        lambda _runtime: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(
        "core.boot_lifecycle.shutdown_system",
        lambda **kwargs: shutdown_calls.append(dict(kwargs)) or {},
    )
    monkeypatch.setattr("core.boot_lifecycle.record_started", lambda _component: None)
    monkeypatch.setattr("core.boot_lifecycle.reset_lifecycle_state", lambda: None)

    with pytest.raises(RequiredSubstrateError) as raised:
        boot.run_continuous()

    assert raised.value is failure
    assert len(shutdown_calls) == 1
    assert shutdown_calls[0]["reason"] == "substrate_restore_failure"
    assert shutdown_calls[0]["runtime"] is not None
