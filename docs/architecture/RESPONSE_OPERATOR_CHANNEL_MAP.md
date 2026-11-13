# Response System + Ask-Operator System Map

> **Status:** Map complete — Phase 1 (Devin) · Phase 2 (plug-in design) is OPERATOR + CLAUDE  
> **Authority:** [`AGENTS.md`](../../AGENTS.md) · live code · verified against repo
> **Date:** 2026-06-12  
> **Work order:** `3ef1a10c` from claude  

---

## Why (the live finding that triggered this)

On the first post-reset run, window 2's reflection chose the epistemically correct move: it refused to invent a learning domain and instead CREATED `operator_priority` pressure (weight 0.9) — **the system decided to ask its operator for direction.** It also drove `epistemic_emptiness` → six `LEARN` intents. The problem: **the system can proactively speak, but it has no outbound channel — the operator never hears it.** A `PROACTIVE_RESPONSE` or operator-directed question is generated and then dies inside the episode body.

This map is the prerequisite for plugging in that channel. It traces what exists, where speech dies, and the candidate plug points. Design decisions happen AFTER the map, with the operator.

---

## A. The Response System — end-to-end flow tables

### A1. `RESPOND` — operator-initiated, WORKING path (the reference shape)

| Step | File | Line | What happens |
|------|------|------|--------------|
| 1 | `hud/server.py` | 4186 | `POST /api/chat` receives operator text |
| 2 | `pressure_loop/operator_chat.py` | 239 | `handle_operator_chat()` entry |
| 3 | `pressure_loop/operator_chat.py` | 303 | `publish_operator_ingress()` writes `LAST_INGRESS_PRESSURE` to SharedState |
| 4 | `pressure_loop/operator_chat.py` | 332 | `from_operator_message()` creates `Contributor(type="operator_query"\|"operator_command", ...)` |
| 5 | `pressure_loop/operator_chat.py` | 333 | `evaluate_operator_ignition()` pins operator as dominant (weight 1.0) |
| 6 | `pressure_loop/pressure.py` | 614 | `EpisodeIgnition(opened_by=dom.type)` — e.g. `"operator_query"` |
| 7 | `cognition_chain.py` | 138 | `ignite_episode()` opens episode, records operator ingress via `_record_operator_ingress()` |
| 8 | `global_loop/intent/run.py` | 22-23 | `opened_by == "operator_query"` → intent `"RESPOND"` |
| 9 | `meta_loop/action/run.py` | 20 | `"RESPOND"` → action `"RESPONSE"` |
| 10 | `auto_loop/promptpack/run.py` | 63-78 | Builds `RESPONSE_REASONING` / `RESPONSE_QUESTION` / `RESPONSE_ACK` prompt pack |
| 11 | `auto_loop/execute/run.py` | 366-474 | Generates `response_text` into `outputs["response_text"]` (line 473 for single-hop, line 458 for mril hop-2) |
| 12 | `auto_loop/fold/run.py` | (implicit) | Auto outcome folded into `episode.auto` slot |
| 13 | `meta_loop/meta_b.py` | 18-29 | `run_meta_b_front()` writes `episode.meta["reflection"]` |
| 14 | `global_loop/loop.py` | 104 | `run_global_b()` assembles outcome |
| 15 | `global_loop/package/run.py` | 77-136 | `run()` + `write_outcome()` writes `episode.global_["outcome"]` with `artifacts.response_text` (line 101) |
| 16 | `pressure_loop/operator_chat.py` | 165 | `_response_from_episode()` reads `artifacts.response_text` (line 180), `tool_result_summary` (line 182), `outcome.summary` (line 184) |
| 17 | `hud/server.py` | 4235 | Synchronous JSONResponse returned to HUD |

**Reference data shape at read point:**
- `artifacts.response_text` (str, the generated text)
- `artifacts.tool_result_summary` (str, synthesized tool result)
- `outcome.summary` (str, Global B summary)
- `episode_id`, `intent`, `action`, `prompt_pack`, `thinking` steps

### A2. `PROACTIVE_RESPONSE` — cognition-initiated, THE GAP

| Step | File | Line | What happens |
|------|------|------|--------------|
| 1 | `pressure_loop/contributors.py` | 27 | `outstanding_question` is a canonical contributor type |
| 2 | `deepsleep_loop/reseed/run.py` | 179-192 | Open questions from `open_queries()` are staged as `Contributor(type="outstanding_question", source="autonomous", ...)` |
| 3 | `pressure_loop/pressure.py` | 84-88 | `stage_reseed()` queues contributors for next `gather_contributors()` |
| 4 | `pressure_loop/pressure.py` | 614 | `EpisodeIgnition(opened_by="outstanding_question")` |
| 5 | `global_loop/intent/run.py` | 26-27 | `opened_by == "outstanding_question"` → intent `"PROACTIVE_RESPONSE"` |
| 6 | `meta_loop/action/run.py` | 22 | `"PROACTIVE_RESPONSE"` → action `"PROACTIVE_RESPONSE"` |
| 7 | `auto_loop/promptpack/run.py` | 80-92 | Builds `PROACTIVE_REASONING` / `PROACTIVE_ACK` prompt pack |
| 8 | `auto_loop/execute/run.py` | 366-474 | **Generates `response_text`** into `outputs["response_text"]` — **line 473** (single-hop) or **line 458** (mril hop-2) |
| 9 | `auto_loop/fold/run.py` | (implicit) | `episode.auto.outputs.response_text` is set |
| 10 | `global_loop/package/run.py` | 101 | `episode.global_["outcome"]["artifacts"]["response_text"]` is set |
| 11 | `cognition_chain.py` | 253 | `store.close(cascade_status=...)` — episode archived to journal |
| 12 | — | — | **NO READER EXISTS** for proactive episodes. The text is produced but never surfaced to the operator. |

**THE GAP — exact lines where text is produced and dropped:**

| Line | File | Data available | What happens next |
|------|------|----------------|-------------------|
| `auto_loop/execute/run.py:473` | `outputs["response_text"] = res.get("text")` | Generated text, episode_id, model_id="CODING_MODEL" | Written to `episode.auto.outputs` via fold |
| `auto_loop/execute/run.py:458` | `outputs["response_text"] = hop2.get("text")` | Generated text (mril hop-2 continuation), episode_id | Written to `episode.auto.outputs` via fold |
| `global_loop/package/run.py:101` | `artifacts={"response_text": outputs.get("response_text"), ...}` | Same text, now in `episode.global_.outcome.artifacts` | Episode closes; no outbound reader |

**Also relevant:** `operator_priority` pressure feedback from Omega is intended to ask the operator, but it is **mis-routed** before it can even reach `PROACTIVE_RESPONSE`:
- `omega_loop/_prompt.py:164` instructs Omega to emit `operator_priority` in `pressure_feedback`
- `deepsleep_loop/reseed/run.py:194-216` stages `Contributor(type="operator_priority", source="omega", ...)`
- `pressure_loop/pressure.py:617` `EpisodeIgnition(opened_by="operator_priority")`
- `global_loop/intent/run.py:20-43` — `operator_priority` is **NOT** in the explicit mapping table. It falls through to the default `return "IDLE"` at **line 43**.
- `meta_loop/action/run.py:27` — `"IDLE"` = "the mind chose quiet, ignite nothing" (SELF_INQUIRY was removed and collapsed to IDLE; the idle-wake awakening owns reflection).
- Result: the system's ask-operator intent is mapped to an idle/quiet episode that ignites nothing. The operator never hears it.

### A3. `RESOLVE_QUERY` — operator-response-initiated

| Step | File | Line | What happens |
|------|------|------|--------------|
| 1 | `global_loop/intent/run.py` | 24-25 | `opened_by == "operator_response"` → intent `"RESOLVE_QUERY"` |
| 2 | `meta_loop/action/run.py` | 21 | `"RESOLVE_QUERY"` → action `"RESPONSE"` |
| 3 | `auto_loop/execute/run.py` | 366-474 | Same AUTO path as `RESPOND`; generates `response_text` |
| 4 | — | — | If autonomously ignited (rare), same gap as `PROACTIVE_RESPONSE`: no reader. If via operator chat, read by `_response_from_episode()`. |

### A4. Episode body slots that carry produced text

| Slot | Writer | Reader | Persists after close? |
|------|--------|--------|----------------------|
| `episode.auto.outputs.response_text` | `auto_loop/execute/run.py:473/458` | `_response_from_episode()` (operator-chat only); window reflection | Yes → journal, window |
| `episode.global_.outcome.artifacts.response_text` | `global_loop/package/run.py:101` | `_response_from_episode()` (operator-chat only) | Yes → journal |
| `episode.global_.outcome.outcome.summary` | `global_loop/package/run.py:88` | `_response_from_episode()` (fallback) | Yes → journal |
| `episode.brain_turns[].response_summary.text` | `cognition_chain.py:124` | Window reflection (Omega) | Yes → window |
| `episode.sensory[]` | `cognition_chain.py:56` `make_sensory_record` | Window reflection | Yes → journal |

**Persistence paths:**
- **Journal:** `pressure_loop/episode_journal.py` `archive_episode()` called from `EpisodeStore.close()` (`episode.py:382`). Writes `enriched_episode_from_episode()` JSON to `guardian/logs/episode_journal.jsonl`.
- **Window:** `pressure_loop/reflection_window.py` — 10-episode window for Omega reflection.
- **Lobe 9:** DeepSleep consolidation writes to memory system, not directly readable as live operator channel.

---

## B. The Ask-Operator System

### B1. Inbound — operator query → episode (WORKS)

| Step | File | Line | What happens |
|------|------|------|--------------|
| 1 | `hud/server.py` | 4186 | `POST /api/chat` receives operator text |
| 2 | `pressure_loop/operator_chat.py` | 239 | `handle_operator_chat()` classifies ingress (`operator_command`, `epistemic_request`, `phatic`) |
| 3 | `pressure_loop/operator_pressure.py` | (called) | `publish_operator_ingress()` writes `LAST_INGRESS_PRESSURE` to SharedState |
| 4 | `pressure_loop/contributors.py` | 93 | `from_operator_message()` creates contributor with type `operator_query` / `operator_command` / `operator_response` |
| 5 | `pressure_loop/operator_chat.py` | 333 | `evaluate_operator_ignition()` pins operator as dominant |
| 6 | `cognition_chain.py` | 138 | `ignite_episode()` → `_record_operator_ingress()` appends sensory + brain_turn receipts |
| 7 | `cognition/topic_resolution/resolve_topic.py` | 534-551 | Priority cascade: `_resolve_operator_query()` is **P1** (first resolver) — extracts topic from operator text |
| 8 | `global_loop/intent/run.py` | 22-23 | `operator_query` → `RESPOND`; `operator_command` → `USE_TOOL` or `LEARN` |
| 9 | (cascade) | | Episode runs, produces response, returns to HUD synchronously |

### B2. Outbound — cognition wants the operator (THE VOID)

#### Path 1: `operator_priority` pressure feedback (mis-routed)

| Step | File | Line | What happens |
|------|------|------|--------------|
| 1 | `omega_loop/_prompt.py` | 164 | Omega prompt instructs: "pressure_feedback lists hold `operator_priority`" |
| 2 | `omega_loop/_omega_state.py` | 8-17 | `PRESSURE_FEEDBACK_WHITELIST` includes `"operator_priority"` |
| 3 | `omega_loop/loop.py` | 38-100 | `run_omega_for_window()` → `reflect_phase.reflect()` produces `pressure_feedback` dict |
| 4 | `deepsleep_loop/reseed/run.py` | 194-216 | Parses `contributors_to_create` / `contributors_to_raise` with type `"operator_priority"` → `Contributor(source="omega", type="operator_priority", ...)` |
| 5 | `pressure_loop/pressure.py` | 84-88 | `stage_reseed()` queues the contributor |
| 6 | `pressure_loop/pressure.py` | 617 | `EpisodeIgnition(opened_by="operator_priority")` |
| 7 | `global_loop/intent/run.py` | **43** | **DEFAULT FALLBACK**: `operator_priority` is not in `_map_opened_by_to_intent()` → returns `"IDLE"` (SELF_INQUIRY was removed and collapsed to IDLE) |
| 8 | `meta_loop/action/run.py` | 27 | `"IDLE"` = "the mind chose quiet, ignite nothing" — no tool, no response for operator |
| 9 | `auto_loop/execute/run.py` | 476-601 | IDLE ignites nothing — no response text for operator |
| 10 | `cognition_chain.py` | 253 | Episode closes, archived to journal |
| 11 | — | — | **TERMINATES HERE**: The operator never hears the ask. The intent was lost at Global A intent mapping. |

#### Path 2: `outstanding_question` → `PROACTIVE_RESPONSE` (text produced, no reader)

| Step | File | Line | What happens |
|------|------|------|--------------|
| 1 | `deepsleep_loop/reseed/run.py` | 179-192 | Stages `Contributor(type="outstanding_question", ...)` |
| 2 | `pressure_loop/pressure.py` | 617 | `EpisodeIgnition(opened_by="outstanding_question")` |
| 3 | `global_loop/intent/run.py` | 26-27 | Maps to `"PROACTIVE_RESPONSE"` |
| 4 | (cascade) | | Same as A2 steps 6-11: text generated, flows to `episode.global_.outcome.artifacts.response_text`, episode closes |
| 5 | — | — | **TERMINATES HERE**: No code reads `artifacts.response_text` for autonomous episodes. The text dies in the episode body. |

### B3. Existing surfaces an outbound channel could ride (factual inventory only)

| Surface | Location | What it supports | What it does NOT support |
|---------|----------|------------------|--------------------------|
| **HUD `POST /api/chat`** | `hud/server.py:4186` | Synchronous request/response for operator-initiated chat | No server-push, no polling endpoint, no chat history GET. Only works when the operator already sent a message. |
| **HUD SSE `/api/events`** | `hud/server.py:1045` | Server-push event stream (phase, signal, memory events) from `guardian.terminal_feedback` | Does NOT carry chat messages, episode outcomes, or response_text. Event types are `'phase'` / `'signal'` / `'memory'`. |
| **SharedState `RECENT_OMEGA_EPOCHS`** | `core/shared_state_keys.py:154` | Ephemeral runtime ring (poll-only). HUD reads for Trajectory Star Map. | No push mechanism. Does not carry text/response payloads. Classification: `EPHEMERAL_RUNTIME` (boot-empty). |
| **SharedState `LAST_AUTO_CYCLE`** | `core/shared_state_keys.py:133` | Canonical inter-loop wire: AUTO writes `CycleEnvelope` with `phases`, `telemetry`. | Shape is envelope-oriented, not free-form text. No reader in HUD for chat delivery. |
| **TTS / voice path** | `runtime/tts.py:31` `synthesize()` → `runtime/client.py:797` `/synthesise` | Runtime socket converts text → audio bytes. Config: `config/tts.yaml`. | No automatic invocation from episode outcomes. No queue/persistence for speech. Would require explicit code to extract `response_text` and call `tts.synthesize()`. |
| **guardian/state/queues/** | `guardian/state/queues/` | Files: `deepsleep_candidates.json`, `reflection_eligible.jsonl` | No notification/alert queue for operator messages. No HUD poller reads these for chat. |
| **guardian terminal feedback** | `guardian/terminal_feedback.py` | Emits to SSE stream + `guardian/logs/feedback_audit.jsonl` | Telemetry/audit surface, not a delivery channel. No structured text payload for operator consumption. |
| **Episode journal** | `pressure_loop/episode_journal.py` | Append-only JSONL (`guardian/logs/episode_journal.jsonl`). Durable, ordered. | Write-only for live consumption. HUD does not tail-read journal for chat (doctrine: audit is write-only). |

### B4. Retired `response_engine` / outlet (FIREWALLED — do not resurrect)

| Reference | File | Line | What it DID | Status |
|-----------|------|------|-------------|--------|
| `response_engine` module | (deleted) | — | Central routing/orchestration of responses | **Deleted**; see `AGENTS.md` |
| `response_hold` outlet | `runtime/runtime_capability_broker.py` | 415 | `response_hold()` method on `CapabilityDecision` | **Only for runtime capability preflight** — not episode routing. |
| Legacy HUD route | `hud/server.py` | 3345 | Returns `"response_engine deleted during rebuild"` | **Degraded stub** |
| `episode.response_hold` | `pressure_loop/episode.py` | 187 | Schema slot comment references it | **Retired** — no code writes it |
| Policy flags | `policy/directives.policy.yaml:23`, `policy/cognition_chain.policy.yaml:39` | `no_symphony_outlet_placeholder: true`, `no_response_hold_short_circuit: true` | Explicitly disables resurrection | **DO-NOT-RESURRECT** |

Per the historical map (superseded by `AGENTS.md`):
> "`run_response_hold_movement`, `response_engine` outlet dispatch — Not on live path; do not reintroduce"
> "Ignore outlet packs, ignition routes, and CueDesk docs unless explicitly deleting or archiving them."

### B5. Operator-chat pause gate + cognition lock

| Gate | File | Line | Constraint on outbound channel |
|------|------|------|-------------------------------|
| Pressure loop paused | `pressure_loop/operator_chat.py` | 265-276 | `is_pressure_loop_paused()` → returns `{"ok": False, "error": "substrate_paused"}`. Any outbound channel that tries to open an episode during pause must respect this. |
| Cognition lock | `core/cognition_lock.py` | (module) | `cognition_lock()` serializes `ignite_episode()`. HUD chat and pressure tick contend for the same lock. An outbound channel that triggers episode ignition would queue behind or block on this lock. |

---

## Gap Table — where operator-directed speech is produced and dropped

| # | Gap point | File:Line | Data available | Intent / episode_id | Why it dies |
|---|-----------|-----------|----------------|---------------------|-------------|
| 1 | **Intent mis-route** | `global_loop/intent/run.py:43` | `opened_by="operator_priority"`, weight 0.9, origin `omega_pressure_feedback` | Was: ask-operator. Becomes: `IDLE` | `operator_priority` not in `_map_opened_by_to_intent()` explicit table; falls to default `"IDLE"` (SELF_INQUIRY removed → collapsed to IDLE) |
| 2 | **Text produced, no reader** | `auto_loop/execute/run.py:473` | `outputs["response_text"]` = generated text (str) | `PROACTIVE_RESPONSE` episode | AUTO writes to `episode.auto.outputs`, but no code path reads this for autonomous episodes |
| 3 | **Artifact packaged, no reader** | `global_loop/package/run.py:101` | `artifacts["response_text"]` = same text | `PROACTIVE_RESPONSE` episode | Global B packages it into `episode.global_.outcome.artifacts`, but `_response_from_episode()` only runs for operator-chat-initiated calls |
| 4 | **Episode closes to journal** | `cognition_chain.py:253` | Full episode body with `artifacts.response_text` | Any proactive/ask-operator episode | `store.close()` → `archive_episode()` → `episode_journal.jsonl`. No live reader surfaces this to operator. |

---

## Candidate plug points (factual list — no design, no recommendation)

| # | Plug point | Existing support | Would need |
|---|------------|-------------------|------------|
| A | **HUD SSE `/api/events`** | Server-push exists, client reconnects with `Last-Event-ID` | New event type (e.g. `chat` or `proactive`) and a writer that emits `response_text` when a proactive episode closes. |
| B | **New SharedState ring** | Pattern exists: `RECENT_OMEGA_EPOCHS`, `RECENT_DEEPSLEEP_CYCLES` are ephemeral rings with HUD pollers | New canonical key (e.g. `RECENT_OPERATOR_MESSAGES`) with `EPHEMERAL_RUNTIME` classification, written at episode close, read by HUD poll. |
| C | **HUD `POST /api/chat` → polling loop** | No polling endpoint today | A GET endpoint or WebSocket that the HUD polls/reads for "unread proactive messages". Would need persistence of "delivered" state. |
| D | **TTS synthesize + audio playback** | `runtime/tts.py:31` works; `runtime/client.py:797` `/synthesise` endpoint | Extractor at episode close that reads `artifacts.response_text` and calls `tts.synthesize()`. No queue/persistence for missed audio. |
| E | **Guardian terminal_feedback SSE** | Already streams phases/signals | Extend emit with a new `kind` carrying proactive text. HUD already subscribes to `/api/events`. |

**What each surface already supports:**
- **SSE (`/api/events`)**: Push, ordering via seq, reconnect with last-event-id, no persistence across server restart.
- **SharedState rings**: Poll-only (client pulls), ephemeral (boot-empty), no ordering guarantee beyond append.
- **TTS**: Synchronous request/response, no queue, no retry, no delivery tracking.
- **Journal (`episode_journal.jsonl`)**: Durable, ordered, append-only. Doctrine: write-only; HUD does not tail-read it.

---

## Verification notes

- All file:line citations verified against `main` branch at commit `45a75723`.
- `operator_priority` does NOT appear in `global_loop/intent/run.py` explicit mapping — confirmed by grep.
- `_response_from_episode()` only exists in `pressure_loop/operator_chat.py` — no other reader for `artifacts.response_text` found.
- No `GET /api/chat/history` or similar endpoint exists in `hud/server.py`.
- `response_engine` module does not exist in repo; confirmed by `find_file_by_name`.
- System is shut down; all observations from static code analysis.

---

*Map only — no code changes. Design decisions are operator-gated Phase 2.*
