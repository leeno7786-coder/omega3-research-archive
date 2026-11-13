"""
sensory/operator_presence.py — Operator Presence Contract (S2)
===========================================================

Spec: docs/research/Enrollment is closed-world.txt (Presence-Gated
Identity Loop) + operator binding answer #2 (which SUPERSEDES the
txt's simpler `latch_seconds` form). Binding contract:
project_reflective_continuity_integration_contract.

TWO-STAGE SEPARATION (the spine):
  - RECOGNITION = brain-local runtime.face.anchor_store.identify, gated by
    RECOGNITION_THRESHOLD = 0.4 (the B7 cross-process IDENTITY TRUTH).
    Answers: "is this plausibly the enrolled operator?". This module
    NEVER imports, mutates, or re-derives that threshold or the store.
  - PRESENCE (this module) = HUD / audit latch only (PRESENT|ABSENT|
    UNKNOWN). Consumes loader.identify() output (recognized_name /
    confidence / degraded). It does NOT gate Symphony episode open,
    pressure, or proactive speech — face is for remembering names,
    not locking the substrate.

DEGRADED CONTRACT (enabled by this session's 59b57db):
  loader.identify() `degraded: True` (local face models unavailable) is NOT
  "operator absent". On degraded -> state UNKNOWN, HOLD the last
  stable state, NO decay, NO ENTER/EXIT. Infra-down must never drop
  the presence latch nor mis-resolve held reflective lineages.

CLOSED-WORLD: presence latches ONLY when the recognized name equals
the enrolled OPERATOR (not any enrolled anchor). No enrollment ->
the pill stays non-PRESENT; cognition and outreach are unaffected.

Pure/deterministic state machine (no I/O, no clock inside — `now` is
injected, so it is unit-testable headless). The watcher is the only
loop; under QUARANTINE Claude integrates it but never runs it (webcam
+ live verification are operator-owned). ASCII only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger("omega.sensory.operator_presence")

PRESENT = "PRESENT"
ABSENT = "ABSENT"
UNKNOWN = "UNKNOWN"

# Operator-locked hysteresis (binding answer #2). Code defaults mirror
# config/omega.yaml :: omega.presence; the live values come from there.
_DEFAULTS = {
    "acquire_threshold": 0.82,
    "release_threshold": 0.65,
    "acquire_frames": 6,
    "release_seconds": 20,
    "degraded_holds_last_state": True,
    # W-PRES: watcher-liveness OBSERVABILITY threshold (seconds). If the
    # watcher hasn't written a heartbeat in this long it is presumed
    # dead and a Guardian event fires. This NEVER mutates presence
    # state (the 59b57db hold-no-decay contract is untouched) — it is
    # NOT the rejected audit TTL that decayed UNKNOWN->ABSENT.
    "heartbeat_max_age_seconds": 90,
    # B7 identity match floor (anchor_store); latched via acquire_frames
    # when enrolled operator matches at >= this confidence.
    "recognition_threshold": 0.4,
    # When True, skip face/webcam latch: every reader and the HUD watcher
    # treat the operator as PRESENT (webcam enroll/recognize still runs for
    # name memory only). Use when local face ID is unreliable.
    "assume_operator_present": False,
}


def _operator_name_match(
    recognized_name: Optional[str],
    operator_name: Optional[str],
) -> bool:
    if operator_name is None or recognized_name is None:
        return False
    return str(recognized_name).casefold() == str(operator_name).casefold()


def presence_cfg() -> Dict[str, Any]:
    """Live presence config (merged over code defaults so a missing
    key degrades to the documented constant, never KeyError). This is
    the ONLY reader -> the config block is live, not phantom."""
    cfg = dict(_DEFAULTS)
    try:
        from config.loader import load_omega_config, load_subsystem_bundle

        bundle = load_subsystem_bundle("sensory")
        live = ((bundle.get("config") or {}).get("sensory") or {}).get("presence") or {}
        if not live:
            live = ((load_omega_config() or {}).get("omega") or {}).get("presence") or {}
        cfg.update({k: v for k, v in live.items() if k in _DEFAULTS})
    except Exception as exc:
        log.debug("operator_presence: cfg fell back to defaults (%s)", exc)
    return cfg


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Observation:
    recognized_name: Optional[str]
    confidence: float
    degraded: bool

    @classmethod
    def from_identify(cls, result: Dict[str, Any]) -> "Observation":
        """Adapt a loader.identify() dict. recognized_name/confidence/
        degraded are exactly the keys loader.identify() guarantees
        (incl. the 59b57db degraded sentinel)."""
        r = result or {}
        return cls(
            recognized_name=r.get("recognized_name"),
            confidence=float(r.get("confidence") or 0.0),
            degraded=bool(r.get("degraded")),
        )


class PresenceMachine:
    """Hysteresis presence latch. Pure: feed Observations + `now`."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        c = cfg or presence_cfg()
        self.acquire_threshold = float(c["acquire_threshold"])
        self.release_threshold = float(c["release_threshold"])
        self.acquire_frames = int(c["acquire_frames"])
        self.release_seconds = float(c["release_seconds"])
        self.degraded_holds = bool(c["degraded_holds_last_state"])
        self.recognition_threshold = float(c.get("recognition_threshold", 0.4))
        self.assume_operator_present = bool(c.get("assume_operator_present", False))

        self.state: str = ABSENT
        self._stable: str = ABSENT  # last non-UNKNOWN state
        self._acquire_count: int = 0
        self._sub_release_since: Optional[datetime] = None

    def _effective_acquire_confidence(
        self,
        obs: Observation,
        *,
        is_operator: bool,
    ) -> float:
        """Closed-world: confirmed operator identity counts toward latch."""
        if not is_operator:
            return obs.confidence
        if obs.confidence > self.recognition_threshold:
            return max(obs.confidence, self.acquire_threshold)
        return obs.confidence

    def observe(
        self,
        obs: Observation,
        *,
        operator_name: Optional[str],
        now: Optional[datetime] = None,
    ) -> Tuple[str, Optional[str]]:
        """Advance the machine. Returns (state, transition_event) where
        transition_event is PRESENCE_ENTER / PRESENCE_EXIT / None.
        Degraded transitions are NEVER ENTER/EXIT (hold semantics — a
        runtime blip must not spam social dispatch signals)."""
        now = now or _now()

        # ── Degraded: runtime-down is NOT operator-absent ────────────
        if obs.degraded and self.degraded_holds:
            if self.state != UNKNOWN:
                self._stable = self.state  # remember to resume
                self.state = UNKNOWN
            # Freeze: no decay, no acquire progress, no release timer
            # advance, no ENTER/EXIT. Hold until recognition recovers.
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "sensory",
                    "presence_state",
                    state=self.state,
                    confidence=obs.confidence,
                    degraded=True,
                    recognized_name=obs.recognized_name,
                )
            except Exception:
                pass
            return self.state, None

        # Effective "current" for hold/release logic: if we were in
        # UNKNOWN (degraded), resume reasoning from the held stable
        # state, then re-evaluate with this fresh observation.
        effective = self._stable if self.state == UNKNOWN else self.state

        is_operator = _operator_name_match(obs.recognized_name, operator_name)
        acq_conf = self._effective_acquire_confidence(obs, is_operator=is_operator)

        prev = self.state
        new_state = effective

        if is_operator and acq_conf >= self.acquire_threshold:
            # Acquiring (or sustaining well above the band).
            self._sub_release_since = None
            self._acquire_count += 1
            if self._acquire_count >= self.acquire_frames:
                new_state = PRESENT
            elif effective != PRESENT:
                # still acquiring -> not yet latched
                new_state = effective if effective == PRESENT else ABSENT
            else:
                new_state = PRESENT  # already latched, stays
        elif effective == PRESENT and is_operator and obs.confidence >= self.release_threshold:
            # Hysteresis band [release, acquire): hold PRESENT, no flap.
            self._acquire_count = 0
            self._sub_release_since = None
            new_state = PRESENT
        else:
            # No operator match OR confidence < release_threshold.
            self._acquire_count = 0
            if self._sub_release_since is None:
                self._sub_release_since = now
            if effective == PRESENT:
                held = (now - self._sub_release_since).total_seconds()
                new_state = ABSENT if held >= self.release_seconds else PRESENT
            else:
                new_state = ABSENT

        # Judge the transition against the HELD STABLE baseline, not
        # raw prev: recovering from a degraded UNKNOWN hold back to the
        # SAME stable state is a resume, not a transition (no spurious
        # re-ENTER). If the world genuinely changed across the outage
        # (held PRESENT, operator actually left -> recover to ABSENT)
        # the real transition still fires.
        baseline = self._stable if prev == UNKNOWN else prev
        self.state = new_state
        self._stable = new_state
        if new_state == PRESENT and baseline == ABSENT:
            transition = "PRESENCE_ENTER"
        elif new_state == ABSENT and baseline == PRESENT:
            transition = "PRESENCE_EXIT"
        else:
            transition = None
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "sensory",
                "presence_state",
                state=new_state,
                confidence=obs.confidence,
                degraded=obs.degraded,
                recognized_name=obs.recognized_name,
                transition=transition,
            )
        except Exception:
            pass
        if new_state == baseline:
            return new_state, None
        if transition == "PRESENCE_ENTER":
            return new_state, "PRESENCE_ENTER"
        if transition == "PRESENCE_EXIT":
            return new_state, "PRESENCE_EXIT"
        return new_state, None


# ── Enrolled-operator resolution (closed-world) ──────────────────────
def enrolled_operator_name() -> Optional[str]:
    """The single enrolled operator's name, or None if not enrolled.
    READS the existing anchor store only — never writes, never touches
    RECOGNITION_THRESHOLD (B7 untouched). No enrollment -> None ->
    presence can never latch (the spec's no-enroll-no-proactive rule)."""
    try:
        from runtime.face.anchor_store import get_anchor_store

        names = list(get_anchor_store().names() or [])
        return names[0] if names else None
    except Exception as exc:
        log.debug(
            "operator_presence: enrolled operator lookup failed (%s) — presence cannot latch", exc
        )
        return None


# ── Guardian audit (metadata-only — observability discipline) ────────
def _emit_presence_event(event: str, *, summary: str = "", **meta: Any) -> None:
    try:
        from guardian.terminal_feedback import emit_phase

        emit_phase(
            "operator_presence",
            event,
            status="ok",
            summary=summary[:160],
            component="sensory.operator_presence",
            **{k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))},
        )
    except Exception as exc:
        log.debug("operator_presence: audit emit skipped (%s)", exc)


# ── W-PRES: watcher-liveness OBSERVABILITY (NOT a state mutation) ────
# The comprehensive-review fix (stale UNKNOWN -> ABSENT TTL) VIOLATES
# the 59b57db contract: degraded/UNKNOWN must HOLD without decay; a
# runtime blip must never flip presence. W-PRES does the only
# contract-respecting thing — expose a PURE staleness PREDICATE so a
# consumer (the HUD presence endpoint, hud/server.py) can tell the
# operator/system the watcher died, while this surface mutates NO
# presence state. There is deliberately NO PresenceMachine reference,
# NO ABSENT, NO state write below. The consumer owns the observability
# emit; this module only answers "is the heartbeat stale?".
def presence_is_stale(
    last_heartbeat_iso: Optional[str],
    *,
    now: Optional[datetime] = None,
    max_age_seconds: Optional[float] = None,
) -> bool:
    """Pure. True iff the watcher heartbeat is older than the
    threshold OR absent/unparseable (watcher never started / died).
    A True result is an OBSERVABILITY fact ONLY — it does NOT make the
    operator absent and MUST NOT be used to mutate presence state."""
    if max_age_seconds is None:
        max_age_seconds = float(presence_cfg()["heartbeat_max_age_seconds"])
    now = now or _now()
    if not last_heartbeat_iso:
        return True  # never written -> watcher dead / not started
    try:
        hb = datetime.fromisoformat(str(last_heartbeat_iso))
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True  # unparseable -> stale (loud, never silent)
    return (now - hb).total_seconds() > float(max_age_seconds)


# ── Presence Watcher (CODE ONLY — operator runs it; not Claude) ──────
def run_watcher(
    stop_predicate: Callable[[], bool],
    *,
    frame_source: Callable[[], Any],
    identify_fn: Optional[Callable[[Any], Dict[str, Any]]] = None,
    operator_name_fn: Callable[[], Optional[str]] = enrolled_operator_name,
    interval_seconds: float = 1.0,
    machine: Optional[PresenceMachine] = None,
    now_fn: Callable[[], datetime] = _now,
    sleep_fn: Optional[Callable[[float], None]] = None,
    emit: bool = True,
    shared_state: Any = None,
) -> PresenceMachine:
    """The presence loop (spec: while True: read -> detect -> latch/
    decay), made production-safe: bounded by `stop_predicate` (no bare
    while True, mirroring orchestrator.run_autonomy_forever), fully
    dependency-injected (real webcam in production; synthetic frames in
    tests), fail-CLOSED (any error -> treat as degraded -> HOLD, never
    spuriously latch PRESENT and speak). All deps default to the live
    wiring; QUARANTINE: Claude integrates this, the operator runs it.
    """
    if identify_fn is None:
        from runtime.face.recognition import identify as identify_fn
    if sleep_fn is None:
        import time as _t

        sleep_fn = _t.sleep
    m = machine or PresenceMachine()
    try:
        from core.shared_state_keys import PRESENCE_STATE
    except Exception:
        PRESENCE_STATE = "presence_state"  # noqa: F811 — import fallback

    # Brain-local face readiness gates each tick. On sustained DOWN
    # the loop must NOT flood [PHASE] / log with per-tick degraded
    # emissions — the Phase C logging discipline ("steady-state silence
    # is signal"; epistemic_emptiness P2-S19 cure pattern) applies here
    # too. Track the last broker state; emit only on TRANSITIONS
    # (ok→hold, hold→hold-with-different-reason, hold→ok).
    _last_broker_state: Optional[str] = None
    _last_broker_reason: Optional[str] = None
    try:
        from runtime.face.recognition import face_status

        _face_status = face_status
        _broker_available = True
    except Exception as exc:
        log.debug("operator_presence: local face import failed (%s); running ungated", exc)
        _broker_available = False
        _face_status = None

    # The machine owns the effective configuration for this watcher.  Reading
    # the global config again here ignored an injected machine configuration,
    # which could silently skip the face broker and hide its failures.
    assume_present = m.assume_operator_present

    try:
        from guardian.terminal_feedback import emit_phase

        emit_phase(
            "sensory",
            "presence_watcher",
            status="enter",
            summary="presence watcher loop started",
        )
    except Exception:
        pass

    while not stop_predicate():
        if assume_present:
            if shared_state is not None:
                try:
                    shared_state.write(PRESENCE_STATE, PRESENT)
                except Exception as exc:
                    log.debug("operator_presence: assume_present write skipped (%s)", exc)
                try:
                    from core.shared_state_keys import (
                        PRESENCE_WATCHER_HEARTBEAT,
                    )

                    shared_state.write(
                        PRESENCE_WATCHER_HEARTBEAT,
                        now_fn().isoformat(),
                    )
                except Exception as exc:
                    log.debug("operator_presence: heartbeat write skipped (%s)", exc)
            sleep_fn(interval_seconds)
            continue

        # FACE readiness — only skip identify when the local CPU models are unavailable.
        # Models lazy-load inside identify(); degraded (weights unloaded)
        # must not block the webcam path.
        broker_hold = False
        if _broker_available:
            try:
                status = _face_status() if _face_status is not None else {}
                # Transition-only emission (P2-S19 refractory pattern).
                cur_state = "up" if status.get("face_ready") else "down"
                cur_reason = str(status.get("reason") or "")
                if cur_state != _last_broker_state or cur_reason != _last_broker_reason:
                    log.info(
                        "operator_presence: FACE broker state transition: %s/%s → %s/%s",
                        _last_broker_state or "(initial)",
                        _last_broker_reason or "(initial)",
                        cur_state,
                        cur_reason,
                    )
                    _last_broker_state = cur_state
                    _last_broker_reason = cur_reason
                broker_hold = cur_state == "down"
            except Exception as exc:
                # Fail-CLOSED: any error in the broker path is treated
                # as broker hold. The watcher's docstring (line 308)
                # mandates "fail-CLOSED (any error) -> treat as
                # degraded -> HOLD, never spuriously latch PRESENT and
                # speak". A transient broker exception must NOT bypass
                # the gate — otherwise infra hiccups silently unlock
                # proactive speech. Differs from the
                # brain/_model_client escape-hatch posture because the
                # watcher's own doctrine is stricter (it's the source
                # of truth for presence-gated speech).
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "sensory",
                    "presence_broker_failed",
                    status="degraded",
                    ok=False,
                    error=str(exc),
                )
                log.debug(
                    "operator_presence: local face readiness raised "
                    "(%s); fail-CLOSED - treating as broker hold",
                    exc,
                )
                broker_hold = True
        if broker_hold:
            # Fail-closed: degraded observation, no identify call.
            obs = Observation(recognized_name=None, confidence=0.0, degraded=True)
        else:
            try:
                frame = frame_source()
                result = identify_fn(frame)
                obs = Observation.from_identify(result)
            except Exception as exc:
                # Fail-closed: an error is treated as DEGRADED (hold last
                # state) — never as a confident operator match. A broken
                # camera/runtime must NOT unlock proactive speech.
                log.debug(
                    "operator_presence watcher: observe failed (%s) — treating as degraded/hold",
                    exc,
                )
                obs = Observation(recognized_name=None, confidence=0.0, degraded=True)
        state, event = m.observe(obs, operator_name=operator_name_fn(), now=now_fn())
        if shared_state is not None:
            try:
                shared_state.write(PRESENCE_STATE, state)
            except Exception as exc:
                log.debug("operator_presence: shared_state write skipped (%s)", exc)
            # P3-S04: write a heartbeat side-channel so the HUD's
            # /api/presence/state endpoint can surface watcher-liveness
            # to the operator (stale watcher == infra problem the
            # operator should see; the state-write above doesn't
            # distinguish "watcher healthy + holding state" from
            # "watcher dead + state frozen at last value").
            # Best-effort; failure here must not affect the gate.
            # P3-S04 review: use the canonical key constant from
            # shared_state_keys.py rather than a string literal (RB#2
            # of P2-S17 — no unclassified key reaches SharedState).
            try:
                from core.shared_state_keys import (
                    PRESENCE_WATCHER_HEARTBEAT,
                )

                heartbeat_iso = now_fn().isoformat()
                shared_state.write(
                    PRESENCE_WATCHER_HEARTBEAT,
                    heartbeat_iso,
                )
            except Exception as exc:
                log.debug("operator_presence: heartbeat write skipped (%s)", exc)
        if event and emit:
            _emit_presence_event(event, summary=f"state={state}", state=state)
        sleep_fn(interval_seconds)
    return m


def read_presence_from_shared_state(shared_state) -> str:
    """W1 reader: default UNKNOWN when unwired (fail-closed).

    When ``omega.presence.assume_operator_present`` is true, always
    returns PRESENT so cognition is not blocked by a broken webcam path.
    """
    if presence_cfg().get("assume_operator_present"):
        return PRESENT
    if shared_state is None:
        return UNKNOWN
    try:
        from core.shared_state_keys import PRESENCE_STATE

        raw = shared_state.read(PRESENCE_STATE)
        if raw in (PRESENT, ABSENT, UNKNOWN):
            return raw
    except Exception as exc:
        log.debug("operator_presence: shared_state read failed (%s)", exc)
    return UNKNOWN
