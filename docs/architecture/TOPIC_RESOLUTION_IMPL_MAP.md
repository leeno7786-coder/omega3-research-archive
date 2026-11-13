# TOPIC_RESOLUTION_IMPL_MAP.md
## OMEGA 3.x — Autonomous Knowledge Target Selection & Mastery-Guided Learning

**Work order ref:** `#07a08049` (Claude → Devin)  
**Spec source:** `docs/architecture/# WORK ORDER.txt` — OMEGA 3.x Autonomous Knowledge Target Selection & Mastery-Guided Learning  
**Status:** MAP ONLY — no code changes. Identifies EXISTS vs NEW, cites file:line, and traces plug-in points.

---

## 1. NEW COMPONENT

| Item | Path | Status |
|------|------|--------|
| Topic Resolution Engine | `cognition/topic_resolution/resolve_topic.py` | **NEW** (directory + file do not exist) |
| `ResolvedTopic` dataclass | inside `resolve_topic.py` | **NEW** |
| `resolve_learning_topic(state)` | inside `resolve_topic.py` | **NEW** |

**Iron rule from spec:** LEARN NEVER emits `learning material` / `general knowledge` / `random` / `unknown`. If no topic resolves, return `NO_TOPIC_AVAILABLE` and abort the learn cleanly (Cat-2 observe-and-skip, NOT a crash).

---

## 2. PRIORITY SOURCE MAP — P1..P6

### P1 — Operator Query (highest authority)

**How an operator "learn X" reaches Meta A:**

1. Operator text enters via pressure contributor.  
   `pressure_loop/contributors.py:26-27` — `epistemic_request` / `phatic` → `operator_query`.  
   `pressure_loop/contributors.py:34-106` — `make_contributor` normalizes `operator_message` → `operator_query` / `operator_command` / `operator_response`.

2. `global_loop/intent/run.py:42-45` — explicit learn prefix matcher (`learn `, `study `, `teach me `, `research `, `explore `) → intent `LEARN`.

3. `global_loop/extract/run.py:34-38` — `_extract_target` strips the prefix and returns the rest as the target string (e.g. `"transformers"`).

4. `meta_loop/action/run.py:60-71` — `MetaAState.target_str` is set from `action_goal.target` (the extracted rest).  
   `meta_loop/action/run.py:72` — `meta = _contributors_metadata(mi)`, which merges every contributor's metadata into `state.meta`.

5. `meta_loop/tool/run.py:52` — current LEARN branch reads `topic = target_str or text or "learning material"`.  
   The **operator query text IS `target_str`** when the prefix matches.

**Spec directive:** `source="operator_query"` when `target_str` is non-empty and the Global intent was `LEARN` triggered by operator message.

**Conclusion:** P1 is **EXISTS** — the data already flows to `MetaAState.target_str` and `MetaAState.text`. No new threading required. The engine just needs to read `state.target_str` / `state.text`.

---

### P2 — Active Open Questions

**Corpus location & structure:**

- Canonical store: `guardian/state/omega_open_questions.jsonl`  
  `pressure_loop/open_questions.py:72` — `OPEN_QUESTIONS_PATH`.

- Record schema: `OpenQuestion` dataclass  
  `pressure_loop/open_questions.py:86-121` — fields: `question_id`, `curiosity_id`, `act`, `question_text`, `asked_cycle`, `asked_at`, `status`, `answer_text`, `answered_cycle`, `answered_at`, `resolved_by`, `resolved_note`, `resolved_at`, `rationale`, `global_operator_outreach`, `outreach_id`, `outreach_status`, `fingerprint`, `response_seed`, `source`, `source_id`, `episode_id`, `source_action_cycle_id`, `urgency`, `extra`, `spoken_cycle`, `spoken_at`, `memory`.

- Read API: `list_open()` → `List[OpenQuestion]`, sorted `asked_at` desc  
  `pressure_loop/open_questions.py:601-608`.

- **No scoring function EXISTS** for `confidence × age × recurrence`. The `urgency` float field exists on `OpenQuestion` (`line 116`) but is not populated by current write paths.  
  `list_open()` returns newest-first (`asked_at` desc), which is a naive proxy for age but NOT the spec's `confidence × age × recurrence` composite.

**What must be NEW for P2:**
- A scoring function that computes `confidence × age × recurrence` across open questions.
- `recurrence` requires counting how many times a similar topic has appeared (no recurrence index exists today).
- `age` = `(now - asked_at)` in seconds, using `core.symphony_time.now_iso()`.
- If no `urgency` / confidence field is populated, the scorer may need to default confidence to a baseline (e.g. 0.5) or derive it from `rationale` / `act`.

**Conclusion:** P2 data **EXISTS** (jsonl + `list_open()`). P2 scoring logic is **NEW**.

---

### P3 — Knowledge Mastery Gaps

**Store location & schema:**

- DeepSleep canonical store: `deepsleep_loop/state/mastery.json` (configurable via `deepsleep_mastery_path()`)  
  `pressure_loop/policy.py:1289` — `deepsleep_mastery_path()`.

- Loader: `deepsleep_loop/mastery_store.py:47-71` — `load_mastery(path) -> dict[str, MasteryRecord]`.

- Record schema: `MasteryRecord`  
  `deepsleep_loop/_deepsleep_state.py:293-319` — fields: `domain_id`, `domain_name`, `knowledge_mastery` (float), `application_mastery` (float), `confidence` (float), `evidence_count` (int), `subdomains` (dict), `target_mastery` (float, default 0.70), `status` (str, default `"learning"`).

- Status thresholds: `deepsleep_loop/mastery_store.py:32-44` — `status_for(knowledge_mastery)` maps to `"learning"` / `"operational"` / `"mastered"`.

- **Legacy file:** `guardian/state/knowledge_mastery.json` is **read but NOT written** by the live loop.  
  `docs/architecture/MEMORY_SYSTEM_MAP_AND_DIFF.md:182-227` — orphan warning: "no live writer updates `knowledge_mastery.json`; readers still read it but data is stale."  
  `symphony_learning_state.py:502-503` — reads `GUARDIAN_STATE_ROOT / "knowledge_mastery.json"` as fallback.

**Selection rule:** lowest `knowledge_mastery` × highest importance.  
The `MasteryRecord` schema has no explicit `importance` field. Importance must be inferred (e.g. from `target_mastery` gap: `target_mastery - knowledge_mastery`, or from `evidence_count` inversion, or from trajectory alignment).  
The spec says "lowest mastery × highest importance" — the `target_mastery` gap is the closest existing proxy.

**Conclusion:** P3 store **EXISTS** (`deepsleep_loop/mastery_store.py`). P3 gap-scoring heuristic (importance proxy) is **NEW**.

---

### P4 — Learning Debt

**Source:** `symphony_learning_state` envelope's `learning_debt_flags`.

- Composer: `pressure_loop/symphony_learning_state.py:105-208` — `build_episode_learning_state(...)`.
- Flags: `learning_debt_flags.empty_substrate`, `.stale_mastery`, `.low_confidence_recall`  
  `symphony_learning_state.py:97-102`.
- Derivation: `_derive_debt_flags()`  
  `symphony_learning_state.py:639-690`.
- The envelope rides on `episode.learning_state`  
  `pressure_loop/episode.py:229` (envelope built at episode open time).

**Mapping debt to domain:**  
The spec asks: "determine which domain generated the debt."  
The current envelope does NOT carry a domain attribution for each flag. It only carries:
- `mastery.top_weak[]` — the weakest domains (up to 3)  
  `symphony_learning_state.py:551-565`.
- `recall_confidence.most_recent_status` / `avg_confidence` — no domain attached.
- `strength_index` — no domain attached.

**Conclusion:** P4 flags **EXIST**, but P4 domain-mapping logic (which weak domain caused `low_confidence_recall` / `stale_mastery`) is **NEW**. The engine must read `learning_state.mastery.top_weak` and pick the lowest-mastery domain as the debt source.

---

### P5 — Trajectory Continuation

**Source:** `trajectory.target_domain`

- Reader: `pressure_loop/temporal_store.py:129-176` — `read_trajectory()`.  
   Returns latest trajectory payload from lobe 9 subdomain 2, or `{}`.
- Schema: `OmegaTrajectory`  
  `schema/omega_trajectory.py` (implied by `_validated_trajectory_dict`).
- Current trajectory shape includes: `target_domain`, `target_capability`, `confidence`, `rationale`, `evidence`, `continuation_recommended`, `next_learning_pressure`, `next_inquiry_pressure`  
  `omega_loop/_omega_state.py:262-270` (degraded trajectory) + `_prompt.py:117` (prompt schema).

**Conclusion:** P5 **EXISTS** — `read_trajectory()` is callable from anywhere. No new threading.

---

### P6 — Pressure-Dominant Topic

**Source:** `dominant_contributor.metadata["domain"]` / `metadata["topic"]`

- Already read in Global A: `global_loop/extract/run.py:39` — `domain = dom.metadata.get("domain") or dom.metadata.get("topic")`.
- Also available on `EpisodeIgnition.dominant_contributor` which is passed through to Meta A intake via `mi.pressure["contributors"]`.
- `meta_loop/intake/run.py:34-40` — `_contributors_metadata(mi)` merges all contributor metadata into `state.meta`.

**Conclusion:** P6 **EXISTS** — `dominant_contributor.metadata` is already in `state.meta`.

---

## 3. PLUG-IN POINT — Meta A LEARN Branch

**Current code:** `meta_loop/tool/run.py:51-56`

```python
elif intent == "LEARN":
    topic = target_str or text or "learning material"
    state.tool = _learn_default_tool()
    state.tool_reason = "Acquire learning material for the target topic"
    state.execution_inputs = {"query": f"{topic} learning material"}
    state.execution_intent = "Acquire knowledge inputs for learning"
```

**What MetaAState exposes that the engine can read:**

| Field | Source | Line |
|-------|--------|------|
| `state.text` | `_dominant_text(mi)` — action_goal.target + contributor metadata messages | `meta_loop/action/run.py:65` |
| `state.target_str` | action_goal.target (stripped prefix for learn) | `meta_loop/action/run.py:67-71` |
| `state.meta` | `_contributors_metadata(mi)` — merged contributor metadata | `meta_loop/action/run.py:72` |
| `state.intent` | action_goal.intent | `meta_loop/action/run.py:62` |
| `state.mi` | The raw `MetaAInput` — carries `pressure`, `trajectory_snapshot`, `action_goal` | `meta_loop/action/run.py:84` |

**Can MetaAState reach mastery / open_questions / trajectory directly?**

- **Mastery:** No direct field. Must call `deepsleep_loop.mastery_store.load_mastery()` or read `state.mi.pressure` → `episode.learning_state.mastery`. The latter is not currently threaded into `MetaAInput`.  
  **RECOMMENDATION:** The engine should call `load_mastery()` directly (it is a read-only, failure-soft call).
- **Open questions:** No direct field. Must call `pressure_loop.open_questions.list_open()`.
- **Trajectory:** No direct field. Must call `pressure_loop.temporal_store.read_trajectory()`.

**Threading verdict:** No changes to `MetaAInput` or `MetaAState` are required. The engine functions can import and call the existing read APIs lazily (consistent with failure-soft discipline).

**Plug-in architecture:**

```text
meta_loop/tool/run.py LEARN branch (lines 51-56)
    ↓
from cognition.topic_resolution.resolve_topic import resolve_learning_topic
    ↓
resolved = resolve_learning_topic(state)   # state = MetaAState
    ↓
if resolved.topic == NO_TOPIC_AVAILABLE:
    # Cat-2 observe-and-skip: set state.execution_intent = "SKIP_NO_TOPIC"
    # do NOT set tool / execution_inputs
else:
    state.tool = _learn_default_tool()
    state.tool_reason = f"Acquire knowledge on topic: {resolved.topic}"
    state.execution_inputs = {
        "query": resolved.topic,          # NO "learning material" suffix
        "domain": resolved.domain,
        "source": resolved.source,
    }
    state.execution_intent = "Acquire knowledge inputs for learning"
```

**NEW behavior for `execution_inputs`:** the `"query"` field drops `" learning material"` and passes the resolved topic directly. The `"domain"` and `"source"` fields are added for downstream traceability.

---

## 4. TOPIC → DOMAIN NORMALIZATION

**Existing slug helper:** `knowledge_extractor/derive/run.py:45-50`

```python
def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower())
    return slug.strip("-") or "general"
```

Also `deepsleep_loop/mastery_store.py:26-29` — `normalize_domain_id(target)`:
```python
def normalize_domain_id(target: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(target or "").strip().lower())
    return cleaned.replace(" ", "_")
```

**Conclusion:** Domain normalization **EXISTS**. The engine can reuse `_slug()` or `normalize_domain_id()`. The spec says "knowledge_extractor domain slug" — `_slug()` is the canonical choice because it is what `knowledge_extractor/derive/run.py:53-75` uses for `_domain_from_normalized()`.

---

## 5. MASTERY UPDATE-ON-ACQUISITION

**Current site:** `deepsleep_loop/mastery/run.py:116-161` — `_apply_mastery_for_episode()`.

- Called from `update_mastery()`  
  `deepsleep_loop/mastery/run.py:164-212`.
- Triggered in DeepSleep consolidation phase (post-window).
- Intent filter: `if intent == "LEARN":` → `knowledge += knowledge_inc`  
  `deepsleep_loop/mastery/run.py:140-141`.
- Domain name: `domain_name=target` where `target` comes from `_episode_target()` or fallback `action_goal.target`  
  `deepsleep_loop/mastery/run.py:199-205`.

**Problem today:** `target` is whatever string was in the episode outcome — often the raw `target_str` from Global A extraction. If the Topic Resolution Engine resolves to a better topic, the **mastery update must use the resolved topic**, not the raw fallback.

**Required change:** The resolved topic (and its normalized domain) must propagate from Meta A → Auto → outcome → episode body so that DeepSleep mastery update reads it.  
The simplest propagation path:
1. `meta_loop/tool/run.py` writes `execution_inputs["domain"]` (resolved domain).
2. `auto_loop/execute/run.py` passes it through to the tool call / knowledge extraction.
3. The episode outcome records the resolved topic/domain.
4. `deepsleep_loop/mastery/run.py:_episode_target()` reads the resolved domain from the outcome.

**Conclusion:** Mastery update site **EXISTS**, but it needs to read the resolved topic from the episode outcome. This implies a small schema addition to the outcome (or reusing an existing field like `goal.target`).

---

## 6. REFLECTION INTEGRATION

**Current Omega intake:** `omega_loop/intake/run.py:10-30` — `build_omega_input_for_window(window)`.

- Builds `OmegaInput` with: `window_id`, `trajectory_snapshot`, `episode_window` (enriched bodies), `trajectory_diff`.
- `episode_window` entries are enriched episode bodies from `pressure_loop/episode_journal.py:144-178` (`enriched_episode_from_episode`).

**Current enriched episode body shape (from `episode_journal.py`):**
- `intent`, `action`, `outcome`, `pressure`, `execution`, `brain_turns`, `open_query_state`, `cascade_status`.
- **No fields for:** `resolved_topic`, `domain`, `source`, `mastery_changes`, `open_questions_closed`, `open_questions_created`.

**Required for reflection (spec §REFLECTION INTEGRATION):**

```json
{
  "topic_history": [...],
  "mastery_changes": [...],
  "open_questions_closed": [...],
  "open_questions_created": [...]
}
```

**Where to inject:**

1. **topic_history** — append `{"topic": ..., "domain": ..., "source": ..., "episode_id": ...}` to each enriched body at episode close time.  
   Site: `pressure_loop/episode_journal.py:144-178` (`enriched_episode_from_episode`) or the caller that builds the body.

2. **mastery_changes** — DeepSleep `update_mastery()` returns `mastery_effects_from_records()`  
   `deepsleep_loop/mastery/run.py:89-113`. These deltas should be attached to the episode body (or the window) before Omega intake.

3. **open_questions_closed / open_questions_created** — `pressure_loop/open_questions.py` has `mark_resolved()` (closed) and `record_open_question()` (created). The counts/events should be aggregated per episode and added to the enriched body.

**Conclusion:** Reflection integration requires **NEW fields** in the enriched episode body + window input. The `omega_loop/intake/run.py` and `omega_loop/_prompt.py` must be updated to include these fields in the prompt context. The `OmegaReflection` dataclass  
`omega_loop/_omega_state.py:50-75` does not need schema changes (it is model-output shaped), but the **prompt** (`_prompt.py:59-69`) should tell the model about the new fields so it can reason over them.

---

## 7. NO_TOPIC_AVAILABLE ABORT

**Spec requirement:** Clean LEARN abort — Cat-2 observe-and-skip per fail-fast taxonomy, NOT a crash.

**Current LEARN branch:** `meta_loop/tool/run.py:51-56` — always sets a tool and execution_inputs. There is no abort path.

**Abort implementation:**

```python
elif intent == "LEARN":
    resolved = resolve_learning_topic(state)
    if resolved is None or resolved.topic == NO_TOPIC_AVAILABLE:
        state.execution_intent = "SKIP_NO_TOPIC"
        state.tool_reason = "NO_TOPIC_AVAILABLE: aborting LEARN (no resolvable target)"
        # Do NOT set state.tool or state.execution_inputs
        # Meta A pack phase will emit a skip pack; the episode will close with
        # outcome status = "skipped_no_topic" (or similar)
        return state
```

**Downstream handling:** The cognition chain (`cognition_chain.py`) must recognize the skip intent and short-circuit Auto/Omega for this episode. The exact skip semantics depend on the pack phase.  
`meta_loop/pack/run.py` (not read in detail) presumably packs the `MetaAState` into an `ExecutionActionPack`. If `tool` is None and `execution_intent` starts with `SKIP_`, the pack should set `action = "IDLE"` or similar.

**Conclusion:** NO_TOPIC_AVAILABLE abort logic is **NEW**. It requires:
- `resolve_learning_topic()` returning a sentinel.
- `meta_loop/tool/run.py` branching on the sentinel.
- `meta_loop/pack/run.py` (or cognition chain) honoring the skip.

---

## 8. POLLUTION REMOVAL SITES

### 8.1 `meta_loop/tool/run.py`

| Line | Current | Replacement |
|------|---------|-------------|
| 52 | `topic = target_str or text or "learning material"` | `resolved = resolve_learning_topic(state)` |
| 54 | `"Acquire learning material for the target topic"` | `"Acquire knowledge on topic: {resolved.topic}"` (or skip reason) |
| 55 | `"query": f"{topic} learning material"` | `"query": resolved.topic` (or omit if skipping) |

### 8.2 Tests hardcoding `"X learning material"`

| File | Line | Hardcoded string | Action |
|------|------|-------------------|--------|
| `tests/core/test_terminal_cognition_s2.py` | 105, 111 | `"transformers learning material"` | Update expected `execution_inputs.query` to `"transformers"` (or resolved topic) |
| `tests/core/test_auto_execution.py` | 118, 132 | `"Python learning material"` | Update expected query to `"Python"` |
| `tests/core/test_failure_log_batch1.py` | 218 | `"Python learning material"` | Update `_exec_pack` base query to `"Python"` |
| `tests/core/test_failure_log_batch2.py` | 181 | `"Python learning material"` | Update `_exec_pack` base query to `"Python"` |

**Note:** These tests should instead assert on the resolved topic behavior (or mock `resolve_learning_topic`). If the engine is imported in tests, they can inject a fake resolver.

---

## 9. MEMORY TOOL INTEGRATION (Future / P2+)

**Spec requirement:** Memory tools must receive `{resolved_topic, domain, source}`.

**Current MRIL recall path:** `auto_loop/mril_tools/remember.py` — deterministic `subdomain_for(text, lobe)` routing  
`auto_loop/mril_tools/subdomain_helpers.py:72-85`.

**Impact:** If `resolved_topic` and `domain` are passed into Auto execution (via `execution_inputs`), the MRIL tool router can use `resolved_topic` as the `query` for `subdomain_for()`, giving more precise recall than the raw operator message.  
This is a **P2+** integration — the map notes it but does not require P1 changes.

---

## 10. PHASE BREAKDOWN

### P1 (MVP — engine + wiring + tests)

| # | Task | Status | Files |
|---|------|--------|-------|
| 1 | Create `cognition/topic_resolution/__init__.py` | NEW | — |
| 2 | Create `cognition/topic_resolution/resolve_topic.py` with `ResolvedTopic` + `resolve_learning_topic(state)` | NEW | — |
| 3 | Implement P1 (read `state.target_str` / `state.text`) | EXISTS data, NEW code | `resolve_topic.py` |
| 4 | Implement P2 (call `list_open()`, score `confidence × age × recurrence`) | EXISTS data, NEW scoring | `resolve_topic.py` + maybe `open_questions.py` if adding index |
| 5 | Implement P3 (call `load_mastery()`, gap-scoring) | EXISTS data, NEW scoring | `resolve_topic.py` |
| 6 | Implement P4 (read `episode.learning_state`, map top_weak → domain) | EXISTS data, NEW mapping | `resolve_topic.py` |
| 7 | Implement P5 (call `read_trajectory()`) | EXISTS | `resolve_topic.py` |
| 8 | Implement P6 (read `state.meta.get("domain")` / `state.meta.get("topic")`) | EXISTS data | `resolve_topic.py` |
| 9 | Plug engine into `meta_loop/tool/run.py:52-56` | EXISTS file, NEW branch | `meta_loop/tool/run.py` |
| 10 | Add NO_TOPIC_AVAILABLE abort path | NEW | `meta_loop/tool/run.py` |
| 11 | Remove `"learning material"` pollution | MODIFY | `meta_loop/tool/run.py` |
| 12 | Update 4 tests (see §8.2) | MODIFY | `test_terminal_cognition_s2.py`, `test_auto_execution.py`, `test_failure_log_batch1.py`, `test_failure_log_batch2.py` |
| 13 | Write 3 acceptance tests per spec | NEW | `tests/cognition/test_topic_resolution.py` (suggested path) |

### P2+ (mastery update, reflection, memory tool, trajectory preferential resolution)

| # | Task | Status | Files |
|---|------|--------|-------|
| 14 | Propagate resolved topic through episode outcome → DeepSleep mastery | EXISTS site, NEW field path | `auto_loop/execute/run.py`, `deepsleep_loop/mastery/run.py` |
| 15 | Add `topic_history`, `mastery_changes`, `open_questions_closed/created` to enriched episode body | EXISTS body builder, NEW fields | `pressure_loop/episode_journal.py` |
| 16 | Thread new fields into `OmegaInput` and prompt | EXISTS intake, NEW fields | `omega_loop/intake/run.py`, `omega_loop/_prompt.py` |
| 17 | Memory tool receives `resolved_topic` / `domain` / `source` | EXISTS router, NEW param | `auto_loop/mril_tools/router.py` or `execute/run.py` |
| 18 | Trajectory preferential resolution (P5 override logic) | NEW logic | `resolve_topic.py` |

---

## 11. ACCEPTANCE TESTS (from spec)

Test 1 — Operator Query:
```python
state = make_meta_a_state(target_str="How do transformers work?")
resolved = resolve_learning_topic(state)
assert resolved.topic == "transformers"
assert resolved.source == "operator_query"
```

Test 2 — Open Question:
```python
# No operator query; inject one open question in jsonl
state = make_meta_a_state(target_str="", text="")
resolved = resolve_learning_topic(state)
assert resolved.topic == "retrieval drift"   # or whatever question text resolves to
assert resolved.source == "open_question"
```

Test 3 — Mastery Gap:
```python
# No operator query, no open questions
# Mock mastery store: attention=0.14, transformers=0.33
state = make_meta_a_state(target_str="", text="")
resolved = resolve_learning_topic(state)
assert resolved.topic == "attention"
assert resolved.source == "mastery_gap"
```

Test 4 — NO_TOPIC_AVAILABLE:
```python
# All six sources empty / return nothing
state = make_meta_a_state(target_str="", text="")
resolved = resolve_learning_topic(state)
assert resolved.topic == NO_TOPIC_AVAILABLE
```

---

## 12. EXISTENCE SUMMARY

| System | Status | Key File:Line |
|--------|--------|---------------|
| Operator query text → MetaAState.target_str | EXISTS | `meta_loop/action/run.py:67-71` |
| Open questions corpus + read API | EXISTS | `pressure_loop/open_questions.py:601-608` |
| Open questions scoring (confidence×age×recurrence) | **NEW** | — |
| Mastery store (DeepSleep) | EXISTS | `deepsleep_loop/mastery_store.py:47-71` |
| Mastery gap-scoring (importance proxy) | **NEW** | — |
| Learning debt flags | EXISTS | `pressure_loop/symphony_learning_state.py:97-102` |
| Learning debt → domain mapping | **NEW** | — |
| Trajectory reader | EXISTS | `pressure_loop/temporal_store.py:129-176` |
| Dominant contributor metadata | EXISTS | `global_loop/extract/run.py:39`, `meta_loop/intake/run.py:34-40` |
| LEARN branch pollution | EXISTS (to remove) | `meta_loop/tool/run.py:52-56` |
| Mastery update site | EXISTS | `deepsleep_loop/mastery/run.py:116-161` |
| Enriched episode body builder | EXISTS | `pressure_loop/episode_journal.py:144-178` |
| Omega intake / prompt | EXISTS | `omega_loop/intake/run.py`, `omega_loop/_prompt.py` |
| Domain slug helper | EXISTS | `knowledge_extractor/derive/run.py:45-50` |
| NO_TOPIC_AVAILABLE abort | **NEW** | — |
| Topic Resolution Engine | **NEW** | `cognition/topic_resolution/resolve_topic.py` |

---

*Mapped by Devin. Ready for Cursor TDD implementation per slice discipline.*
