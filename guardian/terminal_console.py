"""Stdout/console printer for guardian.terminal_feedback emit fan-out.

Default mode is **cognition debug**: cognition-loop phases (with scores/
weights), inference runs, and episode-scoped data land on the operator
terminal + ``guardian/logs/terminal.log``.

Operational/runtime probe noise (capability_evaluate, gallery lists, store
persist ok, etc.) is routed to ``guardian/logs/infra.log`` only. JSON
per-system telemetry files still receive everything via ``write_telemetry``.

Set ``OMEGA_TERMINAL_TELEMETRY_FILTER=all`` to restore the legacy
print-everything behaviour.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("omega.telemetry.terminal")
infra_log = logging.getLogger("omega.telemetry.infra")

_ENV_ENABLED = "OMEGA_TERMINAL_TELEMETRY"
_ENV_FILTER = "OMEGA_TERMINAL_TELEMETRY_FILTER"
_ENV_FILE = "OMEGA_TERMINAL_TELEMETRY_FILE"
_ENV_INFRA_FILE = "OMEGA_TERMINAL_INFRA_FILE"

_INSTALLED = False
_SUBSCRIBER: Optional[Any] = None
_INSTALL_LOCK = threading.Lock()

# Cognition debug stream (stdout + terminal.log). None -> default path.
TERMINAL_LOG_PATH: Optional[Path] = None
_FILE_HANDLER: Optional[logging.Handler] = None

# Operational/runtime probe stream (infra.log only). None -> default path.
INFRA_LOG_PATH: Optional[Path] = None
_INFRA_FILE_HANDLER: Optional[logging.Handler] = None

_FAILURE_STATUSES = frozenset({"fail", "error", "degraded", "crash", "timeout", "declined"})

# Cognition loops — phases from these systems print on the debug terminal.
_COGNITION_LOOPS = frozenset(
    {
        "pressure",
        "pressure_loop",
        "global",
        "global_loop",
        "meta",
        "meta_loop",
        "auto",
        "auto_loop",
        "omega",
        "omega_loop",
        "deepsleep",
        "deepsleep_loop",
        "memory",
        "broker",
        "symphony",
    }
)

# Data phases that are operational probes — infra.log only (JSON telemetry still).
_INFRA_DATA_PHASES = frozenset(
    {
        "capability_evaluate",
        "persist_generated_image",
        "list_generated_images",
        "persist_document",
        "list_documents",
        "runtime_tool_dispatch",
        "socket_preflight_declined",
        "anchor_identify",
        "anchor_enroll",
        "anchor_store_load_failed",
        "face_identify",
        "face_enroll",
        "stt",
        "tts",
        "images_list",
        "images_get",
        "documents_list",
        "documents_get",
        "uploads_list",
        "uploads_delete",
        "face_sidecar_write",
        "image_file_write",
        "image_url_resolution_failed",
        "document_url_resolution_failed",
        "strength_index_read_failed",
        "model_control",
    }
)

# Pressure-loop bookkeeping heartbeat (operator 2026-07-21). The `tick` wrapper
# phase and the `pressure_tick_summary` data row are pure per-cycle bookkeeping —
# tick number, ignited bool — with no decision content. Keep them in the durable
# trail (infra.log + JSONL + HUD ring) but OFF the cognition terminal so it stops
# cycling repetitively. The decision-bearing pressure phases (gather_contributors /
# evaluate / ignite_episode) and any FAILURE still surface.
#
# Operator 2026-09-14: "it should not be moving in the terminal unless theres an active
# lane moving". The per-tick gather/gate/neuromod bookkeeping rows are the same kind of
# heartbeat — they print every cycle whether or not anything ignites — so they join it.
# The terminal moves when an episode ignites and its lanes run.
_PRESSURE_LOOPS = frozenset({"pressure", "pressure_loop"})
_PRESSURE_HEARTBEAT_PHASES = frozenset({
    "tick",
    "pressure_tick_summary",
    "gather_contributors_snapshot",
    "evaluate_contributors_raw",
    "evaluate_neuromod_source",
    "neuromod_source_selected",
    "neuromod_tonic_transition",
    "ignition_gate",
    "ignition_gate_detail",
    "reseed_staging",
    "autonomous_authority_filter",
    "autonomy_held",
})


def _env_enabled() -> bool:
    raw = os.environ.get(_ENV_ENABLED, "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _parse_filter() -> Optional[set[str]]:
    """Return filter tokens. None = default cognition debug; {\"all\"} = legacy verbose."""
    raw = os.environ.get(_ENV_FILTER, "").strip().lower()
    if not raw:
        return None
    if raw == "all":
        return {"all"}
    return {part.strip() for part in raw.split(",") if part.strip()}


def _episode_prefix(episode_id: Any) -> str:
    if episode_id is None:
        return "-"
    text = str(episode_id)
    return text[:8] if text else "-"


def _latency_suffix(latency_ms: Any) -> str:
    if latency_ms is None:
        return ""
    try:
        return f" ({int(latency_ms)}ms)"
    except (TypeError, ValueError):
        return ""


# Curated cognition fields surfaced on phase/data lines — scores, weights,
# routing, and before/after episode artifacts. Order is the read order.
_COGNITION_KEYS = (
    "weights",
    "contributors",
    "dominant",
    "aggregate",
    "agg",
    "margin",
    "neuromod",
    "scores",
    "intent",
    "opened_by",
    "action",
    "tool",
    "domain",
    "trajectory",
    "tok_s",
    "mastery",
    "choice",
    "reasoning",
    "failure_kind",
    "failure_reason",
    "model_id",
    "cycle_kind",
    "route",
    "state",
    "proceed",
    "reason",
    "memory_status",
    "tool_reason",
    "execution_intent",
    "prompt_type",
    "response_mode",
    "tokens_in",
    "tokens_out",
    "sha256",
    "bytes_len",
    "format",
)
_DATA_DEBUG_KEYS = (
    "error",
    "failure_class",
    "ok",
    "tool_name",
    "tool",
    "operation",
    "capability",
    "available",
    "recognized_name",
    "confidence",
)
_MAX_COG_STR = 140


def _fmt_cog_num(v: Any) -> str:
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return f"{v:.3g}"
    s = str(v)
    if len(s) > _MAX_COG_STR:
        return f"{s[:_MAX_COG_STR]}…({len(s)}c)"
    return s


def _render_cog_field(key: str, value: Any) -> str:
    if isinstance(value, dict):
        inner = " ".join(f"{k}={_fmt_cog_num(v)}" for k, v in value.items())
        return f"{key}: {inner}" if inner else ""
    if isinstance(value, (list, tuple)):
        inner = " ".join(_fmt_cog_num(v) for v in value)
        return f"{key}: {inner}" if inner else ""
    return f"{key}={_fmt_cog_num(value)}"


def _render_cognition(event: dict[str, Any]) -> str:
    """Readable curated tail of the cognition the phase emitted (empty if none)."""
    bits = []
    for key in _COGNITION_KEYS:
        if key in event and event[key] is not None:
            rendered = _render_cog_field(key, event[key])
            if rendered:
                bits.append(rendered)
    return "  ".join(bits)


def format_event_line(event: dict[str, Any]) -> str:
    """Render one emit dict into a single terminal line."""
    kind = str(event.get("kind") or "event").lower()
    system = str(event.get("loop") or event.get("system") or "omega")
    phase = str(
        event.get("phase") or event.get("operation") or event.get("op") or event.get("name") or "-"
    )
    status = str(event.get("status") or "ok")
    episode = _episode_prefix(event.get("episode_id"))
    latency = _latency_suffix(event.get("latency_ms"))

    if kind == "phase":
        event_tag = "PHASE"
        summary = str(event.get("summary") or "")
    elif kind == "inference":
        event_tag = "INFERENCE"
        op = str(event.get("operation") or phase)
        bits = [f"op={op}"]
        if event.get("tokens") is not None:
            bits.append(f"tokens={event.get('tokens')}")
        if event.get("ok") is not None:
            bits.append(f"ok={event.get('ok')}")
        if event.get("model") or event.get("slot"):
            bits.append(f"model={event.get('model') or event.get('slot')}")
        tail = str(event.get("summary") or "")
        if tail:
            bits.append(tail)
        summary = " ".join(bits)
    elif kind == "data":
        event_tag = "DATA"
        bits = []
        raw_summary = str(event.get("summary") or "")
        if raw_summary:
            bits.append(raw_summary)
        for key in _DATA_DEBUG_KEYS:
            if key in event and event[key] is not None:
                bits.append(f"{key}={_fmt_cog_num(event[key])}")
        summary = " ".join(bits)
    elif kind == "signal":
        event_tag = "SIGNAL"
        name = str(event.get("name") or phase)
        value = event.get("value")
        narrative = str(event.get("narrative") or "")
        summary = f"{name}={value}"
        if narrative:
            summary = f"{summary} {narrative}"
        system = str(event.get("loop") or "guardian")
        phase = name
        status = status if event.get("status") is not None else "ok"
    elif kind == "memory":
        event_tag = "MEMORY"
        system = "memory"
        phase = str(event.get("op") or phase)
        bits = []
        if event.get("candidate_count") is not None:
            bits.append(f"candidates={event.get('candidate_count')}")
        if event.get("coord") is not None:
            bits.append(f"coord={event.get('coord')}")
        summary = " ".join(bits) or str(event.get("status") or "ok")
    else:
        event_tag = kind.upper()
        summary = str(event.get("summary") or "")

    tail = summary
    if kind in ("phase", "data"):
        cog = _render_cognition(event)
        tail = "  ".join(p for p in (summary, cog) if p)

    base = f"{system}/{phase} {event_tag} {status}{latency} ep={episode}"
    if tail:
        return f"{base} | {tail}"
    return base


def _event_loop(event: dict[str, Any]) -> str:
    return str(event.get("loop") or event.get("system") or "").strip().lower()


def _event_phase(event: dict[str, Any]) -> str:
    return (
        str(
            event.get("phase")
            or event.get("operation")
            or event.get("op")
            or event.get("name")
            or ""
        )
        .strip()
        .lower()
    )


def _has_cognition_payload(event: dict[str, Any]) -> bool:
    if any(key in event and event[key] is not None for key in _COGNITION_KEYS):
        return True
    return bool(event.get("episode_id"))


def _is_failure_event(event: dict[str, Any]) -> bool:
    status = str(event.get("status") or "").strip().lower()
    if status in _FAILURE_STATUSES:
        return True
    if event.get("ok") is False:
        return True
    return bool(event.get("failure_class") or event.get("error"))


def _is_pressure_heartbeat(event: dict[str, Any]) -> bool:
    """Routine pressure bookkeeping (tick wrapper + tick summary) — no decision
    content. Routed off the terminal into infra.log unless it FAILED."""
    return (
        _event_loop(event) in _PRESSURE_LOOPS and _event_phase(event) in _PRESSURE_HEARTBEAT_PHASES
    )


def _is_cognition_terminal_event(event: dict[str, Any]) -> bool:
    """Cognition debug terminal: loop phases, inference, signals, episode data."""
    kind = str(event.get("kind") or "").lower()
    loop = _event_loop(event)
    phase = _event_phase(event)

    # Routine pressure bookkeeping never cycles on the terminal (infra.log only),
    # unless it failed — a failed tick is a real signal, not heartbeat.
    if _is_pressure_heartbeat(event) and not _is_failure_event(event):
        return False

    if kind == "inference":
        return True
    if kind == "signal":
        return True
    if kind == "memory":
        return True

    if kind == "phase":
        if loop in _COGNITION_LOOPS:
            return True
        return loop == "hud" and _is_failure_event(event)

    if kind == "data":
        if phase in _INFRA_DATA_PHASES:
            return False
        if loop in _COGNITION_LOOPS:
            return True
        if _has_cognition_payload(event):
            return True
        if _is_failure_event(event):
            return True
        return False

    return _is_failure_event(event)


def _is_infra_file_event(event: dict[str, Any]) -> bool:
    """Operational probes — infra.log only, never the cognition terminal."""
    if _is_cognition_terminal_event(event):
        return False
    # Routine pressure bookkeeping (non-failure) keeps its durable trail in infra.log.
    if _is_pressure_heartbeat(event):
        return True
    kind = str(event.get("kind") or "").lower()
    loop = _event_loop(event)
    phase = _event_phase(event)
    if kind == "data" and (phase in _INFRA_DATA_PHASES or loop in {"runtime", "hud"}):
        return True
    if kind == "phase" and loop == "hud" and not _is_failure_event(event):
        return True
    return kind == "data" and loop == "runtime"


def _legacy_should_emit(event: dict[str, Any], filt: Optional[set[str]]) -> bool:
    if filt is None:
        return True
    if "no_enter" in filt and event.get("kind") == "phase" and event.get("status") == "enter":
        if filt == {"no_enter"}:
            return False
    kind = str(event.get("kind") or "")
    status = str(event.get("status") or "")
    if kind in filt or status in filt or f"{kind}:{status}" in filt:
        return True
    if "no_enter" in filt:
        return not (kind == "phase" and status == "enter")
    return False


def _on_event(event: dict[str, Any]) -> None:
    try:
        filt = _parse_filter()
        if filt is not None and "all" not in filt:
            if not _legacy_should_emit(event, filt):
                return
            log.info(format_event_line(event))
            return

        if filt is not None and "all" in filt:
            log.info(format_event_line(event))
            return

        # Default: cognition debug terminal + infra sidecar file.
        if _is_cognition_terminal_event(event):
            log.info(format_event_line(event))
        elif _is_infra_file_event(event):
            infra_log.info(format_event_line(event))
    except Exception:
        pass


def is_enabled() -> bool:
    return _env_enabled()


def _resolve_terminal_log_path() -> Path:
    """Resolve the cognition debug log file: module override > env > default."""
    if TERMINAL_LOG_PATH is not None:
        return Path(TERMINAL_LOG_PATH)
    env = os.environ.get(_ENV_FILE, "").strip()
    if env:
        return Path(env)
    from core.paths import guardian_logs_root

    return guardian_logs_root() / "terminal.log"


def _resolve_infra_log_path() -> Path:
    if INFRA_LOG_PATH is not None:
        return Path(INFRA_LOG_PATH)
    env = os.environ.get(_ENV_INFRA_FILE, "").strip()
    if env:
        return Path(env)
    from core.paths import guardian_logs_root

    return guardian_logs_root() / "infra.log"


def _install_file_handler() -> None:
    """Append the rendered terminal stream to a single file (failure-soft).

    A failed file open must never break the live terminal stream, so this
    swallows its own errors — the printer keeps logging to stdout regardless.
    """
    global _FILE_HANDLER
    if _FILE_HANDLER is not None:
        return
    try:
        path = _resolve_terminal_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, mode="a", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(handler)
        # Ensure rendered INFO lines actually reach the handler regardless of
        # ambient root config.
        if log.level == logging.NOTSET or log.level > logging.INFO:
            log.setLevel(logging.INFO)
        _FILE_HANDLER = handler
    except Exception as exc:
        log.debug("terminal_console file handler install failed: %s", exc)


def _install_infra_file_handler() -> None:
    global _INFRA_FILE_HANDLER
    if _INFRA_FILE_HANDLER is not None:
        return
    try:
        path = _resolve_infra_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, mode="a", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        infra_log.addHandler(handler)
        if infra_log.level == logging.NOTSET or infra_log.level > logging.INFO:
            infra_log.setLevel(logging.INFO)
        infra_log.propagate = False
        _INFRA_FILE_HANDLER = handler
    except Exception as exc:
        log.debug("terminal_console infra file handler install failed: %s", exc)


def install() -> None:
    """Subscribe the terminal printer. Idempotent; gated by env/config."""
    global _INSTALLED, _SUBSCRIBER
    if not _env_enabled():
        return
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        _INSTALLED = True
    try:
        from guardian.terminal_feedback import subscribe

        _SUBSCRIBER = _on_event
        subscribe(_SUBSCRIBER)
        _install_file_handler()
        _install_infra_file_handler()
    except Exception as exc:
        log.debug("terminal_console install failed: %s", exc)


def reset_for_tests() -> None:
    """Test isolation — allow re-install."""
    global _INSTALLED, _SUBSCRIBER, _FILE_HANDLER, _INFRA_FILE_HANDLER
    with _INSTALL_LOCK:
        if _SUBSCRIBER is not None:
            try:
                from guardian.terminal_feedback import unsubscribe

                unsubscribe(_SUBSCRIBER)
            except Exception:
                pass
        if _FILE_HANDLER is not None:
            try:
                log.removeHandler(_FILE_HANDLER)
                _FILE_HANDLER.close()
            except Exception:
                pass
            _FILE_HANDLER = None
        if _INFRA_FILE_HANDLER is not None:
            try:
                infra_log.removeHandler(_INFRA_FILE_HANDLER)
                _INFRA_FILE_HANDLER.close()
            except Exception:
                pass
            _INFRA_FILE_HANDLER = None
        _SUBSCRIBER = None
        _INSTALLED = False
