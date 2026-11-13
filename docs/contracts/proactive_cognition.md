# Proactive Cognition — System Reference

**Status**: canonical reference for the proactive-cognition substrate.
Ties together the gap-detection / autonomous-initiation / cadence /
brain-receiver wiring that makes Omega *speak first* when its internal
state demands it. Authoritative source for "what the proactive system
actually is" — point future code at this doc.

> **The principle**: The brain loop is the **mouth**, not the
> gatekeeper. The model is invoked when the mind has something to say —
> including questions the mind needs answered. Reactivity (model speaks
> only when spoken to) is what makes a chatbot. Proactivity (model
> speaks when internal state demands it) is what makes a mind.

Contract docs that cover the *why*: [`03_curiosity_development.md`](03_curiosity_development.md)
+ [`../research/two_way_cognition.md`](../research/two_way_cognition.md). This doc covers the *how* —
the live wiring and contracts.

**See Also:**
- [`README.md`](README.md) — Contracts directory index
- [`03_curiosity_development.md`](03_curiosity_development.md) — Curiosity engine doctrine
- [`06_response_system.md`](06_response_system.md) — Response system
- [`../architecture/OPERATOR_CHANNEL_MAP.md`](../architecture/OPERATOR_CHANNEL_MAP.md) — Operator channel mapping
- [AGENTS.md](../../AGENTS.md) — governing cognition cycle

---

## Why proactive cognition exists

The reactive failure mode this eliminates:

> User says their name → `brain.retrieve` finds no anchor → `brain.respond`
> emits "I have no memories of that" → conversation ends → **no new
> memory written** → the same gap surfaces forever.

The proactive replacement:

> User says name → no anchor exists → curiosity engine surfaces the gap
> → autonomous engine constructs a `BrainIntake(source="autonomous")`
> → cycle fires through the brain loop via orchestrator wiring → **the model
> formulates the clarifying question** → user answers → memory written
> → identity anchor formed → next time the system already knows.

Same architecture pattern handles tool requests (operator approval
gates), contradiction resolution (DeepSleep restructuring proposals),
and capability gaps (tool creation is routed through Omega commit).

---

## The five wires

The proactive substrate has no `proactive/` directory because it is
emergent from how five subsystems compose. Each wire below is required;
removing or muting any one breaks proactivity.

| # | File | Role in the proactive loop |
|---|---|---|
| 1 | `core/curiosity_engine.py` | **Detects** gaps. `detect_gaps(ctx)` walks the cycle context and emits `_Gap` records (one per shortfall). `_score_gap()` produces tension/value/urgency/persistence/cost in `[0, 1]`. `_route()` maps each gap to a `ResolutionPath`. |
| 2 | `core/curiosity_store.py` | **Persists** gaps as `CuriosityEngineEntry` under `memory/000_identity/omega/open_questions/<id>.json`. Provides `mark_active / mark_resolved / mark_deferred / mark_escalated / mark_failed_retry`. |
| 3 | `core/cycle_cadence.py` | **Times** initiation. `HybridScheduler` polls registered `PressureSignal` providers (contradiction_spike, guardian_intervention, persistence_threshold, outstanding_question_buildup, ...) and decides when to fire. |
| 4 | `core/response_engine.py` | **Routes** proactive speech. `sweep_and_dispatch(limit)` (boot cadence + omega phase 4 end) picks dialogue act per curiosity/system signal and dispatches via autonomous or internal brain cycles. Fallback: `core/autonomous_engine.sweep_and_initiate` if import fails. |
| 4b | `core/autonomous_engine.py` | **Initiates** ask-user cycles. `initiate_cycle_from_curiosity` builds `BrainIntake(source="autonomous")`; legacy `sweep_and_initiate` still used as boot fallback. |
| 5 | `core/curiosity_feedback_consumer.py` | **Closes the loop**. Dequeues pending curiosity-resolution records from SharedState after each cycle and routes to the right `mark_*` verb on `curiosity_store`. Without this, entries stay `active` forever. |

`core/brain_receiver.py` remains the canonical intake builder and
entry-point surface. Autonomous sweeps dispatch through the orchestrator
brain runner with source="autonomous" and curiosity provenance carried
on the intake.

---

## The contract surface (don't break these)

### `ResolutionPath` enum (curiosity_engine schema)

```python
ResolutionPath = Literal[
    "ask_user",          # autonomous engine → brain cycle → operator-facing question
    "web_search",        # autonomous engine → web_fetch tool → memory write
    "internal_test",     # autonomous engine → sandbox probe → memory write
    "simulation",        # not yet wired (reserved)
   "tool_creation",     # omega commit phase decides synthesis/registration
    "defer",             # leave open; revisit on a later sweep
]
```

| Path | Wired? | Routes to |
|---|---|---|
| `ask_user` | ✅ | `find_pursuits` → `initiate_cycle_from_curiosity` → brain cycle via orchestrator |
| `tool_creation` | ✅ | Omega phase-4 commit synthesis path (health-gated + dedup + registration) |
| `web_search` | partial | currently captured as `missing_tool:` failure signature; routes via tool synthesis |
| `internal_test` | ⏳ | reserved — design pending |
| `simulation` | ⏳ | reserved — for future "what-if" reasoning |
| `defer` | ✅ | `mark_deferred` keeps entry alive but out of sweep |

### `GapType` enum

```python
GapType = Literal[
    "knowledge",     # missing fact / definition / context
    "capability",    # system can't currently DO something
    "tool",          # specific tool absent (drives tool_creation path)
    "identity",      # operator/self anchor missing or contradicted
    "policy",        # rule/threshold genuinely undecidable from data
    "verification",  # claim couldn't be checked against memory/external
]
```

### `CuriosityStatus` lifecycle

```
open ──(autonomous picks)──▶ active ──(brain finishes)──┬─▶ resolved
                                                        ├─▶ escalated  (Guardian posture blocked)
                                                        ├─▶ deferred   (operator chose later)
                                                        └─▶ open + attempt_count++ (failed_retry)
```

`escalated` is SLICE-10 — added so brain cycles that hit a Guardian
posture (security_block / identity_boundary / omega_lock) don't loop
forever. Operator surface re-routes escalated entries.

### `BrainIntake.source` for autonomous-initiated cycles

`autonomous_engine.build_autonomous_intake(cur)` constructs a
`BrainIntake` with `source="autonomous"` + `autonomous_activation`
populated. The brain loop's audit chain reads `source` to distinguish
operator-driven cycles from self-initiated ones — every cycle's
`layer_state.jsonl` record carries this provenance.

**Don't add new sources without also extending the audit reader** —
operator metrics (e.g. "X% of cycles were proactive") read this field.

### Pressure signal contract (cycle_cadence)

Each provider is `Callable[[], PressureSignal]` returning:

```python
PressureSignal(
    name="contradiction_spike",   # signal identifier
    value=14.0,                   # observed value
    threshold=10.0,               # provider's "should fire" point
    fire_now=True,                # value >= threshold AND provider says yes
    weight=1.5,                   # importance multiplier
    note="contradiction tokens in last 200 layer_state lines: 14",
)
```

Built-in providers:

| Provider | Reads | Fires when |
|---|---|---|
| `contradiction_spike_provider` | last 200 lines of `layer_state.jsonl` | contradiction tokens ≥ threshold (default 10) |
| `guardian_intervention_provider` | tail-50 of `guardian_interventions.jsonl` | interventions ≥ threshold (default 5); weight 2.0 |
| `persistence_threshold_provider` | (stub for L13) | always returns 0 pressure currently |
| `outstanding_question_buildup_provider` | curiosity_store open count | open `ask_user` count ≥ threshold |

DOC-6 doctrine_watcher writes `kind=doctrine_slip` /
`kind=doctrine_initial_breach` records to `guardian_interventions.jsonl`
— these flow into `guardian_intervention_provider` and can drive
proactive cycles when the doctrine slips.

---

## Lifecycle of a proactive cycle (the canonical trace)

```
┌─────────────────────────────────────────────────────────────────┐
│ Epoch tick (boot.py run_continuous loop)                        │
└──────────────────────────────────┬──────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│ HybridScheduler.tick()        cycle_cadence.py                  │
│   - polls every PressureSignal provider                         │
│   - composes weighted score                                     │
│   - returns CadenceTickReport(should_initiate=True, ...)        │
└──────────────────────────────────┬──────────────────────────────┘
                                   │ should_initiate
                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│ autonomous_engine.sweep_and_initiate(limit=1)                   │
│   1. consume_pending(shared_state) ──▶ feedback_consumer.py     │
│      └─ closes prior cycle's curiosity (resolved/escalated/...) │
│   2. find_pursuits(limit) ──▶ curiosity_store.list_open()       │
│      └─ filters: should_pursue=True, recommended_path=ask_user  │
│   3. initiate_cycle_from_curiosity(cur)                         │
└──────────────────────────────────┬──────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│ autonomous_engine.initiate_cycle_from_curiosity(curiosity)      │
│   - build_autonomous_intake(cur) ──▶ BrainIntake(source="auto") │
│   - mark_active(curiosity_id)                                    │
│   - orchestrator.initiate_brain_loop(intake)                     │
└──────────────────────────────────┬──────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│ Brain loop (13 layers) — Phase 10 invokes the model to          │
│ produce the operator-facing question; post-cycle wiring         │
│ enqueues pending-curiosity resolution with the cycle outcome.   │
└──────────────────────────────────┬──────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│ Operator answers (or doesn't) ──▶ next cycle's                  │
│ handle_user_input ──▶ memory.api.write_memory ──▶ anchor formed │
└──────────────────────────────────┬──────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│ Next sweep: consume_pending sees the resolution token →         │
│ curiosity_store.mark_resolved(curiosity_id) → entry closes →    │
│ stress relieved, dopamine reads positive on the closing cycle   │
└─────────────────────────────────────────────────────────────────┘
```

`tool_creation` curiosities are not dispatched by `sweep_and_initiate`.
They are handled in Omega commit (health-gated synthesis + registration)
so tool creation remains under governance and evaluation gates.

---

## Persistent state (guardian + memory; `.omega_data` retired 2026-05-18)

| Path | Producer | Purpose |
|---|---|---|
| `memory/000_identity/omega/open_questions/<curiosity_id>.json` | `core.curiosity_store` | One file per gap; status tracks lifecycle |
| `guardian/state/omega_open_questions.jsonl` | `core.open_questions` | Journal of questions Omega asked and operator answers |
| `guardian/logs/cycle_cadence_audit.jsonl` | `core.cycle_cadence` | Per-tick cadence decision (signals + should_initiate) |
| `guardian/logs/cycle_events.jsonl` | `core.cycle` | Cycle phase events; proactive cycles use `source="autonomous"` on intake |
| `guardian/logs/layer_state.jsonl` | `core.layer_state` | Per-layer-per-cycle records |

Don't move or rename these without updating the readers (cycle_cadence
providers tail several of them, deepsleep replays from `layer_state`).

**See also:** [`06_response_system.md`](06_response_system.md) for the full
response-engine / conv-cog / dialogue-act map and flowcharts.

---

## Architectural rules (non-negotiable)

1. **The model is not the mind.** The model is a tool the mind uses to
   speak (and to listen). Treating the model as the mind collapses the
   architecture back into a chatbot.
2. **Cycles can be self-initiated.** Any change that forces all cycles
   to be user-triggered breaks the inversion. The brain loop is a
   **receiver** — operator / system / autonomous all enter through
   `brain_receiver.handle_*` with the appropriate `source`.
3. **Curiosity entries close on resolution OR escalation OR
   max-retry.** Never silently drop. `attempt_count` + `escalated`
   status give the operator a path; deletion is a mistake.
4. **Pressure providers are read-only.** A provider raise must NEVER
   block the tick. `HybridScheduler` catches per-provider exceptions
   and continues with the rest.
5. **Routing through `ask_user` is real cognition.** When resolution
   genuinely requires operator input, asking IS the right cognition.
   Don't silently drop a gap because the system "should figure it out
   alone."
6. **The neuromodulator pipeline is load-bearing.** Without dopamine
   RPE on the closing cycle, the system can't tell a question that
   landed a useful answer from one that produced nothing. Without
   plasticity gating, the answer doesn't encode durably. Without
   stress accumulation under unresolved curiosities, there's no
   internal pressure to ask in the first place.
7. **`source` on `BrainIntake` is provenance, not policy.** Don't
   gate behavior on `source == "autonomous"`; gate on the actual
   data (e.g. `requires_operator_input` flag). Source is for the
   audit trail.

---

## Test coverage

| File | Covers |
|---|---|
| `tests/test_slice4_autonomous_endpoint_scoring.py` | Autonomous Phase 3 inference rebound; stress field correction |
| `tests/test_slice9_brain_verify_autonomy_feedback.py` | brain.verify scoring + pending curiosity-resolution queue write |
| `tests/test_slice10_curiosity_feedback_closure.py` | `consume_pending` routes resolved/escalated/failed correctly |
| `tests/test_slice13_autonomous_loop_separation.py` | Engine-driven Surface A vs LIVE Surface B sequencer |
| `tests/test_slice14_cross_loop_integration.py` | End-to-end curiosity → brain → consume cycle through real wiring |
| `tests/test_cycle_cadence.py` | HybridScheduler tick math + provider isolation |
| `tests/test_autonomous_loop.py` | Full autonomous loop integration |
| `tests/test_brain_runner_abort_cleanup.py` | Cycle aborts (e.g. operator timeout) clean up curiosity state |

---

## Operator surfaces

| Concern | Surface |
|---|---|
| Manually request a tool | HUD `POST /api/omega/request_tool` (creates `tool_creation` curiosity) |
| List open curiosities | `python -m core.curiosity_store list_open` (Python API) |
| Force-resolve / defer / escalate | `core.curiosity_store.mark_*(curiosity_id, reason)` |
| See proactive cycle activity | HUD telemetry → cycle panel → `source` badge per cycle; layer_state.jsonl tail |
| Tune cadence pressure | `config/cadence.yaml` (provider thresholds + weights) |

---

## Pointers when extending

- **Adding a new gap source**: emit `_Gap(...)` from `detect_gaps(ctx)`
  with the right `gap_type`. Routing is automatic via `_route()`.
- **Adding a new resolution path**: extend `ResolutionPath` enum +
  add a wiring branch in `sweep_and_initiate`. Update this doc + the
  enum table above.
- **Adding a new pressure signal**: write a `Callable[[], PressureSignal]`,
  call `sched.register_signal_provider("name", fn)` at boot. Document
  the provider in the table above.
- **Touching `BrainIntake.source` semantics**: read rule #7 above
  first. Source is provenance, not policy.

---

## Related docs

- `docs/contracts/03_curiosity_development.md` — the contract ("curiosity asks, development builds")
- `docs/research/two_way_cognition.md` — the research paper version
- `core/README.md` § Proactive cognition substrate — short pointer
- `brain/README.md` — the 13-layer loop that the proactive substrate drives
- `autonomous/README.md` — the autonomous loop that owns sweep timing
- Memory: `project_proactive_cognition_doctrine.md` — the doctrine memory (the WHY)
