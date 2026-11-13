# Topic Resolution Phase 3c.2 — Critical Open Question as Trajectory Stabilizer

> **Historical work-order snapshot.** It is retained for provenance, not as
> current architecture authority. See `AGENTS.md` and
> `docs/BASELINE_2026-08-03.md`.

**Work order:** `#699e90d2` (agent bus claude → devin)  
**Date:** 2026-06-09  
**AS-BUILT:** 2026-06-10 — **DONE** and verified (`test_topic_resolution_3c2.py`, `test_open_questions_3c2b.py`).  
**Authority:** Supersedes `TOPIC_RESOLUTION_3C_FEASIBILITY_MAP.md` §4 and `TOPIC_RESOLUTION_3C1_BUILD.md` 3c.2 cascade-reorder plan where divergent. Live operator doc: `cognition/topic_resolution/README.md`.

---

## Design summary

`open_question` is a **trajectory STABILIZER**, not a competing cascade entry. A genuine question born of the system's own decided path does not destabilize the path — it **maturing** the path. The fold keeps the 3c.1 cascade order intact while letting trajectory resolution "absorb" a critical, path-relevant question as its concrete focus.

**Cascade order (unchanged from 3c.1):**

| Priority | Resolver | Gate |
|----------|----------|------|
| P1 | `_resolve_operator_query` | None |
| P2 | `_resolve_mastery_gap` | `best_gap >= severe_threshold` |
| P3 | `_resolve_trajectory` | **NEW — THE FOLD** (see §4) |
| P4 | `_resolve_open_question` | Ungated scoring (non-critical questions only) |
| P5 | `_resolve_learning_debt` | Debt flags present |
| P6 | `_resolve_pressure_meta` | Meta domain/topic present |

**Key insight:** `trajectory_relevance` is **both** the value criterion **and** the stability guarantee. A path-relevant question rising inside trajectory resolution cannot be a detour — it **is** the path maturing. An OFF-path question must NOT steer.

---

## Sub-phase recommendation

| Sub-phase | Scope | Risk | Why |
|-----------|-------|------|-----|
| **3c.2a** | `trajectory_relevance` + THE FOLD (§1, §4) + policy thresholds (§5) | Low | Pure resolve_topic.py changes; no cross-slice I/O |
| **3c.2b** | `persistence` schema + stamp/increment (§2) + `execution_blockage` pre-position + scoring (§3) | Medium-High | Crosses open_questions, reflection_window, deepsleep/failure; schema migrations |

**Operator gate:** 3c.2a can ship independently. 3c.2b requires the schema changes to land together.

---

## §1 — trajectory_relevance(question, trajectory) → float  [LOW]

### What EXISTS

`memory/semantic_state_engine.py:251-316` — `embed(text: str, *, dim: Optional[int] = None) -> Optional[Vector]`  
`memory/semantic_state_engine.py:198-217` — `cosine_similarity(a: Vector, b: Vector) -> Optional[float]`

Both are failure-soft (return `None` when runtime unavailable or inputs invalid).

### What is NEW

A thin wrapper inside `resolve_topic.py`:

```python
def _question_trajectory_relevance(
    question: OpenQuestion, trajectory: dict[str, Any]
) -> float:
    """Cosine similarity between question embedding and trajectory target_domain.
    Returns 0.0 on any degradation (no runtime, empty text, dim mismatch)."""
    from memory.semantic_state_engine import embed, cosine_similarity

    question_text = str(question.question_text or "").strip()
    target_domain = str(trajectory.get("target_domain") or "").strip()
    if not question_text or not target_domain:
        return 0.0
    q_vec = embed(question_text)
    t_vec = embed(target_domain)
    if q_vec is None or t_vec is None:
        return 0.0
    sim = cosine_similarity(q_vec, t_vec)
    return float(sim) if sim is not None else 0.0
```

### Touch-points

| Change | File:Line | Note |
|--------|-----------|------|
| `_question_trajectory_relevance()` | `cognition/topic_resolution/resolve_topic.py` — NEW function ~line 300 | ~10 lines, imports `embed`, `cosine_similarity` |
| Import block | `cognition/topic_resolution/resolve_topic.py:1-16` | Add `from memory.semantic_state_engine import embed, cosine_similarity` (or inline import for failure-soft) |

### RED-first test plan

1. `test_relevance_perfect_match` — same text for question and trajectory → similarity ≈ 1.0 (mock embed to identity vector).
2. `test_relevance_orthogonal` — mock embed returns orthogonal vectors → similarity ≈ 0.0.
3. `test_relevance_degraded_runtime` — `embed()` returns `None` → function returns `0.0` (no crash).
4. `test_relevance_empty_question` — empty `question_text` → returns `0.0`.
5. `test_relevance_empty_trajectory` — trajectory lacks `target_domain` → returns `0.0`.

---

## §2 — persistence(question) → int  [MEDIUM]

### What EXISTS

`OpenQuestion` dataclass `pressure_loop/open_questions.py:86-121`:

```python
asked_cycle: str       # brain cycle_id from dispatch
asked_at: str          # ISO8601 UTC
```

There is **no** `window_id`, `windows_survived`, or window-stamp field.

`ReflectionWindow` `pressure_loop/reflection_window.py:43-53` carries `window_id: str`.

`record_open_question()` at `pressure_loop/open_questions.py:207-261` appends a new OPEN record with no window awareness.

`cognition_chain.py:177` calls `store.archive_and_reset_window()` after Omega → DeepSleep complete.

### What is NEW

**Schema change:**

```python
@dataclass
class OpenQuestion:
    ...
    asked_window_id: Optional[str] = None   # window_id active at creation
    windows_survived: int = 0                # how many archive events survived
```

**Stamp at creation:** inside `record_open_question()` after `question_id` is minted and before `_append()`:

```python
from pressure_loop.reflection_window import load_reflection_window

active_window = load_reflection_window()
asked_window_id = active_window.window_id if active_window else None

q = OpenQuestion(
    ...,
    asked_window_id=asked_window_id,
    windows_survived=0,
)
```

**Increment on archive:** new function in `pressure_loop/open_questions.py`:

```python
def increment_windows_survived_for_open_questions() -> None:
    """After a window archives, increment windows_survived for every
    still-OPEN question. Append-only — rewrites the latest state."""
    records = _load_all()
    latest = _latest_per_id(records)
    for q in latest.values():
        if q.status != STATUS_OPEN:
            continue
        incremented = _copy_question(q, windows_survived=int(q.windows_survived or 0) + 1)
        _append(incremented.to_dict())
```

**Hook location:** call `increment_windows_survived_for_open_questions()` from `cognition_chain.py` between the DeepSleep handoff and `store.archive_and_reset_window()`:

```python
# cognition_chain.py ~line 170-177 (current)
    run_deepsleep_for_window(...)
    increment_windows_survived_for_open_questions()   # NEW
    store.archive_and_reset_window(window, omega_outcome=outcome_dict)
```

### Touch-points

| Change | File:Line | Note |
|--------|-----------|------|
| Schema | `pressure_loop/open_questions.py:86-121` | Add `asked_window_id`, `windows_survived` |
| Stamp | `pressure_loop/open_questions.py:232-242` | Inside `record_open_question` before `_append` |
| Increment function | `pressure_loop/open_questions.py` — NEW ~line 262 | `increment_windows_survived_for_open_questions()` |
| Hook | `cognition_chain.py:170-177` | Insert call before `archive_and_reset_window` |
| Helper read | `pressure_loop/open_questions.py:166-186` | Re-use `_load_all()` + `_latest_per_id()` |

### RED-first test plan

1. `test_record_open_question_stamps_window_id` — mock `load_reflection_window` returning a window → record has `asked_window_id == window.window_id`.
2. `test_record_open_question_no_active_window` — mock returns `None` → `asked_window_id is None`.
3. `test_increment_survived_bumps_open_questions_only` — create one OPEN, one ANSWERED, one RESOLVED → only OPEN gets `windows_survived=1`.
4. `test_increment_survived_appends_only` — verify JSONL length grows by N (open count), not rewritten in-place.
5. `test_increment_survived_idempotent` — call twice → OPEN question has `windows_survived=2`.

---

## §3 — execution_blockage(question, failures) → float  [HIGH]

### What EXISTS

`FailurePattern` `deepsleep_loop/_deepsleep_state.py:240-256`:

```python
@dataclass(frozen=True)
class FailurePattern:
    kind: str
    context: str        # raw goal string or failure_kind
    frequency: int = 0
    last_episode: str = ""
```

`deepsleep_loop/failure/run.py:139-195` — `consolidate_failures()` aggregates window failures. At line 86 `context = _entry_target(entry, fallback=fallback_target) or str(failure_kind or "unknown")` — **raw text, no normalization**.

`deepsleep_loop/failure_store.py:29-50` — `load_failures()` returns `dict[str, FailurePattern]` keyed by `kind::context`.

### What is NEW

**Pre-position FIRST** (operator directive in work order):

Add `domain_id: str = ""` to `FailurePattern` so blockage can be an **exact key lookup**, not fuzzy.

```python
@dataclass(frozen=True)
class FailurePattern:
    kind: str
    context: str
    domain_id: str = ""      # NEW — _slug(context) at write time
    frequency: int = 0
    last_episode: str = ""
```

Update `FailurePattern.from_dict()` to read `domain_id` (back-compat: default `""`).

At `deepsleep_loop/failure/run.py:167-172`, compute `domain_id = _slug(context)`:

```python
from knowledge_extractor.derive.run import _slug

pattern = FailurePattern(
    kind=kind,
    context=context,
    domain_id=_slug(context),          # NEW
    frequency=frequency,
    last_episode=episode_id,
)
```

Same change for the fallback branch at `deepsleep_loop/failure/run.py:181-186`.

**Blockage scoring function** in `resolve_topic.py`:

```python
def _question_execution_blockage(
    question: OpenQuestion, failures: dict[str, FailurePattern]
) -> float:
    """0.0–1.0 blockage score from failure frequency on the question's domain.
    Exact domain_id match (robust because DeepSleep pre-normalized)."""
    q_domain = _slug(_extract_topic_from_text(question.question_text))
    score = 0.0
    for pattern in failures.values():
        if pattern.domain_id == q_domain:
            score += min(pattern.frequency / 5.0, 0.5)   # cap per-pattern at 0.5
    return min(score, 1.0)
```

> **Rationale:** `frequency / 5.0` means 5 failures in the domain = 0.5, 10 = 1.0 (capped). This is a heuristic tuned to the 10-episode window cadence. Policy override can tune the divisor.

### Touch-points

| Change | File:Line | Note |
|--------|-----------|------|
| Schema | `deepsleep_loop/_deepsleep_state.py:240-256` | Add `domain_id: str = ""` to `FailurePattern` |
| from_dict | `deepsleep_loop/_deepsleep_state.py:250-256` | Read `domain_id` with back-compat default |
| Import | `deepsleep_loop/failure/run.py:1-9` | Add `from knowledge_extractor.derive.run import _slug` |
| Write-time slug | `deepsleep_loop/failure/run.py:167-172` | Add `domain_id=_slug(context)` |
| Write-time slug (fallback) | `deepsleep_loop/failure/run.py:181-186` | Same |
| Blockage function | `cognition/topic_resolution/resolve_topic.py` — NEW ~line 315 | `_question_execution_blockage()` |
| Import failures | `cognition/topic_resolution/resolve_topic.py:1-16` | Add `from deepsleep_loop.failure_store import load_failures` |

### RED-first test plan

1. `test_blockage_exact_domain_match` — mock `load_failures` with `domain_id="attention_mechanisms"`, frequency=5 → blockage ≈ 1.0.
2. `test_blockage_no_match` — failures have unrelated `domain_id` → blockage = 0.0.
3. `test_blockage_caps_at_one` — frequency=20 → blockage = 1.0 (not 4.0).
4. `test_blockage_empty_failures` — `load_failures` returns `{}` → blockage = 0.0.
5. `test_blockage_degraded_failure_store` — `load_failures` raises → function returns 0.0 (failure-soft).
6. `test_consolidate_failures_writes_domain_id` — run `consolidate_failures` with a failed episode → resulting `FailurePattern.domain_id` equals `_slug(context)`.

---

## §4 — THE FOLD in `_resolve_trajectory`

### What EXISTS

`_resolve_trajectory` `cognition/topic_resolution/resolve_topic.py:268-278`:

```python
def _resolve_trajectory(_state=None) -> ResolvedTopic | None:
    try:
        trajectory = read_trajectory()
    except Exception:
        return None
    if not isinstance(trajectory, dict):
        return None
    topic = str(trajectory.get("target_domain") or "").strip()
    if _is_concrete_topic(topic):
        return _resolved(topic, "trajectory", confidence=0.85)
    return None
```

`_resolve_open_question` `cognition/topic_resolution/resolve_topic.py:145-179` still scores all open questions and returns the highest-scored one.

### What is NEW

Replace the body of `_resolve_trajectory` with the fold:

```python
def _resolve_trajectory(_state=None) -> ResolvedTopic | None:
    try:
        trajectory = read_trajectory()
    except Exception:
        return None
    if not isinstance(trajectory, dict):
        return None
    target_domain = str(trajectory.get("target_domain") or "").strip()
    if not _is_concrete_topic(target_domain):
        return None

    # --- THE FOLD: critical path-relevant open question steers the focus ---
    critical_question = _pick_critical_open_question(trajectory)
    if critical_question is not None:
        # Scope the question within the trajectory domain
        scoped = f"{target_domain}: {critical_question.question_text}"
        return _resolved(scoped, "trajectory", confidence=0.90)
    # --- end fold ---

    return _resolved(target_domain, "trajectory", confidence=0.85)
```

The helper `_pick_critical_open_question`:

```python
def _pick_critical_open_question(trajectory: dict[str, Any]) -> OpenQuestion | None:
    """Return the highest-scored OPEN question that is CRITICAL and
    path-relevant (trajectory_relevance >= threshold). None if no match."""
    try:
        questions = list_open()
    except Exception:
        return None
    if not questions:
        return None

    thresholds = _critical_open_question_thresholds()
    persistence_threshold = thresholds["persistence_threshold"]      # default 3
    relevance_threshold = thresholds["relevance_threshold"]            # default 0.6
    blockage_threshold = thresholds["blockage_threshold"]              # default 0.8

    try:
        failures = load_failures()
    except Exception:
        failures = {}

    candidates: list[tuple[float, OpenQuestion]] = []
    for q in questions:
        if q.status != STATUS_OPEN:
            continue
        persistence = int(q.windows_survived or 0)
        relevance = _question_trajectory_relevance(q, trajectory)
        blockage = _question_execution_blockage(q, failures)

        # Oscillation guard: off-path questions are NEVER critical
        if relevance < relevance_threshold:
            continue

        is_critical = (persistence >= persistence_threshold) or (blockage >= blockage_threshold)
        if not is_critical:
            continue

        # Score for ranking among multiple critical questions
        score = float(persistence) * relevance * (1.0 + blockage)
        candidates.append((score, q))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]
```

**Why confidence 0.90 for folded topics?** Slightly above the hard-default 0.85 to signal "this is the path maturing into a concrete focus."

### Touch-points

| Change | File:Line | Note |
|--------|-----------|------|
| `_resolve_trajectory` body | `cognition/topic_resolution/resolve_topic.py:268-278` | Replace with fold logic |
| `_pick_critical_open_question()` | `cognition/topic_resolution/resolve_topic.py` — NEW ~line 280 | Criticality gate + scoring |
| Threshold loader | `cognition/topic_resolution/resolve_topic.py` — NEW ~line 260 | `_critical_open_question_thresholds()` |
| Import `STATUS_OPEN` | `cognition/topic_resolution/resolve_topic.py:1-16` | Or reference `pressure_loop.open_questions.STATUS_OPEN` inline |
| Import `load_failures` | `cognition/topic_resolution/resolve_topic.py:1-16` | From `deepsleep_loop.failure_store` |

### RED-first test plan

1. `test_fold_returns_question_when_critical` — persistence=3, relevance=0.8, no blockage → trajectory resolver returns scoped topic containing question text.
2. `test_fold_returns_plain_trajectory_when_no_critical_question` — no open questions → returns plain `target_domain`, confidence=0.85.
3. `test_fold_skips_off_path_question` — persistence=5 but relevance=0.2 → returns plain trajectory (oscillation guard).
4. `test_fold_prefers_blockage_override` — persistence=1, relevance=0.9, blockage=0.85 → returns critical question (blockage OR).
5. `test_fold_ranks_multiple_critical_questions` — two critical questions, one with higher score → higher-scored one wins.
6. `test_fold_degraded_failure_store` — `load_failures` raises → blockage treated as 0.0, persistence-only gate still works.
7. `test_fold_degraded_embed` — `embed()` returns None → relevance=0.0, off-path, so question skipped.

---

## §5 — thresholds policy-driven (symphony.yaml)

### What EXISTS

`core/policy/symphony.yaml:1-74` has no `cognition:` section.  
`pressure_loop/policy.py:183-201` `load_policy()` merges YAML sources.  
`_severe_mastery_gap_threshold()` in `resolve_topic.py:182-194` already loads `cognition.topic_resolution.severe_mastery_gap_threshold` from policy with graceful fallback.

### What is NEW

Add to `core/policy/symphony.yaml`:

```yaml
cognition:
  topic_resolution:
    critical_open_question:
      persistence_threshold: 3
      relevance_threshold: 0.6
      blockage_threshold: 0.8
```

Add loader in `cognition/topic_resolution/resolve_topic.py` (mirrors existing `_severe_mastery_gap_threshold` pattern):

```python
def _critical_open_question_thresholds() -> dict[str, float]:
    defaults = {
        "persistence_threshold": 3.0,
        "relevance_threshold": 0.6,
        "blockage_threshold": 0.8,
    }
    try:
        policy = load_policy()
        cognition = policy.get("cognition") if isinstance(policy, dict) else None
        if isinstance(cognition, dict):
            tr = cognition.get("topic_resolution")
            if isinstance(tr, dict):
                cq = tr.get("critical_open_question")
                if isinstance(cq, dict):
                    for key in defaults:
                        raw = cq.get(key)
                        if raw is not None:
                            try:
                                defaults[key] = float(raw)
                            except (TypeError, ValueError):
                                pass
    except Exception:
        pass
    return defaults
```

### Touch-points

| Change | File:Line | Note |
|--------|-----------|------|
| Policy YAML | `core/policy/symphony.yaml:74` | Append `cognition:` block |
| Loader | `cognition/topic_resolution/resolve_topic.py` — NEW ~line 260 | `_critical_open_question_thresholds()` |

### RED-first test plan

1. `test_thresholds_default_when_policy_missing` — mock `load_policy` returns `{}` → defaults 3 / 0.6 / 0.8.
2. `test_thresholds_override_from_policy` — policy sets `persistence_threshold: 2` → loader returns 2.0.
3. `test_thresholds_invalid_policy_value` — policy has `relevance_threshold: "bad"` → falls back to 0.6.

---

## Oscillation guard (test invariant)

**The core stability promise:** a path-relevant critical question steers the trajectory; an off-path question does NOT.

Dedicated acceptance test:

```python
def test_oscillation_guard_off_path_question_never_steers(monkeypatch):
    """A highly persistent question about an unrelated domain must NOT
    fold into trajectory resolution."""
    # trajectory = "attention mechanisms"
    # open question = "why does the moon shine?" (persistence=5, relevance ~ 0.0)
    # Expected: resolves plain trajectory, NOT the moon question.
```

---

## EXISTS vs NEW summary table

| Piece | EXISTS (File:Line) | NEW | Build weight |
|-------|--------------------|-----|--------------|
| Cascade tuple order | `resolve_topic.py:292-299` | No change (3c.1 order kept) | — |
| `_resolve_trajectory` body | `resolve_topic.py:268-278` | Fold logic + `_pick_critical_open_question()` | Low |
| `trajectory_relevance` wrapper | `semantic_state_engine.py:251-316` | `_question_trajectory_relevance()` | Low |
| `embed` + `cosine_similarity` | `semantic_state_engine.py:251-316`, `:198-217` | Re-used as-is | — |
| `_score_open_question` | `resolve_topic.py:127-131` | Re-used as-is for non-critical ranking | — |
| OpenQuestion schema | `open_questions.py:86-121` | Add `asked_window_id`, `windows_survived` | Medium |
| Stamp window on record | `open_questions.py:207-261` | Read active window, stamp `asked_window_id` | Medium |
| Increment on archive | `cognition_chain.py:177` | Call `increment_windows_survived_for_open_questions()` before archive | Medium |
| `FailurePattern` schema | `_deepsleep_state.py:240-256` | Add `domain_id: str = ""` | Medium |
| Failure write-time slug | `failure/run.py:167-172`, `:181-186` | `domain_id=_slug(context)` at pattern creation | Medium |
| `execution_blockage` scorer | `failure_store.py:29-50` | `_question_execution_blockage()` exact lookup | Medium |
| `_severe_mastery_gap_threshold` | `resolve_topic.py:182-194` | Re-use pattern for `_critical_open_question_thresholds()` | Low |
| Policy YAML | `core/policy/symphony.yaml:1-74` | Append `cognition.topic_resolution.critical_open_question` block | Low |

---

## Deferred / out of scope

- **Recency factor in blockage:** `last_episode` is an episode_id string, not a timestamp. Adding time-decay requires episode metadata lookup; defer until operator asks.
- **Semantic matching fallback:** if `domain_id` is empty (legacy patterns from before this build), `_question_execution_blockage` can fall back to `_slug(pattern.context) == q_domain`. The spec already achieves this if `from_dict` back-fills empty `domain_id` and the scorer checks `pattern.domain_id or _slug(pattern.context)`.
- **Operator notification:** when a critical question folds into trajectory, no HUD surface change is required. The resolved topic already flows through normal Meta A → AUTO logging.

---

## Verification checklist (pre-push)

- [x] `tests/cognition/test_topic_resolution_3c1.py` still passes (3c.1 invariants unbroken)
- [x] `tests/cognition/test_topic_resolution_3c2.py` passes (27 tests)
- [x] `tests/cognition/test_topic_resolution.py` (Phase 1 acceptance) still passes
- [x] `pressure_loop/open_questions.py` schema round-trip (`test_open_questions_3c2b.py`)
- [x] `deepsleep_loop/failure/run.py` consolidate produces `domain_id`
- [x] `deepsleep_loop/_deepsleep_state.py` `FailurePattern.from_dict` back-compat
- [x] Oscillation guard: `test_oscillation_guard_off_path_question_never_steers`
