# Omega Output Consumption Map — Slice 2B Delivery Cutover

> **Status:** READ-ONLY AUDIT (Cursor)  
> **Date:** 2026-06-14  
> **Branch:** main (`git pull origin main` — up to date)  
> **Work order:** agent bus `#db47d84a` (Claude → Cursor)  
> **Spec anchor:** [`OMEGA_PER_EPISODE_COGNITION_SPEC.md`](OMEGA_PER_EPISODE_COGNITION_SPEC.md) §2–3  
> **Related:** [`AUTO_DETERMINISTIC_OMEGA_COGNITION_MAP.md`](AUTO_DETERMINISTIC_OMEGA_COGNITION_MAP.md)

### Verification methodology

| Claim class | How verified |
|-------------|--------------|
| **Proven** | Every `response_text` / `tool_result_summary` reader located via `rg` across `*.py`; line numbers read directly from live files. |
| **Proven** | Operator delivery path traced: `hud/server.py` → `handle_operator_chat` → `ignite_episode` → `_response_from_episode`. |
| **Inferred** | Per-episode Omega hook insertion at `cognition_chain.py:501` (after `run_global_b`, before tenth-episode branch `502–505`) — not implemented yet; packet builder already wired at `global_loop/loop.py:168–170`. |

---

## 1. Readers of `artifacts.response_text` and `artifacts.tool_result_summary`

These fields live on `episode.global_["outcome"]["artifacts"]`, populated today by Global B from `auto.outputs` at package time (`global_loop/package/run.py:100–105`). Consumers below read **artifacts** (or fall back through artifacts).

| # | File:line | Consumer | Field(s) | Purpose |
|---|-----------|----------|----------|---------|
| 1 | `pressure_loop/operator_chat.py:170–211` | `_response_from_episode` | `response_text` → fallback `tool_result_summary` → fallback `outcome.summary` | **Primary operator delivery** — HUD `POST /api/chat` return body (`message`, `delivery.text`). |
| 2 | `cognition_chain.py:106–109` | `_record_system_message` | `artifacts.response_text` (fallback after `auto.outputs`) | ASK_OPERATOR only → `pressure_loop/system_messages.py` HUD badge panel. |
| 3 | `cognition_chain.py:154–157` | `_record_operator_outcome` | `artifacts.response_text` (fallback after `auto.outputs`) | `store.append_brain_turn` → `response_summary.text` (≤500 chars) for window reflection. |
| 4 | `deepsleep_loop/consolidate/run.py:47–82` | `build_journal_entry` | full `artifacts` dict (includes both fields) | Per-episode journal `JournalEntry.artifacts` persistence. |
| 5 | `pressure_loop/episode_journal.py:98–100, 226–267` | `prior_episode_from_episode` / `enriched_episode_from_episode` | entire `global_["outcome"]` (artifacts nested) | Episode journal JSONL, reflection-window bodies (`stage_episode_for_window_reflection`). |
| 6 | `tests/core/test_slice4_acceptance.py:87, 190` | acceptance tests | artifacts + `grounding_basis` | Regression only — not runtime delivery. |

### Related readers of `auto.outputs` (pre-artifacts, same semantic content today)

| File:line | Consumer | Notes |
|-----------|----------|-------|
| `global_loop/package/run.py:93, 101–102` | **Writer** — copies `auto.outputs` → `artifacts` | Runs at Global B **before** per-episode Omega would run. |
| `global_loop/ground/run.py:19–23` | `ground_phase.run` | Existence check on `auto.outputs`; records paths like `auto.outputs.response_text` in `grounding_basis`. Runs at Global B, before Omega. |
| `meta_loop/reflect/run.py:80` | `trace_summary.has_response_text` | Meta B telemetry flag from `auto.outputs`; not operator-facing. |
| `auto_loop/fold/run.py:67–69` | `_fold_episode_auto` | Copies execute outputs into `episode.auto.outputs` (upstream of Global B). |
| `cognition_chain.py:106, 154` | `_record_system_message`, `_record_operator_outcome` | **Prefer `auto.outputs` first**, then artifacts fallback. After Auto cutover, primary path becomes artifacts. |

### Observations flags (not text, but coupled)

| File:line | Field | Source today |
|-----------|-------|--------------|
| `global_loop/package/run.py:93` | `observations.response_text_present` | `auto.outputs.response_text is not None` |
| `deepsleep_loop/loop.py:73–97` | HUD DeepSleep ring `observations` count | Journal entry observations dict |

---

## 2. Operator response delivery — when and where

### Live path (proven)

```text
HUD POST /api/chat
  hud/server.py:4231–4233  handle_operator_chat(**chat_kwargs)
    pressure_loop/operator_chat.py:368–370  ignite_episode(store, ignition)   [BLOCKS]
      cognition_chain.py:221–318  full cascade + finally close
    pressure_loop/operator_chat.py:407  _response_from_episode(episode)
```

### Timing answer

| Question | Answer | Evidence |
|----------|--------|----------|
| When is text sent to the operator? | **After the full cascade completes** — synchronously before the HTTP response returns. | `handle_operator_chat` calls `ignite_episode` then `_response_from_episode` (`operator_chat.py:368–407`). No early return during Auto execute. |
| During Auto execute? | **No.** Operator waits for Global A → Meta A → AUTO → Meta B → Global B → (10th-ep window branch if applicable) → finally-block journal/close. | `cognition_chain.py:276–318` cascade in `try`; close/journal in `finally`. |
| What text is delivered today? | Global B `artifacts.response_text` / `tool_result_summary` — i.e. **Auto-generated text packaged before any per-episode Omega**. | `global_loop/package/run.py:100–105` copies from `auto.outputs`; `_response_from_episode` reads artifacts (`operator_chat.py:180–182`). |
| Can post-Global-B Omega feed delivery? | **Yes, if Omega patches `global_["outcome"]["artifacts"]` before the `finally` block** (still inside `ignite_episode`). `_response_from_episode` runs after `ignite_episode` returns but reads the closed episode's global outcome. | `cognition_chain.py:297–304` finally runs before return; `operator_chat.py:407` reads artifacts post-return. |

### Intent paths that produce deliverable text

| Intent / action | Auto writes today | Artifact field |
|-----------------|-------------------|----------------|
| `RESPOND` / `PROACTIVE_RESPONSE` / `ASK_OPERATOR` | `auto_loop/execute/run.py:458, 473` → `outputs.response_text` | `artifacts.response_text` |
| `USE_TOOL` (after success) | `auto_loop/execute/run.py:570` → `outputs.tool_result_summary` | `artifacts.tool_result_summary` |
| `USE_TOOL` (synth unavailable) | — | operator_chat falls through to `outcome.summary` (`operator_chat.py:184`) — template string from `global_loop/summarize/run.py:8–36` |

### HUD docstring drift

`hud/server.py:4188–4190` still says the chat path returns "AUTO/Global response artifacts." After Slice 2B this should read Omega-sourced artifacts (same shape, different producer).

---

## 3. `auto_synth` (`TOOL_RESULT_SYNTHESIS`) — production and consumers

### Production (today)

| Step | File:line | What happens |
|------|-----------|--------------|
| Trigger | `auto_loop/execute/run.py:553–568` | After successful `USE_TOOL` invoke; `_generate(operation="auto_synth", prompt_type="TOOL_RESULT_SYNTHESIS")`. |
| Model routing | `auto_loop/route/run.py:46` | `select_model_profile("TOOL_RESULT_SYNTHESIS")` → `TEXT_REASONING`. |
| Write | `auto_loop/execute/run.py:570` | `outputs["tool_result_summary"] = synth_res.get("text")`. |
| Fold | `auto_loop/fold/run.py:69` | Into `episode.auto.outputs.tool_result_summary`. |
| Package | `global_loop/package/run.py:102` | Into `artifacts.tool_result_summary`. |

### Downstream consumers of `tool_result_summary`

| File:line | Consumer | Role |
|-----------|----------|------|
| `pressure_loop/operator_chat.py:181–182` | `_response_from_episode` | **Operator delivery fallback** when `response_text` absent (typical USE_TOOL path). |
| `global_loop/ground/run.py:21–23` | `ground_phase.run` | Adds `auto.outputs.tool_result_summary` to `grounding_basis` when present. |
| `pressure_loop/policy.py:1692` | `global_b_ground_auto_output_paths` | Policy path string for grounding config. |
| `deepsleep_loop/consolidate/run.py:47–82` | journal artifacts | Persisted in `JournalEntry.artifacts`. |
| Tests | `test_slice4_acceptance.py:184–190`, etc. | Regression only. |

**No other live reader** consumes `tool_result_summary` directly. The raw `tool_result` (unsynthesized) is separate: stored in artifacts, used for Omega packet (`omega_loop/episode/packet.py:95–96`), grounding, and journal — not shown to operator unless synthesis fallback fails.

---

## 4. Recommendation — Slice 2B cutover wiring

### Target insertion point

```text
cognition_chain.py:_start_meta_b_global_b
  run_global_b(episode, reflection_dict)          # line 501 — packet + artifacts from Auto today
  run_omega_for_episode(episode)                # NEW — per-episode Omega (spec §3)
  apply_omega_result_to_outcome(episode, result) # NEW — backfill artifacts (below)
  if tenth_episode: _run_tenth_episode_window_pipeline(...)  # line 502–505
```

Hook **must** run before:
- `_run_tenth_episode_window_pipeline` (`cognition_chain.py:502–505`) — journal/window body snapshot uses `enriched_episode_from_episode` outcome.
- `finally` block (`cognition_chain.py:297–304`) — `_record_operator_outcome`, `_record_system_message`, per-episode journal.

### Preferred write-back strategy: patch `outcome.artifacts` in place

Add a small helper (e.g. `omega_loop/episode/apply.py`):

```python
def apply_omega_result_to_outcome(episode, result: OmegaEpisodeResult) -> None:
    global_ = dict(episode.global_ or {})
    outcome = dict(global_.get("outcome") or {})
    artifacts = dict(outcome.get("artifacts") or {})
    observations = dict(outcome.get("observations") or {})

    global_["omega_result"] = result.to_dict()  # canonical Omega record

    if result.response_text is not None:
        artifacts["response_text"] = result.response_text
        observations["response_text_present"] = True
    if result has synthesis field:  # tool_result_summary or dedicated Omega field
        artifacts["tool_result_summary"] = ...

    outcome["artifacts"] = artifacts
    outcome["observations"] = observations
    global_["outcome"] = outcome
    episode.global_ = global_
```

**Why this path:** All operator/journal/window consumers already read `global_["outcome"]["artifacts"]`. No HUD or `operator_chat` changes required for basic delivery.

| Consumer | Change needed if artifacts patched? |
|----------|-------------------------------------|
| `_response_from_episode` | **None** |
| `_record_system_message` / `_record_operator_outcome` | **None** (auto.outputs empty → artifacts fallback) |
| `build_journal_entry` / episode journal | **None** |
| `grounding_basis` | **Optional update** — today records `auto.outputs.*` paths at Global B before Omega; after cutover basis may omit text paths unless re-grounded or basis entries added for `omega_result.*` |
| `meta_loop/reflect` `has_response_text` | **No change for delivery** — telemetry only; will read false after Auto cutover unless Meta B moved after Omega (not recommended) |

### Do NOT rely on `episode.auto.outputs` for delivery post-cutover

Auto will stop writing `response_text` / `tool_result_summary` (Slice 2A). Consumers that prefer `auto.outputs` (`cognition_chain.py:106, 154`) already fall back to artifacts — verified safe.

### Fields Omega should emit (per spec)

| OmegaEpisodeResult field | Maps to artifact | Delivery use |
|--------------------------|------------------|--------------|
| `response_text` | `artifacts.response_text` | RESPOND / PROACTIVE_RESPONSE / ASK_OPERATOR |
| synthesis (tool comprehension) | `artifacts.tool_result_summary` | USE_TOOL operator fallback text |
| `knowledge_to_ingest`, `mastery_effects`, `pressure_params` | **Not** in artifacts | DeepSleep / pressure — separate Slice 2C+ wiring |

### Consumers that must NOT move after Omega

None for **operator text delivery** — they already run after the cascade. The fix is **producer relocation** (Omega writes artifacts) not **consumer relocation**.

### 10th-episode edge case

On the window-closing episode, `_run_tenth_episode_window_pipeline` (`cognition_chain.py:410–421`) runs journal + `stage_episode_for_window_reflection` **inline** before `finally`. Per-episode Omega **must** patch artifacts **before** line 502 so the window body and inline journal carry Omega text, not stale Auto text.

### What to delete in Slice 2B (same slice, per spec §8)

- `auto_loop/execute/run.py:447–473` — `auto_response` paths  
- `auto_loop/execute/run.py:553–581` — `auto_synth` / `TOOL_RESULT_SYNTHESIS` block  
- Orphaned `TOOL_RESULT_SYNTHESIS` routing if no other caller (`auto_loop/route/run.py:46`, `auto_loop/_auto_state.py:33`)

---

## 5. Bus summary (file:line index)

| Topic | Key lines |
|-------|-----------|
| Artifact writer (today) | `global_loop/package/run.py:100–105` |
| Operator delivery reader | `pressure_loop/operator_chat.py:180–211` |
| HUD chat entry | `hud/server.py:4184–4233` |
| Cascade + close order | `cognition_chain.py:276–318, 485–505` |
| ASK_OPERATOR system messages | `cognition_chain.py:84–129` |
| brain_turns operator summary | `cognition_chain.py:134–181` |
| Journal artifacts | `deepsleep_loop/consolidate/run.py:47–82` |
| auto_synth production | `auto_loop/execute/run.py:553–570` |
| Omega packet attach (intake) | `global_loop/loop.py:168–170`, `omega_loop/episode/packet.py:114–123` |
| Recommended hook | `cognition_chain.py:501` (after `run_global_b`, before `502`) |
