# Topic Resolution Phase 3c — Developmental Stability Cascade FEASIBILITY MAP

**Work order:** `#f0c322bb` (agent bus)  
**Mapper:** Devin  
**Date:** 2026-06-08  
**AS-BUILT:** 2026-06-10 — Phase 3c shipped as **3c.1 + 3c.2** (see [AS-BUILT summary](#as-built-summary-2026-06-10) at end).  
**Scope:** Original feasibility map (READ-ONLY archaeology). Live authority: `cognition/topic_resolution/README.md`, `TOPIC_RESOLUTION_3C2_SPEC.md`.

---

## Design to ground

The autonomous cascade is **re-ordered for stability**:

| Priority | Source | Gate |
|----------|--------|------|
| P1 | Operator Query | None (always wins when present) |
| P2 | CRITICAL Open Question | Only when **critical** |
| P3 | SEVERE Mastery Deficit | Only when **severe** |
| P4 | Current Trajectory | **Hard default** — always wins unless P2 or P3 trigger |
| P5 | Learning Debt / Pressure | Fallbacks below trajectory |

Old cascade order: `operator_query → open_question → mastery_gap → learning_debt → trajectory → pressure_meta`  
New cascade order: `operator_query → critical_open_question → severe_mastery_gap → trajectory → learning_debt → pressure_meta`

---

## (1) CASCADE RESTRUCTURE — `cognition/topic_resolution/resolve_topic.py`

### Current state (EXISTS)

The six resolvers are declared in `resolve_learning_topic` at **lines 269-276**:

```python
resolvers = (
    _resolve_operator_query,      # line 270 — P1 operator
    _resolve_open_question,       # line 271 — P2 open questions
    _resolve_mastery_gap,         # line 272 — P3 mastery
    _resolve_learning_debt,       # line 273 — P4 learning debt
    _resolve_trajectory,        # line 274 — P5 trajectory
    _resolve_pressure_meta,       # line 275 — P6 pressure
)
```

Each resolver is a standalone function (lines 132-264):
- `_resolve_operator_query(state)` — **lines 132-140**
- `_resolve_open_question(_state=None)` — **lines 143-177**
- `_resolve_mastery_gap(_state=None)` — **lines 180-200**
- `_resolve_learning_debt(state)` — **lines 222-242**
- `_resolve_trajectory(_state=None)` — **lines 245-255**
- `_resolve_pressure_meta(state)` — **lines 258-264**

The `resolve_learning_topic` loop (lines 277-284) iterates the tuple and returns the first non-None, concrete topic.

### What is NEW

1. **Re-order the resolver tuple** — move `_resolve_trajectory` to position 4 (after mastery, before learning_debt/pressure). Move `_resolve_learning_debt` and `_resolve_pressure_meta` to positions 5-6.

2. **Gating functions for P2 and P3**:
   - `_is_critical_open_question(question, trajectory, window_history)` — NEW
   - `_is_severe_mastery_gap(record)` — NEW

3. **"Hard default" semantics for trajectory** — trajectory must be the *fallback* that always produces a topic when available. This means the loop logic changes: if P2 and P3 return None (ungated), we **skip** to trajectory rather than letting learning_debt/pressure_meta intercept. The simplest implementation: re-order the tuple so trajectory comes before the fallbacks.

### Touch-points (file:line)

| Change | Location | Note |
|--------|----------|------|
| Re-order resolver tuple | `resolve_topic.py:269-276` | Low-touch — tuple reorder only |
| Add `_is_critical_open_question()` | `resolve_topic.py` — NEW function ~line 178 | Gates `_resolve_open_question` return |
| Add `_is_severe_mastery_gap()` | `resolve_topic.py` — NEW function ~line 201 | Gates `_resolve_mastery_gap` return |
| Modify `_resolve_open_question` | `resolve_topic.py:143-177` | Wrap return with `if _is_critical(...)` |
| Modify `_resolve_mastery_gap` | `resolve_topic.py:180-200` | Wrap return with `if _is_severe(...)` |

---

## (2) SEVERE MASTERY DEFICIT = GAP

### Current state (EXISTS)

`MasteryRecord` fields in `deepsleep_loop/_deepsleep_state.py` **lines 296-305**:

```python
@dataclass(frozen=True)
class MasteryRecord:
    domain_id: str
    domain_name: str
    knowledge_mastery: float = 0.0
    application_mastery: float = 0.0
    confidence: float = 0.0
    evidence_count: int = 0
    subdomains: dict = field(default_factory=dict)
    target_mastery: float = 0.70
    status: str = "learning"
```

Current gap computation in `_resolve_mastery_gap` **lines 193-194**:

```python
gap = float(record.target_mastery) - float(record.knowledge_mastery)
```

This **only** checks `knowledge_mastery`. It does **not** consider `application_mastery`.

### What is NEW

1. **Gap computation change** — the operator's example (knowledge 0.80, application 0.15 → application gap 0.65 is the bottleneck) implies the gap should be the **maximum gap across both dimensions**, or possibly **application gap takes precedence** when it is the larger bottleneck.

   Proposed computation:
   ```python
   knowledge_gap = float(record.target_mastery) - float(record.knowledge_mastery)
   application_gap = float(record.target_mastery) - float(record.application_mastery)
   gap = max(knowledge_gap, application_gap)
   ```

2. **Severity gate** — `gap >= 0.5` is the threshold for "SEVERE". This is NEW logic; no threshold constant exists today.

### Touch-points

| Change | Location | Note |
|--------|----------|------|
| Gap formula | `resolve_topic.py:193-194` | Change from `target - knowledge` to `max(target - knowledge, target - application)` |
| Severity constant | `resolve_topic.py` — NEW constant | e.g. `SEVERE_MASTERY_GAP_THRESHOLD = 0.5` |
| Gate check | `resolve_topic.py:198-199` | Only return topic when `best_gap >= threshold` |

---

## (3) TRAJECTORY DEFAULT — `pressure_loop/temporal_store.py`

### Current state (EXISTS)

`read_trajectory()` at **lines 129-176** reads the latest trajectory from lobe 9:

```python
def read_trajectory() -> dict[str, Any]:
    latest = _latest_record_by_closed_at(
        _load_subdomain_metas(2), record_type="trajectory"
    )
    ...
    return _validated_trajectory_dict(parsed)
```

The returned dict contains `target_domain` (validated by `OmegaTrajectory.from_dict`).

`_resolve_trajectory` at **lines 245-255** already consumes this:

```python
def _resolve_trajectory(_state=None) -> ResolvedTopic | None:
    trajectory = read_trajectory()
    topic = str(trajectory.get("target_domain") or "").strip()
    if _is_concrete_topic(topic):
        return _resolved(topic, "trajectory", confidence=0.5)
    return None
```

### What is NEW

Making trajectory the **hard default** is primarily a cascade-ordering change (see §1). The resolver itself is EXACT — no code change needed inside `_resolve_trajectory`. The only semantic change: trajectory must always win when available, unless P1/P2/P3 have already fired. This is achieved by placing it 4th in the new tuple, after the gated P2/P3 and before the fallbacks.

### Touch-points

| Change | Location | Note |
|--------|----------|------|
| Confidence bump (optional) | `resolve_topic.py:254` | Consider raising `confidence` from 0.5 to 0.7-0.9 to signal "strong default" |

---

## (4) CRITICAL OPEN QUESTION — 3 NEW signals

### 4a — persistence (windows survived)

#### Current state (EXISTS)

`OpenQuestion` in `pressure_loop/open_questions.py` **lines 85-121** carries:

```python
asked_cycle: str       # brain cycle_id from dispatch
asked_at: str          # ISO8601 UTC
```

There is **no** `window_id`, `windows_survived`, `window_survival_count`, or any window-stamp field on the dataclass.

`ReflectionWindow` in `pressure_loop/reflection_window.py` **lines 43-53** carries:

```python
window_id: str
started_at_episode: str
trajectory_snapshot: dict
episodes: list[str]
episode_bodies: list[dict]
status: str            # "collecting" | "locked"
```

Windows archive to `memory/600_journal/reflection_windows/` (**line 40**). Each archive file is `{window_id}.json` with the full window dict.

#### What is NEW

**Persistence = how many locked windows has this open question survived without being resolved?**

Two implementation paths:

**Path A — Derive from archive + current window (heavier, no schema change):**
- On each check, scan archived windows in `memory/600_journal/reflection_windows/`
- Count windows whose `started_at_episode` / `episodes` list includes episodes AFTER `question.asked_at`
- If `count >= 3`, persistence is critical

**Path B — Stamp at creation (lighter, schema change):**
- Add `asked_window_id: Optional[str]` to `OpenQuestion`
- `open_reflection_window()` (**line 427**) or `append_episode_to_window()` could expose current window_id
- At `record_open_question()` time, stamp the active `window_id`
- On window lock/archive, a post-reflection pass increments `windows_survived` for each still-open question

**Recommendation:** Path B is cleaner but requires schema change + migration. Path A is derivation-only but I/O heavy (directory scan on every topic resolution). Given the bus directive to map, Path B is the likely build path; the schema change is the heaviest part of this signal.

#### Touch-points

| Change | Location | Note |
|--------|----------|------|
| Schema addition | `pressure_loop/open_questions.py:86-121` | Add `asked_window_id`, `windows_survived` to `OpenQuestion` |
| Stamp on record | `pressure_loop/open_questions.py:207-261` | In `record_open_question`, read current window from `EpisodeStore` or runtime and stamp |
| Increment on archive | `pressure_loop/reflection_window.py:372-424` or `cognition_chain.py:177` | After `archive_and_reset_window`, increment `windows_survived` for open questions |
| Persistence check | `resolve_topic.py` — NEW | `_question_persistence(question) -> int` |

---

### 4b — trajectory_relevance (question text vs trajectory target_domain similarity)

#### Current state (EXISTS)

The semantic state engine in `memory/semantic_state_engine.py` provides:

- `embed(text: str, *, dim: Optional[int] = None) -> Optional[Vector]` — **lines 251-316**
- `cosine_similarity(a: Vector, b: Vector) -> Optional[float]` — **lines 198-217**

Both are failure-soft (return None when runtime unavailable).

#### What is NEW

A function that:
1. Embeds `question.question_text`
2. Embeds `trajectory["target_domain"]`
3. Returns `cosine_similarity(embed_q, embed_traj)` or `0.0` on degradation

This is a **thin wrapper** — no new infrastructure. The heaviest risk is embedding latency (two embed calls per open question evaluated). Given the windowed cadence (once per 10 episodes, not per tick), this is acceptable.

#### Touch-points

| Change | Location | Note |
|--------|----------|------|
| Relevance function | `resolve_topic.py` — NEW | `_question_trajectory_relevance(question, trajectory) -> float` |
| Import embed + cosine | `resolve_topic.py:1-16` | Add `from memory.semantic_state_engine import embed, cosine_similarity` |

---

### 4c — execution_blockage (is it blocking trajectory progress)

#### Current state (EXISTS)

`FailurePattern` in `deepsleep_loop/_deepsleep_state.py` **lines 240-256**:

```python
@dataclass(frozen=True)
class FailurePattern:
    kind: str           # "tool" | "planning" | "response"
    context: str        # target domain or failure_kind
    frequency: int = 0
    last_episode: str = ""
```

`failure_store.py` **lines 29-50** — `load_failures()` returns `dict[str, FailurePattern]` keyed by `kind::context`.

`deepsleep_loop/failure/run.py` **lines 139-195** — `consolidate_failures(ds_input)` aggregates window episode failures into patterns. `context` is derived from `_entry_target(entry)` or `fallback_target` (the action goal target). **Lines 86-88** show context = the target string when available, else the failure_kind.

#### What is NEW

**Blockage scoring** requires linking a question's topic/domain to the failure store. The gap:

1. **No domain normalization on failure context** — failure `context` is the raw target string (e.g., "how do attention mechanisms work"), not a normalized domain_id. Matching requires fuzzy/semantic matching, not exact key lookup.

2. **No question-to-failure index** — there is no pre-built mapping from `OpenQuestion.question_id` to `FailurePattern`.

3. **Scoring formula** — NEW. A proposed heuristic:
   ```python
   def _blockage_score(question: OpenQuestion, failures: dict[str, FailurePattern]) -> float:
       """Returns 0.0-1.0 blockage score."""
       q_topic = _normalize_topic(_extract_topic_from_text(question.question_text))
       q_domain = _slug(q_topic)
       score = 0.0
       for key, pattern in failures.items():
           # Exact domain match on context
           if _slug(pattern.context) == q_domain:
               score += min(pattern.frequency / 5.0, 0.5)  # cap at 0.5
           # Kind-level broad match (e.g. "tool" failures on any target)
           elif pattern.kind == "tool" and q_domain in _normalize_topic(pattern.context):
               score += min(pattern.frequency / 10.0, 0.3)
       return min(score, 1.0)
   ```

4. **Recency factor** — `last_episode` is a string (episode_id), not a timestamp. Deriving "how recent" requires cross-referencing episode metadata.

#### Touch-points

| Change | Location | Note |
|--------|----------|------|
| Blockage function | `resolve_topic.py` — NEW | `_question_execution_blockage(question, failures) -> float` |
| Import failure store | `resolve_topic.py:1-16` | Add `from deepsleep_loop.failure_store import load_failures` |
| Normalization helper | `resolve_topic.py:58-60` | Re-use `_normalize_topic` + `_slug` for fuzzy matching |

---

## HEAVIEST SIGNAL — `execution_blockage`

| Signal | Build weight | Rationale |
|--------|-----------|-----------|
| **persistence** | Medium | Requires schema change (`OpenQuestion` + migration) OR archive directory scan. Schema change is cleaner but touches the append-only JSONL store. |
| **trajectory_relevance** | Low | Thin wrapper over existing `embed()` + `cosine_similarity()`. ~10 lines. |
| **execution_blockage** | **High** | Requires: (a) fuzzy domain matching between question text and failure `context`, (b) scoring heuristic definition, (c) potential schema additions to `FailurePattern` (e.g. `domain_id` for exact lookup), (d) recency computation from `last_episode` string. |

`execution_blockage` is the heaviest because the failure store's `context` field is **not normalized** — it is the raw target string from the episode's action goal. Linking a question's topic to failures requires either:
1. Semantic similarity (embed both, cosine match) — adds embedding cost
2. `_slug` normalization on both sides — cheap but brittle (context may contain free-form text)
3. Schema change to `FailurePattern` to store `domain_id` — requires DeepSleep failure consolidation to compute `_slug(context)` at write time

Option 3 is the most robust but spans two slices (DeepSleep failure consolidation + topic resolution). This is why blockage deserves its own sub-phase or deferred integration.

---

## SUB-PHASE RECOMMENDATION

**Yes — split 3c into two sub-phases.**

### 3c.1 — "Trajectory Default + Severe Gap" (ship first)

**Scope:**
- Re-order cascade tuple (`resolve_topic.py:269-276`)
- Change gap formula to `max(knowledge_gap, application_gap)` (`resolve_topic.py:193-194`)
- Add `SEVERE_MASTERY_GAP_THRESHOLD = 0.5` constant
- Gate `_resolve_mastery_gap` with `best_gap >= threshold`
- Bump trajectory confidence to signal hard-default strength

**Risk:** Low. All mechanical changes to deterministic resolvers. No new I/O.

**Test surface:** Unit tests for `resolve_learning_topic` with mocked mastery records.

### 3c.2 — "Critical Open Question Signals" (ship second)

**Scope:**
- `persistence`: Schema change to `OpenQuestion` + stamp/increment logic
- `trajectory_relevance`: Wrapper function over existing embed/cosine
- `execution_blockage`: Domain-to-failure linkage + scoring heuristic
- Gate `_resolve_open_question` with criticality formula:
  ```
  critical = (persistence >= 3 windows AND relevance >= 0.6) OR (blockage >= 0.8)
  ```

**Risk:** Medium-High. Crosses three subsystems (open_questions, memory/embed, deepsleep/failure). Requires integration test.

**Test surface:**
- Open question schema round-trip (JSONL read/write)
- Embedding preflight failure-soft (runtime unavailable)
- Failure matching with mocked `load_failures()`
- End-to-end: question → topic resolution → cascade order

---

## SUMMARY TABLE — EXISTS vs NEW

| Piece | File:Line (EXISTS) | What is NEW | Build weight |
|-------|--------------------|-------------|--------------|
| Cascade re-order | `resolve_topic.py:269-276` | Tuple re-order + gate wrappers | Low |
| Severe gap computation | `resolve_topic.py:193-194` | `max(knowledge_gap, application_gap)` + threshold | Low |
| Trajectory default | `resolve_topic.py:245-255` | Position in tuple (no internal change) | Low |
| persistence signal | `open_questions.py:86-121` | `asked_window_id`, `windows_survived` fields + stamp logic | Medium |
| trajectory_relevance | `semantic_state_engine.py:251-316` | Thin wrapper `_question_trajectory_relevance()` | Low |
| execution_blockage | `failure/run.py:139-195`, `failure_store.py:29-50` | Domain-to-failure linkage + scoring heuristic | **High** |

---

## Open questions for operator/Claude

1. **Gap formula:** Should the mastery gap be `max(knowledge_gap, application_gap)` or should `application_gap` take precedence when larger? The operator example suggests application is the bottleneck, but the spec says "GAP, not absolute."

2. **persistence path:** Schema change (stamp `window_id` on `OpenQuestion`) or derivation from archive scan? Schema change is cleaner but requires JSONL migration.

3. **execution_blockage matching:** Should DeepSleep failure consolidation normalize `context` to `domain_id` at write time (Option 3 — spans slices), or should topic resolution do fuzzy matching at read time (Option 1/2 — localized but brittle)?

4. **Criticality OR threshold:** The design says `(persistence >= 3 AND relevance >= 0.6) OR (blockage >= 0.8)`. Should the thresholds be policy-driven (loaded from `symphony.yaml`) or hard-coded constants?

---

## AS-BUILT summary (2026-06-10)

Phase 3c is **fully integrated**. Shipped in two sub-phases per the recommendation below.

| Feasibility item | Shipped? | Where |
|------------------|----------|-------|
| Cascade re-order (trajectory before fallbacks) | ✅ 3c.1 | `resolve_topic.py` resolver tuple |
| Severe mastery gap (`max` formula + threshold) | ✅ 3c.1 | `_resolve_mastery_gap`, policy overlay |
| Trajectory hard default (confidence 0.85) | ✅ 3c.1 | `_resolve_trajectory` |
| persistence (`asked_window_id`, `windows_survived`) | ✅ 3c.2b | `pressure_loop/open_questions.py` |
| trajectory_relevance (embed + cosine) | ✅ 3c.2a | `_question_trajectory_relevance` |
| execution_blockage (`domain_id` + scorer) | ✅ 3c.2b | `FailurePattern.domain_id`, `_question_execution_blockage` |
| Critical open question gate | ✅ 3c.2a | **THE FOLD** in `_resolve_trajectory` (not separate P2 cascade entry) |
| Policy-driven thresholds | ✅ 3c.2 | `core/policy/symphony.yaml` + `_critical_open_question_thresholds()` |

**Design evolution (vs this map):**

- Critical open questions fold **into** `_resolve_trajectory` (`"{target_domain}: {question}"`, confidence 0.90) instead of a standalone P2 resolver.
- Criticality formula: **relevance ≥ 0.6 is a hard gate**; then `persistence ≥ 3 OR blockage ≥ 0.8` (blockage alone cannot steer off-path questions).
- `FailurePattern.domain_id` uses `canonical_domain_key(context)` at write time (not raw `_slug`).
- Persistence uses **Path B** (schema stamp + increment on archive); Path A (archive scan) was not built.

**Tests:** `tests/cognition/test_topic_resolution_3c1.py` (5), `test_topic_resolution_3c2.py` (27), `tests/pressure_loop/test_open_questions_3c2b.py` (6) — all passing.

**Still deferred:** see `TOPIC_RESOLUTION_3C2_SPEC.md` § Deferred / out of scope (recency factor, HUD fold notification, soft bias-layer cascade).
