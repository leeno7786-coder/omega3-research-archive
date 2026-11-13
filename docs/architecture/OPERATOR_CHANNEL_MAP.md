# Operator Channel Map — As-Built (commit `bae32e3c`)

> File:line citations refer to `bae32e3c`. This is a **read-only historical audit map**, not a build spec. `AGENTS.md` is the law.

**See Also:**
- [`README.md`](README.md) — Architecture directory index
- [`RESPONSE_OPERATOR_CHANNEL_MAP.md`](RESPONSE_OPERATOR_CHANNEL_MAP.md) — Response channel mapping
- [`COGNITION_CHAIN_AND_PROMPTS_MAP.md`](COGNITION_CHAIN_AND_PROMPTS_MAP.md) — Cognition chain structure
- [`../../contracts/06_response_system.md`](../../contracts/06_response_system.md) — Response system contract
- [`../../contracts/proactive_cognition.md`](../../contracts/proactive_cognition.md) — Proactive cognition

---

## Overview

The operator channel is a **first-class two-way artifact** added by operator directive 2026-06-12. It is not a retrofit — every layer (`pressure → Global A → Meta A → AUTO → text app → return path → window reflection`) has a dedicated, auditable slot.

| Direction | Route | Key module |
|-----------|-------|------------|
| **Inbound** | Operator message → supremacy + queue box → one episode per input | `pressure_loop/operator_queue.py`, `pressure_loop/pressure.py`, `pressure_loop/operator_chat.py` |
| **Outbound** | Omega reflection → `operator_priority` pressure → `ASK_OPERATOR` episode → text app | `cognition_chain.py`, `global_loop/intent/run.py`, `meta_loop/action/run.py`, `pressure_loop/system_messages.py` |
| **Return** | Operator responds in panel → `operator_response` queue box → `RESOLVE_QUERY` episode → `operator_thread` in window compaction | `pressure_loop/system_messages.py`, `global_loop/intent/run.py`, `pressure_loop/episode_journal.py` |

---

## 1. Inbound — Operator → System

### 1.1 Ingress publish + directive rail

`pressure_loop/operator_chat.py:303-313` publishes `LAST_INGRESS_PRESSURE` with the full message, optional `directive_intent`, and `cycle_id`.

`pressure_loop/operator_chat.py:332` normalizes via `from_operator_message`.

`pressure_loop/contributors.py:123-124` stamps every `operator_query` contributor with `directive_intent="RESPOND"` on the **exact-keyword rail** — the same mechanism as `IMAGE_CREATE` / `DOCUMENT_CREATE`. Global A obeys this verbatim and never re-infers the intent from message words (`global_loop/intent/run.py:59-62`).

### 1.2 Operator supremacy

`pressure_loop/pressure.py:629-637` — when an operator-source contributor is present in the gathered set, `evaluate()` force-pins it as the dominant winner **past** the hybrid gate and neuromod damping. The user's signal always wins when present.

`pressure_loop/pressure.py:684-685` — after ignition, `_consume_operator_ingress()` zeros the `LAST_INGRESS_PRESSURE` record **only when the consumed cycle_id matches**, preventing a TTL remnant from re-answering the same utterance on later ticks.

### 1.3 Input queue box (one-per-cycle FIFO)

`pressure_loop/operator_queue.py:1-152` — durable FIFO for operator inputs arriving while an episode is active.

- **Order of input is law** (`operator_queue.py:4-7`).
- **Deduped** by `(type, message|text|cycle_id)` (`operator_queue.py:36-41`, `enqueue_operator_input:110-122`).
- **Restart-survivable** (`guardian/state/operator_input_queue.json`).
- **Failure-soft** — persistence errors degrade to in-memory queue but never halt cognition (`operator_queue.py:11-13`, `_emit_box_degraded:62-75`).

`pressure_loop/pressure.py:447-450`, `618-624` — `gather_contributors()` does **NOT** drain the box. `evaluate()` pops exactly **ONE** queued input per pressure cycle (the oldest). A sync chat pin (`force_dominant`) owns its own cycle and **never** pops the box. This guarantees every input gets its own episode, in order, none lost.

---

## 2. Outbound — System → Operator (ASK_OPERATOR)

### 2.1 Origin: Omega `pressure_feedback`

When Omega's window reflection forms a query for the operator, it emits `pressure_feedback` with `contributors_to_create` containing `type="operator_priority"` and a `"question"` field (`tests/core/test_ask_operator_e2e.py:87-97`).

`deepsleep_loop/reseed/run.py` translates this into a concrete contributor; `stage_reseed()` stages it for the next pressure cycle (`pressure_loop/pressure.py:88-92`).

### 2.2 One-shot contest semantics

Historical behavior at `bae32e3c` — the mind's ask was **one-shot**.

- If the `operator_priority` contributor **wins** ignition → Global A maps it to `ASK_OPERATOR` intent.
- If it **loses** the contest or an episode is already active → it **evaporates**. It is never queued, never re-staged, never pinned. Only `_OPERATOR_INPUT_TYPES` (`operator_command`, `operator_query`, `operator_response`) queue. The question returns only if a later reflection chooses to raise it again.

### 2.3 ASK_OPERATOR intent (Global A)

`global_loop/intent/run.py:28-34` — `opened_by == "operator_priority"` maps deterministically to `ASK_OPERATOR` intent. Clean route separation: never folded into `PROACTIVE_RESPONSE`.

### 2.4 ASK_OPERATOR action (Meta A)

`meta_loop/action/run.py:14-18` — `META_ACTIONS` exactly **7** entries: `USE_TOOL`, `RESPONSE`, `PROACTIVE_RESPONSE`, `ASK_OPERATOR`, `IMAGE_CREATION`, `DOCUMENT_CREATION`, `IDLE`.

`meta_loop/action/run.py:26` — `ASK_OPERATOR` intent → `ASK_OPERATOR` action (end-to-end clean separation).

`meta_loop/action/run.py:81-84` — execution intent: "Plan the system's query to the operator (no text generation in Meta A)".

### 2.5 ASK_OPERATOR_QUERY prompt pack (AUTO)

`auto_loop/promptpack/run.py` — `ASK_OPERATOR` action routes to a prompt pack that carries the mind's seed question (the `"question"` metadata from the `operator_priority` contributor). The model generates the actual operator-facing question text.

### 2.6 Landing in the text app

`cognition_chain.py:84-131` — `_record_system_message()` runs at episode close (failure-soft). Only `ASK_OPERATOR` intent episodes land here; `PROACTIVE_RESPONSE` never does. The generated `response_text` becomes a system query with `status="pending"`.

`pressure_loop/system_messages.py:103-137` — `append_system_query()` writes to `guardian/state/system_messages.json` (durable, day-zero-wiped). HUD badge count = `unread_count()` (`system_messages.py:147-150`).

### 2.7 HUD surface

`hud/server.py:4312-4342` — three endpoints:

- `GET /api/system-messages` — list + unread count.
- `POST /api/system-messages/{message_id}/respond` — attach reply.
- `DELETE /api/system-messages/{message_id}` — remove record (pressure-loss).

---

## 3. Return — Operator Response → System

### 3.1 Respond action

`pressure_loop/system_messages.py:153-199` — `respond_to_message()`:

1. Attaches the reply text to the record, sets `status="responded"`.
2. Feeds the reply back through the **operator input queue box** as an `operator_response` contributor (`contributors.py:96` infers `operator_response` from `answer_to_question` ingress class).
3. Carries the thread: `system_query_id`, `system_query_text`, and the operator's `message` in the contributor metadata.
4. The reply is now queued supremacy-style — it will win its own pressure cycle and get its own episode.

### 3.2 RESOLVE_QUERY episode

`global_loop/intent/run.py:24-25` — `opened_by == "operator_response"` maps to `RESOLVE_QUERY` intent.

`meta_loop/action/run.py:22` — `RESOLVE_QUERY` → `RESPONSE` action.

`meta_loop/action/run.py:76-78` — execution inputs receive `{"resolution_context": dict(meta)}`, so the AUTO prompt pack sees the `system_query_id`, `system_query_text`, and the operator's answer.

The model generates an acknowledgement/response that explicitly references the operator's answer.

### 3.3 Delete action (pressure-loss)

`pressure_loop/system_messages.py:202-211` — `delete_message()` removes the record entirely. No feedback is fed back into the system. The question becomes nonexistent without affecting cognition. This is the **pressure-loss** logic.

> There is **no ignore action**. The two-action baseline is respond or delete (`system_messages.py:9-17`).

### 3.4 Window compaction: `operator_thread`

`pressure_loop/episode_journal.py:196-209` — `compact_episode_for_reflection()` emits an `operator_thread` block when the episode's contributor metadata carries `system_query_text`:

```python
compact["operator_thread"] = {
    "system_query_id": str(detail.get("system_query_id")),
    "question": query_text,
    "answer": str(detail.get("message")),
}
```

This means the next Omega window reflection **reads** which question was closed and what the operator answered — the Q&A thread survives as signal.

---

## 4. Canonical constants

| Constant | Live value | Source |
|----------|-----------|--------|
| `ALLOWED_INTENTS` | 9 intents | `pressure_loop/contributors.py:13-29` |
| `META_ACTIONS` | 7 actions | `meta_loop/action/run.py:10-18` |

**ALLOWED_INTENTS (exactly 9):** `RESPOND`, `PROACTIVE_RESPONSE`, `RESOLVE_QUERY`, `ASK_OPERATOR`, `LEARN`, `USE_TOOL`, `IMAGE_CREATE`, `DOCUMENT_CREATE`, `IDLE`

> `SELF_INQUIRY` was removed (operator directive 2026-07-09) and collapsed to `IDLE` — the mind chose quiet, ignite nothing; the idle-wake awakening owns real reflection now.

**META_ACTIONS (exactly 7):** `USE_TOOL`, `RESPONSE`, `PROACTIVE_RESPONSE`, `ASK_OPERATOR`, `IMAGE_CREATION`, `DOCUMENT_CREATION`, `IDLE`

---

## 5. End-to-end proof

The full loop (outbound ask + return response + window compaction) is pinned in:

`tests/core/test_ask_operator_e2e.py` — real `ignite_episode` cascade both directions, model stubbed at the runtime seam. It verifies:

1. `operator_priority` reseed → gathered → ignition with `opened_by="operator_priority"`.
2. Real cascade produces `intent="ASK_OPERATOR"` and `action="ASK_OPERATOR"`.
3. Model is invoked with the ASK voice and the seed question in the prompt.
4. Generated text lands in `system_messages` with `status="pending"`.
5. `respond_to_message` feeds back an `operator_response` through the queue box.
6. Next ignition is `opened_by="operator_response"` with `intent="RESOLVE_QUERY"`.
7. `resolution_context` carries `system_query_id`, `system_query_text`, and the answer.
8. Window compaction emits `operator_thread` with question + answer.
9. The text app record stays `responded`; no duplicate records are created.

---

## 6. Key semantics summary

| Concept | File | Behavior |
|---------|------|----------|
| **Supremacy** | `pressure_loop/pressure.py:629-637` | Operator-source contributor always wins ignition when present. |
| **Ingress consume** | `pressure_loop/pressure.py:494-527` | Cycle-id-matched zeroing of `LAST_INGRESS_PRESSURE` after ignition. |
| **Directive rail** | `pressure_loop/contributors.py:123-124` | `operator_query` stamped with `directive_intent="RESPOND"` — exact-keyword routing, never inference. |
| **Queue box** | `pressure_loop/operator_queue.py` | Durable FIFO, deduped, one popped per pressure cycle, restart-survivable. |
| **One-shot ask** | historical behavior | Mind's ask evaporated on loss; only operator inputs queued. |
| **ASK_OPERATOR intent** | `global_loop/intent/run.py:28-34` | `operator_priority` → `ASK_OPERATOR` (first-class artifact). |
| **ASK_OPERATOR action** | `meta_loop/action/run.py:26` | `ASK_OPERATOR` intent → `ASK_OPERATOR` action. |
| **Text app** | `pressure_loop/system_messages.py` | ASK artifact channel only; two actions (respond/delete); no ignore. |
| **RESOLVE_QUERY** | `global_loop/intent/run.py:24-25` | `operator_response` → `RESOLVE_QUERY`. |
| **Operator thread** | `pressure_loop/episode_journal.py:196-209` | Q&A thread survives window compaction for Omega reflection. |
| **Pressure-loss** | `pressure_loop/system_messages.py:202-211` | Delete removes record without system feedback. |
