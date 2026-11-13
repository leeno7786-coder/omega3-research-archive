"""
HUD telemetry adapters — Omega subsystem state -> JSON for the HUD.

Each `panel_*` function returns the JSON shape the matching
omega_interface.html panel renders. HUD-1 stubs all of them with
'loading' placeholders so the HTML renders cleanly while HUD-2+
fill in real data subsystem-by-subsystem.

Adapter pattern:
    - Pure read; never mutates Omega state
    - Best-effort; on failure returns a degraded-but-shape-correct
      payload so the HUD never receives malformed JSON
    - Each adapter is independently testable + mockable
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional



class AuditRingSnapshot:
    """Single in-process audit ring copy for one telemetry snapshot build."""

    __slots__ = ("_events",)

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    @classmethod
    def capture(cls) -> "AuditRingSnapshot":
        from guardian.terminal_feedback import recent_audit_events

        return cls(list(recent_audit_events(limit=0)))

    def recent(
        self,
        limit: int = 100,
        *,
        predicate: Optional[Callable[[dict[str, Any]], bool]] = None,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        rows = self._events
        if predicate is not None:
            filtered: list[dict[str, Any]] = []
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


_ACTIVE_AUDIT_SNAPSHOT: AuditRingSnapshot | None = None


def _audit_events(
    limit: int = 100,
    *,
    predicate: Optional[Callable[[dict[str, Any]], bool]] = None,
    newest_first: bool = False,
) -> list[dict[str, Any]]:
    snap = _ACTIVE_AUDIT_SNAPSHOT
    if snap is not None:
        return snap.recent(limit, predicate=predicate, newest_first=newest_first)
    from guardian.terminal_feedback import recent_audit_events

    return recent_audit_events(limit, predicate=predicate, newest_first=newest_first)


# ---------------------------------------------------------------------
# Cached cadence scheduler
# ---------------------------------------------------------------------
# The HUD polls /api/telemetry/snapshot + /api/status every ~2s. Each
# call used to invoke `build_default_scheduler()` afresh — which logs
# "built default scheduler with 7 providers" AND walks the curiosity
# store / layer_state log to register every provider. With two HUD
# endpoints polling, that was 4 rebuilds/sec spamming both the log and
# the CPU. The pressure providers re-read their underlying data on
# each `tick()` call, so a single cached scheduler still produces
# fresh signal values.
_scheduler_lock = threading.Lock()
_cached_scheduler: Optional[Any] = None


def _get_cached_scheduler():
    """Return the process-wide cadence scheduler, building on first call.

    Thread-safe; never raises (returns None on import/build failure so
    the snapshot endpoints can degrade to '—' rather than 500).
    """
    global _cached_scheduler
    if _cached_scheduler is not None:
        return _cached_scheduler
    with _scheduler_lock:
        if _cached_scheduler is not None:  # double-checked
            return _cached_scheduler
        try:
            from pressure_loop.cycle_cadence import build_default_scheduler

            _cached_scheduler = build_default_scheduler()
        except Exception as exc:
            log.debug("hud telemetry: scheduler build failed: %s", exc)
            _emit_fail(
                "telemetry_scheduler_build",
                failure_class="scheduler_build_failed",
                summary="HUD telemetry cadence scheduler failed to build — cadence signals degraded",
                error_type=type(exc).__name__,
                error=str(exc)[:120],
            )
            return None
    return _cached_scheduler


log = logging.getLogger("omega.hud.telemetry")


def _emit_fail(
    operation: str, *, status: str = "degraded", failure_class: str, summary: str, **fields: Any
) -> None:
    """Remote-diagnosable record for telemetry build-FAILED paths only
    (NOT the per-pill best-effort swallows). Lazy-import; its own
    failure handler is log.debug only (no recursion)."""
    try:
        from guardian.terminal_feedback import emit_phase

        emit_phase(
            "hud",
            operation,
            status=status,
            summary=summary,
            component="hud.telemetry",
            failure_class=failure_class,
            **fields,
        )
    except Exception as _e:  # noqa: BLE001 - never recurse into emit
        log.debug("telemetry emit_phase failed (%s): %s", operation, _e)


# =====================================================================
# Status pill states (HUD-2)
# =====================================================================
# Glyph state vocabulary — drives the .status-glyph class on the HUD.
# Keep this set small + stable so the CSS animation table can rely on it.
STATE_LIVE = "live"  # subsystem fully operational
STATE_LOADING = "loading"  # subsystem booting / not yet ready
STATE_DEGRADED = "degraded"  # subsystem reachable but reporting issues
STATE_DOWN = "down"  # subsystem unreachable / failed


def _pill(state: str, label: str, detail: str) -> Dict[str, Any]:
    """Builder — single source of truth for the pill JSON shape."""
    return {"state": state, "label": label, "detail": detail}


# ---------------------------------------------------------------------
# HUD-2: per-pill probes
# Each probe is best-effort + returns a shape-correct payload even on
# failure. The HUD's status bar reads this every ~2s so each probe
# must be cheap.
# ---------------------------------------------------------------------


def _probe_voice() -> Dict[str, Any]:
    """Voice pill — probe the backend sensory stack.

    Voice is live only when both backend halves are reachable through Omega's
    local services. Browser Web Speech is not the canonical boot path. Both
    halves route through the portable local-services process, and each check is
    NAMED by the engine that service reports: a hardcoded name cannot be wrong
    about a swap it never reads, which is how the retired engine survived here
    (2026-09-20).
    """
    checks: list[tuple[str, bool, str]] = []
    try:
        from runtime.tts import status as _tts_status

        tts = _tts_status()
        checks.append(
            (
                str(tts.get("engine") or "tts"),
                bool(tts.get("available")),
                "ready" if tts.get("available") else "down",
            )
        )
    except Exception as exc:
        # The probe itself failed, so no engine is known and none is invented:
        # the capability is named instead.
        checks.append(("tts", False, f"{type(exc).__name__}"))
    try:
        from runtime.stt import status as _stt_status

        stt = _stt_status()
        checks.append(
            (
                "parakeet",
                bool(stt.get("available")),
                "ready" if stt.get("available") else "down",
            )
        )
    except Exception as exc:
        checks.append(("parakeet", False, f"{type(exc).__name__}"))

    ready = [name for name, ok, _ in checks if ok]
    missing = [name for name, ok, _ in checks if not ok]
    detail = " · ".join(f"{name}:{state if ok else 'missing'}" for name, ok, state in checks)
    if len(ready) == 2:
        return _pill(STATE_LIVE, "voice", detail)
    if ready:
        return _pill(STATE_DEGRADED, "voice", detail)
    return _pill(STATE_DOWN, "voice", detail or f"missing: {', '.join(missing)}")


def _probe_identity(broker_snapshot: Mapping[Any, Any]) -> Dict[str, Any]:
    """Identity pill — surface current identity-gate health.

    The pill goes LIVE when the FACE capability is non-failing AND the
    operator presence primitive is not UNKNOWN (fail-closed). It can
    still incorporate charter/ledger integrity as legacy detail, but
    charter probe MUST NOT flip a live/broker+presence posture to DOWN.
    """
    # --- Current system SoT: brain-local CPU face status + presence primitive ---
    face_state, face_reason = _local_face_state()

    # Presence UNKNOWN means fail-closed identity gate (production only).
    presence_state = "UNKNOWN"
    try:
        from sensory.operator_presence import read_presence_from_shared_state

        ss = _get_shared_state()
        if ss is not None:
            presence_state = read_presence_from_shared_state(ss)
    except Exception as exc:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "presence_read_failed",
            ok=False,
            error=str(exc),
        )
        presence_state = "UNKNOWN"

    try:
        from guardian.production_mode import is_production_mode

        production = bool(is_production_mode())
    except Exception:
        production = False

    if production and (presence_state == "UNKNOWN" or face_state in ("down",)):
        detail = " · ".join(
            p
            for p in (
                f"face:{face_state}"
                + (f" · {face_reason}" if face_reason and face_state != "available" else ""),
                "presence:UNKNOWN — fail-closed"
                if presence_state == "UNKNOWN"
                else f"presence:{presence_state}",
            )
            if p
        )
        return _pill(STATE_DOWN, "identity", detail)

    base_state = (
        STATE_LIVE
        if face_state == "available"
        else STATE_DEGRADED
        if face_state in ("degraded", "unknown")
        else STATE_DOWN
    )

    # Operator presence ABSENT is a held-gate posture: never fully LIVE.
    if presence_state == "ABSENT" and base_state == STATE_LIVE:
        base_state = STATE_DEGRADED

    # Operator anchors (best-effort; optional)
    op_count = 0
    op_name = ""
    try:
        from runtime.face.anchor_store import get_anchor_store

        names = get_anchor_store().names()
        op_count = len(names)
        op_name = names[0] if names else ""
    except Exception as exc:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "operator_name_read_failed",
            ok=False,
            error=str(exc),
        )

    parts: list[str] = []
    parts.append(f"face:{face_state}")
    if face_reason and face_state != "available":
        parts.append(face_reason)
    parts.append(f"presence:{presence_state}")

    if op_count:
        parts.append(op_name if op_count == 1 and op_name else f"{op_count} operators")
    else:
        parts.append("awaiting operator")

    detail = " · ".join([p for p in parts if p])
    return _pill(base_state, "identity", detail)


def _local_face_state() -> tuple[str, str]:
    try:
        from runtime.face.recognition import face_status

        status = face_status()
    except Exception as exc:
        return "unknown", type(exc).__name__
    if status.get("face_ready"):
        return "available", "brain-local-cpu"
    return "down", str(status.get("reason") or "face models unavailable")


def _probe_local_face() -> Dict[str, Any]:
    state, reason = _local_face_state()
    detail = state if not reason else f"{state} · {reason}"
    if state == "available":
        return _pill(STATE_LIVE, "face", detail)
    if state == "unknown":
        return _pill(STATE_DEGRADED, "face", detail)
    return _pill(STATE_DOWN, "face", detail)


def _probe_retina() -> Dict[str, Any]:
    """Resident Ettin encoder + currently mapped projection codes."""
    try:
        from memory import retina

        stats = retina.stats()
    except Exception as exc:
        return _pill(STATE_DOWN, "ettin", f"probe raised: {type(exc).__name__}")
    resident = bool(stats.get("encoder_resident"))
    loaded = bool(stats.get("loaded"))
    codes = int(stats.get("codes", 0) or 0)
    if resident and loaded:
        return _pill(STATE_LIVE, "ettin", f"resident · {codes} codes")
    if resident:
        return _pill(STATE_LOADING, "ettin", "resident · projection codes pending")
    return _pill(STATE_DOWN, "ettin", "encoder not seated")


def _probe_memory(broker_snapshot: Mapping[Any, Any]) -> Dict[str, Any]:
    """Memory pill — probe Ettin, StrengthIndex, and encoding journal state."""
    _ = broker_snapshot  # compatibility with the shared panel-status call shape
    retina_pill = _probe_retina()
    embed_state = {
        STATE_LIVE: "available",
        STATE_LOADING: "loading",
        STATE_DEGRADED: "degraded",
        STATE_DOWN: "down",
    }.get(str(retina_pill.get("state") or ""), "unknown")
    embed_reason = str(retina_pill.get("detail") or "")

    if embed_state == "down":
        detail = f"embed:{embed_state}" + (f" · {embed_reason}" if embed_reason else "")
        return _pill(STATE_DOWN, "memory", detail)

    try:
        from memory.strength_index import ensure_strength_index
    except Exception as exc:
        detail = " · ".join(
            p
            for p in (
                f"embed:{embed_state}"
                + (f" · {embed_reason}" if embed_reason and embed_state != "available" else ""),
                f"substrate missing: {type(exc).__name__}",
            )
            if p
        )
        return _pill(STATE_DEGRADED, "memory", detail)

    try:
        si = ensure_strength_index()
        records = len(getattr(si, "_records", {}) or {})

        # Encoding journal health is read-only here.  Recovery remains a boot
        # responsibility, but malformed rows must still degrade the HUD signal
        # instead of being reported as a clean journal.
        journal_clean = False
        journal_detail = "journal unavailable"
        try:
            from memory.encoding_journal import journal_status

            status = journal_status()
            journal_clean = bool(status.get("clean", False))
            malformed_count = int(status.get("malformed_count", 0) or 0)
            unclosed_count = int(status.get("unclosed_count", 0) or 0)
            if malformed_count:
                line_numbers = ",".join(str(line) for line in status.get("malformed_lines", []))
                suffix = f"; lines {line_numbers}" if line_numbers else ""
                journal_detail = f"journal MALFORMED({malformed_count}{suffix})"
            elif unclosed_count:
                journal_detail = f"journal DIRTY({unclosed_count} unclosed)"
            elif status.get("error"):
                journal_detail = "journal ERROR"
            else:
                journal_detail = "journal clean"
        except Exception as exc:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "hud",
                "journal_check",
                ok=False,
                error=str(exc)[:120],
            )
            journal_clean = False

        if records == 0:
            detail = " · ".join(
                p
                for p in (
                    f"embed:{embed_state}"
                    + (f" · {embed_reason}" if embed_reason and embed_state != "available" else ""),
                    "0 records · cortex empty",
                )
                if p
            )
            if embed_state in ("degraded", "unknown"):
                return _pill(STATE_DEGRADED, "memory", detail)
            return _pill(STATE_LOADING, "memory", detail)

        parts: list[str] = []
        parts.append(f"embed:{embed_state}")
        if embed_reason and embed_state != "available":
            parts.append(embed_reason)
        parts.append(f"{records} records")
        parts.append(journal_detail)
        detail = " · ".join([p for p in parts if p])

        # Overall state follows the in-process retina and journal.
        if embed_state in ("degraded", "unknown"):
            return _pill(STATE_DEGRADED, "memory", detail)
        if embed_state == "loading":
            return _pill(STATE_LOADING, "memory", detail)
        return _pill(STATE_LIVE if journal_clean else STATE_DEGRADED, "memory", detail)
    except Exception as exc:
        detail = " · ".join(
            p
            for p in (
                f"embed:{embed_state}"
                + (f" · {embed_reason}" if embed_reason and embed_state != "available" else ""),
                f"probe raised: {type(exc).__name__}",
            )
            if p
        )
        return _pill(STATE_DEGRADED, "memory", detail)


def _probe_l0() -> Dict[str, Any]:
    """L0 semantic-core pill — the in-brain Ettin retina."""
    retina_pill = _probe_retina()
    return _pill(
        str(retina_pill.get("state") or STATE_DOWN),
        "semantic core",
        f"Ettin · {retina_pill.get('detail') or 'unknown'}",
    )


# =====================================================================
# P3-S04 — v2 substrate probes (presence / pressure-loop / broker caps)
# =====================================================================
#
# Each probe returns the same `{state, label, detail}` pill shape used
# above so the HUD's existing 2s `/api/status` poll covers the new
# v2 surfaces without a separate endpoint per glyph. Probes are
# best-effort + return shape-correct payload even on failure.
# Spec: docs/architecture/HUD_PILL_TELEMETRY_V2.md §5.7 + §8.


def _probe_presence() -> Dict[str, Any]:
    """Operator-presence pill — PRESENT (green) / ABSENT (amber) /
    UNKNOWN (red, fail-CLOSED per the watcher's degraded-runtime
    doctrine). Reads PRESENCE_STATE from the v2 PressureRuntime
    SharedState via the canonical accessor.
    """
    try:
        from sensory.operator_presence import (
            read_presence_from_shared_state,
            enrolled_operator_name,
        )
    except Exception as exc:
        return _pill(STATE_DEGRADED, "presence", f"probe deps missing: {type(exc).__name__}")
    try:
        ss = _get_shared_state()
        if ss is None:
            return _pill(STATE_DEGRADED, "presence", "runtime not registered")
        state = read_presence_from_shared_state(ss)
        enrolled = enrolled_operator_name() or ""
    except Exception as exc:
        return _pill(STATE_DEGRADED, "presence", f"probe raised: {type(exc).__name__}")
    if state == "PRESENT":
        detail = f"PRESENT ({enrolled})" if enrolled else "PRESENT"
        return _pill(STATE_LIVE, "presence", detail)
    if state == "ABSENT":
        detail = f"ABSENT ({enrolled})" if enrolled else "ABSENT"
        return _pill(STATE_DEGRADED, "presence", detail)
    # UNKNOWN — fail-CLOSED, treat as DOWN for the HUD glyph
    return _pill(STATE_DOWN, "presence", "UNKNOWN — face module degraded")


def _probe_pressure_loop() -> Dict[str, Any]:
    """Pressure-loop pause pill — running (live) / paused (loading,
    using blue-tinted "loading" glyph; UI surfaces owner list in
    tooltip via the /api/pressure-loop/state endpoint for detail).
    The pill itself is intentionally NOT amber/down when paused —
    a paused substrate is an OPERATOR-INITIATED state, not a failure.
    """
    try:
        from pressure_loop.pressure_loop_pause import pressure_loop_pause_state
    except Exception as exc:
        return _pill(STATE_DEGRADED, "pressure", f"probe deps missing: {type(exc).__name__}")
    try:
        s = pressure_loop_pause_state()
        paused = bool(s.get("paused"))
        owners = list(s.get("owners") or [])
    except Exception as exc:
        return _pill(STATE_DEGRADED, "pressure", f"probe raised: {type(exc).__name__}")
    if not paused:
        return _pill(STATE_LIVE, "pressure", "running")
    detail = f"paused by {', '.join(owners)}" if owners else "paused"
    return _pill(STATE_LOADING, "pressure", detail)


# Broker capability → HUD pill mapping. Enum-typed at module load so
# any typo lands as ImportError, not a silently DEGRADED pill (banked
# P3-S04 review finding I3 — stringly-typed names hid silent
# corruption). The glyph label is the HUD display string (kept short
# for the pill bar).
try:
    from runtime.runtime_capability_broker import RuntimeCapability as _RC

    _BROKER_PILL_MAP = ((_RC.CODING_MODEL, "llm", "coding_model"),)
except Exception as _broker_pill_exc:  # noqa: BLE001
    log.warning(
        "broker pill map load failed (%s); v2 pills will report DEGRADED until imports recover",
        _broker_pill_exc,
    )
    _RC = None  # type: ignore
    _BROKER_PILL_MAP = ()


def _probe_capability_from_status(
    status: Any,
    glyph_label: str,
) -> Dict[str, Any]:
    """Render a broker-capability pill from a pre-fetched
    RuntimeCapabilityStatus. Used by panel_status() to avoid N
    separate /health HTTP fetches per request (banked P3-S04 review
    finding I1 — `check_runtime_capability` does NOT cache health
    body; calling it 4 times per status poll meant 4 HTTP roundtrips,
    8s wall in the worst case with a 2s health timeout × 4).
    """
    if status is None:
        return _pill(STATE_DEGRADED, glyph_label, "snapshot unavailable")
    state_str = status.state.value
    detail = state_str
    if status.reason and state_str != "available":
        detail = f"{state_str} · {status.reason}"
    if state_str == "available":
        return _pill(STATE_LIVE, glyph_label, detail)
    if state_str == "degraded":
        return _pill(STATE_DEGRADED, glyph_label, detail)
    return _pill(STATE_DOWN, glyph_label, detail)


_CHARTER_CACHE: Dict[str, Any] | None = None
_CHARTER_CACHE_KEY: Any = None


def _charter_cache_key() -> Any:
    """Identity of the ledger chain file — changes only on a real ledger write."""
    try:
        from guardian.hmac_ledger import DEFAULT_LEDGER_PATH

        st = DEFAULT_LEDGER_PATH.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None  # absent (dev / pre-seal) — stable key


def _probe_charter() -> Dict[str, Any]:
    """Charter / HMAC ledger posture for identity panel (not status pills).

    Cached + invalidated on the ledger chain file's mtime/size: the posture
    only changes when the ledger is written, so the full-chain verify() runs
    ~once at boot instead of on every ~2s HUD refresh (avoids read amplification
    + contention with the reflection inference).
    """
    global _CHARTER_CACHE, _CHARTER_CACHE_KEY
    key = _charter_cache_key()
    if _CHARTER_CACHE is not None and _CHARTER_CACHE_KEY == key:
        return dict(_CHARTER_CACHE)
    try:
        from guardian.hmac_ledger import HMACLedger
        from guardian.production_mode import is_production_mode

        production = bool(is_production_mode())
        ledger = HMACLedger()
        loaded = bool(ledger.load()) if production else False
        verify_ok, last_seq = ledger.verify()
        entry0 = ledger.get_entry(0) if loaded else None
        identity_hash = "dev-deferred"
        if entry0 is not None:
            identity = entry0.payload.get("identity")
            if isinstance(identity, dict):
                identity_hash = str(
                    identity.get("identity_hash") or identity.get("id") or entry0.entry_hmac[:16]
                )
            else:
                identity_hash = entry0.entry_hmac[:16]
        result = {
            "mode": "production" if production else "dev",
            "identity_hash": identity_hash,
            "verify_ok": bool(verify_ok),
            "ledger_ok": bool(verify_ok) and (loaded if production else True),
            "ledger_entries": (last_seq + 1) if last_seq >= 0 else 0,
            "born": bool(loaded),
        }
        _CHARTER_CACHE = result
        _CHARTER_CACHE_KEY = key
        return dict(result)
    except Exception as exc:
        log.debug("panel_identity: charter probe failed: %s", exc)
        # FAILURE_LOG coverage (charter.load / charter.verify / ledger.verify):
        # the combined probe must EMIT the integrity-unknown failure, not just
        # log.debug it, so it reaches guardian/logs/failures.jsonl. The returned
        # dict is an honest degraded posture (mode "unknown", placeholder dashes).
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "guardian",
                "charter_probe_failed",
                status="degraded",
                ok=False,
                error=str(exc)[:160],
            )
        except Exception as _e:
            log.debug("panel_identity: charter_probe_failed emit skipped: %s", _e)
        return {
            "mode": "unknown",
            "identity_hash": "—",
            "verify_ok": False,
            "ledger_ok": False,
            "ledger_entries": "—",
            "born": False,
        }


def panel_status() -> Dict[str, Any]:
    """Top status bar — 5 legacy pills + 6 v2 pills.

    Each legacy pill is independently probed (their probes touch
    different subsystems and don't compose).

    v2 broker pills share a SINGLE `runtime_capability_snapshot()` call for
    runtime-owned capabilities. Face is brain-local CPU and is probed separately.
    Classification is deterministic and memory semantics come
    from the in-process Ettin retina.
    instead of 4 separate fetches. Banked P3-S04 review finding.

    Legacy pills (HUD pre-v2): voice / identity / memory / l0.
    v2 pills (P3-S04, per HUD_PILL_TELEMETRY_V2.md §5.7):
      presence       — operator presence (PRESENT/ABSENT/UNKNOWN)
      pressure_loop  — pressure-loop pause state (running/paused)
      embed          — broker EMBED capability
      classify       — broker CLASSIFY capability
       face           — brain-local CPU face capability
      coding_model   — broker CODING_MODEL (LLM) capability
    """
    # Build the broker snapshot ONCE so 4 capability pills share one
    # runtime /health call. On import / fetch error the snapshot is
    # an empty dict; the per-pill renderer maps that to DEGRADED.
    broker_snapshot: Dict[Any, Any] = {}
    try:
        from runtime.runtime_capability_broker import (
            runtime_capability_snapshot,
        )

        broker_snapshot = runtime_capability_snapshot()
    except Exception as exc:
        log.debug("panel_status: broker snapshot failed (%s)", exc)
        broker_snapshot = {}

    out: Dict[str, Any] = {
        # Legacy 5 — independent probes
        "voice": _probe_voice(),
        "identity": _probe_identity(broker_snapshot),
        "memory": _probe_memory(broker_snapshot),
        "l0": _probe_l0(),
        "embed": _probe_retina(),
        "classify": _pill(STATE_LIVE, "classify", "nominal (deterministic)"),
        "face": _probe_local_face(),
        # v2 — single-subsystem probes
        "presence": _probe_presence(),
        "pressure_loop": _probe_pressure_loop(),
    }
    # v2 broker pills — render from the shared snapshot.
    for cap_enum, glyph_label, pill_key in _BROKER_PILL_MAP:
        status = broker_snapshot.get(cap_enum)
        out[pill_key] = _probe_capability_from_status(status, glyph_label)

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "panel_status",
            ok=True,
            panel="status",
            voice_state=out.get("voice", {}).get("state", ""),
            identity_state=out.get("identity", {}).get("state", ""),
            memory_state=out.get("memory", {}).get("state", ""),
            presence_state=out.get("presence", {}).get("state", ""),
        )
    except Exception:
        pass

    return out


_SPA_STATUS_PILL_KEYS = (
    "voice",
    "identity",
    "memory",
    "l0",
    "presence",
    "pressure_loop",
    "embed",
    "classify",
    "face",
    "coding_model",
)


def panel_status_spa_substrate(pills: Dict[str, Any]) -> Dict[str, str]:
    """Map legacy ``panel_status`` cells to SPA status-bar glyph strings.

    React SPA reads ``/api/status`` as ``{substrate: {voice: "ok", ...}}``
    and passes each value through ``P3()`` for color mapping.
    """
    substrate: Dict[str, str] = {}
    for key in _SPA_STATUS_PILL_KEYS:
        cell = pills.get(key) if isinstance(pills.get(key), dict) else {}
        state = str(cell.get("state") or "")
        detail = str(cell.get("detail") or "")
        if key == "presence":
            if "PRESENT" in detail:
                substrate[key] = "PRESENT"
            elif "ABSENT" in detail:
                substrate[key] = "ABSENT"
            else:
                substrate[key] = "UNKNOWN"
        elif key == "pressure_loop":
            if state == STATE_LIVE:
                substrate[key] = "running"
            elif state == STATE_LOADING:
                substrate[key] = "paused"
            elif state == STATE_DEGRADED:
                substrate[key] = "degraded"
            else:
                substrate[key] = "fail"
        elif key in ("embed", "classify", "face", "coding_model"):
            if state == STATE_LIVE:
                substrate[key] = "ok"
            elif state == STATE_DEGRADED:
                substrate[key] = "degraded"
            else:
                substrate[key] = "fail"
        elif state == STATE_LIVE:
            substrate[key] = "ok"
        elif state == STATE_LOADING:
            substrate[key] = "unknown"
        elif state == STATE_DEGRADED:
            substrate[key] = "degraded"
        elif state == STATE_DOWN:
            substrate[key] = "fail"
        else:
            substrate[key] = "unknown"
    return substrate


# Module-level cache for the first-ready timestamp. Once the system
# reaches READY, the banner should stay faded even if a transient
# pill blip later. Without this cache, every poll would recompute
# ready_at_iso (or worse, allow the banner to re-appear).
_first_ready_iso: str = ""


# Pills that MUST be live for the system to be considered ready.
# Per operator: voice is required because Omega is hands-free / all
# voice. Identity is intentionally excluded — dev mode keeps it
# deferred (loading) by design; that should not block readiness.
_READY_REQUIRED = ("voice", "l0", "memory")


def panel_boot_state() -> Dict[str, Any]:
    """Boot banner state (HUD-3). Drives the .banner fade-out.

    Phase vocabulary:
        booting  — at least one required pill is `loading`, none down
        ready    — all required pills are `live`
        degraded — at least one required pill is `down` or `degraded`

    Required pills (per operator spec): voice, l0, memory.
    Voice is REQUIRED because Omega operates hands-free / all voice;
    identity is intentionally excluded (dev mode deferred is OK).

    Once ready is reached, ready_at_iso is cached at module level so
    the banner stays faded across subsequent polls — a transient pill
    blip after ready does not re-show the banner.
    """
    global _first_ready_iso
    status = panel_status()

    states = {pill: status[pill]["state"] for pill in _READY_REQUIRED}
    not_live = [p for p, s in states.items() if s != STATE_LIVE]
    has_dead = any(s in (STATE_DOWN, STATE_DEGRADED) for s in states.values())

    if not not_live:
        # All required pills live -> ready
        if not _first_ready_iso:
            from datetime import datetime, timezone

            _first_ready_iso = datetime.now(timezone.utc).isoformat()
        phase = "ready"
        summary = f"all required substrates live ({', '.join(_READY_REQUIRED)})"
    elif has_dead:
        phase = "degraded"
        broken = [p for p, s in states.items() if s in (STATE_DOWN, STATE_DEGRADED)]
        summary = f"degraded: {', '.join(broken)} not reachable"
    else:
        phase = "booting"
        summary = f"loading: {', '.join(not_live)} not yet live"

    result = {
        "phase": phase,
        "ready_at_iso": _first_ready_iso or None,
        "summary": summary,
        "required": list(_READY_REQUIRED),
        "states": states,
    }

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "panel_boot_state",
            ok=True,
            panel="boot_state",
            phase=phase,
            ready_at_iso=_first_ready_iso or None,
        )
    except Exception:
        pass

    return result


def reset_boot_state() -> None:
    """Test-only helper — clear the first-ready cache so successive
    test runs don't see stale `ready_at_iso` values."""
    global _first_ready_iso
    _first_ready_iso = ""


# =====================================================================
# HUD-4..9: unified telemetry snapshot
# =====================================================================
# The design's `Telemetry` singleton (omega_interface.html line 1180)
# exposes update(patch) with deep-merge semantics. /api/telemetry/snapshot
# returns a partial patch containing whatever live data we can probe
# cheaply; unwired fields keep their mock values until subsequent
# iterations populate them.
#
# Top-level keys mirror the design's state shape:
#   cycle / layers / memory / omega / system / autoloop
# =====================================================================

_runtime_absent_logged = False


def _get_shared_state():
    """Resolve v2 PressureRuntime.shared_state. None if not booted."""
    global _runtime_absent_logged
    try:
        from pressure_loop.runtime import try_get_default_runtime

        rt = try_get_default_runtime()
        if rt is None:
            if not _runtime_absent_logged:
                logging.getLogger("omega.hud.telemetry").debug(
                    "no PressureRuntime registered — HUD telemetry degraded"
                )
                _runtime_absent_logged = True
            return None
        return rt.shared_state
    except Exception as exc:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "shared_state_read_failed",
            ok=False,
            error=str(exc),
        )
        return None


def _shared_state_read(key: str) -> Any:
    """Best-effort shared_state read."""
    ss = _get_shared_state()
    if ss is None:
        return None
    try:
        return ss.read(key)
    except Exception as exc:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "shared_state_key_read_failed",
            ok=False,
            error=str(exc),
        )
        return None


def _pressure_live_snapshot() -> Dict[str, Any]:
    """Consolidated pressure telemetry from audit ring + shared_state.

      The v2 loop emits ``agg`` on ``pressure.evaluate`` phase exits and
      ``aggregate_pressure`` on ``episode_ignition`` data events.  Prior
      HUD reads only looked at episode_ignition and broke on the first
      ``pressure.tick`` — so the brain panel stayed empty between ignitions
    and never showed evaluate-time pressure at all.
    """
    snapshot: Dict[str, Any] = {}
    try:
        raw_tick = _shared_state_read("_pressure_tick")
        if raw_tick is not None:
            snapshot["tick"] = int(raw_tick)
    except Exception:
        pass
    try:
        latest_eval: Dict[str, Any] | None = None
        latest_gather_weights: Dict[str, Any] | None = None
        latest_ignition: Dict[str, Any] | None = None
        latest_tick: Dict[str, Any] | None = None

        for rec in reversed(_audit_events(800)):
            loop = str(rec.get("loop") or rec.get("system") or "")
            if loop != "pressure":
                continue
            kind = str(rec.get("kind") or "")
            phase = str(rec.get("phase") or "")
            status = str(rec.get("status") or "")

            if kind == "data" and phase == "episode_ignition" and latest_ignition is None:
                latest_ignition = rec
            elif kind == "phase" and phase == "evaluate" and status == "ok" and latest_eval is None:
                latest_eval = rec
            elif (
                kind == "phase"
                and phase == "gather_contributors"
                and status == "ok"
                and latest_gather_weights is None
            ):
                weights = rec.get("weights")
                if isinstance(weights, dict) and weights:
                    latest_gather_weights = weights
            elif kind == "phase" and phase == "tick" and latest_tick is None:
                latest_tick = rec

            if (
                latest_eval is not None
                and latest_gather_weights is not None
                and latest_ignition is not None
                and latest_tick is not None
            ):
                break

        if latest_eval is not None:
            agg = latest_eval.get("agg")
            if agg is None:
                agg = latest_eval.get("aggregate_pressure")
            if agg is not None:
                snapshot["pressure_aggregate"] = float(agg)
            if latest_eval.get("dominant"):
                snapshot["dominant"] = str(latest_eval.get("dominant"))
            if latest_eval.get("ts"):
                snapshot["last_eval_at"] = latest_eval.get("ts")

        if latest_ignition is not None:
            snapshot["opened_episode"] = True
            snapshot["opened_by"] = str(latest_ignition.get("opened_by") or "")
            snapshot["suppress_reason"] = ""
            dominant_contributor = latest_ignition.get("dominant_contributor")
            if isinstance(dominant_contributor, dict):
                snapshot["dominant_contributor"] = dict(dominant_contributor)
            trajectory_snapshot = latest_ignition.get("trajectory_snapshot")
            if isinstance(trajectory_snapshot, dict):
                snapshot["trajectory_snapshot"] = dict(trajectory_snapshot)
            if latest_ignition.get("ts"):
                snapshot["last_tick_at"] = latest_ignition.get("ts")
            if snapshot.get("pressure_aggregate") is None:
                agg = latest_ignition.get("aggregate_pressure")
                if agg is not None:
                    snapshot["pressure_aggregate"] = float(agg)
            contrib_list = latest_ignition.get("contributors") or []
            if isinstance(contrib_list, list) and contrib_list:
                snapshot["ignition_contributors"] = contrib_list

        if latest_tick is not None:
            if latest_tick.get("ts"):
                snapshot.setdefault("last_tick_at", latest_tick.get("ts"))
            tick_status = str(latest_tick.get("status") or "")
            if tick_status == "skip":
                summary = str(
                    latest_tick.get("summary") or latest_tick.get("suppress_reason") or ""
                )
                snapshot["opened_episode"] = False
                snapshot["suppress_reason"] = summary or "skipped"
            elif not snapshot.get("opened_episode") and latest_ignition is None:
                snapshot.setdefault("opened_episode", False)
                snapshot.setdefault("suppress_reason", "")

        if latest_gather_weights is not None:
            snapshot["weights"] = latest_gather_weights

        try:
            from pressure_loop.pressure_loop_pause import pressure_loop_pause_state

            ps = pressure_loop_pause_state() or {}
            owners = list(ps.get("owners") or [])
            if owners:
                snapshot["pause_owners"] = owners
        except Exception:
            pass
    except Exception:
        pass
    return snapshot


def _pipeline_snapshots() -> Dict[str, Any]:
    """Physical envelopes are the current execution authority."""
    from core.pipeline_state import envelopes
    from pressure_loop.cognition_queue import queue_depth

    now = time.time()
    rows: Dict[str, Any] = {}
    for envelope in envelopes():
        row = envelope.to_dict()
        durable_state = str(row.pop("state"))
        row["state"] = (
            "free" if durable_state == "IDLE" else "failed" if durable_state == "FAILED" else "running"
        )
        row["elapsed_s"] = 0.0
        if row["state"] == "running":
            try:
                from core.pipeline_state import envelope_path

                row["elapsed_s"] = round(max(0.0, now - envelope_path(envelope.pipeline_id).stat().st_mtime), 3)
            except OSError:
                pass
        rows[str(envelope.pipeline_id)] = row
    return {
        "pipelines": rows,
        "queue_depth": queue_depth(),
    }


def pipeline_telemetry() -> Dict[str, Any]:
    """Uncached physical-pipeline telemetry for the HUD.

    Deliberately NOT folded into ``telemetry_snapshot``: that snapshot is
    TTL-cached and deep-merged as one object, and pipeline state is the fastest
    moving thing on the panel — an episode can open, run and close inside
    one cached window. A worker view served from a stale cache reproduces the
    exact defect this exists to fix, just with more steps.

    Shape:

        {"pipelines": {"0": {pipeline_id, state, phase, queue_id,
                              cycle_id, job_id, work_kind,
                              active_episode_id, elapsed_s}, ...},
         "queue_depth": int}

    The durable envelope is the authority. Work waiting in the shared queue has
    no physical owner and therefore never receives a fabricated assignment.
    """
    return _pipeline_snapshots()


def _clamped_unit(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        number = fallback
    return round(min(1.0, max(0.0, number)), 3)


def _cognition_visual_label(raw: Any) -> str:
    text = str(raw or "").strip().lower().replace("-", "_")
    if not text:
        return "signal"
    if "operator" in text:
        return "operator"
    if "learn" in text:
        return "learning"
    if "curiosity" in text or "question" in text or "clarif" in text:
        return "inquiry"
    if "contrad" in text or "conflict" in text:
        return "contrast"
    if "verify" in text or "evidence" in text:
        return "verify"
    if "memory" in text or "taxonomy" in text or "recall" in text:
        return "memory"
    if "response" in text or "reply" in text or "chat" in text:
        return "reply"
    if "rollback" in text or "restore" in text:
        return "restore"
    if "skill" in text or "build" in text or "tool" in text or "execute" in text:
        return "act"
    if "proactive" in text or "outreach" in text:
        return "outreach"
    return "signal"


def _cognition_motion_label(action: Any, intent: Any = "") -> str:
    text = f"{action or ''} {intent or ''}".strip().upper()
    if not text:
        return "pending"
    if "RESPONSE" in text or "RESPOND" in text or "REPLY" in text:
        return "reply"
    if "USE_SKILL" in text or "APPLY" in text or "EXECUTE" in text or "BUILD" in text:
        return "act"
    if "LEARN" in text:
        return "learn"
    if "ASK" in text or "CLARIFY" in text:
        return "ask"
    if "IDLE" in text or "NOOP" in text or "HOLD" in text:
        return "hold"
    return "select"


def _latest_cognition_motion() -> str:
    try:
        from pressure_loop.episode_journal import journal_window

        window = journal_window(1)
        if not window:
            return "pending"
        last = window[-1]
        return _cognition_motion_label(
            getattr(last, "action", None),
            getattr(last, "intent", None),
        )
    except Exception:
        return "pending"


def _cognition_visuals_from_pressure_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    winner_raw = snapshot.get("opened_by") or snapshot.get("dominant") or ""
    aggregate = snapshot.get("pressure_aggregate")
    trajectory = snapshot.get("trajectory_snapshot")
    trajectory = trajectory if isinstance(trajectory, dict) else {}
    progress = (
        trajectory.get("progress")
        if trajectory.get("progress") is not None
        else trajectory.get("path_progress")
    )
    if progress is None:
        progress = trajectory.get("confidence")
    path_active = bool(trajectory)
    motion = _latest_cognition_motion()
    return {
        "winner": {
            "label": _cognition_visual_label(winner_raw),
            "level": _clamped_unit(aggregate),
            "state": "active" if winner_raw else "pending",
        },
        "path": {
            "label": "active" if path_active else "pending",
            "level": _clamped_unit(progress, 0.0 if not path_active else 0.5),
            "state": "active" if path_active else "pending",
        },
        "motion": {
            "label": motion,
            "state": "selected" if motion != "pending" else "pending",
        },
    }


def last_pressure_tick_snapshot() -> Dict[str, Any] | None:
    """Best-effort v2 pressure-tick snapshot (replaces LAST_CUEDESK_TICK)."""
    snapshot = _pressure_live_snapshot()
    if snapshot:
        snapshot["cognition_visuals"] = _cognition_visuals_from_pressure_snapshot(snapshot)
    return snapshot or None


# Canonical layer position map. The 11 slots cover the 5 brain phases
# (intake/retrieve/translate/respond/store) + the 5 cognitive loops
# (auto/meta/global/omega/deepsleep) + the brain-loop close. Total =
# 11 — the brain panel renders position as "<n> / 11 — <friendly>".
_LAYER_POSITIONS: Dict[str, tuple] = {
    "pressure": (1, "pressure"),
    "pressure.tick": (1, "pressure"),
    "pressure.gather_contributors": (2, "gather"),
    "pressure.evaluate": (3, "evaluate"),
    "pressure.ignite_episode": (4, "ignite"),
    "brain.intake": (5, "perceive"),
    "brain.retrieve": (6, "memory_resolution"),
    "brain.translate": (7, "translate"),
    "brain.respond": (8, "respond"),
    "brain.store": (9, "encode"),
    "brain": (10, "brain_loop"),
    "global": (11, "global"),
    "meta": (12, "meta"),
    "auto": (13, "autonomous"),
    "omega": (14, "omega"),
    "deepsleep": (15, "deepsleep"),
}
_LAYER_TOTAL = max(pos[0] for pos in _LAYER_POSITIONS.values())


def _layer_pretty_name(layer_id: str) -> str:
    pos = _LAYER_POSITIONS.get(layer_id)
    if pos:
        return pos[1]
    # Strip "brain." / other prefixes for unknown layers
    return layer_id.split(".", 1)[-1]


def _layer_summary(rec: Dict[str, Any]) -> str:
    """Best-effort one-line summary for a layer_state record."""
    schema = rec.get("schema") or {}
    if isinstance(schema, dict) and schema.get("summary"):
        return str(schema["summary"])[:96]
    j = rec.get("justification") or ""
    return str(j)[:96] if j else ""


def _layer_tag(rec: Dict[str, Any]) -> str:
    did_work = bool(rec.get("did_work"))
    j = (rec.get("justification") or "").lower()
    if "exception" in j or "failed" in j or "error" in j:
        return "fail"
    if not did_work:
        return "skip"
    return "ok"


def _telemetry_cycle() -> Dict[str, Any]:
    """`cycle` subtree — current cycle id + source + layer + LIVE +
    pressure.

    P3-S28 HUD live-feed doctrine v3 — reads RECENT_LAYER_STATES ring
    (EPHEMERAL_RUNTIME SharedState, last 40 records) instead of tailing
    the on-disk layer-state log. Boot-empty by construction; fills as
    layers emit. Disk log remains the forensic source-of-truth.
    Pre-cure this was the most-polled disk read in the HUD (every 2s)."""
    import time as _time
    from datetime import datetime as _dt

    out: Dict[str, Any] = {
        "id": "—",
        "source": "—",
        "layer": "—",
        "pressure": "—",
        "live": False,
    }
    try:
        from core.shared_state_keys import RECENT_LAYER_STATES

        ring = _shared_state_read(RECENT_LAYER_STATES) or []
        recs = ring if isinstance(ring, list) else []
        # Preserve prior last-12 semantics — current-cycle hero only
        # needs the most recent slice for layer-position computation.
        if len(recs) > 12:
            recs = recs[-12:]
    except Exception:
        recs = []
    if recs:
        last = recs[-1]
        out["id"] = last.get("cycle_id") or "—"
        # `source` lives on the loop-close record (layer_kind=='loop')
        # for that cycle. Walk backwards to find the most recent loop
        # record carrying it; fall back to last record's own field.
        for r in reversed(recs):
            if r.get("layer_kind") == "loop" and r.get("source"):
                out["source"] = r["source"]
                break
        else:
            # `schema` field can be dict / None / (rarely) string;
            # only call .get() when it's actually a dict.
            schema_obj = last.get("schema")
            schema_source = schema_obj.get("source") if isinstance(schema_obj, dict) else None
            out["source"] = last.get("source") or schema_source or "—"
        # Layer position — most recent NON-loop emission for clarity;
        # fall back to last record if everything in the tail is "loop".
        layer_rec = None
        for r in reversed(recs):
            if r.get("layer_kind") != "loop":
                layer_rec = r
                break
        if layer_rec is None:
            layer_rec = last
        lid = layer_rec.get("layer_id") or "—"
        pos = _LAYER_POSITIONS.get(lid)
        if pos:
            out["layer"] = f"{pos[0]} / {_LAYER_TOTAL} — {pos[1]}"
        else:
            out["layer"] = lid
        # LIVE if the most recent record is < 5 s old AND it isn't
        # the loop-close record (which signals the cycle is done).
        try:
            ts = last.get("ts") or ""
            ts_dt = _dt.fromisoformat(ts.replace("Z", "+00:00"))
            age_s = _time.time() - ts_dt.timestamp()
            is_close = last.get("layer_kind") == "loop"
            out["live"] = (age_s < 5.0) and not is_close
        except Exception:
            pass
    # Fallback live signal: recent pressure/cognition phases in audit ring.
    if not out.get("live"):
        try:
            import time as _time
            from datetime import datetime as _dt

            for p in reversed(_phases_from_audit(limit=8)):
                ts = p.get("ts") or ""
                if not ts:
                    continue
                ts_dt = _dt.fromisoformat(ts.replace("Z", "+00:00"))
                age_s = _time.time() - ts_dt.timestamp()
                if age_s < 8.0 and p.get("status") in ("ok", "enter"):
                    out["live"] = True
                    if out["id"] == "—":
                        out["id"] = p.get("episode_id") or p.get("loop") or "—"
                    lid = f"{p.get('loop')}.{p.get('phase')}"
                    pos = _LAYER_POSITIONS.get(lid) or _LAYER_POSITIONS.get(
                        str(p.get("loop") or "")
                    )
                    if pos and out["layer"] == "—":
                        out["layer"] = f"{pos[0]} / {_LAYER_TOTAL} — {pos[1]}"
                    elif out["layer"] == "—":
                        out["layer"] = lid
                    break
        except Exception:
            pass
    # Fallback: shared_state if layer log empty
    if out["id"] == "—":
        last_resp = _shared_state_read("last_brain_response")
        if isinstance(last_resp, dict):
            out["id"] = last_resp.get("cycle_id") or "—"
            out["source"] = last_resp.get("source") or "—"
    # Pressure — prefer live tick aggregate, then cadence scheduler.
    tick_snap = last_pressure_tick_snapshot() or {}
    agg = tick_snap.get("pressure_aggregate")
    if isinstance(agg, (int, float)):
        out["pressure"] = f"{float(agg):.2f}"
    else:
        try:
            sched = _get_cached_scheduler()
            if sched is not None:
                ps = getattr(sched, "_pressure_score", None) or getattr(
                    sched, "pressure_score", None
                )
                if isinstance(ps, (int, float)):
                    out["pressure"] = f"{ps:.2f}"
        except Exception:
            pass
    return out


def _telemetry_layers(limit: int = 6) -> list:
    """Top-level `layers` list — last N layer_state records mapped to
    {id (friendly), info (summary), tag (ok/skip/fail)}. Drives the
    brain panel's RECENT LAYERS section.

    P3-S28: reads from RECENT_LAYER_STATES ring instead of disk-tail."""
    try:
        from core.shared_state_keys import RECENT_LAYER_STATES

        ring = _shared_state_read(RECENT_LAYER_STATES) or []
        recs = ring[-limit:] if isinstance(ring, list) else []
    except Exception:
        return []
    out = []
    for r in recs:
        lid = r.get("layer_id") or "?"
        out.append(
            {
                "id": _layer_pretty_name(lid).replace(".", "_"),
                "info": _layer_summary(r),
                "tag": _layer_tag(r),
            }
        )
    return out


def _strength_tag(score: float) -> str:
    """Bucket a write_strength into the HUD's HIGH/MED/LOW tag."""
    if score >= 0.7:
        return "HIGH"
    if score >= 0.4:
        return "MED"
    return "LOW"


def _relative_age(iso_ts: str) -> str:
    """Format an ISO timestamp as 'just now' / 'Nm ago' / 'Nh ago' / 'Nd ago'.

    HUD live-feed stabilization (2026-05-28): the sub-minute bucket used
    to return a per-SECOND value ("5s ago" -> "6s ago"), which changed on
    every 2s telemetry poll. Panels hash their telemetry slice to decide
    whether to re-render, so a per-second field churned the hash and
    forced a destructive card rebuild every poll. Coarsening the
    sub-minute bucket to a stable string ('just now') means the value
    only changes at minute/hour boundaries — the hash stays stable
    between real state changes and the card stops resetting."""
    if not iso_ts:
        return "—"
    from datetime import datetime as _dt
    import time as _time

    try:
        dt = _dt.fromisoformat(iso_ts.replace("Z", "+00:00"))
        age = max(0, _time.time() - dt.timestamp())
    except Exception:
        return "—"
    if age < 60:
        return "just now"
    if age < 3600:
        return f"{int(age / 60)}m ago"
    if age < 86400:
        return f"{int(age / 3600)}h ago"
    return f"{int(age / 86400)}d ago"


def _coord_gist(coord: str) -> str:
    """Build a friendly gist from a coord like '2.03.gradient_descent'.
    Real per-record gists need a memory.read by id (deferred — single-
    shot lookups would slow the snapshot poll). Slug-from-coord gives
    the operator something readable until that's wired."""
    parts = (coord or "").split(".", 2)
    slug = parts[-1] if len(parts) == 3 else coord
    return slug.replace("_", " ")


def _telemetry_memory() -> Dict[str, Any]:
    """`memory` subtree — recall trace from last brain.retrieve, top
    recall list, persistence anchors for the top-1 memory.

    Source-of-truth fields:
        recallTrace[]  — derived from last brain.retrieve schema counts
        topRecall[]    — retrieved_memory_refs zipped with relevance_scores
        anchors        — StrengthIndex stats for the top-1 memory
                         (recurrence, last_access, strength, strengthTag)
    """
    out: Dict[str, Any] = {
        "anchors": {"recurrence": "—", "last_access": "—", "strength": "—", "strengthTag": ""},
        "topRecall": [],
        "recallTrace": [],
    }

    # --- Pull the last brain.retrieve record from the ring -----
    # P3-S28: reads RECENT_LAYER_STATES (last 40) instead of disk-tail.
    schema: Dict[str, Any] = {}
    try:
        from core.shared_state_keys import RECENT_LAYER_STATES

        ring = _shared_state_read(RECENT_LAYER_STATES) or []
        recs = ring if isinstance(ring, list) else []
        for r in reversed(recs):
            if r.get("layer_id") == "brain.retrieve":
                schema = r.get("schema") or {}
                break
    except Exception:
        pass

    refs = schema.get("retrieved_memory_refs") or []
    scores = schema.get("relevance_scores") or []
    contras = schema.get("contradictions") or []

    # --- MP-1: Recall trace stages ---------------------------------
    # Real pipeline emits these counts inside the schema; we render
    # them as the trace lines so the operator sees what actually ran
    # rather than the static design mock.
    if schema:
        cand_count = max(len(refs), len(scores))
        out["recallTrace"] = [
            {"step": "candidates", "value": f"{cand_count} retrieved from index"},
            {
                "step": "embed similarity",
                "value": f"{sum(1 for s in scores if s > 0.55)} above 0.55 cosine"
                if scores
                else "—",
            },
            {
                "step": "rerank arbitrate",
                "value": f"top-{min(3, len(refs))} selected" if refs else "no candidates",
            },
            {
                "step": "contradiction merge",
                "value": (f"{len(contras)} flagged" if contras else "none flagged"),
            },
            {"step": "final pack", "value": f"{len(refs)} memories returned"},
        ]
    else:
        out["recallTrace"] = [{"step": "—", "value": "no recall on record yet"}]

    # --- MP-2: Top recall (zip refs + scores) -----------------------
    pairs = list(zip(refs, scores))
    pairs.sort(key=lambda p: p[1], reverse=True)
    out["topRecall"] = [
        {
            "coord": ref,
            "gist": _coord_gist(ref),
            "score": f"{score:.2f}" if isinstance(score, (int, float)) else str(score),
        }
        for ref, score in pairs[:5]
    ]

    # --- MP-3: Persistence anchors for the top-1 memory ------------
    si_records: Dict[str, Any] = {}
    try:
        from memory.strength_index import ensure_strength_index

        si = ensure_strength_index()
        si_records = getattr(si, "_records", {}) or {}
    except Exception:
        pass

    top_rec = None
    if pairs and si_records:
        top_rec = si_records.get(pairs[0][0])
    if top_rec:
        touch = int(top_rec.get("touch_count", 0))
        ing_at = (top_rec.get("ingested_at") or "")[:10]  # YYYY-MM-DD
        last = top_rec.get("last_access") or ""
        ws = float(top_rec.get("write_strength") or 0.0)
        out["anchors"] = {
            "recurrence": f"{touch} (since {ing_at})" if ing_at else f"{touch}",
            "last_access": _relative_age(last),
            "strength": f"{ws:.2f}",
            "strengthTag": _strength_tag(ws),
        }
    elif si_records:
        # Top-1 not in StrengthIndex (or no recall yet) — fall back to
        # corpus-wide stats so the panel reads live rather than blank.
        out["anchors"] = {
            "recurrence": f"{len(si_records)} records indexed",
            "last_access": "—",
            "strength": f"{len(si_records)}",
            "strengthTag": "",
        }

    # ---- P3-S14: Wave 2C — memory panel data extension ------------
    # Add 4 fields powering the rebuilt panel: recent_writes feed,
    # lobe_activity heatmap, l0_state broker snapshot, recall_mode.
    out["recent_writes"] = []
    out["lobe_activity"] = {}  # coord_key (e.g. "1.0") → count (last hour)
    out["lobe_activity_total"] = 0
    out["l0_state"] = {"embed": "unknown", "classify": "unknown"}
    out["recall_mode"] = "semantic"  # downgraded to "lexical_fallback" on degraded EMBED
    out["persistence_anchors"] = []  # top-8 strongest memories from StrengthIndex
    out["pending_promotions"] = []  # near-empty under P3-S11 doctrine

    # --- Top-8 persistence anchors from StrengthIndex --------------
    try:
        if si_records:
            ranked = sorted(
                si_records.items(),
                key=lambda kv: float((kv[1] or {}).get("write_strength") or 0.0),
                reverse=True,
            )[:8]
            for mid, rec in ranked:
                if not isinstance(rec, dict):
                    continue
                title = (rec.get("title") or rec.get("summary") or mid)[:80]
                strength = float(rec.get("write_strength") or 0.0)
                out["persistence_anchors"].append(
                    {
                        "id": str(mid),
                        "title": title,
                        "strength": round(strength, 3),
                        "touch": int(rec.get("touch_count", 0)),
                    }
                )
    except Exception:
        pass

    # --- L0 Ettin retina state -------------------------------------
    try:
        retina_pill = _probe_retina()
        embed_state = str(retina_pill.get("state") or "unknown").lower()
        out["l0_state"] = {
            "embed": embed_state,
            "routing": "deterministic",
        }
        if embed_state != STATE_LIVE:
            out["recall_mode"] = "lexical_fallback"
    except Exception:
        pass

    # --- Recent writes feed + lobe activity heatmap ----------------
    # P3-S25 HUD live-feed doctrine v2 — read the in-process events ring
    # via `_audit_events` filtered to memory.write.summary signals.
    # No disk tail. Ring is bounded (~5-10 min on hot substrate); operator
    # intent for this card is "what just landed in memory?" — bounded
    # recency is correct. Disk audit log remains the forensic record.
    try:
        from datetime import datetime, timezone, timedelta

        def _is_memory_write_summary(rec: Dict[str, Any]) -> bool:
            if rec.get("kind") == "signal" and rec.get("name") == "memory.write.summary":
                return True
            loop = str(rec.get("loop") or rec.get("system") or "")
            phase = str(rec.get("phase") or "")
            return rec.get("kind") == "data" and loop == "memory" and phase == "write_memory"

        ring = _audit_events(
            predicate=_is_memory_write_summary,
            newest_first=False,
        )

        now_dt = datetime.now(timezone.utc)
        hour_ago = now_dt - timedelta(hours=1)

        writes_collected: list = []
        for rec in ring:
            outcome = str(
                rec.get("outcome") or rec.get("validation_status") or rec.get("status") or ""
            ).strip()
            ts = rec.get("ts") or ""
            coord = rec.get("coord") or rec.get("memory_id") or rec.get("canonical_ref") or ""
            # Coord might be a string id like "6.00.37c5ff70" —
            # extract the X.Y prefix for the heatmap bucket.
            if isinstance(coord, (list, tuple)) and len(coord) >= 2:
                try:
                    lobe_key = f"{int(coord[0])}.{int(coord[1])}"
                except (TypeError, ValueError):
                    lobe_key = None
                coord_str = ".".join(str(part) for part in coord)
            else:
                coord_str = str(coord)
                parts = coord_str.split(".")
                if len(parts) >= 2 and parts[0].isdigit():
                    lobe_key = f"{int(parts[0])}.{int(parts[1])}"
                else:
                    lobe_key = None
            writes_collected.append(
                {
                    "ts": ts,
                    "outcome": outcome,
                    "coord": coord_str[:32],
                    "memory_id": str(rec.get("memory_id") or rec.get("canonical_ref") or "")[:32],
                    "narrative": str(
                        rec.get("narrative") or rec.get("summary") or rec.get("type") or ""
                    )[:80],
                }
            )
            # Bucket by lobe (last hour only).
            if lobe_key is not None and ts:
                try:
                    rec_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if rec_dt >= hour_ago:
                        out["lobe_activity"][lobe_key] = out["lobe_activity"].get(lobe_key, 0) + 1
                except Exception:
                    pass

        writes_collected.reverse()
        out["recent_writes"] = writes_collected[:10]
        out["lobe_activity_total"] = sum(out["lobe_activity"].values())
    except Exception as exc:
        log.debug("memory panel writes feed ring read failed: %s", exc)

    out["dashboard"] = _memory_dashboard_metrics(out)
    return out


# ----- Telemetry formatting helpers ----------------------------------


def _age_short(iso_ts: str) -> str:
    """Compact age formatter — 'now', '12M', '2H', '1D'. Returns '—'
    for empty / unparseable. Different from _relative_age (which
    appends 'ago').

    HUD live-feed stabilization (2026-05-28): the sub-minute bucket used
    to return a per-SECOND value ('5S' -> '6S'), churning the panel hash
    on every 2s poll and forcing a card rebuild. Coarsened to a stable
    'now' so the value only changes at minute/hour boundaries."""
    if not iso_ts:
        return "—"
    from datetime import datetime as _dt
    import time as _time

    try:
        dt = _dt.fromisoformat(iso_ts.replace("Z", "+00:00"))
        sec = max(0, _time.time() - dt.timestamp())
    except Exception:
        return "—"
    if sec < 60:
        return "now"
    if sec < 3600:
        return f"{int(sec / 60)}M"
    if sec < 86400:
        return f"{int(sec / 3600)}H"
    return f"{int(sec / 86400)}D"


def _contradiction_tag(score: float) -> str:
    if score >= 0.6:
        return "HIGH"
    if score >= 0.3:
        return "MED"
    return "LOW"


def _telemetry_omega() -> Dict[str, Any]:
    """`omega` subtree — every field driving the omega panel:

    objective            — long-arc objective (current_objective)
    online               — days since first capability tick + status
    posture              — last omega evaluation status (or default)
    coherence            — self_vector confidence + drift tag
    traits               — list of {key,value,delta,arrow} (empty for now)
    contradiction        — {score, tag, oppositions[]}
    gaps                 — list of {id,subject,age}
    capability           — list of {label,delta,window}
    timeline             — list of {day,event,tag}
    emergent             — list of {cycle,day,kind,detail}
    journal              — DeepSleep journal aggregate (per build_log
                           0106): runs/finalized/pattern_type_counts/
                           trait_emergence_count/total_source_cycles/
                           latest_snapshot. Time-traversal across all
                           consolidation records.
    """

    out: Dict[str, Any] = {
        "objective": "—",
        "online": "—",
        "onlineLabel": "uninterrupted",
        "posture": "consolidating",
        "coherence": "—",
        "coherenceTag": "—",
        "epoch": 0,
        "reflection": None,
        # These are assigned below in the new P3-S16/5W1H pipeline; we
        # include reflection here because the contract test requires
        # `omega.reflection` to exist even when no records are present.
        "traits": [],
        "contradiction": {"score": 0.0, "tag": "LOW", "oppositions": []},
        "gaps": [],
        "capability": [],
        "timeline": [],
        "emergent": [],
    }

    # ---- OP-1: current objective ----------------------------------
    proposals = _shared_state_read("last_omega_proposals")
    if isinstance(proposals, dict):
        plist = proposals.get("proposals") or []
        if plist:
            first = plist[0]
            out["objective"] = str(
                first.get("reason") or first.get("target") or first.get("description") or "—"
            )[:240]

    # ---- OP-2: online days + posture + identity coherence ---------
    # P3-S28 — capability_metrics.jsonl is no longer written by any
    # production code path (the legacy capability_metric.py writer was
    # retired pre-2026-05-08). The file doesn't exist on disk, so the
    # prior conditional disk-read returned empty and `online`/
    # `onlineLabel`/`timeline` fell through to defaults anyway. The
    # disk dependency is dropped here per the HUD live-feed doctrine
    # (no disk audit logs as live sources). Future slice: re-source
    # `online_days` from a real live signal (IdentityCharter created_at
    # or a `boot_timestamp` SS key) if the operator wants the field
    # restored. Until then the default "—" / "uninterrupted" stays.

    # Cycle epoch — top-right counter on the HUD.
    #
    # CANONICAL SOURCE (per `project_guardian_sole_writer_doctrine`):
    # the live SharedState `_epoch` key, published by `boot.run_continuous`
    # at the start of every epoch. That's the authoritative live tick —
    # what the loop scheduler sees right now. Pre-P3-S28 there was a
    # disk-tail fallback against capability_metrics.jsonl; dropped now
    # that the file no longer exists.
    epoch_live = _shared_state_read("_epoch")
    if isinstance(epoch_live, int) and epoch_live > 0:
        out["epoch"] = int(epoch_live)
    else:
        out["epoch"] = 0

    eval_ = _shared_state_read("last_omega_evaluation")
    if isinstance(eval_, dict):
        st = str(eval_.get("status") or "").strip()
        if st:
            out["posture"] = st

    # Identity coherence from self_vector_history (latest confidence).
    # P3-S28 HUD live-feed doctrine v3 — read RECENT_SELF_VECTORS ring
    # (EPHEMERAL_RUNTIME SharedState, last 20 entries, restart-empty)
    # instead of tail-reading the disk history file. Disk
    # self_vector_history.jsonl remains the forensic source-of-truth.
    try:
        from core.shared_state_keys import RECENT_SELF_VECTORS

        sv_ring = _shared_state_read(RECENT_SELF_VECTORS) or []
        if isinstance(sv_ring, list) and sv_ring:
            latest = sv_ring[-1] if isinstance(sv_ring[-1], dict) else {}
            conf = float(latest.get("confidence") or 0.0)
            out["coherence"] = f"{conf:.2f}"
            # Drift tag — compare latest two vectors
            if len(sv_ring) >= 2 and isinstance(sv_ring[-2], dict):
                prev = sv_ring[-2]
                v1 = latest.get("vector") or []
                v0 = prev.get("vector") or []
                if v0 and v1 and len(v0) == len(v1):
                    import math

                    drift = math.sqrt(sum((a - b) ** 2 for a, b in zip(v0, v1)))
                    out["coherenceTag"] = "stable" if drift < 0.15 else "drifting"
                else:
                    out["coherenceTag"] = "stable"
            else:
                out["coherenceTag"] = "stable"
    except Exception:
        pass

    # ---- OP-3: trait trajectory ----------------------------------
    # Two-tier surface (2026-05-12 — Trait commit pipeline landed):
    #   1. COMMITTED traits — Trait() records written to lobe 0 (0,2)
    #      via core.trait_writer. These are the system's REAL traits;
    #      they exist in memory, carry MeasurableBehavior, and survive
    #      across boots. Surfaced as `value: "committed"`.
    #   2. PROPOSED-but-uncommitted — DeepSleep emitted a trait but
    #      the commit pipeline hasn't run yet (or failed). Surfaced
    #      as `value: "proposed"` so the operator sees the in-flight
    #      signal without confusing it with state.
    #
    # Both tiers dedupe by trait_name (operator's principle: each
    # DeepSleep cycle re-proposes the same trait when the underlying
    # pattern persists; surfacing N rows = noise, surfacing one with
    # an "Nx emitted / N finalized" delta preserves the reliability
    # signal AND keeps the panel readable).
    try:
        from core.trait_writer import list_committed_traits

        committed = list_committed_traits() or []
    except Exception as exc:
        log.debug("trait trajectory: trait_writer unavailable: %s", exc)
        committed = []

    try:
        from core import deepsleep_store

        records = deepsleep_store.list_all() or []
        records.sort(key=lambda r: r.timestamp, reverse=True)
        by_name: dict = {}
        order: list = []
        for r in records:
            te = getattr(r, "trait_evolution", None)
            if not te or not getattr(te, "new_trait_created", False):
                continue
            name = (getattr(te, "trait_name", None) or "—").strip()
            if name not in by_name:
                by_name[name] = {
                    "name": name,
                    "emissions": 0,
                    "approved_count": 0,
                    "latest_reason": (getattr(te, "trait_reason", None) or "").strip()[:96],
                    "latest_cycle_id": r.sleep_cycle_id,
                    "latest_ts": r.timestamp,
                }
                order.append(name)
            agg = by_name[name]
            agg["emissions"] += 1
            if getattr(r, "finalized", False):
                agg["approved_count"] += 1

        rows: list = []
        # Walk committed traits first — those have the strongest
        # status (real Trait() in memory). Then any proposed-only
        # names that haven't reached commit yet.
        seen: set = set()
        for trait in committed:
            name = (trait.get("name") or "—").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            agg = by_name.get(name) or {
                "emissions": int(trait.get("reproductions", 1) or 1),
                "approved_count": 1,
                "latest_ts": trait.get("last_finalized_at") or trait.get("emerged_at") or "",
                "latest_reason": (trait.get("description") or "")[:96],
                "latest_cycle_id": trait.get("last_consolidation_id")
                or trait.get("proposal_id")
                or "",
            }
            mb = trait.get("measurable_behavior") or {}
            metric = mb.get("metric") or "—"
            current = mb.get("current")
            current_txt = (
                f"{current:.2f}" if isinstance(current, (int, float)) else str(current or "—")
            )
            age_short = _age_short(agg["latest_ts"])
            reps = int(trait.get("reproductions", agg.get("emissions", 1)) or 1)
            delta_txt = f"{metric}={current_txt} · {reps}× confirmed · {age_short}"
            rows.append(
                {
                    "name": name,
                    "value": "committed",
                    "delta": delta_txt,
                    "arrow": "stable",
                    "_raw_metric": metric,
                    "_raw_current": current,
                    "_raw_emerged_at": trait.get("emerged_at"),
                    "_raw_trait_id": trait.get("trait_id"),
                    "_raw_reason": trait.get("description") or "",
                }
            )

        # Then proposed-only (no committed record yet).
        for name in order:
            if name in seen or len(rows) >= 8:
                continue
            seen.add(name)
            agg = by_name[name]
            n = agg["emissions"]
            approved = agg["approved_count"]
            status = "proposed" if approved == 0 else "finalized_uncommitted"
            age_short = _age_short(agg["latest_ts"])
            delta_txt = f"{n}× emitted · latest {age_short}" + (
                f" · {approved} finalized" if approved else ""
            )
            rows.append(
                {
                    "name": name,
                    "value": status,
                    "delta": delta_txt,
                    "arrow": "up",
                    "_raw_cycle_id": agg["latest_cycle_id"],
                    "_raw_timestamp": agg["latest_ts"],
                    "_raw_emissions": n,
                    "_raw_reason": agg["latest_reason"],
                }
            )

        out["traits"] = rows[:8]
    except Exception as exc:
        log.debug("trait trajectory probe failed: %s", exc)
        out["traits"] = []

    # ---- OP-4: contradiction load ---------------------------------
    # P3-S28: reads RECENT_LAYER_STATES ring (last 40) instead of disk-tail.
    contras: list = []
    try:
        from core.shared_state_keys import RECENT_LAYER_STATES

        ring = _shared_state_read(RECENT_LAYER_STATES) or []
        recs = ring if isinstance(ring, list) else []
        for r in reversed(recs):
            if r.get("layer_id") == "brain.retrieve":
                contras = list((r.get("schema") or {}).get("contradictions") or [])
                break
    except Exception:
        pass
    cont_score = min(1.0, len(contras) * 0.06)  # 17 contras → ~1.0
    out["contradiction"] = {
        "score": cont_score,
        "scoreLabel": f"{cont_score:.2f}",
        "tag": _contradiction_tag(cont_score),
        "oppositions": [
            {
                "left": str(c.get("left") or c.get("a") or "—"),
                "right": str(c.get("right") or c.get("b") or "—"),
                "weight": float(c.get("weight") or c.get("score") or 0.0),
            }
            for c in contras[:5]
            if isinstance(c, dict)
        ],
    }

    # ---- OP-5: unresolved cognitive gaps --------------------------
    try:
        from hud.curiosity_store import list_open as _cur_open

        for e in (_cur_open() or [])[:6]:
            cid = str(getattr(e, "curiosity_id", ""))
            short = ("GAP_" + cid.replace("cur-", "")[:8]).upper()
            ts = getattr(e, "created_at", "") or ""
            out["gaps"].append(
                {
                    "id": short,
                    "subject": str(getattr(e, "problem_statement", ""))[:96],
                    "age": _age_short(ts) if ts else "—",
                }
            )
    except Exception:
        pass

    # ---- OP-6: capability compounding (30D delta) -----------------
    # P3-S28 — the 30D capability-delta computation depended on
    # capability_metrics.jsonl (now retired) for both the tool /
    # curiosity / omega capability metrics AND the multi-day baseline
    # comparison anchor. With no live source for capability metrics
    # over a 30-day horizon, this card can't honestly compute deltas.
    # `out["capability"]` stays empty until a slice rebuilds the
    # surface from a live metrics source. Tracked as a follow-up.

    # ---- OP-7: identity timeline ---------------------------------
    # P3-S28 — the legacy timeline derived from capability_metrics.jsonl
    # GENESIS + deepsleep-consolidation milestones is retired. The
    # underlying file is no longer written by any production code path,
    # and the doctrine forbids the HUD reading disk audit logs as live
    # state. `out["timeline"]` stays empty until a slice reseeds the
    # surface from a live source — candidates: RECENT_DEEPSLEEP_CYCLES
    # ring (P3-S25) + IdentityCharter created_at for the GENESIS entry.
    # Tracked as a follow-up; intentionally NOT bundled into P3-S28 to
    # keep the disk-removal cure focused.

    # ---- WIRE-7: tool synthesis activity --------------------------
    # Reads the reliability store + curiosity store to surface:
    #   - tools by reliability bucket (sandbox_only / graduated /
    #     deprecated / retired)
    #   - tool_creation curiosity entries currently open (the
    #     autonomous engine's queue depth)
    #   - recently registered tools (from guardian/logs/tool_decisions.jsonl
    #     per build_log 0107)
    out["tool_synthesis"] = _tool_synthesis_summary()

    # ---- WIRE-8: DeepSleep journal time-traversal aggregate -------
    # Per build_log 0106: DeepSleep is the cascade terminus; each
    # consolidation record snapshots upstream loop telemetry into
    # `upstream_telemetry`. Omega's view of "what happened across
    # time" comes from aggregating those records via
    # `metacognition.omega.phases.phase_1_intake.state_collector.
    # _aggregate_deepsleep_journal()`.
    out["journal"] = {
        "runs": 0,
        "finalized": 0,
        "unfinalized": 0,
        "pattern_type_counts": {},
        "trait_emergence_count": 0,
        "policy_proposal_count": 0,
        "identity_anchor_updates": 0,
        "total_source_cycles": 0,
        "total_reflection_refs": 0,
        "latest_sleep_cycle_id": None,
        "latest_timestamp": None,
        "latest_snapshot_slots": [],
    }
    try:
        from metacognition.omega.phases.phase_1_intake.state_collector import (
            _aggregate_deepsleep_journal,
        )

        agg = _aggregate_deepsleep_journal()
        out["journal"].update(
            {
                "runs": int(agg.get("runs", 0)),
                "finalized": int(agg.get("finalized", 0)),
                "unfinalized": int(agg.get("unfinalized", 0)),
                "pattern_type_counts": dict(agg.get("pattern_type_counts") or {}),
                "trait_emergence_count": int(agg.get("trait_emergence_count", 0)),
                "policy_proposal_count": int(agg.get("policy_proposal_count", 0)),
                "identity_anchor_updates": int(agg.get("identity_anchor_updates", 0)),
                "total_source_cycles": int(agg.get("total_source_cycles", 0)),
                "total_reflection_refs": int(agg.get("total_reflection_refs", 0)),
                "latest_sleep_cycle_id": agg.get("latest_sleep_cycle_id"),
                "latest_timestamp": agg.get("latest_timestamp"),
                # Surface only the slot keys (not the full payload — keeps
                # the panel response light; full snapshot is on the
                # deepsleep panel via panel_deepsleep upstream_telemetry).
                "latest_snapshot_slots": sorted((agg.get("latest_snapshot") or {}).keys()),
            }
        )
    except Exception as exc:
        log.debug("_telemetry_omega: journal aggregate failed (%s)", exc)

    # ---- 2026-05-14: Active OmegaTrajectory + lifecycle ----------
    # Surface omega's long-arc directional pressure: what omega last
    # decided to push the system toward, how many cycles remain,
    # whether global has been honoring it. Reads the live shared-state
    # slot (LAST_OMEGA_TRAJECTORY) directly so the panel reflects the
    # operator-visible state in real time. Empty dict when no
    # trajectory is active.
    try:
        traj = _shared_state_read("last_omega_trajectory")
        if isinstance(traj, dict) and traj:
            cycles_remaining = int(traj.get("cycles_remaining", 0) or 0)
            persistence = int(traj.get("persistence_cycles", 5) or 5)
            completed = bool(traj.get("completed"))
            out["trajectory"] = {
                "active": not completed and cycles_remaining > 0,
                "trajectory_id": traj.get("trajectory_id", ""),
                "intent": traj.get("intent", ""),
                "target_domain": traj.get("target_domain", ""),
                "target_capability": traj.get("target_capability", ""),
                "rationale": (traj.get("rationale") or "")[:200],
                "influencer_text": (traj.get("influencer_text") or "")[:200],
                "cycles_remaining": cycles_remaining,
                "persistence_cycles": persistence,
                "progress": (round((persistence - cycles_remaining) / max(1, persistence), 3)),
                "completed": completed,
                "completion_reason": traj.get("completion_reason", ""),
                "epoch_emitted": int(traj.get("epoch_emitted", 0) or 0),
            }
        else:
            out["trajectory"] = {"active": False}
    except Exception as exc:
        log.debug("_telemetry_omega: trajectory read failed (%s)", exc)
        out["trajectory"] = {"active": False}

    # ---- Topic fold (THE FOLD) — last critical open question absorbed
    # into trajectory resolution for LEARN episodes.
    try:
        fold = _shared_state_read("last_topic_fold")
        if isinstance(fold, dict) and fold.get("active"):
            out["topic_fold"] = {
                "active": True,
                "target_domain": str(fold.get("target_domain") or "")[:120],
                "question_id": str(fold.get("question_id") or "")[:64],
                "question_text": str(fold.get("question_text") or "")[:200],
                "scoped_topic": str(fold.get("scoped_topic") or "")[:240],
                "at": fold.get("at"),
            }
        else:
            out["topic_fold"] = {"active": False}
    except Exception as exc:
        log.debug("_telemetry_omega: topic_fold read failed (%s)", exc)
        out["topic_fold"] = {"active": False}

    # ---- P3-S16: Trajectory Star Map — live ring + audit fallback.
    out["recent_epochs"] = _omega_epochs_unified()

    # ---- 5W1H reflection: most recent summary ----------------------
    # Reflection persists to the identity lobe via the memory gate
    # (`omega_reflection` records). The HUD should surface the most recent
    # reflection without disk tails; we use the identity gate's recency
    # primitive and project a small, stable shape.
    try:
        from metacognition.omega.reflection.reflection_store import list_recent_reflections

        recs = list_recent_reflections(limit=1) or []
        if recs and isinstance(recs[0], dict):
            r = recs[0]
            prov = r.get("provenance") if isinstance(r.get("provenance"), dict) else {}
            traj = r.get("trajectory") if isinstance(r.get("trajectory"), dict) else {}
            raw_text = str(r.get("text") or "")
            raw_summary = str(r.get("summary") or "")
            summary = (raw_summary or raw_text).strip()
            out["reflection"] = {
                "title": str(
                    r.get("title")
                    or raw_summary
                    or (raw_text[:96] if raw_text else "")
                    or "omega reflection"
                )[:160],
                "domain": str(r.get("domain") or "")[:96],
                "summary": summary[:320],
                "ingested_at": prov.get("ingested_at") if isinstance(prov, dict) else None,
                "trajectory": {
                    "target_domain": traj.get("target_domain"),
                    "target_capability": traj.get("target_capability"),
                    "rationale": (str(traj.get("rationale") or "")[:200]),
                    "confidence": traj.get("confidence"),
                }
                if traj
                else {},
            }
    except Exception as exc:
        log.debug("_telemetry_omega: reflection read failed (%s)", exc)

    out["dashboard"] = _omega_dashboard_metrics(out)
    _spa_enrich_omega(out)
    return out


def _spa_enrich_omega(out: Dict[str, Any]) -> None:
    """Add React SPA field aliases expected by the omega slide-out panel."""
    traj = out.get("trajectory") if isinstance(out.get("trajectory"), dict) else {}
    reflection = out.get("reflection") if isinstance(out.get("reflection"), dict) else {}
    ref_traj = (
        reflection.get("trajectory") if isinstance(reflection.get("trajectory"), dict) else {}
    )
    out["trajectory_decision"] = (
        traj.get("intent")
        or ref_traj.get("target_capability")
        or ("active" if traj.get("active") else "—")
    )
    out["trajectory_diff"] = traj.get("target_domain") or ref_traj.get("target_domain") or "—"
    if traj.get("active"):
        persistence = int(traj.get("persistence_cycles") or 10)
        remaining = int(traj.get("cycles_remaining") or 0)
        out["window_episodes"] = max(0, persistence - remaining)
        out["window_size"] = persistence
    else:
        out.setdefault("window_episodes", 0)
        out.setdefault("window_size", 10)
    if isinstance(out.get("trajectory"), dict):
        out["trajectory"].setdefault(
            "decision",
            out.get("trajectory_decision", "—"),
        )
        out["trajectory"].setdefault(
            "diff",
            out.get("trajectory_diff", "—"),
        )


def _spa_enrich_deepsleep(out: Dict[str, Any]) -> None:
    """Add React SPA field aliases expected by the deepsleep slide-out panel."""
    phase_states = out.get("phase_states") if isinstance(out.get("phase_states"), list) else []
    phases: list = []
    for row in phase_states:
        if not isinstance(row, dict):
            continue
        phases.append(dict(row))
        alias = {
            "reactivation": "consolidate",
            "reconciliation": "consolidate_memory",
            "persistence": "closure",
        }.get(str(row.get("phase") or ""))
        if alias:
            phases.append(
                {
                    "phase": alias,
                    "name": row.get("name"),
                    "status": row.get("status"),
                    "summary": row.get("summary"),
                }
            )
    out["phases"] = phases
    out["phase"] = out.get("current_phase") or ""
    out["stores"] = {
        "intake": out.get("intake") or {},
        "replay": out.get("replay") or {},
        "schemas": out.get("schemas") or {},
        "contradictions": out.get("contradictions") or {},
        "forgetting": out.get("forgetting") or {},
        "consolidation": out.get("consolidation") or {},
        "pruning": out.get("pruning") or {},
    }


def _tool_synthesis_summary() -> Dict[str, Any]:
    """Rolls up the live tool-synthesis state for the omega panel.

    Buckets:
        registry  — count by lifecycle status (sandbox/graduated/
                    deprecated/retired) + top reliability scores;
                    derived from the cascade journal cascade
                    (autonomous-side cognitive tools)
        queue     — open curiosity entries with recommended_path=
                    tool_creation (what's about to be synthesised)
        recent    — last N Guardian tool decisions
                    (guardian/logs/tool_decisions.jsonl per build_log 0107)
        dev       — operator-facing dev-tool invocation rollup from
                    `core.dev_tool_registry.aggregate_invocation_stats`
                    (2026-05-14: makes dev-agent + dev-pipeline
                    tool calls visible in the HUD, same transparency
                    pattern the autonomous cascade gets)
    """
    summary: Dict[str, Any] = {
        "registry": {
            "sandbox_only": 0,
            "graduated": 0,
            "deprecated": 0,
            "retired": 0,
            "top_reliability": [],
        },
        "queue": [],
        "recent": [],
        "dev": {
            "total_invocations": 0,
            "successful_invocations": 0,
            "tools_observed": 0,
            "by_tool": {},
            "recent_calls": [],
        },
    }
    # --- Registry: tool reliability stats ---
    # 2026-05-14: tool_reliability_store retired. Derive per-tool stats
    # from the cascade telemetry stored in DeepSleep journal records
    # (each record's upstream.auto.telemetry carries tool_called +
    # tool_success). Same source omega's intake reads — single source
    # of truth for "what tools fired and how often."
    try:
        from metacognition.omega.phases.phase_1_intake.state_collector import (
            _derive_tool_reliability_from_cascade,
        )
        from core.paths import TOOL_SCHEMA_ROOT

        # Tool inventory: cognitive tools live as ToolSpec JSON files
        # under schema/tools/<name>.json (the canonical record).
        cognitive_count = (
            sum(1 for _ in TOOL_SCHEMA_ROOT.glob("*.json")) if TOOL_SCHEMA_ROOT.exists() else 0
        )
        summary["registry"]["sandbox_only"] = cognitive_count

        rel = _derive_tool_reliability_from_cascade()
        by_tool = rel.get("by_tool") or {}
        # Top 5 by success_rate × invocations (composite reliability)
        ranked = sorted(
            by_tool.items(),
            key=lambda kv: (kv[1].get("success_rate", 0.0) or 0.0) * kv[1].get("invocations", 0),
            reverse=True,
        )[:5]
        summary["registry"]["top_reliability"] = [
            {
                "tool_id": name,
                "tool_name": name,
                "reliability": round(float(stats.get("success_rate", 0.0) or 0.0), 3),
                "success_rate": round(float(stats.get("success_rate", 0.0) or 0.0), 3),
                "invocation_count": int(stats.get("invocations", 0) or 0),
                "status": "sandbox_only",
            }
            for name, stats in ranked
        ]
        summary["registry"]["total_invocations"] = rel.get("total_invocations", 0)
        summary["registry"]["successful_invocations"] = rel.get("successful_invocations", 0)
        summary["registry"]["epochs_observed"] = rel.get("epochs_observed", 0)
    except Exception as exc:
        log.debug("tool_synthesis_summary: cascade-derived registry failed (%s)", exc)
    # --- Queue: open tool_creation curiosities ---
    # Filter FIRST, then slice — otherwise the cap eats non-tool-
    # creation entries and the queue silently misreports as empty.
    try:
        from hud.curiosity_store import list_open as _cur_open

        opens = _cur_open() or []
        tc = [e for e in opens if getattr(e, "recommended_path", "") == "tool_creation"]

        # Sort priority: operator-sourced FIRST (the operator just
        # asked — they expect to see it), then by urgency desc, then
        # by created_at desc. Without the operator boost, high-urgency
        # reflection entries (urg=0.95) crowd out the operator entry
        # (urg=0.7) and the HUD silently misreports.
        def _sort_key(e):
            src = getattr(e, "trigger_source", "")
            operator_boost = 1 if src == "operator" else 0
            return (
                operator_boost,
                float(getattr(e, "urgency_score", 0.0) or 0.0),
                str(getattr(e, "created_at", "")),
            )

        tc.sort(key=_sort_key, reverse=True)
        for e in tc[:8]:
            summary["queue"].append(
                {
                    "id": str(getattr(e, "curiosity_id", ""))[:32],
                    "problem": str(getattr(e, "problem_statement", ""))[:120],
                    "trigger_source": getattr(e, "trigger_source", "?"),
                    "urgency": round(float(getattr(e, "urgency_score", 0.0) or 0.0), 2),
                    "created_at": getattr(e, "created_at", ""),
                }
            )
    except Exception as exc:
        log.debug("tool_synthesis_summary: queue read failed (%s)", exc)
    # --- Recent: last synthesised tools ---
    # P3-S28 HUD live-feed doctrine v3 — read from the in-process events
    # ring (P3-S25) filtered to P3-S12 tool synthesis dispatch
    # breadcrumbs. The legacy disk source guardian/logs/tool_decisions.jsonl
    # is no longer written by any production code path (the Guardian
    # tool_registrar that fed it was retired); the file doesn't exist
    # on disk. Tool synthesis decisions now surface through the
    # `tool_synthesis_dispatch` emit_phase rows in feedback_audit.jsonl
    # (which the ring mirrors) — see P3-S12 cure.
    try:

        def _is_tool_decision(rec: Dict[str, Any]) -> bool:
            return (
                rec.get("kind") == "phase"
                and rec.get("loop") == "omega"
                and rec.get("phase") == "tool_synthesis_dispatch"
                and "approved" in rec  # decision exit rows carry this
            )

        decisions = _audit_events(
            predicate=_is_tool_decision,
            limit=8,
            newest_first=True,
        )
        for rec in decisions:
            summary["recent"].append(
                {
                    "tool_name": str(rec.get("spec_name") or rec.get("tool_name") or "?")[:48],
                    "approved": bool(rec.get("approved")),
                    "scope": str(rec.get("scope") or "")[:16],
                    "source": str(rec.get("source") or "omega_synthesis")[:24],
                    "reason": str(rec.get("reason") or rec.get("summary") or "")[:120],
                    "ts": str(rec.get("ts") or ""),
                }
            )
    except Exception as exc:
        log.debug("tool_synthesis_summary: ring read failed (%s)", exc)

    # --- Dev: operator-facing dev tool invocation rollup ---
    # 2026-05-14: makes dev-agent + dev-pipeline tool calls
    # visible. Aggregate is over the last 500 ledger rows; recent_calls
    # is the last 12 individual rows newest-first for live activity feed.
    try:
        from core.dev_tool_registry import (
            aggregate_invocation_stats,
            read_recent_invocations,
        )

        agg = aggregate_invocation_stats(limit=500)
        # Keep `by_tool` ranked by invocation count (most-used first)
        # so the panel shows the busiest tools at the top.
        by_tool_ranked = dict(
            sorted(
                (agg.get("by_tool") or {}).items(),
                key=lambda kv: kv[1].get("invocations", 0),
                reverse=True,
            )
        )
        recent_rows = read_recent_invocations(limit=12)
        summary["dev"] = {
            "total_invocations": int(agg.get("total_invocations", 0)),
            "successful_invocations": int(agg.get("successful_invocations", 0)),
            "tools_observed": int(agg.get("tools_observed", 0)),
            "by_tool": by_tool_ranked,
            "recent_calls": [
                {
                    "ts": r.get("ts", ""),
                    "tool_name": r.get("tool_name", ""),
                    "call_id": r.get("call_id", ""),
                    "tool_ok": bool(r.get("tool_ok")),
                    "latency_ms": int(r.get("latency_ms") or 0),
                    "error": str(r.get("error") or "")[:120],
                }
                for r in reversed(recent_rows)  # newest first
            ],
        }
    except Exception as exc:
        log.debug("tool_synthesis_summary: dev ledger read failed (%s)", exc)

    return summary


def _pressure_tag(score: float) -> str:
    """Bucket scheduler pressure_score into the HUD's vocabulary."""
    if score >= 0.8:
        return "ACUTE"
    if score >= 0.6:
        return "HIGH"
    if score >= 0.3:
        return "MED"
    return "LOW"


# Map scheduler PressureSignal.name → friendly HUD label. The screenshot
# uses "contradiction load / unresolved curiosity / pending guardian
# queue / memory uncertainty / strategic conflict" — translate the
# real signal names into those.
_PRESSURE_LABEL = {
    "contradiction_spike": "contradiction load",
    "guardian_intervention": "pending guardian queue",
    "unresolved_curiosity": "unresolved curiosity",
    "memory_uncertainty": "memory uncertainty",
    "strategic_conflict": "strategic conflict",
    "autonomous_pressure": "autonomous pressure",
    "deepsleep_backlog": "deepsleep backlog",
}


def _dev_status_tag(status: str) -> str:
    """Translate DevStatus enum → HUD tag."""
    s = (status or "").lower()
    return {
        "proposed": "PENDING GUARDIAN",
        "simulated": "STAGED",
        "approved": "PERSISTED",
        "deployed": "PERSISTED",
        "rolled_back": "REJECTED",
    }.get(s, s.upper() or "—")


def _autoloop_audit_ring_overlay() -> Dict[str, Any]:
    """Live audit-ring slice for autoloop respiratory / suppress surfaces.

    Always read fresh — even when ``panel_autoloop`` reuses the snapshot
    cache for scheduler pressure/cadence, neuromod sparklines must reflect
    events emitted after the last ``telemetry_snapshot()`` capture.
    """
    autonomic: Dict[str, list] = {
        "dopamine": [],
        "plasticity": [],
        "strength": [],
        "stress": [],
    }
    suppress_histogram: Dict[str, int] = {
        "proceeded": 0,
        "pressure_loop_paused": 0,
        "presence_gate_held": 0,
        "autonomy_paused": 0,
        "omega_lock_held": 0,
        "below_threshold": 0,
        "other": 0,
        "total": 0,
    }
    last_cycle_at = "—"
    signal_to_bucket = {
        "auto.cycle.dopamine": "dopamine",
        "auto.cycle.plasticity": "plasticity",
        "auto.cycle.strength": "strength",
        "auto.cycle.stress": "stress",
        "symphony.neuromod.dopamine": "dopamine",
        "symphony.neuromod.plasticity": "plasticity",
        "symphony.neuromod.strength": "strength",
        "symphony.neuromod.stress": "stress",
    }
    try:
        from datetime import datetime, timezone, timedelta

        hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    except Exception:
        hour_ago = None

    try:
        ring = _audit_events(newest_first=False)
        for rec in ring:
            kind = rec.get("kind")
            if kind == "signal":
                nm = rec.get("name") or ""
                bucket = signal_to_bucket.get(nm)
                if bucket is not None:
                    try:
                        v = float(rec.get("value") or 0.0)
                    except (TypeError, ValueError):
                        v = 0.0
                    autonomic[bucket].append({"v": round(v, 4), "ts": rec.get("ts", "")})
            elif kind == "phase":
                loop = str(rec.get("loop") or "")
                phase = str(rec.get("phase") or "")
                if loop == "pressure" and phase == "tick":
                    keep = True
                    if hour_ago is not None and rec.get("ts"):
                        try:
                            rec_dt = datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))
                            if rec_dt < hour_ago:
                                keep = False
                        except Exception:
                            pass
                    if not keep:
                        continue
                    if str(rec.get("status") or "") == "ok":
                        suppress_histogram["proceeded"] += 1
                    else:
                        reason = (rec.get("suppress_reason") or rec.get("summary") or "").strip()
                        if reason in suppress_histogram:
                            suppress_histogram[reason] += 1
                        elif reason:
                            suppress_histogram["other"] += 1
                        else:
                            suppress_histogram["other"] += 1
                    suppress_histogram["total"] += 1
                    last_cycle_at = rec.get("ts") or last_cycle_at
            elif kind == "data":
                loop = str(rec.get("loop") or rec.get("system") or "")
                phase = str(rec.get("phase") or "")
                if loop == "autonomic" and phase == "neuromod_compute":
                    for field, bucket in (
                        ("dopamine", "dopamine"),
                        ("plasticity", "plasticity"),
                        ("strength", "strength"),
                        ("stress", "stress"),
                    ):
                        try:
                            v = float(rec.get(field) or 0.0)
                        except (TypeError, ValueError):
                            v = 0.0
                        autonomic[bucket].append({"v": round(v, 4), "ts": rec.get("ts", "")})
                if loop == "pressure" and phase == "episode_ignition":
                    keep = True
                    if hour_ago is not None and rec.get("ts"):
                        try:
                            rec_dt = datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))
                            if rec_dt < hour_ago:
                                keep = False
                        except Exception:
                            pass
                    if not keep:
                        continue
                    suppress_histogram["proceeded"] += 1
                    suppress_histogram["total"] += 1
                    last_cycle_at = rec.get("ts") or last_cycle_at

        for key in list(autonomic.keys()):
            autonomic[key] = autonomic[key][-30:]
    except Exception as exc:
        log.debug("autoloop audit-ring overlay failed: %s", exc)

    return {
        "autonomic": autonomic,
        "suppress_histogram": suppress_histogram,
        "last_cycle_at": last_cycle_at,
    }


def _telemetry_autoloop(*, cadence_report: Any = None) -> Dict[str, Any]:
    """`autoloop` subtree — every field driving the autoloop panel:

    pressure      — {score, scoreLabel, tag, signals[]}
    cadence       — {mode, baseInterval, currentInterval,
                     lastSweep, evaluated, escalated}
    curiosity     — open curiosity entries (id, subject, urgency)
    proposals     — open development proposals (id, subject, tag)
    resolved      — completed development (id, subject, outcome,
                                           guardian, tag)
    abandoned     — falsified/rejected/inconclusive paths
    """
    out: Dict[str, Any] = {
        "pressure": {"score": 0.0, "scoreLabel": "0.00", "tag": "LOW", "signals": []},
        "cadence": {
            "mode": "hybrid (timer + pressure)",
            "baseInterval": "—",
            "currentInterval": "—",
            "lastSweep": "—",
            "evaluated": 0,
            "escalated": 0,
        },
        "curiosity": [],
        "proposals": [],
        "resolved": [],
        "abandoned": [],
    }

    # ---- AL-1/2: pressure + cadence from CadenceTickReport --------
    # Cached scheduler — see _get_cached_scheduler. tick() re-reads
    # its providers each call, so signal values are always fresh.
    try:
        sched = _get_cached_scheduler()
        if sched is None:
            raise RuntimeError("cadence scheduler unavailable")
        cfg = sched.config
        report = cadence_report if cadence_report is not None else sched.tick()

        score = float(getattr(report, "pressure_score", 0.0) or 0.0)
        out["pressure"]["score"] = score
        out["pressure"]["scoreLabel"] = f"{score:.2f}"
        out["pressure"]["tag"] = _pressure_tag(score)
        signals = getattr(report, "trigger_signals", []) or []
        # Sort by weighted contribution so the eye lands on what
        # actually moved the score the most.
        sig_rows = []
        for s in signals:
            name = getattr(s, "name", "?")
            value = float(getattr(s, "value", 0) or 0)
            weight = float(getattr(s, "weight", 1.0) or 1.0)
            label = _PRESSURE_LABEL.get(name, name.replace("_", " "))
            # Normalise to a 0..1 contribution (scheduler weights vary)
            contrib = min(1.0, (value * weight) / 100.0)
            sig_rows.append(
                {
                    "name": label,
                    "value": value,
                    "weight": weight,
                    "delta": f"+{contrib:.2f}",
                    "_raw_contrib": contrib,
                }
            )
        sig_rows.sort(key=lambda r: r["_raw_contrib"], reverse=True)
        out["pressure"]["signals"] = sig_rows[:6]

        # Cadence row
        out["cadence"]["mode"] = "hybrid (timer + pressure)"
        out["cadence"]["baseInterval"] = (
            f"{int(cfg.min_interval_seconds)}-{int(cfg.max_interval_seconds)}s · pressure-modulated"
        )
        cur_int = float(getattr(report, "interval_used", 0) or 0)
        out["cadence"]["currentInterval"] = f"{int(cur_int)}s" if cur_int else "—"
        # Last sweep age from seconds_since_last_sweep. Sub-minute bucket
        # coarsened to a stable 'just now' (HUD live-feed stabilization,
        # 2026-05-28) so this field stops ticking per-second and churning
        # the autoloop panel hash every 2s poll.
        secs = float(getattr(report, "seconds_since_last_sweep", 0) or 0)
        if secs < 60:
            out["cadence"]["lastSweep"] = "just now"
        elif secs < 3600:
            out["cadence"]["lastSweep"] = f"{int(secs / 60)}m ago"
        elif secs < 86400:
            out["cadence"]["lastSweep"] = f"{int(secs / 3600)}h ago"
        else:
            out["cadence"]["lastSweep"] = "never"
        # Evaluated/escalated counters (signal count proxy)
        out["cadence"]["evaluated"] = len(signals)
        out["cadence"]["escalated"] = sum(1 for s in signals if getattr(s, "fire_now", False))
    except Exception as exc:
        log.debug("autoloop scheduler probe failed: %s", exc)

    # ---- AL-3: open curiosity entries -----------------------------
    try:
        from hud.curiosity_store import list_open as _list_open_cur

        cur_open = _list_open_cur() or []
        # Sort by urgency desc and take top 8
        cur_open = sorted(
            cur_open, key=lambda e: float(getattr(e, "urgency_score", 0) or 0), reverse=True
        )
        for e in cur_open[:8]:
            cid = str(getattr(e, "curiosity_id", ""))
            short = ("CUR_" + cid.replace("cur-", "")[:8]).upper()
            out["curiosity"].append(
                {
                    "id": short,
                    "subject": str(getattr(e, "problem_statement", ""))[:96],
                    "urgency": f"{float(getattr(e, 'urgency_score', 0) or 0):.2f}",
                    "_raw_id": cid,
                }
            )
    except Exception:
        pass

    # Development proposals panel: producer retired 2026-05-18 — leave
    # proposals/resolved/abandoned empty (tool-synth is the live path).

    # ---- AL-6: abandoned paths from curiosity (status=ignored) ----
    try:
        from hud.curiosity_store import list_all as _list_all_cur

        all_cur = (
            _list_all_cur()
            if hasattr(__import__("hud.curiosity_store", fromlist=["list_all"]), "list_all")
            else []
        )
    except Exception:
        all_cur = []
    for e in all_cur or []:
        status = str(getattr(e, "status", "") or "").lower()
        if status in ("ignored", "abandoned", "falsified", "inconclusive"):
            cid = str(getattr(e, "curiosity_id", ""))
            short = ("CUR_" + cid.replace("cur-", "")[:8]).upper()
            tag = {
                "ignored": "REJECTED",
                "abandoned": "REJECTED",
                "falsified": "FALSIFIED",
                "inconclusive": "INCONCLUSIVE",
            }.get(status, status.upper())
            out["abandoned"].append(
                {
                    "id": short,
                    "subject": str(getattr(e, "problem_statement", ""))[:96],
                    "tag": tag,
                    "detail": str(getattr(e, "ignore_reason", "") or "")[:160],
                }
            )
    out["abandoned"] = out["abandoned"][:6]

    # ---- P3-S09: Respiratory Field hero data --------------------
    # Four fields powering the §9.4 visual:
    #   autonomic         — last-N samples of the 4 vital signals
    #   suppress_histogram— last-hour bucketed pressure-tick suppress reasons
    #   pause_state       — pressure_loop_pause owners + paused boolean
    #   last_cycle_at     — ISO timestamp of most recent cycle phase
    out["pause_state"] = {"paused": False, "owners": []}

    # Pause state — sync read.
    try:
        from pressure_loop.pressure_loop_pause import pressure_loop_pause_state

        ps = pressure_loop_pause_state() or {}
        owners = ps.get("owners") or []
        out["pause_state"] = {
            "paused": bool(owners),
            "owners": [str(o) for o in owners][:8],
        }
    except Exception:
        pass

    out.update(_autoloop_audit_ring_overlay())

    out["dashboard"] = _autoloop_dashboard_metrics(out)
    return out


def _telemetry_meta() -> Dict[str, Any]:
    """`meta` subtree — last meta-loop cycle output. Reads
    `LAST_META_ORDERS` from shared_state. Defaults to a calm idle
    shape when no cycle has fired yet (first boot / pre-cadence)."""
    out: Dict[str, Any] = {
        "cycle_id": "—",
        "orders": [],
        "tool_catalog": {"canonical": 0, "lifecycle": 0},
        "phase_status": "idle",
        "summary": "no meta cycle yet",
    }
    rec = _shared_state_read("last_meta_orders")
    if isinstance(rec, dict):
        out["cycle_id"] = str(rec.get("cycle_id", "—"))
        out["phase_status"] = str(rec.get("status", "idle"))
        orders = rec.get("orders") or []
        out["orders"] = [
            {
                "id": str(o.get("id", "—"))[:32],
                "kind": str(o.get("kind", "—")),
                "summary": str(o.get("summary", "—"))[:120],
                "priority": o.get("priority", "—"),
            }
            for o in (orders if isinstance(orders, list) else [])[:8]
        ]
        cat = rec.get("tool_catalog") or {}
        if isinstance(cat, dict):
            out["tool_catalog"] = {
                "canonical": int(cat.get("canonical", 0) or 0),
                "lifecycle": int(cat.get("lifecycle", 0) or 0),
            }
        out["summary"] = (
            f"{len(out['orders'])} orders · catalog "
            f"{out['tool_catalog']['canonical']}+{out['tool_catalog']['lifecycle']}"
        )
    return out


def _telemetry_global() -> Dict[str, Any]:
    """`global` subtree — last global-loop cycle output. Reads
    `LAST_GLOBAL_OBSERVATIONS` + `LAST_GLOBAL_GOALS` from shared_state.

    Shape of LAST_GLOBAL_OBSERVATIONS (per core/loop_adapters.py:201):
        {"cycle_id": str, "observations": list[Observation]}
    Shape of LAST_GLOBAL_GOALS (per loop_adapters.py:205):
        {"cycle_id": str, "goals": list[Goal]}

    No `intake` sub-tree exists on the wire — this function used to
    fabricate one, which crashed with int(list) when `observations`
    was the actual list (not a count). Now derived correctly via len().
    """
    out: Dict[str, Any] = {
        "cycle_id": "—",
        "observations": 0,
        "goals_emitted": 0,
        "goals": [],
        "summary": "no global cycle yet",
    }
    obs = _shared_state_read("last_global_observations")
    if isinstance(obs, dict):
        out["cycle_id"] = str(obs.get("cycle_id", "—"))
        # observations is a list of Observation records — we only
        # surface the count for the panel; let panelGlobal cold-fetch
        # the full list if it ever needs to render them.
        olist = obs.get("observations") or []
        if isinstance(olist, list):
            out["observations"] = len(olist)
    goals = _shared_state_read("last_global_goals")
    if isinstance(goals, dict):
        glist = goals.get("goals") or []
        if isinstance(glist, list):
            out["goals_emitted"] = len(glist)
            out["goals"] = [
                {
                    "id": str(g.get("id", "—"))[:32],
                    "summary": str(g.get("summary") or g.get("text") or "—")[:120],
                    "priority": g.get("priority", "—"),
                }
                for g in glist[:8]
            ]
    out["summary"] = f"{out['observations']} observations · {out['goals_emitted']} goals"
    return out


def _telemetry_system() -> Dict[str, Any]:
    """`system` subtree — bridge / voice / vision states."""
    out: Dict[str, Any] = {
        "bridge": {"state": "open", "uri": "live", "last_event": "polling"},
    }
    # Voice + vision states (from the same probes used by status pills,
    # but reformatted for the Telemetry shape).
    voice = _probe_voice()
    try:
        from runtime.tts import status as _tts_status

        tts = _tts_status()
    except Exception as exc:
        tts = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        from runtime.stt import status as _stt_status

        stt = _stt_status()
    except Exception as exc:
        stt = {"error": f"{type(exc).__name__}: {exc}"}
    out["voice"] = {
        "state": voice["state"],
        "supported": True,
        "last_event": voice["detail"],
        "engine": "backend",
        "tts": {
            "engine": str(tts.get("engine") or "unavailable"),
            "available": bool(tts.get("available")),
            "voice": tts.get("voice"),
            "base": tts.get("base"),
            "device": tts.get("device"),
            "execution_provider": tts.get("execution_provider"),
        },
        "stt": {
            "engine": "parakeet",
            "available": bool(stt.get("available")),
            "base": stt.get("base"),
            "device": stt.get("device"),
            "backend": stt.get("backend"),
        },
    }
    # Face/vision is opt-in from Settings -> operator profile. Keep the
    # telemetry shape passive here so routine HUD polling does not import
    # or ping face runtime/model paths during boot.
    out["vision"] = {
        "state": "idle",
        "supported": None,
        "last_event": "face memory opt-in available in settings",
        "runtime": None,
        "anchor_count": None,
    }
    out["face_watch"] = {
        "state": "idle",
        "supported": None,
        "last_event": "not started",
        "known_names": [],
        "last_recognized": None,
        "last_confidence": 0,
    }

    # ---- v2 badge primitives (P3-S04) --------------------------------
    # Presence + pressure-loop are consumed by the HUD's
    # _applyV2PillBadges() from Telemetry.system.v2.
    v2_presence = "UNKNOWN"
    try:
        from sensory.operator_presence import read_presence_from_shared_state

        ss = _get_shared_state()
        if ss is not None:
            v2_presence = read_presence_from_shared_state(ss)
    except Exception:
        pass

    v2_paused = False
    v2_owners: list[str] = []
    try:
        from pressure_loop.pressure_loop_pause import pressure_loop_pause_state

        pls = pressure_loop_pause_state()
        v2_paused = bool(pls.get("paused"))
        v2_owners = list(pls.get("owners") or [])
    except Exception:
        pass

    tick_snapshot = last_pressure_tick_snapshot() or {}

    out["v2"] = {
        "presence": {
            "state": v2_presence,
        },
        "pressure_loop": {
            "paused": v2_paused,
            "owners": v2_owners,
        },
        "pressure_last_tick": tick_snapshot,
        # Back-compat: the browser still fills this from /api/cuedesk/last_tick.
        "cuedesk_last_tick": tick_snapshot,
    }
    return out


def _cadence_tick_report() -> Any:
    """Single cadence tick per HUD snapshot (autoloop + brain share it)."""
    try:
        sched = _get_cached_scheduler()
        if sched is None:
            return None
        return sched.tick()
    except Exception:
        return None


_SNAPSHOT_CACHE: dict[str, Any] | None = None
_SNAPSHOT_CACHE_TS = 0.0
_SNAPSHOT_CACHE_TTL_SECONDS = 1.5


def telemetry_snapshot() -> Dict[str, Any]:
    """Unified snapshot patch the HUD frontend deep-merges into its
    Telemetry state. Each subsystem is independently best-effort —
    a degraded subsystem returns an empty dict (preserving the mock
    field) rather than blocking other subsystems."""
    global _SNAPSHOT_CACHE, _SNAPSHOT_CACHE_TS, _ACTIVE_AUDIT_SNAPSHOT
    now = time.monotonic()
    if _SNAPSHOT_CACHE is not None and (now - _SNAPSHOT_CACHE_TS) < _SNAPSHOT_CACHE_TTL_SECONDS:
        return dict(_SNAPSHOT_CACHE)
    try:
        _ACTIVE_AUDIT_SNAPSHOT = AuditRingSnapshot.capture()
        cycle = _telemetry_cycle()
        cadence_report = _cadence_tick_report()
        payload = {
            "cycle": cycle,
            "memory": _telemetry_memory(),
            "omega": _telemetry_omega(),
            "meta": _telemetry_meta(),
            "global": _telemetry_global(),
            "autoloop": _telemetry_autoloop(cadence_report=cadence_report),
            "identity": _telemetry_identity(),
            "cognition": _telemetry_cognition(),
            "system": _telemetry_system(),
            "layers": _telemetry_layers(6),
            "brain": _telemetry_brain(cycle=cycle, cadence_report=cadence_report),
            "deepsleep": panel_deepsleep(),
        }
        _SNAPSHOT_CACHE = payload
        _SNAPSHOT_CACHE_TS = now
        return dict(payload)
    finally:
        _ACTIVE_AUDIT_SNAPSHOT = None


def _brain_wheel_meta(cycle: Dict[str, Any]) -> tuple:
    """Cycle wheel registry + active segment index from cycle.layer."""
    import re as _re

    cycle_layers: list = []
    cycle_active_index = -1
    try:
        ordered = sorted(_LAYER_POSITIONS.items(), key=lambda kv: kv[1][0])
        cycle_layers = [{"idx": pos[0], "key": key, "label": pos[1]} for key, pos in ordered]
        cycle_layer_str = (cycle or {}).get("layer") or ""
        m = _re.match(r"\s*(\d+)\s*/", str(cycle_layer_str))
        if m:
            cycle_active_index = int(m.group(1))
    except Exception:
        pass
    return cycle_layers, cycle_active_index


def _telemetry_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or val == "—" or val == "-":
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _phases_from_audit(*, limit: int = 40) -> list:
    """Recent phase events across cognition loops (in-process audit ring)."""
    try:
        allowed = {
            "pressure",
            "global",
            "meta",
            "auto",
            "omega",
            "deepsleep",
            "brain",
        }
        out: list = []
        for rec in reversed(_audit_events(500)):
            if rec.get("kind") != "phase":
                continue
            loop = str(rec.get("loop") or rec.get("system") or "").strip().lower()
            if loop not in allowed:
                continue
            out.append(
                {
                    "loop": loop,
                    "phase": str(rec.get("phase") or ""),
                    "status": str(rec.get("status") or "ok"),
                    "latency_ms": int(rec.get("latency_ms") or 0),
                    "ts": rec.get("ts") or "",
                    "episode_id": str(rec.get("episode_id") or rec.get("cycle_id") or ""),
                }
            )
            if len(out) >= limit:
                break
        out.reverse()
        return out
    except Exception:
        return []


_CASCADE_PHASES = {
    "pressure": {"pressure"},
    "global_a": {"ignition", "route", "intent"},
    "meta_a": {"intake", "action", "tool", "memory", "pack", "skill_selection"},
    "auto": {"intake", "validate", "recall", "promptpack", "route", "execute", "fold"},
    "meta_b": {"consistency", "disposition", "reflect"},
    "global_b": {"assemble", "episode_outcome", "ground", "summarize", "followups", "package"},
    "omega": {"intake", "reason", "reflect", "package", "reflect_retry"},
    "deepsleep": {
        "intake",
        "mastery",
        "failure",
        "semantic",
        "identity",
        "trajectory",
        "query_lifecycle",
        "consolidate",
        "projection",
        "consolidate_memory",
        "memory_integrity",
        "reseed",
        "closure",
    },
}


def _cascade_cycle_for_phase(loop: str, phase: str) -> str:
    """Map an audit phase to the visible cascade cycle without heuristics."""
    loop_key = str(loop or "").strip().lower().replace("-", "_")
    phase_key = str(phase or "").strip().lower().replace("-", "_")
    if loop_key in _CASCADE_PHASES and phase_key in _CASCADE_PHASES[loop_key]:
        return loop_key
    if loop_key in {"global_a", "global_b", "meta_a", "meta_b"}:
        return loop_key
    if loop_key == "global":
        return "global_a" if phase_key in _CASCADE_PHASES["global_a"] else "global_b"
    if loop_key == "meta":
        return "meta_a" if phase_key in _CASCADE_PHASES["meta_a"] else "meta_b"
    if loop_key in _CASCADE_PHASES:
        return loop_key
    return ""


def _latest_cascade_phase() -> Dict[str, Any]:
    phases = _phases_from_audit(limit=80)
    if not phases:
        return {
            "cycle": "",
            "loop": "",
            "phase": "",
            "status": "idle",
            "episode_id": "",
        }
    latest = phases[-1]
    loop = str(latest.get("loop") or "").strip().lower()
    phase = str(latest.get("phase") or "").strip().lower()
    return {
        "cycle": _cascade_cycle_for_phase(loop, phase),
        "loop": loop,
        "phase": phase,
        "status": str(latest.get("status") or "ok").lower(),
        "episode_id": str(latest.get("episode_id") or ""),
        "ts": str(latest.get("ts") or ""),
    }


def _latest_tool_provenance() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "tool": "",
        "action": "",
        "query": "",
        "domain": "",
        "learned_link": "",
        "source_name": "",
        "result_count": 0,
        "status": "idle",
    }
    try:
        for rec in reversed(_audit_events(800)):
            kind = str(rec.get("kind") or "")
            loop = str(rec.get("loop") or rec.get("system") or "").lower()
            phase = str(rec.get("phase") or "").lower()
            if kind == "data" and loop == "auto" and phase == "tool_invocation":
                tool = str(rec.get("tool_name") or rec.get("tool") or "").strip()
                if not tool:
                    continue
                out.update(
                    {
                        "tool": tool,
                        "action": str(rec.get("action") or "USE_TOOL"),
                        "query": str(rec.get("query") or "")[:160],
                        "domain": str(rec.get("domain") or "")[:80],
                        "learned_link": str(rec.get("learned_link") or "")[:500],
                        "source_name": str(rec.get("source_name") or "")[:120],
                        "result_count": int(rec.get("result_count") or 0),
                        "status": "success" if rec.get("ok", True) else "failed",
                    }
                )
                return out
            if kind == "phase" and loop == "tools" and phase == "web_fetch":
                out.update(
                    {
                        "tool": "web_fetch",
                        "query": str(rec.get("query_preview") or "")[:160],
                        "status": str(rec.get("status") or "ok").lower(),
                    }
                )
                return out
    except Exception:
        pass
    return out


def _active_episode_snapshot() -> Dict[str, Any]:
    """Return the one live episode without inventing activity on cold boot.

    ``PressureRuntime.episode_store`` is the lifecycle authority. Audit rows remain
    valuable phase evidence, but their newest row can describe a completed episode
    and therefore must not be used to claim that cognition is still active.
    """
    episode = None
    try:
        from pressure_loop.runtime import try_get_default_runtime

        runtime = try_get_default_runtime()
        store = getattr(runtime, "episode_store", None) if runtime is not None else None
        episode = store.current() if store is not None else None
    except (ImportError, RuntimeError, OSError, ValueError, AttributeError) as exc:
        log.debug("active episode snapshot unavailable: %s", exc)
        episode = None
    if episode is None:
        return {"id": "", "state": "resting", "active": False}
    return {
        "id": str(getattr(episode, "episode_id", "") or ""),
        "state": str(getattr(episode, "state", "active") or "active"),
        "active": True,
    }


def _idle_learning_snapshot(active_episode: bool) -> Dict[str, Any]:
    """Project the only genuine autonomy timer: idle-learning eligibility."""
    try:
        from pressure_loop import idle_wake

        enabled = bool(idle_wake.governor_enabled())
        if not enabled:
            return {
                "enabled": False,
                "state": "disabled",
                "seconds_remaining": None,
                "label": "idle learning disabled",
            }
        if active_episode:
            return {
                "enabled": True,
                "state": "held",
                "seconds_remaining": None,
                "label": "held while episode active",
            }
        seconds = max(0, int(round(idle_wake.seconds_until_wake_attempt())))
        state = "eligible" if seconds == 0 else "resting"
        label = (
            "eligible now"
            if seconds == 0
            else f"eligible in {seconds // 60:02d}:{seconds % 60:02d}"
        )
        return {
            "enabled": True,
            "state": state,
            "seconds_remaining": seconds,
            "label": label,
        }
    except Exception:
        return {
            "enabled": None,
            "state": "unknown",
            "seconds_remaining": None,
            "label": "idle learning unavailable",
        }


def _telemetry_cognition() -> Dict[str, Any]:
    tick = last_pressure_tick_snapshot() or {}
    dominant = tick.get("dominant_contributor")
    dominant = dominant if isinstance(dominant, dict) else {}
    cascade = _latest_cascade_phase()
    episode = _active_episode_snapshot()
    idle_learning = _idle_learning_snapshot(bool(episode.get("active")))
    if episode.get("active") and episode.get("id"):
        cascade = dict(cascade)
        cascade["episode_id"] = str(episode["id"])
    if episode.get("active"):
        autonomy_state = "cascade_active"
        autonomy_reason = "pressure ignition opened this episode"
    elif idle_learning.get("state") == "eligible":
        autonomy_state = "idle_learning_eligible"
        autonomy_reason = "idle learning may create new pressure"
    else:
        autonomy_state = "resting"
        autonomy_reason = "awaiting pressure ignition"
    return {
        "episode": episode,
        "cascade": cascade,
        "ignition": {
            "winner": str(tick.get("opened_by") or tick.get("dominant") or ""),
            "aggregate": tick.get("pressure_aggregate"),
            "source": str(dominant.get("source") or ""),
            "contributors": _contributors_from_audit(),
            "opened": bool(tick.get("opened_episode")),
        },
        "tool": _latest_tool_provenance(),
        "autonomy": {
            "state": autonomy_state,
            "time_driven": False,
            "reason": autonomy_reason,
            "continuation": "Omega decides successor pressure at foldback",
        },
        "idle_learning": idle_learning,
    }


def _contributor_rows_from_ignition_list(contributors: list) -> list:
    """Map episode_ignition contributor records to HUD row shape."""
    rows = []
    for c in contributors:
        if not isinstance(c, dict):
            continue
        typ = str(c.get("type") or "?")
        val = _telemetry_float(c.get("weight"))
        if val <= 0.001:
            continue
        label = _PRESSURE_LABEL.get(typ, typ.replace("_", " "))
        contrib = min(1.0, val if val <= 1.0 else val / 100.0)
        score = round(contrib, 4)
        rows.append(
            {
                "name": label,
                "type": typ,
                "source": str(c.get("source") or ""),
                "weight": score,
                "value": round(val, 4),
                "contrib": score,
                "score": score,
            }
        )
    rows.sort(key=lambda r: r["contrib"], reverse=True)
    return rows[:6]


def _contributor_rows_from_weights(weights: Dict[str, Any]) -> list:
    rows = []
    for name, raw in sorted(
        weights.items(),
        key=lambda kv: _telemetry_float(kv[1]),
        reverse=True,
    ):
        val = _telemetry_float(raw)
        label = _PRESSURE_LABEL.get(name, name.replace("_", " "))
        contrib = min(1.0, val if val <= 1.0 else val / 100.0)
        score = round(contrib, 4)
        rows.append(
            {
                "name": label,
                "weight": score,
                "value": round(val, 4),
                "contrib": score,
                "score": score,
            }
        )
    return rows[:6]


def _contributors_from_audit() -> list:
    """Pressure contributor weights — prefer last ignition roster over gather."""
    snap = _pressure_live_snapshot()
    ign = snap.get("ignition_contributors")
    if isinstance(ign, list) and ign:
        rows = _contributor_rows_from_ignition_list(ign)
        if rows:
            return rows
    weights = snap.get("weights")
    if isinstance(weights, dict) and weights:
        return _contributor_rows_from_weights(weights)
    try:
        for rec in reversed(_audit_events(800)):
            loop = str(rec.get("loop") or rec.get("system") or "")
            kind = str(rec.get("kind") or "")
            phase = str(rec.get("phase") or "")
            if kind == "data" and loop == "pressure" and phase == "episode_ignition":
                contrib_list = rec.get("contributors") or []
                if isinstance(contrib_list, list) and contrib_list:
                    rows = _contributor_rows_from_ignition_list(contrib_list)
                    if rows:
                        return rows
            if kind != "phase" or loop != "pressure":
                continue
            if phase != "gather_contributors":
                continue
            w = rec.get("weights")
            if isinstance(w, dict) and w:
                return _contributor_rows_from_weights(w)
    except Exception:
        pass
    return []


def _activity_series_from_phases(phases: list, *, buckets: int = 32) -> list:
    """Normalize recent phase density into a 0..1 sparkline series."""
    import time as _time
    from datetime import datetime as _dt

    series = [0.0] * buckets
    if not phases:
        return series
    now = _time.time()
    window_s = 300.0
    for p in phases:
        ts = p.get("ts") or ""
        if not ts:
            continue
        try:
            age = now - _dt.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if age < 0 or age > window_s:
            continue
        idx = int((1.0 - age / window_s) * (buckets - 1))
        idx = max(0, min(buckets - 1, idx))
        series[idx] += 1.0 + (_telemetry_float(p.get("latency_ms")) / 400.0)
    peak = max(series) or 1.0
    return [round(v / peak, 4) for v in series]


def _phase_heatmap_from_audit() -> list:
    """7×8 activity grid (day-of-week × 3-hour bucket), tier 0..3."""
    from datetime import datetime, timezone, timedelta

    grid = [[0] * 8 for _ in range(7)]
    try:
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        for rec in _audit_events(800):
            if rec.get("kind") != "phase":
                continue
            ts = rec.get("ts") or ""
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            if dt < week_ago:
                continue
            row = dt.weekday()
            col = min(7, dt.hour // 3)
            grid[row][col] += 1
    except Exception:
        pass
    tiers: list = []
    for row in grid:
        for count in row:
            if count == 0:
                tiers.append(0)
            elif count <= 2:
                tiers.append(1)
            elif count <= 6:
                tiers.append(2)
            else:
                tiers.append(3)
    return tiers


def _latest_phase_label(phases: list) -> str:
    if not phases:
        return "—"
    p = phases[-1]
    loop = p.get("loop") or "?"
    phase = p.get("phase") or "?"
    return f"{loop}.{phase}"


def _bar_diagnostics(
    label: str,
    pct: float,
    detail: str,
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> list:
    """HUD bar-row drill-down rows (panel-row shaped)."""
    p = round(_telemetry_float(pct), 3)
    tag = "warn" if p >= 0.66 else "ok" if p >= 0.33 else "idle"
    rows = [
        {"k": "channel", "v": str(label or "—"), "t": ""},
        {"k": "load", "v": f"{int(p * 100)}%", "t": tag},
        {"k": "detail", "v": str(detail or "—"), "t": ""},
    ]
    for key, val in (extra or {}).items():
        rows.append({"k": str(key), "v": str(val), "t": ""})
    return rows


def _brain_throughput(phases: list, tick_snap: Dict[str, Any]) -> list:
    """Throughput bars derived from live phase cadence."""
    ok_phases = [p for p in phases if p.get("status") in ("ok", "enter")]
    latencies = [_telemetry_float(p.get("latency_ms")) for p in ok_phases if p.get("latency_ms")]
    avg_lat = (sum(latencies) / len(latencies)) if latencies else 0.0
    tick = int(tick_snap.get("tick") or 0)
    phase_rate = min(1.0, len(ok_phases) / 20.0)
    lat_norm = min(1.0, avg_lat / 2000.0) if avg_lat else 0.0
    bars = [
        {
            "label": "phase cadence",
            "detail": f"{len(ok_phases)} recent",
            "pct": round(phase_rate, 3),
        },
        {
            "label": "avg latency",
            "detail": f"{int(avg_lat)} ms" if avg_lat else "—",
            "pct": round(lat_norm, 3),
        },
        {
            "label": "pressure tick",
            "detail": str(tick) if tick else "—",
            "pct": min(1.0, tick / 100.0) if tick else 0.0,
        },
    ]
    for bar in bars:
        bar["diagnostics"] = _bar_diagnostics(
            bar["label"],
            bar["pct"],
            bar["detail"],
        )
    return bars


def _oppositions_for_dashboard(
    contras: list,
    contributors: list,
) -> list:
    """Contradiction pairs — real recall oppositions first, else pressure pairs."""
    out: list = []
    for c in contras[:4]:
        if not isinstance(c, dict):
            continue
        left = str(c.get("left") or c.get("a") or "").strip()
        right = str(c.get("right") or c.get("b") or "").strip()
        if not left or not right:
            continue
        out.append(
            {
                "left": left,
                "right": right,
                "weight": round(_telemetry_float(c.get("weight") or c.get("score")), 3),
            }
        )
    if out:
        return out
    for row in contributors[:3]:
        name = str(row.get("name") or "?")
        w = _telemetry_float(row.get("contrib"))
        out.append(
            {
                "left": name,
                "right": "equilibrium",
                "weight": round(w, 3),
            }
        )
    return out


def _pressure_channels_from_contributors(contributors: list) -> list:
    channels = []
    for row in contributors[:4]:
        pct = _telemetry_float(row.get("contrib"))
        val = row.get("value")
        score = row.get("score")
        if score is None:
            score = pct
        label = str(row.get("name") or "?")
        pct_r = round(pct, 3)
        detail = f"score {float(score):.3f}"
        if isinstance(val, (int, float)):
            detail = f"score {float(score):.3f} · weight {float(val):.3f}"
        channels.append(
            {
                "label": label,
                "pct": pct_r,
                "score": round(float(score), 4),
                "detail": detail,
                "diagnostics": _bar_diagnostics(
                    label,
                    pct_r,
                    detail,
                    extra={
                        "type": row.get("type", "—"),
                        "source": row.get("source", "—"),
                        "weight": row.get("weight", "—"),
                        "contrib": row.get("contrib", "—"),
                    },
                ),
            }
        )
    while len(channels) < 4:
        channels.append(
            {
                "label": "—",
                "pct": 0.0,
                "detail": "awaiting tick",
                "diagnostics": _bar_diagnostics("—", 0.0, "awaiting tick"),
            }
        )
    return channels


def _brain_dashboard_metrics(
    cycle: Dict[str, Any],
    *,
    live_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Production dashboard metrics for the brain telemetry panel."""
    tick = _pressure_live_snapshot()
    live = live_fields or {}
    contributors = list(live.get("contributors") or [])
    if not contributors:
        contributors = _contributors_from_audit()

    pressure_val = _telemetry_float(tick.get("pressure_aggregate"))
    if not pressure_val:
        pressure_val = _telemetry_float(cycle.get("pressure"))
    if pressure_val > 1.0:
        pressure_val = min(1.0, pressure_val / 100.0)

    phases = _phases_from_audit(limit=36)
    coherence = 0.0
    try:
        from core.shared_state_keys import RECENT_SELF_VECTORS

        sv_ring = _shared_state_read(RECENT_SELF_VECTORS) or []
        if isinstance(sv_ring, list) and sv_ring:
            coherence = _telemetry_float((sv_ring[-1] or {}).get("confidence"))
    except Exception:
        pass

    contras: list = []
    try:
        from core.shared_state_keys import RECENT_LAYER_STATES

        ring = _shared_state_read(RECENT_LAYER_STATES) or []
        for r in reversed(ring if isinstance(ring, list) else []):
            if r.get("layer_id") == "brain.retrieve":
                contras = list((r.get("schema") or {}).get("contradictions") or [])
                break
    except Exception:
        pass

    cognitive_load = min(1.0, max(pressure_val, (coherence * 0.35) + (pressure_val * 0.65)))
    load_status = "elevated" if cognitive_load >= 0.66 else "nominal"
    if cycle.get("live") and load_status == "nominal":
        load_status = "protected"

    rings = [
        {"label": "load", "value": round(cognitive_load, 3)},
        {"label": "pressure", "value": round(pressure_val, 3)},
        {"label": "coherence", "value": round(coherence, 3)},
    ]
    for i, row in enumerate(contributors[:3]):
        if i < len(rings):
            rings[i] = {
                "label": str(row.get("name") or rings[i]["label"])[:14],
                "value": _telemetry_float(row.get("contrib")),
            }

    return {
        "cognitive_load": round(cognitive_load, 3),
        "load_status": load_status,
        "coherence": round(coherence, 3),
        "coherence_trend": "stable",
        "contradictions": len(contras),
        "contradiction_trend": "low" if len(contras) < 4 else "elevated",
        "pressure": round(pressure_val, 3),
        "pressure_trend": "calm" if pressure_val < 0.4 else "elevated",
        "live": bool(cycle.get("live")),
        "tick": int(tick.get("tick") or 0),
        "episode_id": str(tick.get("opened_by") or tick.get("dominant") or cycle.get("id") or "—"),
        "active_phase": _latest_phase_label(phases),
        "neural_series": _activity_series_from_phases(phases),
        "activity_rings": rings,
        "throughput": _brain_throughput(phases, tick),
        "heatmap": _phase_heatmap_from_audit(),
        "oppositions": _oppositions_for_dashboard(contras, contributors),
        "pressure_channels": _pressure_channels_from_contributors(contributors),
        "recent_phases": [
            {
                "label": f"{p.get('loop')}.{p.get('phase')}",
                "status": p.get("status") or "ok",
                "latency_ms": int(p.get("latency_ms") or 0),
            }
            for p in phases[-10:]
        ],
    }


def _memory_dashboard_metrics(mem: Dict[str, Any]) -> Dict[str, Any]:
    recalls = mem.get("topRecall") or []
    scores = [_telemetry_float(r.get("score")) for r in recalls if r.get("score")]
    utilization = (sum(scores) / len(scores)) if scores else 0.0
    writes = mem.get("recent_writes") or []
    write_series = [1.0] * min(16, max(1, len(writes)))
    if len(writes) > 1:
        write_series = [
            round(0.2 + (i / max(1, len(writes) - 1)) * 0.8, 3) for i in range(min(16, len(writes)))
        ]
    anchors = mem.get("persistence_anchors") or []
    anchor_bars = [
        {
            "label": str(a.get("title") or a.get("id") or "?")[:28],
            "pct": min(1.0, _telemetry_float(a.get("strength"))),
            "detail": f"{int(a.get('touch') or 0)} touches",
        }
        for a in anchors[:4]
    ]
    l0 = mem.get("l0_state") or {}
    lobe_writes = int(mem.get("lobe_activity_total") or 0)
    return {
        "live": bool(writes) or lobe_writes > 0 or len(recalls) > 0,
        "utilization": round(utilization, 3),
        "recall_count": len(recalls),
        "write_total": len(writes),
        "lobe_writes": lobe_writes,
        "recall_mode": str(mem.get("recall_mode") or "semantic"),
        "embed_up": str(l0.get("embed") or "").lower() in ("up", "ok", "ready", "available"),
        "classify_up": str(l0.get("classify") or "").lower() in ("up", "ok", "ready", "available"),
        "write_series": write_series,
        "anchor_bars": anchor_bars,
        "lobe_activity": mem.get("lobe_activity") or {},
    }


def _omega_epochs_unified() -> list:
    """Recent omega window epochs — SharedState ring with audit-ring fallback."""
    epochs: list = []
    try:
        from core.shared_state_keys import RECENT_OMEGA_EPOCHS

        ring = _shared_state_read(RECENT_OMEGA_EPOCHS)
        if isinstance(ring, list) and ring:
            epochs = [e for e in ring[-20:] if isinstance(e, dict)]
    except Exception as exc:
        log.debug("_omega_epochs_unified: ring read failed (%s)", exc)

    audit_epochs: list = []
    try:
        from guardian.terminal_feedback import _omega_epoch_from_event

        for rec in reversed(_audit_events(500)):
            ep = _omega_epoch_from_event(rec)
            if ep is not None:
                audit_epochs.append(ep)
            if len(audit_epochs) >= 20:
                break
        audit_epochs.reverse()
    except Exception as exc:
        log.debug("_omega_epochs_unified: audit fallback failed (%s)", exc)

    if not epochs:
        return audit_epochs[-20:]
    if not audit_epochs:
        return epochs

    seen = {str(e.get("window_id") or "") for e in epochs}
    for ep in audit_epochs:
        wid = str(ep.get("window_id") or "")
        if wid and wid not in seen:
            epochs.append(ep)
            seen.add(wid)
    epochs.sort(key=lambda e: str(e.get("timestamp") or e.get("window_id") or ""))
    return epochs[-20:]


def _omega_live_signal(epochs: list, traj: Dict[str, Any]) -> bool:
    if traj.get("active"):
        return True
    if epochs:
        return True
    try:
        for rec in reversed(_audit_events(200)):
            loop = str(rec.get("loop") or rec.get("system") or "").strip().lower()
            if loop == "omega":
                return True
    except Exception:
        pass
    return False


def _omega_dashboard_metrics(omega: Dict[str, Any]) -> Dict[str, Any]:
    epochs = omega.get("recent_epochs") or []
    contradiction = omega.get("contradiction") or {}
    coh = _telemetry_float(omega.get("coherence"))
    if not coh and epochs:
        coh = _telemetry_float(epochs[-1].get("coherence"))
    epoch_series = [round(_telemetry_float(e.get("coherence")), 3) for e in epochs[-16:]]
    applied_series = [int(e.get("applied") or 0) for e in epochs[-16:]]
    rejected_series = [int(e.get("rejected") or 0) for e in epochs[-16:]]
    dir_counts: Dict[str, int] = {}
    for ep in epochs:
        direction = str(ep.get("direction") or "stable")
        dir_counts[direction] = dir_counts.get(direction, 0) + 1

    traj = omega.get("trajectory") or {}
    traits = omega.get("traits") or []
    trait_bars = []
    for trait in traits[:6]:
        status = str(trait.get("value") or "proposed")
        pct = 1.0 if status == "committed" else (0.65 if status == "finalized_uncommitted" else 0.4)
        trait_bars.append(
            {
                "label": str(trait.get("name") or "?")[:24],
                "pct": round(pct, 3),
                "detail": str(trait.get("delta") or status)[:48],
            }
        )

    tool_syn = omega.get("tool_synthesis") or {}
    tool_queue = tool_syn.get("queue") or []
    tool_reliability = (tool_syn.get("registry") or {}).get("top_reliability") or []
    tool_queue_bars = [
        {
            "label": str(q.get("id") or "pending")[:20],
            "pct": round(min(1.0, _telemetry_float(q.get("urgency"), 0.5)), 3),
            "detail": str(q.get("subject") or q.get("problem_statement") or "")[:40],
        }
        for q in tool_queue[:4]
        if isinstance(q, dict)
    ]
    tool_reliability_bars = [
        {
            "label": str(t.get("tool_name") or t.get("tool_id") or "?")[:20],
            "pct": round(_telemetry_float(t.get("reliability")), 3),
            "detail": f"{int(t.get('invocation_count') or 0)} calls",
        }
        for t in tool_reliability[:4]
        if isinstance(t, dict)
    ]

    gaps = omega.get("gaps") or []
    gap_bars = [
        {
            "label": str(g.get("id") or "gap")[:16],
            "pct": 0.55,
            "detail": str(g.get("subject") or "")[:40],
        }
        for g in gaps[:4]
        if isinstance(g, dict)
    ]

    journal = omega.get("journal") or {}
    runs = max(0, int(journal.get("runs") or 0))
    finalized = max(0, int(journal.get("finalized") or 0))
    trait_emerged = max(0, int(journal.get("trait_emergence_count") or 0))
    journal_bars = [
        {
            "label": "runs",
            "pct": round(min(1.0, runs / 20.0), 3),
            "detail": str(runs),
        },
        {
            "label": "finalized",
            "pct": round(min(1.0, finalized / max(1, runs)), 3),
            "detail": str(finalized),
        },
        {
            "label": "traits emerged",
            "pct": round(min(1.0, trait_emerged / 10.0), 3),
            "detail": str(trait_emerged),
        },
    ]

    reflection = omega.get("reflection") or {}
    topic_fold = omega.get("topic_fold") or {}
    oppositions = list(contradiction.get("oppositions") or [])[:4]
    cont_score = _telemetry_float(contradiction.get("score"))
    if not oppositions and epochs:
        cont_score = max(
            cont_score,
            min(1.0, (int(epochs[-1].get("rejected") or 0)) * 0.12),
        )

    trajectory_bars = [
        {
            "label": "trajectory arc",
            "pct": round(_telemetry_float(traj.get("progress")), 3),
            "detail": (
                f"{int(traj.get('cycles_remaining') or 0)} cycles left"
                if traj.get("active")
                else "dormant"
            ),
        },
    ]
    if traj.get("target_domain"):
        trajectory_bars.append(
            {
                "label": str(traj.get("target_domain") or "domain")[:20],
                "pct": round(_telemetry_float(traj.get("progress")), 3),
                "detail": str(traj.get("target_capability") or "")[:40],
            }
        )

    return {
        "live": _omega_live_signal(epochs, traj),
        "coherence": round(coh, 3),
        "coherence_tag": str(omega.get("coherenceTag") or "stable"),
        "epoch": int(omega.get("epoch") or 0),
        "posture": str(omega.get("posture") or "consolidating")[:32],
        "objective": str(omega.get("objective") or "—")[:120],
        "contradiction_score": round(cont_score, 3),
        "contradiction_tag": str(contradiction.get("tag") or "LOW"),
        "contradiction_count": len(oppositions),
        "oppositions": oppositions,
        "epoch_series": epoch_series,
        "applied_series": applied_series,
        "rejected_series": rejected_series,
        "direction_counts": dir_counts,
        "epochs": epochs[-20:],
        "trajectory_active": bool(traj.get("active")),
        "trajectory_progress": round(_telemetry_float(traj.get("progress")), 3),
        "trajectory_intent": str(traj.get("intent") or "")[:80],
        "trajectory_target_domain": str(traj.get("target_domain") or "")[:80],
        "trajectory_cycles_remaining": int(traj.get("cycles_remaining") or 0),
        "trait_count": len(traits),
        "trait_bars": trait_bars,
        "tool_queue_bars": tool_queue_bars,
        "tool_reliability_bars": tool_reliability_bars,
        "gap_bars": gap_bars,
        "journal_bars": journal_bars,
        "reflection_summary": str(reflection.get("summary") or "")[:160],
        "reflection_domain": str(reflection.get("domain") or "")[:64],
        "topic_fold_active": bool(topic_fold.get("active")),
        "topic_fold_domain": str(topic_fold.get("target_domain") or "")[:80],
        "trajectory_bars": trajectory_bars,
    }


def _curiosity_bars_for_dashboard(curiosity: list) -> list:
    bars = []
    for c in curiosity[:4]:
        label = str(c.get("id") or "?")
        pct_r = min(1.0, _telemetry_float(c.get("urgency")))
        detail = str(c.get("subject") or "")[:48]
        bars.append(
            {
                "label": label,
                "pct": pct_r,
                "detail": detail,
                "diagnostics": _bar_diagnostics(
                    label,
                    pct_r,
                    detail,
                    extra={"urgency": c.get("urgency", "—")},
                ),
            }
        )
    return bars


def _autoloop_dashboard_metrics(auto: Dict[str, Any]) -> Dict[str, Any]:
    pressure = auto.get("pressure") or {}
    score = _telemetry_float(pressure.get("score"))
    autonomic = auto.get("autonomic") or {}
    rings = []
    for key, label in (
        ("dopamine", "dopamine"),
        ("plasticity", "plasticity"),
        ("strength", "strength"),
        ("stress", "stress"),
    ):
        series = autonomic.get(key) or []
        val = _telemetry_float(series[-1].get("v")) if series else 0.0
        rings.append({"label": label, "value": round(val, 3)})
    hist = auto.get("suppress_histogram") or {}
    total = int(hist.get("total") or 0) or 1
    suppress_bars = []
    for key, label in (
        ("proceeded", "proceeded"),
        ("below_threshold", "below threshold"),
        ("pressure_loop_paused", "loop paused"),
        ("presence_gate_held", "presence gate"),
    ):
        count = int(hist.get(key) or 0)
        pct_r = round(count / total, 3)
        detail = f"{count} ticks"
        suppress_bars.append(
            {
                "label": label,
                "pct": pct_r,
                "detail": detail,
                "diagnostics": _bar_diagnostics(
                    label,
                    pct_r,
                    detail,
                    extra={"histogram_total": total},
                ),
            }
        )
    curiosity = auto.get("curiosity") or []
    hist_total = int(hist.get("total") or 0)
    autonomic_live = any(
        (auto.get("autonomic") or {}).get(key)
        for key in ("dopamine", "plasticity", "strength", "stress")
    )
    return {
        "live": (score > 0 or hist_total > 0 or autonomic_live or len(curiosity) > 0),
        "pressure": round(score, 3),
        "pressure_tag": str(pressure.get("tag") or "LOW"),
        "paused": bool((auto.get("pause_state") or {}).get("paused")),
        "rings": rings,
        "suppress_bars": suppress_bars,
        "curiosity_count": len(curiosity),
        "curiosity_bars": _curiosity_bars_for_dashboard(curiosity),
        "signals": (pressure.get("signals") or [])[:4],
    }


def _identity_dashboard_metrics(ident: Dict[str, Any]) -> Dict[str, Any]:
    sv = ident.get("selfVector") or {}
    conf = _telemetry_float(sv.get("confidence"))
    drift = str(sv.get("drift") or "stable")
    operators = ident.get("operators") or []
    traits = ident.get("traits") or []
    cw = ident.get("closedWorld") or {}
    vector_count = int(sv.get("count") or 0)
    return {
        "live": vector_count > 0 or len(operators) > 0 or conf > 0,
        "confidence": round(conf, 3),
        "drift": drift,
        "drift_velocity": _telemetry_float(sv.get("drift_velocity")),
        "operator_count": len(operators),
        "trait_count": len(traits),
        "sealed": bool(cw.get("sealed")),
        "ledger_ok": bool(cw.get("ledger_ok")),
        "vector_count": vector_count,
    }


def _deepsleep_phase_bars(phases: list) -> list:
    bars = []
    for p in (phases or [])[:4]:
        label = str(p.get("name") or p.get("phase") or "?")
        status = p.get("status") or "—"
        pct_r = 1.0 if status == "completed" else 0.55 if status == "active" else 0.12
        detail = str(p.get("summary") or status or "—")[:40]
        bars.append(
            {
                "label": label,
                "pct": pct_r,
                "detail": detail,
                "diagnostics": _bar_diagnostics(
                    label,
                    pct_r,
                    detail,
                    extra={"status": status},
                ),
            }
        )
    return bars


def _deepsleep_dashboard_metrics(ds: Dict[str, Any]) -> Dict[str, Any]:
    phases = ds.get("phase_states") or []
    intake = ds.get("intake") or {}
    replay = ds.get("replay") or {}
    contradictions = ds.get("contradictions") or {}
    consolidation = ds.get("consolidation") or {}
    completed = sum(1 for p in phases if p.get("status") == "completed")
    progress = completed / max(1, len(phases))
    cycle_id = str((ds.get("cycle") or {}).get("id") or "—")
    return {
        "live": cycle_id not in ("—", "", None),
        "cycle_id": cycle_id,
        "progress": round(progress, 3),
        "phase_bars": _deepsleep_phase_bars(phases),
        "intake_fragments": int(intake.get("fragments") or 0),
        "replay_count": int(replay.get("count") or 0),
        "contradictions_resolved": int(contradictions.get("resolved") or 0),
        "consolidation_promoted": int(consolidation.get("promoted") or 0),
    }


def _brain_live_fields(*, cadence_report: Any = None) -> Dict[str, Any]:
    """Lightweight brain panel fields for snapshot-driven HUD refresh."""
    out: Dict[str, Any] = {
        "suppress_reason": "",
        "opened_by": "",
        "conversation": {
            "pending_operator_question": "",
            "last_response_excerpt": "",
            "cycle_counter": 0,
            "last_user_turn_at": "",
        },
        "contributors": [],
    }
    try:
        tick_snap = last_pressure_tick_snapshot()
        if isinstance(tick_snap, dict):
            out["suppress_reason"] = str(tick_snap.get("suppress_reason") or "")
            out["opened_by"] = str(tick_snap.get("opened_by") or "")
    except Exception:
        pass
    try:
        from pressure_loop.system_messages import current_outreach

        outreach = current_outreach()
        if outreach:
            out["conversation"]["pending_operator_question"] = str(
                outreach.get("question_text") or ""
            )[:200]
    except Exception:
        pass
    try:
        last_resp = _shared_state_read("last_brain_response")
        if isinstance(last_resp, dict):
            excerpt = last_resp.get("text") or last_resp.get("response") or ""
            out["conversation"]["last_response_excerpt"] = str(excerpt)[:160]
        conv = _shared_state_read("conversation_state")
        if isinstance(conv, dict):
            out["conversation"]["cycle_counter"] = int(conv.get("turn_count") or 0)
            out["conversation"]["last_user_turn_at"] = str(conv.get("last_user_turn_at") or "")
    except Exception:
        pass
    audit_rows = _contributors_from_audit()
    if audit_rows:
        out["contributors"] = audit_rows
    else:
        try:
            report = cadence_report
            if report is None:
                sched = _get_cached_scheduler()
                if sched is not None:
                    report = sched.tick()
            if report is not None:
                signals = getattr(report, "trigger_signals", []) or []
                rows = []
                for s in signals:
                    name = getattr(s, "name", "?")
                    value = float(getattr(s, "value", 0) or 0)
                    weight = float(getattr(s, "weight", 1.0) or 1.0)
                    label = _PRESSURE_LABEL.get(name, name.replace("_", " "))
                    contrib = min(1.0, (value * weight) / 100.0)
                    score = round(contrib, 4)
                    rows.append(
                        {
                            "name": label,
                            "weight": round(weight, 4),
                            "value": round(value, 4),
                            "contrib": score,
                            "score": score,
                        }
                    )
                rows.sort(key=lambda r: r["contrib"], reverse=True)
                out["contributors"] = rows[:6]
        except Exception:
            pass
    return out


def _telemetry_brain(
    *,
    cycle: Optional[Dict[str, Any]] = None,
    cadence_report: Any = None,
) -> Dict[str, Any]:
    """Brain hints for snapshot-driven HUD refresh (wheel + conversation)."""
    c = cycle if cycle is not None else _telemetry_cycle()
    layers, active_idx = _brain_wheel_meta(c)
    out = _brain_live_fields(cadence_report=cadence_report)
    out["cycle_layers"] = layers
    out["cycle_active_index"] = active_idx
    out["dashboard"] = _brain_dashboard_metrics(c, live_fields=out)
    return out


def panel_brain() -> Dict[str, Any]:
    """Brain detail panel — cycle traversal + recent layers + cycle
    wheel data + conversation state.

    Spec ref: docs/architecture/HUD_PANEL_CONTENT_V2.md §2.1 + §9.2.
    P3-S10 (Layer-3 Wave-1C): extended for the Cycle Wheel hero. The
    wheel needs the layer registry to render segments, the active
    layer index to position the spotlight, top pressure contributors
    to position the orbiting satellites, and the current CueDesk
    suppress reason so the wheel freezes mid-rotation when the
    substrate is suppressing.
    """
    try:
        from guardian.terminal_feedback import emit_data
    except Exception:
        pass

    cycle = _telemetry_cycle()
    out: Dict[str, Any] = {
        "cycle": cycle,
        "layers": _telemetry_layers(8),
    }
    live = _telemetry_brain(cycle=cycle)
    out.update(live)
    out["dashboard"] = _brain_dashboard_metrics(cycle, live_fields=live)

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "panel_brain",
            ok=True,
            panel="brain",
            cycle_id=cycle.get("id", ""),
            active_layer=cycle.get("layer", ""),
            layer_count=len(out.get("layers", [])),
        )
    except Exception:
        pass

    return out


def panel_memory() -> Dict[str, Any]:
    """Memory detail panel — recall trace + top recall + persistence
    anchors. Same shape as `telemetry_snapshot()`'s memory subtree."""
    try:
        from guardian.terminal_feedback import emit_data
    except Exception:
        pass

    result = _telemetry_memory()

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "panel_memory",
            ok=True,
            panel="memory",
            recall_count=len(result.get("recent_recalls", [])),
            top_recall=result.get("top_recall", ""),
        )
    except Exception:
        pass

    return result


def panel_omega() -> Dict[str, Any]:
    """Omega detail panel — full identity + governance surface (8
    sections). Same shape as `_telemetry_omega()`."""
    try:
        from guardian.terminal_feedback import emit_data
    except Exception:
        pass

    result = _telemetry_omega()

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "panel_omega",
            ok=True,
            panel="omega",
            identity_hash=result.get("identity", {}).get("identity_hash", ""),
            mode=result.get("identity", {}).get("mode", ""),
        )
    except Exception:
        pass

    return result


def panel_autoloop() -> Dict[str, Any]:
    """Autoloop detail panel — pressure, cadence, curiosity, dev
    proposals, resolved, abandoned. Same shape as `_telemetry_autoloop()`."""
    try:
        from guardian.terminal_feedback import emit_data
    except Exception:
        pass

    # Reuse the snapshot cache when fresh so opening the panel does not
    # rerun sched.tick() + audit-ring walks on a second cadence tick.
    now = time.monotonic()
    cached = (
        _SNAPSHOT_CACHE is not None
        and (now - _SNAPSHOT_CACHE_TS) < _SNAPSHOT_CACHE_TTL_SECONDS
        and isinstance(_SNAPSHOT_CACHE.get("autoloop"), dict)
    )
    if cached:
        result = dict(_SNAPSHOT_CACHE["autoloop"])
        # Ring-backed respiratory fields must not lag the 1.5s snapshot TTL.
        result.update(_autoloop_audit_ring_overlay())
        try:
            from pressure_loop.pressure_loop_pause import pressure_loop_pause_state

            ps = pressure_loop_pause_state() or {}
            owners = ps.get("owners") or []
            result["pause_state"] = {
                "paused": bool(owners),
                "owners": [str(o) for o in owners][:8],
            }
        except Exception:
            pass
        result["dashboard"] = _autoloop_dashboard_metrics(result)
    else:
        result = _telemetry_autoloop(cadence_report=_cadence_tick_report())

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "panel_autoloop",
            ok=True,
            panel="autoloop",
            pressure=result.get("pressure", {}).get("aggregate", 0),
            curiosity_count=len(result.get("curiosity", [])),
        )
    except Exception:
        pass

    return result


def panel_meta() -> Dict[str, Any]:
    """Meta detail panel — last meta-loop cycle (orders + tool catalog).
    Same shape as `_telemetry_meta()`."""
    try:
        from guardian.terminal_feedback import emit_data
    except Exception:
        pass

    result = _telemetry_meta()

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "panel_meta",
            ok=True,
            panel="meta",
            cycle_id=result.get("cycle_id", ""),
            action=result.get("action", ""),
        )
    except Exception:
        pass

    return result


def panel_global() -> Dict[str, Any]:
    """Global detail panel — last global-loop cycle (observations +
    emitted goals). Same shape as `_telemetry_global()`."""
    try:
        from guardian.terminal_feedback import emit_data
    except Exception:
        pass

    result = _telemetry_global()

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "panel_global",
            ok=True,
            panel="global",
            cycle_id=result.get("cycle_id", ""),
            intent=result.get("intent", ""),
        )
    except Exception:
        pass

    return result


def panel_deepsleep() -> Dict[str, Any]:
    """DeepSleep detail panel — full 4-phase rollup of the latest cycle.

    Reads the live ``RECENT_DEEPSLEEP_CYCLES`` SharedState ring populated
    by the v2 DeepSleep loop. Surfaces all five biological-sleep functions:

        Phase 1 — intake counts
        Phase 2 — replay (observations) + schema integration (candidates)
        Phase 3 — contradiction resolution + active forgetting
        Phase 4 — consolidation + pruning + engine pattern

    Returns an empty/placeholder shape if no cycle has run yet, so
    the panel always renders cleanly.
    """
    out: Dict[str, Any] = {
        "cycle": {"id": "—", "timestamp": "—"},
        "intake": {"fragments": 0, "tools_known": 0, "insights": 0},
        "replay": {"count": 0, "history_kept": 0},
        "schemas": {"count": 0},
        "contradictions": {"detected": 0, "resolved": 0},
        "forgetting": {"candidates": 0, "applied": 0},
        "consolidation": {"promoted": 0, "reinforced": 0, "skipped": 0},
        "pruning": {"pruned": 0, "isolated": 0, "archived": 0},
        "engine": {"pattern": None, "sleep_cycle_id": None, "guardian_review_required": False},
        # P3-S19 reviewer-finding cure: initialize the P3-S18 Consolidation
        # Flow fields (phase_states / current_phase / next_cycle_eta)
        # at default-shape time so a fresh-boot panel_deepsleep call
        # (no audit log present yet) returns them populated rather than
        # missing. The post-audit-read block further below re-populates
        # phase_states from real data when a cycle has run; this
        # default ensures the contract holds in every code path.
        "phase_states": [
            {"phase": "intake", "name": "P1 · intake", "status": "pending", "summary": "—"},
            {
                "phase": "reactivation",
                "name": "P2 · reactivation",
                "status": "pending",
                "summary": "—",
            },
            {
                "phase": "reconciliation",
                "name": "P3 · reconciliation",
                "status": "pending",
                "summary": "—",
            },
            {
                "phase": "persistence",
                "name": "P4 · persistence",
                "status": "pending",
                "summary": "—",
            },
        ],
        "current_phase": None,
        "next_cycle_eta": "—",
    }
    # P3-S25 HUD live-feed doctrine v2 — read from RECENT_DEEPSLEEP_CYCLES
    # ring (EPHEMERAL_RUNTIME SharedState). Boot empty; fills as v2
    # DeepSleep consolidates episode/window cycles.
    rec = None
    try:
        from core.shared_state_keys import RECENT_DEEPSLEEP_CYCLES

        ring = _shared_state_read(RECENT_DEEPSLEEP_CYCLES)
        if isinstance(ring, list) and ring:
            tail = ring[-1]
            if isinstance(tail, dict):
                rec = tail
    except Exception as exc:
        log.debug("panel_deepsleep: ring read failed (%s)", exc)

    if rec is None:
        out["dashboard"] = _deepsleep_dashboard_metrics(out)
        _spa_enrich_deepsleep(out)
        return out

    out["cycle"] = {
        "id": str(rec.get("cycle_id") or "—"),
        "timestamp": str(rec.get("timestamp") or "—"),
        "audit_id": str(rec.get("audit_id") or ""),
        "age": _relative_age(rec.get("timestamp") or ""),
    }
    # Phase 1 — intake (cached on the persistence_report under the
    # legacy field names; defaults to 0 when not present)
    out["intake"] = {
        "fragments": int(rec.get("fragments_consolidated", 0) or 0),
        "tools_known": int(rec.get("tools_added", 0) or 0),  # legacy field; may be 0
        "insights": int(rec.get("global_insights", 0) or 0),
    }
    # Phase 2 — Reactivation
    out["replay"] = {
        "count": int(rec.get("replay_observations", 0) or 0),
        "history_kept": int(rec.get("replay_history_kept", 0) or 0),
    }
    out["schemas"] = {
        "count": int(rec.get("schema_candidates", 0) or 0),
    }
    # Phase 3 — Reconciliation
    out["contradictions"] = {
        "detected": int(rec.get("contradictions_detected", 0) or 0),
        "resolved": int(rec.get("contradictions_resolved", 0) or 0),
    }
    out["forgetting"] = {
        "candidates": int(rec.get("forgetting_candidates", 0) or 0),
        "applied": int(rec.get("forgetting_applied", 0) or 0),
    }
    # Phase 4 — Persistence + engine
    cons = rec.get("consolidation") or {}
    if isinstance(cons, dict):
        out["consolidation"] = {
            "promoted": int(cons.get("promoted", 0) or 0),
            "reinforced": int(cons.get("reinforced", 0) or 0),
            "skipped": int(cons.get("skipped", 0) or 0),
        }
    prun = rec.get("pruning") or {}
    if isinstance(prun, dict):
        out["pruning"] = {
            "pruned": int(prun.get("pruned", 0) or 0),
            "isolated": int(prun.get("isolated", 0) or 0),
            "archived": int(prun.get("archived", 0) or 0),
        }
    out["engine"] = {
        "pattern": rec.get("pattern_type"),
        "sleep_cycle_id": rec.get("engine_sleep_cycle_id"),
        "guardian_review_required": bool(rec.get("guardian_review_required") or False),
        "source_cycle_count": len(rec.get("source_cycles") or []),
    }

    # Cascade snapshot — per build_log 0106, DeepSleep is the loop
    # telemetry terminus. Each consolidation record carries
    # `upstream_telemetry` with a slot per upstream loop. Surface
    # which slots populated this cycle so the panel reflects "what
    # the cascade above contributed in the run-up to this sleep."
    out["upstream_telemetry"] = {
        "slots": [],
        "auto_cycle_id": None,
        "global_cycle_id": None,
        "meta_cycle_id": None,
        # Honest label (Audit C2): deepsleep runs before omega, so the
        # journal's omega slot is the PRIOR epoch's eval, not this
        # run-up's. Surfaced as omega_prior_epoch, not omega_epoch.
        "omega_prior_epoch": None,
        "brain_cycle_id": None,
    }
    try:
        from core import deepsleep_store

        sleep_id = rec.get("engine_sleep_cycle_id") or ""
        if sleep_id:
            cons_rec = deepsleep_store.load(sleep_id)
            snap = getattr(cons_rec, "upstream_telemetry", None) if cons_rec else None
            if isinstance(snap, dict):
                out["upstream_telemetry"]["slots"] = sorted(snap.keys())
                # 2026-05-14: slot keys = canonical layer names matching
                # the CycleEnvelope upstream block. 2026-05-18 (Audit
                # C2): the omega slot is "omega_prior" — deepsleep
                # precedes omega so it's the prior epoch's eval, not
                # this run-up's (was silently keyed "omega").
                ac = snap.get("auto") or {}
                gobs = snap.get("global") or {}
                meta = snap.get("meta") or {}
                ome = snap.get("omega_prior") or {}
                br = snap.get("brain") or {}
                out["upstream_telemetry"]["auto_cycle_id"] = ac.get("cycle_id")
                out["upstream_telemetry"]["global_cycle_id"] = gobs.get("cycle_id")
                out["upstream_telemetry"]["meta_cycle_id"] = meta.get("cycle_id")
                out["upstream_telemetry"]["omega_prior_epoch"] = ome.get("epoch")
                out["upstream_telemetry"]["brain_cycle_id"] = br.get("cycle_id")
    except Exception as exc:
        log.debug("panel_deepsleep: upstream_telemetry read failed (%s)", exc)

    # ---- P3-S18: Consolidation Flow — phase_states + ETA ---------
    # Spec §9.6: 4 phase nodes (P1 intake → P2 reactivation → P3
    # reconciliation → P4 persistence). Each node has a state:
    #   pending     — outlined dim circle (no data yet)
    #   active      — pulsing (cycle currently running this phase)
    #   completed   — solid with ✓ (cycle's terminal record present)
    # Active detection: the last audit row was written < 3s ago AND
    # there's no live ring (the audit lands at end-of-cycle, so a
    # very-fresh record means we just saw a completion). Cycle-mid
    # detection requires a live SharedState signal which doesn't
    # exist yet — deferred enhancement.
    out["phase_states"] = []
    out["current_phase"] = None
    out["next_cycle_eta"] = "—"

    from datetime import datetime as _dt, timezone as _tz

    cycle_ts = out["cycle"].get("timestamp") or ""
    cycle_age_s = None
    if cycle_ts and cycle_ts != "—":
        try:
            cycle_dt = _dt.fromisoformat(cycle_ts.replace("Z", "+00:00"))
            cycle_age_s = (_dt.now(_tz.utc) - cycle_dt).total_seconds()
        except Exception:
            cycle_age_s = None

    # Heuristic: if the cycle audit is younger than 3s, treat it as
    # "freshly completed" (visually it'll show all-4 completed with a
    # subtle just-landed glow; no animation chase).
    cycle_just_completed = cycle_age_s is not None and cycle_age_s < 3.0

    phase_specs = [
        ("intake", "P1 · intake", f"{out['intake'].get('fragments', 0)} fragments"),
        (
            "reactivation",
            "P2 · reactivation",
            f"{out['replay'].get('count', 0)} replays · {out['schemas'].get('count', 0)} schemas",
        ),
        (
            "reconciliation",
            "P3 · reconciliation",
            f"{out['contradictions'].get('detected', 0)} contradictions · "
            f"{out['forgetting'].get('applied', 0)} forgot",
        ),
        (
            "persistence",
            "P4 · persistence",
            f"{out['consolidation'].get('promoted', 0)} promoted · "
            f"{out['pruning'].get('pruned', 0)} pruned",
        ),
    ]
    has_cycle = out["cycle"].get("id", "—") != "—"
    for key, name, summary in phase_specs:
        if not has_cycle:
            status = "pending"
        else:
            status = "completed"
        out["phase_states"].append(
            {
                "phase": key,
                "name": name,
                "status": status,
                "summary": summary,
            }
        )

    # ETA: rough estimate — DeepSleep cadence is operator-tunable;
    # absent a live "next-scheduled" signal, report cycle age so
    # the operator at least knows when the last cycle ran.
    # Sub-minute bucket coarsened to a stable 'just now' (HUD live-feed
    # stabilization, 2026-05-28) so this ETA stops ticking per-second and
    # churning the deepsleep panel hash every 2s poll.
    if cycle_age_s is not None:
        age_int = int(cycle_age_s)
        if age_int < 60:
            out["next_cycle_eta"] = "last cycle just now"
        elif age_int < 3600:
            out["next_cycle_eta"] = f"last cycle {age_int // 60}m ago"
        elif age_int < 86400:
            out["next_cycle_eta"] = f"last cycle {age_int // 3600}h ago"
        else:
            out["next_cycle_eta"] = f"last cycle {age_int // 86400}d ago"
    elif cycle_just_completed:
        out["next_cycle_eta"] = "just completed"

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "panel_deepsleep",
            ok=True,
            panel="deepsleep",
            cycle_id=out.get("cycle", {}).get("id", ""),
            current_phase=out.get("current_phase"),
            next_cycle_eta=out.get("next_cycle_eta", ""),
        )
    except Exception:
        pass

    out["dashboard"] = _deepsleep_dashboard_metrics(out)
    _spa_enrich_deepsleep(out)
    return out


def _identity_self_vector_fields() -> Dict[str, Any]:
    """Self-vector trajectory + trait scores from RECENT_SELF_VECTORS ring."""
    out: Dict[str, Any] = {"selfVector": {}, "traits": []}
    try:
        from core.shared_state_keys import RECENT_SELF_VECTORS

        sv_ring = _shared_state_read(RECENT_SELF_VECTORS) or []
        recs = sv_ring if isinstance(sv_ring, list) else []
        if not recs:
            return out
        latest = recs[-1] if isinstance(recs[-1], dict) else {}
        conf = float(latest.get("confidence") or 0.0)
        drift_tag = "stable"
        if len(recs) >= 2:
            import math

            deltas = []
            for i in range(max(0, len(recs) - 5), len(recs) - 1):
                v0 = recs[i].get("vector") or []
                v1 = recs[i + 1].get("vector") or []
                if v0 and v1 and len(v0) == len(v1):
                    deltas.append(math.sqrt(sum((a - b) ** 2 for a, b in zip(v0, v1))))
            if deltas:
                avg = sum(deltas) / len(deltas)
                drift_tag = "stable" if avg < 0.10 else ("drifting" if avg < 0.30 else "rapid")
        drift_velocity = 0.0
        if len(recs) >= 2:
            import math

            deltas2 = []
            for i in range(max(0, len(recs) - 5), len(recs) - 1):
                v0 = recs[i].get("vector") or []
                v1 = recs[i + 1].get("vector") or []
                if v0 and v1 and len(v0) == len(v1):
                    deltas2.append(math.sqrt(sum((a - b) ** 2 for a, b in zip(v0, v1))))
            if deltas2:
                drift_velocity = sum(deltas2) / len(deltas2)
        out["selfVector"] = {
            "count": len(recs),
            "confidence": f"{conf:.2f}",
            "drift": drift_tag,
            "drift_velocity": round(drift_velocity, 4),
            "latest_at": _relative_age(latest.get("stamped_at") or ""),
        }
        latest_traits = latest.get("traits") or {}
        if isinstance(latest_traits, dict) and latest_traits:
            pairs = []
            for tname, tscore in latest_traits.items():
                try:
                    pairs.append({"name": str(tname), "score": float(tscore)})
                except (TypeError, ValueError):
                    continue
            pairs.sort(key=lambda p: p["score"], reverse=True)
            out["traits"] = pairs[:12]
    except Exception:
        pass
    return out


def _telemetry_identity() -> Dict[str, Any]:
    """Lightweight identity subtree for the main telemetry poll."""
    out: Dict[str, Any] = {
        "operators": [],
        "selfVector": {},
        "traits": [],
        "closedWorld": {"sealed": False, "ledger_ok": False},
        "dashboard": {},
    }
    sv_fields = _identity_self_vector_fields()
    out["selfVector"] = sv_fields.get("selfVector") or {}
    out["traits"] = sv_fields.get("traits") or []
    try:
        c = _probe_charter() or {}
        out["closedWorld"] = {
            "sealed": (
                c.get("mode") == "production"
                and bool(c.get("verify_ok"))
                and bool(c.get("ledger_ok"))
            ),
            "ledger_ok": bool(c.get("ledger_ok")),
        }
    except Exception:
        pass
    out["dashboard"] = _identity_dashboard_metrics(out)
    return out


def panel_identity() -> Dict[str, Any]:
    """Identity detail panel — charter + operator + self-vector + voice
    + traits + closedWorld + recentEvents.

    Spec ref: docs/architecture/HUD_PANEL_CONTENT_V2.md §2.4. Layer-3 Wave-1A
    rebuild adds 3 new blocks alongside the orbital-hero Presence Orb in the
    client.

    Sections:
        operators    — list of {name, sample_count, anchored_at, thumbnail_url}
        selfVector   — {count, confidence, drift, drift_velocity, latest_at}
        voice        — {engine, model, voice_resident, sample_rate}
        traits       — list of {name, score} (latest record; empty if absent)
        closedWorld  — {mode, identity_hash, verify_ok, ledger_ok, sealed}
        recentEvents — last 5 identity-relevant audit rows (signal/phase)
    """
    out: Dict[str, Any] = {
        "operators": [],
        "selfVector": {},
        "voice": {},
        "traits": [],
        "charter": _probe_charter(),
        "closedWorld": {},
        "recentEvents": [],
    }

    # ---- Operators (face anchors) --------------------------------
    try:
        from runtime.face.anchor_store import get_anchor_store
        from runtime.face.recognition import anchor_thumbnail_bytes

        store = get_anchor_store()
        store._ensure_loaded()
        for name, entry in (store._anchors or {}).items():
            # P3-S08 cure: thumbnail existence check goes through the
            # whitelist+resolve-under-root validator so any pre-cure
            # anchor with a name that wouldn't pass today's whitelist
            # surfaces as null instead of as a link to a 404.
            try:
                thumb_exists = anchor_thumbnail_bytes(name) is not None
            except ValueError:
                thumb_exists = False
            out["operators"].append(
                {
                    "name": name,
                    "sample_count": int(entry.get("sample_count") or 0),
                    "anchored_at": (entry.get("anchored_at") or "")[:10],
                    "updated_at": _relative_age(entry.get("updated_at") or ""),
                    "thumbnail_url": f"/api/vision/anchor-thumbnail/{name}"
                    if thumb_exists
                    else None,
                }
            )
    except Exception:
        out["operators"] = []

    # ---- Self-vector trajectory ----------------------------------
    sv_fields = _identity_self_vector_fields()
    out["selfVector"] = sv_fields.get("selfVector") or {}
    out["traits"] = sv_fields.get("traits") or []

    # ---- Voice posture -------------------------------------------
    try:
        from runtime.tts import status as _tts_status

        st = _tts_status() or {}
        out["voice"] = {
            "engine": str(st.get("engine") or "unavailable"),
            "model": (st.get("model_file") or "").split("\\")[-1],
            "voice_resident": bool(st.get("voice_resident")),
            "sample_rate": st.get("sample_rate"),
        }
    except Exception:
        pass

    # ---- Closed-world status -------------------------------------
    # Distinct from charter: surfaces the *posture* (sealed vs dev,
    # ledger integrity, identity-hash full) the way an operator looks
    # at it. Charter block stays compact (hash/mode/born). This is
    # the integrity-of-self readout.
    try:
        c = out.get("charter") or {}
        out["closedWorld"] = {
            "mode": c.get("mode", "—"),
            "identity_hash": c.get("identity_hash", "—"),
            "verify_ok": bool(c.get("verify_ok")),
            "ledger_ok": bool(c.get("ledger_ok")),
            "ledger_entries": c.get("ledger_entries", "—"),
            "sealed": (
                c.get("mode") == "production"
                and bool(c.get("verify_ok"))
                and bool(c.get("ledger_ok"))
            ),
        }
    except Exception:
        out["closedWorld"] = {}

    # ---- Recent identity-relevant events -------------------------
    # P3-S25 HUD live-feed doctrine v2 — read in-process events ring,
    # filter to identity / presence / face / anchor signals + phases.
    # No disk tail. Disk audit log remains the forensic record.
    try:

        def _is_identity_event(rec: Dict[str, Any]) -> bool:
            nm = (rec.get("name") or rec.get("phase") or "").lower()
            if not nm:
                return False
            return (
                nm.startswith("identity.")
                or nm.startswith("response.presence_gate")
                or "presence" in nm
                or "vision_anchor" in nm
                or nm.startswith("face_")
            )

        ring = _audit_events(
            predicate=_is_identity_event,
            limit=5,
            newest_first=True,
        )
        hits = []
        for rec in ring:
            nm = (rec.get("name") or rec.get("phase") or "").lower()
            hits.append(
                {
                    "ts": rec.get("ts", ""),
                    "kind": rec.get("kind", ""),
                    "name": nm,
                    "status": rec.get("status", ""),
                }
            )
        out["recentEvents"] = hits
    except Exception:
        out["recentEvents"] = []

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "panel_identity",
            ok=True,
            panel="identity",
            mode=out.get("charter", {}).get("mode", ""),
            operator_count=len(out.get("operators", [])),
            sealed=out.get("closedWorld", {}).get("sealed", False),
        )
    except Exception:
        pass

    out["dashboard"] = _identity_dashboard_metrics(out)
    return out


def panel_settings() -> Dict[str, Any]:
    """Settings detail panel — operator profile, HUD theme, voice & speech.

    Aggregates all settings data that the Settings panel displays.
    Settings are persisted via dedicated routes; this function provides
    a unified read-only view for the panel endpoint.

    Sections:
        operatorProfile — name, preferred name, birthday, pronouns, etc.
        hudTheme        — theme, density, motion, glow, panel_depth
        voiceSpeech     — TTS/STT settings, voice configuration
        functions       — available settings functions
    """
    from hud import settings as hud_settings

    out: Dict[str, Any] = {
        "operatorProfile": {},
        "hudTheme": {},
        "voiceSpeech": {},
        "functions": [],
    }

    # ---- Operator profile -------------------------------------------
    try:
        from hud.operator_identity import latest_operator_profile_fields

        fields = latest_operator_profile_fields()
        if fields:
            out["operatorProfile"] = {
                "name": fields.get("name", ""),
                "preferred_name": fields.get("preferred_name", ""),
                "birthday": fields.get("birthday", ""),
                "pronouns": fields.get("pronouns", ""),
                "location": fields.get("location", ""),
                "timezone": fields.get("timezone", ""),
                "occupation": fields.get("occupation", ""),
                "relationship": fields.get("relationship", fields.get("role", "")),
                "notes": fields.get("notes", ""),
            }
    except Exception:
        log.exception("panel_settings: operator profile read failed")
        raise

    # ---- HUD theme settings -----------------------------------------
    try:
        theme_data = hud_settings.load_hud_theme_settings()
        out["hudTheme"] = theme_data.get("settings", {})
        out["hudTheme"]["options"] = {
            "theme": hud_settings.HUD_THEME_OPTIONS.get("theme", ()),
            "density": hud_settings.HUD_THEME_OPTIONS.get("density", ()),
            "motion": hud_settings.HUD_THEME_OPTIONS.get("motion", ()),
            "glow": hud_settings.HUD_THEME_OPTIONS.get("glow", ()),
            "panel_depth": hud_settings.HUD_THEME_OPTIONS.get("panel_depth", ()),
        }
    except Exception as exc:
        log.debug("panel_settings: hud theme load failed: %s", exc)

    # ---- Voice & speech settings ------------------------------------
    try:
        voice_data = hud_settings.load_voice_settings()
        out["voiceSpeech"] = voice_data.get("settings", {})
        out["voiceSpeech"]["options"] = {
            "tts_voice": hud_settings.HUD_VOICE_OPTIONS.get("tts_voice", ()),
            "stt_language": hud_settings.HUD_VOICE_OPTIONS.get("stt_language", ()),
            "stt_task": hud_settings.HUD_VOICE_OPTIONS.get("stt_task", ()),
            "stt_model": hud_settings.HUD_VOICE_OPTIONS.get("stt_model", ()),
        }
    except Exception as exc:
        log.debug("panel_settings: voice settings load failed: %s", exc)

    # ---- Settings functions ----------------------------------------
    try:
        out["functions"] = hud_settings.settings_functions()
    except Exception as exc:
        log.debug("panel_settings: settings functions failed: %s", exc)

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "hud",
            "panel_settings",
            ok=True,
            panel="settings",
            theme=out.get("hudTheme", {}).get("theme", ""),
            tts_enabled=out.get("voiceSpeech", {}).get("tts_enabled", False),
            stt_enabled=out.get("voiceSpeech", {}).get("stt_enabled", False),
            has_picture=bool(out.get("operatorProfile", {}).get("picture_path", "")),
        )
    except Exception:
        pass

    return out
