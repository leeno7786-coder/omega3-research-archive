"""Terminal feedback hooks — failure-soft observability for the rebuild path.

Full audit/HUD fan-out can grow here; cognition preflight only needs
``phase_span`` + ``emit_phase`` without raising.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, Optional

log = logging.getLogger("omega.guardian.terminal_feedback")

TAG_PHASE = "phase"
TAG_SIGNAL = "signal"
TAG_MEMORY = "memory"

_SUBSCRIBERS: list[Callable[[dict[str, Any]], None]] = []
_SUBSCRIBER_LOCK = threading.Lock()
_RECENT_EVENTS_RING: deque[dict[str, Any]] = deque(maxlen=5000)
_RING_LOCK = threading.Lock()


def subscribe(callback: Callable[[dict[str, Any]], None]) -> None:
    with _SUBSCRIBER_LOCK:
        if callback not in _SUBSCRIBERS:
            _SUBSCRIBERS.append(callback)


def unsubscribe(callback: Callable[[dict[str, Any]], None]) -> None:
    with _SUBSCRIBER_LOCK:
        if callback in _SUBSCRIBERS:
            _SUBSCRIBERS.remove(callback)


def recent_audit_events(
    limit: int = 100,
    *,
    predicate: Optional[Callable[[dict[str, Any]], bool]] = None,
    newest_first: bool = False,
) -> list[dict[str, Any]]:
    """Return recent in-process audit events.

    Default order is oldest -> newest to preserve the original ring contract.
    ``predicate`` and ``newest_first`` let HUD panels query a bounded live feed
    without each panel re-implementing filtering/reversal over the full ring.
    """
    with _RING_LOCK:
        rows = list(_RECENT_EVENTS_RING)
    if predicate is not None:
        filtered = []
        for row in rows:
            try:
                if predicate(row):
                    filtered.append(row)
            except Exception:
                continue
        rows = filtered
    if newest_first:
        rows = list(reversed(rows))
    if limit <= 0:
        return rows
    if newest_first:
        return rows[:limit]
    return rows[-limit:]


def _reset_recent_events_ring_for_tests() -> None:
    """Test isolation — clear the in-process audit ring."""
    with _RING_LOCK:
        _RECENT_EVENTS_RING.clear()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _shared_state_for_hud_mirror() -> Any:
    """Best-effort access to the live PressureRuntime SharedState."""
    try:
        from pressure_loop.runtime import get_default_runtime

        return get_default_runtime().shared_state
    except Exception:
        return None


def _append_shared_state_ring(key: str, record: dict[str, Any], cap: int) -> None:
    ss = _shared_state_for_hud_mirror()
    if ss is None:
        return
    try:
        current = ss.read(key)
        ring = list(current) if isinstance(current, list) else []
        ring.append(record)
        ss.write(key, ring[-cap:])
    except Exception:
        pass


def _layer_state_from_event(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Project v2 phase telemetry into the HUD's recent layer-state ring."""
    if event.get("kind") != "phase":
        return None
    loop = str(event.get("loop") or event.get("system") or "").strip().lower()
    if loop not in {
        "brain",
        "global",
        "meta",
        "auto",
        "omega",
        "deepsleep",
        "pressure",
    }:
        return None
    phase = str(event.get("phase") or "").strip().lower()
    status = str(event.get("status") or "ok").strip().lower()
    if loop == "brain" and phase:
        layer_id = f"brain.{phase}"
    elif loop == "pressure" and phase:
        layer_id = f"pressure.{phase}"
    else:
        layer_id = loop
    summary = str(event.get("summary") or "")
    schema = {
        "summary": summary or f"{loop}.{phase} {status}".strip(),
        "loop": loop,
        "phase": phase,
        "status": status,
        "latency_ms": event.get("latency_ms"),
    }
    for key in (
        "scores",
        "contributors",
        "dominant",
        "aggregate_pressure",
        "agg",
        "weights",
        "margin",
        "tick",
        "opened_by",
        "memory_status",
        "cycle_kind",
        "failure_kind",
        "model_id",
    ):
        if key in event and event[key] is not None:
            schema[key] = event[key]
    return {
        "ts": event.get("ts") or _now_iso(),
        "cycle_id": (
            event.get("cycle_id") or event.get("episode_id") or event.get("window_id") or ""
        ),
        "layer_id": layer_id,
        "layer_kind": "phase",
        "source": loop,
        "did_work": status not in {"skip", "fail", "failed", "error"},
        "justification": summary or f"{loop}.{phase} {status}".strip(),
        "schema": schema,
    }


def _omega_epoch_from_event(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    if event.get("kind") != "data":
        return None
    loop = str(event.get("loop") or event.get("system") or "").strip().lower()
    phase = str(event.get("phase") or "").strip().lower()
    if loop != "omega" or phase != "omega_outcome_pack":
        return None
    update = event.get("trajectory_update")
    update = dict(update) if isinstance(update, dict) else {}
    diff = str(event.get("trajectory_diff") or "").strip().upper()
    direction = {
        "FOLLOWING": "stable",
        "PARTIAL_FOLLOWING": "changing",
        "DRIFTING": "contradiction_rich",
        # ESTABLISHING (no baseline yet) falls through to "consolidation"
    }.get(diff, "consolidation")
    try:
        coherence = float(update.get("confidence") or event.get("coherence") or 0.0)
    except (TypeError, ValueError):
        coherence = 0.0
    feedback = event.get("pressure_feedback")
    feedback = dict(feedback) if isinstance(feedback, dict) else {}
    applied = sum(
        len(feedback.get(k) or []) for k in ("contributors_to_raise", "contributors_to_create")
    )
    rejected = len(feedback.get("contributors_to_lower") or [])
    epoch = event.get("epoch")
    if not isinstance(epoch, int):
        ss = _shared_state_for_hud_mirror()
        try:
            epoch = int(ss.read("_epoch") or 0) if ss is not None else 0
        except Exception:
            epoch = 0
    return {
        "epoch": epoch,
        "window_id": str(event.get("episode_id") or event.get("cycle_id") or ""),
        "timestamp": event.get("ts") or _now_iso(),
        "coherence": f"{max(0.0, min(1.0, coherence)):.2f}",
        "applied": applied,
        "rejected": rejected,
        "direction": direction,
        "trajectory_diff": diff or "UNKNOWN",
        "trajectory_decision": str(
            event.get("trajectory_decision") or update.get("decision") or ""
        ),
        "target_domain": update.get("target_domain") or "",
        "target_capability": update.get("target_capability") or "",
    }


def _mirror_hud_live_feed(event: dict[str, Any]) -> None:
    """Maintain the SharedState rings the HUD polls as live visual feeds."""
    try:
        from core.shared_state_keys import RECENT_LAYER_STATES, RECENT_OMEGA_EPOCHS

        layer_record = _layer_state_from_event(event)
        if layer_record is not None:
            _append_shared_state_ring(RECENT_LAYER_STATES, layer_record, 40)
        omega_record = _omega_epoch_from_event(event)
        if omega_record is not None:
            _append_shared_state_ring(RECENT_OMEGA_EPOCHS, omega_record, 20)
    except Exception:
        pass


def _route_telemetry(event: dict[str, Any]) -> None:
    try:
        from guardian.telemetry import write_telemetry

        kind = str(event.get("kind") or "")
        loop = str(event.get("loop") or event.get("system") or "")
        phase = str(event.get("phase") or "")
        status = str(event.get("status") or "")
        episode_id = event.get("episode_id")

        if kind == "phase":
            if status == "enter":
                tel_event = "enter"
            else:
                tel_event = "exit"
            write_telemetry(
                system=loop,
                phase=phase,
                event=tel_event,
                status=status,
                summary=str(event.get("summary") or ""),
                episode_id=episode_id,
                latency_ms=event.get("latency_ms"),
                error=event.get("error_type") or event.get("error"),
                scores=event.get("scores"),
            )
        elif kind == "inference":
            write_telemetry(
                system=loop or "runtime",
                phase=phase or str(event.get("operation") or ""),
                event="inference",
                status=status,
                summary=str(event.get("summary") or ""),
                episode_id=episode_id,
                latency_ms=event.get("latency_ms"),
                tokens=event.get("tokens"),
                ok=event.get("ok"),
                error=event.get("error"),
                operation=event.get("operation"),
                model=event.get("model") or event.get("slot"),
            )
        elif kind == "signal":
            write_telemetry(
                system=loop or "guardian",
                phase=str(event.get("name") or ""),
                event="signal",
                status=status or "ok",
                summary=str(event.get("narrative") or ""),
                episode_id=episode_id,
                value=event.get("value"),
                components=event.get("components"),
            )
        elif kind == "memory":
            write_telemetry(
                system="memory",
                phase=str(event.get("op") or ""),
                event="data",
                status=str(event.get("status") or status or "ok"),
                episode_id=episode_id,
                coord=event.get("coord"),
                candidate_count=event.get("candidate_count"),
            )
        elif kind == "data":
            write_telemetry(
                system=loop or "guardian",
                phase=phase,
                event="data",
                status=status or "ok",
                summary=str(event.get("summary") or ""),
                episode_id=episode_id,
                **{
                    k: v
                    for k, v in event.items()
                    if k
                    not in {
                        "kind",
                        "loop",
                        "system",
                        "phase",
                        "status",
                        "summary",
                        "episode_id",
                    }
                },
            )
    except Exception as exc:
        log.debug("telemetry route failed: %s", exc)


def _publish_event(event: dict[str, Any]) -> None:
    event = dict(event)
    event.setdefault("ts", _now_iso())
    try:
        with _RING_LOCK:
            _RECENT_EVENTS_RING.append(dict(event))
    except Exception:
        pass
    _mirror_hud_live_feed(event)
    _route_telemetry(event)
    with _SUBSCRIBER_LOCK:
        subscribers = list(_SUBSCRIBERS)
    for callback in subscribers:
        try:
            callback(event)
        except Exception as exc:
            log.debug("terminal_feedback subscriber failed: %s", exc)


def emit_phase(
    loop: str,
    phase: str,
    *,
    status: str,
    summary: str = "",
    **fields: Any,
) -> None:
    try:
        _publish_event(
            {
                "kind": "phase",
                "loop": loop,
                "phase": phase,
                "status": status,
                "summary": summary,
                **fields,
            }
        )
    except Exception as exc:
        log.debug("emit_phase failed: %s", exc)


def emit_inference(
    system: str,
    operation: str,
    *,
    phase: str = "",
    status: str = "ok",
    episode_id: Optional[str] = None,
    latency_ms: Optional[int] = None,
    tokens: Optional[Any] = None,
    ok: Optional[bool] = None,
    model: Optional[str] = None,
    slot: Optional[str] = None,
    summary: str = "",
    error: Optional[str] = None,
    **fields: Any,
) -> None:
    try:
        _publish_event(
            {
                "kind": "inference",
                "loop": system,
                "phase": phase or operation,
                "operation": operation,
                "status": status,
                "episode_id": episode_id,
                "latency_ms": latency_ms,
                "tokens": tokens,
                "ok": ok,
                "model": model,
                "slot": slot,
                "summary": summary,
                "error": error,
                **fields,
            }
        )
    except Exception as exc:
        log.debug("emit_inference failed: %s", exc)


def emit_signal(
    name: str,
    value: Any,
    *,
    components: Optional[Dict[str, Any]] = None,
    narrative: str = "",
    **fields: Any,
) -> None:
    try:
        _publish_event(
            {
                "kind": "signal",
                "name": name,
                "value": value,
                "components": components,
                "narrative": narrative,
                **fields,
            }
        )
    except Exception as exc:
        log.debug("emit_signal failed: %s", exc)


_LOOP_SYSTEMS = frozenset(
    {"pressure", "global", "meta", "auto", "omega", "deepsleep", "brain", "guardian"}
)


def emit_data(
    system: str,
    phase: str,
    *,
    status: str = "ok",
    summary: str = "",
    episode_id: Optional[str] = None,
    ok: Optional[bool] = None,
    **fields: Any,
) -> None:
    try:
        if ok is False and status == "ok":
            status = "fail"
        _publish_event(
            {
                "kind": "data",
                "loop": system,
                "system": system,
                "phase": phase,
                "status": status,
                "ok": ok,
                "summary": summary,
                "episode_id": episode_id,
                **fields,
            }
        )
        loop = str(system or "").strip().lower()
        if loop in _LOOP_SYSTEMS:
            try:
                from guardian.data_logger import emit_event

                emit_event(
                    f"omega.{loop}.loop",
                    f"data_{phase}",
                    status=status,
                    summary=summary,
                    episode_id=episode_id,
                    ok=ok,
                    _skip_terminal_mirror=True,
                    _skip_telemetry_mirror=True,
                    **fields,
                )
            except Exception as exc:
                log.debug("emit_data guardian log mirror failed: %s", exc)
    except Exception as exc:
        log.debug("emit_data failed: %s", exc)


def emit_memory(
    op: str,
    *,
    coord: Any = None,
    status: Optional[str] = None,
    candidate_count: Optional[int] = None,
    **fields: Any,
) -> None:
    try:
        _publish_event(
            {
                "kind": "memory",
                "op": op,
                "coord": coord,
                "status": status,
                "candidate_count": candidate_count,
                **fields,
            }
        )
    except Exception as exc:
        log.debug("emit_memory failed: %s", exc)


def _loop_from_subsystem(subsystem: str) -> str:
    raw = str(subsystem or "").strip().lower()
    if raw.startswith("omega."):
        raw = raw[len("omega.") :]
    if not raw:
        return "guardian"
    return raw.split(".")[0] or "guardian"


def emit_guardian_event(
    subsystem: str,
    event_name: str,
    *,
    kind: str = "data",
    status: str | None = None,
    summary: str = "",
    mirror_terminal: bool = True,
    **fields: Any,
) -> None:
    """
    Bridge helper: emit a guardian JSONL event *and* mirror it to the terminal feed.

    - Writes to guardian/data_logger (durable JSONL + telemetry mirror).
    - Publishes to terminal_feedback ring/terminal_console so operators see it live.
    - Failure-soft: never raises.
    """
    event_kind = str(kind or "data")
    # Preserve provided summary if passed via **fields to avoid duplication.
    summary_value = summary or str(fields.pop("summary", "") or "")
    status_value = status or str(fields.get("status") or "ok")
    loop = _loop_from_subsystem(subsystem)

    terminal_event = {
        "kind": event_kind,
        "loop": loop,
        "system": loop,
        "phase": event_name,
        "status": status_value,
        "summary": summary_value,
        **{k: v for k, v in fields.items() if v is not None},
    }

    if mirror_terminal:
        try:
            _publish_event(terminal_event)
        except Exception as exc:
            log.debug("guardian event terminal mirror failed: %s", exc)

    try:
        from guardian.data_logger import emit_event as _emit_event

        dl_fields = {k: v for k, v in fields.items() if v is not None}
        if status is not None:
            dl_fields["status"] = status
        if summary_value:
            dl_fields["summary"] = summary_value
        # Prevent recursive terminal mirroring when we already published above.
        dl_fields["_skip_terminal_mirror"] = True
        # The terminal publish above already wrote the canonical telemetry row.
        dl_fields["_skip_telemetry_mirror"] = True
        _emit_event(subsystem, event_name, **dl_fields)
    except Exception as exc:
        log.debug("guardian event data_logger emit failed: %s", exc)


class _PhaseSpan:
    def __init__(self) -> None:
        self._extra: dict[str, Any] = {}
        self._skipped = False
        self._skip_summary = ""

    def set(self, **fields: Any) -> None:
        self._extra.update(fields)

    def skip(self, summary: str) -> None:
        self._skipped = True
        self._skip_summary = str(summary or "")


def _safe_emit_phase(**kwargs: Any) -> None:
    try:
        emit_phase(**kwargs)
    except Exception as exc:
        log.debug("emit_phase soft-fail: %s", exc)


def _log_phase_data_logger(hook: str, *, loop: str, phase: str, status: str, **fields: Any) -> None:
    try:
        from guardian.loop_phase_logger import log_loop_phase

        # phase_span ALREADY published this boundary to the terminal feed via
        # _safe_emit_phase; this is the durable data_logger sink. data_logger.emit_event
        # now mirrors to the terminal by default, so without _skip_terminal_mirror every
        # phase boundary (and every FAILURE) is published — and FAILURE_LOG'd — twice.
        log_loop_phase(
            hook=hook,
            loop=loop,
            phase=phase,
            status=status,
            _skip_terminal_mirror=True,
            _skip_telemetry_mirror=True,
            **fields,
        )
    except Exception as exc:
        log.debug("loop phase data logger soft-fail: %s", exc)


@contextmanager
def phase_span(loop: str, phase: str, **fields: Any) -> Iterator[_PhaseSpan]:
    span = _PhaseSpan()
    t0 = time.perf_counter()
    try:
        _safe_emit_phase(loop=loop, phase=phase, status="enter", **fields)
        _log_phase_data_logger("before", loop=loop, phase=phase, status="enter", **fields)
        yield span
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if span._skipped:
            _safe_emit_phase(
                loop=loop,
                phase=phase,
                status="skip",
                summary=span._skip_summary,
                latency_ms=latency_ms,
                **{**fields, **span._extra},
            )
            _log_phase_data_logger(
                "after",
                loop=loop,
                phase=phase,
                status="skip",
                summary=span._skip_summary,
                latency_ms=latency_ms,
                **{**fields, **span._extra},
            )
        else:
            _safe_emit_phase(
                loop=loop,
                phase=phase,
                status="ok",
                latency_ms=latency_ms,
                **{**fields, **span._extra},
            )
            _log_phase_data_logger(
                "after",
                loop=loop,
                phase=phase,
                status="ok",
                latency_ms=latency_ms,
                **{**fields, **span._extra},
            )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        _safe_emit_phase(
            loop=loop,
            phase=phase,
            status="fail",
            summary=str(exc),
            error_type=type(exc).__name__,
            latency_ms=latency_ms,
            **{**fields, **span._extra},
        )
        _log_phase_data_logger(
            "after",
            loop=loop,
            phase=phase,
            status="fail",
            summary=str(exc),
            error_type=type(exc).__name__,
            latency_ms=latency_ms,
            **{**fields, **span._extra},
        )
        raise
