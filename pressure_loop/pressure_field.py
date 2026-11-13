"""Pressure field — S3 scaffold.

Aggregates pressure contributions from multiple sources into a single
`PressureDecision`. CueDesk consumes the decision to decide whether to
open a CognitiveEpisode this tick.

This is the structural alternative to the timer-as-conductor pattern:
instead of meta/global running every N seconds whether warranted or
not, the cadence/timer FEEDS pressure here; when aggregate >= threshold
AND quarantine/presence guards pass, an episode opens and the wired
movement chain (S2: META_A -> AUTO -> META_B) runs once for that
episode. See PACKET_S3.md §2 for the production-truth vs target
diagram.

S3 scope (this packet):
  - Built-in contributors (curiosity_gap, operator_message) wired
    against the existing canonical SS key LAST_CURIOSITY_CYCLE and
    the durable operator queue.
  - Stub contributors (operator_intent, neuromod_spike, guardian)
    return 0.0 with the contributor slot present so the registry shape
    is testable. Live wiring deferred to S6 (operator_intent) and
    future slices.
  - `evaluate_pressure()` returns a PressureDecision with aggregate,
    per-contributor breakdown, presence/autonomy-pause-respecting
    open_new_episode bool, and a human-readable suppress_reason.

NOT in scope (deferred to S4+/S6):
  - The pressure loop lives in pressure_loop/loop.py (this module is just the field).
  - Movement runners (S2 + future slices).
  - operator_intent live wire (S6).
  - Per-contributor weights from yaml (S3 ships uniform weights; S4+
    can extend to cue-sheet-driven weights).

Doctrine link (project_sharedstate_five_incompatible_roles): pressure
is the convergent signal that REPLACES SharedState's documented-but-
impossible role of "session conversation buffer telling meta what to
do." Pressure is read at the conductor, not threaded through the
cascade bus; the episode it opens is the same-cycle truth surface that
survives the epoch boundary the bus can't cross.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.shared_state import SharedState
from core.shared_state_keys import LAST_CURIOSITY_CYCLE
from pressure_loop.symphony_signals import SymphonySignal

log = logging.getLogger("omega.core.pressure_field")


# Contributor IDs — mirror the cue sheet pressure.contributors block.
# Adding a new contributor here REQUIRES adding the matching reader
# below + (when applicable) extending the cue sheet `pressure:` block.
CONTRIBUTOR_CURIOSITY_GAP = "curiosity_gap"
CONTRIBUTOR_OPERATOR_INTENT = "operator_intent"
CONTRIBUTOR_NEUROMOD_SPIKE = "neuromod_spike"
CONTRIBUTOR_GUARDIAN = "guardian"
# The `operator_message` contributor represents the durable operator queue:
# a waiting turn is pressure, and an empty queue is not. The queue is the
# sole ingress gate; no SharedState mirror is consulted.
CONTRIBUTOR_OPERATOR_MESSAGE = "operator_message"
# Phase 2 §P2-S8: endogenous-pressure contributor closing the
# drive-precedes-goals cure law (see
# `[[project-pressure-from-emptiness-drive-precedes-goals]]` and
# FINAL_OMEGA_DOCTRINE.md). A brand-new no-knowledge substrate should
# experience emptiness AS pressure — the want-to-understand is the
# engine, not an implanted goal. The reader is read-only over
# StrengthIndex record count; pressure decays naturally as the
# substrate writes verified memory through DeepSleep archive
# (existing keep_substrate path), so no contributor-side writes are
# needed and no flood-prevention logic is added here. CueDesk's
# normal open-threshold + tick-rate logic governs episode frequency.
CONTRIBUTOR_EPISTEMIC_EMPTINESS = "epistemic_emptiness"
# Phase 2 §P2-S10: durable ASK/HOLD evidence terminal contributor.
# Reads `hud.curiosity_store` for entries whose recommended_path is
# `ask_user` (ASK terminals — operator question pending) or `defer`,
# plus any entry with status == "deferred" (HOLD terminals — blocked
# evidence work). Bridges S12's durable terminals into the pressure
# field so the product path picks them up as action/hold-meaningful
# pressure rather than letting them sit on disk invisible to the
# shared moment. See PACKET_P2_S10.md §3 #1: "Durable ASK/HOLD
# pressure is explicit."
CONTRIBUTOR_EVIDENCE_TERMINAL = "evidence_terminal"
# Phase 2 §P2-S15: broader idle probe aggregator. Reads
# `pressure_loop.symphony_probes.run_idle_probes()` and sums the per-probe
# weights into a single contributor scalar. Per-probe detail
# (probe_classes_seen list + has_meaningful_class flag) flows into
# `PressureDecision.contributor_details["idle_probe_aggregate"]` so
# Global action selection can branch on probe class without
# rescanning the substrate. The aggregator itself is read-only;
# probe-level refractory caches live in `pressure_loop.symphony_probes`
# (per-probe in-process dicts) and `core.pressure_field`'s
# `_GUARDIAN_INTERVENTION_HANDLED` (restart-safe via WAL).
CONTRIBUTOR_IDLE_PROBE_AGGREGATE = "idle_probe_aggregate"
# 2026-05-26 — operator-directed mastery-pressure drive. Reads the
# learning_state envelope (Phase 2 §P2-S13 schema-faithful: mastery
# section + learning_debt_flags) and produces pressure proportional
# to "learning want" — coverage gap + freshness gap + recall
# confidence gap. Low/stale/empty mastery → high learning_debt →
# more episodes open to explore + relearn; high coverage + fresh →
# decays toward a baseline floor so curiosity NEVER fully dies.
# This is the missing wire from symphony_learning_state.py:27-30
# ("turning learning_debt into a pressure source belongs to a later
# operator-directed slice"). Now landed.
CONTRIBUTOR_LEARNING_DEBT = "learning_debt"
# 2026-06-01 Slice 2: tool-creation-need contributor. Reads the curiosity
# store for OPEN entries whose recommended_path == "tool_creation" (Omega's
# phase_3 surfaced a tool need). Drives MINT_TOOL in the choice aggregator,
# which routes to the AUTO mint_tool handler. Fixed weight above the open
# threshold so a tool need alone can ignite one episode (mirrors
# evidence_terminal). Read-only; never raises.
CONTRIBUTOR_TOOL_CREATION_NEED = "tool_creation_need"
# 2026-06-15 (cognition+CLI merge, Phase 5a): execution-pressure contributor.
# Fires when a domain is LEARNED (knowledge_mastery >= target) but NOT yet
# APPLIED (application_mastery < target) AND the memory substrate is warm enough
# to practice. Routes to the APPLY episode (qwen executor). DISJOINT from
# learning_debt (knowledge < target) — the two never count the same domain, so
# application pressure is EARNED-by-doing, never a re-count of a learning gap.
# ARMED (EXECUTION_GAP_ENABLED=True, Phase 5x complete) — autonomous APPLY
# pressure is live; the staged-approval gate (apply_lock + pre-APPLY checkpoint +
# hard deadline) downstream is what keeps an opened APPLY from running unguarded.
CONTRIBUTOR_EXECUTION_GAP = "execution_gap"

ALL_CONTRIBUTORS: tuple[str, ...] = (
    CONTRIBUTOR_CURIOSITY_GAP,
    CONTRIBUTOR_OPERATOR_INTENT,
    CONTRIBUTOR_NEUROMOD_SPIKE,
    CONTRIBUTOR_GUARDIAN,
    CONTRIBUTOR_OPERATOR_MESSAGE,
    CONTRIBUTOR_EPISTEMIC_EMPTINESS,
    CONTRIBUTOR_EVIDENCE_TERMINAL,
    CONTRIBUTOR_IDLE_PROBE_AGGREGATE,
    CONTRIBUTOR_LEARNING_DEBT,
    CONTRIBUTOR_TOOL_CREATION_NEED,
    CONTRIBUTOR_EXECUTION_GAP,
)


# V2 live-boot audit Fix 2: refractory-pattern emit cache for pressure
# contributors (per P2-S19 discipline). Maps contributor name -> last-
# emitted weight. emit fires only on:
#   - first read where weight > 0 (0->non-zero crossing)
#   - weight crosses the zero boundary in either direction
#   - weight differs from last-emitted by > epsilon
# This avoids flooding feedback_audit.jsonl on steady-state ticks while
# surfacing real pressure changes for the operator running live boot.
_CONTRIBUTOR_EMIT_CACHE: Dict[str, float] = {}
_CONTRIBUTOR_EMIT_EPSILON: float = 0.05
# Code-review followup (2026-05-26): contributor reads may be invoked
# off the main boot thread (autonomic publishers, response paths). The
# read-decide-write across cache lookup + comparison + assignment is
# not atomic — two concurrent calls for the same contributor can both
# observe prev=None and double-emit the first-fire. Same lock pattern
# as pressure_loop/pressure_loop_pause.py.
_CONTRIBUTOR_EMIT_LOCK = threading.Lock()


def _reset_contributor_emit_cache_for_tests() -> None:
    """Test-only reset; not invoked from production code."""
    with _CONTRIBUTOR_EMIT_LOCK:
        _CONTRIBUTOR_EMIT_CACHE.clear()


def _emit_contributor_signal(
    name: str,
    weight: float,
    *,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Failure-soft refractory emit for a pressure contributor weight.

    Emits emit_signal('pressure.<name>.weight', weight, ...) only when:
      - first time this contributor is read (cache empty for name) AND weight > 0
      - weight crosses the zero boundary (0 -> non-zero OR non-zero -> 0)
      - weight differs from last-emitted by > _CONTRIBUTOR_EMIT_EPSILON
    Otherwise no-op. Matches the P2-S19 _emit_emptiness_state discipline.

    read_epistemic_emptiness is excluded — it has its own richer
    _emit_emptiness_state transition emit. All other 8 contributors
    route through this helper.
    """
    # Critical section: read-decide-write under lock; emit OUTSIDE lock
    # so the (possibly slow) import + I/O does not block other threads.
    with _CONTRIBUTOR_EMIT_LOCK:
        prev = _CONTRIBUTOR_EMIT_CACHE.get(name)
        crossed_zero = (
            (prev is None and weight > 0)  # first read, non-zero
            or (prev is not None and prev == 0.0 and weight > 0)  # 0 -> non-zero
            or (prev is not None and prev > 0.0 and weight == 0.0)  # non-zero -> 0
        )
        delta_large = (
            prev is not None
            and abs(weight - prev) > _CONTRIBUTOR_EMIT_EPSILON
            and not crossed_zero  # avoid double-emit
        )
        if not (crossed_zero or delta_large):
            # Steady state or first-read-at-zero — no emit needed.
            if prev is None:
                # Still record the zero baseline so a later non-zero crossing works.
                _CONTRIBUTOR_EMIT_CACHE[name] = weight
            return
        _CONTRIBUTOR_EMIT_CACHE[name] = weight
    log.debug(
        "pressure.contributor %s weight=%.4f %s",
        name,
        float(weight),
        details or {},
    )
    try:
        from guardian.pressure_observability import log_contributor_field

        log_contributor_field(name, float(weight), details)
    except Exception:
        pass


# Phase 2 §P2-S10: in-process handled-cache for durable evidence
# terminals. Bounded LRU. The response-hold outlet calls
# ``mark_evidence_terminal_handled(curiosity_id)`` after a successful
# targeted ask/report dispatch so the same record cannot be re-picked
# on the next tick; the contributor reader excludes any curiosity_id
# in this cache. PACKET §3 #7 ("No repeated same-record flood")
# explicitly allows "a deterministic in-process refractory" for this
# slice. Cache survives process restart — if the substrate boots
# fresh, S12 entries that are still durable will re-appear, which is
# the correct behavior; flood prevention is per-process-lifetime.
_EVIDENCE_TERMINAL_HANDLED: "OrderedDict[str, str]" = OrderedDict()
_EVIDENCE_TERMINAL_HANDLED_CAP: int = 128

# Phase 2 §P2-S10: fixed weight when at least one selectable ASK or
# HOLD terminal exists. Above the cue-sheet open threshold (0.55) so
# a durable terminal alone is sufficient to open one episode through
# CueDesk's normal pressure gate — empty-state pressure not required.
# Below 1.0 so a coincident guardian (1.0) still wins on raw weight.
_EVIDENCE_TERMINAL_WEIGHT: float = 0.6

# 2026-06-01 Slice 2: fixed weight when >=1 open tool_creation curiosity
# exists. 0.6 matches evidence_terminal -- above the cue-sheet open threshold
# (0.55) so a tool need alone can open one episode, below guardian's 1.0.
_TOOL_CREATION_NEED_WEIGHT: float = 0.6
# Side-channel detail for evaluate_pressure enrichment (mirrors the
# _LAST_IDLE_PROBE_FINDINGS pattern). Populated each read; surfaced under
# contributor_details["tool_creation_need"] so META can build the mint order
# (curiosity_id + capability_gap) without rescanning the store.
_LAST_TOOL_CREATION_NEED_DETAIL: Dict[str, Any] = {}


def mark_evidence_terminal_handled(curiosity_id: str) -> None:
    """Outlet-side hook: after the response-hold outlet successfully
    dispatches a targeted ask/report for a durable terminal, it calls
    this so the same curiosity_id stops contributing pressure until
    the LRU cache evicts. Keeps PACKET §3 #7 (no repeated same-record
    flood) honest without adding a second conductor / sweeper.
    """
    if not curiosity_id:
        return
    _EVIDENCE_TERMINAL_HANDLED[curiosity_id] = _now_iso_evidence()
    _EVIDENCE_TERMINAL_HANDLED.move_to_end(curiosity_id)
    while len(_EVIDENCE_TERMINAL_HANDLED) > _EVIDENCE_TERMINAL_HANDLED_CAP:
        _EVIDENCE_TERMINAL_HANDLED.popitem(last=False)


def _reset_evidence_terminal_handled() -> None:
    """Reset the in-process handled cache. Production callers do not
    need this; test helper for audits that drive multiple independent
    scenarios in a single process.
    """
    _EVIDENCE_TERMINAL_HANDLED.clear()


# Phase 2 §P2-S15: Guardian intervention same-event handled cache.
# Per PACKET_P2_S15.md RB#3: "Repeated identical Guardian intervention
# IDs must not reopen work forever once handled." Mirrors the
# evidence_terminal LRU handled-cache pattern (P2-S10).
_GUARDIAN_INTERVENTION_HANDLED: "OrderedDict[str, str]" = OrderedDict()
_GUARDIAN_INTERVENTION_HANDLED_CAP: int = 256


def mark_guardian_intervention_handled(intervention_id: str) -> None:
    """Outlet-side hook: after the route consumer (response_hold /
    omega_prelude / executive) handles a Guardian intervention, it
    calls this so the same `intervention_id` stops contributing
    pressure (via read_guardian's handled-cache skip) and stops
    refiring the guardian_intervention probe.

    Mirrors `mark_evidence_terminal_handled`. The LRU cap evicts
    oldest entries after `_GUARDIAN_INTERVENTION_HANDLED_CAP` distinct
    ids; the 600s pressure-window helper in `_recent_jsonl_entries`
    means natural age-out continues to age out entries even when the
    handled cache is forgotten via LRU eviction.
    """
    if not intervention_id:
        return
    _GUARDIAN_INTERVENTION_HANDLED[intervention_id] = _now_iso_evidence()
    _GUARDIAN_INTERVENTION_HANDLED.move_to_end(intervention_id)
    while len(_GUARDIAN_INTERVENTION_HANDLED) > _GUARDIAN_INTERVENTION_HANDLED_CAP:
        _GUARDIAN_INTERVENTION_HANDLED.popitem(last=False)


def _reset_guardian_intervention_handled() -> None:
    """Reset the in-process Guardian handled cache. Test helper for
    audits driving multiple scenarios in a single process; production
    code never resets — the cache is the restart-safe flood guard."""
    _GUARDIAN_INTERVENTION_HANDLED.clear()


def _now_iso_evidence() -> str:
    """Handled-cache entry timestamp helper.

    Phase 2 §P2-S20: route through the temporal authority so handled-
    cache entries pin under fake time alongside the other cognitive
    surfaces. Pre-S20 this used a direct `datetime.now(timezone.utc).
    isoformat()` to avoid a circular import; the authority module
    has no such cycle so the direct call is no longer necessary.
    """
    from core.symphony_time import now_iso

    return now_iso()


# Phase 2 §P2-S8: epistemic-emptiness baseline. Below this record
# count the contributor returns positive pressure (the substrate has
# few enough records that "want to understand" is a real signal);
# at or above this count, the contributor returns 0.0 (the substrate
# has enough memory to draw from, so emptiness is no longer driving).
# Conservative baseline so a no-knowledge boot produces meaningful
# pressure without immediately saturating the field — episode-open
# decision remains aggregate + CueDesk's responsibility, never one
# contributor's alone.
_EPISTEMIC_EMPTINESS_FULL_BASELINE: int = 100


# Phase 2 §P2-S8 refractory: prevent flood when substrate state stays
# unchanged across consecutive ticks. The contributor caches the
# previously-observed record count; after the refractory threshold of
# unchanged-state ticks, the returned pressure decays exponentially.
# This is the contributor-side circuit breaker per the operator BLOCK
# (2026-05-24): "duplicate idle ticks must not flood repeated executive
# movement." Once an episode fires and writes verified memory, the
# record count changes and the cache resets — the natural decay path
# resumes. The cache is in-process module state (single-threaded per
# tick, mirrors StrengthIndex's own singleton pattern); no SharedState
# write, no goals.jsonl, no memory write from the contributor.
#
# Phase 2 §P2-S19 (2026-05-25): the exponential decay alone protected
# against rapid flood but did not give a structural guarantee of "at
# most one observe per refractory window". The cure adds an explicit
# event/refractory semantic on top of the decay:
#   - `fired_in_window` (bool) records whether an opening weight has
#     already been emitted in the current refractory window. After a
#     fire, subsequent unchanged ticks return 0.0 (hard non-opening)
#     until the window expires OR substrate state changes.
#   - `_EPISTEMIC_EMPTINESS_REFRACTORY_WINDOW` (int) is the window length
#     in consecutive-unchanged ticks. Default: 20 (matches the boot-
#     cascade audit at PACKET_P2_S19 §Boot-Cascade Scenario).
# Substrate change (record_count != last) ALWAYS resets the window.
_EPISTEMIC_EMPTINESS_CACHE: Dict[str, Any] = {
    "last_record_count": None,  # int or None
    "consecutive_unchanged": 0,  # int (ticks since last state change)
    "fired_in_window": False,  # P2-S19: opening weight already emitted in current refractory window
}
_EPISTEMIC_EMPTINESS_REFRACTORY_THRESHOLD: int = 1
_EPISTEMIC_EMPTINESS_REFRACTORY_WINDOW: int = 20  # P2-S19 (packet §Boot-Cascade)


# ── Building-pressure drive (operator doctrine 2026-05-30) ───────────────
# Pressure is a DRIVE that INTEGRATES (builds) every tick with NO decay.
# evaluate_pressure adds THIS tick's contributor sum onto the carried-forward
# total; it resets to 0.0 ONLY when an ignited episode DIES (EpisodeStore.
# close() — "the episode starts at GLOBAL, ends when OMEGA records it"). It is
# NEVER reset per-tick and there is NO decay — that was the bug: `aggregate`
# was recomputed = sum(this-tick contributors) each tick, so it pinned at the
# idle sum (~0.15), never built to open_threshold, never ignited, never
# learned. The pressure loop never stops ticking; this value just carries.
# Resets to 0.0 only on episode close (EpisodeStore.close()).
_PRESSURE_ACCUMULATOR: Dict[str, float] = {"accumulated": 0.0}


def reset_pressure_accumulator() -> None:
    """Zero the building-pressure drive. Called from EpisodeStore.close() when
    an ignited episode reaches lifecycle completion (its death), so the next
    idle window builds fresh from 0.0. This is the ONLY write-to-zero — no
    decay, no per-tick reset (any other reset path reintroduces the
    pinned-at-0.15-never-ignites bug)."""
    _PRESSURE_ACCUMULATOR["accumulated"] = 0.0


# ── Neuromod tonic (advisory body state; decays on idle, refreshes on close) ─
_NEUROMOD_TONIC: Dict[str, float] = {
    "dopamine": 0.0,
    "serotonin": 0.0,
    "stress": 0.0,
}
_NEUROMOD_TONIC_LOCK = threading.Lock()
_TONIC_KEYS = ("dopamine", "serotonin", "stress")


def _safe_tonic_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:
        return default
    return parsed


def reset_neuromod_tonic() -> None:
    """Zero neuromod tonic state (test harness)."""
    with _NEUROMOD_TONIC_LOCK:
        for key in _TONIC_KEYS:
            _NEUROMOD_TONIC[key] = 0.0


def export_pressure_field_state() -> Dict[str, Any]:
    """JSON-safe pressure-field substrate for shutdown snapshot."""
    return {
        "accumulator": float(_PRESSURE_ACCUMULATOR.get("accumulated", 0.0) or 0.0),
        "neuromod_tonic": read_neuromod_tonic(),
    }


def import_pressure_field_state(state: Any) -> None:
    """Restore pressure-field substrate from a shutdown snapshot."""
    if not isinstance(state, dict):
        return
    try:
        acc = float(state.get("accumulator", 0.0) or 0.0)
        _PRESSURE_ACCUMULATOR["accumulated"] = max(0.0, min(1.0, acc))
    except (TypeError, ValueError):
        pass
    tonic = state.get("neuromod_tonic")
    if isinstance(tonic, dict):
        refresh_neuromod_tonic(tonic)


def read_neuromod_tonic() -> Dict[str, float]:
    """Return current neuromod tonic (dopamine/serotonin/stress). Never raises."""
    try:
        with _NEUROMOD_TONIC_LOCK:
            return {k: _safe_tonic_float(_NEUROMOD_TONIC.get(k, 0.0)) for k in _TONIC_KEYS}
    except Exception:
        return {"dopamine": 0.0, "serotonin": 0.0, "stress": 0.0}


def refresh_neuromod_tonic(neuromod: Any) -> None:
    """Replace tonic from closed-episode neuromod dict. Missing keys failure-soft."""
    before = read_neuromod_tonic()
    if not isinstance(neuromod, dict):
        return
    try:
        with _NEUROMOD_TONIC_LOCK:
            for key in _TONIC_KEYS:
                _NEUROMOD_TONIC[key] = _safe_tonic_float(neuromod.get(key))
    except Exception:
        pass
    try:
        from guardian.pressure_observability import log_neuromod_tonic_transition

        log_neuromod_tonic_transition(
            "episode_close_refresh",
            before,
            read_neuromod_tonic(),
            episode_neuromod=neuromod,
        )
    except Exception:
        pass


def decay_neuromod_tonic() -> None:
    """Decay tonic toward baseline on idle ticks. Never raises."""
    before = read_neuromod_tonic()
    try:
        from pressure_loop.policy import pressure_neuromod_tonic_decay

        cfg = pressure_neuromod_tonic_decay()
        if not cfg.get("enabled", True):
            return
        rate = _safe_tonic_float(cfg.get("decay_rate", 0.25), 0.25)
        rate = max(0.0, min(1.0, rate))
        baseline = _safe_tonic_float(cfg.get("baseline", 0.0))
        factor = 1.0 - rate
        snap_eps = 1e-4
        with _NEUROMOD_TONIC_LOCK:
            for key in _TONIC_KEYS:
                val = _safe_tonic_float(_NEUROMOD_TONIC.get(key, 0.0)) * factor
                if abs(val - baseline) < snap_eps:
                    val = baseline
                _NEUROMOD_TONIC[key] = val
    except Exception:
        pass
    try:
        from guardian.pressure_observability import log_neuromod_tonic_transition

        log_neuromod_tonic_transition(
            "idle_tick_decay",
            before,
            read_neuromod_tonic(),
        )
    except Exception:
        pass


# Phase 2 §P2-S19: side-channel detail for the evaluate_pressure
# detail-enrichment pass. Mirrors the _LAST_IDLE_PROBE_FINDINGS pattern
# from P2-S15. Populated each call to read_epistemic_emptiness; read
# (read-only) by evaluate_pressure to expose
# contributor_details["epistemic_emptiness"]. The detail surface lets
# downstream consumers (Global selector, archive snapshots, WAL) see
# WHY the contributor returned what it did this tick — "idle/no new
# evidence" vs "fresh_or_changed" vs "refractory_window_expired" vs
# "above_baseline" — without recomputing the cache themselves.
_LAST_EPISTEMIC_EMPTINESS_DETAIL: Dict[str, Any] = {}


def _reset_epistemic_emptiness_cache() -> None:
    """Reset the in-process refractory cache. Production callers do
    not need this — the cache self-resets when StrengthIndex becomes
    unwired (e.g., process restart). Test helper for audits that
    drive multiple independent scenarios in a single process.

    Phase 2 §P2-S19: also resets `fired_in_window` and the side-
    channel detail surface so a fresh scenario starts from clean
    refractory state.
    """
    _EPISTEMIC_EMPTINESS_CACHE["last_record_count"] = None
    _EPISTEMIC_EMPTINESS_CACHE["consecutive_unchanged"] = 0
    _EPISTEMIC_EMPTINESS_CACHE["fired_in_window"] = False
    _LAST_EPISTEMIC_EMPTINESS_DETAIL.clear()


# Reader signature: takes the live SharedState, returns a scalar in
# [0.0, 1.0]. Readers MUST be pure (no SS writes, no side effects)
# and NEVER raise -- always return 0.0 on degraded/missing data.
ContributorReader = Callable[[SharedState], float]


def _clamp_unit(x: Any) -> float:
    """Coerce to [0.0, 1.0], silently mapping NaN/None/garbage to 0.0.
    Contributor readers route through this so a misbehaving reader can
    never poison the aggregate."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN check (NaN != NaN by IEEE)
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


# ---------------------------------------------------------------------
# Built-in contributor readers
# ---------------------------------------------------------------------


def read_curiosity_gap(shared_state: SharedState) -> float:
    """Curiosity pressure scalar -- symphony S7 LIVE from store.

    Symphony S7 (PACKET_S7.md §4.3): curiosity pressure is now
    EPISODE-BOUND -- the substrate reads the curiosity_store live
    on every CueDesk tick instead of waiting for the legacy
    curiosity boot epoch to write LAST_CURIOSITY_CYCLE. That decoupling
    is the cure for the 'pressure stale until boot epoch runs' bug
    (cue sheet acceptance: 'seeded open gap → non-zero pressure
    WITHOUT make_curiosity_runner').

    Aggregate formula (documented in handoff):
        max(
          max(entry.tension_score for entry in curiosity_store.list_open()),
          LAST_CURIOSITY_CYCLE.pressure scalar,  # legacy projection fallback
        )

    Store-side: list_open() returns open + active entries (in-flight
    curiosities). Their `tension_score` is the deterministic
    pressure scalar per docs/contracts/04_scoring_contract.md. Max
    over the set surfaces the most-urgent open thread without
    requiring per-cycle aggregation by the legacy epoch.

    SS-side: legacy LAST_CURIOSITY_CYCLE.pressure remains a valid
    contributor for backward compatibility (the legacy boot epoch
    still writes it). The two sources are unioned via max() so:
      * Store-only (symphony post-S12): store wins, SS often empty.
      * SS-only (legacy boot still running): SS wins, store empty.
      * Both populated (transition period): max wins, neither blocks.

    Returns 0.0 on store/SS error, missing data, or genuinely empty
    state -- 'silence is data, not pressure' (S3 contract preserved)."""
    # Store-side aggregate (S7 LIVE). Phase 2 §P2-S10: ASK and HOLD
    # terminals route via the evidence_terminal contributor, not via
    # generic curiosity_gap. PACKET §3 #3: "An ASK terminal must not
    # be routed as plain `curiosity_gap -> GLOBAL_PRELUDE` when it is
    # the selected meaningful durable terminal." Exclude entries that
    # match _is_ask_terminal / _is_hold_terminal so they cannot
    # double-count toward generic curiosity pressure.
    store_max = 0.0
    try:
        from hud import curiosity_store

        open_entries = curiosity_store.list_open() or []
        non_terminal_entries = [
            e for e in open_entries if not _is_ask_terminal(e) and not _is_hold_terminal(e)
        ]
        if non_terminal_entries:
            store_max = max(
                _clamp_unit(getattr(e, "tension_score", 0.0)) for e in non_terminal_entries
            )
    except Exception as exc:
        # Store import / read errors must NEVER raise from a contributor
        log.debug("pressure_field.read_curiosity_gap: store probe skipped (%s)", exc)
        store_max = 0.0

    # SS-side projection (legacy compat)
    ss_pressure = 0.0
    try:
        cur = shared_state.read(LAST_CURIOSITY_CYCLE)
        if isinstance(cur, dict):
            ss_pressure = _clamp_unit(cur.get("pressure"))
    except Exception:
        ss_pressure = 0.0

    weight = _clamp_unit(max(store_max, ss_pressure))
    _emit_contributor_signal("curiosity_gap", weight)
    return weight


def read_operator_intent(shared_state: SharedState) -> float:
    """Operator intent pressure (S6 LIVE). Reads
    LAST_OPERATOR_DIRECTIVE via pressure_loop.operator_pressure (TTL-aware
    reader). Returns the directive's `weight` scalar in [0, 1] when
    a non-expired directive is published; 0.0 otherwise. NEVER raises.

    Wired live in S6 from the prior 0.0 stub. The directive is
    published by core.brain_receiver when it detects a learning
    intent in operator chat -- pressure then flows naturally through
    evaluate_pressure -> cue_desk.tick -> episode open with
    opened_by='operator_intent' (when this contributor is dominant)
    AND trajectory seed pointing at the directive's domain."""
    try:
        from pressure_loop.operator_pressure import read_operator_directive

        rec = read_operator_directive(shared_state)
    except Exception:
        _emit_contributor_signal("operator_intent", 0.0)
        return 0.0
    if not rec:
        _emit_contributor_signal("operator_intent", 0.0)
        return 0.0
    weight = _clamp_unit(rec.get("weight"))
    _emit_contributor_signal("operator_intent", weight)
    return weight


def read_neuromod_spike(
    shared_state: SharedState,
    *,
    episode_store=None,
) -> float:
    """Failure-aware neuromod spike pressure scalar from episode body-state.

    Precedence (native weave):
      1. Active episode ``episode.auto["neuromod"]`` — same-cycle body-state.
      2. ``episode_store.last_closed_body()["neuromod"]`` — cross-tick carry.

    Both paths score via ``compute_closed_body_spike`` (NOT success-gated).
    ``shared_state`` is retained for registry signature compatibility but is
    unused for neuromod reads.

    Returns scalar in [0, 1]. NEVER raises — absence or garbage → 0.0.
    """
    try:
        if episode_store is not None:
            from autonomic.symphony_neuromod import compute_closed_body_spike

            try:
                episode = episode_store.current()
            except Exception:
                episode = None
            if episode is not None:
                auto = getattr(episode, "auto", None) or {}
                neuromod = auto.get("neuromod") if isinstance(auto, dict) else None
                if isinstance(neuromod, dict):
                    result = _clamp_unit(compute_closed_body_spike(neuromod))
                    _emit_contributor_signal("neuromod_spike", result)
                    return result

            try:
                body = episode_store.last_closed_body()
            except Exception:
                body = None
            if isinstance(body, dict):
                neuromod = body.get("neuromod")
                if isinstance(neuromod, dict):
                    result = _clamp_unit(compute_closed_body_spike(neuromod))
                    _emit_contributor_signal("neuromod_spike", result)
                    return result
    except Exception as exc:
        log.debug("read_neuromod_spike: episode-store path failed (%s)", exc)

    _emit_contributor_signal("neuromod_spike", 0.0)
    return 0.0


_GUARDIAN_ADVERSARIAL_KINDS: frozenset[str] = frozenset(
    {"reject", "quarantine", "flag", "block", "deny"}
)


def read_guardian(shared_state: SharedState) -> float:
    """Guardian-intervention pressure scalar (v0.2 §S2 LIVE).

    Was a 0.0 stub through S3-S9; v0.2 §S2 acceptance wires it against
    the same ledger the legacy `pressure_loop.cycle_cadence
    .guardian_intervention_provider` reads — `guardian/logs/
    guardian_interventions.jsonl` — by sharing the time-windowed tail
    helper `_recent_jsonl_entries`.

    Semantics — BINARY, matching the legacy
    `_DEFAULT_GUARDIAN_INTERVENTION_THRESHOLD = 1`: any single
    in-window adversarial intervention returns full pressure (1.0).
    Guardian intervention is an *authority block*, not a soft ambient
    mood signal — one block should open attention. Routing into
    RESPONSE_HOLD vs OMEGA_PRELUDE happens downstream at the ignition
    selector (§S9); this contributor's job is to make the open call.

    Counts ONLY adversarial decisions
    (`{reject, quarantine, flag, block, deny}`). Benign auto-allows
    (`approve`) and non-adversarial admin kinds (`release`, `retract`,
    `rollback`) contribute 0.0 — the bio-loop's default-allow doctrine
    means "approve" is the system working normally, NOT Guardian
    intervention; counting them was the production phantom-pressure
    spin (every memory promotion → permanent immediate-trigger).

    INHERITED from sharing `_recent_jsonl_entries` (JSONL-safety
    pieces — verify in `pressure_loop/cycle_cadence.py:106`):
      * Time window  — entries older than `_PRESSURE_WINDOW_SECONDS`
                       (600s default) age out so a frozen log tail
                       goes quiet.
      * Structured kind filter — we match against a typed
                       `kind in _GUARDIAN_ADVERSARIAL_KINDS` set;
                       string-substring drift can't fire this.
      * Malformed timestamp exclusion — the helper EXCLUDES entries
                       with no parseable `ts`/`timestamp` ("a stale
                       log must not keep pressure hot").

    NOT inherited (deferred to §S3 CueDesk audit / §S9 ignition):
      * Refractory / replay suppression. The helper does not
        deduplicate by `intervention_id`; the same in-window
        adversarial entry will count on every tick until it ages out
        of the 600s window. The route consumer (ignition →
        response_hold / omega_prelude) MUST mark the intervention
        handled so the next open is gated on a new event rather than
        the same one. Tracked as: "same `intervention_id` must not
        reopen forever after the selected route handles it." Until
        that lands, repeated opens on a single Guardian event are an
        accepted behavior bounded by the 600s window — not a doctrine
        violation, but a corner the next slice closes.

    Output: 1.0 iff ≥1 in-window adversarial intervention, else 0.0.
    NEVER raises — any read/import failure returns 0.0.
    """
    try:
        from pressure_loop.cycle_cadence import _recent_jsonl_entries

        for entry in _recent_jsonl_entries(("guardian", "logs", "guardian_interventions.jsonl")):
            if str(entry.get("kind", "")).strip().lower() not in _GUARDIAN_ADVERSARIAL_KINDS:
                continue
            # Phase 2 §P2-S15 cure (RB#3): skip entries whose
            # intervention_id is in the handled cache. The route
            # consumer (response_hold / omega_prelude / executive)
            # marks an intervention handled via
            # `mark_guardian_intervention_handled(intervention_id)`
            # after dispatching its response.
            iid = str(entry.get("intervention_id", "") or "")
            if iid and iid in _GUARDIAN_INTERVENTION_HANDLED:
                continue
            _emit_contributor_signal("guardian", 1.0)
            return 1.0
    except Exception as exc:
        # Contributor reader contract: NEVER raise. Helper import or
        # JSONL read failure → 0.0 with a debug-level breadcrumb.
        log.debug("read_guardian: ledger tail failed (%s)", exc)
        _emit_contributor_signal("guardian", 0.0)
        return 0.0
    _emit_contributor_signal("guardian", 0.0)
    return 0.0


def read_operator_message(shared_state: SharedState) -> float:
    """Operator pressure = IS ANYONE IN LINE? (AGENTS.md §4.0)

    The line itself is the pressure. A turn waiting in the durable queue means
    operator pressure exists; an empty queue means none.

    This used to read a SharedState mirror under linear TTL decay — ~44 lines
    of timestamp parsing, timezone coercion and age math with FIVE
    independent `return 0.0` branches, so "the operator said something" and
    "the operator said nothing" were the same value (§2.1). The 60 s TTL also
    expired during any longer episode, making a turn still waiting in line
    report as no operator input at all, and made metadata survival a function
    of wall-clock episode duration — which silently confounded every
    measurement over the chat path (audit §1.6, §1.13).

    A line needs no decay clock: it self-clears because the head walks through
    the gate. Leasing PEEKS — it never consumes.
    """
    del shared_state  # the line is durable; it is not SharedState state
    from pressure_loop.cognition_queue import has_waiting_input

    # Ask, never claim: this runs every tick, and a claiming read would either
    # brick the gate or hand ignition a turn nobody released.
    #
    # This pressure component asks specifically whether an operator-authored item
    # is waiting in the shared FIFO. Machine-minted work uses the same queue but
    # does not become operator pressure merely by waiting there.
    head = has_waiting_input(operator_only=True)
    weight = _clamp_unit(float(head.weight)) if head is not None else 0.0
    _emit_contributor_signal("operator_message", weight)
    return weight


def read_epistemic_emptiness(shared_state: SharedState) -> float:
    """Phase 2 §P2-S8: endogenous emptiness-pressure reader.

    Closes the drive-precedes-goals cure law: a brand-new no-knowledge
    substrate experiences emptiness AS pressure. The want-to-understand
    is the engine, not an implanted boot goal. See
    ``[[project-pressure-from-emptiness-drive-precedes-goals]]`` and
    ``FINAL_OMEGA_DOCTRINE.md``.

    Read-only over ``memory.strength_index`` record count via the
    public ``items_snapshot()`` API. Base scalar in [0.0, 1.0]:

        record_count == 0           ->  1.0  (maximum want-to-understand)
        0 < count < BASELINE        ->  linear ramp 1.0 -> 0.0
        count >= BASELINE           ->  0.0  (substrate has enough)

    The natural decay path: as the substrate writes verified memory
    through the existing DeepSleep archive, record_count grows and the
    base pressure decreases organically.

    Refractory (operator BLOCK fix, 2026-05-24): "duplicate idle ticks
    must not flood repeated executive movement." The contributor caches
    the previously-observed record_count. On consecutive ticks WITHOUT
    a substrate-state change, the returned pressure decays exponentially
    after a tunable refractory threshold (default: 1 unchanged tick).
    Once the substrate state changes (memory ingest, etc.), the cache
    resets and base pressure returns. The cache is in-process module
    state — read-only with respect to SharedState; no goals.jsonl,
    memory, Guardian, or curiosity writes from the contributor.

    Forbidden side effects: this reader MUST NOT write SharedState,
    ``goals.jsonl``, memory records, Guardian state, curiosity entries,
    or invoke movement runners / loop adapters / model runtimes / HUD /
    browser / network. Per P2-S8 packet §3 + §5 audit Req 3.

    NEVER raises — any read/import failure returns 0.0 (silence is
    data, not pressure, per the S3 contract). When the StrengthIndex
    singleton is unwired, the reader returns 0.0 AND resets the
    refractory cache (so the first wired read after restart starts
    from a clean slate).

    The ``shared_state`` argument is required by the ContributorReader
    signature but intentionally unused — emptiness reads global
    substrate state (memory.strength_index singleton), not SharedState
    projections. Mirrors the curiosity_gap pattern.
    """
    del shared_state  # interface compatibility; this reader is global
    try:
        from memory import strength_index as si_module

        si = si_module.get_strength_index()
    except Exception:
        return 0.0
    if si is None:
        # Singleton unwired (e.g., direct-mode harness, fresh process
        # before boot() runs init_strength_index). Reset the refractory
        # cache so the first wired read starts from a clean slate.
        _reset_epistemic_emptiness_cache()
        return 0.0
    try:
        snapshot = si.items_snapshot()
        record_count = len(snapshot)
    except Exception:
        return 0.0

    # Phase B-3 logging coverage helper: emit a heartbeat for the
    # rare state TRANSITIONS (cold_start / substrate_changed /
    # refractory_window_expired / first_fire_in_window /
    # above_baseline). The noisy steady-state `idle_no_new_evidence`
    # is NOT emitted (it repeats every tick during healthy idle —
    # would flood feedback_audit.jsonl). Best-effort: emit failure
    # never affects pressure semantics.
    def _emit_emptiness_state(state: str, **fields: Any) -> None:
        log.debug(
            "pressure.epistemic_emptiness state=%s record_count=%s",
            state,
            fields.get("record_count"),
        )

    # Base pressure from substrate emptiness.
    if record_count >= _EPISTEMIC_EMPTINESS_FULL_BASELINE:
        # At or above baseline — no emptiness pressure. Reset the
        # refractory cache so subsequent re-emptying starts clean.
        _EPISTEMIC_EMPTINESS_CACHE["last_record_count"] = record_count
        _EPISTEMIC_EMPTINESS_CACHE["consecutive_unchanged"] = 0
        _EPISTEMIC_EMPTINESS_CACHE["fired_in_window"] = False
        _LAST_EPISTEMIC_EMPTINESS_DETAIL.clear()
        _LAST_EPISTEMIC_EMPTINESS_DETAIL.update(
            {
                "state": "above_baseline",
                "record_count": record_count,
                "consecutive_unchanged": 0,
                "fired_in_window": False,
            }
        )
        _emit_emptiness_state(
            "above_baseline",
            record_count=record_count,
            baseline=_EPISTEMIC_EMPTINESS_FULL_BASELINE,
        )
        return 0.0
    if record_count <= 0:
        base = 1.0
    else:
        base = _clamp_unit(1.0 - (float(record_count) / float(_EPISTEMIC_EMPTINESS_FULL_BASELINE)))

    # Phase 2 §P2-S19: event/refractory-based gating (cures busy-wait).
    #
    # Three triggers may emit OPENING weight (`base`):
    #   1. Substrate version/count changed (last != record_count, or
    #      last is None on the very first read after reset/restart-cold).
    #   2. Refractory window expired (consecutive_unchanged > window).
    #   3. Same substrate but `fired_in_window` is False — i.e., the
    #      current window has not yet fired (covers the first idle tick
    #      after either trigger 1 or 2 reset the window).
    #
    # All other unchanged ticks return 0.0 (hard non-opening). This is
    # the structural cure on top of the prior exponential decay: the
    # contributor itself refuses to emit opening weight within a window
    # that has already fired, even before CueDesk's threshold can drop
    # it. Peer contributors that ASK for action/hold are evaluated
    # independently by their own readers; the selector's
    # _ACTION_MEANINGFUL_CONTRIBUTORS precedence rule (P2-S9) still
    # lets them win at lower weight when emptiness contributes 0.0.
    last = _EPISTEMIC_EMPTINESS_CACHE["last_record_count"]
    state_changed = last is not None and last != record_count
    cold_start = last is None

    if state_changed or cold_start:
        # Trigger 1: substrate evolved. Window is fresh — one fire
        # allowed, then refractory begins on the next unchanged tick.
        _state = "substrate_changed" if state_changed else "cold_start"
        _EPISTEMIC_EMPTINESS_CACHE["last_record_count"] = record_count
        _EPISTEMIC_EMPTINESS_CACHE["consecutive_unchanged"] = 0
        _EPISTEMIC_EMPTINESS_CACHE["fired_in_window"] = True
        _LAST_EPISTEMIC_EMPTINESS_DETAIL.clear()
        _LAST_EPISTEMIC_EMPTINESS_DETAIL.update(
            {
                "state": _state,
                "record_count": record_count,
                "consecutive_unchanged": 0,
                "fired_in_window": True,
            }
        )
        _emit_emptiness_state(
            _state,
            record_count=record_count,
            weight=round(float(base), 3),
        )
        return base

    # Unchanged tick — increment the refractory counter, update last, decide.
    # (Phase E4: the P3-S31 runtime-busy "tick freeze" was retired with the
    # auto-driven pressure system; the counter advances every unchanged tick.)
    _EPISTEMIC_EMPTINESS_CACHE["consecutive_unchanged"] += 1
    _EPISTEMIC_EMPTINESS_CACHE["last_record_count"] = record_count
    unchanged = _EPISTEMIC_EMPTINESS_CACHE["consecutive_unchanged"]
    fired = bool(_EPISTEMIC_EMPTINESS_CACHE.get("fired_in_window", False))

    if unchanged >= _EPISTEMIC_EMPTINESS_REFRACTORY_WINDOW:
        # Trigger 2: window expired without substrate change — allow
        # one more fire. Reset the window counter; the next batch of
        # unchanged ticks will again return 0.0 until the next expiry.
        # Condition is `>=` so that a window of N means at most one
        # fire per N unchanged ticks (cold-start fire counts as the
        # window's allotted fire; the (N+1)-th total tick is the
        # first to re-trigger).
        _EPISTEMIC_EMPTINESS_CACHE["consecutive_unchanged"] = 0
        _EPISTEMIC_EMPTINESS_CACHE["fired_in_window"] = True
        _LAST_EPISTEMIC_EMPTINESS_DETAIL.clear()
        _LAST_EPISTEMIC_EMPTINESS_DETAIL.update(
            {
                "state": "refractory_window_expired",
                "record_count": record_count,
                "consecutive_unchanged": 0,
                "fired_in_window": True,
                "window": _EPISTEMIC_EMPTINESS_REFRACTORY_WINDOW,
            }
        )
        _emit_emptiness_state(
            "refractory_window_expired",
            record_count=record_count,
            window=_EPISTEMIC_EMPTINESS_REFRACTORY_WINDOW,
            weight=round(float(base), 3),
        )
        return base

    if not fired:
        # Trigger 3: same substrate but window hasn't fired yet. Can
        # happen after an explicit cache reset or after a fresh restart
        # where the WAL restored last_record_count but fired_in_window
        # came back False (e.g., migration from pre-S19 cache shape).
        _EPISTEMIC_EMPTINESS_CACHE["fired_in_window"] = True
        _LAST_EPISTEMIC_EMPTINESS_DETAIL.clear()
        _LAST_EPISTEMIC_EMPTINESS_DETAIL.update(
            {
                "state": "first_fire_in_window",
                "record_count": record_count,
                "consecutive_unchanged": unchanged,
                "fired_in_window": True,
            }
        )
        _emit_emptiness_state(
            "first_fire_in_window",
            record_count=record_count,
            consecutive_unchanged=unchanged,
            weight=round(float(base), 3),
        )
        return base

    # Fired once already; within the window → idle/no new evidence.
    _LAST_EPISTEMIC_EMPTINESS_DETAIL.clear()
    _LAST_EPISTEMIC_EMPTINESS_DETAIL.update(
        {
            "state": "idle_no_new_evidence",
            "record_count": record_count,
            "consecutive_unchanged": unchanged,
            "fired_in_window": True,
            "window": _EPISTEMIC_EMPTINESS_REFRACTORY_WINDOW,
        }
    )
    return 0.0


# Phase 2 §P2-S10: durable ASK/HOLD evidence terminal pickup helpers.
# Reads canonical fields from CuriosityEngineEntry — recommended_path,
# requires_operator_input, status, ignore_reason, problem_statement.
# PACKET §3 #2: "Use list_all() or an equivalent explicit helper to
# find deferred HOLD records without broadening normal curiosity-gap
# semantics accidentally." Reader is READ-ONLY: no store mutation, no
# SharedState write, no memory write, no Guardian write, no goal emit,
# no movement/runner call, no network/HUD/browser call.


def _is_ask_terminal(entry: Any) -> bool:
    """ASK = the entry recommends asking the operator AND requires
    operator input AND the entry is still live (open/active). Matches
    PACKET §3 #1: ``recommended_path='ask_user'``,
    ``requires_operator_input=True``."""
    if entry is None:
        return False
    return (
        getattr(entry, "recommended_path", "") == "ask_user"
        and bool(getattr(entry, "requires_operator_input", False))
        and getattr(entry, "status", "") in ("open", "active")
    )


def _is_hold_terminal(entry: Any) -> bool:
    """HOLD = the canonical deferred shape, OR the transitional shape
    where the recommendation is to defer but the entry hasn't been
    lifecycle-transitioned yet. PACKET §3 #1 names both
    ``recommended_path='defer'`` and ``status='deferred'`` as HOLD;
    §3 #2 calls out the deferred-status visibility cure.

    Operator BLOCK 2026-05-24 (post-2c8adab): an entry with
    ``status='resolved'`` (work done) or ``status='escalated'``
    (out-of-live-sweep awaiting operator attention) but a stale
    ``recommended_path='defer'`` MUST NOT resurrect as fresh HOLD
    pressure. ``curiosity_store._LIVE_STATUSES`` defines only
    ``open`` / ``active`` as live; ``resolved`` / ``deferred`` /
    ``escalated`` are closed lifecycle states.

    Lifecycle gating:
      - status == "deferred"  -> HOLD (canonical, regardless of
                                 recommended_path; this is the
                                 ``deferred not in list_open()``
                                 cure)
      - status in {"open", "active"} AND recommended_path == "defer"
                              -> HOLD (transitional: the evaluator
                                 recommended deferral but lifecycle
                                 mark_deferred hasn't yet run)
      - any other combination -> NOT HOLD (closed work stays closed)
    """
    if entry is None:
        return False
    status = str(getattr(entry, "status", "") or "")
    if status == "deferred":
        return True
    if str(getattr(entry, "recommended_path", "") or "") == "defer":
        return status in ("open", "active")
    return False


def _compact_terminal_for_pressure(
    entry: Any,
    kind: str,
) -> Dict[str, Any]:
    """Compact dispatch hint dict for episode.pressure detail. Carries
    just enough for the outlet to ask or report the selected terminal
    without scanning the store again (PACKET §3 #3 ASK pack contents,
    §3 #4 HOLD pack contents). Long text fields are bounded at 240
    chars so the pressure snapshot stays compact."""
    problem = str(getattr(entry, "problem_statement", "") or "")[:240]
    ignore_reason = str(getattr(entry, "ignore_reason", "") or "")[:240]
    return {
        "terminal_kind": kind,  # "ask" | "hold"
        "curiosity_id": str(getattr(entry, "curiosity_id", "") or ""),
        "recommended_path": str(getattr(entry, "recommended_path", "") or ""),
        "status": str(getattr(entry, "status", "") or ""),
        "requires_operator_input": bool(
            getattr(entry, "requires_operator_input", False),
        ),
        "problem_statement": problem,
        "ignore_reason": ignore_reason,
        "tension_score": float(
            getattr(entry, "tension_score", 0.0) or 0.0,
        ),
        "trigger_source": str(
            getattr(entry, "trigger_source", "") or "",
        ),
        "gap_type": str(getattr(entry, "gap_type", "") or ""),
    }


def _selected_evidence_terminal() -> Optional[Dict[str, Any]]:
    """Pick the selected ASK or HOLD terminal from durable S12 state,
    skipping handled-cache entries. ASK takes precedence over HOLD
    (an operator question pending is the more time-sensitive shape).
    Among ASKs, highest tension_score wins; among HOLDs, same.

    Returns the compact dispatch hint dict (terminal_kind, curiosity_id,
    recommended_path, problem_statement, ignore_reason, ...) or None
    when nothing is selectable. NEVER raises — store import or read
    failure returns None.
    """
    try:
        from hud import curiosity_store
    except Exception as exc:
        log.debug("evidence_terminal: curiosity_store import skipped (%s)", exc)
        return None
    try:
        entries = curiosity_store.list_all()
    except Exception as exc:
        log.debug("evidence_terminal: list_all read skipped (%s)", exc)
        return None

    # ONE operator question at a time (operator spec 2026-05-29). The
    # durable open-question journal records every query already surfaced
    # to the operator. While ANY such query is open/unresolved, the
    # operator channel is occupied: the loop must NOT surface another ASK
    # as pressure — it "knows it has an open one and bypasses until
    # resolved", so cognition keeps LEARNING instead of hanging. New ask
    # curiosities are still recorded (queued) by the response_hold
    # handler; they simply don't re-hold the loop, and surface once the
    # open one resolves. Because the marker is the durable jsonl, this
    # survives reboots (the old in-process handled cache was lost on
    # restart -> the reported permanent jam). Failure-soft: if the
    # journal can't be read, fall back to the in-process handled cache.
    try:
        from pressure_loop import open_questions as _oq

        _has_open_question = bool(_oq.asked_curiosity_ids())
    except Exception as exc:
        log.debug("evidence_terminal: open-question gate skipped (%s)", exc)
        _has_open_question = False

    best_ask: Optional[Any] = None
    best_ask_tension: float = -1.0
    best_hold: Optional[Any] = None
    best_hold_tension: float = -1.0

    for entry in entries or []:
        cid = str(getattr(entry, "curiosity_id", "") or "")
        if not cid:
            continue
        if cid in _EVIDENCE_TERMINAL_HANDLED:
            continue
        tension = float(getattr(entry, "tension_score", 0.0) or 0.0)
        if _is_ask_terminal(entry):
            # An operator question is already open and unresolved -> the
            # channel is occupied. Bypass ALL asks (queue silently) so the
            # loop continues learning; the next surfaces once the open one
            # resolves. Never hold the route waiting.
            if _has_open_question:
                continue
            if tension > best_ask_tension:
                best_ask = entry
                best_ask_tension = tension
        elif _is_hold_terminal(entry):
            if tension > best_hold_tension:
                best_hold = entry
                best_hold_tension = tension

    # ASK precedence: an operator-pending question is more time-sensitive
    # than a deferred-evidence hold report. Per PACKET §3 #3 + §3 #4 the
    # two terminal kinds share the RESPONSE_HOLD outlet but ASK requires
    # operator-side response while HOLD is report-style.
    if best_ask is not None:
        return _compact_terminal_for_pressure(best_ask, "ask")
    if best_hold is not None:
        return _compact_terminal_for_pressure(best_hold, "hold")
    return None


def read_evidence_terminal(shared_state: SharedState) -> float:
    """Phase 2 §P2-S10: durable ASK/HOLD evidence terminal reader.

    Returns ``_EVIDENCE_TERMINAL_WEIGHT`` (0.6) when at least one
    selectable ASK or HOLD terminal exists in the S12 curiosity store,
    else 0.0. The selected terminal's compact detail is exposed via
    ``evaluate_pressure``'s contributor_details slot so Global can
    build a targeted RESPONSE_HOLD action pack without re-scanning the
    durable store.

    PACKET §3 #1 + §3 #2: read CuriosityEngineEntry records from
    curiosity_store via list_all() (NOT list_open(), which excludes
    deferred HOLDs). Read-only — no SharedState/memory/Guardian/goal
    writes; no runtime/HUD/browser/network; no movement runners; no
    response dispatch.

    The shared_state arg is required by the ContributorReader
    signature but unused — pickup reads global durable substrate via
    curiosity_store (mirrors curiosity_gap / epistemic_emptiness
    pattern of bypassing SS for live substrate reads).
    """
    del shared_state
    selected = _selected_evidence_terminal()
    if selected is None:
        _emit_contributor_signal("evidence_terminal", 0.0)
        return 0.0
    _emit_contributor_signal(
        "evidence_terminal",
        _EVIDENCE_TERMINAL_WEIGHT,
        details={
            "terminal_kind": selected.get("terminal_kind"),
            "curiosity_id": selected.get("curiosity_id"),
        },
    )
    return _EVIDENCE_TERMINAL_WEIGHT


# Module-level cache for the most-recent idle-probe sweep so the
# evaluate_pressure detail-enrichment block can read the same findings
# the reader saw without invoking probes twice. Mirrors the
# evidence_terminal detail-enrichment pattern (P2-S10) but for the
# multi-finding aggregate. Mutated only by `read_idle_probe_aggregate`
# inside one evaluate_pressure call; readers don't need locking
# because evaluate_pressure runs single-threaded per tick.
_LAST_IDLE_PROBE_FINDINGS: List[Dict[str, Any]] = []


def read_idle_probe_aggregate(
    shared_state: SharedState,
    *,
    episode_store=None,
) -> float:
    """Phase 2 §P2-S15: aggregate idle-probe pressure.

    Calls `pressure_loop.symphony_probes.run_idle_probes(ss, episode_store)`
    and sums the per-finding `weight` field (clamped to [0, 1] via
    the aggregator), returning the clamped aggregate. Per-probe
    refractory caches in `pressure_loop.symphony_probes` (plus the restart-
    safe `_GUARDIAN_INTERVENTION_HANDLED` cache in this module)
    bound repeat-fire on the same substrate fact.

    Findings (a copy) are stashed on
    ``_LAST_IDLE_PROBE_FINDINGS`` so the evaluate_pressure detail
    enrichment block can populate
    ``PressureDecision.contributor_details["idle_probe_aggregate"]``
    with the probe_classes_seen list + has_meaningful_class flag
    without invoking probes again.

    Never raises — probe failures land in symphony_probes' own
    log.debug breadcrumbs; the aggregator returns 0.0 on a fully
    degraded probe path.
    """
    _LAST_IDLE_PROBE_FINDINGS.clear()
    try:
        from pressure_loop import symphony_probes

        findings = symphony_probes.run_idle_probes(shared_state, episode_store) or []
    except Exception as exc:  # noqa: BLE001 - contributor must not raise
        log.debug(
            "read_idle_probe_aggregate: run_idle_probes failed (%s); aggregate degraded to 0.0",
            exc,
        )
        _emit_contributor_signal("idle_probe_aggregate", 0.0)
        return 0.0
    if not findings:
        _emit_contributor_signal("idle_probe_aggregate", 0.0)
        return 0.0
    total_weight = 0.0
    for f in findings:
        try:
            w = float(getattr(f, "weight", 0.0) or 0.0)
        except (TypeError, ValueError):
            w = 0.0
        if w > 0.0:
            total_weight += w
        # Cache compact dict for the detail enrichment pass
        try:
            _LAST_IDLE_PROBE_FINDINGS.append(f.to_dict())
        except Exception:  # noqa: BLE001
            pass
    weight = _clamp_unit(total_weight)
    _emit_contributor_signal("idle_probe_aggregate", weight)
    return weight


# =====================================================================
# Learning-debt contributor (mastery-driven pressure)
# =====================================================================
#
# Per-rung weight ladder. Operator-tunable defaults (2026-05-26):
# all values are floats in [0, 0.5].
#
# Base weight by mastery GAP count (domains where knowledge < target):
#   - empty_substrate flag fires (no knowledge ∧ SI empty ∧ no recall)
#       → LEARNING_DEBT_BASE_EMPTY
#   - 0 gaps (every category at/above target) → 0.0  (NO floor — the mind
#       rests; curiosity is grounded in real gaps, not a perpetual pull)
#   - 1..THRESHOLD_GROWING gaps   → LEARNING_DEBT_BASE_GROWING (few gaps)
#   - more than THRESHOLD_GROWING → LEARNING_DEBT_BASE_EARLY    (many gaps)
#
# Additive penalties (only when there IS a gap to learn):
#   - stale_mastery       → +LEARNING_DEBT_PENALTY_STALE
#   - low_confidence_recall → +LEARNING_DEBT_PENALTY_LOW_CONF
#
# Final weight clamped to [0, LEARNING_DEBT_HARD_CAP] so the
# contributor NEVER dominates pressure alone.
LEARNING_DEBT_BASE_EMPTY: float = 0.40
LEARNING_DEBT_BASE_EARLY: float = 0.25
LEARNING_DEBT_BASE_GROWING: float = 0.10
LEARNING_DEBT_PENALTY_STALE: float = 0.15
LEARNING_DEBT_PENALTY_LOW_CONF: float = 0.10
LEARNING_DEBT_HARD_CAP: float = 0.50
LEARNING_DEBT_THRESHOLD_GROWING: int = 2  # ≤ N gaps → GROWING (few); more → EARLY


def _mastery_gap_count() -> int:
    """Count mastery domains with an OPEN gap (knowledge < target).

    The grounded source of learning pressure: 0 gaps ⇒ nothing to learn ⇒ no
    debt. Reads the mastery store (the single source of truth)."""
    try:
        from deepsleep_loop.mastery_store import load_mastery

        records = load_mastery()
        n = 0
        for r in records.values():
            knowledge = float(getattr(r, "knowledge_mastery", 0.0))
            # Clean-switch partition (Step 0): when the band is ON, a domain
            # leaves the learning set at OPERATIONAL_MASTERY and enters the
            # practice set — so learning and application never count the same
            # domain. Band OFF → the original target gate (byte-identical).
            floor = (
                OPERATIONAL_MASTERY
                if OPERATIONAL_PRACTICE_BAND
                else float(getattr(r, "target_mastery", 0.0))
            )
            if knowledge < floor:
                n += 1
        return n
    except Exception:
        return 0


def read_learning_debt(
    shared_state: SharedState,
    *,
    learning_state_envelope: Optional[Dict[str, Any]] = None,
) -> float:
    """Pressure from real mastery gaps (knowledge < target), stale knowledge, weak recall.

    The substrate's "learning want" — fires when there are unmastered
    categories (or a newly-created/seeded subcategory), decays as mastery
    reaches target. REACHES 0 when every category is at/above target: a
    fully-mastered substrate manufactures no exploration pressure (the mind
    rests until a new gap appears).

    Reads the SAME compact envelope that rides on
    ``episode.learning_state`` (Phase 2 §P2-S13 schema). Specifically
    consumes:
      - mastery.domains_count       (int — coverage tier)
      - learning_debt_flags.stale_mastery        (bool — freshness penalty)
      - learning_debt_flags.low_confidence_recall (bool — recall penalty)
      - learning_debt_flags.empty_substrate       (bool — first-tier gate)

    Args:
      shared_state: unused at present (kept for ContributorReader
        signature parity; future revisions may read SS for cycle-id
        scoping). The envelope is the single source of truth.
      learning_state_envelope: optional pre-built envelope dict from
        ``pressure_loop.symphony_learning_state.build_episode_learning_state``.
        When None (production), this function calls the composer
        itself. Tests inject deterministic fixtures.

    Returns weight in [0, LEARNING_DEBT_HARD_CAP], failure-soft (any
    composer failure → 0.0 with refractory-bound emit).
    """
    try:
        if learning_state_envelope is None:
            from pressure_loop.symphony_learning_state import build_episode_learning_state

            envelope = build_episode_learning_state()
        else:
            envelope = learning_state_envelope
        if not isinstance(envelope, dict):
            envelope = {}
        mastery_section = envelope.get("mastery")
        if not isinstance(mastery_section, dict):
            mastery_section = {}
        flags = envelope.get("learning_debt_flags")
        if not isinstance(flags, dict):
            flags = {}
        mastery_source = str(mastery_section.get("source") or "unavailable")

        # GROUNDED: learning_debt is justified ONLY by real mastery gaps —
        # categories where knowledge < target — counted by a DIRECT read of the
        # mastery store (_mastery_gap_count), NOT the envelope's composed source.
        # A flaky SI/recall surface (mastery_source != "live") therefore does NOT
        # zero out real gaps; the envelope only supplies best-effort
        # freshness/recall flags. There is NO baseline floor: a fully-mastered
        # substrate yields 0 and the mind RESTS. A newly-created/seeded
        # subcategory is a target>0 / actual≈0 record = a gap = debt, so
        # onboarding, a work-order, or web-discovered domain all raise justified
        # learning pressure (operator: "dont add learning debt unless omega
        # hasnt reached mastery in a category or outside info is added ... anytime
        # a new sub cat is created, that should get added to pressure").
        empty_substrate = bool(flags.get("empty_substrate", False))
        gap_count = _mastery_gap_count()

        if gap_count > 0:
            # Real gaps take priority over the empty flag — a seeded substrate
            # has gap records even before any content/SI exists.
            base = (
                LEARNING_DEBT_BASE_GROWING
                if gap_count <= LEARNING_DEBT_THRESHOLD_GROWING
                else LEARNING_DEBT_BASE_EARLY
            )
        elif empty_substrate:
            base = LEARNING_DEBT_BASE_EMPTY
        else:
            base = 0.0

        penalty = 0.0
        if base > 0.0:
            if bool(flags.get("stale_mastery", False)):
                penalty += LEARNING_DEBT_PENALTY_STALE
            if bool(flags.get("low_confidence_recall", False)):
                penalty += LEARNING_DEBT_PENALTY_LOW_CONF

        weight = max(0.0, min(base + penalty, LEARNING_DEBT_HARD_CAP))
        _emit_contributor_signal(
            "learning_debt",
            weight,
            details={
                "mastery_source": mastery_source,
                "gap_count": gap_count,
                "empty_substrate": empty_substrate,
                "stale_mastery": bool(flags.get("stale_mastery", False)),
                "low_confidence_recall": bool(flags.get("low_confidence_recall", False)),
                "base": base,
                "penalty": penalty,
            },
        )
        return weight
    except Exception as exc:  # noqa: BLE001
        log.debug("read_learning_debt: failure-soft fallback (%s)", exc)
        _emit_contributor_signal("learning_debt", 0.0)
        return 0.0


# =====================================================================
# Execution-gap contributor (application-driven pressure) — Phase 5a
# =====================================================================
#
# The application-side analogue of learning_debt. learning_debt counts domains
# where knowledge < target (the LEARN want); execution_gap counts domains where
# knowledge >= target but application < target (the "learned it, now practice
# it" want). The two sets are DISJOINT — they partition on knowledge < target vs
# knowledge >= target — so applying is never a re-count of a learning gap.
#
# Practice bar (memory-weighted, design §5): a learned domain becomes
# practice-ready only when the mind is WARM (recent memory activity). A fully
# cold mind manufactures no execution pressure — don't practice into the void.
#
# Phase 5b sharpened the bar from AGGREGATE warmth to PER-DOMAIN warmth: each
# learned-but-unapplied domain is gated on ITS OWN hot memory count (via
# ``memory.symphony_strength.hot_count_for_domain``), not a global hot_count.
# The 5a aggregate bar would fire when ANY domain was hot — so a hot ``python``
# substrate could ignite an APPLY for a stone-cold ``rust`` domain, practicing
# into a cold rust-substrate. Now a domain practices only on its own warmth.
#
# ARMED (Phase 5x complete, live-validated 2026-06-16): EXECUTION_GAP_ENABLED is
# the master switch for autonomous APPLY pressure. It is now ON by default — the
# safety boundary is NO LONGER this flag but the staged-approval GUARD downstream
# (auto_loop/execute/run.py USE_SKILL -> auto_loop/skill/run.py dispatch_skill_pack:
# skill_execution_guard + workspace jail +
# pre-APPLY mind checkpoint + hard deadline + staged approval). A learned-but-
# unapplied domain may now AUTONOMOUSLY OPEN an APPLY episode; whether that
# episode runs real code is decided at the gate (pending_approval at 0/0; only
# after STAGED_APPROVAL_N operator-approved APPLYs does it graduate to autonomous
# dispatch). Force this flag False to use it as a kill-switch.
EXECUTION_GAP_ENABLED: bool = True
EXECUTION_GAP_BASE_GROWING: float = 0.10
EXECUTION_GAP_BASE_EARLY: float = 0.20
EXECUTION_GAP_HARD_CAP: float = (
    0.35  # below learning_debt's 0.50: applying is secondary to closing knowledge gaps
)
EXECUTION_GAP_THRESHOLD_GROWING: int = 2
EXECUTION_GAP_MIN_HOT: int = 1  # >= N hot memories IN-DOMAIN = warm enough to practice that domain

# Step 0 of the metacognitive pressure evolution (operator-ratified 2026-06-19,
# docs/architecture/metacognitive-pressure-evolution.md §7 Step 0). OPERATIONAL
# is the "learned enough to start practicing" floor (matches the deepsleep reseed
# application_gap gate). When OPERATIONAL_PRACTICE_BAND is ON, the learning vs
# application partition moves from `target` (0.70) down to OPERATIONAL (0.60) as a
# CLEAN SWITCH — a domain in [OPERATIONAL, target) leaves learning_debt and
# becomes application/practice-eligible, so the two never double-count. Default
# OFF → byte-identical to the target-based partition. Omega (Step 5) later owns
# the within-band learn-vs-practice split via learning-progress.
OPERATIONAL_MASTERY: float = 0.60
# Step 5b KEYSTONE (2026-06-19): flipped ON. A domain in
# [OPERATIONAL, target) now LEAVES learning_debt and becomes application/practice-
# eligible — so a learned-but-unapplied domain drops out of the learning_debt count
# (learning_debt falls) and feeds application_gap (APPLY can win). Set back to False
# to revert to the target-based partition (learn until target, then apply).
OPERATIONAL_PRACTICE_BAND: bool = True


def has_application_gap(knowledge: float, target: float, application: float) -> bool:
    """Canonical application-gap predicate (#15 — single source of truth).

    A domain has an application gap when it is LEARNED enough to start practicing
    (knowledge >= the practice-ready lower bound) AND not yet APPLIED to target
    (application < target). The lower bound is OPERATIONAL_MASTERY when the
    practice band is ON (practice begins at operational, not full target), else
    `target` (the original target-based partition). Disjoint from the learning
    set by construction — learning is knowledge < this same bound.

    BOTH the field reader (_application_gap_domains) and the DeepSleep reseed
    emitter (deepsleep_loop.reseed._application_gap_contributors) call this so the
    field can never disagree with what consolidation reseeds. Replaced the
    reseed's bespoke `application >= knowledge - 0.20` band that silently drifted.
    """
    lower = OPERATIONAL_MASTERY if OPERATIONAL_PRACTICE_BAND else target
    return knowledge >= lower and application < target


def _application_gap_domains() -> list[str]:
    """Domain_ids LEARNED (knowledge >= target) but NOT yet APPLIED
    (application < target) — the practice-ready CANDIDATE set, before the
    per-domain memory-warmth bar (Phase 5b).

    Disjoint from the learning-gap set (knowledge < target): a domain is either
    still being learned or eligible for application, never both. Keyed by the
    record's ``domain_id`` (falling back to the store key) so the result joins
    directly against the coord-index ``domain`` field. Failure-soft → []."""
    try:
        from deepsleep_loop.mastery_store import load_mastery

        records = load_mastery()
        out: list[str] = []
        for key, r in records.items():
            knowledge = float(getattr(r, "knowledge_mastery", 0.0))
            target = float(getattr(r, "target_mastery", 0.0))
            application = float(getattr(r, "application_mastery", 0.0))
            if has_application_gap(knowledge, target, application):
                out.append(str(getattr(r, "domain_id", "") or key))
        return out
    except Exception:
        return []


def _application_gap_count() -> int:
    """Count of learned-but-unapplied domains (the candidate set size).

    Thin wrapper over :func:`_application_gap_domains` — kept as the public
    count surface so callers/telemetry that only need the magnitude don't
    re-walk the mastery store."""
    return len(_application_gap_domains())


def read_execution_gap(
    shared_state: SharedState,
    *,
    strength_index: Any = None,
) -> float:
    """Pressure to APPLY learned-but-unapplied domains, gated by PER-DOMAIN warmth.

    Fires only when EXECUTION_GAP_ENABLED and at least one domain is BOTH (a)
    learned (knowledge >= target) but not applied (application < target) AND (b)
    warm IN ITS OWN RIGHT (>= EXECUTION_GAP_MIN_HOT hot memories belonging to
    that domain). Application is EARNED by doing on the domain's own hot
    substrate, never a nagging gap and never borrowed from an unrelated hot
    domain (Phase 5b — the 5a aggregate bar conflated the two). Failure-soft → 0.0.

    Args:
      shared_state: unused (ContributorReader signature parity).
      strength_index: optional injected StrengthIndex (tests); production
        resolves the singleton via ``ensure_strength_index()`` inside the
        per-domain join.
    """
    if not EXECUTION_GAP_ENABLED:
        return 0.0
    try:
        gap_domains = _application_gap_domains()
        if not gap_domains:
            _emit_contributor_signal("execution_gap", 0.0)
            return 0.0

        # Per-domain practice bar (Phase 5b): a candidate domain is practice-
        # ready only when ITS OWN substrate clears EXECUTION_GAP_MIN_HOT hot
        # memories. A hot but unrelated domain contributes nothing here.
        from memory.symphony_strength import hot_count_for_domain

        practice_ready = [
            d
            for d in gap_domains
            if int(hot_count_for_domain(d, strength_index=strength_index)) >= EXECUTION_GAP_MIN_HOT
        ]
        n = len(practice_ready)
        if n <= 0:
            _emit_contributor_signal(
                "execution_gap",
                0.0,
                details={
                    "application_gap": len(gap_domains),
                    "practice_ready": 0,
                    "gated": "cold_domain_memory",
                },
            )
            return 0.0

        base = (
            EXECUTION_GAP_BASE_GROWING
            if n <= EXECUTION_GAP_THRESHOLD_GROWING
            else EXECUTION_GAP_BASE_EARLY
        )
        weight = max(0.0, min(base, EXECUTION_GAP_HARD_CAP))
        _emit_contributor_signal(
            "execution_gap",
            weight,
            details={
                "application_gap": len(gap_domains),
                "practice_ready": n,
                "domains": practice_ready[:5],
            },
        )
        return weight
    except Exception as exc:  # noqa: BLE001
        log.debug("read_execution_gap: failure-soft fallback (%s)", exc)
        _emit_contributor_signal("execution_gap", 0.0)
        return 0.0


def read_tool_creation_need(shared_state: SharedState) -> float:
    """Slice 2: pressure from an open tool_creation curiosity.

    Returns ``_TOOL_CREATION_NEED_WEIGHT`` (0.6) when the curiosity store
    holds >=1 OPEN/ACTIVE entry with recommended_path == "tool_creation"
    (Omega decided a tool is needed), else 0.0. Stashes the highest-tension
    such entry's {curiosity_id, capability_gap} on
    ``_LAST_TOOL_CREATION_NEED_DETAIL`` for evaluate_pressure to surface.

    Read-only -- no SS/memory/goal writes. NEVER raises (a contributor that
    raises poisons the aggregate). The shared_state arg is unused (the
    reader consults the global curiosity_store, like curiosity_gap)."""
    del shared_state
    _LAST_TOOL_CREATION_NEED_DETAIL.clear()
    try:
        from hud import curiosity_store

        entries = curiosity_store.list_open() or []
    except Exception as exc:
        log.debug("read_tool_creation_need: store probe skipped (%s)", exc)
        _emit_contributor_signal("tool_creation_need", 0.0)
        return 0.0
    candidates = [
        e
        for e in entries
        if str(getattr(e, "recommended_path", "") or "").lower() == "tool_creation"
        and str(getattr(e, "status", "") or "") in ("open", "active")
    ]
    if not candidates:
        _emit_contributor_signal("tool_creation_need", 0.0)
        return 0.0
    best = max(candidates, key=lambda e: float(getattr(e, "tension_score", 0.0) or 0.0))
    _LAST_TOOL_CREATION_NEED_DETAIL.update(
        {
            "curiosity_id": str(getattr(best, "curiosity_id", "") or ""),
            "capability_gap": str(
                getattr(best, "problem_statement", "") or getattr(best, "statement", "") or ""
            )[:240],
            "tension_score": float(getattr(best, "tension_score", 0.0) or 0.0),
        }
    )
    _emit_contributor_signal(
        "tool_creation_need",
        _TOOL_CREATION_NEED_WEIGHT,
        details={"curiosity_id": _LAST_TOOL_CREATION_NEED_DETAIL["curiosity_id"]},
    )
    return _TOOL_CREATION_NEED_WEIGHT


# Default contributor registry. Order is intentional: live readers
# first (so a misconfigured stub never overshadows a real signal),
# stubs last. Tests can build alternate registries via
# build_registry(...) for deterministic per-contributor coverage.
CONTRIBUTOR_OPERATOR_PRIORITY = "operator_priority"


def read_operator_priority(shared_state: SharedState) -> float:
    """Operator-directed priority pressure (2026-06-30): non-zero when the operator has flagged
    domains as priorities (``pressure_loop.operator_priority_store``). Routes to ASK_OPERATOR
    (global_loop _map_opened_by_to_intent). Empty by default -> 0.0. Failure-soft — a store
    hiccup must never break the pressure tick."""
    try:
        from pressure_loop.operator_priority_store import list_priorities

        return 0.55 if list_priorities() else 0.0
    except Exception:
        return 0.0


_DEFAULT_READERS: Dict[str, ContributorReader] = {
    CONTRIBUTOR_CURIOSITY_GAP: read_curiosity_gap,
    CONTRIBUTOR_OPERATOR_INTENT: read_operator_intent,
    CONTRIBUTOR_NEUROMOD_SPIKE: read_neuromod_spike,
    CONTRIBUTOR_OPERATOR_MESSAGE: read_operator_message,
    CONTRIBUTOR_EPISTEMIC_EMPTINESS: read_epistemic_emptiness,
    CONTRIBUTOR_IDLE_PROBE_AGGREGATE: read_idle_probe_aggregate,
    CONTRIBUTOR_LEARNING_DEBT: read_learning_debt,
    CONTRIBUTOR_EXECUTION_GAP: read_execution_gap,
    CONTRIBUTOR_OPERATOR_PRIORITY: read_operator_priority,
}


def build_registry(
    *,
    overrides: Optional[Dict[str, ContributorReader]] = None,
    extras: Optional[Dict[str, ContributorReader]] = None,
) -> Dict[str, ContributorReader]:
    """Build a contributor registry from the defaults + optional
    test/operator overrides + extras. Overrides replace built-in
    readers (e.g. inject a deterministic curiosity reader in tests);
    extras add new contributors not in the built-in set."""
    out = dict(_DEFAULT_READERS)
    if overrides:
        out.update(overrides)
    if extras:
        out.update(extras)
    return out


# ---------------------------------------------------------------------
# Pressure decision
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class PressureDecision:
    """Outcome of one evaluate_pressure() call.

    aggregate          -- sum of contributor scalars, clamped to [0, 1].
                          (Sum-then-clamp keeps contributors orthogonal
                           without per-pair tuning -- four 0.3 signals
                           lift to 1.0; one 0.8 + one 0.4 also 1.0.)
    contributors       -- per-id scalar dict, even when 0.0 (audit-
                          visible registry shape).
    open_threshold     -- the threshold used (echoed for trace).
    open_new_episode   -- True iff aggregate >= threshold AND every
                          gate (presence, autonomy-pause) allowed.
    suppress_reason    -- human-readable when open_new_episode is False
                          (e.g. 'below_threshold', 'presence_not_present',
                          'autonomy_paused').
    presence_state     -- snapshot for trace (raw SS value).
    emits              -- signals the caller should publish if it
                          decides to open the episode (PRESSURE_OPEN).
                          Empty when not opening.
    """

    aggregate: float
    contributors: Dict[str, float]
    open_threshold: float
    open_new_episode: bool
    suppress_reason: str = ""
    presence_state: Optional[str] = None
    emits: List[str] = field(default_factory=list)
    # Phase 2 §P2-S10: per-contributor detail. Empty for legacy
    # contributors (curiosity_gap, operator_intent, etc.); populated
    # for evidence_terminal with the selected ASK/HOLD terminal's
    # compact dispatch hint so Global can build a targeted
    # RESPONSE_HOLD action pack without re-scanning the durable
    # curiosity_store at the action-pack seam. Schema slot has
    # existed since S3 (to_snapshot() declared detail: {}) but was
    # not populated until P2-S10.
    contributor_details: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_snapshot(self) -> Dict[str, Any]:
        """Schema-aligned snapshot for episode.pressure on open.

        Mirrors symphony_episode_schema.json $.pressure: aggregate +
        contributors[] array of {id, weight, detail}. P2-S10 began
        populating per-contributor detail for evidence_terminal so
        the selected ASK/HOLD dispatch hint flows through the
        snapshot into Global; legacy contributors still pass {}."""
        snap: Dict[str, Any] = {
            "aggregate": self.aggregate,
            "contributors": [
                {
                    "id": cid,
                    "weight": w,
                    "detail": dict(self.contributor_details.get(cid) or {}),
                }
                for cid, w in self.contributors.items()
            ],
        }
        churn_detail = self.contributor_details.get("cognitive_churn") or {}
        if churn_detail:
            snap["cognitive_churn"] = dict(churn_detail)
        return snap


# ---------------------------------------------------------------------
# Terminal feedback — every pressure tick (guardian + operator terminal)
# ---------------------------------------------------------------------


def _pressure_tick_of(shared_state: SharedState) -> int:
    try:
        raw = shared_state.read("_pressure_tick", None)
        if raw is None:
            raw = shared_state.read("_epoch", 0)
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _contributors_summary_line(contributors: Dict[str, float]) -> str:
    ranked = sorted(
        ((str(k), float(v or 0.0)) for k, v in (contributors or {}).items()),
        key=lambda kv: (-kv[1], kv[0]),
    )
    parts = [f"{k}={v:.3f}" for k, v in ranked if v > 0.0001]
    return ",".join(parts) if parts else "all_zero"


def emit_pressure_evaluation_terminal(
    decision: "PressureDecision",
    shared_state: SharedState,
    *,
    raw_sum: float,
) -> None:
    """One log row per evaluate_pressure() — full contributor feed."""
    tick = _pressure_tick_of(shared_state)
    contrib_line = _contributors_summary_line(decision.contributors)
    summary = (
        f"tick={tick} agg={decision.aggregate:.3f} raw={raw_sum:.3f} "
        f"thresh={decision.open_threshold:.2f} "
        f"open={int(decision.open_new_episode)} "
        f"suppress={decision.suppress_reason or 'none'} "
        f"contrib=[{contrib_line}]"
    )[:240]
    log.info("pressure.evaluate %s", summary)


def emit_pressure_evaluation_skipped(
    shared_state: SharedState,
    *,
    reason: str,
) -> None:
    """Log when pressure evaluation is skipped (pause / early exit)."""
    tick = _pressure_tick_of(shared_state)
    log.info("pressure.evaluate skip tick=%s reason=%s", tick, reason)


# ---------------------------------------------------------------------
# evaluate_pressure
# ---------------------------------------------------------------------

DEFAULT_OPEN_THRESHOLD = 1.0  # hybrid winner ceiling (dominant must reach)


def _read_presence(shared_state: SharedState) -> str:
    """Fail-closed read of PRESENCE_STATE unless assume_operator_present."""
    from sensory.operator_presence import read_presence_from_shared_state

    return read_presence_from_shared_state(shared_state)


def _autonomy_paused(orchestrator) -> bool:
    """Best-effort autonomy-pause check via the orchestrator. None /
    missing method -> False so headless tests + harness paths don't
    silently suppress; production wires a real orchestrator."""
    if orchestrator is None:
        return False
    method = getattr(orchestrator, "is_autonomy_paused", None)
    if not callable(method):
        return False
    try:
        return bool(method())
    except Exception:
        return False


def evaluate_pressure(
    shared_state: SharedState,
    *,
    open_threshold: float = DEFAULT_OPEN_THRESHOLD,
    registry: Optional[Dict[str, ContributorReader]] = None,
    orchestrator=None,
    require_presence: bool = False,
    episode_store=None,
) -> PressureDecision:
    """Aggregate contributor pressures into a decision.

    Args:
      shared_state: live SS for contributor reads.
      open_threshold: dominant contributor must reach this ceiling
        with a clear margin over runner-up (hybrid gate; default 1.0).
      registry: contributor reader map. Defaults to built-in registry.
        S9: when set, threading `episode_store` does NOT inject the
        S9 neuromod precedence (caller-owned registry stays
        deterministic). When the registry is the built-in default,
        we wrap `read_neuromod_spike` with the supplied episode_store
        so episode-first precedence kicks in.
      orchestrator: optional Orchestrator for autonomy-pause check.
        None disables the autonomy gate (used by headless tests + the
        S3 harness when no orchestrator is available).
      require_presence: when True, the presence gate denies
        episode open unless PRESENCE_STATE == "PRESENT". Default is
        False — face/name attachment is for HUD + memory; the
        substrate does not lock behind operator presence. Proactive
        speech is also presence-blind (the retired response dispatcher did
        not consult PRESENCE_STATE). Tests may pass True to exercise
        the optional gate.
      episode_store: symphony S9 -- optional EpisodeStore so the
        neuromod_spike contributor can read the active episode's
        same-cycle `episode.auto["neuromod"]` (precedence #1 in
        `read_neuromod_spike`). The pressure loop (pressure_loop.loop) passes
        `orchestrator.episode_store` when available; tests + headless
        paths pass None to exercise the SS-slot fallback path.

    Returns:
      PressureDecision with full audit trail. NEVER raises -- a
      misbehaving contributor reader contributes 0.0 (read_* helpers
      already catch exceptions); a missing SS slot contributes 0.0.
    """
    # S9: when caller didn't supply a custom registry, wrap the
    # built-in neuromod reader with the supplied episode_store so
    # episode-first precedence works. Custom registries are
    # caller-owned (S3/S6/S7 isolation pattern); we don't second-guess
    # what they want.
    if registry is None:
        if episode_store is not None:
            from functools import partial

            readers = dict(_DEFAULT_READERS)
            readers[CONTRIBUTOR_NEUROMOD_SPIKE] = partial(
                read_neuromod_spike,
                episode_store=episode_store,
            )
            # Phase 2 §P2-S15: thread episode_store into the
            # idle_probe_aggregate so probes that consult episode
            # state (e.g., learning_debt composed from active
            # episode context) have it available. Test/custom
            # registries are caller-owned and skip this wrap.
            readers[CONTRIBUTOR_IDLE_PROBE_AGGREGATE] = partial(
                read_idle_probe_aggregate,
                episode_store=episode_store,
            )
        else:
            readers = _DEFAULT_READERS
    else:
        readers = registry

    contributors: Dict[str, float] = {}
    contributor_details: Dict[str, Dict[str, Any]] = {}
    for cid, reader in readers.items():
        try:
            v = reader(shared_state)
        except Exception as exc:
            log.warning(
                "pressure_field: contributor %r raised %s -- contributing 0.0",
                cid,
                type(exc).__name__,
            )
            v = 0.0
        contributors[cid] = _clamp_unit(v)

    # Phase 2 §P2-S10: evidence_terminal detail enrichment. The reader
    # returns a scalar (matches ContributorReader contract); the
    # selected terminal's compact dispatch hint comes from a separate
    # single-store-read helper invoked ONLY when the contributor fired.
    # Detail carries the selected terminal's curiosity_id +
    # terminal_kind + recommended_path + problem_statement (truncated)
    # into episode.pressure["contributors"][i]["detail"] so Global can
    # construct a targeted RESPONSE_HOLD pack without re-scanning the
    # durable store at the action-pack seam.
    if contributors.get(CONTRIBUTOR_EVIDENCE_TERMINAL, 0.0) > 0.0:
        try:
            selected = _selected_evidence_terminal()
        except Exception as exc:
            log.debug(
                "pressure_field: evidence_terminal detail enrichment skipped (%s)",
                exc,
            )
            selected = None
        if selected is not None:
            contributor_details[CONTRIBUTOR_EVIDENCE_TERMINAL] = selected

    # Phase 2 §P2-S19: epistemic_emptiness detail enrichment. The
    # reader records the WHY of its return value on
    # `_LAST_EPISTEMIC_EMPTINESS_DETAIL` each call; we surface that
    # under contributor_details["epistemic_emptiness"] so consumers
    # (Global selector, archive snapshots, WAL) can see whether the
    # contributor is in "idle_no_new_evidence" (busy-wait suppressed),
    # "substrate_changed" (fresh signal), "refractory_window_expired"
    # (one more allowed), "cold_start" (first read), or "above_baseline"
    # (substrate full). Detail surfaces independent of weight so the
    # "0.0 idle" tick still carries explanatory state for diagnostics.
    if _LAST_EPISTEMIC_EMPTINESS_DETAIL:
        contributor_details[CONTRIBUTOR_EPISTEMIC_EMPTINESS] = dict(
            _LAST_EPISTEMIC_EMPTINESS_DETAIL
        )

    # Phase 2 §P2-S15: idle_probe_aggregate detail enrichment. The
    # reader cached its findings list on `_LAST_IDLE_PROBE_FINDINGS`
    # this evaluate_pressure call; here we build the compact summary
    # Global action selection branches on:
    #   - probe_classes_seen: sorted unique class names from findings
    #   - has_meaningful_class: True iff any finding's probe_class is
    #     in symphony_probes.MEANINGFUL_PROBE_CLASSES (per packet RB#4)
    #   - selected_meaningful_finding: compact dict of the highest-
    #     weight meaningful finding (Global outlet uses this to
    #     route to RESPONSE_HOLD without rescanning substrates)
    # Cognitive churn (2026-05-28): one synchronous assessment per tick
    # so habituated domains/goals attenuate curiosity + learning_debt
    # pressure instead of re-opening the same lane forever.
    try:
        from autonomic.cognitive_churn import (
            assess_cognitive_churn,
            primary_learning_debt_domain,
        )

        churn = assess_cognitive_churn()
        if contributors.get(CONTRIBUTOR_CURIOSITY_GAP, 0.0) > 0.0:
            scale = churn.curiosity_pressure_multiplier()
            contributors[CONTRIBUTOR_CURIOSITY_GAP] = _clamp_unit(
                contributors[CONTRIBUTOR_CURIOSITY_GAP] * scale,
            )
            if scale < 1.0:
                contributor_details.setdefault(CONTRIBUTOR_CURIOSITY_GAP, {})
                contributor_details[CONTRIBUTOR_CURIOSITY_GAP]["churn_scale"] = round(
                    scale,
                    3,
                )
                contributor_details[CONTRIBUTOR_CURIOSITY_GAP]["churn_saturated_goals"] = sorted(
                    churn.saturated_goal_ids,
                )
        if contributors.get(CONTRIBUTOR_LEARNING_DEBT, 0.0) > 0.0:
            try:
                from pressure_loop.symphony_learning_state import build_episode_learning_state

                envelope = build_episode_learning_state()
            except Exception:
                envelope = None
            dom = primary_learning_debt_domain(envelope)
            scale = churn.learning_debt_multiplier(primary_domain=dom)
            contributors[CONTRIBUTOR_LEARNING_DEBT] = _clamp_unit(
                contributors[CONTRIBUTOR_LEARNING_DEBT] * scale,
            )
            if scale < 1.0:
                contributor_details.setdefault(CONTRIBUTOR_LEARNING_DEBT, {})
                contributor_details[CONTRIBUTOR_LEARNING_DEBT]["churn_scale"] = round(
                    scale,
                    3,
                )
                contributor_details[CONTRIBUTOR_LEARNING_DEBT]["churn_domain"] = dom
        contributor_details["cognitive_churn"] = churn.to_dict()
    except Exception as exc:
        log.debug("pressure_field: cognitive_churn skipped (%s)", exc)

    if contributors.get(CONTRIBUTOR_IDLE_PROBE_AGGREGATE, 0.0) > 0.0:
        try:
            from pressure_loop.symphony_probes import MEANINGFUL_PROBE_CLASSES
        except Exception:
            MEANINGFUL_PROBE_CLASSES = frozenset()  # noqa: N806
        findings_snapshot = list(_LAST_IDLE_PROBE_FINDINGS)
        classes_seen = sorted(
            {
                str(f.get("probe_class") or "")
                for f in findings_snapshot
                if isinstance(f, dict) and f.get("probe_class")
            }
        )
        meaningful_findings = [
            f
            for f in findings_snapshot
            if isinstance(f, dict) and str(f.get("probe_class") or "") in MEANINGFUL_PROBE_CLASSES
        ]
        meaningful_findings.sort(
            key=lambda f: float(f.get("weight") or 0.0),
            reverse=True,
        )
        selected_meaningful = dict(meaningful_findings[0]) if meaningful_findings else {}
        contributor_details[CONTRIBUTOR_IDLE_PROBE_AGGREGATE] = {
            "probe_classes_seen": classes_seen,
            "has_meaningful_class": bool(meaningful_findings),
            "selected_meaningful_finding": selected_meaningful,
            "finding_count": len(findings_snapshot),
        }

    # 2026-06-01 Slice 2: tool_creation_need detail enrichment. The reader
    # stashed the selected curiosity's compact on _LAST_TOOL_CREATION_NEED_
    # DETAIL this call; surface it so the action pack can stamp a mint_tool
    # dispatch_hint carrying curiosity_id + capability_gap.
    if (
        contributors.get(CONTRIBUTOR_TOOL_CREATION_NEED, 0.0) > 0.0
        and _LAST_TOOL_CREATION_NEED_DETAIL
    ):
        contributor_details[CONTRIBUTOR_TOOL_CREATION_NEED] = dict(_LAST_TOOL_CREATION_NEED_DETAIL)

    raw_sum = sum(contributors.values())
    # Building-pressure drive: INTEGRATE this tick's contributor sum onto the
    # carried-forward accumulator (NO decay — operator doctrine: pressure
    # builds until it ignites an episode, then resets ONLY on that episode's
    # death). Clamp ceiling 1.0. This replaces the old per-tick snapshot
    # `aggregate = clamp(raw_sum)` that pinned at the idle sum (~0.15) and
    # never built to open_threshold. `raw_sum` is kept as the INSTANTANEOUS
    # contribution (exposed in contributor_details["_drive"] for snapshots/
    # telemetry / tests that assert this-tick contribution).
    accumulated = _clamp_unit(_PRESSURE_ACCUMULATOR["accumulated"] + raw_sum)
    _PRESSURE_ACCUMULATOR["accumulated"] = accumulated
    dominant_w = max(contributors.values(), default=0.0)
    aggregate = _clamp_unit(dominant_w)
    contributor_details["_drive"] = {
        "raw_sum": round(float(raw_sum), 6),
        "accumulated": round(float(accumulated), 6),
        "dominant": round(float(dominant_w), 6),
    }

    presence_state = _read_presence(shared_state) if require_presence else None
    suppress = ""

    from pressure_loop.pressure import (
        _hybrid_effective_gate_params,
        _neuromod_from_last_body,
        hybrid_ignition_gate,
    )

    nm = _neuromod_from_last_body(episode_store)
    eff_ceiling, eff_margin = _hybrid_effective_gate_params(
        nm,
        ceiling_override=open_threshold,
    )
    contributor_details["_neuromod"] = {
        "dopamine": round(float(nm.get("dopamine", 0.0)), 4),
        "serotonin": round(float(nm.get("serotonin", 0.0)), 4),
        "stress": round(float(nm.get("stress", 0.0)), 4),
        "effective_ceiling": round(float(eff_ceiling), 4),
        "effective_margin": round(float(eff_margin), 4),
    }
    open_ok, hybrid_suppress, _dominant_id = hybrid_ignition_gate(
        contributors,
        ceiling=eff_ceiling,
        margin=eff_margin,
        neuromod=nm,
    )
    if not open_ok:
        suppress = hybrid_suppress or "below_ceiling"
        decision_open = False
    elif _autonomy_paused(orchestrator):
        suppress = "autonomy_paused"
        decision_open = False
    elif require_presence and presence_state != "PRESENT":
        suppress = f"presence_{(presence_state or 'unknown').lower()}"
        decision_open = False
    else:
        decision_open = True
    # NOTE: the legacy `autonomy_paused` suppress branch is preserved
    # for backward-compat with the bridge's per-run / per-mode pauses
    # and the S3/S6/S9 regression test contracts. The NEW v2 substrate
    # pressure-loop pause (driven by the dev pill) gates upstream at
    # CueDesk.tick() entry via pressure_loop.pressure_loop_pause — when that
    # gate fires, evaluate_pressure isn't even called. Both gates coexist
    # cleanly: pressure_loop_paused dominates; legacy autonomy_paused
    # fires only when evaluate_pressure runs at all.
    #
    # NOTE 2 (2026-05-26 operator doctrine): the presence gate above
    # is OFF by default. Episode opening is presence-blind — the
    # substrate is fully autonomous and runs cognitive episodes from
    # endogenous pressure with no requirement that the operator be
    # at the camera. Presence ONLY gates proactive speech-to-operator
    # dispatch at core/response_engine.py:should_speak_now() (the
    # P3-S02 surface). Reactive paths + internal cognition never
    # consult presence. The `require_presence=True` branch above is
    # retained as a tool for legacy / test paths that want to assert
    # the gate firing.

    emits = [SymphonySignal.PRESSURE_OPEN.value] if decision_open else []

    decision = PressureDecision(
        aggregate=aggregate,
        contributors=contributors,
        open_threshold=open_threshold,
        open_new_episode=decision_open,
        suppress_reason=suppress,
        presence_state=presence_state,
        emits=emits,
        contributor_details=contributor_details,
    )
    try:
        emit_pressure_evaluation_terminal(
            decision,
            shared_state,
            raw_sum=raw_sum,
        )
    except Exception:
        pass
    return decision
