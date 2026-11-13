# Topic Resolution Engine

Rule-based priority cascade that resolves a **concrete** learning topic before AUTO execution. The **eight** sources are deterministic regex, priority ordering, and policy thresholds; the 3c.2a fold uses the memory subsystem's Ettin retina similarity for relevance gating (with a perception-unavailable fallback).

## Architecture

**Entry point:** `resolve_learning_topic(state) -> ResolvedTopic(topic, domain, source, confidence)` (`cognition/topic_resolution/resolve_topic.py:656`).

**IRON RULE:** No fallback to generic topics (`"learning material"`, `"general knowledge"`, `"unknown"`, `"general"`, `"random"`). If all sources fail, returns `NO_TOPIC_AVAILABLE`, which causes Meta A to abort the LEARN intent as `action=IDLE` (`meta_loop/tool/run.py:130-136`).

## Eight-Source Priority Cascade (DESCENDING)

The resolver walks these sources in order; the first concrete topic wins:

1. **`operator_query`** — operator text/question patterns (`resolve_topic.py:_resolve_operator_query`, `:135`). Extracts topics from `target_str` / `text` via `domain_key.extract_topic_from_text`.
2. **`omega_choice`** — Omega's specific awakening/episode-chosen topic (`resolve_topic.py:_resolve_omega_chosen_topic`, `:199`). One-shot, consumed via `take_chosen_topic`; a bare-domain echo yields.
3. **`mind_choice`** — the mind's own domain-scoped learning setpoint (`resolve_topic.py:_resolve_mind_choice`, `:148`). Strongest omega-sourced `mastery_gap` pressure level.
4. **`trajectory`** — the mind's goal arc (`resolve_topic.py:_resolve_trajectory`, `:610`). Drives LEARN until ready-to-apply, then yields; absorbs a CRITICAL path-relevant open question as its scoped focus (`trajectory_fold`), else returns the plain `target_domain`.
5. **`mastery_gap`** — largest gap in non-mastered domains (`resolve_topic.py:_resolve_mastery_gap`, `:276`). **Bootstrap-only fallback** when no trajectory is set yet; only surfaces when `max(knowledge_gap, application_gap) >= severe_mastery_gap_threshold` (default 0.5, `:22`).
6. **`open_question`** — oldest/urgent/repeated open questions (`resolve_topic.py:_resolve_open_question`, `:224`). Scored by `confidence * age_seconds * recurrence`.
7. **`learning_debt`** — weakest domain from the episode learning state envelope (`resolve_topic.py:_resolve_learning_debt`, `:324`). Only active when `learning_debt_flags` indicate debt.
8. **`pressure_meta`** — domain/topic metadata from pressure contributors (`resolve_topic.py:_resolve_pressure_meta`, `:647`).

> Note: the actual call order in `resolve_learning_topic` (`resolve_topic.py:666-675`) is: operator_query → omega_choice → mind_choice → trajectory → mastery_gap → open_question → learning_debt → pressure_meta.

## Phase 3c — Developmental-Stability Cascade

### 3c.1 Trajectory Hard-Default + Severe Mastery Gap

- Trajectory (`_resolve_trajectory`, `:610`) is the **hard default once set** — it resolves before open_question, learning_debt, and the mastery-gap bootstrap fallback.
- `SEVERE_MASTERY_GAP_THRESHOLD = 0.5` (`resolve_topic.py:22`). A mastery gap only steers on **cold start** (no trajectory yet) when the largest non-mastered gap is severe (`max(knowledge_gap, application_gap) >= 0.5`).

### 3c.2a — The Fold (Open-Question → Trajectory Scope)

Inside `_resolve_trajectory` (`resolve_topic.py:610-644`), a **critical** open question can **scope** the trajectory:

1. `_pick_critical_open_question(trajectory)` (`resolve_topic.py:506`) scans all OPEN questions.
2. **Relevance gate:** `_question_trajectory_relevance(question, trajectory)` computes binary Ettin-retina similarity between the question and trajectory `target_domain` (`memory.retina.similarity`).
3. **Criticality gate:** `windows_survived >= persistence_threshold` (default 3) **OR** `_question_execution_blockage >= blockage_threshold` (default 0.8).
4. **Oscillation guard:** relevance is a **hard gate** — off-path questions (relevance < 0.6) are never allowed to steer the trajectory, no matter how persistent or blocked.
5. If critical + relevant: the trajectory is scoped to `"{target_domain}: {question_text}"` with confidence 0.90.

Policy thresholds live in `policy/symphony.policy.yaml` and are read through
`pressure_loop.policy`:
```yaml
cognition:
  topic_resolution:
    critical_open_question:
      persistence_threshold: 3
      relevance_threshold: 0.6
      blockage_threshold: 0.8
      blockage_recency_decay: 0.85
      blockage_recency_window: 20
```

**Blockage recency:** `_question_execution_blockage` (`resolve_topic.py:402`) weights each matching `FailurePattern` by episode journal position (`last_episode` vs recent `journal_window` tail). Older failures decay by `blockage_recency_decay` per episode step.

**THE FOLD HUD:** When a critical open question folds into trajectory, `last_topic_fold` is written to shared state and surfaced on the omega panel (`topic_fold` in telemetry).

### 3c.2b — Persistence + Execution Blockage

- **`windows_survived`** (`OpenQuestion.windows_survived`, `pressure_loop/open_questions.py:122`): stamped at creation (`asked_window_id`) and incremented after every archived window.
- **Increment hook:** `increment_windows_survived_for_open_questions()` is called by the active window-reflection handoff before the collecting window is archived, so only still-OPEN questions that have survived a completed window get the bump.
- **`FailurePattern.domain_id`** (`deepsleep_loop/_deepsleep_state.py:250`): set to `canonical_domain_key(context)` at DeepSleep write-time (`deepsleep_loop/failure/run.py:183`, `198`). `_question_execution_blockage` (`resolve_topic.py:361`) does an **exact domain-id match** (with legacy fallback to `canonical_domain_key(pattern.context)`), so blockage scores are precise rather than string-fuzzy.

## Integration

- **Meta A LEARN branch:** `meta_loop/tool/run.py:123` calls `resolve_learning_topic(state)`. If `NO_TOPIC_AVAILABLE`, the tool phase aborts to `SKIP_NO_TOPIC` (`meta_loop/tool/run.py:130-136`) → pack phase overrides `action = "IDLE"`.
- **Meta A memory phase:** `meta_loop/memory/run.py:143` calls `_apply_learn_knowledge_recall`, forcing `remember_knowledge` recall on the resolved topic so LEARN always starts from prior knowledge.
- **DeepSleep mastery:** `deepsleep_loop/mastery/run.py:24-34` (`_episode_target`) prefers `execution_action_pack.execution_inputs.query` (the resolved topic) for mastery credit, ensuring concrete topic attribution.

## Determinism

The eight-source cascade is rule-based and deterministic. The 3c.2a fold calls `memory.retina.similarity()` for question→trajectory relevance; when the brain-resident retina is unavailable, relevance is `0.0` and the cascade falls back to persistence/blockage. The retina supplies binary candidate similarity only; it is not a runtime HTTP service and does not write memory.

## Files

| File | Role |
|------|------|
| `cognition/topic_resolution/resolve_topic.py` | Engine — all resolvers, scoring, and the fold |
| `cognition/topic_resolution/__init__.py` | Re-exports `ResolvedTopic`, `resolve_learning_topic`, `NO_TOPIC_AVAILABLE` |
| `cognition/topic_resolution/domain_key.py` | Stable domain-key generation and topic extraction from free text. Used by `resolve_topic.py` and DeepSleep failure consolidation (`deepsleep_loop/_deepsleep_state.py`, `deepsleep_loop/failure/run.py`). |

## Current State / Legacy Findings

As of the latest codebase audit:

| Metric | Count |
|---|---|
| Python source files in `cognition/` | 4 |
| Actively used modules | 3 (`topic_resolution/__init__.py`, `resolve_topic.py`, `domain_key.py`) |
| Unused modules | 1 (`cognition/__init__.py` — docstring-only package marker) |
| Stub / TODO modules | 0 |

- `resolve_learning_topic()` is on the live Meta A LEARN path (`meta_loop/tool/run.py:123`).
- `domain_key.canonical_domain_key()` is load-bearing for DeepSleep failure-pattern `domain_id` derivation.
- This README previously under-counted the cascade as six sources and omitted `omega_choice`/`mind_choice`; the code and `cognition/README.md` agree on **eight sources**.
- All line-number references in this README were stale and have been updated to the current file.

## Architecture docs

- `docs/architecture/TOPIC_RESOLUTION_AND_MASTERY_GUIDED_LEARNING.md` — Sections 2, 3, 5, 6, 7
- `docs/architecture/TOPIC_RESOLUTION_3C2_SPEC.md` — 3c.2 full spec
- Historical 3c.1 build notes are archived and are not current architecture authority.
- `docs/architecture/TOPIC_RESOLUTION_3C_FEASIBILITY_MAP.md` — feasibility map
