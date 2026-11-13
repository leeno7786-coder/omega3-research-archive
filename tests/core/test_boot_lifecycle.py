"""Boot lifecycle — reverse teardown order + substrate persistence."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import boot_lifecycle as bl  # noqa: E402
from core.required_substrate import RequiredSubstrateError  # noqa: E402
from core.shared_state import SharedState  # noqa: E402
from core.shared_state_keys import LAST_GLOBAL_GOALS  # noqa: E402
from pressure_loop.pressure_field import (  # noqa: E402
    export_pressure_field_state,
    import_pressure_field_state,
    read_neuromod_tonic,
)
from pressure_loop.runtime import PressureRuntime  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_lifecycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bl.reset_lifecycle_state()
    monkeypatch.setattr(bl, "BOOT_MANIFEST_PATH", tmp_path / "boot.manifest.json")
    monkeypatch.setattr(bl, "SUBSTRATE_SNAPSHOT_PATH", tmp_path / "substrate_snapshot.json")
    monkeypatch.setattr(
        "guardian.state_persistence.SAVE_PATH",
        tmp_path / "save_state.json",
    )
    monkeypatch.setattr(
        "autonomic.temporal_grounding.temporal_persistence.load_state",
        lambda: {},
    )
    monkeypatch.setattr(
        "autonomic.temporal_grounding.temporal_persistence.record_shutdown",
        lambda **_kwargs: None,
    )


def test_shutdown_reverses_boot_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Components tear down in reverse — after ONE step that precedes them all.

    The cognition threads are stopped before the walk begins (RISK 4(b),
    core.boot_lifecycle._stop_cognition_threads), and asking the pressure loop
    to stop is part of that: no new cascade may be dispatched while the ones in
    flight are being drained. So the loop's stop is requested twice, and the
    FIRST of the two is the load-bearing one — it lands before the
    runtime_process step and the sweep take the workers away. The reverse order
    itself is unchanged, which is what the tail of this assertion says.
    """
    order: list[str] = []

    for component in bl.BOOT_COMPONENTS:
        bl.record_started(component)

    monkeypatch.setattr(
        "pressure_loop.loop.request_pressure_loop_stop",
        lambda: order.append("pressure_loop"),
    )
    monkeypatch.setattr(
        "hud.spawner.shutdown_hud",
        lambda: order.append("hud"),
    )
    monkeypatch.setattr(
        "mcp_platform.service.shutdown_default_mcp_host",
        lambda: order.append("mcp_host")
        or {"sessions_closed": 0, "sessions_forced": 0, "errors": 0},
    )

    def _persist(_rt, *, reason: str = "shutdown") -> dict:
        order.append("substrate")
        return {}

    monkeypatch.setattr(bl, "persist_substrate_state", _persist)
    monkeypatch.setattr(
        "runtime.supervisor.shutdown_runtime_process",
        lambda: order.append("runtime_process") or {},
    )
    monkeypatch.setattr(
        "core.boot_lock.release",
        lambda: order.append("boot_lock"),
    )

    runtime = PressureRuntime()
    summary = bl.shutdown_system(reason="test", runtime=runtime)

    assert order == [
        "pressure_loop",  # the pre-walk cognition-thread stop
        "pressure_loop",
        "hud",
        "mcp_host",
        "substrate",
        "runtime_process",
        "boot_lock",
    ]
    owned = set(bl.BOOT_COMPONENTS)
    assert [
        step["component"] for step in summary["steps"] if step["component"] in owned
    ] == [
        "pressure_loop",
        "hud",
        "cognition_runtime",
        "mcp_host",
        "substrate",
        "runtime_process",
        "boot_lock",
    ]


@pytest.mark.parametrize("reason,clean", [
    ("shutdown", True), ("keyboard_interrupt", True), ("pressure_loop_stop", True),
    ("error:ValueError", False), ("substrate_start_failure", False), ("mcp_host_start_failure", False),
])
def test_the_lifetime_clock_is_stamped_first_and_by_intent(monkeypatch: pytest.MonkeyPatch, reason, clean) -> None:
    """Operator 2026-09-15: a crash that walked the teardown read clean; a console close that
    Windows killed mid-teardown read unclean. The stamp is the first step and follows the reason."""
    stamps: list[dict] = []
    monkeypatch.setattr(
        "autonomic.temporal_grounding.temporal_persistence.load_state", lambda: {"last_boot_at": 100.0})
    monkeypatch.setattr(
        "autonomic.temporal_grounding.temporal_persistence.record_shutdown",
        lambda **kwargs: stamps.append(kwargs))
    monkeypatch.setattr("pressure_loop.loop.request_pressure_loop_stop", lambda: None)
    monkeypatch.setattr("core.boot_lock.release", lambda: None)
    bl.record_started("boot_lock")

    summary = bl.shutdown_system(reason=reason)

    assert summary["steps"][0]["component"] == "temporal_clock"
    assert len(stamps) == 1 and stamps[0]["clean"] is clean
    assert stamps[0]["session_seconds"] > 0
    assert bl.shutdown_is_clean(reason) is clean


def test_mcp_host_is_boot_owned_between_substrate_and_cognition() -> None:
    assert bl.BOOT_COMPONENTS.index("substrate") < bl.BOOT_COMPONENTS.index("mcp_host")
    assert bl.BOOT_COMPONENTS.index("mcp_host") < bl.BOOT_COMPONENTS.index(
        "cognition_runtime"
    )


def test_shutdown_stops_mcp_before_substrate_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        "mcp_platform.service.shutdown_default_mcp_host",
        lambda: order.append("mcp")
        or {"sessions_closed": 0, "sessions_forced": 0, "errors": 0},
    )
    monkeypatch.setattr(
        bl,
        "persist_substrate_state",
        lambda *_a, **_k: order.append("persist") or {},
    )
    bl.record_started("substrate")
    bl.record_started("mcp_host")

    bl.shutdown_system(reason="test", runtime=PressureRuntime())

    assert order == ["mcp", "persist"]


def test_shutdown_marks_substrate_step_failed_when_persistence_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted = {
        "shared_state": {"ok": False, "error": "disk full"},
        "strength_index": {"ok": True},
        "substrate_snapshot": {"ok": True},
    }
    monkeypatch.setattr(bl, "_stop_cognition_threads", lambda: {})
    monkeypatch.setattr(bl, "persist_substrate_state", lambda *_a, **_k: persisted)
    bl.record_started("substrate")

    summary = bl.shutdown_system(reason="test", runtime=PressureRuntime())

    substrate_step = next(
        step for step in summary["steps"] if step["component"] == "substrate"
    )
    assert substrate_step["ok"] is False
    assert "shared_state" in substrate_step["error"]
    assert summary["persist"] == persisted


@pytest.mark.parametrize(
    ("counts", "expected_ok"),
    [
        ({"sessions_closed": 3, "sessions_forced": 0, "errors": 0}, True),
        ({"sessions_closed": 2, "sessions_forced": 1, "errors": 0}, False),
        ({"sessions_closed": 2, "sessions_forced": 0, "errors": 1}, False),
    ],
)
def test_shutdown_records_exact_mcp_counts_and_failure_status(
    monkeypatch: pytest.MonkeyPatch,
    counts: dict[str, int],
    expected_ok: bool,
) -> None:
    monkeypatch.setattr(
        "mcp_platform.service.shutdown_default_mcp_host",
        lambda: counts,
    )
    bl.record_started("mcp_host")

    summary = bl.shutdown_system(reason="test", runtime=PressureRuntime())

    assert summary["mcp_host"] == counts
    mcp_step = next(
        step for step in summary["steps"] if step["component"] == "mcp_host"
    )
    assert mcp_step["ok"] is expected_ok


def test_shutdown_stops_the_omega_owned_runtime_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A graceful shutdown delegates the owned runtime process to its supervisor."""
    calls: list[str] = []
    bl.record_started("runtime_process")

    monkeypatch.setattr(
        "runtime.supervisor.shutdown_runtime_process",
        lambda: calls.append("runtime_process") or {"ok": True},
    )
    bl.shutdown_system(reason="test", runtime=PressureRuntime())

    assert calls == ["runtime_process"]


def test_shutdown_releases_in_process_model_seats_before_runtime_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr("memory.retina.reset_encoder", lambda: calls.append("retina"))
    monkeypatch.setattr(
        "memory.ettin_encoder.reset_encoder_cache",
        lambda: calls.append("ettin_cache"),
    )
    monkeypatch.setattr(
        "global_loop.intent.router.reset_router_cache",
        lambda: calls.append("intent_router"),
    )
    monkeypatch.setattr(
        "memory.dme.shutdown_runtime",
        lambda: calls.append("dme") or {"ok": True},
    )
    monkeypatch.setattr(
        "runtime.supervisor.shutdown_runtime_process",
        lambda: calls.append("runtime_process") or {"ok": True},
    )
    bl.record_started("runtime_process")

    bl.shutdown_system(reason="test", runtime=PressureRuntime())

    assert calls == ["retina", "ettin_cache", "intent_router", "dme", "runtime_process"]


def test_shutdown_releases_the_browser_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator 2026-09-14: shutdown releases Omega's browser; nothing about it is saved."""
    from types import SimpleNamespace

    from auto_loop.execute import brain_skill_executor as executor

    detached: list[bool] = []
    monkeypatch.setattr(
        executor, "_BROWSER_CONTROLLER", SimpleNamespace(release=lambda: detached.append(True))
    )

    summary = bl.shutdown_system(reason="test", runtime=PressureRuntime())

    assert detached == [True] and executor._BROWSER_CONTROLLER is None
    assert {"component": "browser", "ok": True} in summary["steps"]


def test_shutdown_does_not_sweep_outside_a_real_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process not recorded as a boot component is not shut down by this run."""
    calls: list[str] = []
    bl.record_started("cognition_runtime")  # no boot_lock

    monkeypatch.setattr(
        "runtime.supervisor.shutdown_runtime_process",
        lambda: calls.append("runtime_process") or {"ok": True},
    )
    bl.shutdown_system(reason="test", runtime=PressureRuntime())

    assert calls == [], "an unstarted runtime component must not be shut down"


def test_shutdown_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    bl.record_started("hud")
    monkeypatch.setattr(
        "hud.spawner.shutdown_hud",
        lambda: calls.append(1),
    )
    monkeypatch.setattr(bl, "persist_substrate_state", lambda *_a, **_k: {})

    runtime = PressureRuntime()
    bl.shutdown_system(runtime=runtime)
    bl.shutdown_system(runtime=runtime)

    assert calls == [1]


def test_persist_and_restore_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_path = tmp_path / "save_state.json"
    snap_path = tmp_path / "substrate_snapshot.json"
    monkeypatch.setattr("guardian.state_persistence.SAVE_PATH", save_path)
    monkeypatch.setattr(bl, "SUBSTRATE_SNAPSHOT_PATH", snap_path)

    import_pressure_field_state(
        {"accumulator": 0.42, "neuromod_tonic": {"dopamine": 0.1, "serotonin": 0.2, "stress": 0.0}}
    )

    runtime = PressureRuntime(shared_state=SharedState())
    runtime.shared_state.write(
        LAST_GLOBAL_GOALS, {"cycle_id": "c1", "goals": [{"id": "g1", "text": "test"}]}
    )
    runtime.shared_state.write("_pressure_tick", 7)

    result = bl.persist_substrate_state(runtime, reason="test")
    assert result["substrate_snapshot"].get("ok") is True
    assert save_path.is_file()

    runtime2 = PressureRuntime(shared_state=SharedState())
    restore = bl.restore_substrate_state(runtime2)
    assert restore["shared_state"].get("keys_restored", 0) >= 1
    assert runtime2.shared_state.read(LAST_GLOBAL_GOALS) is not None
    assert runtime2.shared_state.read("_pressure_tick") == 7
    assert restore["substrate_snapshot"].get("pressure_tick") == 7
    assert export_pressure_field_state()["accumulator"] == pytest.approx(0.42)
    assert read_neuromod_tonic()["dopamine"] == pytest.approx(0.1)


def test_restore_shared_state_failure_is_not_reported_as_a_cold_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args, **_kwargs):
        raise RuntimeError("shared state restore unavailable")

    monkeypatch.setattr("guardian.state_persistence.restore_at_boot", fail)

    with pytest.raises(RequiredSubstrateError) as raised:
        bl.restore_substrate_state(PressureRuntime())

    assert raised.value.component == "substrate_state"
    assert raised.value.operation == "restore"
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_degraded_shared_state_restore_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "guardian.state_persistence.restore_at_boot",
        lambda _shared_state: SimpleNamespace(
            keys_restored=0,
            keys_dropped_stale=1,
            cold_start=False,
            degradation_reason="key_write_failures",
            pressure_evidence_degraded=True,
            ok=False,
        ),
    )

    with pytest.raises(RequiredSubstrateError) as raised:
        bl.restore_substrate_state(PressureRuntime())

    assert raised.value.component == "substrate_state"
    assert raised.value.operation == "restore"
    assert raised.value.reason == "key_write_failures"


def _cold_start_report(reason: str) -> SimpleNamespace:
    return SimpleNamespace(
        keys_restored=0,
        keys_dropped_stale=0,
        cold_start=True,
        degradation_reason=reason,
        pressure_evidence_degraded=True,
        ok=True,
    )


def test_a_snapshot_past_the_hydrate_window_cold_starts_instead_of_refusing_to_boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hydrate_max_age_seconds`` is policy: an older snapshot is deliberately not
    hydrated. Refusing to boot meant Omega could not start after six hours off."""
    monkeypatch.setattr(bl, "SUBSTRATE_SNAPSHOT_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(
        "guardian.state_persistence.restore_at_boot",
        lambda _shared_state: _cold_start_report("max_age_exceeded"),
    )

    report = bl.restore_substrate_state(PressureRuntime())

    assert report["shared_state"]["cold_start"] is True
    assert report["shared_state"]["degradation_reason"] == "max_age_exceeded"
    assert report["shared_state"]["pressure_evidence_degraded"] is True


def test_a_snapshot_from_another_schema_still_refuses_to_boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bl, "SUBSTRATE_SNAPSHOT_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(
        "guardian.state_persistence.restore_at_boot",
        lambda _shared_state: _cold_start_report("schema_version_mismatch"),
    )

    with pytest.raises(RequiredSubstrateError) as raised:
        bl.restore_substrate_state(PressureRuntime())

    assert raised.value.reason == "schema_version_mismatch"


def test_corrupt_substrate_snapshot_is_not_reported_as_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "substrate_snapshot.json"
    snapshot.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(bl, "SUBSTRATE_SNAPSHOT_PATH", snapshot)
    monkeypatch.setattr(
        "guardian.state_persistence.restore_at_boot",
        lambda _shared_state: SimpleNamespace(
            keys_restored=0,
            keys_dropped_stale=0,
            cold_start=True,
            degradation_reason="no_save_file",
            pressure_evidence_degraded=True,
            ok=True,
        ),
    )

    with pytest.raises(RequiredSubstrateError) as raised:
        bl.restore_substrate_state(PressureRuntime())

    assert raised.value.component == "substrate_snapshot"
    assert raised.value.operation == "restore"
    assert isinstance(raised.value.__cause__, json.JSONDecodeError)


def test_shutdown_persistence_does_not_side_write_reflection_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reflection state is committed by DeepSleep, never by shutdown teardown."""
    calls: list[object] = []
    monkeypatch.setattr(
        "pressure_loop.reflection_window.save_reflection_window",
        lambda *_args, **_kwargs: calls.append(object()),
    )
    runtime = SimpleNamespace(
        episode_store=SimpleNamespace(
            _reflection_window=SimpleNamespace(window_id="window-live", episode_count=3),
            _reflection_window_path=tmp_path / "reflection_window.json",
        ),
        shared_state=SharedState(),
    )

    result = bl.persist_substrate_state(runtime, reason="test")

    assert calls == []
    assert result["reflection_window"] == {
        "status": "owned_by_deepsleep",
        "persisted": False,
    }


def test_boot_manifest_written_and_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bl.record_started("substrate")
    bl.record_started("hud")
    assert bl.BOOT_MANIFEST_PATH.is_file()
    payload = json.loads(bl.BOOT_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert payload["components"] == ["substrate", "hud"]

    monkeypatch.setattr(bl, "persist_substrate_state", lambda *_a, **_k: {})
    bl.shutdown_system(runtime=PressureRuntime())

    assert not bl.BOOT_MANIFEST_PATH.exists()


def test_boot_components_match_config_yaml() -> None:
    from config.loader import load_boot_config

    cfg = load_boot_config().get("boot") or {}
    configured = tuple(str(item) for item in (cfg.get("components") or []))
    assert configured
    assert bl.BOOT_COMPONENTS == configured
