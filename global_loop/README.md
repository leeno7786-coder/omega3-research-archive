# Global Loop

The **GLOBAL LOOP** — a pure sequencer with per-phase callable modules. This subsystem orchestrates Global A and Global B phase chains in a deterministic, no-model architecture.

> **Current model (no goal):** Global A **parses an intent** — it produces an
> `IntentDecision`, never a goal or a target. Global B reflects the episode into
> a single `EpisodeOutcome`; `action_goal` / `goal_state` survive only as
> historical compatibility fields for persisted readers.

## Config, policy, and schema

| Artifact | Path | Role |
| -------- | ---- | ---- |
| Defaults | `config/global.yaml` (Global A), `config/global_b.yaml` (Global B) |
| Intent routing | `config/global_intent.yaml` | `opened_by` → intent + `operator_command` heuristics (mirrors `intent.py`) |
| Types | `schema/global.schema.yaml`, `schema/global_b.schema.yaml` |
| Intent types | `schema/global_intent.schema.yaml` | Allowed intents + `opened_by_map` shape |
| Merged policy | `policy/symphony.policy.yaml` → `global:` | Runtime overrides; loaders in `pressure_loop/policy.py` |

Slice cascade flags remain under `pressure:` in `policy/symphony.policy.yaml`
and are read through `pressure_loop.policy`.

## Scope

The global loop is a **pure sequencer** with no business logic. It coordinates two entry points:
- `run_global_a` — Ignition → intent (produces `IntentDecision`)
- `run_global_b` — Meta B pack up → assemble → ground → summarize → followups → package (produces `GlobalOutcomePack` carrying the `EpisodeOutcome`)

Each phase is a separate callable module under `global_loop/`. The loop is deterministic and runs no models.

## Architecture

### `loop.py` — Pure Sequencer
The main sequencer with two entry points (`global_loop/loop.py`).

**Key functions:**
- `run_global_a(episode, ignition)` — Runs Global A chain: ignition → intent. Builds the `IntentDecision` inline (confidence + reason from the ignition), writes `episode.global_['intent_decision']`, and returns the `IntentDecision`. **No extract, no goal, no target.**
- `run_global_b(episode, meta_reflection)` — Runs Global B chain: assemble → ground → summarize → followups → package. Computes the trajectory alignment / divergence / execution outcome inline, writes `episode.global_['outcome']` (the `EpisodeOutcome`), and returns the `GlobalOutcomePack`. **No goal_state phase.**

Omega handoff is owned by `cognition_chain.py` after Global B.

### Phase Sub-Package Pattern
Each phase is a separate sub-package under `global_loop/` with the standard pattern:
- `<phase>/run.py` — Contains the `run()` function for that phase
- `<phase>/__init__.py` — Re-exports `run` (from .run import run)

The loop imports each phase package and calls its `run()` function. This modular design allows phases to be developed and tested independently.

## Global A Phase Chain

Global A is **`ignition` → `route` → `intent`** plus the inline
`IntentDecision` assembly in `run_global_a`. The `route` phase runs only for an
operator chat turn with no explicit selection; target/topic resolution belongs
downstream.

### 1. `ignition/` — Pass-through
Validates shape only; never rebuilds or re-derives the ignition (`global_loop/ignition/run.py`).

**Input:** `EpisodeIgnition` from pressure
**Output:** `EpisodeIgnition` (validated, unchanged)
**Behavior:** Validates required fields (`opened_by`, `dominant_contributor`)

### 2. `route` — Operator Intent Router
`global_loop/intent/route.py`. An `operator_query` / `operator_command` turn with no
`directive_intent` (other than `RESPOND`), no plugin selection and no explicit learning
command is routed by the brain-resident ModernBERT intent router
(`global_loop/intent/router.py`, a hash-pinned ONNX bundle seated at boot). The router
reads the message with the previous assistant action (`global_loop/intent/context.py`)
and returns `FREEFORM_CHAT`, `CODING_AGENT`, `BROWSER_AGENT` or `DOCUMENT_CREATION`,
or abstains. An abstention escalates to Global A's reasoning model (operation
`intent_escalation`, strict JSON `{route, reason}`), which chooses the route.

| Route | Intent | Lane |
|---|---|---|
| `FREEFORM_CHAT` | `RESPOND` | chat |
| `CODING_AGENT` | `RESPOND` | agent |
| `BROWSER_AGENT` | `BROWSE` (binds the open browser workflow or authors a plan) | browser |
| `DOCUMENT_CREATION` | `DOCUMENT_CREATE` | chat |

The decision is written to `episode.global_['operator_route']`; Meta A reads its lane.
Ingress never routes: it only handles deterministic commands and closes an open
browser workflow on an explicit finish phrase.

### 3. `intent/` — Intent Mapping
A routed turn takes the route's intent. Every other ignition maps `opened_by` +
dominant message → one of `ALLOWED_INTENTS`
(`global_loop/intent/run.py` — `_map_opened_by_to_intent`).

**Input:** `EpisodeIgnition`, optional operator route
**Output:** `intent` string (one of ALLOWED_INTENTS)
**Mapping rules (unrouted ignitions):**
- `operator_query` → `RESPOND`
- `operator_response` → `RESOLVE_QUERY`
- `operator_priority` → `ASK_OPERATOR` (the mind's own ask; first-class artifact, never folded into `PROACTIVE_RESPONSE`)
- `proactive_update` → `PROACTIVE_RESPONSE` (autonomous status → main chat feed)
- `learning_debt` / `curiosity_gap` / `epistemic_emptiness` / `mastery_gap` → `LEARN` (`mastery_gap` is the mind-primacy learning signal from the Omega lever; must reach `LEARN` or the mind-driven learn loop never closes)
- `clarification_gap` → `ASK_OPERATOR` (the mind's own ASK lever: `clarification_debt_delta` → `clarification_gap`; mirrors the `mastery_gap` learn lever and the `execution_gap` build lever)
- `identity_gap` / `trajectory_gap` → `SELF_INQUIRY`
- `execution_gap` / `application_gap` / `execution_continuation` → `USE_SKILL` (flag ON) / `USE_TOOL` (flag OFF) — agentic execution of learned-but-unapplied work; `APPLY` is retired
- `operator_command` → `IMAGE_CREATE` / `DOCUMENT_CREATE` for explicit directives, `LEARN` for a leading learning request, `RESPOND` for remember/note and ordinary work requests (the chat executor chooses tools), or `PLUGIN_OPERATION` for an eligible signed plugin operation
- `operator_query` / `operator_command` → `PLUGIN_OPERATION` when signed plugin discovery finds an eligible operation
- `idle_probe_aggregate` → `IDLE`
- `PURSUE_OUTCOME` is reserved for validated Omega-authored emergent pressure and maps to motor-program execution.
- **any other type** → `IDLE` unless a more specific mapping above applies.

**Directive override:** an explicit `directive_intent` in the dominant contributor metadata (validated against `ALLOWED_INTENTS`) is authoritative — Global A obeys it verbatim and never re-infers from message words (the `opened_by` mapping above is the fallback for typed/voice turns with no command). This is the same exact-keyword rail used by `IMAGE_CREATE` / `DOCUMENT_CREATE`.

### 4. `IntentDecision` assembly (inline in `run_global_a`)
There is no target/goal assembly in Global A. After the intent maps,
`run_global_a` assembles the `IntentDecision` inline and writes it to
`episode.global_['intent_decision']`.

**Input:** validated `EpisodeIgnition` + mapped `intent`
**Output:** `IntentDecision`
**Fields:**
- `episode_id` — from ignition
- `intent` — from the intent phase
- `confidence` — `clamp(dominant_contributor.weight or aggregate_pressure, 0..1)`
- `reason` — `f"opened_by={opened_by} aggregate={aggregate_pressure:.3f}"`

Meta A consumes the intent decision; topic/target resolution is owned downstream (e.g. `cognition/topic_resolution`), not by Global A.

## Global B Phase Chain

Global B reflects the closed episode — the intent chosen vs the active trajectory vs Meta B's execution verdict — into a single **`EpisodeOutcome`**. The chain is `assemble → ground → summarize → followups → package`. There is **no `goal_state` phase**: the execution disposition is read inline in `run_global_b` and passed to the downstream phases as `compat_status` (the old goal_state vocabulary, kept so locked windows / DeepSleep readers still parse — see [Compat aliases](#compat-aliases)).

### 1. `assemble/` — Input Assembly
Assembles `GlobalBInput` from episode + Meta B reflection pack (global_loop/assemble/run.py).

**Input:** `episode`, `meta_reflection` (from Meta B)
**Output:** `GlobalBInput` (dataclass: `episode_id`, `pressure`, `source_action_goal`, `execution_pack`, `auto_outcome`, `meta_reflection`, `trajectory_snapshot`)
**Behavior:** Folds episode data (pressure, global_, meta, auto) into a single input structure. Falls back to direct episode reads if `meta_loop.meta_b.meta_b_input_from_episode` is unavailable. The `source_action_goal` field name is a **compat alias** — it is dual-read from `global_['intent_decision'] or global_['action_goal']`, so the source is the live `IntentDecision`.

### 2. Disposition → `compat_status` (inline in `run_global_b`)
No separate goal phase exists. `run_global_b` reads the execution disposition
and uses it as `compat_status` for the summarize/followups/package phases. The
downstream parameter name `goal_state` is a historical compatibility spelling
for that disposition.

| disposition / `compat_status` | meaning |
|--------------------------------|---------|
 | `completed`, `degraded`, `failed`, `skipped`, `idle`, `invalid`, `pending_input` | passed through as the episode disposition |

### 3. `ground/` — Existence-based Grounding
Determines grounding_basis from existing outputs (no fabrication) (global_loop/ground/run.py).

**Input:** `auto_outcome`, `meta_reflection`
**Output:** `(grounded: bool, grounding_basis: List[str])`
**Behavior:** Checks for existence of:
- `auto.outputs.response_text`
- `auto.outputs.tool_result`
- `auto.outputs.tool_result_summary`
- `auto.outputs.image_artifact`
- `meta.execution.disposition`

Grounded = true if any outputs exist AND disposition is present.

### 4. `summarize/` — Deterministic Summary
Template-based summary from `compat_status` + action + failure_kind (global_loop/summarize/run.py).

**Input:** `goal_state` (param name; carries `compat_status`), `action`, `failure_kind`
**Output:** `summary` string
**Templates** (sourced from `config/global_b.yaml` → `summarize.completed_by_action`):
- `completed` + `USE_TOOL` → "Tool execution completed successfully."
- `completed` + `USE_SKILL` → "Skill-mediated execution completed successfully."
- `completed` + `RESPONSE` → "Response generated successfully."
- `completed` + `PROACTIVE_RESPONSE` → "Proactive response generated successfully."
- `completed` + `IMAGE_CREATION` → "Image creation completed successfully."
- `completed` (other) → "Execution completed successfully."
- `failed` → "Execution failed: {failure_kind}."
- `degraded` → "Execution partially completed with degradation."
- `skipped` → "No execution was performed."
- `idle` → "Chose idle with a recorded reason."
- `invalid` → "Outcome invalid due to packet inconsistency or missing trace."

### 5. `followups/` — Non-executable Followups
Deterministic followup suggestions based on `compat_status` + failure_kind (global_loop/followups/run.py).

**Input:** `goal_state` (param name; carries `compat_status`), `failure_kind`, `action`
**Output:** `List[dict]` with followup suggestions
**Rules:**
- `completed` → `none` (no followup needed)
- `invalid` → `needs_clarification` (suggested_intent: `IDLE`) — per live `config/global_b.yaml`
- `failed` + clarification errors → `needs_clarification` (suggested_intent: `RESPOND`)
- `failed` + tool errors → `needs_retry` (suggested_intent: `USE_TOOL`)
- `degraded` → `needs_retry` (suggested_intent: `USE_TOOL`, `USE_SKILL`, or `RESPOND` — action-keyed from `config/global_b.yaml`; `USE_SKILL` when the original action was `USE_SKILL`, `USE_TOOL` when it was `USE_TOOL`, `RESPOND` otherwise)
- `skipped` → `none`
- `idle` → `none`

### 6. `package/` — GlobalOutcomePack Assembly
Assembles `GlobalOutcomePack` (carrying the `EpisodeOutcome`) and writes it to the episode (global_loop/package/run.py).

**Input:** `GlobalBInput`, `intent`, `trajectory_alignment`, `divergence_score`, `execution_outcome`, `compat_status`, `grounded`, `grounding_basis`, `summary`, `followups` (+ optional episode `temporal` / `learning_state`)
**Output:** `GlobalOutcomePack` (dataclass)
**Fields:**
- `episode_id` — from input
 - `outcome` — the **`EpisodeOutcome`**: `intent`, `trajectory_alignment`, `divergence_score`, `execution_outcome` (`{executed, status, failure_reason}`), plus `status`, `grounded`, `grounding_basis`, `summary`, `failure_kind`, `failure_reason`. There is no goal field.
- `observations` — presence flags (response_text, tool_result, image_artifact, document_artifact, memory_used), warnings
- `artifacts` — actual outputs (response_text, tool_result_summary, tool_result, image_artifact, document_artifact)
- `followups` — followup suggestions list
- `consistency` — from meta_reflection
- `temporal`, `learning_state` — compact summaries threaded for Omega

**`trajectory_alignment` / `divergence_score`** are computed inline in `run_global_b._trajectory_alignment`: the resolved-topic domain is compared against `trajectory_snapshot.target_domain`/`target_capability`; `divergence_score = round(1 − Jaccard, 3)`. Non-learning episodes with an empty domain return `no-trajectory, 0.0` (not drift).

**Side effect:** `package.write_outcome(episode, pack)` writes `episode.global_['outcome']` with the pack as a dict.

## Sequence / Handoffs

1. **Pressure ignites** → `EpisodeIgnition` handed to the cognition chain
2. **Global A** (`run_global_a`) → produces `IntentDecision` (written to `episode.global_['intent_decision']`), drops to Meta A via `cognition_chain`
3. **Meta A → AUTO → Meta B** run their loops (not in global_loop/); there is
   no model inference between Meta A and AUTO
4. **Meta B** (`meta_loop.loop.run_meta_b`) ends by handing its reflection pack UP to `global_loop.loop.run_global_b`
5. **Global B** (`run_global_b`) → produces `GlobalOutcomePack` (the `EpisodeOutcome` on `episode.global_['outcome']`)
6. **Omega** — `cognition_chain.py` runs the per-episode fold after Global B. A
   separate staged ten-episode window lane may also open a reflection episode;
   it does not replace the per-episode fold.

## Current State / Legacy Findings

As of the latest codebase audit:

| Metric | Count |
|---|---|
| Python files in `global_loop/` | 17 |
| Live cognition-path files | 8 (`loop.py` + 7 phase packages) |
| Compat / test-only files | 1 (`global_b.py`) |
| Unused package markers | 1 (`global_loop/__init__.py` — docstring only, no exports, no callers) |
| Stub / TODO modules | 0 |

**Live path:** `loop.py` exports `run_global_a` and `run_global_b`. Global A phases: `ignition` → `intent`. Global B phases: `assemble` → `ground` → `summarize` → `followups` → `package`. All phase packages are imported and called by `loop.py`.

**Compat / test-only:**
- `global_b.py` is a Slice 4 compat re-export shim. It has **no production callers** but is used by tests (`test_global_b_*`, `test_slice4_acceptance.py`, etc.).
- `global_loop/__init__.py` is a docstring-only package marker with no exports; it is not imported anywhere.

**Audit note:** Omega handoff is owned by `cognition_chain.py`; older policy and
architecture wording that assigns it to Global B is historical and not current
execution ownership.

## Determinism

The global loop is **fully deterministic** — no model inference, no random choices. All mappings are 1:1 or template-based.

## Historical compatibility fields

The current cascade emits an `IntentDecision` and an `EpisodeOutcome`. Historical
field names remain only in compatibility-shaped persisted data and reader
parameters so older records and reflection-window payloads can still be read:

- **`action_goal`** — `assemble/run.py` dual-reads `global_['intent_decision'] or global_['action_goal']`; `GlobalBInput.source_action_goal` keeps that field name but is sourced from the live `IntentDecision`.
- **`goal_state`** — no phase package; the disposition string is computed inline as `compat_status` and flows through `summarize`/`followups` (whose parameter is still spelled `goal_state`). The `EpisodeOutcome.status` field == the old goal_state vocab so locked-window / DeepSleep readers still parse.
- **`global_b.py`** — compatibility re-export shim; logic lives in the phase
  sub-packages and `loop.py`.

## Ignition Note

The ignition phase takes **EXACTLY** the `EpisodeIgnition` that pressure sends —
no rebuild and no re-derivation.
