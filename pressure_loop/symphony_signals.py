"""Symphony signals — S1 scaffold + v0.2 ignition surface.

Canonical signal set. Authoritative list is the `signals:` block in
docs/architecture/symphony_cue_sheet.yaml; this enum MUST stay in sync
with it (test_symphony_episode_store.py asserts coverage on every run).

For S1 the enum is a passive constants surface — no publisher, no
subscriber, no CueDesk. Movements emit/consume these symbols once the
S3+ pressure conductor and the CueDesk land. Until then they exist so
the rest of the symphony code (EpisodeStore, future movement runners,
tests) can name the contract by its constants rather than by drift-
prone strings.

v0.2 addition (S1 audit, 2026-05-21):
  The four IGNITE_* signals (IGNITE_META_EXECUTIVE / GLOBAL_PRELUDE /
  OMEGA_PRELUDE / RESPONSE_HOLD) were declared in symphony_cue_sheet.yaml
  by PR #6 but absent from this enum, leaving the strict-equality
  drift-guard test red. Adding them now keeps the enum at parity with
  the cue sheet contract; the producer (ignition selector, SWING §S9)
  lands later and reads these from here rather than re-declaring them.
  This is the same passive-constants pattern the existing 13 signals
  followed before their producers landed.

Doctrine link (project_sharedstate_five_incompatible_roles): signals
are part of the convergence cure — they replace the implicit
"who-reads-whom" coupling SharedState pretended to encode. A signal
fires; whoever cares listens; the chain is wire-explicit, not
loop-order-implicit.
"""

from __future__ import annotations

from enum import Enum


class SymphonySignal(str, Enum):
    """Canonical movement signals.

    str-mixin so values are interchangeable with their string form
    (e.g. for YAML/JSON round-trips) while still benefiting from
    enum identity checks. Every member's value MUST equal its name
    so the cue-sheet YAML (which uses bare uppercase strings) round-
    trips into this enum without translation."""

    # ── Canonical Symphony cue (P3-S34, operator-locked 2026-05-27) ──
    # The chain emits below trace ONE episode from pressure open to
    # deepsleep close. Each emit is a fold point — the episode
    # accumulates that movement's final telemetry at the emit boundary,
    # and the next movement reads forward from it. "One goal per
    # episode if emitted; no leftover goals" — when Global phase 3
    # branches to omega (idle/response action), the meta + auto +
    # post-auto fold path is skipped entirely and the episode goes
    # straight to omega → deepsleep. Operator framing:
    #
    #   "pressure releases → global P1 initiates (receives the pressure
    #    data) → global P2 (decides an action based off the data:
    #    response, action goal for meta, etc) → global P3 (emits the
    #    action — depending on what that action is, initiates a goal,
    #    a response, or idle) → if goal: meta P1 (receive action) →
    #    meta P2 (select tool + add param knowledge) → meta P3 (build
    #    pack for auto + drop) → auto (all phases) → meta P4 (reflect
    #    + build data pack → send to global P4) → global P4 (reason
    #    about data + outcome, add to knowledge mastery if web-searched,
    #    telemetry to omega) → omega (4 phases, journal + trajectory)
    #    → deepsleep (memory housekeeping)"
    #
    # Trajectory output from omega gets passed into pressure data to
    # influence the next episode's open. That's how the substrate
    # learns from one episode to the next.
    PRESSURE_OPEN = "PRESSURE_OPEN"
    # v0.2 ignition routes. Pressure opens attention; ignition selects
    # the first movement. Inert at S1: no producer yet, contract names only.
    IGNITE_META_EXECUTIVE = "IGNITE_META_EXECUTIVE"
    IGNITE_GLOBAL_PRELUDE = "IGNITE_GLOBAL_PRELUDE"
    IGNITE_OMEGA_PRELUDE = "IGNITE_OMEGA_PRELUDE"
    IGNITE_RESPONSE_HOLD = "IGNITE_RESPONSE_HOLD"
    SENSORY_INJECT = "SENSORY_INJECT"
    # Global pre-auto: 3 boundaries — initiate (P1 intake), action
    # (P2 decide), dispatch (P3 emit action). Three separate emits so
    # the chain log tells the story: substrate received pressure,
    # decided an action, dispatched it.
    GLOBAL_INITIATE = "GLOBAL_INITIATE"
    GLOBAL_ACTION = "GLOBAL_ACTION"
    GLOBAL_DISPATCH = "GLOBAL_DISPATCH"
    # Meta pre-auto: P1 (receive action) + P2 (tool select + param
    # knowledge) + P3 (build pack + drop to auto). Single chain emit
    # because the three phases are tightly coupled (no value in
    # splitting the chain log at every internal meta phase).
    META_PRE_AUTO = "META_PRE_AUTO"
    AUTO_INITIATE = "AUTO_INITIATE"
    AUTO_COMPLETED = "AUTO_COMPLETED"
    AUTO_FAILED = "AUTO_FAILED"
    # Meta post-auto: P4 (reflect on outcome, build data pack, send to
    # global P4). Single chain emit.
    META_POST_AUTO = "META_POST_AUTO"
    # Global post-auto: P4 (reason about data + outcome, knowledge
    # mastery update if web-searched, telemetry to omega). Replaces
    # the silent global_late_fold step with a named cue emit.
    GLOBAL_REASON_REFLECT = "GLOBAL_REASON_REFLECT"
    OMEGA_CONDUCTED = "OMEGA_CONDUCTED"
    TRAJECTORY_UPDATED = "TRAJECTORY_UPDATED"
    EPISODE_COMPLETE = "EPISODE_COMPLETE"
    DEEPSLEEP_DONE = "DEEPSLEEP_DONE"

    # ── Deprecated v0.1 emit names (P3-S34 alias section) ──
    # Kept as Python-enum aliases of the canonical labels above so
    # external code (handoff docs, tests, tools) that imports e.g.
    # `SymphonySignal.GLOBAL_ORIENTED` still resolves cleanly to the
    # new canonical value. Per Python enum semantics, an alias has
    # `.value == <canonical value>` and `is <canonical member>`, so
    # any check that reads the value gets the new label automatically.
    # DO NOT use these in new code — the chain emits should read in
    # the operator's canonical cue order.
    GLOBAL_ORIENTED = "GLOBAL_INITIATE"
    GLOBAL_ACTED = "GLOBAL_DISPATCH"
    META_DISPATCHED = "META_PRE_AUTO"
    META_REFLECTED = "META_POST_AUTO"


# Tuple of all canonical signal string values. Provided so importers
# can iterate without re-deriving from the Enum class.
ALL_SIGNALS: tuple[str, ...] = tuple(s.value for s in SymphonySignal)
