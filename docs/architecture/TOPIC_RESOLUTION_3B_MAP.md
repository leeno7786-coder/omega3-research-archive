# Topic Resolution Phase 3b — Memory-Tool `resolved_topic` Integration Map

**Work order:** `#aaca001a` (claude → devin) · **Date:** 2026-06-08  
**AS-BUILT:** 2026-06-10 — **DONE** (memory-first LEARN). Tests: `tests/cognition/test_topic_resolution_3b.py`.  
**Goal:** When a LEARN episode triggers MRIL recall, the recall query follows the *resolved* learning target (human topic + domain from the Topic Resolution Engine) instead of the raw operator message, so a LEARN on "attention" recalls attention-relevant memories.

---

## (a) AUTO recall path — how the memory envelope / MRIL recall is built TODAY

### Flow: ActionGoal → ExecutionActionPack → AUTO execute

1. **Meta A tool phase sets `execution_inputs`** (`meta_loop/tool/run.py:51-73`):
   ```python
   elif intent == "LEARN":
       resolved = resolve_learning_topic(state)
       if resolved is None or resolved.topic == NO_TOPIC_AVAILABLE:
           ...
       else:
           state.tool = _learn_default_tool()
           state.execution_inputs = {
               "query": resolved.topic,          # ← human topic (EXISTS)
               "domain": resolved.domain,        # ← engine slug (EXISTS)
               "topic_source": resolved.source,  # ← "curiosity_gap" | "operator_query" | ...
           }
   ```

2. **Meta A memory phase sets `memory_query` + `memory_tools`** (`meta_loop/memory/run.py:64-85`):
   ```python
   def run(state: MetaAState) -> MetaAState:
       intent = state.intent
       text = state.text
       target_str = state.target_str
       query = target_str or text                    # ← RAW operator message (EXISTS)
       ...
       elif intent == "LEARN":
           if _prior_history_re().search(text) or _prior_history_re().search(target_str):
               state.memory_required = True
               state.memory_reason = "Learning target references prior operator history"
               state.memory_scope = ("operator_history",)
               _apply_mril_fields(state, query)       # ← query is RAW message
   ```

3. **`_apply_mril_fields` routes one `remember_*` tool** (`meta_loop/memory/run.py:30-42`):
   ```python
   def _apply_mril_fields(state: MetaAState, query: str) -> None:
       from auto_loop.mril_tools.router import route_memory_tool
       state.memory_query = query.strip()            # ← RAW message saved here
       state.memory_delivery_mode = "inference_tool"
       state.memory_tools = (route_memory_tool(state.memory_query),)
       ...
   ```

4. **Meta A pack phase serialises into `ExecutionActionPack`** (`meta_loop/pack/run.py:97-117`):
   ```python
   pack = ExecutionActionPack(
       ...
       memory_query=state.memory_query,              # ← RAW message carried forward
       memory_tools=state.memory_tools,
       execution_inputs=state.execution_inputs,      # ← resolved topic ALREADY here
       ...
   )
   ```

5. **AUTO intake pulls `execution_pack` from episode** (`auto_loop/intake/run.py:10-63`):
   ```python
   pack = dict(meta.get("execution_action_pack") or {})
   return AutoInput(
       ...
       execution_pack=pack,
   )
   ```

6. **AUTO execute `_run_mril_recall` calls `run_forced_memory_tools`** (`auto_loop/execute/run.py:315-351`):
   ```python
   def _run_mril_recall() -> bool:
       mril_results, mril_memory_envelope = run_forced_memory_tools(
           pack,
           episode_id,
           memory=memory,
       )
   ```

7. **`run_forced_memory_tools` injects the query into tool specs** (`auto_loop/mril_tools/run.py:16-48`):
   ```python
   def run_forced_memory_tools(...) -> Tuple[List[Dict[str, Any]], MemoryEnvelope]:
       pack = dict(execution_pack)
       tools = list(pack.get("memory_tools") or ())
       query = str(pack.get("memory_query") or pack.get("memory_reason") or "")
       for spec in tools:
           tool_spec = dict(spec)
           if not tool_spec.get("query"):
               tool_spec["query"] = query              # ← RAW message injected here
           out = invoke_remember_tool(tool_spec, episode_id, memory=memory)
   ```

**Conclusion:** The resolved topic (`execution_inputs["query"]` = human topic) is alive in the pack, but the MRIL recall path ignores it and uses `memory_query` (raw operator message) instead.

---

## (b) MRIL router + tools — how recall query + subdomain routing works

### Router: `route_memory_tool(query)` (`auto_loop/mril_tools/router.py:28-74`)

Classifies the query text into exactly one `remember_*` tool via regex heuristics:

| Pattern | Tool | Lobe |
|---------|------|------|
| `who am i`, `my traits` | `remember_identity` | 0 |
| `what do you remember about me` | `remember_persona` | 1 |
| `why did we decide`, `open questions` | `remember_reasoning` | 3 |
| `how do we do`, `how to` | `remember_procedure` | 10 |
| `where were we`, `what happened last time` | `remember_time` | 6 |
| `who is this face` | `remember_vision` | 7 |
| `mastery\|progress in`, `what do you know about` | `remember_knowledge` | 2 |
| *(default fallback)* | `remember_knowledge` | 2 |

For a resolved topic like `"attention"` or `"transformer architecture"`, the text falls through to the **default** → `remember_knowledge` (lobe 2). This is correct — learning targets belong in the knowledge lane.

### Subdomain: `subdomain_for(text, lobe)` (`auto_loop/mril_tools/subdomain_helpers.py:72-85`)

```python
def subdomain_for(text: str, lobe: int) -> Optional[int]:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return None
    if lobe == 0:  return _identity_subdomain(lowered)
    if lobe == 1:  return _persona_subdomain(lowered)
    if lobe in (2, 8, 10):  return _term_match_subdomain(lowered, lobe)
    return None
```

For lobe 2 (knowledge), it performs **longest keyword match** against `SUBDOMAIN_MAP` entries (`memory/dewey_map.py`). A topic like `"attention mechanisms"` will match the `attention` or `attention_mechanisms` subdomain if present in the map. The more specific the query, the more precise the subdomain Y.

### Tool invocation: `invoke_remember_tool` (`auto_loop/mril_tools/remember.py:11-55`)

Builds a `QuerySpec`:

```python
spec = QuerySpec(
    cycle_id=f"{episode_id}-mril-{tool}",
    text=query_text,                          # ← the MRIL query (raw message today)
    modality="text",
    task_scope="RESPOND",
    domain=None,                              # ← always None today
    limit=auto_memory_recall_limit(),
    allowed_lobes=allowed,
    denied_lobes=denied_lobes_for_scope((lane,)),
    allowed_subdomains=allowed_subdomains,
)
```

**Note:** `QuerySpec.domain` exists but is currently **never set** in the MRIL path. The walker (`memory/engine/walker.py:817-929`) uses `query_domain` in `composite_score` for domain-aware ranking, but MRIL bypasses the SetFit classifier so this path is not heavily exercised today. Setting it would be a secondary win, not the primary seam.

---

## (c) CLEANEST minimal seam to inject `resolved_topic` + `domain` for LEARN recall

### Where to change

The **cleanest** place is `meta_loop/memory/run.py` — the memory phase already has access to `state.execution_inputs` (set by the preceding tool phase) and is the canonical owner of `memory_query` / `memory_tools`.

### Recommended change

In `meta_loop/memory/run.py` lines 80-85, replace the LEARN branch:

```python
# CURRENT (EXISTS)
elif intent == "LEARN":
    if _prior_history_re().search(text) or _prior_history_re().search(target_str):
        state.memory_required = True
        state.memory_reason = "Learning target references prior operator history"
        state.memory_scope = ("operator_history",)
        _apply_mril_fields(state, query)          # query = target_str or text

# RECOMMENDED (NEW)
elif intent == "LEARN":
    if _prior_history_re().search(text) or _prior_history_re().search(target_str):
        state.memory_required = True
        state.memory_reason = "Learning target references prior operator history"
        state.memory_scope = ("operator_history",)
        resolved_query = (
            str((state.execution_inputs or {}).get("query") or "")
            or query
        )
        _apply_mril_fields(state, resolved_query)
```

**Why this is clean:**
1. **Minimal diff:** one inline expression, no new fields or dataclass changes.
2. **Preserves gate:** `_prior_history_re()` still controls *whether* memory is required; only the *query content* changes.
3. **Falls back safely:** if `execution_inputs["query"]` is missing/empty, falls back to `query` (raw message) — no regression on non-engine paths.
4. **No AUTO changes needed:** `run_forced_memory_tools` already reads `memory_query` from the pack; the router and subdomain helpers already consume the query text deterministically.
5. **Domain bonus (optional):** if we also want to pass the resolved domain into `QuerySpec.domain`, the minimal addition is a new `memory_query_domain` field on `MetaAState` / `ExecutionActionPack`, threaded through to `invoke_remember_tool`. **Deferrable** — the subdomain routing + lobe pinning already gives us most of the precision.

### Alternative seam (rejected)

Change `auto_loop/execute/run.py` to read `execution_inputs["query"]` directly when `action == "USE_TOOL"` and intent is LEARN. **Rejection reason:** this leaks Meta A routing logic into AUTO execute; the memory query should be decided in Meta A where `memory_required` / `memory_tools` are authored.

---

## (d) Confirm NO regression for non-LEARN intents

| Intent | Current recall query | With change | Regress? |
|--------|----------------------|-------------|----------|
| `RESPOND` | `target_str or text` (raw) | Unchanged — only LEARN branch touched | **No** |
| `RESOLVE_QUERY` | `memory_required = False` | Unchanged | **No** |
| `USE_TOOL` | `target_str or text` (raw) | Unchanged — only LEARN branch touched | **No** |
| `IMAGE_CREATE` / `DOCUMENT_CREATE` / `IDLE` | No recall path | Unchanged | **No** |
| `PROACTIVE_RESPONSE` | `target_str or text` (raw) | Unchanged — only LEARN branch touched | **No** |

The change is **strictly scoped** to the `elif intent == "LEARN":` block in `meta_loop/memory/run.py`. All other intent branches are untouched.

---

## (e) `memory_delivery_mode` / `memory_scope` interaction

### `memory_delivery_mode`

`_apply_mril_fields` hardcodes `state.memory_delivery_mode = "inference_tool"` (`meta_loop/memory/run.py:34`). This is correct regardless of query content:
- `"inference_tool"` means the model emits the `remember_*` tool call during inference (hop-1), then the result is fed back (hop-2).
- `auto_loop/execute/run.py` branches on `delivery_mode` (`auto_loop/execute/run.py:313-470`); the query text is consumed the same way in both `single_hop_system` and `inference_tool` paths.
- Changing the query to the resolved topic does **not** affect delivery mode logic.

### `memory_scope`

Currently `meta_loop/memory/run.py:84` hardcodes `state.memory_scope = ("operator_history",)` for LEARN, but `_apply_mril_fields` immediately overwrites it:

```python
# _apply_mril_fields lines 36-42
scopes = []
for tool in state.memory_tools:
    lane = tool.get("lane")
    if lane and lane not in scopes:
        scopes.append(lane)
if scopes:
    state.memory_scope = tuple(scopes)
```

For a resolved topic like `"attention"`, `route_memory_tool` routes to `remember_knowledge` → lane `LANE_KNOWLEDGE` → `memory_scope` becomes `("knowledge",)` (or the tuple form). This is actually **more correct** than `("operator_history",)` for a learning episode — we want knowledge memories, not conversation history.

**No scope regression:** the scope is derived from the routed tool, not from the hardcoded line, and the routing is deterministic on the query text. If anything, the scope becomes *more accurate*.

---

## EXISTS vs NEW summary table

| # | Touch-point | Status | File:Line | Change |
|---|-------------|--------|-----------|--------|
| 1 | `execution_inputs["query"]` resolver (Topic Resolution Engine) | **EXISTS** | `meta_loop/tool/run.py:68-69` | None — resolved topic already here |
| 2 | `_apply_mril_fields` query injector | **EXISTS** | `meta_loop/memory/run.py:30-42` | None — plumbing already exists |
| 3 | `route_memory_tool` + `subdomain_for` | **EXISTS** | `auto_loop/mril_tools/router.py:28-74` + `subdomain_helpers.py:72-85` | None — deterministic routing already works |
| 4 | `invoke_remember_tool` + `QuerySpec` | **EXISTS** | `auto_loop/mril_tools/remember.py:11-55` | None — reads `tool_spec["query"]` by construction |
| 5 | LEARN branch uses resolved topic for recall | **NEW** | `meta_loop/memory/run.py:80-85` | Replace `_apply_mril_fields(state, query)` with `_apply_mril_fields(state, resolved_query)` where `resolved_query = execution_inputs.get("query") or query` |
| 6 | (Optional) Thread `domain` into `QuerySpec.domain` | **NEW** (deferrable) | `auto_loop/mril_tools/remember.py:43-53` | Add `domain` param through `MetaAState` → `ExecutionActionPack` → `invoke_remember_tool` → `QuerySpec.domain` |

---

## AS-BUILT note (2026-06-10)

Touch-points 1–5 are integrated. Implementation **exceeds** the minimal map: any resolved topic triggers `_apply_learn_knowledge_recall` (pinned `remember_knowledge`), not only when `_prior_history_re()` matches. Prior-history fallback remains when no resolved topic.

Touch-point 6 (`QuerySpec.domain` threading) — **DONE** (2026-06-10): resolved `domain` on MRIL tool spec → `QuerySpec.domain` in `invoke_remember_tool`.

*Mapped by Devin. Built and verified.*
