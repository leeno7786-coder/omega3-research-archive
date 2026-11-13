"""Omega 3.0 substrate boot — pressure loop + readiness warmup.

Boots: logging, PressureRuntime (episode store + shared_state), runtime
health probe, memory/guardian warmup, in-process HUD (fail-closed),
then pressure_loop.
Does NOT boot BrainLoop or legacy orchestrator/handlers.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.paths import OMEGA_ROOT
from core.required_substrate import RequiredSubstrateError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Every moving part gets its own file in guardian/logs/<logger.name>.log —
# one root-level demux handler, zero call-site changes (failure-soft).
try:
    from guardian.log_router import install_component_logging

    install_component_logging()
except Exception:  # logging must never block boot
    pass

log = logging.getLogger("omega.boot")


def _set_boot_status(phase: str, percent: int, detail: str) -> None:
    """Publish startup progress for the portable setup window.

    Boot status is setup UI observability, not cognition state. A status-write
    problem must remain visible in the normal boot log without preventing the
    substrate from starting.
    """
    try:
        from core.boot_status import write_boot_status

        write_boot_status(
            OMEGA_ROOT,
            phase=phase,
            percent=percent,
            detail=detail,
            pid=os.getpid(),
        )
    except Exception as exc:  # noqa: BLE001 - setup feedback cannot gate boot
        # During boot (percent < 100), warn so operator sees diagnostic info
        # for disk/permission issues. After boot complete, debug is sufficient.
        if percent < 100:
            log.warning("boot status update failed (%s): %s", phase, exc)
        else:
            log.debug("boot status update failed (%s): %s", phase, exc)



def _boot_semantic_retina(report: dict[str, Any]) -> None:
    """Seat the Ettin retina for the session and verify the active generation's codes.

    Recall binds each read transaction to its own generation's codes, so boot maps
    nothing process-wide. It proves the published generation's semantic plane is
    either verified under the pinned encoder contract or declared empty; missing,
    corrupt or foreign codes fail boot rather than reporting a healthy retina.
    """
    try:
        from memory import retina

        encoder = retina.encoder()
        report["retina"] = "up"
        log.info("Semantic retina: Ettin resident (%s)", getattr(encoder, "snapshot", "pinned"))
    except Exception as exc:
        report["retina"] = "down"
        failure = RequiredSubstrateError.from_exception("memory_retina", "load", exc)
        raise failure from exc
    try:
        from memory.projection.generation import projection_read_transaction

        with projection_read_transaction(None):
            bound = retina.bound_generation()
        log.info("Semantic retina: generation %s %s (%d codes)", bound["generation_id"][:12], bound["state"], bound["codes"])
    except Exception as exc:  # noqa: BLE001 — verified codes are required for recall readiness
        report["retina"] = "down"
        failure = RequiredSubstrateError.from_exception("memory_retina", "map", exc)
        raise failure from exc
    try:
        from memory.api import set_embedding_substrate_state
        from memory.ettin_encoder import ETTIN_MODEL_ID

        detail = (
            f"Ettin resident ({ETTIN_MODEL_ID}); generation={bound['generation_id'][:12]}; "
            f"codes={bound['codes']}" + (" (empty semantic corpus)" if bound["state"] == "empty" else "")
        )
        set_embedding_substrate_state("ok", detail)
        log.info("Memory semantic substrate reported ok (%s)", detail)
    except Exception as exc:
        failure = RequiredSubstrateError.from_exception(
            "memory",
            "embedding_substrate_state",
            exc,
        )
        raise failure from exc


def _boot_intent_router(report: dict[str, Any]) -> None:
    """Seat Global A's intent router for the session.

    The promoted ModernBERT router is verified against config/intent_router.pin.json and
    its ONNX session is created once, here, so the first operator turn is routed without
    a load. Every operator turn crosses this seat, so a router that cannot load fails
    boot closed instead of leaving Global A unable to choose an episode route.
    """
    try:
        from global_loop.intent.router import get_router

        router = get_router()
    except Exception as exc:
        report["intent_router"] = "down"
        failure = RequiredSubstrateError.from_exception("intent_router", "load", exc)
        raise failure from exc
    report["intent_router"] = "up"
    log.info("Intent router: ModernBERT-base resident (%s)", router.bundle)


def _boot_projection_and_recovery(report: dict[str, Any]) -> None:
    """Recover canonical writes, then let Program 0 replay and publish the active view.

    Recovery precedes publication so the first generation reflects any canonical rollback.
    Program 0 also resumes interrupted generation intents and drains the outbox under one
    publication authority; no fixed-name compiler runs at boot.

    OMEGA_BRAIN_CLOUD_ONLY=1 is the explicit cloud-only integration mode: it requires a
    configured cloud cognition seat and defers the local runtime gateway. Normal cloud
    cognition still starts the local runtime for the other modalities."""
    try:
        from memory.encoding_journal import recover_unclosed
        from memory.rollback import rollback_canonical_write

        recovered = recover_unclosed(rollback_canonical_write) or []
        report["encoding_journal_recovery"] = len(recovered)
        if recovered:
            log.warning(
                "encoding_journal boot recovery: rolled back %d unclosed entries",
                len(recovered),
            )
    except RequiredSubstrateError:
        report["encoding_journal_recovery"] = "error"
        raise
    except Exception as exc:
        report["encoding_journal_recovery"] = "error"
        failure = RequiredSubstrateError.from_exception(
            "memory",
            "journal_recovery",
            exc,
        )
        raise failure from exc

    try:
        from memory.projection.dispatch import projection_dispatch

        dispatch = projection_dispatch(reason="boot")
        report["projection_owner"] = dispatch.owner
        report["projection"] = f"{dispatch.owner}:{dispatch.action}"
        if dispatch.action == "failed":
            raise RequiredSubstrateError(
                "memory_projection",
                "dispatch_execution",
                reason=dispatch.drain_status or "failed",
                detail=str(dispatch.detail or ""),
            )
        report["graph"] = "up"
        log.info(
            "Projection ready: owner=%s action=%s generation=%s",
            dispatch.owner,
            dispatch.action,
            (dispatch.generation_id or "")[:12],
        )
    except RequiredSubstrateError:
        report["graph"] = "down"
        raise
    except Exception as exc:  # noqa: BLE001 — required publication cannot degrade readiness
        report["graph"] = "down"
        failure = RequiredSubstrateError.from_exception(
            "memory_projection",
            "dispatch_execution",
            exc,
        )
        report["projection"] = f"program0:failed:{failure.reason}"
        log.critical("projection dispatch failed at boot: %s", failure)
        raise failure from exc


def _boot_local_runtime(report: dict[str, Any]) -> None:
    """Spawn/own the local runtime gateway (the normal, non-cloud-only boot)."""
    try:
        from runtime.client import get_runtime_client

        client = get_runtime_client()
        # The brain owns its runtime (operator 2026-07-19): if the gateway is
        # down, spawn it here — that brings up every organ behind it (workers,
        # whisper FLM sidecar) — and shutdown_system tears down what WE spawned.
        # An operator-started runtime is left alone. A requested runtime that
        # cannot start is not a smaller valid Omega configuration.
        from runtime.supervisor import ensure_runtime_process

        spawn_report = ensure_runtime_process(client=client)
        if not spawn_report.get("ok"):
            raise RequiredSubstrateError(
                "runtime",
                "startup",
                reason=str(spawn_report.get("reason") or "not_ready"),
                detail=str(spawn_report.get("detail") or ""),
            )
        if spawn_report.get("spawned"):
            from core.boot_lifecycle import record_started

            record_started("runtime_process")
            log.info(
                "Runtime gateway spawned by brain (pid=%s) — teardown owned",
                spawn_report.get("pid"),
            )

        ready = client.health()
        if not ready:
            raise RequiredSubstrateError(
                "runtime",
                "health_probe",
                reason="not_ready",
                detail=str(client.base_url),
            )
        report["runtime"] = "up"
        report["embedding_worker"] = "retina"  # the in-process Ettin encoder; no worker seat
        log.info("Runtime model server ready at %s", client.base_url)
    except RequiredSubstrateError:
        raise
    except Exception as exc:
        failure = RequiredSubstrateError.from_exception(
            "runtime",
            "startup",
            exc,
        )
        raise failure from exc


def _boot_sealed_custody(report: dict[str, Any]) -> None:
    """Fail closed, early and attributably, when the drive's sealed custody cannot open.

    Omega is portable: the OAuth vault key and the release signing seeds live in
    passphrase-sealed files under guardian/state (``mcp_platform.sealed_secrets``).
    On a machine without ``OMEGA_SEAL_PASSPHRASE`` (or ``_FILE``) every later
    consumer -- plugin grant reconciliation, the HUD's lifecycle service -- would
    surface a deep ``oauth_vault_locked``; this names the real cause before any
    of them run. A DPAPI-held or not-yet-born vault needs no passphrase.
    """
    from core import paths as core_paths
    from mcp_platform import sealed_secrets

    sealed_vault_key = core_paths.MCP_AUTHORIZATION_ROOT / sealed_secrets.VAULT_KEY_FILENAME
    if not sealed_vault_key.exists():
        report["custody"] = "desktop" if (
            core_paths.MCP_AUTHORIZATION_ROOT / "oauth-vault-key.dpapi"
        ).exists() else "unbound"
        return
    try:
        passphrase = sealed_secrets.passphrase_from_environment()
        if passphrase is None:
            raise RequiredSubstrateError(
                "custody",
                "startup",
                reason="sealed_passphrase_required",
                detail=(
                    f"{sealed_vault_key} is passphrase-sealed; set "
                    f"{sealed_secrets.PASSPHRASE_ENV} or {sealed_secrets.PASSPHRASE_FILE_ENV}"
                ),
            )
        sealed_secrets.read_sealed_file(
            sealed_vault_key,
            purpose=sealed_secrets.VAULT_KEY_PURPOSE,
            passphrase=passphrase,
        )
    except sealed_secrets.SealedSecretError as exc:
        raise RequiredSubstrateError(
            "custody",
            "startup",
            reason=str(exc),
            detail=f"{sealed_vault_key} did not open with the configured passphrase",
        ) from exc
    report["custody"] = "sealed"
    log.info("Sealed custody unlocked (%s)", sealed_vault_key.name)


def boot() -> dict[str, Any]:
    """Infra required for pressure ticks and cognition_chain ignition.

    Returns readiness only after every required substrate operation commits.
    Optional observability failures remain non-fatal; runtime, memory, taxonomy,
    recovery, compaction, and Guardian failures raise with attribution.
    """
    log.info("Omega 3.0 substrate boot from %s", OMEGA_ROOT)
    _set_boot_status("starting", 5, "Starting the portable Omega runtime…")

    try:
        from guardian.telemetry import emit_boot_marker

        run_id = emit_boot_marker()
        log.info("Telemetry boot marker run_id=%s", run_id)
    except Exception as exc:
        log.warning("Telemetry boot marker failed: %s", exc)

    report: dict[str, Any] = {}

    # Memory-organ Cut 13 (D-13-75): hold guardian/lifecycle.lock SHARED for the process
    # lifetime BEFORE touching profile state — day-zero claims it EXCLUSIVE, so a startup
    # racing a reset is excluded (and a reset cannot begin while Omega is up).
    try:
        from memory.projection.dispatch import hold_lifecycle_lock

        if not hold_lifecycle_lock():
            log.critical("day-zero reset holds the lifecycle interlock — boot refused before touching profile state")
            report["lifecycle"] = "reset_in_progress"
            return report
        report["lifecycle"] = "held"
    except RequiredSubstrateError:
        raise
    except Exception as exc:  # noqa: BLE001 — a missing interlock cannot permit startup
        failure = RequiredSubstrateError.from_exception("lifecycle", "lock", exc)
        log.critical("lifecycle interlock unavailable: %s", failure)
        raise failure from exc

    # Portable custody must open before anything reads guardian state (2026-09-06).
    _boot_sealed_custody(report)
    _set_boot_status("loading", 14, "Opening security and runtime services…")

    from pressure_loop.runtime import PressureRuntime, set_default_runtime

    set_default_runtime(PressureRuntime())
    log.info("PressureRuntime ready (episode store + shared_state)")

    # Cold-start pressure hygiene before the first live gather, so a warm / in-process
    # reboot never carries the prior process's pressure state into this session.
    try:
        from pressure_loop import pressure as _pressure

        _pressure.reset_mind_primacy()  # Step 7 completion: bootstrap latch (cold start)
    except Exception as exc:
        failure = RequiredSubstrateError.from_exception("pressure", "overlay_reset", exc)
        log.critical("Omega overlay boot-clear failed: %s", failure)
        raise failure from exc

    from runtime import brain_cloud

    if os.environ.get("OMEGA_BRAIN_CLOUD_ONLY") == "1":
        # Explicit cloud-only integration mode (other-laptop plugin work,
        # 2026-09-05): a configured cloud BRAIN seat is the cognition runtime and
        # the local gateway is deferred. Without a usable seat this is not a
        # smaller valid Omega — it fails before any local startup.
        if not brain_cloud.enabled():
            raise RequiredSubstrateError(
                "runtime",
                "startup",
                reason="cloud_seat_unavailable",
                detail="Cloud-only boot requires a configured cloud seat and provider key",
            )
        report["runtime"] = "cloud"
        report["embedding_worker"] = "deferred"
        log.info("Cloud cognition seat configured — local runtime gateway not required")
        try:
            from memory.api import set_embedding_substrate_state

            set_embedding_substrate_state(
                "degraded",
                "local embedding runtime not started; perception stays with the "
                "in-process retina's published codes",
            )
        except Exception as exc:
            failure = RequiredSubstrateError.from_exception(
                "memory",
                "embedding_substrate_state",
                exc,
            )
            raise failure from exc
    else:
        _boot_local_runtime(report)
    _set_boot_status("loading", 34, "Runtime services are ready; warming memory…")
    try:
        import memory  # noqa: F401 — package import (deterministic routing; SetFit retired)

        from memory.engine.walker import COORD_INDEX, load_coord_index
        from memory.strength_index import ensure_strength_index

        load_coord_index()
        ensure_strength_index()
        from memory.strength_index import register_graceful_shutdown

        handlers = register_graceful_shutdown()
        log.info("StrengthIndex graceful shutdown handlers: %s", handlers)
        coord_count = len(COORD_INDEX)
        log.info(
            "Memory warmup: coord_index=%d entries, strength_index singleton ready", coord_count
        )
    except Exception as exc:
        failure = RequiredSubstrateError.from_exception(
            "memory",
            "warmup",
            exc,
        )
        raise failure from exc
    report["memory"] = "up"
    _set_boot_status("memory", 52, "Preparing memory indexes and recovery…")

    # Memory-organ Cut 13: compile-on-boot / journal recovery / projection dispatch run
    # through the ONE dispatcher. Required work inside it fails closed (RequiredSubstrateError).
    _boot_projection_and_recovery(report)
    _set_boot_status("memory", 64, "Memory recovery is complete; seating the semantic retina…")

    try:
        from memory.queues import compact_reflection_eligible

        dropped = compact_reflection_eligible()
    except Exception as exc:
        failure = RequiredSubstrateError.from_exception(
            "memory",
            "queue_compaction",
            exc,
        )
        raise failure from exc
    if dropped:
        log.info("reflection_eligible queue compacted: dropped=%d", dropped)

    # The semantic retina (operator 2026-09-05: "the ettin goes in the brain and
    # resident on boot and released by the shutdown lifecycle. The frozen Ettin
    # 17M encoder is verified by hash and seated in-process HERE, once; this
    # generation's codes are mapped
    # so the first recall already perceives. Required work: an encoder that
    # cannot load fails boot closed. What used to stand here seated the
    # runtime's Gemma embed worker and waited 12 s for a model the runtime has
    # since retired (17 §4: "remove memory embed boot requirement").
    _boot_semantic_retina(report)
    _boot_intent_router(report)
    _set_boot_status("warming", 74, "Semantic memory and the intent router are ready; warming Guardian…")

    try:
        # Seam 9 — register the qwen executor's shipped skills as Omega's applied
        # capabilities (lobe 5). Idempotent (deterministic ids overwrite), so it
        # converges the catalog to the current bundled set on every boot.
        from memory.capability_catalog import sync_capabilities

        cap = sync_capabilities()
        log.info(
            "Capability catalog: %d qwen skills registered as applied capabilities (lobe 5), "
            "%d skipped, %d failed",
            cap.get("count", 0),
            len(cap.get("skipped_disabled", [])),
            len(cap.get("failed", [])),
        )
        report["capabilities"] = cap.get("count", 0)
    except Exception as exc:
        log.warning("Capability catalog sync failed: %s", exc)
        report["capabilities"] = "down"

    try:
        from guardian.response_classes import reload, status

        reload()
        rc_status = status()
        log.info(
            "Guardian response_classes: %d classes, %d triggers",
            rc_status.get("total_classes", 0),
            rc_status.get("total_triggers", 0),
        )
        report["guardian"] = "up"
    except Exception as exc:
        failure = RequiredSubstrateError.from_exception(
            "guardian",
            "warmup",
            exc,
        )
        raise failure from exc

    _set_boot_status("warming", 84, "Core services are ready; starting the HUD…")

    try:
        from guardian.terminal_console import install as install_terminal_console

        install_terminal_console()
        log.info(
            "Cognition debug terminal registered "
            "(terminal.log + infra.log; OMEGA_TERMINAL_TELEMETRY_FILTER=all for verbose)"
        )
    except Exception as exc:
        log.warning("Terminal telemetry printer registration failed: %s", exc)

    summary = (
        f"readiness: runtime={report['runtime']} "
        f"memory={report['memory']} guardian={report['guardian']}"
    )
    log.info(summary)
    report["summary"] = summary
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "boot",
            "readiness",
            summary=summary,
            runtime=report["runtime"],
            memory=report["memory"],
            guardian=report["guardian"],
        )
    except Exception as exc:
        log.debug("boot readiness emit failed: %s", exc)
    log.info("Substrate boot complete.")
    return report


def boot_paused_for_hud(*, pause_owner: str = "hud.standalone") -> dict[str, Any]:
    r"""Boot substrate for HUD-only inspection — memory/guardian live, loop held.

    Used by ``.\.venv-brain\Scripts\python.exe -m hud.standalone`` (without ``--no-boot``). Registers
    ``PressureRuntime``, warms memory/guardian, and holds the pressure-loop
    pause so panels + settings memory graph render real data without ticking.
    """
    report = boot()
    from pressure_loop.pressure_loop_pause import pause_pressure_loop

    pause_pressure_loop(
        pause_owner,
        reason="HUD visual inspection — pressure loop held",
    )
    report["pressure_loop"] = "paused"
    report["mode"] = "hud_paused"
    log.info(
        "Substrate ready for HUD (pressure loop paused by %s). "
        "For live cognition run: .\\.venv-brain\\Scripts\\python.exe boot.py",
        pause_owner,
    )
    return report


def _run_post_mcp_continuous(
    *,
    runtime: Any,
    record_started: Any,
    shutdown_system: Any,
) -> None:
    """Own teardown for every startup and pressure action after MCP starts."""
    shutdown_state = {"reason": "startup_failure"}
    try:
        _run_owned_continuous(
            runtime=runtime,
            record_started=record_started,
            shutdown_state=shutdown_state,
        )
    except BaseException:
        try:
            shutdown_system(reason=shutdown_state["reason"], runtime=runtime)
        except BaseException:
            log.exception("reverse shutdown failed while preserving the original exit")
        raise
    else:
        shutdown_system(reason=shutdown_state["reason"], runtime=runtime)


def run_continuous() -> None:
    """Boot and run the pressure tick loop until interrupt."""
    import signal

    def handle_shutdown_signal(signum, frame):
        log.info("Received signal %d. Requesting graceful shutdown...", signum)
        from pressure_loop.loop import request_pressure_loop_stop

        request_pressure_loop_stop()
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, handle_shutdown_signal)
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handle_shutdown_signal)
    elif hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_shutdown_signal)

    from core.boot_lifecycle import (
        record_started,
        reset_lifecycle_state,
        restore_substrate_state,
        shutdown_system,
    )
    from core.boot_lock import acquire as _acquire_boot_lock, BootAlreadyRunning
    from pressure_loop.loop import reset_pressure_loop_stop

    reset_lifecycle_state()
    reset_pressure_loop_stop()

    try:
        _acquire_boot_lock()
        record_started("boot_lock")
    except BootAlreadyRunning as dup:
        log.error("boot refused: %s", dup)
        print(
            f"Error: {dup}\n"
            "  .\\.venv-brain\\Scripts\\python.exe scripts\\shutdown.py --dry-run\n"
            "  .\\.venv-brain\\Scripts\\python.exe scripts\\shutdown.py\n"
            "  .\\.venv-brain\\Scripts\\python.exe boot.py --once --json",
            file=sys.stderr,
        )
        raise SystemExit(1) from dup

    from pressure_loop.policy import assert_pressure_boot_allowed

    assert_pressure_boot_allowed()

    # Lifetime clock (operator 2026-07-16): stamp this boot BEFORE the substrate warms, so
    # offline duration + unclean-shutdown detection are known to everything downstream. This is
    # the live continuous run only (run_once / tests call boot() directly and never touch it).
    try:
        import time as _time

        from autonomic.temporal_grounding.temporal_persistence import record_boot

        _clock = record_boot(now=_time.time())
        log.info(
            "Lifetime clock: boot #%d · offline %s · prior shutdown %s",
            _clock.get("boot_count"),
            (
                f"{_clock['offline_seconds']:.0f}s"
                if _clock.get("offline_seconds") is not None
                else "unknown"
            ),
            ("none (first boot)" if _clock.get("prior_clean_shutdown") is None
             else "clean" if _clock.get("prior_clean_shutdown") else "UNCLEAN"),
        )
    except Exception as exc:
        log.warning("lifetime clock record_boot failed: %s", exc)

    try:
        substrate_report = boot()
    except BaseException:
        from pressure_loop.runtime import try_get_default_runtime

        try:
            shutdown_system(
                reason="substrate_start_failure",
                runtime=try_get_default_runtime(),
            )
        except BaseException:
            log.exception("reverse shutdown after substrate boot failure also failed")
        raise
    record_started("substrate")

    # Gateway watchdog — LIVE CONTINUOUS RUNS ONLY (operator 2026-08-11). boot()
    # is shared with run_once() and the test suite, so starting the keeper there
    # would leave a daemon thread respawning gateways behind a one-shot tick.
    # Same real-live-boot gate as the teardown sweep, for the same reason.
    # Started after boot() so it never races boot's own spawn.
    if substrate_report.get("runtime") == "cloud":
        # Cloud-only cognition (OMEGA_BRAIN_CLOUD_ONLY=1) never spawned a local
        # gateway, so there is nothing for a keeper to respawn.
        log.info("Gateway watchdog skipped: cloud cognition runtime, no local gateway")
    else:
        try:
            from runtime.supervisor import start_gateway_watchdog

            log.info("Gateway watchdog: %s", start_gateway_watchdog())
        except Exception as exc:
            log.warning("gateway watchdog start failed: %s", exc)

    from pressure_loop.runtime import get_default_runtime

    runtime = get_default_runtime()
    try:
        restore_report = restore_substrate_state(runtime)
    except BaseException:
        try:
            shutdown_system(reason="substrate_restore_failure", runtime=runtime)
        except BaseException:
            log.exception("reverse shutdown after substrate-state restore failure also failed")
        raise
    log.info("Substrate state restore: %s", restore_report.get("shared_state"))

    # Phase 4 cut 11: restore the LANES. Beside the substrate restore because it
    # is the same act one level up — that one brings back what the mind knew,
    # this one brings back what it was in the middle of DOING.
    from core.pipeline_state import restore_pipelines_at_boot
    from pressure_loop.cognition_queue import settle_interrupted_operator_arrivals

    interrupted_arrivals = settle_interrupted_operator_arrivals()
    if interrupted_arrivals:
        log.error("Interrupted operator intake requires resubmission: %s", interrupted_arrivals)
    lane_restore = restore_pipelines_at_boot()
    if lane_restore.get("pipelines"):
        log.info("Lane state restore: %s", lane_restore.get("pipelines"))
    else:
        log.info("Lane state restore: every lane was at rest")

    # MCP is a boot-owned organ. Its signed registry and external switch are
    # durable substrate state, so start only after restore and fail the whole
    # boot if that state cannot be validated.
    try:
        from mcp_platform.service import start_default_mcp_host

        start_default_mcp_host()
    except BaseException:
        try:
            shutdown_system(reason="mcp_host_start_failure", runtime=runtime)
        except BaseException:
            log.exception("reverse shutdown after MCP host start failure also failed")
        raise
    record_started("mcp_host")

    _run_post_mcp_continuous(
        runtime=runtime,
        record_started=record_started,
        shutdown_system=shutdown_system,
    )


def _run_owned_continuous(
    *,
    runtime: Any,
    record_started: Any,
    shutdown_state: dict[str, str],
) -> None:


    # Boot verifies only the selected cognition transport. Local inference stays
    # lazy so startup does not contend with graph, embedding, HUD, and bridge
    # initialization; the first real request owns model load failure telemetry.
    from runtime import brain_cloud, brain_cloud_config
    from runtime.model_control import probe_cognition_runtime

    if brain_cloud.enabled():
        # Cognition runs on the cloud BRAIN seat, so do NOT load the local gemma cognition
        # model — nor the A1 window-fold seat, which the cloud path overrides (client.py
        # routes to the cloud before consulting the per-op seat). Loading them would burn
        # VRAM/RAM/boot-time for a mind that never calls them. Reachability is probed so a
        # dead endpoint is visible at boot, not silent per-generate.
        d = brain_cloud_config.descriptor() or {}
        base = str(d.get("base_url") or "").rstrip("/")
        # Probe what the OpenAI contract actually DEFINES: GET {base}/models,
        # authenticated. The previous probe stripped /v1 and asked for /health —
        # a LOCAL runtime's shape, which every cloud provider 404s. It reported
        # "unreachable, cognition degrades" on 2026-08-15 against a seat that was
        # answering chat/completions in 0.7s, which is a lie in the direction
        # that wastes an operator's afternoon. If the seat only speaks the one
        # route it must, this says so honestly instead of guessing again.
        reachable = False
        try:
            import urllib.request

            req = urllib.request.Request(f"{base}/models", method="GET")
            key = str(d.get("api_key") or "").strip()
            if key:
                req.add_header("Authorization", f"Bearer {key}")
            with urllib.request.urlopen(req, timeout=4) as r:  # noqa: S310
                reachable = getattr(r, "status", 200) == 200
        except Exception:
            reachable = False
        log.info(
            "Cognition on CLOUD brain seat: %s @ %s (reachable=%s) — local gemma/A1 NOT loaded",
            d.get("model"),
            d.get("base_url"),
            reachable,
        )
        if not reachable:
            log.warning(
                "Cloud brain seat unreachable at boot — cognition degrades until the endpoint is up"
            )
        # Reconcile a STALE resident: a gateway the brain just spawned skips the gemma
        # autoload (OMEGA_SKIP_RESIDENT_LLM), but a gateway already up from a prior
        # local-brain boot / operator start may still hold gemma resident. The cloud
        # brain never calls it, so evict it now to reclaim the VRAM. No-op when nothing
        # is loaded. Best-effort — a failed evict never blocks the boot.
        try:
            from runtime.model_control import unload_endpoint

            # Address the CAPABILITY, not the weights: the runtime owns which
            # model sits behind /generate, so it is the one that knows what to
            # release (operator 2026-08-15). Naming a model here is what broke
            # this on 2026-08-15 — see runtime.model_control.unload_model.
            evicted = unload_endpoint("generate")
            if evicted.get("ok"):
                log.info(
                    "Released the local generate seat (%s) — cloud brain owns cognition",
                    ", ".join(evicted.get("payload", {}).get("models") or ["?"]),
                )
            else:
                # Never silent: a seat we could not release is VRAM held for a
                # model nothing will call, and the operator should see why.
                log.warning(
                    "Local generate seat NOT released (%s) — it stays resident with no callers",
                    evicted.get("error") or evicted.get("reason") or "unknown",
                )
        except Exception as exc:
            log.warning("stale resident evict skipped (non-fatal): %s", exc)
    else:
        # PROBE ONLY — nothing is loaded here. Boot is the worst moment to pull a
        # multi-gigabyte model onto a shared-memory iGPU: it contends with the
        # graph compile, the embed lane indexing the initial graph, the HUD and
        # the bridge, and the operator measured severe boot lag from exactly that
        # (2026-07-30). The seat loads on its first real use instead — safe
        # because the request path already loads on demand, unguarded
        # (gateway/workers/proxy.py:312 _ensure_model_worker).
        #
        # What is still checked is the failure that actually happens at boot and
        # costs one HTTP call: a runtime that is not there. A model that will not
        # load is caught on the first generate instead of here — later than ideal,
        # but it is rare, permanent and config-shaped, and detecting it was never
        # worth a full load on every single boot.
        residency = probe_cognition_runtime()
        log.info(
            "Cognition runtime reachable; seat %s loads on first use (not resident)",
            residency.get("model"),
        )
    record_started("cognition_runtime")

    # Fresh chat panel every boot (operator 2026-07-12: "clear the chat panel
    # every shutdown" — clearing at boot also covers crash shutdowns). Durable
    # content lives in its own stores: questions/results in system messages,
    # build narration in agent activity, proactive updates in the proactive feed.
    try:
        from hud.chat_history import clear_messages

        cleared = clear_messages()
        if cleared:
            log.info("chat panel cleared: %d prior-session messages dropped", cleared)
    except Exception as exc:
        log.warning("chat panel clear failed: %s", exc)

    from hud.spawner import spawn_hud_in_thread

    # Start the in-process server before Edge.  The first ordinary page passed
    # to the debugger-enabled process is therefore the HUD itself; this avoids
    # a blank bootstrap window followed by a second Edge launch.
    hud_listen_url = spawn_hud_in_thread()
    import os as _os

    open_hud_browser = _os.environ.get("OMEGA_HUD_OPEN_BROWSER", "1").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )
    hud_browser_prepared = False
    hud_initial_url_opened = False
    if open_hud_browser and hud_listen_url:
        try:
            from browser_agent.lifecycle import prepare_hud_browser

            receipt = prepare_hud_browser(initial_url=hud_listen_url)
            hud_browser_prepared = True
            hud_initial_url_opened = bool(receipt.get("initial_url_opened"))
        except Exception as exc:
            # Keep the substrate failure visible without terminating a normal
            # personal Edge session. The browser bootstrap owns a separate
            # SSD-local profile, so this path is limited to a real setup error.
            log.warning(
                "HUD Edge session was not prepared; HUD will not auto-open a browser window: %s",
                exc,
            )

    if hud_listen_url:
        log.info("HUD operator surface: %s", hud_listen_url)
        record_started("hud")
        _set_boot_status("ready", 100, f"Omega HUD is ready at {hud_listen_url}.")
        # Speak once, now that there is a surface to speak through (operator 2026-09-22). The
        # onboarding phrase until setup completes, then a greeting by name — and it is the audible
        # proof that the talker is resident on the GPU from boot: silence means the speech chain is
        # broken, and you learn it before saying a word. Never fatal; voice is not cognition.
        try:
            from pressure_loop.boot_greeting import last_failure, publish_boot_greeting

            if not publish_boot_greeting():
                log.error("Boot greeting was not published: %s", last_failure() or "already said")
        except Exception:
            log.error("Boot greeting could not be prepared", exc_info=True)
        if hud_browser_prepared and not hud_initial_url_opened:
            try:
                from browser_agent.lifecycle import open_hud_window

                open_hud_window(hud_listen_url)
            except Exception as exc:
                log.warning("Failed to open the HUD in the prepared Edge session: %s", exc)
        elif open_hud_browser and not hud_browser_prepared:
            log.warning(
                "HUD is listening at %s but no debugger-enabled Edge window was opened",
                hud_listen_url,
            )
    else:
        log.info("HUD operator surface: explicitly disabled")
        _set_boot_status("ready", 100, "Omega substrate is ready; the HUD is disabled by configuration.")

    # Tool execution belongs to the shared chat brain: AUTO's RESPONSE pass offers
    # Mistral the bounded tool table and runs the calls it makes. Boot starts no
    # separate CLI/TUI agent process.

    from pressure_loop.loop import pressure_loop_stop_requested, run_pressure_tick_loop

    log.info("Starting pressure loop (tick → ignition → cognition cascade)")
    record_started("pressure_loop")
    shutdown_state["reason"] = "shutdown"
    try:
        run_pressure_tick_loop(runtime)
        if pressure_loop_stop_requested():
            shutdown_state["reason"] = "pressure_loop_stop"
    except KeyboardInterrupt:
        log.info("Shutdown: keyboard interrupt")
        shutdown_state["reason"] = "keyboard_interrupt"
    except SystemExit:
        raise
    except Exception as exc:
        log.error("run_continuous exit: %s: %s", type(exc).__name__, exc)
        shutdown_state["reason"] = f"error:{type(exc).__name__}"
        raise


def run_once(*, emit_json: bool = False) -> dict[str, Any]:
    """Smoke: one pressure tick (no boot_lock, no infinite loop)."""
    report = boot()
    from pressure_loop.runtime import get_default_runtime
    from pressure_loop.loop import run_pressure_tick_once

    run_pressure_tick_once(get_default_runtime())
    report["mode"] = "once"
    report["exit_code"] = 0
    if emit_json:
        print(json.dumps(report, indent=2))
    return report


_BOOT_EPILOG = """
Examples:
  .\\.venv-brain\\Scripts\\python.exe boot.py
  .\\.venv-brain\\Scripts\\python.exe boot.py --once
  .\\.venv-brain\\Scripts\\python.exe boot.py --once --json
  .\\.venv-brain\\Scripts\\python.exe scripts\\shutdown.py --dry-run
"""


# Operator-confirmed LIVE boot profile (2026-06-26): the portable venv command boots the
# fully-configured mind-driven version — no env incantation. Applied via setdefault so an
# explicit env var ALWAYS overrides (set OMEGA_X=0 to force a flag off). Flags deliberately
# LEFT at their off defaults (the mind-driven retirements) are NOT listed: OMEGA_FAST_REPLY
# (retired 3b209a00) and MIND_PRIMACY_SUSTAIN_ENABLED (retired Phase-E driver).
# OMEGA_PRODUCTION_MODE is NEVER auto-set — its effects are irreversible.
_LIVE_BOOT_PROFILE = {
    # memory taxonomy — artifact capture + recall is always live (the "I remember what I built"
    # substrate). OMEGA_MEMORY_TAXONOMY_PRESSURE is deliberately OFF. It makes the taxonomy MANUFACTURE
    # pressure from failure clusters (capability_gap), which is a deterministic auto-driver that
    # fires at startup off accumulated failures and amplifies a failure loop — against the
    # mind-driven north-star (only mind-metacog contributors; idle = quiet).
    "OMEGA_SIGNALS_RECORD": "1",
    "OMEGA_OPERATOR_REDIRECT": "1",
    "OMEGA_ONBOARDING_ESTABLISH": "1",
    # Dopamine -> engram reinforce (operator directive 2026-07-11: earned success IS
    # rewarded). Reward-hacking is bounded by design: the RPE expectation baseline
    # plateaus repeated same-domain success (config/dopamine.yaml), the predicted band
    # (±0.20) teaches nothing, and write_strength clamps to [0, 1] with relevance-scaled
    # deltas (memory/strength_index.reinforce).
    "OMEGA_DOPAMINE_REINFORCE": "1",
    # Memory writes are native and projection freshness is separate from
    # canonical durability. Semantic perception is the brain-resident Ettin
    # retina seated by _boot_semantic_retina; there is no runtime Gemma seat.
    "OMEGA_MEMORY_NATIVE_CITIZENSHIP": "1",
    # Live agentic tools fail closed until the operator explicitly grants a
    # directory with /trust and selects it with /cd. The historical sibling
    # omega-workspace remains only as a test/deployment compatibility override
    # and is never created by the portable launcher.
    "OMEGA_TRUSTED_FOLDERS_REQUIRED": "1",
}


def _apply_live_boot_profile() -> None:
    """Set the live-config flag defaults so the portable venv command boots the correct version.
    setdefault => an explicit env var always wins. Runs at the SCRIPT entrypoint only (not on
    programmatic boot()/test imports), BEFORE boot()'s lazy imports resolve any flag."""
    import os

    for k, v in _LIVE_BOOT_PROFILE.items():
        os.environ.setdefault(k, v)
    log.info(
        "live boot profile applied (%d flags; trusted-folder access required; no implicit workspaces; env overrides honored)",
        len(_LIVE_BOOT_PROFILE),
    )


def main(argv: Optional[list[str]] = None) -> int:
    _apply_live_boot_profile()
    import argparse

    parser = argparse.ArgumentParser(
        description="Omega 3.0 substrate boot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_BOOT_EPILOG,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Boot + one pressure tick then exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="With --once: emit readiness JSON on stdout",
    )
    args = parser.parse_args(argv)
    if args.json and not args.once:
        parser.error("--json requires --once (continuous mode has no one-shot report)")
    try:
        if args.once:
            run_once(emit_json=args.json)
            return 0
        run_continuous()
        return 0
    except Exception as exc:
        _set_boot_status(
            "failed",
            100,
            f"Omega boot failed: {type(exc).__name__}: {exc}",
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
