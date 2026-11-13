"""Required boot and HUD work must fail with attributable substrate errors."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

import boot
from core.required_substrate import RequiredSubstrateError


def _ready_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMEGA_BRAIN_CLOUD_ONLY", raising=False)
    # the drive's sealed custody is its own substrate (tests/core/test_boot_sealed_custody.py)
    monkeypatch.setattr(boot, "_boot_sealed_custody", lambda report: report.update(custody="unbound"))
    monkeypatch.setattr("runtime.brain_cloud.enabled", lambda: False)
    monkeypatch.setattr("guardian.telemetry.emit_boot_marker", lambda: "test-run")
    monkeypatch.setattr("pressure_loop.pressure.reset_mind_primacy", lambda: None)
    monkeypatch.setattr(
        "runtime.client.get_runtime_client",
        lambda: SimpleNamespace(health=lambda: True, base_url="http://runtime.test"),
    )
    monkeypatch.setattr(
        "runtime.supervisor.ensure_runtime_process",
        lambda **_kwargs: {"ok": True, "spawned": False},
    )
    monkeypatch.setattr("memory.api.set_embedding_substrate_state", lambda *_args: None)
    monkeypatch.setattr("memory.engine.walker.load_coord_index", lambda: {})
    monkeypatch.setattr("memory.engine.walker.COORD_INDEX", {})
    monkeypatch.setattr("memory.strength_index.ensure_strength_index", lambda: object())
    monkeypatch.setattr("memory.strength_index.register_graceful_shutdown", lambda: [])
    monkeypatch.setattr(
        "memory.projection.dispatch.projection_dispatch",
        lambda *_args, **_kwargs: SimpleNamespace(
            owner="program0", action="no_work", generation_id=None
        ),
    )
    monkeypatch.setattr("memory.encoding_journal.recover_unclosed", lambda _rollback: [])
    monkeypatch.setattr("memory.queues.compact_reflection_eligible", lambda: 0)
    monkeypatch.setattr("memory.retina.encoder", lambda: SimpleNamespace(snapshot="test"))
    monkeypatch.setattr(
        "memory.retina.bound_generation",
        lambda: {"state": "empty", "generation_id": "0" * 64, "codes": 0},
    )
    monkeypatch.setattr("global_loop.intent.router.get_router", lambda: SimpleNamespace(bundle="test"))
    monkeypatch.setattr(
        "memory.capability_catalog.sync_capabilities",
        lambda: {"count": 0, "skipped_disabled": [], "failed": []},
    )
    monkeypatch.setattr("guardian.response_classes.reload", lambda: None)
    monkeypatch.setattr(
        "guardian.response_classes.status",
        lambda: {"total_classes": 0, "total_triggers": 0},
    )
    monkeypatch.setattr("guardian.terminal_console.install", lambda: None)
    monkeypatch.setattr("guardian.terminal_feedback.emit_data", lambda *_args, **_kwargs: None)


@pytest.mark.parametrize(
    ("target", "component", "operation"),
    (
        ("runtime.supervisor.ensure_runtime_process", "runtime", "startup"),
        ("memory.engine.walker.load_coord_index", "memory", "warmup"),
        ("memory.projection.dispatch.projection_dispatch", "memory_projection", "dispatch_execution"),
        ("memory.encoding_journal.recover_unclosed", "memory", "journal_recovery"),
        ("memory.queues.compact_reflection_eligible", "memory", "queue_compaction"),
        ("global_loop.intent.router.get_router", "intent_router", "load"),
        ("guardian.response_classes.reload", "guardian", "warmup"),
    ),
)
def test_required_boot_failure_never_returns_ready(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    component: str,
    operation: str,
) -> None:
    _ready_boot(monkeypatch)

    def fail(*_args, **_kwargs):
        raise RuntimeError("required work failed")

    monkeypatch.setattr(target, fail)

    with pytest.raises(RequiredSubstrateError) as raised:
        boot.boot()

    assert raised.value.component == component
    assert raised.value.operation == operation
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_runtime_not_ready_is_a_boot_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _ready_boot(monkeypatch)
    monkeypatch.setattr(
        "runtime.supervisor.ensure_runtime_process",
        lambda **_kwargs: {"ok": False, "spawned": False, "reason": "runtime_missing"},
    )

    with pytest.raises(RequiredSubstrateError) as raised:
        boot.boot()

    assert raised.value.component == "runtime"
    assert raised.value.operation == "startup"
    assert raised.value.reason == "runtime_missing"


def test_semantic_retina_mapping_failure_is_not_reported_as_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_boot(monkeypatch)

    def fail(*_args, **_kwargs):
        raise RuntimeError("DME mapping unavailable")

    monkeypatch.setattr("memory.retina.bound_generation", fail)

    with pytest.raises(RequiredSubstrateError) as raised:
        boot.boot()

    assert raised.value.component == "memory_retina"
    assert raised.value.operation == "map"
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_projection_dispatch_failure_is_not_reported_as_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_boot(monkeypatch)

    def fail(*_args, **_kwargs):
        raise RuntimeError("projection dispatch unavailable")

    monkeypatch.setattr("memory.projection.dispatch.projection_dispatch", fail)

    with pytest.raises(RequiredSubstrateError) as raised:
        boot.boot()

    assert raised.value.component == "memory_projection"
    assert raised.value.operation == "dispatch_execution"
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_failed_program0_report_is_not_reported_as_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_boot(monkeypatch)

    monkeypatch.setattr(
        "memory.projection.dispatch.projection_dispatch",
        lambda *_args, **_kwargs: SimpleNamespace(
            owner="program0",
            action="failed",
            generation_id=None,
            drain_status="blocked_on_obligations",
            detail={"reason": "blocked_on_obligations", "unclosed_count": 1},
        ),
    )

    with pytest.raises(RequiredSubstrateError) as raised:
        boot.boot()

    assert raised.value.component == "memory_projection"
    assert raised.value.operation == "dispatch_execution"
    assert raised.value.reason == "blocked_on_obligations"
    assert "unclosed_count" in raised.value.detail


def test_lifecycle_interlock_failure_is_not_reported_as_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_boot(monkeypatch)

    def fail(*_args, **_kwargs):
        raise RuntimeError("lifecycle interlock unavailable")

    monkeypatch.setattr("memory.projection.dispatch.hold_lifecycle_lock", fail)

    with pytest.raises(RequiredSubstrateError) as raised:
        boot.boot()

    assert raised.value.component == "lifecycle"
    assert raised.value.operation == "lock"
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_omega_overlay_reset_failure_is_not_reported_as_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_boot(monkeypatch)
    import pressure_loop.pressure as pressure

    original_reset = pressure.reset_mind_primacy
    failed_once = False

    def fail(*_args, **_kwargs):
        nonlocal failed_once
        if failed_once:
            return original_reset()
        failed_once = True
        raise RuntimeError("stale pressure state could not be reset")

    monkeypatch.setattr("pressure_loop.pressure.reset_mind_primacy", fail)

    with pytest.raises(RequiredSubstrateError) as raised:
        boot.boot()

    assert raised.value.component == "pressure"
    assert raised.value.operation == "overlay_reset"
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_deepsleep_program0_publication_failure_is_attributed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepsleep_loop.loop as deepsleep

    def fail(*_args, **_kwargs):
        raise RuntimeError("post-DeepSleep projection unavailable")

    monkeypatch.setattr("memory.projection.dispatch.projection_dispatch", fail)

    with pytest.raises(RequiredSubstrateError) as raised:
        deepsleep._publish_projection_after_deepsleep(
            context_id="episode-projection-publish",
            scope="episode",
        )

    assert raised.value.component == "memory_projection"
    assert raised.value.operation == "dispatch_execution"
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_deepsleep_source_validity_rebuild_failure_is_attributed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.paths as core_paths
    import deepsleep_loop.loop as deepsleep

    monkeypatch.setattr(core_paths, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(core_paths, "OMEGA_ROOT", tmp_path)
    def fail(*_args, **_kwargs):
        raise RuntimeError("source validity rebuild unavailable")

    monkeypatch.setattr("memory.source_validity.rebuild_from_canonical", fail)

    with pytest.raises(RequiredSubstrateError) as raised:
        deepsleep._maintain_projection_inputs_after_deepsleep(
            context_id="episode-source-validity",
            scope="episode",
        )

    assert raised.value.component == "memory_source_validity"
    assert raised.value.operation == "rebuild"
    assert isinstance(raised.value.__cause__, RuntimeError)


@pytest.mark.parametrize(
    ("helper", "target", "component"),
    (
        (
            "_compact_episode_temporal_summary",
            "core.episode_temporal.compact_temporal_summary",
            "episode_temporal",
        ),
        (
            "_compact_episode_learning_state_summary",
            "pressure_loop.symphony_learning_state.compact_learning_state_summary",
            "learning_state",
        ),
    ),
)
def test_deepsleep_summary_compaction_failure_is_attributed(
    monkeypatch: pytest.MonkeyPatch,
    helper: str,
    target: str,
    component: str,
) -> None:
    import deepsleep_loop.loop as deepsleep

    def fail(*_args, **_kwargs):
        raise RuntimeError("summary compaction unavailable")

    monkeypatch.setattr(target, fail)

    with pytest.raises(RequiredSubstrateError) as raised:
        getattr(deepsleep, helper)({"required": True})

    assert raised.value.component == component
    assert raised.value.operation == "compact"
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_configured_cloud_seat_boots_without_starting_local_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_boot(monkeypatch)
    monkeypatch.setattr("runtime.brain_cloud.enabled", lambda: True)
    monkeypatch.setenv("OMEGA_BRAIN_CLOUD_ONLY", "1")
    states: list[tuple[str, str]] = []

    def unexpected_runtime(*_args, **_kwargs):
        raise AssertionError("cloud cognition must not launch or probe local inference")

    monkeypatch.setattr("runtime.client.get_runtime_client", unexpected_runtime)
    monkeypatch.setattr("runtime.supervisor.ensure_runtime_process", unexpected_runtime)
    monkeypatch.setattr(
        "memory.api.set_embedding_substrate_state",
        lambda state, detail: states.append((state, detail)),
    )

    report = boot.boot()

    assert report["runtime"] == "cloud"
    assert states[0][0] == "degraded"
    assert "embedding" in states[0][1]
    assert report["memory"] == "up"
    assert report["guardian"] == "up"
    assert report["embedding_worker"] == "deferred"


def test_cloud_cognition_keeps_local_services_unless_cloud_only_is_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_boot(monkeypatch)
    monkeypatch.setattr("runtime.brain_cloud.enabled", lambda: True)

    report = boot.boot()

    assert report["runtime"] == "up"
    assert report["embedding_worker"] == "retina"  # in-process encoder; the worker seat is retired


def test_cloud_only_without_a_usable_seat_fails_before_local_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_boot(monkeypatch)
    monkeypatch.setenv("OMEGA_BRAIN_CLOUD_ONLY", "1")
    local_calls: list[str] = []
    monkeypatch.setattr(
        "runtime.supervisor.ensure_runtime_process",
        lambda **_kw: local_calls.append("start") or {"ok": True},
    )

    with pytest.raises(RequiredSubstrateError) as raised:
        boot.boot()

    assert raised.value.reason == "cloud_seat_unavailable"
    assert local_calls == []


def test_hud_thread_start_failure_is_not_reported_as_no_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hud.spawner as spawner

    uvicorn = ModuleType("uvicorn")
    uvicorn.Config = lambda **_kwargs: object()
    uvicorn.Server = lambda _config: SimpleNamespace()
    hud_server = ModuleType("hud.server")
    hud_server.app = object()

    class BrokenThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self) -> None:
            raise RuntimeError("thread refused")

    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setitem(sys.modules, "hud.server", hud_server)
    monkeypatch.setattr(spawner.threading, "Thread", BrokenThread)
    monkeypatch.setattr(spawner, "_thread", None)
    monkeypatch.setattr(spawner, "_server", None)
    monkeypatch.setattr(spawner, "_disabled_reason", None)
    monkeypatch.delenv("OMEGA_HUD_DISABLE", raising=False)

    with pytest.raises(RequiredSubstrateError) as raised:
        spawner.spawn_hud_in_thread()

    assert raised.value.component == "hud"
    assert raised.value.operation == "thread_start"
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_hud_listener_must_reach_ready_before_returning_a_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hud.spawner as spawner

    uvicorn = ModuleType("uvicorn")
    uvicorn.Config = lambda **_kwargs: object()
    uvicorn.Server = lambda _config: SimpleNamespace(started=False)
    hud_server = ModuleType("hud.server")
    hud_server.app = object()

    class DeadThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False

    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setitem(sys.modules, "hud.server", hud_server)
    monkeypatch.setattr(spawner.threading, "Thread", DeadThread)
    monkeypatch.setattr(spawner, "_thread", None)
    monkeypatch.setattr(spawner, "_server", None)
    monkeypatch.setattr(spawner, "_disabled_reason", None)
    monkeypatch.delenv("OMEGA_HUD_DISABLE", raising=False)

    with pytest.raises(RequiredSubstrateError) as raised:
        spawner.spawn_hud_in_thread()

    assert raised.value.component == "hud"
    assert raised.value.operation == "listener_start"
    assert raised.value.reason == "thread_exited_before_ready"
