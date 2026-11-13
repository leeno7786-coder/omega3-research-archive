"""
SharedState canonical key schema (SLICE-2)
============================================

Per `docs/DOCTRINE_V2.md` §"Shared state (the inter-loop wire)":
the Orchestrator's SharedState is the data passing surface between
loops. Every loop reads upstream loops' outputs through it and writes
its own output for downstream consumers.

This module is the **contract for that surface** — every canonical key
declares:
  - the WRITER subsystem (who is allowed to write it)
  - the READERS subsystems (who consumes it; documentary, not enforced)
  - a SHAPE VALIDATOR (what the value must look like)
  - a DESCRIPTION (what the key means)

`SharedState.write_canonical(key, value)` enforces the schema at write
time. Ad-hoc / test writes can still use `SharedState.write(key, value)`
which is permissive (any key, any value) — that surface stays so REPAIR-1
tests + future test scaffolding don't break.

Per the contract's "deterministic over magical" directive: every write
to a canonical key validates; no silent shape drift.

ARCHITECTURAL RULES:
    - The 9 canonical keys are exhaustive for the inter-loop wire.
      Adding a new canonical key = doctrine-grain change (memory file +
      DOCTRINE_V2 + this registry + downstream slice work).
    - Validators are deliberately LIGHT. They check the dict shape +
      required top-level keys + obvious type errors. Per-loop output
      schemas live in each loop's own module — duplicating them here
      would couple SharedState to every loop's contract.
    - This module MUST NOT be redefined elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
import logging

log = logging.getLogger("omega.core.shared_state_keys")


# ---------------------------------------------------------------------
# Phase 2 §P2-S17 — SharedState restart-integrity classification.
#
# Per PACKET_P2_S17.md RB#1: every product-read SharedState key carries
# a classification that controls its restart-survivability behavior.
# Four classes:
#
#   durable_projection      — must survive a fresh Orchestrator restart
#                             via Guardian state_persistence. Pressure,
#                             Global, response-hold, Omega, DeepSleep,
#                             temporal context, learning state, and
#                             runtime capability receipts treat these
#                             as live evidence.
#   tick_boundary_projection — captured per-tick (typically via tick-
#                             state WAL); not relied on across full
#                             restart. Examples: TTL-decayed pressure
#                             scalars that the next tick recomputes.
#   ephemeral_runtime       — in-process only. Operator presence latch,
#                             transient cog/affect signals. Restart
#                             must NOT restore these as authority.
#   forbidden_to_persist    — must never reach durable storage. Raw
#                             model output, image/audio bytes,
#                             embeddings, stack traces.
#
# The classification flows through `KeyContract.classification` and is
# consumed by:
#   * guardian.state_persistence._persisted_keys (filter)
#   * boot.run_continuous (per-tick save_now)
#   * core.shared_state_classification probe surface (audit + scenario)
# ---------------------------------------------------------------------


class Classification(str, Enum):
    """SharedState key restart-survivability classification (RB#1)."""

    DURABLE_PROJECTION = "durable_projection"
    TICK_BOUNDARY_PROJECTION = "tick_boundary_projection"
    EPHEMERAL_RUNTIME = "ephemeral_runtime"
    FORBIDDEN_TO_PERSIST = "forbidden_to_persist"


# Substring tokens that, when present at any key inside a persisted
# value's dict shape, force the value to be classified as
# FORBIDDEN_TO_PERSIST regardless of the key's contract. Raw payloads,
# blobs, embeddings, and exception payloads must never reach disk
# even if a canonical key's contract accidentally accepts them.
FORBIDDEN_PAYLOAD_KEY_TOKENS: tuple[str, ...] = (
    "raw_model_output",
    "raw_payload",
    "raw_response_text",
    "model_output_blob",
    "embedding",
    "embeddings",
    "image_bytes",
    "audio_bytes",
    "video_bytes",
    "stack_trace",
    "traceback",
)


# ---------------------------------------------------------------------
# Canonical key constants
# ---------------------------------------------------------------------

LAST_BRAIN_RESPONSE = "last_brain_response"
# S2 live wire: operator presence watcher writes each frame; W1 reads
# (default UNKNOWN when absent -> fail-closed).
PRESENCE_STATE = "presence_state"
# P3-S04 — heartbeat side-channel for operator-presence watcher
# liveness. Written by sensory.operator_presence.run_watcher each tick
# alongside PRESENCE_STATE (ISO timestamp string). Read by
# hud.api_presence_state to compute `watcher_stale` for the identity
# pill — stale value means the watcher process is dead or starved,
# which the operator needs to see (the gate doesn't distinguish
# "watcher healthy + holding UNKNOWN" from "watcher dead + state
# frozen at last value"). Diagnostic ONLY — never gates presence
# decisions.
PRESENCE_WATCHER_HEARTBEAT = "presence_watcher_heartbeat"
# P3-S06 — CueDesk last-tick snapshot for the HUD brain/autoloop panels.
# Written by `core.cue_desk._emit_tick_phase` at the end of every tick
# (every return path in CueDesk.tick goes through that helper). Read by
# `hud.api_cuedesk_last_tick` so the HUD can render "what just happened
# this tick" — suppress reason, opened_by contributor, idle probes
# count, pause owners — without scraping feedback_audit.jsonl.
# Tick-boundary projection (per-tick snapshot, never reaches disk via
# Guardian SS restore — a frozen restore would lie about live state).
LAST_CUEDESK_TICK = "last_cuedesk_tick"
LAST_AUTO_CYCLE = "last_auto_cycle"
LAST_GLOBAL_OBSERVATIONS = "last_global_observations"
LAST_GLOBAL_GOALS = "last_global_goals"
LAST_META_ORDERS = "last_meta_orders"
LAST_OMEGA_EVALUATION = "last_omega_evaluation"
LAST_OMEGA_PROPOSALS = "last_omega_proposals"
# 2026-05-14: long-arc directional pressure from omega. Multi-epoch
# influence on global's goal formation. Omega is the sole writer;
# global reads each cycle; deepsleep snapshots into journal upstream.
# This slot is INTENTIONALLY NOT cleared by deepsleep's cascade reset
# — trajectories are multi-epoch by design.
LAST_OMEGA_TRAJECTORY = "last_omega_trajectory"
# P3-S16: live in-memory ring buffer of recent omega-cycle commits.
# HUD's Trajectory Star Map (§9.5) reads this — the panel rides the
# live server feed, NOT the disk audit log. Boot starts empty by
# construction (EPHEMERAL_RUNTIME — does not survive boot); fills as
# omega cycles commit. omega_audit.jsonl on disk remains the
# write-only forensic record (audit-log-is-write-only doctrine —
# audit is "write-only forever, never re-read as live").
RECENT_OMEGA_EPOCHS = "recent_omega_epochs"
# P3-S25 HUD live-feed doctrine v2 — ring of last 5 DeepSleep
# consolidation cycle records. HUD's panel_deepsleep reads this instead
# of tailing guardian/logs/deep_sleep_audit.jsonl. Written by
# metacognition.deep_sleep.phases.phase_4_persistence.audit_logger.log_cycle
# AFTER the disk audit write lands (doctrine: audit is source of truth,
# ring is live feed). EPHEMERAL_RUNTIME — boot empty, fills as cycles
# consolidate. Restored stale entries would render dead cycles as live.
RECENT_DEEPSLEEP_CYCLES = "recent_deepsleep_cycles"
# P3-S25 HUD live-feed doctrine v2 — ring of last 50 guardian
# intervention records. The HUD guardian panel that read this was
# removed; the ring is still written by
# guardian.decision.record_intervention AFTER the disk write lands.
# EPHEMERAL_RUNTIME — same restart-empty rationale.
RECENT_GUARDIAN_INTERVENTIONS = "recent_guardian_interventions"
# P3-S28 HUD live-feed doctrine v3 — ring of last 30 layer-state records.
# HUD's _telemetry_cycle reads this instead of tail-reading
# guardian/logs/layer_state.jsonl every 2s (the most-polled disk read
# pre-cure). Written by core.layer_state.emit_layer_state AFTER the disk
# write lands. EPHEMERAL_RUNTIME — boot empty (live cycle state is by
# definition session-bound; a restored stale ring would render dead
# cycles as live).
RECENT_LAYER_STATES = "recent_layer_states"
# P3-S28 HUD live-feed doctrine v3 — ring of last 20 self-vector history
# entries. HUD's _telemetry_omega (coherence + drift) and panel_identity
# (trajectory) read this instead of tail-reading
# guardian/state/identity/self_vector_history.jsonl. Written by
# memory.self_vector.record_self_vector AFTER the disk write lands.
# EPHEMERAL_RUNTIME — boot empty by construction.
RECENT_SELF_VECTORS = "recent_self_vectors"
# FINAL VERDICT law (2026-05-16): meta disposition truth + curiosity
# pressure truth. Same-cycle outcome invariant — every goal terminates
# sent_to_auto|failed_to_initiate, surfaced here for global+deepsleep;
# DeepSleep persists ALL LAST_* before SharedState clear.
LAST_META_CYCLE = "last_meta_cycle"
LAST_CURIOSITY_CYCLE = "last_curiosity_cycle"
OUTSTANDING_USER_QUESTION = "outstanding_user_question"
CONVERSATION_STATE = "conversation_state"
# Symphony S6 — operator learning directive surface. Receiver publishes
# a deterministic record when it detects 'learn X' / 'study X' / etc.;
# pressure_field.read_operator_intent + cue_desk consume it to open
# an episode with opened_by='operator_intent' AND seed
# LAST_OMEGA_TRAJECTORY before META_A runs. TTL-based decay so stale
# directives don't keep opening episodes forever.
OPERATOR_DIRECTIVE = "operator_directive"

# Phase 2 §P2-S12: temporal context for episode open.
# Captured at tick boundary and threaded onto episode.temporal.
# Writer: cognition_chain calls core.episode_temporal.capture_temporal_context.
# Readers: episode.temporal field carries this envelope through the loop.


# ---------------------------------------------------------------------
# Per-key contract
# ---------------------------------------------------------------------

ShapeValidator = Callable[[Any], Optional[str]]


@dataclass(frozen=True)
class KeyContract:
    """The contract for a single canonical shared_state key.

    Attributes:
        name:            the key string itself.
        writer:          subsystem name allowed to write this key
                         (e.g., "brain.runner", "omega_loop"). Documentary.
        readers:         subsystems that consume this key. Documentary.
        description:     what the key means.
        shape_validator: callable taking the to-be-written value;
                         returns None if valid OR an error string
                         describing the violation.
        classification:  Phase 2 §P2-S17 — restart-survivability class.
                         Default DURABLE_PROJECTION because every key
                         landing in this registry through P2-S16 has
                         been deliberately exposed as same-cycle truth
                         the substrate consumes; the explicit defaults
                         below override to TICK_BOUNDARY or EPHEMERAL
                         where the key is a transient/runtime signal
                         that pressure recomputes from substrate state
                         on the next tick.
    """

    name: str
    writer: str
    readers: List[str]
    description: str
    shape_validator: ShapeValidator
    classification: Classification = Classification.DURABLE_PROJECTION


# ---------------------------------------------------------------------
# Light shape validators
# ---------------------------------------------------------------------


def _require_dict(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return f"expected dict, got {type(value).__name__}"
    return None


def _require_keys(value: dict, required: set) -> Optional[str]:
    missing = required - set(value.keys())
    if missing:
        return f"missing required keys: {sorted(missing)}"
    return None


def _validate_brain_response(value: Any) -> Optional[str]:
    err = _require_dict(value)
    if err:
        return err
    return _require_keys(value, {"cycle_id", "source", "message"})


# 2026-05-14 — canonical CycleEnvelope shape (see schema.cycle_envelope).
# Every loop adapter writes its slot as a CycleEnvelope serialized to dict
# via `envelope.to_dict()`. The validators below enforce that contract:
# upstream is byte-for-byte from the layer below (or {} for auto), phases
# carries the per-phase outputs, telemetry carries the headline facts.
_ENVELOPE_REQUIRED = {"layer", "cycle_id", "epoch", "upstream", "phases", "telemetry"}


def _validate_envelope(value: Any, expected_layer: str) -> Optional[str]:
    """Shared validator for slot writes that conform to CycleEnvelope.

    Checks: dict shape, all required outer fields present, layer matches
    the slot's writer, upstream/phases/telemetry are dicts. Does NOT
    inspect telemetry contents — those are layer-specific and free-form
    by design (the SHAPE is what's locked, the contents flow with the work).
    """
    err = _require_dict(value)
    if err:
        return err
    err = _require_keys(value, _ENVELOPE_REQUIRED)
    if err:
        return err
    if value["layer"] != expected_layer:
        return f"envelope.layer must be {expected_layer!r}, got {value['layer']!r}"
    for k in ("upstream", "phases", "telemetry"):
        if not isinstance(value[k], dict):
            return f"envelope.{k} must be a dict, got {type(value[k]).__name__}"
    return None


def _validate_auto_cycle(value: Any) -> Optional[str]:
    return _validate_envelope(value, "auto")


def _validate_global_observations(value: Any) -> Optional[str]:
    return _validate_envelope(value, "global")


def _validate_global_goals(value: Any) -> Optional[str]:
    # Legacy goals-only slot kept by global runner for narrow consumers
    # (deepsleep aggregator, meta intake fallback). Not a CycleEnvelope.
    err = _require_dict(value)
    if err:
        return err
    err = _require_keys(value, {"cycle_id", "goals"})
    if err:
        return err
    if not isinstance(value["goals"], list):
        return "goals must be a list"
    return None


def _validate_meta_orders(value: Any) -> Optional[str]:
    return _validate_envelope(value, "meta")


def _validate_omega_evaluation(value: Any) -> Optional[str]:
    err = _require_dict(value)
    if err:
        return err
    return _require_keys(value, {"epoch", "status"})


def _validate_omega_proposals(value: Any) -> Optional[str]:
    err = _require_dict(value)
    if err:
        return err
    err = _require_keys(value, {"epoch", "proposals"})
    if err:
        return err
    if not isinstance(value["proposals"], list):
        return "proposals must be a list"
    return None


def _validate_omega_trajectory(value: Any) -> Optional[str]:
    """Validate the OmegaTrajectory shape on slot writes.

    The slot may carry None when no trajectory is active OR a dict
    matching `schema.omega_trajectory.OmegaTrajectory.to_dict()`.
    Both are valid: None means "no active trajectory."
    """
    if value is None:
        return None
    err = _require_dict(value)
    if err:
        return err
    err = _require_keys(
        value,
        {
            "trajectory_id",
            "cycle_id",
            "epoch_emitted",
            "intent",
            "target_domain",
            "target_capability",
            "rationale",
            "influencer_text",
            "persistence_cycles",
            "cycles_remaining",
            "completed",
        },
    )
    if err:
        return err
    valid_intents = {
        "exercise_new_tool",
        "explore_domain",
        "investigate_anomaly",
        "deepen_capability",
    }
    if value["intent"] not in valid_intents:
        return f"intent must be one of {sorted(valid_intents)}, got {value['intent']!r}"
    if not isinstance(value["persistence_cycles"], int) or value["persistence_cycles"] < 1:
        return "persistence_cycles must be a positive int"
    if not isinstance(value["cycles_remaining"], int) or value["cycles_remaining"] < 0:
        return "cycles_remaining must be a non-negative int"
    return None


# Same-cycle outcome dispositions per symphony_episode_schema.json
# ($defs/grounded_goal + goals[].disposition enum, lines 105-111):
#   sent_to_auto          -- meta dispatched + auto succeeded (or auto
#                            outcome not observed at meta time -- the
#                            pre-S2 two-arm legacy default)
#   failed_to_initiate    -- meta did NOT dispatch (no tool/order
#                            mapped); IS a failure record
#   auto_execution_failed -- meta dispatched cleanly, but auto's
#                            same-cycle outcome was non-success
#                            (added by symphony S2 META_B; closes
#                            INTEGRATION_MAP §3.2 GAP)
_META_DISPOSITIONS = {"sent_to_auto", "failed_to_initiate", "auto_execution_failed"}
# Single source of truth — the failure-record schema owns the cause-
# resolution enum (schema rule: "MUST NOT be redefined elsewhere"). We
# consume, not redefine. FINAL VERDICT hardening #1.
try:
    from schema.failure_record import (  # noqa: E402
        CAUSE_RESOLUTION_STATES as _CAUSE_RESOLUTION_STATES,
    )
except ModuleNotFoundError:
    _CAUSE_RESOLUTION_STATES = frozenset(
        {"resolved", "unresolved", "deferred", "escalated"},
    )

# Dispositions that ARE failure records and MUST carry the full causal
# evidence + hardening fields. sent_to_auto is the only success
# disposition; the other two both record a failure (one at the dispatch
# boundary, the other inside auto's execution).
_META_FAILURE_DISPOSITIONS = {"failed_to_initiate", "auto_execution_failed"}


def _validate_meta_cycle(value: Any) -> Optional[str]:
    """Meta disposition truth (FINAL VERDICT law + symphony S2 cure).
    Same-cycle outcome invariant: every goal terminates as EXACTLY ONE
    of sent_to_auto | failed_to_initiate | auto_execution_failed — no
    silent drops. Both failure dispositions ARE failure records and
    MUST carry causal evidence + hardening fields (cause_resolution_state
    #1, failure_fingerprint #2)."""
    # None is the cleared-slot state. DeepSleep persists this slot
    # before clear; the clear writes None (loop_adapters.py). Convention
    # mirrors _validate_omega_trajectory.
    if value is None:
        return None
    err = _require_dict(value)
    if err:
        return err
    err = _require_keys(value, {"cycle_id", "goals"})
    if err:
        return err
    goals = value["goals"]
    if not isinstance(goals, list):
        return "goals must be a list"
    for i, g in enumerate(goals):
        if not isinstance(g, dict):
            return f"goals[{i}] must be a dict"
        gk = _require_keys(g, {"source_goal_id", "disposition"})
        if gk:
            return f"goals[{i}]: {gk}"
        disp = g.get("disposition")
        if disp not in _META_DISPOSITIONS:
            return (
                f"goals[{i}].disposition must be one of "
                f"{sorted(_META_DISPOSITIONS)} (same-cycle outcome "
                f"invariant), got {disp!r}"
            )
        if disp in _META_FAILURE_DISPOSITIONS:
            fk = _require_keys(
                g,
                {
                    "reason",
                    "cause",
                    "cause_resolution_state",
                    "failure_fingerprint",
                },
            )
            if fk:
                return f"goals[{i}] {disp}: {fk}"
            crs = g.get("cause_resolution_state")
            if crs not in _CAUSE_RESOLUTION_STATES:
                return (
                    f"goals[{i}].cause_resolution_state must be one "
                    f"of {sorted(_CAUSE_RESOLUTION_STATES)}, "
                    f"got {crs!r}"
                )
    return None


def _validate_curiosity_cycle(value: Any) -> Optional[str]:
    """Pressure truth (FINAL VERDICT law). Curiosity owns deterministic
    pressure computation only; the damping invariant
    (pressure_next <= pressure_prev + bounded_delta unless catastrophic)
    is enforced at the writer, recorded here for global+deepsleep."""
    # None is the cleared-slot state. DeepSleep persists this slot
    # before clear; the clear writes None (loop_adapters.py). Convention
    # mirrors _validate_omega_trajectory.
    if value is None:
        return None
    err = _require_dict(value)
    if err:
        return err
    return _require_keys(value, {"cycle_id", "pressure"})


def _validate_conversation_state(value: Any) -> Optional[str]:
    """CONVERSATION_STATE shape — short-horizon conversational pressure
    state. Tracks turns_since_user_spoke, turns_since_system_spoke,
    recent_dialogue_acts (list of last N), recent_user_replies (list of
    last N), user_engagement_estimate (0..1), last_updated (ISO).

    Read by core.dialogue_act_selector + core.reply_evaluator (for
    repetition detection). Updated by core.brain_receiver on user
    input + brain.respond on system utterance. TTL-evicted to defaults
    after 1 hour of total silence — a fresh conversation."""
    err = _require_dict(value)
    if err:
        return err
    err = _require_keys(
        value,
        {
            "turns_since_user_spoke",
            "turns_since_system_spoke",
            "recent_dialogue_acts",
            "recent_user_replies",
            "user_engagement_estimate",
            "last_updated",
        },
    )
    if err:
        return err
    if not isinstance(value["turns_since_user_spoke"], int):
        return "turns_since_user_spoke must be int"
    if not isinstance(value["turns_since_system_spoke"], int):
        return "turns_since_system_spoke must be int"
    if not isinstance(value["recent_dialogue_acts"], list):
        return "recent_dialogue_acts must be list"
    if not isinstance(value["recent_user_replies"], list):
        return "recent_user_replies must be list"
    if not isinstance(value["user_engagement_estimate"], (int, float)):
        return "user_engagement_estimate must be number"
    if not isinstance(value["last_updated"], str) or not value["last_updated"]:
        return "last_updated must be non-empty ISO-8601 string"
    return None


def _validate_outstanding_user_question(value: Any) -> Optional[str]:
    """OUTSTANDING_USER_QUESTION shape — set by brain_runner after a
    proactive cycle successfully delivers a question; consumed by
    brain_receiver.handle_user_input on the next user input. TTL-bounded
    by `asked_at` + `ttl_seconds`."""
    err = _require_dict(value)
    if err:
        return err
    # `consumed` marker is the post-consumption write — accept it.
    if value.get("consumed") is True:
        return None
    err = _require_keys(
        value,
        {"curiosity_id", "question_text", "asked_at", "brain_cycle_id", "ttl_seconds"},
    )
    if err:
        return err
    if not isinstance(value["asked_at"], str) or not value["asked_at"]:
        return "asked_at must be non-empty ISO-8601 string"
    if not isinstance(value["ttl_seconds"], (int, float)) or value["ttl_seconds"] <= 0:
        return "ttl_seconds must be a positive number"
    return None


def _validate_operator_directive(value: Any) -> Optional[str]:
    """OPERATOR_DIRECTIVE shape — symphony S6. Receiver publishes when
    it detects a learning directive ('learn X' / 'study X' / etc.);
    pressure_field.read_operator_intent + cue_desk consume to open an
    episode with opened_by='operator_intent' AND seed
    LAST_OMEGA_TRAJECTORY before META_A runs. TTL-bounded so stale
    directives don't keep opening episodes.

    None is the cleared-slot state -- the receiver can publish None
    when explicitly retiring a directive (rare; normally TTL handles
    it). Convention mirrors _validate_omega_trajectory's None-accepts."""
    if value is None:
        return None
    err = _require_dict(value)
    if err:
        return err
    err = _require_keys(value, {"text", "domain", "weight", "issued_at"})
    if err:
        return err
    if not isinstance(value["text"], str) or not value["text"].strip():
        return "text must be a non-empty string"
    if not isinstance(value["domain"], str):
        return "domain must be a string (use '' when unknown)"
    if not isinstance(value["weight"], (int, float)) or not (0.0 <= float(value["weight"]) <= 1.0):
        return "weight must be a number in [0, 1]"
    if not isinstance(value["issued_at"], str) or not value["issued_at"]:
        return "issued_at must be a non-empty ISO-8601 string"
    # ttl_seconds optional; when present must be positive
    if "ttl_seconds" in value:
        ttl = value["ttl_seconds"]
        if not isinstance(ttl, (int, float)) or ttl <= 0:
            return "ttl_seconds (when present) must be a positive number"
    return None


# ---------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------

KEY_REGISTRY: Dict[str, KeyContract] = {
    LAST_BRAIN_RESPONSE: KeyContract(
        name=LAST_BRAIN_RESPONSE,
        writer="brain.runner",
        # deepsleep_loop reads this via deepsleep_engine._UPSTREAM_SLOTS
        # (records-only consolidation). `readers` is documentary, so the
        # honest invariant is: every (subsystem,key) in _UPSTREAM_SLOTS
        # has "deepsleep_loop" here. Was omitted (audit pattern-#2).
        readers=["autonomous_loop", "global_loop", "omega_loop", "deepsleep_loop"],
        description=(
            "Most recent brain cycle result dict — cycle_id, source, "
            "message, delivery, structured_response, memory_refs, "
            "verification (autonomous-source only)."
        ),
        shape_validator=_validate_brain_response,
    ),
    PRESENCE_STATE: KeyContract(
        name=PRESENCE_STATE,
        writer="sensory.operator_presence",
        readers=["autonomous_engine"],
        description=(
            "Latest operator presence latch (PRESENT|ABSENT|UNKNOWN). "
            "Written by the presence watcher; absent -> UNKNOWN fail-closed."
        ),
        shape_validator=lambda v: (
            None
            if v in (None, "PRESENT", "ABSENT", "UNKNOWN")
            else "presence_state must be PRESENT|ABSENT|UNKNOWN"
        ),
        # P2-S17: live face/presence latch — the watcher publishes per
        # frame. A restored stale "PRESENT" from hours ago would be
        # authority-incorrect; reads must fail-closed to UNKNOWN until
        # the live watcher republishes.
        classification=Classification.EPHEMERAL_RUNTIME,
    ),
    LAST_CUEDESK_TICK: KeyContract(
        name=LAST_CUEDESK_TICK,
        writer="core.cue_desk",
        readers=["hud.api_cuedesk_last_tick"],
        description=(
            "Per-tick snapshot of CueDesk.tick's CueDeskReport — the "
            "HUD brain + autoloop panels read this to render 'what "
            "just happened this tick' (suppress_reason, opened_by, "
            "idle_probes_run, pause_owners). Tick-boundary projection "
            "so a Guardian restore can't replay a frozen tick state "
            "as live."
        ),
        shape_validator=lambda v: (
            None if v is None or isinstance(v, dict) else "last_cuedesk_tick must be dict or None"
        ),
        classification=Classification.TICK_BOUNDARY_PROJECTION,
    ),
    PRESENCE_WATCHER_HEARTBEAT: KeyContract(
        name=PRESENCE_WATCHER_HEARTBEAT,
        writer="sensory.operator_presence",
        readers=["hud.api_presence_state"],
        description=(
            "Per-tick ISO timestamp from the operator-presence watcher. "
            "Stale value (>90s old) means the watcher process is dead or "
            "starved. Diagnostic ONLY — never gates presence decisions. "
            "The PRESENCE_STATE latch is authoritative for the speech "
            "gate; this side-channel just lets the HUD show the operator "
            "that the latch is current."
        ),
        shape_validator=lambda v: (
            None
            if v is None or isinstance(v, str)
            else "presence_watcher_heartbeat must be ISO string"
        ),
        # Ephemeral: a restored stale heartbeat from hours ago is
        # worse than no heartbeat (would silently mask a dead watcher).
        # Same fail-closed rationale as PRESENCE_STATE.
        classification=Classification.EPHEMERAL_RUNTIME,
    ),
    LAST_AUTO_CYCLE: KeyContract(
        name=LAST_AUTO_CYCLE,
        writer="autonomous_loop",
        readers=["global_loop", "omega_loop", "deepsleep_loop"],
        description=(
            "Most recent autonomous cycle summary — cycle_id, status, "
            "neuromodulator_signals (dopamine_score, plasticity_strength, "
            "strength_index, stress_level)."
        ),
        shape_validator=_validate_auto_cycle,
    ),
    LAST_GLOBAL_OBSERVATIONS: KeyContract(
        name=LAST_GLOBAL_OBSERVATIONS,
        writer="global_loop",
        readers=["meta_loop", "omega_loop", "deepsleep_loop"],
        description=(
            "Most recent global reflection observations — cycle_id, "
            "observations (list of observation strings or dicts)."
        ),
        shape_validator=_validate_global_observations,
    ),
    LAST_GLOBAL_GOALS: KeyContract(
        name=LAST_GLOBAL_GOALS,
        writer="global_loop",
        readers=["meta_loop"],
        description=(
            "Most recent global goal emission — cycle_id, goals (list of "
            "goal dicts each with id, priority, domain, success_metric, etc.)."
        ),
        shape_validator=_validate_global_goals,
    ),
    LAST_META_ORDERS: KeyContract(
        name=LAST_META_ORDERS,
        writer="meta_loop",
        readers=["autonomous_loop", "deepsleep_loop"],
        description=(
            "Most recent Meta order package — cycle_id, orders (list "
            "of order dicts each with order_id, domain, goal, tools, "
            "constraints, priority). Autonomous reads on next epoch."
        ),
        shape_validator=_validate_meta_orders,
    ),
    LAST_META_CYCLE: KeyContract(
        name=LAST_META_CYCLE,
        writer="meta_loop",
        readers=["global_loop", "deepsleep_loop"],
        description=(
            "Meta disposition truth (FINAL VERDICT law) — per-goal "
            "terminal outcome THIS cycle: {cycle_id, goals:[{"
            "source_goal_id, disposition: sent_to_auto|"
            "failed_to_initiate, reason, cause, cause_resolution_state,"
            " failure_fingerprint}]}. Same-cycle outcome invariant; no "
            "silent drops. Global grounds learning goals on it; "
            "DeepSleep persists it before SharedState clear."
        ),
        shape_validator=_validate_meta_cycle,
    ),
    LAST_CURIOSITY_CYCLE: KeyContract(
        name=LAST_CURIOSITY_CYCLE,
        writer="core.curiosity_engine",
        readers=["global_loop", "deepsleep_loop"],
        description=(
            "Pressure truth (FINAL VERDICT law) — deterministic "
            "curiosity pressure computation THIS cycle: {cycle_id, "
            "pressure, pressure_prev, bounded_delta, "
            "catastrophic_override}. Damping invariant. Global grounds "
            "learning goals on curiosity pressure evidence; DeepSleep "
            "persists it before SharedState clear."
        ),
        shape_validator=_validate_curiosity_cycle,
    ),
    LAST_OMEGA_EVALUATION: KeyContract(
        name=LAST_OMEGA_EVALUATION,
        writer="omega_loop",
        readers=["meta_loop", "deepsleep_loop"],
        description=(
            "Most recent Omega health evaluation — epoch, status "
            "(healthy / degradation / instability / stagnation), signals."
        ),
        shape_validator=_validate_omega_evaluation,
    ),
    LAST_OMEGA_PROPOSALS: KeyContract(
        name=LAST_OMEGA_PROPOSALS,
        writer="omega_loop",
        readers=["operator_approval_queue"],
        description=(
            "Most recent Omega proposal batch — epoch, proposals (list "
            "of {target, old, new, reason, risk}). Operator approval "
            "required for policy proposals; tools route through Guardian."
        ),
        shape_validator=_validate_omega_proposals,
    ),
    LAST_OMEGA_TRAJECTORY: KeyContract(
        name=LAST_OMEGA_TRAJECTORY,
        writer="omega_loop",
        readers=["global_loop", "deepsleep_loop"],
        description=(
            "The active OmegaTrajectory — multi-epoch directional pressure "
            "from omega. Shape: see schema.omega_trajectory.OmegaTrajectory. "
            "May be None when no trajectory is active. NOT cleared by "
            "deepsleep's cascade reset (multi-epoch by design). Global reads "
            "each cycle to bias goal formation; deepsleep snapshots into "
            "journal upstream so omega can audit + steer it next cycle."
        ),
        shape_validator=_validate_omega_trajectory,
    ),
    RECENT_OMEGA_EPOCHS: KeyContract(
        name=RECENT_OMEGA_EPOCHS,
        writer="metacognition.omega.phases.phase_4_commit.auditor",
        readers=["hud.telemetry.panel_omega"],
        description=(
            "Ring buffer (last 20 entries) of recent omega-cycle commit "
            "summaries — feeds the HUD Trajectory Star Map (§9.5). Each "
            "entry: {epoch, timestamp, applied, rejected, coherence, "
            "direction, audit_id}. The disk audit log "
            "(guardian/logs/omega_audit.jsonl) remains the forensic "
            "source-of-truth; this ring is the LIVE feed the HUD reads. "
            "P3-S16 operator doctrine (2026-05-27): HUD rides the same "
            "telemetry feed as the server — boot starts empty by "
            "construction (EPHEMERAL_RUNTIME), fills as cycles run."
        ),
        shape_validator=lambda v: (
            None
            if v is None or (isinstance(v, list) and all(isinstance(e, dict) for e in v))
            else "recent_omega_epochs must be list[dict] or None"
        ),
        # Ephemeral: boot starts fresh; a restored stale ring buffer
        # from a prior session would show dead epochs as if they were
        # current. Same "live feed starts empty" rationale as
        # PRESENCE_STATE / PRESENCE_WATCHER_HEARTBEAT.
        classification=Classification.EPHEMERAL_RUNTIME,
    ),
    RECENT_DEEPSLEEP_CYCLES: KeyContract(
        name=RECENT_DEEPSLEEP_CYCLES,
        writer="metacognition.deep_sleep.phases.phase_4_persistence.audit_logger",
        readers=["hud.telemetry.panel_deepsleep"],
        description=(
            "Ring buffer (last 5 entries) of recent DeepSleep "
            "consolidation cycles — feeds the HUD Consolidation Flow "
            "panel (§9.6). Each entry is the full cycle audit record "
            "(cycle_id, timestamp, fragments_consolidated, "
            "tools_added, global_insights, phase rollups). The disk "
            "audit log (guardian/logs/deep_sleep_audit.jsonl) remains "
            "the forensic source-of-truth; this ring is the LIVE feed. "
            "P3-S25 HUD live-feed doctrine v2 extension of P3-S16."
        ),
        shape_validator=lambda v: (
            None
            if v is None or (isinstance(v, list) and all(isinstance(e, dict) for e in v))
            else "recent_deepsleep_cycles must be list[dict] or None"
        ),
        # Ephemeral: same restart-empty rationale as RECENT_OMEGA_EPOCHS.
        classification=Classification.EPHEMERAL_RUNTIME,
    ),
    RECENT_GUARDIAN_INTERVENTIONS: KeyContract(
        name=RECENT_GUARDIAN_INTERVENTIONS,
        writer="guardian.decision",
        readers=[],
        description=(
            "Ring buffer (last 50 entries) of recent guardian "
            "intervention records — feeds the HUD Guardian Activity "
            "Stream (§2.3). Each entry mirrors the disk record from "
            "guardian/logs/guardian_interventions.jsonl. The disk "
            "ledger remains the forensic source-of-truth; this ring "
            "is the LIVE feed. P3-S25 HUD live-feed doctrine v2."
        ),
        shape_validator=lambda v: (
            None
            if v is None or (isinstance(v, list) and all(isinstance(e, dict) for e in v))
            else "recent_guardian_interventions must be list[dict] or None"
        ),
        # Ephemeral: same restart-empty rationale as RECENT_OMEGA_EPOCHS.
        classification=Classification.EPHEMERAL_RUNTIME,
    ),
    RECENT_LAYER_STATES: KeyContract(
        name=RECENT_LAYER_STATES,
        writer="core.layer_state",
        readers=[
            "hud.telemetry._telemetry_cycle",
            "hud.telemetry._telemetry_layers",
            "hud.telemetry._telemetry_memory",
            "hud.telemetry._telemetry_guardian",
            "hud.telemetry._telemetry_omega",
        ],
        description=(
            "Ring buffer (last 40 entries) of recent layer-state "
            "records — feeds the HUD brain panel's current-cycle + "
            "recent-layers surfaces plus 3 helper-function scanners "
            "(memory recall trace, guardian response mode, omega "
            "contradictions). Each entry mirrors the disk record "
            "from guardian/logs/layer_state.jsonl. The disk log "
            "remains the forensic source-of-truth; this ring is the "
            "LIVE feed (the most-polled disk read in the HUD "
            "pre-P3-S28). Written by core.layer_state.emit_layer_state "
            "AFTER the disk write lands. Cap = max scanner depth (40). "
            "P3-S28 HUD live-feed v3."
        ),
        shape_validator=lambda v: (
            None
            if v is None or (isinstance(v, list) and all(isinstance(e, dict) for e in v))
            else "recent_layer_states must be list[dict] or None"
        ),
        classification=Classification.EPHEMERAL_RUNTIME,
    ),
    RECENT_SELF_VECTORS: KeyContract(
        name=RECENT_SELF_VECTORS,
        writer="memory.self_vector",
        readers=[
            "hud.telemetry._telemetry_omega",
            "hud.telemetry.panel_identity",
        ],
        description=(
            "Ring buffer (last 20 entries) of recent self-vector "
            "history records — feeds the HUD's identity coherence + "
            "drift surfaces (omega coherence card; identity self-"
            "vector trajectory card). Each entry mirrors the disk "
            "record from guardian/state/identity/self_vector_history."
            "jsonl. The disk history remains the forensic record + "
            "the source of truth for full historical analysis; this "
            "ring is the LIVE feed for what the HUD renders. Vectors "
            "are full embedding-dimensional lists — keep ring cap "
            "small. P3-S28 HUD live-feed v3."
        ),
        shape_validator=lambda v: (
            None
            if v is None or (isinstance(v, list) and all(isinstance(e, dict) for e in v))
            else "recent_self_vectors must be list[dict] or None"
        ),
        classification=Classification.EPHEMERAL_RUNTIME,
    ),
    OUTSTANDING_USER_QUESTION: KeyContract(
        name=OUTSTANDING_USER_QUESTION,
        writer="brain.runner",
        readers=["brain_receiver"],
        description=(
            "Most recent question the system asked the user via a "
            "proactive (autonomous-source) brain cycle. Carries "
            "curiosity_id, question_text, asked_at (ISO timestamp), "
            "brain_cycle_id, ttl_seconds. brain_receiver.handle_user_input "
            "checks this on entry — if set + within TTL, treats the user's "
            "reply as the actual answer and updates the curiosity_store "
            "resolution_summary with the user's text. Without this loop "
            "closure, 'resolved' status only meant 'system delivered the "
            "question', NOT 'user answered it'."
        ),
        shape_validator=_validate_outstanding_user_question,
    ),
    OPERATOR_DIRECTIVE: KeyContract(
        name=OPERATOR_DIRECTIVE,
        # Receiver publishes when it detects a learning directive in
        # operator chat. pressure_loop.operator_pressure also exposes a
        # standalone publish helper for tests + harness paths.
        writer="brain_receiver",
        # pressure_field.read_operator_intent + cue_desk read this on
        # every tick; the trajectory seed helper reads it once per
        # episode open.
        readers=["pressure_field", "cue_desk", "symphony_runner"],
        description=(
            "Symphony S6 operator-intent pressure surface. Carries "
            "{text, domain, weight, issued_at, ttl_seconds?} -- the "
            "deterministic record (NO model inference on the receiver "
            "path) that lets pressure_field count operator 'learn X' "
            "directives as live pressure contribution, lets cue_desk "
            "stamp the resulting episode with opened_by='operator_intent', "
            "and lets symphony_runner seed LAST_OMEGA_TRAJECTORY with "
            "the directive's domain BEFORE META_A runs. TTL-bounded so "
            "stale directives don't keep opening episodes forever."
        ),
        shape_validator=_validate_operator_directive,
    ),
    CONVERSATION_STATE: KeyContract(
        name=CONVERSATION_STATE,
        writer="core.conversation_state",
        readers=["dialogue_act_selector", "reply_evaluator", "brain_receiver"],
        description=(
            "Short-horizon conversational pressure state. Tracks "
            "turns_since_user_spoke, turns_since_system_spoke, "
            "recent_dialogue_acts (last N), recent_user_replies (last N), "
            "user_engagement_estimate (0..1), last_updated (ISO). Updated "
            "by brain_receiver after every user input and by brain.respond "
            "after every system utterance. TTL-evicted to defaults after "
            "1 hour of total silence — that's a fresh conversation. Used "
            "by the dialogue act selector to dampen monologuing or going "
            "dead, and by the reply evaluator for repetition detection."
        ),
        shape_validator=_validate_conversation_state,
    ),
}


# ---------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------


def list_keys() -> List[str]:
    """Return the canonical keys in sorted order (deterministic)."""
    return sorted(KEY_REGISTRY.keys())


def writer_for(key: str) -> Optional[str]:
    """Return the documented writer for a key, or None if unknown."""
    contract = KEY_REGISTRY.get(key)
    return contract.writer if contract is not None else None


def readers_for(key: str) -> List[str]:
    """Return the documented readers for a key, or [] if unknown."""
    contract = KEY_REGISTRY.get(key)
    return list(contract.readers) if contract is not None else []


def validate_write(key: str, value: Any) -> Optional[str]:
    """Validate `value` against the canonical shape for `key`.

    Returns:
        None on valid; an error string describing the violation otherwise.
        For an unknown `key`, returns "unknown canonical key …"
        (the caller can decide whether to refuse the write or accept
        it as a non-canonical ad-hoc write).
    """
    contract = KEY_REGISTRY.get(key)
    if contract is None:
        return f"unknown canonical key {key!r}; only {list_keys()} are in the SLICE-2 registry"
    return contract.shape_validator(value)


# ---------------------------------------------------------------------
# Phase 2 §P2-S17 — classification probe surface
# ---------------------------------------------------------------------


def classify(key: str) -> Classification:
    """Return the classification for a canonical key. Unknown / non-
    canonical keys default to EPHEMERAL_RUNTIME — the safe-by-default
    fallback so an unclassified key cannot accidentally persist."""
    contract = KEY_REGISTRY.get(key)
    if contract is None:
        return Classification.EPHEMERAL_RUNTIME
    return contract.classification


def is_persistable(key: str) -> bool:
    """Per PACKET_P2_S17.md RB#2: only DURABLE_PROJECTION keys
    persist to Guardian state. TICK_BOUNDARY_PROJECTION keys ride
    through the tick-state WAL via their owning caches (or are
    recomputed naturally on the next tick). EPHEMERAL_RUNTIME and
    FORBIDDEN_TO_PERSIST never reach disk."""
    return classify(key) is Classification.DURABLE_PROJECTION


def keys_by_classification(
    classification: Classification,
) -> List[str]:
    """Return canonical keys with the given classification, sorted."""
    return sorted(
        name for name, contract in KEY_REGISTRY.items() if contract.classification is classification
    )


def value_contains_forbidden_payload(value: Any) -> Optional[str]:
    """Walk a value's nested dict shape and return the first key name
    that matches a FORBIDDEN_PAYLOAD_KEY_TOKENS substring, or None.

    Used by guardian state persistence to reject writes containing
    raw model output / embeddings / image-audio bytes / stack traces
    even when the canonical key's contract accidentally accepts them.
    Walk is depth-bounded at 8 nesting levels to keep cost bounded
    against pathological inputs.
    """

    def _walk(obj: Any, depth: int) -> Optional[str]:
        if depth > 8:
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                key_str = str(k).lower()
                for token in FORBIDDEN_PAYLOAD_KEY_TOKENS:
                    if token in key_str:
                        return f"{k}"
                child = _walk(v, depth + 1)
                if child is not None:
                    return child
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                child = _walk(v, depth + 1)
                if child is not None:
                    return child
        return None

    return _walk(value, 0)
