# Topic Resolution + Mastery-Guided Learning Architecture

> **Historical snapshot — superseded 2026-08-03.** Its window-level Omega and
> DeepSleep integration details are retired. See `AGENTS.md` and
> `docs/BASELINE_2026-08-03.md`.

**Status:** COMPLETE + verified across 4 commits  
**Commits:** P1 `fe8a0247` (engine + cascade), P2 `165ebd54` (mastery moves + reflection context), cleanup `bbd66c38` (human topic names + unified slug keys), DeepSleep-split `d7bf6549` (per-episode consolidation, no double-count)

---

## 1. PROBLEM -- Learning Material Masking

The windowed Omega reflection diagnosed a structural pathology: **learning material masked actual topics**, which produced:

- **No mastery direction** -- episodes targeted `"learning material"`, `"general knowledge"`, or `"unknown"`, so DeepSleep mastery updates accumulated credit against meaningless domain IDs.
- **No trajectory** -- the trajectory snapshot received `target_domain="learning material"`, rendering `TrajectoryDiff` meaningless and Omega's trajectory recommendations incoherent.
- **Wasted acquisition cycles** -- LEARN intent fired with generic queries, producing low-salience memory that did not close open questions or fill mastery gaps.

The fix is a **deterministic, fail-hard topic-resolution pipeline** that sits between Meta A's LEARN branch and AUTO execution, coupled to a **per-episode mastery credit system** and **window-level Omega reflection** over concrete topics.

---

## 2. Topic Resolution Engine

**File:** `cognition/topic_resolution/resolve_topic.py`

### 2.1 Core Type

```python
@dataclass(frozen=True)
class ResolvedTopic:
    topic: str      # human-readable topic
    domain: str     # slugified domain_id
    source: str     # which cascade source won
    confidence: float
```

(`resolve_topic.py:50-56`)

### 2.2 Six-Source Priority Cascade (DESCENDING)

| Priority | Source | Function | Lines |
|----------|--------|----------|-------|
| P1 | `operator_query` | `_resolve_operator_query(state)` | `resolve_topic.py:132-140` |
| P2 | `open_question` | `_resolve_open_question()` | `resolve_topic.py:143-177` |
| P3 | `mastery_gap` | `_resolve_mastery_gap()` | `resolve_topic.py:180-200` |
| P4 | `learning_debt` | `_resolve_learning_debt(state)` | `resolve_topic.py:222-242` |
| P5 | `trajectory` | `_resolve_trajectory()` | `resolve_topic.py:245-255` |
| P6 | `pressure_meta` | `_resolve_pressure_meta(state)` | `resolve_topic.py:258-264` |

**Entry point:** `resolve_learning_topic(state)` (`resolve_topic.py:267-284`) iterates the six resolvers in order and returns the first concrete topic.

### 2.3 IRON RULE -- No Fallback, No Generic Topics

- **Forbidden topics** (`_FORBIDDEN_TOPICS`, `resolve_topic.py:19-27`): `"learning material"`, `"general knowledge"`, `"random"`, `"unknown"`, `"general"`. Any resolver returning these is treated as `_no_topic()`.
- **`_is_concrete_topic(topic)`** (`resolve_topic.py:86-88`) normalizes and gates against the forbidden set.
- **No fallback:** if all six sources fail, the engine returns `NO_TOPIC_AVAILABLE` (`resolve_topic.py:17`). There is **no** default-to-operator-text or default-to-trajectory fallback. The mind must abort the LEARN action rather than acquire noise.

### 2.4 Operator-Query Extraction

`_resolve_operator_query` (`resolve_topic.py:132-140`) scans `state.target_str` then `state.text`, strips LEARN prefixes (`"learn "`, `"study "`, `"teach me "`, `"research "`, `"explore "`, `"about "` -- `resolve_topic.py:29-36`), and runs regex question patterns (`"how does X work"`, `"what is X"`, `"explain X"`, `"tell me about X"`, `"why does X occur"` -- `resolve_topic.py:38-47`).

### 2.5 Open-Question Scoring

`_resolve_open_question` (`resolve_topic.py:143-177`) scores questions by `urgency * age_seconds * recurrence`, preferring old, urgent, repeated questions. Confidence is fixed at `0.5`.

### 2.6 Mastery-Gap Selection

`_resolve_mastery_gap` (`resolve_topic.py:180-200`) loads the full mastery store (`deepsleep_loop.mastery_store.load_mastery`), skips mastered domains, and selects the domain with the largest `target_mastery - knowledge_mastery` gap. Confidence is clamped to the gap magnitude.

---

## 3. Meta Plug-In -- LEARN Branch + SKIP_NO_TOPIC Abort

**File:** `meta_loop/tool/run.py`

### 3.1 LEARN Branch

When Meta A action maps the Global A intent to `LEARN`, the tool phase calls the resolution engine (`meta_loop/tool/run.py:51-73`):

```python
resolved = resolve_learning_topic(state)
if resolved is None or resolved.topic == NO_TOPIC_AVAILABLE:
    state.tool = None
    state.execution_inputs = {}
    state.execution_intent = "SKIP_NO_TOPIC"
    state.tool_reason = (
        "NO_TOPIC_AVAILABLE -- no resolvable learning target; aborting LEARN"
    )
else:
    state.tool = _learn_default_tool()          # default: web_fetch
    state.tool_reason = f"Acquire knowledge on topic: {resolved.topic}"
    state.execution_inputs = {
        "query": resolved.topic,
        "domain": resolved.domain,
        "topic_source": resolved.source,
    }
    state.execution_intent = "Acquire knowledge inputs for learning"
```

(`meta_loop/tool/run.py:57-73`)

### 3.2 Pack Phase -- IDLE Abort

The pack phase detects `SKIP_NO_TOPIC` and overrides the action to `IDLE` (`meta_loop/pack/run.py:97-100`):

```python
if state.tool is None and state.execution_intent == "SKIP_NO_TOPIC":
    action = "IDLE"
```

This produces an `ExecutionActionPack` with `action="IDLE"`, `tool=None`, and `execution_intent="SKIP_NO_TOPIC"`. AUTO sees `IDLE` and skips model inference, so no compute is wasted on a generic or missing topic.

---

## 4. Mastery-Guided Learning

### 4.1 Domain Normalization -- Unified Slug

`normalize_domain_id(target)` (`deepsleep_loop/mastery_store.py:25-29`) delegates to `knowledge_extractor.derive.run._slug`, producing a stable ASCII slug from any human topic string. This is the **single normalization path** used by:
- Topic Resolution Engine (`resolve_topic.py:94`)
- Mastery store reads/writes (`mastery_store.py:25-29`, `mastery/run.py:137`)
- DeepSleep semantic promotion

**Human names preserved:** `MasteryRecord.domain_name` stores the original human topic (`deepsleep_loop/_deepsleep_state.py:293`), while `domain_id` stores the slug (`bbd66c38`).

### 4.2 Episode-Target Extraction

`_episode_target(entry, fallback="")` (`deepsleep_loop/mastery/run.py:35-53`) **prefers the resolved topic** from the execution inputs:

1. `execution.execution_action_pack.execution_inputs.query` -- the resolved topic carried through Meta A
2. Fallback to `outcome.goal.target` -- the raw Global A target

This ensures mastery credit is awarded against the **concrete resolved topic**, not a generic LEARN fallback.

### 4.3 Mastery Acquisition Bumps

`_apply_mastery_for_episode()` (`deepsleep_loop/mastery/run.py:125-170`) applies the **Application Credit Law**:

- `LEARN` intent -- increments `knowledge_mastery` by `deepsleep_mastery_knowledge_increment()` (default configurable via `config/deepsleep.yaml`)
- `USE_TOOL` intent -- increments `application_mastery` by `deepsleep_mastery_application_increment()`
- Other intents -- no mastery update

Only **successful episodes** receive credit (`_episode_success`, `mastery/run.py:62-75`).

### 4.4 Status Ladder

`status_for(knowledge_mastery)` (`deepsleep_loop/mastery_store.py:32-44`):
- `learning` -- `< 0.60`
- `operational` -- `>= 0.60` and `< 0.80`
- `mastered` -- `>= 0.80`

Thresholds are configurable via `pressure_loop.policy.deepsleep_mastery_status_thresholds()`.

---

## 5. DeepSleep Per-Episode Split

**Commit:** `d7bf6549`

### 5.1 Problem Solved

Before the split, DeepSleep ran **only** at the 10th episode (window-level). Mastery, failure, and semantic updates were batched per-window, which:
- Delayed recall of learned material by up to 9 episodes
- Made journal attribution ambiguous (which episode earned the credit?)
- Caused trajectory-decay to run 10x in one pass, over-correcting

### 5.2 Two-Level Design

| Level | Phases | Trigger | Omega Dependency |
|-------|--------|---------|------------------|
| **Episode-level** | mastery, failure, semantic, consolidate, consolidate_memory | `store.close()` after every episode | None (deterministic) |
| **Window-level** | identity, trajectory-decay, query_lifecycle, reseed, closure, projection | 10th-episode locked window, after Omega | Omega-coupled or 10x-corruption risk |

### 5.3 Episode-Level Path

`cognition_chain._maybe_run_per_episode_deepsleep()` (`cognition_chain.py:95-125`) runs after every `store.close()`:

```python
body = store.last_closed_body()
run_deepsleep_for_episode(store, episode_id, body)
```

This invokes `_run_deepsleep_episode_core()` (`deepsleep_loop/loop.py:73-170`), a **truncated chain**:

1. `intake` -- `build_deepsleep_input_for_episode(episode_body, episode_id)` (`deepsleep_loop/intake/run.py:44-55`)
2. `mastery` -- `update_mastery(ds_input)` (`deepsleep_loop/mastery/run.py:173-221`)
3. `failure` -- `consolidate_failures(ds_input)`
4. `semantic` -- `promote_semantic(ds_input, mastery_records)`
5. `consolidate` -- `consolidate(..., scope="episode")`
6. `consolidate_memory` -- `consolidate_to_memory(context_id, journal_entry, ...)`

**Skipped in episode-level:** identity, trajectory, query_lifecycle, reseed, projection, closure.

### 5.4 Window-Level Path

`cognition_chain._maybe_run_window_reflection()` (`cognition_chain.py:128-166`) runs when the window locks (10 episodes):

1. **Omega** -- `run_omega_for_window(window)`
2. **DeepSleep** -- `run_deepsleep_for_window(store, window, omega_outcome, skip_episode_level_phases=True)` (`cognition_chain.py:153-158`)

`skip_episode_level_phases=True` tells `_run_deepsleep_core()` (`deepsleep_loop/loop.py:173-384`) to **skip** mastery, failure, and semantic phases, because the per-episode path already executed them. It runs:
- identity
- trajectory (with decay)
- query_lifecycle
- consolidate (with `scope="window"`)
- consolidate_memory
- projection
- reseed
- closure (`finalize_window` stages reseed only)

### 5.5 Double-Count Prevention

`skip_episode_level_phases` is the explicit guard. When `True`:
- `mastery` loads existing records but does not re-run updates (`loop.py:198-199`)
- `semantic` and `failure` are bypassed entirely (`loop.py:218`, `loop.py:234-241`)

This ensures a domain only receives **one** knowledge_mastery bump per episode, even when both episode-level and window-level DeepSleep fire.

### 5.6 Journal Scope Tags

The `consolidate` phase receives a `scope` argument:
- `scope="episode"` for per-episode consolidation
- `scope="window"` for window-level consolidation

This tag will be used to distinguish episode-granular vs window-granular journal entries in downstream analytics.

### 5.7 Test Isolation

`tests/conftest.py` isolates **all** DeepSleep state paths inside a `tmp_path` fixture (`conftest.py:112-129`):

```python
saved_policy = {
    "deepsleep_journal_path": policy.deepsleep_journal_path,
    "deepsleep_mastery_path": policy.deepsleep_mastery_path,
    ...
}
policy.deepsleep_journal_path = lambda policy=None: deepsleep_root / "journal.jsonl"
policy.deepsleep_mastery_path = lambda policy=None: deepsleep_root / "mastery.json"
...
```

Every DeepSleep test (mastery, identity, semantic, trajectory, failure, query, projection, window consolidation, per-episode split) uses this fixture to avoid polluting the developer's live state.

---

## 6. Reflection Integration -- OmegaInput Enrichment

**File:** `omega_loop/intake/run.py`

Omega's intake now carries three enriched fields beyond the raw episode window:

### 6.1 `current_mastery_state`

`_current_mastery_state()` (`omega_loop/intake/run.py:17-30`) loads the full mastery store and maps each record to:

```python
{
    domain_id: {
        "domain_name": record.domain_name,
        "knowledge_mastery": float(record.knowledge_mastery),
        "application_mastery": float(record.application_mastery),
        "status": record.status,
    }
}
```

This lets Omega reason about **which domains are learning, operational, or mastered** across the 10-episode window.

### 6.2 `open_questions_summary`

`_open_questions_summary()` (`omega_loop/intake/run.py:33-40`) counts:
- `open` -- questions with `status == STATUS_OPEN`
- `resolved` -- questions with `status == STATUS_RESOLVED`

Omega uses this to assess query lifecycle health (stale open questions, resolution rate).

### 6.3 Episode `topic_history`

`enriched_episode_from_episode()` (`pressure_loop/episode_journal.py:144-198`) builds the maintained-window body with a `topic_history` field:

```python
topic_history = {
    "topic": inputs.get("query"),      # resolved topic
    "domain": inputs.get("domain"),    # slugified domain
    "source": inputs.get("topic_source"),  # cascade winner (e.g. "mastery_gap")
}
```

This is extracted from `execution_action_pack.execution_inputs` (`episode_journal.py:168-179`), so every episode body in the window carries a **traceable topic provenance**.

### 6.4 Prompt Consumption

`build_omega_prompt()` (`omega_loop/_prompt.py:167-176`) injects all three into the user prompt:

```python
OMEGA_REASONING_TASK.format(
    trajectory_snapshot=_json_block(omega_input.trajectory_snapshot),
    trajectory_diff=omega_input.trajectory_diff,
    episode_window=_json_block(omega_input.episode_window),
    current_mastery_state=_json_block(omega_input.current_mastery_state),
    open_questions_summary=_json_block(omega_input.open_questions_summary),
)
```

The Omega prompt explicitly asks the model to analyze:
- `6. Mastery progression (use current_mastery_state and per-episode topic_history)`
- `11. Topic resolution effectiveness (how often did LEARN resolve a concrete topic vs abort?)`
- `12. Open question lifecycle (open vs resolved ratio, stale questions)`

(`omega_loop/_prompt.py:83-89`)

---

## 7. Data Flow -- End to End

```
Pressure tick
    |
    v
gather_contributors()  -- may emit "mastery_gap" or "learning_debt" contributors
    |
    v
evaluate() -- opens episode when dominant contributor reaches ceiling + margin
    |
    v
EpisodeStore.open()
    |
    v
Global A -- ActionGoal(intent=LEARN, target="...")
    |
    v
Meta A -- action=USE_TOOL
    |
    v
Meta A tool phase -- resolve_learning_topic(state)
    |   - P1 operator_query > P2 open_questions > P3 mastery_gap
    |   - P4 learning_debt > P5 trajectory > P6 pressure_meta
    |
    +-- NO_TOPIC_AVAILABLE --> execution_intent=SKIP_NO_TOPIC --> action=IDLE (pack)
    |
    +-- ResolvedTopic(topic, domain, source, confidence)
            |
            v
        ExecutionActionPack.execution_inputs = {
            "query": resolved.topic,
            "domain": resolved.domain,
            "topic_source": resolved.source,
        }
            |
            v
        AUTO -- runs web_fetch (or configured default tool) with concrete query
            |
            v
        Meta B + Global B -- outcome recorded
            |
            v
        EpisodeStore.close() -- episode archived
            |
            +-- _maybe_run_per_episode_deepsleep()
            |       |
            |       v
            |   build_deepsleep_input_for_episode(body)
            |       |
            |       v
            |   mastery.update_mastery() -- +knowledge_mastery for LEARN
            |   failure.consolidate_failures()
            |   semantic.promote_semantic()
            |   consolidate(scope="episode")
            |   consolidate_to_memory()
            |
            +-- reflection window append (enriched body with topic_history)
                |
                v
            Window locks at 10 episodes?
                |
                +-- YES: _maybe_run_window_reflection()
                        |
                        v
                    Omega intake -- build_omega_input_for_window()
                        |   - episode_window (10 enriched bodies)
                        |   - trajectory_snapshot
                        |   - trajectory_diff
                        |   - current_mastery_state
                        |   - open_questions_summary
                        |
                        v
                    Omega reason -- one model pass
                        |
                        v
                    Omega reflect -- OmegaReflection + TrajectoryUpdate
                        |   - pressure_feedback: contributors_to_raise/lower/create
                        |
                        v
                    Omega package -- persist to lobe 9 (temporal)
                        |
                        v
                    DeepSleep window -- run_deepsleep_for_window(skip_episode_level_phases=True)
                        |   - identity, trajectory-decay, query_lifecycle
                        |   - consolidate(scope="window")
                        |   - consolidate_memory, projection, reseed, closure
                        |
                        v
                    archive_and_reset_window()
                        |
                        v
                    Next pressure tick -- gather_contributors()
                        |
                        +-- reseed from DeepSleep (mastery_gap, application_gap, trajectory_gap, outstanding_question)
                        +-- Omega pressure_feedback:
                                raise/create -> stage_reseed(Contributor(source="omega", type, weight))
                                lower -> stage_reseed_suppress(type, weight)
```

### 7.1 Pressure-Loop Closure

Omega `pressure_feedback` is translated by DeepSleep reseed (`deepsleep_loop/reseed/run.py:148-202`) into concrete pressure contributors for the **next** tick:

- **Raise / Create** -- additive `Contributor(source="omega", type, weight, metadata={"origin": "omega_pressure_feedback"})` staged via `stage_reseed()` (`pressure_loop/pressure.py:84-89`)
- **Lower** -- suppression overlay staged via `stage_reseed_suppress(type, weight)` (`pressure_loop/pressure.py:91-99`), applied in `gather_contributors()` as `weight = max(0.0, weight - suppress[type])`

This closes the metacognitive loop: **Omega observes a 10-episode window and steers the next pressure cycle's intent.**

---

## 8. Live-Proof Evidence

The system was live-proven with real pressure ticks after the split landed:

- **Mastery +1/episode linear** -- per-episode DeepSleep awarded exactly one `knowledge_mastery` increment per successful LEARN episode. The mastery store grew monotonically with `evidence_count` matching episode count.
- **No window double-count** -- window-level DeepSleep with `skip_episode_level_phases=True` did not re-increment knowledge_mastery. The delta between window-level `consolidate` and the pre-existing mastery store was zero for all knowledge/application fields.
- **Trajectory-decay catch** -- `_mark_superseded()` and `passes_since_reinforcement` decay ran exactly once per window, not 10x. This prevented the prior over-correction where trajectory confidence collapsed after a single inactive window.
- **Topic concreteness** -- no `"learning material"` or `"general"` entries appeared in `topic_history` after the IRON RULE gate was enforced. Aborted LEARN intents (SKIP_NO_TOPIC) were correctly logged as `action=IDLE` with zero AUTO cost.

---

## 9. Phase 3 — AS-BUILT (2026-06-10)

| Enhancement | Status | Where |
|-------------|--------|-------|
| **Mastery-delta narrative (3a)** | **DONE** | `ReflectionWindow.episode_mastery_deltas` → `OmegaInput.mastery_changes`; `tests/cognition/test_topic_resolution_3a.py` |
| **Memory-tool resolved_topic (3b)** | **DONE** | `meta_loop/memory/run.py` — memory-first LEARN on resolved topic; `tests/cognition/test_topic_resolution_3b.py` |
| **Trajectory-preferential cascade (3c)** | **DONE** | 3c.1 severe-gap + trajectory default; 3c.2 THE FOLD + persistence/blockage; `tests/cognition/test_topic_resolution_3c1.py`, `test_topic_resolution_3c2.py` |

**Follow-on (2026-06-10):**

| Item | Status |
|------|--------|
| `QuerySpec.domain` for MRIL recall (3b optional) | **DONE** — `meta_loop/memory/run.py`, `auto_loop/mril_tools/remember.py` |
| Cross-window `mastery_changes` (P2 defer) | **DONE** — `prior_window_mastery_changes` in `OmegaInput`; `load_latest_archived_reflection_window()` |
| Omega prompt token compaction (Fix 2) | **DONE** — `compact_episode_for_reflection`; `test_omega_prompt_size.py` |

**Still deferred (optional):**

| Item | Notes |
|------|-------|
| Blockage recency / time-decay on `last_episode` | See `TOPIC_RESOLUTION_3C2_SPEC.md` § Deferred |
| Soft trajectory *bias layer* (vs hard cascade) | Design path not taken; hard cascade + fold shipped instead |
| HUD surface when THE FOLD fires | Out of scope per 3c.2 spec |

---

## 10. Canonical File Reference Map

| Concern | File | Key Lines |
|---------|------|-----------|
| Topic resolution engine | `cognition/topic_resolution/resolve_topic.py` | 1-284 |
| Forbidden topic set | `cognition/topic_resolution/resolve_topic.py` | 19-27 |
| Six-source cascade | `cognition/topic_resolution/resolve_topic.py` | 267-284 |
| Meta A LEARN branch | `meta_loop/tool/run.py` | 51-73 |
| SKIP_NO_TOPIC -> IDLE | `meta_loop/pack/run.py` | 97-100 |
| Mastery store + slug | `deepsleep_loop/mastery_store.py` | 25-29 |
| Mastery status ladder | `deepsleep_loop/mastery_store.py` | 32-44 |
| Episode target extraction | `deepsleep_loop/mastery/run.py` | 35-53 |
| Mastery acquisition bump | `deepsleep_loop/mastery/run.py` | 125-170 |
| Per-episode DeepSleep core | `deepsleep_loop/loop.py` | 73-170 |
| Window DeepSleep core | `deepsleep_loop/loop.py` | 173-384 |
| Episode-level intake | `deepsleep_loop/intake/run.py` | 44-55 |
| Window-level intake | `deepsleep_loop/intake/run.py` | 10-41 |
| Omega input enrichment | `omega_loop/intake/run.py` | 17-64 |
| Omega prompt build | `omega_loop/_prompt.py` | 167-176 |
| Omega prompt task text | `omega_loop/_prompt.py` | 59-113 |
| Enriched episode body | `pressure_loop/episode_journal.py` | 144-198 |
| Per-episode hook | `cognition_chain.py` | 95-125 |
| Window reflection hook | `cognition_chain.py` | 128-166 |
| Pressure reseed staging | `pressure_loop/pressure.py` | 84-89 |
| Pressure suppress staging | `pressure_loop/pressure.py` | 91-99 |
| DeepSleep reseed generation | `deepsleep_loop/reseed/run.py` | 85-217 |
| Test state isolation | `tests/conftest.py` | 112-129 |
