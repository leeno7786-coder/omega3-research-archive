"""Boot lifecycle — ordered start manifest + reverse teardown.

Boot order (``run_continuous``):
  boot_lock → pressure policy → substrate → MCP host → cognition runtime → HUD
  → pressure loop

Shutdown is the exact reverse: stop pressure loop → HUD → cognition runtime
(no-op) → MCP host → persist substrate state → runtime process → release boot
lock. Idempotent — safe from ``finally``, signal hooks, and
``scripts/shutdown.py`` (via SIGINT/CTRL_BREAK into the boot process).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

from core.paths import GUARDIAN_STATE_ROOT

log = logging.getLogger("omega.core.boot_lifecycle")

_DEFAULT_BOOT_COMPONENTS: tuple[str, ...] = (
    "boot_lock",
    "runtime_process",
    "substrate",
    "mcp_host",
    "cognition_runtime",
    "hud",
    "pressure_loop",
)


def _boot_components_from_config() -> tuple[str, ...]:
    try:
        from config.loader import load_subsystem_bundle

        raw = (load_subsystem_bundle("boot").get("config") or {}).get("boot") or {}
        raw = raw.get("components")
        if isinstance(raw, list) and raw:
            return tuple(str(item) for item in raw)
    except Exception as exc:
        log.debug("boot components from config failed: %s", exc)
    return _DEFAULT_BOOT_COMPONENTS


BOOT_COMPONENTS: tuple[str, ...] = _boot_components_from_config()

BOOT_MANIFEST_PATH = GUARDIAN_STATE_ROOT / "boot.manifest.json"
SUBSTRATE_SNAPSHOT_PATH = GUARDIAN_STATE_ROOT / "substrate_snapshot.json"

_started: list[str] = []
_shutdown_done = False
_lifecycle_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def reset_lifecycle_state() -> None:
    """Test helper — clear in-process lifecycle flags.

    Also the LIVE path: ``run_continuous`` calls it before every boot, which is
    what lets the teardown halt flag be one-way within a boot and still leave a
    re-boot in the same process (and every test after a shutdown test) unhalted.
    """
    global _shutdown_done
    with _lifecycle_lock:
        _started.clear()
        _shutdown_done = False
    try:
        from core.cognition_halt import reset_for_tests as _reset_halt

        _reset_halt()
    except Exception:  # noqa: BLE001 — a reset never raises
        log.debug("cognition halt reset skipped", exc_info=True)


def record_started(component: str) -> None:
    """Record a boot component that completed startup."""
    name = str(component or "").strip()
    if not name:
        return
    with _lifecycle_lock:
        if name not in _started:
            _started.append(name)
        _write_boot_manifest()


def started_components() -> list[str]:
    with _lifecycle_lock:
        return list(_started)


def shutdown_in_progress() -> bool:
    """True once ``shutdown_system`` has begun (it latches before it tears down).

    Public because supervision threads need it: the gateway watchdog must never
    resurrect a gateway that a deliberate shutdown is on its way to killing, and
    the flag is set before the component walk starts, so reading it here closes
    that whole window rather than just the instant of the kill.
    """
    with _lifecycle_lock:
        return _shutdown_done


def _write_boot_manifest() -> None:
    try:
        BOOT_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "started_at": _now_iso(),
            "components": list(_started),
        }
        tmp = BOOT_MANIFEST_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, BOOT_MANIFEST_PATH)
    except OSError as exc:
        log.debug("boot manifest write skipped (%s)", exc)


def _clear_boot_manifest() -> None:
    try:
        BOOT_MANIFEST_PATH.unlink(missing_ok=True)
    except OSError as exc:
        log.debug("boot manifest clear skipped (%s)", exc)


def restore_substrate_state(runtime: Any) -> dict[str, Any]:
    """Hydrate durable substrate state after ``PressureRuntime`` is registered.

    A failed restore is not a cold start. Treating an unreadable or partially
    applied snapshot as an empty substrate lets cognition continue with a
    state the operator did not actually approve. The explicit cold-start cases
    remain valid: no prior save exists, policy deliberately disabled shared-state
    restoration, or the snapshot is older than policy's hydrate window
    (``hydrate_max_age_seconds``) and is deliberately not hydrated -- refusing to
    boot there meant Omega could not start after being off for six hours. The
    cold start still reports ``pressure_evidence_degraded``.
    """
    from core.required_substrate import RequiredSubstrateError

    report: dict[str, Any] = {"shared_state": {}, "substrate_snapshot": {}}
    try:
        from guardian.state_persistence import restore_at_boot

        restore_report = restore_at_boot(runtime.shared_state)
        report["shared_state"] = {
            "keys_restored": restore_report.keys_restored,
            "keys_dropped_stale": restore_report.keys_dropped_stale,
            "cold_start": restore_report.cold_start,
            "degradation_reason": restore_report.degradation_reason,
            "pressure_evidence_degraded": restore_report.pressure_evidence_degraded,
        }
        if restore_report.cold_start:
            log.info(
                "substrate restore: cold start (%s)",
                restore_report.degradation_reason or "no prior save",
            )
        else:
            log.info(
                "substrate restore: %d shared_state keys hydrated",
                restore_report.keys_restored,
            )
    except Exception as exc:
        failure = RequiredSubstrateError.from_exception(
            "substrate_state",
            "restore",
            exc,
        )
        log.critical("substrate shared_state restore failed: %s", failure)
        raise failure from exc

    degradation_reason = str(getattr(restore_report, "degradation_reason", "") or "")
    cold_start = bool(getattr(restore_report, "cold_start", False))
    restore_ok = bool(getattr(restore_report, "ok", True))
    dropped = int(getattr(restore_report, "keys_dropped_stale", 0) or 0)
    corrupt_keys = list(getattr(restore_report, "corrupt_keys", []) or [])
    allowed_cold_start = {"", "no_save_file", "restore_disabled_by_policy", "max_age_exceeded"}
    if (
        not restore_ok
        or dropped
        or corrupt_keys
        or (cold_start and degradation_reason not in allowed_cold_start)
    ):
        reason = degradation_reason or "restore_degraded"
        detail = ""
        if corrupt_keys:
            detail = "corrupt_keys=" + ",".join(map(str, corrupt_keys))
        elif dropped:
            detail = f"keys_dropped_stale={dropped}"
        failure = RequiredSubstrateError(
            "substrate_state",
            "restore",
            reason=reason,
            detail=detail,
        )
        log.critical("substrate shared_state restore did not commit cleanly: %s", failure)
        raise failure

    try:
        if SUBSTRATE_SNAPSHOT_PATH.is_file():
            raw = json.loads(SUBSTRATE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("substrate snapshot root must be an object")
            from pressure_loop.pressure_field import import_pressure_field_state

            import_pressure_field_state(raw.get("pressure_field") or {})
            tick = raw.get("pressure_tick")
            if tick is not None:
                try:
                    normalized_tick = int(tick)
                except (TypeError, ValueError) as exc:
                    raise ValueError("substrate snapshot pressure_tick is not an integer") from exc
                runtime.shared_state.write("_pressure_tick", normalized_tick)
                runtime.shared_state.write("_epoch", normalized_tick)
            report["substrate_snapshot"] = {
                "restored": True,
                "saved_at": raw.get("saved_at"),
                "pressure_tick": tick,
            }
    except Exception as exc:
        failure = RequiredSubstrateError.from_exception(
            "substrate_snapshot",
            "restore",
            exc,
        )
        log.critical("substrate snapshot restore failed: %s", failure)
        raise failure from exc

    return report


def persist_substrate_state(runtime: Any, *, reason: str = "shutdown") -> dict[str, Any]:
    """Flush durable substrate state before process exit."""
    result: dict[str, Any] = {
        "reason": reason,
        "shared_state": {},
        "strength_index": {},
        "reflection_window": {
            "status": "owned_by_deepsleep",
            "persisted": False,
        },
        "substrate_snapshot": {},
    }

    try:
        from memory.strength_index import _flush_singleton

        _flush_singleton(reason)
        result["strength_index"] = {"ok": True}
    except Exception as exc:
        log.warning("strength index flush at shutdown failed: %s", exc)
        result["strength_index"] = {"ok": False, "error": str(exc)}

    try:
        from guardian.state_persistence import save_now

        tick = runtime.shared_state.read("_pressure_tick", 0)
        save_result = save_now(runtime.shared_state, cycle_id=str(tick or ""), force=True)
        result["shared_state"] = {
            "ok": save_result.ok,
            "keys_saved": save_result.keys_saved,
            "bytes_written": save_result.bytes_written,
            "error_kind": save_result.error_kind,
        }
    except Exception as exc:
        log.warning("shared_state save at shutdown failed: %s", exc)
        result["shared_state"] = {"ok": False, "error": str(exc)}

    try:
        from pressure_loop.pressure_field import export_pressure_field_state

        payload = {
            "schema_version": 1,
            "saved_at": _now_iso(),
            "reason": reason,
            "pressure_field": export_pressure_field_state(),
            "pressure_tick": runtime.shared_state.read("_pressure_tick"),
        }
        SUBSTRATE_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SUBSTRATE_SNAPSHOT_PATH.with_suffix(".json.tmp")
        body = json.dumps(payload, indent=2, ensure_ascii=False)
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, SUBSTRATE_SNAPSHOT_PATH)
        result["substrate_snapshot"] = {"ok": True, "bytes": len(body.encode("utf-8"))}
    except Exception as exc:
        log.warning("substrate snapshot save at shutdown failed: %s", exc)
        result["substrate_snapshot"] = {"ok": False, "error": str(exc)}

    return result


# How long teardown waits for a cascade already in flight. Measured on this
# appliance 2026-08-11: the longest post-SIGTERM tail was 72s (14:14:30 gateway
# gone → 14:15:42 exit), but ~34s of that was one embed request burning the
# local-resident retry envelope against a dead worker — which the halt flag now
# collapses to a single attempt. What is left is a deepsleep terminus plus a
# memory commit, both of which land in seconds. 30s is generous for that and
# still leaves the unit's TimeoutStopSec=330 budget almost entirely to the
# substrate persist and the 90s sweep.
_LANE_TEARDOWN_TIMEOUT_S = 30.0


def _stop_cognition_threads(timeout_s: float = _LANE_TEARDOWN_TIMEOUT_S) -> dict[str, Any]:
    """Stop the threads that USE the runtime, before anything kills the runtime.

    RISK 4(b), operator 2026-08-11. Teardown's order was reverse-of-boot, which
    is right for components and wrong for this: the lane executors are not a
    boot component (they are created on first dispatch, not at start), so
    nothing stopped them, and the reverse walk's runtime_process step plus the
    final sweep took the gateway and the llama-servers out from under cascades
    that were still running. Measured: SIGTERM 14:14:22, gateway gone 14:14:30,
    the agent lane still folding back at 14:15:42 — every model pass, broker
    check, deepsleep phase and embed backfill in that 72-second tail talking to
    processes that no longer existed, and systemd holding the unit open for all
    of it.

    Three things happen here, in this order and for this reason:

      1. the pressure loop is asked to stop, so no NEW cascade is dispatched
         behind our backs while we are draining the ones in flight. Idempotent —
         the signal handler and the reverse walk's own pressure_loop step both
         call it too, and it is the cheapest of the three.
      2. the halt flag is set (``core.cognition_halt``), so a cascade waiting on
         an embed learns that nothing is coming back instead of spending 33.5s
         of backoff per request on a worker that is about to be killed.
      3. the lane executors are shut down and their threads JOINED, bounded.

    Bounded is the contract, not an optimization: a cascade that will not finish
    must not be able to wedge the shutdown, exactly as a failed or hung model
    unload has never been able to. Past the deadline this returns, names what is
    still running, and teardown proceeds to kill the workers anyway — the same
    outcome as before this fix, but reached deliberately and only after the
    threads were given an honest chance to land their durable work first.
    """
    report: dict[str, Any] = {}
    try:
        from pressure_loop.loop import request_pressure_loop_stop

        request_pressure_loop_stop()
        report["pressure_loop_stop"] = True
    except Exception as exc:  # noqa: BLE001 — teardown never raises
        log.warning("pressure loop stop request failed: %s", exc)
        report["pressure_loop_stop"] = False

    try:
        from pressure_loop.pipeline_dispatch import shutdown_pipelines

        report["pipelines"] = shutdown_pipelines(timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 — teardown never raises
        log.warning("lane executor teardown failed: %s", exc)
        report["pipelines"] = {"error": str(exc)}
        # The lane shutdown normally sets it; if that path blew up, the flag
        # still has to be set before the sweep or the storm comes back.
        try:
            from core.cognition_halt import request_halt

            request_halt("runtime_teardown")
        except Exception:  # noqa: BLE001
            log.debug("cognition halt request failed", exc_info=True)

    log.info("cognition threads stopped before the runtime sweep: %s", report)
    return report


def _release_in_process_model_seats() -> dict[str, Any]:
    """Release model seats held by the brain process before child teardown."""
    released: list[str] = []
    failures: list[Exception] = []
    operations = (
        ("retina", "memory.retina", "reset_encoder"),
        ("ettin_cache", "memory.ettin_encoder", "reset_encoder_cache"),
        ("intent_router", "global_loop.intent.router", "reset_router_cache"),
        ("dme", "memory.dme", "shutdown_runtime"),
    )
    for name, module_name, function_name in operations:
        try:
            module = __import__(module_name, fromlist=[function_name])
            getattr(module, function_name)()
            released.append(name)
        except Exception as exc:  # noqa: BLE001 - every seat gets an attempt
            failure = RuntimeError(f"{name} model-seat release failed")
            failure.__cause__ = exc
            failures.append(failure)
    report = {"released": released}
    if failures:
        raise ExceptionGroup("in-process model-seat release failed", failures)
    return report


def _release_browser() -> None:
    """Release Omega's DevTools connection and tabs if this process ever attached one."""
    import sys

    executor = sys.modules.get("auto_loop.execute.brain_skill_executor")
    if executor is not None:
        executor.release_browser_controller()


# The stops the operator (or the loop itself) intends. Everything else — ``error:<Type>``
# from an unhandled cascade exception, ``*_failure`` from a boot that never came up — is an
# unclean end even though the teardown still runs.
CLEAN_SHUTDOWN_REASONS = frozenset({"shutdown", "keyboard_interrupt", "pressure_loop_stop"})


def shutdown_is_clean(reason: str) -> bool:
    """Whether a shutdown with this reason was an intended stop."""
    return str(reason or "") in CLEAN_SHUTDOWN_REASONS


def shutdown_system(
    *,
    reason: str = "shutdown",
    runtime: Any | None = None,
) -> dict[str, Any]:
    """Tear down every in-process boot component in reverse start order."""
    global _shutdown_done
    with _lifecycle_lock:
        if _shutdown_done:
            return {"status": "already_shutdown"}
        _shutdown_done = True
        started = list(_started)

    summary: dict[str, Any] = {"reason": reason, "steps": [], "started": started}

    def _step(name: str, fn) -> None:
        try:
            fn()
            summary["steps"].append({"component": name, "ok": True})
        except Exception as exc:
            log.warning("shutdown step %s failed: %s", name, exc)
            summary["steps"].append({"component": name, "ok": False, "error": str(exc)})

    # Lifetime clock (operator 2026-07-16, corrected 2026-09-15): the stamp is the FIRST
    # step, not the last. A console close or SIGBREAK gives the process seconds before
    # Windows kills it, and the teardown below can take longer than that, so a real stop
    # used to read UNCLEAN on the next boot while a crash, which walks the whole teardown at
    # leisure, read clean. Cleanliness is the reason's: an intended stop is clean, a crash
    # or a start failure is not. Gated on a real live boot (boot_lock is acquired only by
    # run_continuous), so tests and one-shot runs never touch the persisted clock. Session
    # uptime folds into lifetime_seconds either way.
    if "boot_lock" in started:

        def _record_clock() -> None:
            import time as _time

            from autonomic.temporal_grounding.temporal_persistence import (
                load_state,
                record_shutdown,
            )

            now = _time.time()
            last_boot = load_state().get("last_boot_at")
            session_seconds = (
                max(0.0, now - float(last_boot)) if isinstance(last_boot, (int, float)) else 0.0
            )
            record_shutdown(now=now, session_seconds=session_seconds, clean=shutdown_is_clean(reason))

        _step("temporal_clock", _record_clock)

    # BEFORE the component walk, not inside it — RISK 4(b), see
    # _stop_cognition_threads. Everything below this line takes workers away:
    # the runtime_process step stops the gateway, the sweep force-kills the
    # llama-servers. The threads that USE those workers have to be off first,
    # and they are not a boot component (no lane executor is "started", they
    # are created on first dispatch), so their stop cannot ride the reverse
    # walk. Failure-soft like every other step: _step swallows, teardown goes on.
    _step(
        "cognition_threads",
        lambda: summary.__setitem__("cognition_threads", _stop_cognition_threads()),
    )

    # Omega's browser session belongs to this process only (operator 2026-09-14:
    # "when shutdown or crashed, omega needs to release the browser"). Cognition has
    # stopped, so no step is mid-action; release it before the HUD and runtime go.
    _step("browser", _release_browser)

    # In-process seats are not boot components, so they do not appear in the
    # reverse walk. Release them after cognition has stopped and before the
    # child runtime is killed; this covers the semantic retina and DME globals.
    _step(
        "model_seats",
        lambda: summary.__setitem__(
            "model_seats", _release_in_process_model_seats()
        ),
    )

    reverse = [c for c in reversed(BOOT_COMPONENTS) if c in started]

    for component in reverse:
        if component == "pressure_loop":

            def _stop_loop() -> None:
                from pressure_loop.loop import request_pressure_loop_stop

                request_pressure_loop_stop()
                log.info("pressure loop: stop requested")

            _step("pressure_loop", _stop_loop)
        elif component == "hud":

            def _stop_hud() -> None:
                from hud.spawner import shutdown_hud

                shutdown_hud()

            _step("hud", _stop_hud)
        elif component == "cognition_runtime":
            _step("cognition_runtime", lambda: None)
        elif component == "mcp_host":

            def _stop_mcp_host() -> None:
                from mcp_platform.service import shutdown_default_mcp_host

                counts = shutdown_default_mcp_host()
                summary["mcp_host"] = counts
                if counts.get("sessions_forced", 0) or counts.get("errors", 0):
                    raise RuntimeError(
                        "MCP host shutdown forced sessions or reported errors"
                    )

            _step("mcp_host", _stop_mcp_host)
        elif component == "substrate":

            def _persist() -> None:
                if runtime is None:
                    from pressure_loop.runtime import try_get_default_runtime

                    rt = try_get_default_runtime()
                else:
                    rt = runtime
                if rt is not None:
                    persisted = persist_substrate_state(rt, reason=reason)
                    summary["persist"] = persisted
                    failed = [
                        name
                        for name in ("strength_index", "shared_state", "substrate_snapshot")
                        if isinstance(persisted.get(name), dict)
                        and persisted[name].get("ok") is False
                    ]
                    if failed:
                        raise RuntimeError(
                            "substrate persistence incomplete: " + ", ".join(failed)
                        )

            _step("substrate", _persist)
        elif component == "runtime_process":

            def _stop_runtime() -> None:
                from runtime.supervisor import (
                    shutdown_runtime_process,
                    stop_gateway_watchdog,
                )

                # Stop the keeper before killing what it keeps. shutdown_runtime_process
                # latches the same stop event itself, so this is belt-and-braces for the
                # thread join — but the ORDER is the load-bearing part.
                stop_gateway_watchdog()
                summary["runtime_process"] = shutdown_runtime_process()
                log.info("runtime gateway teardown: %s", summary["runtime_process"])

            _step("runtime_process", _stop_runtime)
        elif component == "boot_lock":

            def _release_lock() -> None:
                from core.boot_lock import release

                release()

            _step("boot_lock", _release_lock)
        # cognition_runtime (model residency) deliberately records a no-op step.
        # The sweep below reclaims every seat by killing the processes holding
        # them, which also reaps workers whose gateway already died.

    _clear_boot_manifest()

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "boot",
            "shutdown",
            reason=reason,
            components=reverse,
            summary=f"shutdown complete ({reason})",
        )
    except Exception:
        pass

    log.info("Omega substrate shutdown complete (%s)", reason)
    return summary
