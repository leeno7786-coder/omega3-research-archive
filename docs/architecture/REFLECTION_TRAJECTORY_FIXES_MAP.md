# REFLECTION_TRAJECTORY_FIXES_MAP.md
## Devin cold-map — work_order #6c4d4038 from claude
## READ-ONLY mapping; no code changes. Cites file:line, EXISTS vs NEW.

---

## FIX 1 — TrajectoryDiff ABANDONED mislabel

### Symptom restatement
The Omega reflection’s `trajectory` block may emit `continuation_recommended=true` with the **same** `target_domain`, yet the precomputed `trajectory_diff` label fed into the prompt (and emitted in the outcome pack) reads `ABANDONED`. A same-domain method pivot is classified as abandonment, which undermines the 3c trajectory-as-default stance.

### Where the diff is computed

| File | Line | Role |
|------|------|------|
| `omega_loop/_omega_state.py` | 138–200 | **`compute_trajectory_diff`** — deterministic preflight label from snapshot vs episode window. EXISTS. |
| `omega_loop/intake/run.py` | 57 | Calls `compute_trajectory_diff` during `build_omega_input_for_window`. EXISTS. |
| `omega_loop/loop.py` | 52 | `trajectory_diff=omega_input.trajectory_diff` passed straight to `package_window`. EXISTS. |
| `omega_loop/package/run.py` | 23 | `trajectory_diff` stored in `OmegaOutcomePack`. EXISTS. |

### What drives ABANDONED (root cause)

`compute_trajectory_diff` (`_omega_state.py:138-200`) scores each episode as **aligned** or **drift** with this logic per entry:

```python
# _omega_state.py:166-190 (paraphrased)
success = status in {"success", "completed", "ok"}
domain_match = (
    not target_domain
    or update_domain == target_domain
    or target_domain in intent_lower
    or (success and not update_domain and intent_lower == "learn")
)
capability_match = (...)
if success and domain_match and capability_match:
    aligned += 1
elif success:
    drift += 1
else:
    drift += 1
```

Then:
- ratio >= 0.75 → FOLLOWING
- ratio >= 0.40 → PARTIAL_FOLLOWING
- ratio >= 0.15 → DRIFTING
- else → **ABANDONED**

**Critical missing input:** `update_domain` is read from `payload.get("trajectory_update")` (`_omega_state.py:156-164`). The enriched episode bodies in the reflection window are built by `enriched_episode_from_episode` (`pressure_loop/episode_journal.py:144-213`). That body **does NOT include** `trajectory_update` — it carries `episode_id`, `intent`, `action`, `outcome`, `pressure`, `execution`, `brain_turns`, `topic_history`, `open_query_state`, `cascade_status`, `neuromod`, but omits the `trajectory_update` field that `prior_episode_from_episode` extracts from `ep.omega`.

Because `update_domain` is always empty string for windowed bodies:
- `domain_match` only succeeds when:
  - `target_domain` is blank (cold-start), OR
  - `target_domain in intent_lower` (e.g. intent literally contains the domain string), OR
  - `success and intent_lower == "learn"` (the LEARN fallback)
- Any successful non-LEARN episode whose intent does not contain the domain string is counted as **drift**.
- With 10 episodes, if fewer than ~2 are aligned, ratio < 0.15 → **ABANDONED**.

**Result:** A window where the system pursued the same domain via RESPOND, USE_TOOL, PROACTIVE_RESPONSE, etc. (not LEARN) reads as ABANDONED because those intents do not embed the domain name.

### Prior "always-ABANDONED" fix (Model C windowed reflection)

`docs/architecture/MODEL_C_WINDOWED_REFLECTION_MAP.md` notes a prior fix: the `TrajectoryDiff` was previously always ABANDONED because `open_reflection_window` used an empty `trajectory_snapshot`. That was fixed by reading `read_trajectory()` from lobe 9 (`pressure_loop/reflection_window.py:211-217`, `open_reflection_window:434-450`).

**This is a distinct regression:** the snapshot is now correctly populated, but `compute_trajectory_diff` cannot see episode-level trajectory updates because the enrichment drops the field.

### Proposed fix

**Two options mapped:**

**Option A — Restore `trajectory_update` into enriched bodies (minimal, data-fix)**
- Add `trajectory_update` (from `prior_episode_from_episode`) into the body dict returned by `enriched_episode_from_episode` (`episode_journal.py:181-198`).
- This makes `compute_trajectory_diff` see the actual per-episode trajectory updates and correctly match domain.
- **Risk:** low. Just restores a field the diff logic already expects.
- **Scope:** 1 file, 1 line insertion.

**Option B — Post-reflection diff override (semantic-fix)**
- After `reflect()` produces `reflection`, check `reflection.trajectory.continuation_recommended` and `reflection.trajectory.target_domain`.
- If `continuation_recommended=True` and `target_domain == snapshot.target_domain`, override `trajectory_diff` to at minimum `DRIFTING` (or `PARTIAL_FOLLOWING`), never `ABANDONED`.
- Apply this override in `run_omega_for_window` (`omega_loop/loop.py:36-54`) before packaging, or inside `package_window`.
- **Risk:** slightly higher — changes the meaning of `trajectory_diff` from "pure behavior metric" to "behavior metric corrected by model intent". Could confuse future readers.
- **Scope:** `omega_loop/loop.py` or `package/run.py`.

**Recommended:** Option A (data-fix) first. If ABANDONED still fires spuriously after that, layer Option B.

### Consumers of the ABANDONED label (blast radius)

| Consumer | File:Line | Uses `trajectory_diff`? | Impact if changed |
|----------|-----------|------------------------|-------------------|
| Omega model prompt | `omega_loop/_prompt.py:66,181` | Yes — fed as `{trajectory_diff}` into reasoning task | Prompt quality improves if label matches reality |
| Omega telemetry / HUD | `omega_loop/reflect/run.py:102-103` | Yes — emitted as `summary=omega_input.trajectory_diff` | Observational only |
| OmegaOutcomePack | `omega_loop/package/run.py:23,49` | Yes — stored in pack + emitted to temporal telemetry | Observational only |
| Temporal store (trajectory read) | `pressure_loop/temporal_store.py:120` | **Strips it** — `raw.pop("trajectory_diff", None)` | None |
| DeepSleep reseed | `deepsleep_loop/reseed/run.py:85-217` | **NO** — uses `continuation_recommended` + `next_learning_pressure` from reflection, not diff | None |
| Pressure feedback | `pressure_loop/pressure.py:84-99` | **NO** — uses `contributors_to_raise/lower/create` | None |
| Tests | `tests/core/test_windowed_omega_trajectory.py`, `tests/core/test_omega_types.py` | Assert on values | Update expected values |

**Blast radius summary:** `trajectory_diff` is **observational/telemetry only** downstream of Omega. No deterministic reseed, pressure, or trajectory logic consumes it. Changing its classification affects only the model prompt and the operator-facing HUD. Safe to fix.

### RED-first test plan for Fix 1

1. **Reproduction test** (`tests/core/test_trajectory_diff_regression.py` — NEW)
   - Build a `ReflectionWindow` with 10 episodes where `intent="RESPOND"`, `outcome.status="success"`, snapshot `target_domain="python"`.
   - Assert `compute_trajectory_diff` currently returns `ABANDONED` (red).
   - After fix (Option A), assert it returns `FOLLOWING` or `PARTIAL_FOLLOWING` (green).

2. **Enriched body field test** (`tests/core/test_enriched_episode_fields.py` — NEW or extend existing)
   - Create a `CognitiveEpisode` with `omega.trajectory_update = {"target_domain": "python"}`.
   - Call `enriched_episode_from_episode`.
   - Assert `"trajectory_update" in body` (red → green).

3. **Intake integration test** (`tests/core/test_omega_input.py` — extend)
   - Feed a window with mixed intents to `build_omega_input_for_window`.
   - Verify `trajectory_diff` is not `ABANDONED` when the same domain is pursued via non-LEARN intents.

---

## FIX 2 — Reflection prompt-trim (raw bodies → DeepSleep consolidations)

### Symptom
The reflection prompt is **prefill-dominated** (~16,752 tok / 67k chars for 10 raw enriched episode bodies). The `{episode_window}` block dwarfs the reasoning instructions.

### Where the prompt is built

| File | Line | Role |
|------|------|------|
| `omega_loop/intake/run.py` | 43–67 | **`build_omega_input_for_window`** — assembles `OmegaInput` from `window.episode_bodies`. EXISTS. |
| `omega_loop/_prompt.py` | 60–69, 177–187 | **`OMEGA_REASONING_TASK`** contains `{episode_window}` placeholder; `build_omega_prompt` fills it with `_json_block(omega_input.episode_window)`. EXISTS. |
| `omega_loop/reason/run.py` | 34–61 | Telemetry emits prompt composition (block sizes). EXISTS. |

### What the enriched body contains (cost drivers)

`enriched_episode_from_episode` (`pressure_loop/episode_journal.py:144-213`) produces:

```python
body = {
    "episode_id": ...,
    "intent": ...,
    "action": ...,
    "outcome": ...,
    "pressure": <full pressure snapshot dict>,
    "execution": <execution_action_pack + state>,
    "brain_turns": [ <list of full brain_turn dicts per turn> ],   # ← largest variable
    "topic_history": ...,
    "open_query_state": [ <ALL currently open queries, per-episode> ],  # ← duplicated 10×
    "cascade_status": ...,
    "neuromod": <neuromod dict>,
}
```

**Top bloat sources:**
1. **`brain_turns`** — each turn carries `cycle_id`, `ingress_class`, `outcome_hint`, `action_kind`, `ingress_confidence`, `pressure_contribution`, `response_summary`, `memory_operation`, `tool_or_memory_context`, `store_receipt`. If an episode has 5–10 turns, this is ~50–100 nested dicts.
2. **`open_query_state`** — `_active_open_query_state()` (`episode_journal.py:131-141`) returns **all currently open questions** at close time. With many open queries, this is large. It is **duplicated into every one of the 10 episode bodies** — O(n²) explosion.
3. **`pressure`** — full pressure snapshot per episode (contributors, aggregate, etc.).
4. **`execution`** — full execution_action_pack per episode.

### What DeepSleep per-episode consolidation produces

`run_deepsleep_for_episode` (`deepsleep_loop/loop.py:60-170`) produces a `DeepSleepOutcomePack` containing:

```python
pack.journal_entry   # → JournalEntry dict from consolidate_phase.consolidate()
pack.updated_mastery  # → mastery store snapshot
```

The `JournalEntry` (`deepsleep_loop/_deepsleep_state.py:132-172`) contains:

```python
JournalEntry(
    episode_id=...
    timestamp=...
    intent=...
    action=...
    outcome=...
    trajectory_snapshot=...
    omega_reasoning=...
    observations=...
    artifacts=...
    mastery_effects=...
    identity_effects=...
    semantic_effects=...
    trajectory_effects=...
    failure_effects=...
    query_effects=...
    scope=...
)
```

**Key differences from raw enriched body:**
- **NO `brain_turns`** — replaced by `observations` (summarized) and `artifacts`.
- **NO `open_query_state`** — replaced by `query_effects` (per-episode delta only).
- **NO `pressure`** — replaced by `mastery_effects` / `identity_effects`.
- **NO `execution`** — `intent` + `action` + `outcome` preserved directly.
- **Compact effects** — each effect dict is a distilled delta, not a full state dump.

### Is the consolidation stored / accessible per episode in the window?

**NO — not in the window.**

- `cognition_chain.py:95-136` (`_maybe_run_per_episode_deepsleep`) runs `run_deepsleep_for_episode` and extracts only `journal_entry.get("mastery_effects")` into `store.append_episode_mastery_delta(...)` (`episode.py:691-710`).
- The **full `journal_entry`** is persisted to `deepsleep_loop/journal_store.py` (`append_journal_entry`, JSONL) but is **not attached to the `ReflectionWindow`**.
- `ReflectionWindow` stores only: `episode_bodies` (raw), `episode_mastery_deltas` (just the mastery_effects dict), `cascade_status_by_episode`, `episodes`.

**Accessible if we read back:** `read_journal(n)` (`journal_store.py:42-72`) returns last N `JournalEntry` records. We could cross-match by `episode_id`, but there is no indexed lookup — it would require scanning the JSONL.

### Proposed fix

**Two options mapped:**

**Option A — Compact the enriched body in-place (no new storage)**
- In `enriched_episode_from_episode` (`episode_journal.py`), add an optional `compact=True` mode (or a separate `compact_episode_for_reflection` function) that produces:
  ```python
  compact_body = {
      "episode_id": base.episode_id,
      "intent": base.intent,
      "action": base.action,
      "outcome": base.outcome,
      "cascade_status": base.cascade_status,
      "topic_history": topic_history,
      # brain_turns → COUNT only (or omit entirely)
      "brain_turn_count": len(turns),
      # open_query_state → OMIT (already present once at window level via open_questions_summary)
      # pressure → OMIT (already present at window level via trajectory_snapshot / pressure reset context)
      # neuromod → PRESERVE (small)
      "neuromod": body.get("neuromod"),
  }
  ```
- In `build_omega_input_for_window` (`omega_loop/intake/run.py`), switch to compact bodies.
- **Risk:** Omega may lose granularity for "behavioral drift" and "repeated failures/successes" if it cannot read the raw turn content. However, the prompt already instructs reasoning over "observed evidence across ALL episodes" — the consolidation effects (`mastery_changes`) are already fed separately.

**Option B — Attach full journal entries to the window (richer, more work)**
- Extend `EpisodeStore.append_episode_mastery_delta` (or add a new `append_episode_consolidation`) to also store the full `journal_entry` dict on the window, e.g. in a new `ReflectionWindow.episode_consolidations` list.
- In `build_omega_input_for_window`, read these consolidations and feed them as `episode_window`.
- **Risk:** changes window serialization (`reflection_window.json` size increases). Requires migration of `ReflectionWindow.from_dict` / `to_dict`.
- **Scope:** `pressure_loop/episode.py`, `pressure_loop/reflection_window.py`, `omega_loop/intake/run.py`.

**Option C — Hybrid (recommended): compact raw body + deduplicate open_query_state**
- Keep raw bodies but:
  1. **Remove `open_query_state`** from every episode body. It is already window-level data; include it once in the prompt via `open_questions_summary` (already present).
  2. **Replace `brain_turns`** with `brain_turn_count` + `brain_turn_summary` (or first/last turn only).
  3. **Trim `pressure`** to `aggregate_pressure` + `opened_by` only.
  4. **Trim `execution`** to `action` + `state` only.
- This is the smallest code change with the biggest token reduction.

### Estimated token reduction

Assuming current ~67k chars / ~16.7k tok for 10 episodes:
- Removing `open_query_state` duplication: if 10 open queries × 200 chars each × 10 episodes = 20k chars → **~5k tok saved**.
- Replacing `brain_turns` with count: if 5 turns × 300 chars × 10 episodes = 15k chars → **~3.7k tok saved**.
- Trimming `pressure` + `execution`: ~5k chars → **~1.2k tok saved**.
- **Total estimate: ~10k tok reduction (60% smaller prefill)** from a compacted body alone.

Using full `JournalEntry` would save even more (no `brain_turns`, no `pressure`, no `execution`), possibly **~70-80% reduction**.

### Reflection quality risk

| Data element | Needed for prompt tasks? | Present in consolidation? | Risk if dropped |
|--------------|-------------------------|--------------------------|-----------------|
| `intent` / `action` / `outcome` | Yes — trajectory adherence, repeated successes/failures | Yes (JournalEntry) | None |
| `brain_turns` (full) | Partial — "behavioral patterns", "strategic effectiveness" | No (replaced by `observations`) | **Medium** — Omega loses verbatim turn content, but `observations` may suffice |
| `pressure` (full) | Partial — "identity pressure across episodes" | No (replaced by `identity_effects`) | **Low** — effects capture pressure impact |
| `execution` (full) | Partial — "strategic effectiveness" | No (replaced by `artifacts`) | **Low** |
| `open_query_state` | Yes — "open query evolution", "open question lifecycle" | Partial (`query_effects` per episode) | **Low** if included once at window level; **Medium** if fully dropped |
| `neuromod` | Partial — "mastery and identity pressure" | No | **Low** — `mastery_changes` already carries deltas |
| `topic_history` | Yes — "mastery progression", "topic resolution effectiveness" | No | **Medium** — needed for domain tracking; should be preserved |

**Mitigation:** Keep `topic_history` + `neuromod` + `cascade_status` in any compact representation. Ensure `open_questions_summary` at window level is accurate and up to date.

### RED-first test plan for Fix 2

1. **Prompt size regression test** (`tests/core/test_omega_prompt_size.py` — NEW)
   - Build an `OmegaInput` with 10 realistic enriched bodies (fixture with brain_turns + open queries).
   - Call `build_omega_prompt` and measure `len(user)`.
   - Assert it is **below a threshold** (e.g. 30k chars) after the fix (red → green).

2. **Compact body schema test** (`tests/core/test_compact_episode_body.py` — NEW)
   - Call a new `compact_episode_for_reflection(body)` helper.
   - Assert `brain_turns` absent, `brain_turn_count` present.
   - Assert `open_query_state` absent.
   - Assert `intent`, `outcome`, `topic_history` still present.

3. **Intake integration test** (`tests/core/test_omega_input.py` — extend)
   - Verify `build_omega_input_for_window` uses compacted bodies when configured.
   - Verify `OmegaInput.episode_window` entries do not contain `brain_turns` or `open_query_state`.

4. **Quality smoke test** (manual / fixture-based)
   - Feed a window with known patterns (3 LEARN successes, 2 RESPOND failures, etc.) through the compacted prompt.
   - Verify Omega reflection still identifies "repeated successes" and "trajectory adherence" correctly.

---

## Files touched summary

| Fix | File | Change type | Exists / New |
|-----|------|-------------|--------------|
| Fix 1 | `omega_loop/_omega_state.py` | Read-only mapping of `compute_trajectory_diff` | Exists |
| Fix 1 | `pressure_loop/episode_journal.py` | Map missing `trajectory_update` in enrichment | Exists |
| Fix 1 | `omega_loop/loop.py` | Map override site (Option B) | Exists |
| Fix 1 | `omega_loop/package/run.py` | Map `trajectory_diff` consumer | Exists |
| Fix 1 | `pressure_loop/temporal_store.py` | Map strip of `trajectory_diff` | Exists |
| Fix 2 | `omega_loop/intake/run.py` | Map `episode_window` assembly | Exists |
| Fix 2 | `omega_loop/_prompt.py` | Map prompt formatting | Exists |
| Fix 2 | `pressure_loop/episode_journal.py` | Map enrichment bloat sources | Exists |
| Fix 2 | `deepsleep_loop/loop.py` | Map per-episode consolidation output | Exists |
| Fix 2 | `deepsleep_loop/_deepsleep_state.py` | Map `JournalEntry` schema | Exists |
| Fix 2 | `deepsleep_loop/journal_store.py` | Map journal persistence | Exists |
| Fix 2 | `cognition_chain.py` | Map per-episode DeepSleep handoff + mastery delta capture | Exists |
| Fix 2 | `pressure_loop/episode.py` | Map `append_episode_mastery_delta` window storage | Exists |
| Fix 2 | `pressure_loop/reflection_window.py` | Map `ReflectionWindow` fields | Exists |
| Both | `tests/core/test_trajectory_diff_regression.py` | NEW test | New |
| Both | `tests/core/test_omega_prompt_size.py` | NEW test | New |
| Both | `tests/core/test_compact_episode_body.py` | NEW test | New |

---

*Map delivered: Devin read-only lane. No code modified.*
