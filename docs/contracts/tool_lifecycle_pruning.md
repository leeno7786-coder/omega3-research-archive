# Tool Lifecycle Auto-Pruning

**Status**: production. Wired through `core.tool_reliability_store`
+ DeepSleep Phase 4. Read this before touching anything in the
lifecycle code path or changing the auto-eval cadence.

The "creates tools" half of the system is the Tool Synthesis Doctrine
(TS-A through TS-H). This doc is the **other half** — the lifecycle
that **takes them away** when they're broken or unused. Without
pruning, the registry grows monotonically; broken tools poison the
catalog forever; stale tools occupy mental footprint operators have
to scan past.

---

## Why pruning matters

A self-creating tool system that never prunes:

1. **Accumulates broken tools** — the model keeps seeing
   `format_csv_v0` in the catalog even though it's failed 8/10 calls,
   because nothing demoted it. Future planning wastes calls on
   known-bad tools.
2. **Bloats the catalog** — `_match_tools` walks `n` tools per meta
   cycle; `n` grows linearly with synthesis events forever.
3. **Loses the operator's mental model** — by week three the
   operator can't tell the 3 useful tools apart from the 47 that
   were synthesized once and never reused.

Auto-pruning solves all three by retiring tools the system isn't
actually getting value from, while keeping the audit trail intact so
provenance never breaks.

---

## The four auto-transition rules

All four live in `core.tool_reliability_store.evaluate_auto_transitions()`.
First-match-wins; order matters (graduation runs **before** disuse so
a healthy tool's first auto-eval doesn't get short-circuited by the
disuse branch with `days_since_last_used = 0`).

| # | Rule | Threshold | From → To | Reason format |
|---|---|---|---|---|
| 1 | **Failure-driven deprecate** | `success_rate < 0.5` AND `total_invocations >= 5` | `sandbox_only`/`graduated` → `deprecated` | `auto-deprecated (failure): success_rate=X.XX < 0.5 over N invocations` |
| 2 | **Auto-graduate** | `success_rate >= 0.9` AND `total_invocations >= 10` | `sandbox_only` → `graduated` | `auto-graduated: success_rate=X.XX >= 0.9 over N invocations` |
| 3 | **Disuse-driven deprecate** | `last_used >= 30 days` ago AND `total_invocations >= 1` | `sandbox_only`/`graduated` → `deprecated` | `auto-deprecated (disuse): last_used N.N days ago >= 30d threshold` |
| 4 | **Stale-deprecated retire** | `>= 14 days` since the deprecation transition | `deprecated` → `retired` | `auto-retired (stale-deprecated): N.N days in deprecated state >= 14d threshold` |

When an auto-transition fires:
1. The new state is appended to the tool's transition history.
2. A `transition` event is written to `tool_reliability_events.jsonl`.
3. If the new state is `deprecated` or `retired`, the tool is
   **unregistered from `voice_box_tools`** so it's no longer callable
   from either BrainLoop or the Autonomous loop.

The tool's spec + code stay in `tool_registry_log.jsonl` for
provenance forever. Re-synthesis is cheap if the same need surfaces
later — TS-F just runs the chain again.

---

## Cadence: where auto-eval actually runs

Two call sites:

### Per-invocation (immediate, narrow)

`tool_reliability_store.record_invocation(tool_id, ..., auto_evaluate=True)`
— called by `core.voice_box_tools.execute_tool_call` after every
dispatch. Evaluates only the just-invoked tool (`tool_id=` filter).

This is what catches **failure-driven deprecation immediately** —
the moment a tool crosses the success_rate floor, the next dispatch
of that tool itself triggers the deprecation. The operator sees it
in the same epoch.

### Per-epoch (periodic, broad)

`metacognition.deep_sleep.phases.phase_4_persistence.run` calls
`evaluate_auto_transitions()` (no tool_id filter — walks every tool)
once per DeepSleep cycle.

This is what catches **disuse-driven deprecation** + **stale-
deprecated retirement** — these are time-based, so they only fire on
a periodic walk, not on invocation (a stale tool, by definition,
isn't being invoked).

**Cadence math**: DeepSleep runs once per 7-minute epoch. The 30-day
disuse threshold is `30 * 24 * 60 / 7 ≈ 6170 epochs` of grace. The
14-day retire threshold gives another 2880 epochs. Plenty of time for
the operator to graduate a tool back if needed.

---

## Operator override

Auto-eval **never overrides operator decisions**:

* `transition_to_graduated(tool_id, approver_id="operator:xyz", reason)`
  flips a deprecated tool back to graduated. The next auto-eval pass
  starts fresh from "graduated" — no penalty.
* `transition_to_retired(tool_id, approver_id, reason)` lets the
  operator retire any tool early.
* `operator_set_status(tool_id, new_status, approver_id, reason)`
  is the generic override for any valid status.

The audit chain in `tool_reliability_events.jsonl` carries
`approver_id` so it's always clear which transitions came from the
operator vs. from `auto`.

---

## Re-synthesis after retirement

Retirement is terminal but the spec lives forever. If the same
capability gap surfaces after a tool was retired:

1. Curiosity engine emits a `_Gap` with the same problem statement.
2. `_route()` lands it on `recommended_path="tool_creation"` again.
3. Autonomous engine's next sweep picks it up.
4. `synthesize_from_curiosity` runs — produces a NEW spec_id +
   tool_id (UUIDs differ from the retired one).
5. The new tool starts at `sandbox_only` with zero invocations.
   Same name → DIFFERENT identity. The retired tool's stats stay
   intact for forensic review.

In practice: a retired tool that the operator wants back faster than
auto-resynthesis would handle should be **re-graduated**, not
re-synthesized. The original tool_id is still there;
`transition_to_graduated` brings it back without losing reliability
history.

---

## Persistent state touched

| File | Producer | Role in pruning |
|---|---|---|
| `.omega_data/data/tool_reliability_events.jsonl` | `tool_reliability_store._append_event` | One JSONL line per `invocation` / `transition` / `context_fit`. The auto-eval reads this through the cache to compute `success_rate` + `last_used`. |
| `.omega_data/data/tool_registry_log.jsonl` | `tool_lifecycle_ops.record_registration` | Append-only; never pruned. Retired tools stay here so re-synthesis can find the original spec for diff. |
| `.omega_data/data/tool_decisions.jsonl` | `guardian.tool_decision._persist` | Guardian's decisions per spec. Never pruned. |
| In-memory: `voice_box_tools._TOOL_REGISTRY` | `register_tool` / `unregister_tool` | Auto-deprecate + auto-retire call `unregister_tool` so the tool drops out of the catalog the moment it transitions. |

---

## What pruning does NOT do (intentional)

* **Does not delete files.** Spec + code stays on disk. Provenance is
  permanent.
* **Does not refund the sandbox.** `.omega_data/sandbox/runs/<uuid>/`
  dirs from past invocations stay until operator runs
  `cleanup_sandbox_run`. Pruning is about the registry, not the disk.
* **Does not undo Guardian decisions.** A tool retired today stays
  whatever Guardian said it was (APPROVE / SANDBOX_ONLY); the
  decision record never gets rewritten.
* **Does not reverse-trigger re-synthesis automatically.** If the
  operator wants a retired tool back, they explicitly graduate it OR
  let the curiosity engine notice the gap on its own next time.

---

## Tuning

All thresholds are constants at the top of
`core/tool_reliability_store.py`. Change with care — they reflect a
real cost/benefit balance:

```python
GRADUATE_SUCCESS_FLOOR    = 0.9    # 9 of 10 calls must succeed
GRADUATE_MIN_INVOCATIONS  = 10     # need a track record
DEPRECATE_SUCCESS_CEIL    = 0.5    # half the calls failing is broken
DEPRECATE_MIN_INVOCATIONS = 5      # don't punish an unlucky first use
DEPRECATE_DISUSE_DAYS     = 30     # one month grace before "stale"
RETIRE_DEPRECATED_DAYS    = 14     # two weeks deprecated → terminal
```

Lower thresholds → faster pruning, less retention; can punish
recoverable tools.
Higher thresholds → slower pruning, more accumulation; can keep dead
weight too long.

The defaults match what feels right for a system that runs ~one
cycle per minute. Bumping cycle frequency 10× would warrant
proportionally lower day-thresholds.

---

## Failure modes (and what happens)

| Failure | Behavior |
|---|---|
| `evaluate_auto_transitions` raises | DeepSleep Phase 4 logs warning, continues. Operator sees `tool_lifecycle_transitions: []` in the persistence_report instead of a populated list. |
| `voice_box_tools.unregister_tool` raises after auto-transition | Logged at DEBUG. The store's transition record is already persisted; the tool may stay callable for one more invocation cycle until the next eval re-tries. |
| Tool retired but the spec_id is missing from `tool_registry_log.jsonl` | Pruning still works — uses tool_id for everything. Provenance reads degrade gracefully. |
| Operator graduates an already-retired tool | Allowed via `operator_set_status` (retired is not strictly terminal for operator overrides). The transition gets logged with `approver_id="operator:..."` and the tool comes back. |
| `last_used` field is malformed/None | `_days_since` returns `None` → disuse check skips that tool. No false positives. |

---

## Test coverage

* `tests/test_tool_lifecycle_auto_pruning.py` (17 checks) — all four
  rules + operator override + voice_box unregistration.
* `tests/test_ts_e_tool_reliability_store.py` (89 checks) — the
  underlying reliability store; new rules don't regress existing
  graduation behavior.
* `tests/test_deepsleep_phases_2_3.py` (25 checks) — DeepSleep Phase 4
  still completes cleanly with the auto-eval call wired in.

---

## Pointers when extending

* **Add a new auto-transition rule**: extend
  `evaluate_auto_transitions` in priority order. New rules go AFTER
  existing ones unless they should override (in which case make the
  precedence explicit in a comment).
* **Add a new threshold**: drop a constant at the top of
  `core/tool_reliability_store.py` next to the existing ones; keep
  defaults conservative.
* **Change cadence**: the per-invocation call site fires from
  `voice_box_tools.execute_tool_call → record_invocation`; the
  periodic call site is in DeepSleep Phase 4. Don't add more — too
  many call sites makes timing non-obvious.
* **Surface in HUD**: `_tool_synthesis_summary()` in `hud/telemetry.py`
  could grow a `recently_pruned` list reading the last N transition
  events with `approver_id="auto"` and `new_status` in `(deprecated,
  retired)`. Not yet wired.
* **Document the why**: every threshold change should land with a
  justification in this README — operators will ask why `X` not `Y`.

---

## Related docs

* `core/README.md` § Tool synthesis layer — where the lifecycle store
  lives among the other TS modules.
* `metacognition/omega/tool_synthesis/README.md` — the creating-half
  of the lifecycle (the doctrine + pipeline).
* `docs/contracts/03_curiosity_development.md` — why a system that
  creates without pruning isn't actually self-directed.
* Memory: `project_tool_synthesis_doctrine.md` — the operator-locked
  9-principle contract that pruning serves (Principle 5: reliability
  scoring; tools that fail get deprecated/retired).
