# Cognition

Deterministic cognitive utilities that sit **between** pressure and execution -- topic resolution, intent classification, and knowledge extraction helpers. The eight priority sources are rule-based. The 3c.2a criticality fold uses the memory subsystem's Ettin retina similarity as a candidate relevance signal; persistence and execution-blockage rules remain the fallback when that perception lane is unavailable.

## Modules

### `topic_resolution/resolve_topic.py` -- Topic Resolution Engine

Deterministic eight-source priority cascade that resolves a concrete learning topic before AUTO execution. See `cognition/topic_resolution/README.md` for the full engine + the Phase 3c developmental-stability cascade, and `docs/architecture/TOPIC_RESOLUTION_3C2_SPEC.md` for the stabilizer fold.

**Entry point:** `resolve_learning_topic(state) -> ResolvedTopic(topic, domain, source, confidence)`

**Eight-source cascade (DESCENDING):**
1. `operator_query` -- operator text/question patterns
2. `omega_choice` -- Omega's specific awakening/episode-chosen topic (one-shot, consumed via take_chosen_topic; a bare-domain echo yields)
3. `mind_choice` -- the mind's own domain-scoped learning setpoint (strongest omega-sourced mastery_gap pressure level)
4. `trajectory` -- the mind's goal arc; drives LEARN until ready-to-apply, then yields; absorbs a CRITICAL path-relevant open question as its scoped focus (trajectory_fold), else the plain `target_domain`
5. `mastery_gap` -- BOOTSTRAP fallback (cold start, no choice/trajectory yet): SEVERE deficit only: `max(target-knowledge, target-application) >= 0.5`
6. `open_question` -- non-critical fallback
7. `learning_debt` -- weakest domain from episode learning state
8. `pressure` -- domain/topic metadata from state.meta

**IRON RULE:** No fallback to generic topics (`"learning material"`, `"general knowledge"`, `"unknown"`, `"general"`, `"random"`). If all eight sources fail, returns `NO_TOPIC_AVAILABLE`, which causes Meta A to set `state.execution_intent = "SKIP_NO_TOPIC"` and abort the LEARN branch.

**Integration:** Called from `meta_loop/tool/run.py` in the LEARN branch (`meta_loop/tool/run.py:123-170`).

### `topic_resolution/__init__.py`

Re-exports `ResolvedTopic`, `resolve_learning_topic`, `NO_TOPIC_AVAILABLE`.

### `cognition/__init__.py`

Documentation-only package marker; it exports nothing and is not imported by
live code or tests.

## Current State / Legacy Findings

As of the latest codebase audit:

| Metric | Count |
|---|---|
| Python source files in `cognition/` | 4 |
| Actively used modules | 3 (`topic_resolution/__init__.py`, `resolve_topic.py`, `domain_key.py`) |
| Unused modules | 1 (`cognition/__init__.py` — docstring-only package marker, never imported) |
| Stub / TODO modules | 0 |

- `resolve_learning_topic()` is the only live production entry point, called from `meta_loop/tool/run.py` in the LEARN branch.
- `domain_key.py` is also used by DeepSleep failure consolidation for stable `domain_id` derivation.
- No code deletion is warranted; all functional modules are exercised by production code or tests (`tests/cognition/` contains 11 test files).

## Determinism

The priority cascade (operator_query, omega_choice, mind_choice, trajectory, mastery_gap, open_question, learning_debt, pressure) is rule-based and deterministic. The 3c.2a fold calls `memory.retina.similarity()` for question→trajectory relevance; when the brain-resident retina is unavailable, the score is `0.0` and the cascade falls back to persistence/blockage scoring. The retina returns binary candidate similarity and does not write or verify memory.
