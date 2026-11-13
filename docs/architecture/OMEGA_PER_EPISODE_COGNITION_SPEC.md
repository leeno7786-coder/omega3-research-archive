# Omega Per-Episode Cognition — Build Spec (Keystone)

> **Status:** SPEC (Claude) — pre-build, nothing changed in tree
> **Date:** 2026-06-14
> **Branch:** main
> **Source map:** [`AUTO_DETERMINISTIC_OMEGA_COGNITION_MAP.md`](AUTO_DETERMINISTIC_OMEGA_COGNITION_MAP.md) (file:line verified)
> **Loop audit:** [`LEARN_MASTERY_APPLICATION_LOOP_MAP.md`](LEARN_MASTERY_APPLICATION_LOOP_MAP.md)
> **Plan lineage:** [`OMEGA_FIRST_WAKE_DYNAMIC_SUBDOMAIN_PLAN.md`](OMEGA_FIRST_WAKE_DYNAMIC_SUBDOMAIN_PLAN.md)
>
> **Build doctrine (operator, 2026-06-14):** *"do this right the first time and don't look back."* No back-compat, no feature flags, no legacy shims, no dead imports. Each slice replaces the old path with the new and **deletes the dead code in the same slice**. The test gate — not a runtime flag — is the safety net.

---

## 0. The decision

The model never *decides* a tool call — it only emits one the system was already going to execute. So:

- **Auto = the hands.** Deterministic. Fill the Meta-bound params, execute the tool, return the raw result. **Zero `invoke_runtime_generate`.**
- **Omega = the mind.** The single per-episode model call, on the fold-back. It comprehends the raw result, **diffs recalled knowledge vs. the new result**, writes *grounded* mastery, and adjusts pressure. The model relocates from dispatch to cognition — it does not multiply.
- **Pressure is justified by substrate state.** A named, unmastered subcategory is a real reason to learn; raw `epistemic_emptiness` is not.

This collapses loop breaks **#1, #2, #5** into one mechanism: *Omega diffs recall vs. result and writes grounded mastery.* Recall-first **is** that diff's baseline.

---

## 1. Invariants (every slice holds these)

1. **Auto emits zero text inference.** No `invoke_runtime_generate` on any Auto path. (`image_tool` via `invoke_runtime_tool` is a socket, not a text LLM — it stays.)
2. **The model fires at most once per episode**, inside per-episode Omega, **only when `cognized=True`.** A bare IDLE episode runs no model.
3. **Recall is always system-side** (`run_forced_memory_tools` → `memory.api.read_memory`), never a model hop. **Empty recall = VALID** ("I know nothing here yet"). **Recall ERROR = fail-hard** (`memory_continuity_substrate_invalid`).
4. **Single mastery writer = `upsert_mastery`** (`deepsleep_loop/mastery/run.py:169`). Omega **proposes** `mastery_effects`; DeepSleep **persists**. No second write path.
5. **Mastery is grounded coverage, not a counter.** `mastery_effects` carry **absolute** estimates ∈ [0,1] grounded in `recall ∪ new-knowledge` vs. the target competency. Mastery **may decrease.** The blind `+= inc` and its config keys are **deleted**.
6. **`learning_debt` is justified ONLY by mastery-gap records** (`target − knowledge > 0`). The single lever that raises learning pressure is an unmastered subcategory. Creating any new subdomain (onboarding / workorder / web-discovered) writes a gap record ⇒ debt. All categories at/above target ⇒ `learning_debt = 0` (the mind rests / ASK_OPERATOR — it never manufactures pressure). No ungrounded contributors survive.
7. **Windowed Omega is untouched.** The every-10 reflection (`cognition_chain.py:502-505`) keeps owning trajectory/setpoint. Per-episode Omega is additive cognition.
8. **No legacy.** Each slice cuts over and deletes dead code + orphaned imports/config/tests in the same change. Final slice sweeps for stragglers.
9. **Fail-hard cognition; failure-soft substrate boot** — existing doctrine, unchanged.

---

## 2. The fold → Omega packet (intake path)

The fold *is* the intake mechanism — Omega never digs raw scraps out of `episode.auto`.

```
Auto (execute, raw result)
  └─> Meta B  : reflects OUTCOME — executed? success/failure + reason + return-ref
       └─> Global B : BUILDS THE OMEGA PACKET — consolidates raw web_fetch result
                      + recall envelope + outcome + intent/domain/target into one input
            └─> Omega (per-episode) : cognizes the packet
```

- **Meta B** (`meta_loop/consistency` + `reflect`) already folds `executed` + success/failure + reason. Extension: carry the **return reference** so Global B can pull the raw result.
- **Global B** (`global_loop/package/run.py`) becomes the **packet builder** — it already assembles `artifacts.tool_result` / `response_text`; it now assembles the full `OmegaEpisodeInput` and attaches it to the episode for the per-episode Omega hook.
- **Omega** consumes the packet only.

---

## 3. The per-episode Omega contract

New package `omega_loop/episode/` (sibling to windowed `omega_loop/`).

```python
# omega_loop/episode/types.py  (all frozen)

@dataclass(frozen=True)
class MasteryView:
    domain: str
    knowledge_mastery: float
    application_mastery: float
    target_mastery: float

@dataclass(frozen=True)
class OmegaEpisodeInput:            # built by Global B (the packet)
    episode_id: str
    intent: str                    # intent_decision.intent
    action: str                    # USE_TOOL / RESPONSE / IDLE / ...
    tool: str | None
    domain: str | None             # resolved-topic subdomain (the named gap)
    operator_text: str | None      # RESPOND/ASK paths
    outcome: str                   # "success" | "failure" | "none"
    failure_reason: str | None
    raw_tool_result: dict | None   # consolidated raw web_fetch / tool output
    recall: tuple[dict, ...]       # remembered_knowledge_tool_results (may be empty)
    recall_error: str | None       # set => upstream already failed hard
    prior_mastery: MasteryView | None

@dataclass(frozen=True)
class KnowledgeProduct:
    domain: str
    statement: str                 # comprehended, storable fact -> semantic_effects
    confidence: float

@dataclass(frozen=True)
class MasteryEffect:
    domain: str
    knowledge_mastery: float       # ABSOLUTE grounded estimate (NOT a delta)
    application_mastery: float
    rationale: str

@dataclass(frozen=True)
class OmegaEpisodeResult:
    episode_id: str
    cognized: bool                 # False => gate skip (no model ran)
    knowledge_to_ingest: tuple[KnowledgeProduct, ...]
    mastery_effects: tuple[MasteryEffect, ...]
    pressure_params: dict[str, float]
    response_text: str | None      # RESPOND output (relocated from Auto)
    reflection: str | None
```

**Entry point:** `run_omega_for_episode(episode, *, memory=None, runtime_client=None) -> OmegaEpisodeResult`
**Hook:** `cognition_chain.py:501`, immediately after `run_global_b(...)`, before the window gate at 502.

### Action → prompt registry (`omega_loop/episode/prompts.py`)

| Action / intent | Diffs against | Emits |
|---|---|---|
| `LEARN` (→ USE_TOOL + web_fetch) | `recall` vs `raw_tool_result` | `knowledge_to_ingest`, `mastery_effects.knowledge`, `pressure_params` |
| `RESPONSE` / `PROACTIVE_RESPONSE` / `ASK_OPERATOR` | `operator_text` vs `recall` | `response_text` |
| `USE_TOOL` (non-learn) | `recall` vs `raw_tool_result` | `mastery_effects.application`, `knowledge_to_ingest?` |
| `DOCUMENT_CREATE` | template + content | document body (→ render) |
| `IDLE` / nothing to cognize | — | `cognized=False` (skip, no model) |

Structured JSON output, validated, retries on mismatch — same discipline as `omega_loop/_prompt.py`.

---

## 4. Mastery grounding (the heart)

**Delete** the free counter (`mastery/run.py:149-154`) and its config (`deepsleep_mastery_knowledge_increment` / `_application_increment`). `_apply_mastery_for_episode` consumes Omega's `mastery_effects`:

```python
eff = omega_mastery_effects[domain_id]          # required for LEARN/USE_TOOL episodes
knowledge   = eff.knowledge_mastery             # ABSOLUTE, Omega-grounded (may be < prior)
application = eff.application_mastery
record = MasteryRecord(..., knowledge_mastery=knowledge, application_mastery=application,
                       target_mastery=_target_mastery(action_goal), status=status_for(knowledge))
upsert_mastery(record, path=resolved_path)       # still the only writer
```

Mastery is now *what Omega can evidence* (recall ∪ new) vs. target — it can stall or drop. A LEARN episode that fetched nothing new does not raise mastery.

---

## 5. Pressure-loop correctness

`read_learning_debt` (`pressure_loop/pressure_field.py:1408-1515`) is rewritten to derive **solely** from mastery-gap records:

- **Source = mastery store.** Debt magnitude scales with the set/severity of records where `target − knowledge > 0`. No gap records ⇒ `learning_debt = 0`.
- **New subdomain ⇒ gap ⇒ debt.** The subdomain-create path (`memory/subdomain_allocator.py` / `knowledge_router`) registers a `MasteryRecord(knowledge=0, application=0, target=<importance or 0.70>)` via `upsert_mastery` **at create time** — so onboarding, a workorder, and Omega's own web-discovery all raise justified learning pressure through one mechanism.
- **Delete** any ungrounded learning_debt contributors / synthetic seeds.

The named topic already flows from mastery records (`resolve_topic._resolve_mastery_gap`), so no per-(lobe,subdomain) debt struct is needed — the records *are* the per-domain substrate; the scalar is just magnitude.

---

## 6. Slices (RED-first, clean-cut, each leaves a GREEN tree)

No flags. Ordering guarantees a working tree after every slice; dead code dies in-slice.

| # | Slice | Owner | RED asserts (value + fail-mode) |
|---|---|---|---|
| **1** | **Fold packet.** Meta B carries outcome + return-ref; Global B builds `OmegaEpisodeInput` (consolidates raw result + recall + outcome). New `omega_loop/episode/types.py`. Packet built + attached, not yet consumed. | Claude specs / **Cursor** builds | packet carries raw `tool_result` + `recall` (possibly empty) + outcome + domain/target; recall_error propagates; shape exact |
| **2** | **Omega cognition + cut Auto generation (one cutover).** `run_omega_for_episode` + hook at `cc:501` + prompt registry; Omega yields `response_text` + synthesis + `knowledge_to_ingest`. **Same slice deletes** `auto_response`/`auto_synth`/`auto_document` `_generate` + now-orphaned helpers/imports. | **Claude** logic / Cursor mechanical cut / **Claude QC** | operator `response_text` from Omega; LEARN synthesis from Omega; **Auto emits zero `invoke_runtime_generate`**; deleted helpers gone (no dead imports); IDLE ⇒ no model |
| **3** | **Mastery grounding.** `_apply_mastery_for_episode` consumes `mastery_effects` (absolute). **Delete** increment branch + `deepsleep_mastery_*_increment` config + dead refs. | **Claude** (crux) | grounded value reflects diff (empty-recall+rich-fetch ⇒ Omega estimate, fetched-nothing ⇒ no rise); mastery **can decrease**; single writer; increment config gone |
| **4** | **Delete rubber-stamps.** Remove MRIL hop-1 `_generate` + `validate_mril_hop1_tool_call` ⇒ always `run_forced_memory_tools`; remove `TOOL_EXECUTOR` arg-gen ⇒ enforce `tool_call_is_direct`; close the web_fetch-without-query config edge. Delete orphaned helpers/imports. | Cursor / **Claude QC hard** | recall runs with **no** model hop; `mril_hop1_tool_call_invalid` unreachable + its code gone; tool executes on Meta-bound args; empty recall VALID, error fail-hard |
| **5** | **Pressure correctness.** `read_learning_debt` derives solely from mastery-gap records; delete ungrounded contributors. | **Claude** (crux) | debt > 0 **iff** gap records exist; all-mastered ⇒ debt 0; magnitude scales with gap severity |
| **6** | **Subdomain-create ⇒ gap (the keystone wiring).** `subdomain_allocator`/`knowledge_router` create path writes `MasteryRecord(target>0, actual=0)`; wire onboarding save + workorder ingest as callers. | **Claude** | new subdomain ⇒ gap record (target−knowledge ≥ 0.5) ⇒ `learning_debt > 0`; `resolve_learning_topic` names the new domain; onboarding seed ⇒ first LEARN topic = seeded subcategory |
| **7** | **Dead-code sweep + live verify.** find-references sweep for stragglers from slices 2–5 (helpers, imports, config, tests of deleted paths); delete. Live: day-zero → onboard → first-LEARN-on-seeded-topic → grounded mastery climb → recall round-trip; Auto zero-inference in telemetry; gate green. | **Devin** sweep-map / Cursor delete / **Claude** live | zero orphans remain; live boot ignites on the seeded gap, recalls-then-learns, mastery grounded, no Auto `invoke_runtime_generate` |

Per-episode DeepSleep consolidation already runs (`cc:330-341` → `loop.py:226`): Omega's `knowledge_to_ingest` feeds `semantic_effects`, `mastery_effects` feed `update_mastery`. No cadence rebuild.

---

## 7. Open decisions (operator)

1. **Seed target value** — flat **0.70**, or **occupation/subcategory-driven** importance (ties to HUD onboarding Slice 6/7 of the first-wake plan)? Default 0.70 now, drive later.
2. **`pressure_params` Omega may set per-episode** — proposed tight allow-set: lower `learning_debt` contribution for the just-learned domain (via its mastery rise — automatic), raise `application`/`synthesis` debt. Omega does **not** write arbitrary pressure keys.

Both default cleanly; neither blocks Slice 1.

---

## 8. Non-goals (this spec)

- Windowed/trajectory reflection internals.
- The HUD onboarding form (occupation/subcategory dropdowns) — first-wake plan Slice 6; this spec consumes its seed output.
- `image_tool` socket path (not a text LLM).
