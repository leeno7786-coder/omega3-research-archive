"""Continuous boot owns the default MCP host for the Omega process lifetime."""

from __future__ import annotations

from typing import Any

import pytest

import boot as boot_module
from core import boot_lifecycle as bl
from pressure_loop.runtime import PressureRuntime, set_default_runtime


def _prepare_continuous_boot(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
) -> None:
    runtime = PressureRuntime()
    monkeypatch.setattr(
        "runtime.supervisor.start_gateway_watchdog", lambda: {"started": False}
    )

    def _boot() -> dict[str, Any]:
        events.append("substrate_boot")
        set_default_runtime(runtime)
        return {}

    monkeypatch.setattr("signal.signal", lambda *_a, **_k: None)
    monkeypatch.setattr(boot_module, "boot", _boot)
    monkeypatch.setattr("core.boot_lock.acquire", lambda: events.append("boot_lock"))
    monkeypatch.setattr("pressure_loop.policy.assert_pressure_boot_allowed", lambda: None)
    monkeypatch.setattr(
        "autonomic.temporal_grounding.temporal_persistence.record_boot",
        lambda **_k: {
            "boot_count": 1,
            "offline_seconds": None,
            "prior_clean_shutdown": True,
        },
    )
    monkeypatch.setattr(
        bl,
        "restore_substrate_state",
        lambda _runtime: events.append("restore") or {"shared_state": {}},
    )


def _prepare_post_mcp_boot(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    started: list[str],
    shutdown_calls: list[dict[str, Any]],
) -> None:
    _prepare_continuous_boot(monkeypatch, events)
    monkeypatch.setattr(
        "mcp_platform.service.start_default_mcp_host",
        lambda: events.append("mcp_start") or object(),
    )
    monkeypatch.setattr("runtime.brain_cloud.enabled", lambda: False)
    monkeypatch.setattr(
        "runtime.model_control.probe_cognition_runtime",
        lambda: events.append("cognition_probe") or {"model": "test"},
    )
    monkeypatch.setattr("core.workspace_port.reconcile_stale_port", lambda: None)
    monkeypatch.setattr("hud.chat_history.clear_messages", lambda: 0)
    monkeypatch.setattr(
        "browser_agent.lifecycle.prepare_hud_browser",
        lambda **_kwargs: {"edge_started": False},
    )
    monkeypatch.setattr("browser_agent.lifecycle.open_hud_window", lambda _url: None)
    monkeypatch.setattr("hud.spawner.spawn_hud_in_thread", lambda: None)
    monkeypatch.setattr("webbrowser.open", lambda _url: False)
    monkeypatch.setattr("pressure_loop.loop.run_pressure_tick_loop", lambda _runtime: None)
    monkeypatch.setattr("pressure_loop.loop.pressure_loop_stop_requested", lambda: False)
    monkeypatch.setattr(bl, "record_started", started.append)
    monkeypatch.setattr(
        bl,
        "shutdown_system",
        lambda **kwargs: shutdown_calls.append(kwargs) or {},
    )


def test_cloud_continuous_boot_does_not_start_a_local_gateway_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _prepare_post_mcp_boot(monkeypatch, events, [], [])
    prepared_boot = boot_module.boot

    def cloud_boot():
        return {**prepared_boot(), "runtime": "cloud"}

    monkeypatch.setattr(boot_module, "boot", cloud_boot)
    watchdog_calls: list[str] = []
    monkeypatch.setattr(
        "runtime.supervisor.start_gateway_watchdog",
        lambda: watchdog_calls.append("start") or {"started": True},
    )

    boot_module.run_continuous()

    assert watchdog_calls == []
    assert "mcp_start" in events


def test_continuous_boot_starts_and_records_mcp_before_cognition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _prepare_continuous_boot(monkeypatch, events)
    host = object()
    monkeypatch.setattr(
        "mcp_platform.service.start_default_mcp_host",
        lambda: events.append("mcp_start") or host,
    )
    monkeypatch.setattr("runtime.brain_cloud.enabled", lambda: False)
    monkeypatch.setattr(
        "runtime.model_control.probe_cognition_runtime",
        lambda: events.append("cognition_probe") or {"model": "test"},
    )
    monkeypatch.setattr("core.workspace_port.reconcile_stale_port", lambda: None)
    monkeypatch.setattr("hud.chat_history.clear_messages", lambda: 0)
    monkeypatch.setattr("hud.spawner.spawn_hud_in_thread", lambda: None)
    monkeypatch.setattr("pressure_loop.loop.run_pressure_tick_loop", lambda _runtime: None)
    monkeypatch.setattr("pressure_loop.loop.pressure_loop_stop_requested", lambda: False)
    monkeypatch.setattr(bl, "shutdown_system", lambda **_k: {})
    monkeypatch.setattr(
        bl,
        "record_started",
        lambda component: events.append(f"record:{component}"),
    )

    boot_module.run_continuous()

    assert events.index("restore") < events.index("mcp_start")
    assert events.index("mcp_start") < events.index("record:mcp_host")
    assert events.index("record:mcp_host") < events.index("cognition_probe")


def test_corrupt_mcp_state_is_boot_fatal_and_rolls_back_started_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    started: list[str] = []
    _prepare_continuous_boot(monkeypatch, events)
    failure = ValueError("corrupt MCP durable state")
    monkeypatch.setattr(
        "mcp_platform.service.start_default_mcp_host",
        lambda: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(bl, "record_started", started.append)

    def _shutdown(**kwargs: Any) -> dict[str, Any]:
        events.append(f"rollback:{kwargs['reason']}")
        assert kwargs["runtime"] is not None
        return {}

    monkeypatch.setattr(bl, "shutdown_system", _shutdown)

    with pytest.raises(ValueError, match="corrupt MCP durable state") as caught:
        boot_module.run_continuous()

    assert caught.value is failure
    assert started == ["boot_lock", "substrate"]
    assert events[-1] == "rollback:mcp_host_start_failure"


def test_cognition_startup_failure_shuts_down_mcp_and_preserves_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    started: list[str] = []
    shutdown_calls: list[dict[str, Any]] = []
    _prepare_post_mcp_boot(monkeypatch, events, started, shutdown_calls)
    failure = RuntimeError("cognition probe failed")
    monkeypatch.setattr(
        "runtime.model_control.probe_cognition_runtime",
        lambda: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(RuntimeError, match="cognition probe failed") as caught:
        boot_module.run_continuous()

    assert caught.value is failure
    assert started == ["boot_lock", "substrate", "mcp_host"]
    assert len(shutdown_calls) == 1
    assert shutdown_calls[0]["reason"] == "startup_failure"
    assert shutdown_calls[0]["runtime"] is not None


def test_hud_startup_failure_shuts_down_started_cognition_and_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    started: list[str] = []
    shutdown_calls: list[dict[str, Any]] = []
    _prepare_post_mcp_boot(monkeypatch, events, started, shutdown_calls)
    failure = LookupError("HUD spawn failed")
    monkeypatch.setattr(
        "hud.spawner.spawn_hud_in_thread",
        lambda: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(LookupError, match="HUD spawn failed") as caught:
        boot_module.run_continuous()

    assert caught.value is failure
    assert started == [
        "boot_lock",
        "substrate",
        "mcp_host",
        "cognition_runtime",
    ]
    assert len(shutdown_calls) == 1
    assert shutdown_calls[0]["reason"] == "startup_failure"
    assert shutdown_calls[0]["runtime"] is not None


@pytest.mark.parametrize(
    "teardown_failure",
    [OSError("teardown failed"), SystemExit("teardown cancelled")],
)
def test_startup_failure_survives_teardown_failure(
    monkeypatch: pytest.MonkeyPatch,
    teardown_failure: BaseException,
) -> None:
    events: list[str] = []
    started: list[str] = []
    shutdown_calls: list[dict[str, Any]] = []
    _prepare_post_mcp_boot(monkeypatch, events, started, shutdown_calls)
    failure = RuntimeError("cognition probe failed")
    monkeypatch.setattr(
        "runtime.model_control.probe_cognition_runtime",
        lambda: (_ for _ in ()).throw(failure),
    )

    def _failed_shutdown(**kwargs: Any) -> None:
        shutdown_calls.append(kwargs)
        raise teardown_failure

    monkeypatch.setattr(bl, "shutdown_system", _failed_shutdown)

    with pytest.raises(RuntimeError, match="cognition probe failed") as caught:
        boot_module.run_continuous()

    assert caught.value is failure
    assert len(shutdown_calls) == 1


def test_mcp_start_failure_survives_baseexception_during_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    started: list[str] = []
    _prepare_continuous_boot(monkeypatch, events)
    failure = ValueError("corrupt MCP durable state")
    monkeypatch.setattr(
        "mcp_platform.service.start_default_mcp_host",
        lambda: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(bl, "record_started", started.append)
    monkeypatch.setattr(
        bl,
        "shutdown_system",
        lambda **_kwargs: (_ for _ in ()).throw(SystemExit("rollback cancelled")),
    )

    with pytest.raises(ValueError, match="corrupt MCP durable state") as caught:
        boot_module.run_continuous()

    assert caught.value is failure


@pytest.mark.parametrize(
    ("loop_outcome", "expected_reason", "expected_exception"),
    [
        (None, "shutdown", None),
        (KeyboardInterrupt(), "keyboard_interrupt", None),
        (SystemExit(7), "shutdown", SystemExit),
        (RuntimeError("loop failed"), "error:RuntimeError", RuntimeError),
    ],
)
def test_pressure_outcomes_each_shutdown_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    loop_outcome: BaseException | None,
    expected_reason: str,
    expected_exception: type[BaseException] | None,
) -> None:
    events: list[str] = []
    started: list[str] = []
    shutdown_calls: list[dict[str, Any]] = []
    _prepare_post_mcp_boot(monkeypatch, events, started, shutdown_calls)

    def _run_loop(_runtime: PressureRuntime) -> None:
        if loop_outcome is not None:
            raise loop_outcome

    monkeypatch.setattr("pressure_loop.loop.run_pressure_tick_loop", _run_loop)

    if expected_exception is None:
        boot_module.run_continuous()
    else:
        with pytest.raises(expected_exception) as caught:
            boot_module.run_continuous()
        assert caught.value is loop_outcome

    assert len(shutdown_calls) == 1
    assert shutdown_calls[0]["reason"] == expected_reason
