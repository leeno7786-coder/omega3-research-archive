"""
Cycle cadence -- compatibility scheduler and signal helper
===========================================================

Per `Final Decisions.txt` item 6:

> **Decision: hybrid trigger system**
> Never pure timer. Never pure event. Use both.
>
> Base sweep -- Light autonomous sweep every 90-180 seconds depending
> on system pressure.
>
> Immediate triggers -- Fire instantly on:
>   - contradiction spike
>   - Guardian intervention
>   - persistence threshold crossing
>   - failed repeated tool action
>   - unresolved tension escalation
>   - high-value curiosity event
>   - self-vector drift threshold
>
> This creates biological pressure-driven cognition. Far stronger.

Historically this module was the scheduler that decided when to ask
`core.autonomous_engine.sweep_and_initiate` to run a cycle. After the
Symphony S17 cutover, product boot no longer uses it as a conductor:
`boot.run_continuous()` calls `CueDesk.tick(...)` only. The scheduler
surface and signal-provider helpers remain for compatibility tests,
direct callers, and pressure inputs such as JSONL tail scans.

Pressure-modulated base interval: when many signals are firing, the
scheduler tightens to MIN_INTERVAL (90s). When the system is quiet,
it relaxes to MAX_INTERVAL (180s). Linear in pressure_score [0, 1].

ARCHITECTURAL RULES:
    - Pure decision module: tick() is read-only against signal
      providers; no side effects. The caller acts on the report.
    - Signal providers are pluggable: register_signal_provider lets
      future subsystems contribute new pressure signals without
      modifying scheduler core.
    - Provider failure is per-provider: a raise from one provider
      logs at WARNING and excludes that signal from the tick;
      others still run. Scheduler never crashes the caller.
    - In symphony mode, tick reports are silenced as initiation
      authority. Do not wire `tick()` beside CueDesk as a second
      product conductor.
    - Audit: every compatibility initiation decision (immediate or
      timer) is written fsync'd to
      ``guardian/logs/cycle_cadence_audit.jsonl`` so the audit trail
      names exactly which trigger fired and why.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from core.paths import OMEGA_ROOT

log = logging.getLogger("omega.pressure_loop.cycle_cadence")


def _emit_failure(operation: str, status: str, exc: Exception | None = None, **fields) -> None:
    """Lazy-import structured failure emit → guardian/logs (poller-parseable).
    Own failure handler is log.debug ONLY (never emit — no recursion)."""
    try:
        from guardian.terminal_feedback import emit_signal

        payload = {
            "component": "cycle_cadence",
            "operation": operation,
            "status": status,
        }
        if exc is not None:
            payload["error_type"] = type(exc).__name__
            payload["reason"] = str(exc)[:120]
        payload.update(fields)
        emit_signal("cycle_cadence.failure", 1.0, **payload)
    except Exception as _e:  # noqa: BLE001
        log.debug("cycle_cadence: emit_failure suppressed: %s", _e)


_AUDIT_PATH = OMEGA_ROOT / "guardian" / "logs" / "cycle_cadence_audit.jsonl"
_audit_lock = threading.Lock()


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Default pressure window — providers count signals only within this
# many seconds of now. Mirrors CadenceConfig.pressure_window_seconds;
# providers are zero-arg (registry contract) so they read the module
# default rather than a per-scheduler config.
_PRESSURE_WINDOW_SECONDS = 600.0

# Cap how many trailing lines a provider parses per tick. Anything
# older than the cap is necessarily older than the window too, so
# this bounds per-tick I/O without changing the time-filter result.
_TAIL_SCAN_CAP = 4000


def _recent_jsonl_entries(
    rel_parts: tuple[str, ...],
    *,
    window_seconds: float = _PRESSURE_WINDOW_SECONDS,
    now: Optional[datetime] = None,
):
    """Yield parsed jsonl entries whose `ts`/`timestamp` is within
    `window_seconds` of now, scanning at most the last
    `_TAIL_SCAN_CAP` lines.

    This is THE fix for the phantom-pressure spin: log-tailing
    providers must count signals by TIME, not by line position.
    A frozen log tail (dev-mode pause, idle) ages out of the window
    so the provider goes quiet instead of firing every tick forever.
    Entries with no parseable timestamp are EXCLUDED — a stale log
    must not keep pressure hot.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - window_seconds
    path = OMEGA_ROOT.joinpath(*rel_parts)
    try:
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-_TAIL_SCAN_CAP:]
    except OSError as exc:
        _emit_failure(
            "recent_jsonl_entries",
            "degraded",
            exc,
            reason="signal_log_unreadable",
            log_path="/".join(rel_parts),
        )
        return
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        raw_ts = entry.get("ts") or entry.get("timestamp")
        if not isinstance(raw_ts, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw_ts)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.timestamp() >= cutoff:
            yield entry


def _emit_audit(event: Dict[str, Any]) -> None:
    """Append-then-fsync. Never blocks the scheduler."""
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _audit_lock:
            with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
    except OSError as exc:
        log.debug("cadence audit write failed: %s", exc)


# =====================================================================
# Schemas
# =====================================================================


@dataclass(frozen=True)
class PressureSignal:
    """One observation from a registered provider."""

    name: str  # signal identifier (e.g. "contradiction_spike")
    value: float  # raw signal magnitude [0, inf)
    threshold: float  # value >= threshold => fire_now
    fire_now: bool  # immediate-trigger flag
    weight: float = 1.0  # contribution to pressure_score [0, 1]
    note: str = ""  # human-readable explanation


@dataclass(frozen=True)
class CadenceConfig:
    """Tunable cadence parameters."""

    min_interval_seconds: float = 90.0
    max_interval_seconds: float = 180.0
    pressure_window_seconds: float = 600.0  # how far back triggers count
    audit_enabled: bool = True


@dataclass(frozen=True)
class CadenceTickReport:
    """Returned from tick(). Caller acts on `should_initiate`."""

    decided_at: str
    should_initiate: bool
    trigger_kind: str  # "immediate" | "timer" | "none" | "symphony_silenced"
    trigger_signals: List[PressureSignal] = field(default_factory=list)
    pressure_score: float = 0.0  # [0, 1] aggregate
    interval_used: float = 0.0  # current modulated interval
    seconds_since_last_sweep: float = 0.0
    next_eligible_at_seconds: float = 0.0  # 0 if should_initiate=True
    # Symphony S3 trace flag. True when symphony_mode_enabled() forced
    # should_initiate=False because CueDesk owns the conductor surface
    # in symphony mode -- cadence still polls signals (so pressure_score
    # remains a valid contributor input) but does NOT parallel-fire
    # sweep_and_initiate / response_engine. Legacy_epoch ticks always
    # have pressure_contributed=False.
    pressure_contributed: bool = False


# =====================================================================
# Scheduler
# =====================================================================


class HybridScheduler:
    """The hybrid timer + immediate-trigger cadence engine.

    Lifecycle:
        sched = HybridScheduler(CadenceConfig())
        sched.register_signal_provider("contradiction_spike", provider_fn)
        ... while True:
        ...     report = sched.tick()
        ...     if report.should_initiate:
        ...         sweep_and_initiate(limit=1)
        ...         sched.acknowledge_initiated()
    """

    def __init__(self, config: Optional[CadenceConfig] = None):
        self.config = config or CadenceConfig()
        self._providers: Dict[str, Callable[[], PressureSignal]] = {}
        self._last_sweep_time: float = 0.0
        # Self-throttle: time of the last should_initiate=True decision
        # (immediate OR timer). The immediate path enforces a refractory
        # off THIS, not _last_sweep_time, so the scheduler can't thrash
        # even if the caller never calls acknowledge_initiated(). 0.0 =
        # never fired, so the first hot signal fires instantly (correct
        # boot-time reactivity). Biological model: refractory period
        # after firing.
        self._last_initiate_time: float = 0.0
        self._lock = threading.Lock()

    # -- Provider registry ---------------------------------------------

    def register_signal_provider(
        self,
        name: str,
        fn: Callable[[], PressureSignal],
    ) -> None:
        """Register a per-signal provider. Idempotent; replacing an
        existing name is allowed (last write wins)."""
        if not name or not callable(fn):
            raise ValueError("register_signal_provider requires non-empty name + callable")
        self._providers[name] = fn

    def unregister_signal_provider(self, name: str) -> None:
        self._providers.pop(name, None)

    def registered_signals(self) -> List[str]:
        return sorted(self._providers.keys())

    # -- State ----------------------------------------------------------

    def acknowledge_initiated(self, *, now: Optional[float] = None) -> None:
        """Caller MUST call this after running a cycle; otherwise the
        scheduler thinks the request is still pending and may double-fire
        next tick."""
        with self._lock:
            self._last_sweep_time = now if now is not None else time.time()
        if self.config.audit_enabled:
            _emit_audit(
                {
                    "event": "cycle_cadence_acknowledged",
                    "timestamp": _iso(),
                    "last_sweep_time": self._last_sweep_time,
                }
            )

    def last_sweep_time(self) -> float:
        with self._lock:
            return self._last_sweep_time

    # -- Tick -----------------------------------------------------------

    def tick(self, *, now: Optional[float] = None) -> CadenceTickReport:
        """Decide whether to initiate. Read-only against providers;
        no side effects beyond audit log.

        Symphony S3 rewire: when conductor_mode is symphony, the
        cadence still polls signal providers (so its pressure_score
        remains a valid input contributor for CueDesk + a useful
        trace), but it FORCES should_initiate=False -- the cue desk
        (core.cue_desk.CueDesk.tick) owns the conductor surface in
        symphony mode, not the legacy sweep_and_initiate /
        response_engine path. Legacy_epoch ticks are unchanged."""
        now = now if now is not None else time.time()
        signals = self._poll_signals()
        triggered = [s for s in signals if s.fire_now]
        pressure = self._pressure_score(signals)
        interval = self._modulate_interval(pressure)

        # Symphony short-circuit -- evaluated BEFORE the trigger/timer
        # paths so a sustained hot signal can't accidentally fire the
        # legacy sweep in symphony mode (and the audit row clearly
        # records why we're silencing). Module-level import would
        # create a boot-order coupling; deferred so cycle_cadence
        # stays importable when symphony_policy isn't on the path yet.
        try:
            from pressure_loop.policy import symphony_mode_enabled

            _symphony = symphony_mode_enabled()
        except Exception:
            _symphony = True
        if _symphony:
            with self._lock:
                since = now - self._last_sweep_time
            return CadenceTickReport(
                decided_at=_iso(),
                should_initiate=False,
                trigger_kind="symphony_silenced",
                trigger_signals=triggered,  # for trace, not action
                pressure_score=pressure,
                interval_used=interval,
                seconds_since_last_sweep=since,
                next_eligible_at_seconds=0.0,
                pressure_contributed=True,
            )

        with self._lock:
            since = now - self._last_sweep_time

        # Immediate trigger wins over the (pressure-modulated) timer —
        # it can fire as early as min_interval instead of waiting up to
        # max_interval — but it is NOT exempt from a refractory. A
        # sustained hot signal must not re-fire every boot-loop tick;
        # it fires once, then is suppressed for min_interval. Gated off
        # _last_initiate_time so this holds even if the caller never
        # acknowledges (self-throttling per ARCHITECTURAL RULES).
        with self._lock:
            since_initiate = now - self._last_initiate_time
        in_refractory = since_initiate < self.config.min_interval_seconds
        if triggered and not in_refractory:
            report = CadenceTickReport(
                decided_at=_iso(),
                should_initiate=True,
                trigger_kind="immediate",
                trigger_signals=triggered,
                pressure_score=pressure,
                interval_used=interval,
                seconds_since_last_sweep=since,
                next_eligible_at_seconds=0.0,
            )
            with self._lock:
                self._last_initiate_time = now
            if self.config.audit_enabled:
                _emit_audit(
                    {
                        "event": "cycle_cadence_initiate",
                        "timestamp": report.decided_at,
                        "trigger_kind": "immediate",
                        "trigger_signals": [
                            {
                                "name": s.name,
                                "value": s.value,
                                "threshold": s.threshold,
                                "note": s.note,
                            }
                            for s in triggered
                        ],
                        "pressure_score": pressure,
                    }
                )
            return report

        # Timer path. Also subject to the refractory floor: nothing
        # initiates within min_interval of the last initiation. This
        # also closes the no-acknowledge thrash — if the caller never
        # calls acknowledge_initiated(), _last_sweep_time stays 0 and
        # `since` is unbounded, but _last_initiate_time still gates.
        if since >= interval and not in_refractory:
            report = CadenceTickReport(
                decided_at=_iso(),
                should_initiate=True,
                trigger_kind="timer",
                trigger_signals=[],
                pressure_score=pressure,
                interval_used=interval,
                seconds_since_last_sweep=since,
                next_eligible_at_seconds=0.0,
            )
            with self._lock:
                self._last_initiate_time = now
            if self.config.audit_enabled:
                _emit_audit(
                    {
                        "event": "cycle_cadence_initiate",
                        "timestamp": report.decided_at,
                        "trigger_kind": "timer",
                        "interval_used": interval,
                        "pressure_score": pressure,
                        "seconds_since_last_sweep": since,
                    }
                )
            return report

        # Hold.
        return CadenceTickReport(
            decided_at=_iso(),
            should_initiate=False,
            trigger_kind="none",
            trigger_signals=[],
            pressure_score=pressure,
            interval_used=interval,
            seconds_since_last_sweep=since,
            next_eligible_at_seconds=max(0.0, interval - since),
        )

    # -- Internal -------------------------------------------------------

    def _poll_signals(self) -> List[PressureSignal]:
        out: List[PressureSignal] = []
        for name, fn in self._providers.items():
            try:
                signal = fn()
            except Exception as exc:
                log.warning("cadence: signal provider %r raised: %s", name, exc)
                _emit_failure(
                    "poll_signals", "degraded", exc, reason="provider_raised", provider=name
                )
                continue
            if signal is None:
                continue
            if not isinstance(signal, PressureSignal):
                log.warning(
                    "cadence: provider %r returned non-PressureSignal (%s); skipping",
                    name,
                    type(signal).__name__,
                )
                _emit_failure(
                    "poll_signals",
                    "degraded",
                    reason="non_pressuresignal_return",
                    provider=name,
                    returned_type=type(signal).__name__,
                )
                continue
            out.append(signal)
        return out

    def _pressure_score(self, signals: List[PressureSignal]) -> float:
        """Weighted mean of normalized signal values, clipped to [0, 1].

        Each signal's value is normalized against its threshold:
            normalized = min(1.0, value / threshold)  (when threshold > 0)
        """
        if not signals:
            return 0.0
        weighted_sum = 0.0
        total_weight = 0.0
        for s in signals:
            if s.threshold > 0:
                normalized = min(1.0, s.value / s.threshold)
            else:
                normalized = 1.0 if s.fire_now else 0.0
            weighted_sum += s.weight * normalized
            total_weight += s.weight
        if total_weight <= 0:
            return 0.0
        return max(0.0, min(1.0, weighted_sum / total_weight))

    def _modulate_interval(self, pressure: float) -> float:
        """High pressure -> MIN_INTERVAL; low pressure -> MAX_INTERVAL.
        Linear interpolation."""
        lo = self.config.min_interval_seconds
        hi = self.config.max_interval_seconds
        return hi - max(0.0, min(1.0, pressure)) * (hi - lo)


# =====================================================================
# Built-in signal providers (one per Final-Decisions trigger)
# =====================================================================

# Default thresholds. Conservative; future calibration via L11 reflection
# wisdom or observed false-positive / false-negative rates.
_DEFAULT_CONTRADICTION_SPIKE_THRESHOLD = 3  # >=3 contradictions in window
_DEFAULT_GUARDIAN_INTERVENTION_THRESHOLD = 1  # any new intervention fires
_DEFAULT_TOOL_FAILURE_THRESHOLD = 3  # >=3 same-tool failures
_DEFAULT_TENSION_ESCALATION_THRESHOLD = 0.75  # tension score >=0.75
_DEFAULT_CURIOSITY_EVENT_THRESHOLD = 0.85  # curiosity urgency >=0.85


def _truthy_signal(obj: Any, key_substr: str) -> bool:
    """True iff `obj` (recursively) has a key containing `key_substr`
    mapping to a POSITIVE value: non-empty list, number > 0, or True.

    This is the architectural fix for the phantom-pressure disease
    class: pressure providers must read STRUCTURED signal fields, not
    substrings of serialized telemetry. `"contradiction" in
    json.dumps(entry)` matched routine logs that report ZERO
    contradictions ("contradictions": [], "contradictions_detected":
    0, "no contradiction-shape pairs found") — the word is ubiquitous
    in benign telemetry. Matching the structured value instead
    separates "a contradiction was detected" from "the word appears."
    String values are ignored (journal repr-summaries are secondary,
    not the live signal).
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if key_substr in str(k).lower():
                if isinstance(v, bool):
                    if v:
                        return True
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    if v > 0:
                        return True
                elif isinstance(v, (list, tuple, set)):
                    if len(v) > 0:
                        return True
            if _truthy_signal(v, key_substr):
                return True
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            if _truthy_signal(item, key_substr):
                return True
    return False


def contradiction_spike_provider() -> PressureSignal:
    """Count entries with a STRUCTURED positive contradiction signal
    in layer_state WITHIN the pressure window. Time-bounded (frozen
    tail ages out) AND structure-bounded (not a substring match) —
    see _recent_jsonl_entries + _truthy_signal."""
    count = 0
    for entry in _recent_jsonl_entries(("guardian", "logs", "layer_state.jsonl")):
        if _truthy_signal(entry, "contradiction"):
            count += 1
    threshold = _DEFAULT_CONTRADICTION_SPIKE_THRESHOLD
    return PressureSignal(
        name="contradiction_spike",
        value=float(count),
        threshold=float(threshold),
        fire_now=count >= threshold,
        weight=1.5,
        note=(
            f"contradiction layer_state entries in last {int(_PRESSURE_WINDOW_SECONDS)}s: {count}"
        ),
    )


def guardian_intervention_provider() -> PressureSignal:
    """Count guardian interventions in the recent ledger tail."""
    # Count ONLY adversarial decisions. Per Guardian doctrine
    # (project_guardian_out_of_memory_path /
    # project_guardian_autodrain_deepsleep) a default-allow
    # auto-commit is the bio-loop working normally — it is NOT an
    # intervention and must generate ZERO cognitive pressure.
    # A real intervention = Guardian actually stopped/quarantined
    # something. Counting benign auto-approvals here was the
    # production phantom-pressure spin (every memory promotion =
    # one "approve" = permanent immediate-trigger).
    _ADVERSARIAL = {"reject", "quarantine", "flag", "block", "deny"}
    count = 0
    for e in _recent_jsonl_entries(("guardian", "logs", "guardian_interventions.jsonl")):
        kind = str(e.get("kind", "")).strip().lower()
        if kind in _ADVERSARIAL:
            count += 1
    threshold = _DEFAULT_GUARDIAN_INTERVENTION_THRESHOLD
    return PressureSignal(
        name="guardian_intervention",
        value=float(count),
        threshold=float(threshold),
        fire_now=count >= threshold,
        weight=2.0,  # Guardian events are high-priority
        note=(
            f"adversarial guardian interventions in last {int(_PRESSURE_WINDOW_SECONDS)}s: {count}"
        ),
    )


def persistence_threshold_provider() -> PressureSignal:
    """STUB until L13 (Persistence layer) lands. Returns zero pressure."""
    return PressureSignal(
        name="persistence_threshold",
        value=0.0,
        threshold=1.0,
        fire_now=False,
        weight=1.0,
        note="L13 persistence layer not yet built; provider is a stub",
    )


def tool_failure_provider() -> PressureSignal:
    """Count repeated tool-failure markers in the layer_state tail."""
    count = 0
    for entry in _recent_jsonl_entries(("guardian", "logs", "layer_state.jsonl")):
        if "action" not in str(entry.get("layer_id", "")).lower():
            continue
        # Structured failure signal only — `"fail" in json.dumps`
        # matched "failure_reason": null and benign strings. Require a
        # positive failed/failure/error field (same disease-class fix
        # as contradiction_spike). did_work==False is also a failure.
        if (
            entry.get("did_work") is False
            or _truthy_signal(entry, "fail")
            or _truthy_signal(entry, "error")
        ):
            count += 1
    threshold = _DEFAULT_TOOL_FAILURE_THRESHOLD
    return PressureSignal(
        name="tool_failure",
        value=float(count),
        threshold=float(threshold),
        fire_now=count >= threshold,
        weight=1.0,
        note=(f"action-layer failures in last {int(_PRESSURE_WINDOW_SECONDS)}s: {count}"),
    )


def tension_escalation_provider() -> PressureSignal:
    """Read scoring outputs from layer_state for high tension scores."""
    max_tension = 0.0
    for e in _recent_jsonl_entries(("guardian", "logs", "layer_state.jsonl")):
        schema = e.get("schema") or {}
        # Tension may live in different schema keys depending on the
        # layer; check the obvious locations.
        for key in ("tension_score", "tension", "unresolved_tension"):
            v = schema.get(key) if isinstance(schema, dict) else None
            if isinstance(v, (int, float)) and v > max_tension:
                max_tension = float(v)
    threshold = _DEFAULT_TENSION_ESCALATION_THRESHOLD
    return PressureSignal(
        name="tension_escalation",
        value=max_tension,
        threshold=threshold,
        fire_now=max_tension >= threshold,
        weight=1.5,
        note=(f"max tension in last {int(_PRESSURE_WINDOW_SECONDS)}s: {max_tension:.2f}"),
    )


def curiosity_event_provider() -> PressureSignal:
    """Read open curiosity entries; fire on any with urgency above threshold.

    BUG-FIX 2026-05-05: previously imported `list_entries` (does not
    exist) + checked `resolved` boolean (schema uses `status` enum) —
    bare-except swallowed both errors, provider returned 0 silently.
    Now uses the actual public API `list_open()` which filters by
    status correctly, so high-urgency curiosities can finally
    trip the cadence.
    """
    max_urgency = 0.0
    open_count = 0
    try:
        from hud.curiosity_store import list_open

        for e in list_open():
            open_count += 1
            u = float(getattr(e, "urgency_score", 0.0) or 0.0)
            if u > max_urgency:
                max_urgency = u
    except Exception as exc:
        log.debug("curiosity_event_provider: list_open failed (%s)", exc)
        _emit_failure("curiosity_event_provider", "degraded", exc, reason="list_open_failed")
    threshold = _DEFAULT_CURIOSITY_EVENT_THRESHOLD
    return PressureSignal(
        name="curiosity_event",
        value=max_urgency,
        threshold=threshold,
        fire_now=max_urgency >= threshold,
        weight=1.0,
        note=f"max curiosity urgency: {max_urgency:.2f} (open: {open_count})",
    )


# =====================================================================
# Convenience: build a scheduler with all defaults registered
# =====================================================================


def build_default_scheduler(
    config: Optional[CadenceConfig] = None,
) -> HybridScheduler:
    """Construct a HybridScheduler with the seven Final-Decisions
    signal providers pre-registered."""
    sched = HybridScheduler(config)
    sched.register_signal_provider("contradiction_spike", contradiction_spike_provider)
    sched.register_signal_provider("guardian_intervention", guardian_intervention_provider)
    sched.register_signal_provider("persistence_threshold", persistence_threshold_provider)
    sched.register_signal_provider("tool_failure", tool_failure_provider)
    sched.register_signal_provider("tension_escalation", tension_escalation_provider)
    sched.register_signal_provider("curiosity_event", curiosity_event_provider)
    log.info(
        "cycle_cadence: built default scheduler with %d providers", len(sched.registered_signals())
    )
    return sched
