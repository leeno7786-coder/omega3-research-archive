# AUTO Integration Surface

**Purpose:** Map the integration surface for Slice 3 (AUTO) — the first layer allowed to run models. AUTO executes the Meta A ExecutionActionPack and folds the outcome.

**Scope:** READ-ONLY mapping of existing integration points. No code changes.

> **Historical map.** Generated June 4, 2026 against the retired `brain/*`,
> `core/runtime_capability_broker.py`, and dense `/embed` surfaces. Retained for
> provenance only. Current runtime ownership is `runtime/client.py` and
> `runtime/runtime_capability_broker.py`; current semantic perception is
> `memory/retina.py`.

**See Also:**
- [`README.md`](README.md) — Architecture directory index
- [`AUTO_LOOP_MAP.md`](AUTO_LOOP_MAP.md) — Auto loop implementation
- [`SLICE_3_AUTO_PACKETS.md`](SLICE_3_AUTO_PACKETS.md) — Slice 3 packets
- [`COGNITION_CHAIN_AND_PROMPTS_MAP.md`](COGNITION_CHAIN_AND_PROMPTS_MAP.md) — Cognition chain structure
- [AGENTS.md](../../AGENTS.md) — governing cognition cycle

---

## 1. MODEL RUNTIME/ROUTER

### 1.1 Runtime Client
**Location:** `brain/runtime_client.py` (RuntimeClient class)

**Singleton access:**
```python
from brain.runtime_client import get_runtime_client
rt = get_runtime_client()
```

**Base URL:** `http://127.0.0.1:11434` (configurable via `OMEGA_RUNTIME_URL` env var)

### 1.2 Capability Broker
**Location:** `core/runtime_capability_broker.py`

**RuntimeCapability enum** (file:line 17-35):
- `VOICE` → `/voice/command` → `voice_command`
- `IMAGE` → `/image/generate` → `image_generate`
- `STT` → `/transcribe` → `transcribe`
- `TTS` → `/synthesise` → `synthesise`
- `FACE` → `/face/identify` → `face_identify`
- `DOCUMENT_PARSING` → `/document/extract` → `document_extract`
- `CODING_MODEL` → `/generate` → `generate_completion`
- `SPECIALIST_VISION` → `/vision/describe` → `vision_describe`
- `SPECIALIST_VIDEO` → `/video/describe` → `video_describe` (alternate: SPECIALIST_VISION)
- `EMBED` → `/embed` → `embed`
- `CLASSIFY` → `/classify` → `classify_hierarchical`

**Preflight API** (file:line 371-431):
```python
from core.runtime_capability_broker import preflight_runtime_operation, RuntimeCapability

decision = preflight_runtime_operation(
    RuntimeCapability.CODING_MODEL,
    operation="autonomous_generate",
    cycle_id=cycle_id,
    client=rt,
)
# decision.proceed: bool — whether to call the runtime
# decision.receipt: RuntimeOperationReceipt — audit trail
```

### 1.3 Model Client (Brain Phase Helper)
**Location:** `brain/phases/_model_client.py`

**Text completion API** (file:line 77-246):
```python
from brain.phases._model_client import call_model_kind

text, tokens_in, tokens_out = call_model_kind(
    model_kind="VOICE_BOX",  # Only VOICE_BOX currently routes through runtime
    system_prompt=system,
    user_prompt=user,
    params={
        "max_tokens": 1024,
        "temperature": 0.7,
        "top_p": 0.9,
        "stop": ["\n\n"],
        "chat_template_kwargs": {"enable_thinking": False},
    },
    caller="auto.phase",
    cycle_id=cycle_id,
)
# Returns (text, tokens_in, tokens_out) or (None, 0, 0) on failure
```

**Direct runtime client API** (file:line 437-493):
```python
res = rt.generate_completion(
    prompt=prompt,
    max_tokens=1024,
    temperature=0.7,
    top_p=0.9,
    top_k=40,
    stop=["\n\n"],
    system=system_prompt,
    tools=[...],  # OpenAI tool-calling format
    chat_template_kwargs={"enable_thinking": False},
    enable_thinking=False,  # Default: False (operator directive P3-S32)
    timeout=60,
    cycle_id=cycle_id,
)
# Returns dict: {"text": str, "tokens_in": int, "tokens_out": int, "tool_calls": [...], "finish_reason": str} or None
```

**Image generation API** (file:line 708-738):
```python
image_bytes = rt.image_generate(
    prompt=prompt,
    width=512,
    height=512,
    steps=4,
    seed=None,  # Omit for random seed
)
# Returns bytes or None on failure
```

### 1.4 Profile Mapping
**RuntimeCapability → AUTO profiles:**
- `TEXT_REASONING` → `CODING_MODEL` (via `generate_completion`)
- `TEXT_FAST` → `CODING_MODEL` (via `generate_completion` with lower max_tokens)
- `IMAGE_GENERATION` → `IMAGE` (via `image_generate`)
- `TOOL_EXECUTOR` → `CODING_MODEL` (via `generate_completion` with tools)

**Model identities:** Runtime owns model selection via its registry. Cognitive side asks for capability, not model ID.

---

## 2. TOOL REGISTRY

### 2.1 Tool Storage
**Schema location:** `core/schema/tools/` (JSON ToolSpec files)
- Example: `core/schema/tools/web_fetch.json`
- Implementation: `core/schema/tools/web_fetch.py`

**Lobe storage:** `memory/500_tools/cognitive/` (lobe records for catalog)

### 2.2 Tool Authority (Creation Path)
**Location:** `guardian/tool_authority.py`

**Authorization pipeline** (file:line 11-40):
1. INSPECT — schema validation + policy check + dedupe
2. SANDBOX — mock simulation with timeout
3. DECIDE — pass/fail based on inspect + sandbox
4. WRITE — on PASS: write schema to `core/schema/tools/` + lobe entry via `ToolsGate.ingest`
5. AUDIT — append to `guardian/logs/tool_authority.jsonl`

**API:**
```python
from guardian.tool_authority import authorize_tool
from core.schema.tool_spec import ToolSpec

result = authorize_tool(spec=tool_spec)
# result.approved: bool
# result.schema_path: str (on approval)
# result.lobe_record_id: str (on approval)
```

### 2.3 Tool Catalog (Read Path)
**Location:** `metacognition/tool_catalog.py`

**API** (file:line 14-74):
```python
from metacognition.tool_catalog import read_tool_catalog

tools = read_tool_catalog()
# Returns: List[Dict[str, Any]] with keys: name, title, description
```

### 2.4 Tool Dispatch (Execution Path)
**Location:** `core/tool_dispatch.py`

**Deterministic overlay API** (file:line 64-84):
```python
from core.tool_dispatch import overlay_tool_call_args, deterministic_args_from_order

# Build deterministic args from meta order
deterministic = deterministic_args_from_order(
    tool_name="web_fetch",
    order_domain="python",  # From meta synthesis["domain"]
    meta_bound={"max_results": 3},
)

# Overlay with model-chosen query
final_args = overlay_tool_call_args(
    tool_name="web_fetch",
    deterministic=deterministic,
    model_args={"query": "asyncio tutorial"},  # Model's tool_call arguments
)
```

**Web fetch special handling** (file:line 22-26):
- Model may influence: `query`, `topic`, `search_query`, `domain_query`
- AUTO binds deterministically: `domain` (from meta subdomain), `max_results`

### 2.5 Existing Tools
**Cognitive tools:**
- `web_fetch` — knowledge retrieval from curated sources (file:line 358-503 in `core/schema/tools/web_fetch.py`)
  - API: `web_fetch(query, max_results=3, domain=None, **kwargs) -> dict`
  - Returns: `{"query": str, "results": [...], "credible_sources": [...], "domain": str, "total_results": int}`

**Dev tools** (separate registry, invisible to cognition):
- Location: `core/dev_tool_registry.py`
- API: `invoke_dev_tool(name, args, call_id="") -> dict`
- Audit ledger: `guardian/logs/dev_tool_invocations.jsonl`

**NOTE:** Tool MINTING was removed in the rebuild. AUTO calls EXISTING tools only.

---

## 3. MEMORY SUBSYSTEM

### 3.1 Recall API
**Location:** `memory/api.py`

**Canonical API** (file:line 267-366):
```python
from memory.api import read_memory, QuerySpec

result = read_memory(
    QuerySpec(
        cycle_id="episode-123-auto",
        text="how does asyncio work",
        modality="text",
        task_scope="RESPOND",
        recall_policy_id="default",
        domain="coding",
        limit=10,
        allowed_lobes=[8],  # Coding lobe only (lane discipline)
        denied_lobes=[6, 11],  # Deny journal + dev
    )
)
# Returns: RetrievalResult(cycle_id, candidates: List[RetrievalCandidate], meta: Dict)
```

**QuerySpec fields** (file:line 68-93):
- `cycle_id` (required) — episode/cycle correlation
- `text` — query text
- `modality` — "text" (default)
- `task_scope` — e.g., "RESPOND", "LEARN"
- `recall_policy_id` — posture selector
- `domain` — coarse domain hint
- `limit` — max candidates (default 10)
- `allowed_lobes` — lane discipline: only these lobes
- `denied_lobes` — lane discipline: deny these lobes (takes precedence)

**RetrievalCandidate fields** (file:line 95-135):
- `id` — memory ID
- `coord` — [lobe, subdomain, ...]
- `score` — similarity score
- `provenance` — metadata dict
- `title`, `summary`, `domain`, `subtopic`, `tags`
- `contradiction` — contradiction state
- `flagged`, `flag_reason`, `flag_response_mode` — Guardian immune response
- `via_link` — reached via entity_linker edge
- `history` — revision history (supersedes chain)
- `version` — memory version
- `rerank_score` — final arbitration score

### 3.2 Recall Lanes
**Location:** `memory/recall_lanes.py`

**Lane catalog** (file:line 66-100):
- `identity`, `persona`, `knowledge`, `reasoning`, `situational`
- `tools`, `skills`, `coding`, `temporal`
- `workflows`, `project`, `dev`, `journal`, `vision`
- `embedding`, `strength` (non-lobe)

**Lane → lobe mapping** (file:line 105-120):
- Lobe 0 (identity) → identity
- Lobe 1 (personas) → persona
- Lobe 2 (knowledge) → knowledge
- Lobe 3 (reasoning) → reasoning
- Lobe 4 (situational) → situational
- Lobe 5 (skills) → skills + tools (subdomain 1)
- Lobe 6 (journal) → journal
- Lobe 7 (vision) → vision
- Lobe 8 (coding) → coding
- Lobe 9 (temporal) → temporal
- Lobe 10 (workflows) → workflows + project
- Lobe 11 (dev) → dev

**Lane execution API** (file:line 152-515):
```python
from memory.recall_lanes import run_recall_lanes, LaneHit
from memory.recall_compiler import compile_recall_plan
from memory.recall_arbitrator import arbitrate_evidence

recall_plan = compile_recall_plan(query)
lane_hits = run_recall_lanes(
    recall_plan,
    coord_index_snapshot(),
    load_meta_by_path,
)
# Returns: List[LaneHit(memory_id, lane, score, reason, metadata)]
```

### 3.3 Recall Arbitration
**Location:** `memory/recall_arbitrator.py`

**API:**
```python
from memory.recall_arbitrator import arbitrate_evidence
from memory.evidence_set import EvidenceSet

ev_set = EvidenceSet()
for hit in lane_hits:
    ev_set.add_hit(hit)

winners = arbitrate_evidence(ev_set, recall_plan)
# Returns: List[Winner(memory_id, lanes, lane_scores, reasons, final_score)]
```

### 3.4 Recall Receipt
**Location:** `memory/recall_receipt.py`

**API:**
```python
from memory.recall_receipt import build_recall_receipt

receipt = build_recall_receipt(query, recall_plan, ev_set, winners)
# Returns: Dict with recall metadata for audit trail
```

---

## 4. LEGACY AUTO (Phase 3 Substrate)

### 4.1 Legacy AUTO Entry Point
**Location:** `core/symphony_runner.py` (file:line 2729-2899)

**Function:** `run_symphony_through_deepsleep` (S5 full-chain harness)

**AUTO branch** (file:line 2869-2899):
```python
# Triggered when META_A ExecutionActionPack action is USE_TOOL or IMAGE_CREATION
if str((episode.meta or {}).get("execution_action_pack", {}).get("action", "")) in ("USE_TOOL", "IMAGE_CREATION"):
    auto_result = auto_runner(shared_state)
    episode.auto = dict(auto_result or {})
    episode.state = "auto_running"
```

**MINT_TOOL special case** (file:line 2827-2868):
- When META_A action is MINT_TOOL, AUTO runs `mint_tool_from_curiosity` directly
- Skips normal autonomous loop
- Synthesis model fires in this path (not in Omega phase_4)

### 4.2 Auto Runner Adapter
**Location:** `core/loop_adapters.py` (file:line 298-389)

**Function:** `make_auto_runner()` returns `Callable[[SharedState], Dict[str, Any]]`

**Implementation** (file:line 308-389):
```python
def runner(shared_state: SharedState) -> Dict[str, Any]:
    from autonomous.autonomous_loop.loop import run_autonomous_loop
    from autonomous.autonomous_loop.loop_context import AutoLoopContext

    ctx_in = AutoLoopContext(cycle_id=cycle_id)
    ctx_out = run_audited_loop("auto", run_autonomous_loop, ctx_in)
    autonomous_feedback(ctx_out)

    # Build canonical CycleEnvelope
    # Extract: tool_calls, tool_results, neuromodulator signals
    # Write to LAST_AUTO_CYCLE
    return {...}
```

### 4.3 Autonomous Loop
**Location:** `autonomous/autonomous_loop/loop.py`

**Phases:**
- Phase 1: Read orders (`phase_1_read_orders/run.py`)
- Phase 2: Build prompt (`phase_2_build_prompt/run.py`)
- Phase 2: Resolve schema (`phase_2_resolve_schema/run.py`)
- Phase 3: Inference (`phase_3_inference/run.py`)
- Phase 3.5-3.8: Neuromodulators (dopamine, plasticity, strength, stress)
- Phase 4: Persist (`phase_4_persist/run.py`)

**Inference phase** (file:line 89-281 in `phase_3_inference/run.py`):
- Calls runtime via `rt.generate_completion` with tools
- Executes tool calls via `core/tool_dispatch.py`
- Returns `tool_calls` + `tool_results`

**NOTE:** This is the legacy AUTO that Slice 3 replaces. DELETE TARGET after Slice 3 rebuild.

---

## 5. INTEGRATION TRICKY POINTS

### 5.1 Runtime Capability Preflight
**Issue:** Every runtime operation MUST preflight via `preflight_runtime_operation` before calling the runtime.
**Impact:** AUTO must check `decision.proceed` before calling `rt.generate_completion` or `rt.image_generate`.
**Reference:** `brain/phases/_model_client.py` file:line 136-165 (example implementation)

### 5.2 Tool Dispatch Determinism
**Issue:** Meta binds routing params (domain, max_results); model only chooses query string.
**Impact:** AUTO must use `deterministic_args_from_order` + `overlay_tool_call_args` to merge deterministic routing with model query.
**Reference:** `core/tool_dispatch.py` file:line 87-108

### 5.3 Memory Lane Discipline
**Issue:** Recall must respect `allowed_lobes` and `denied_lobes` to prevent journal/dev leakage.
**Impact:** AUTO must pass these filters from the ExecutionActionPack to `read_memory`.
**Reference:** `memory/api.py` file:line 331-366 (filter implementation)

### 5.4 Runtime Busy Tracking
**Issue:** Pressure tick counters must NOT advance during runtime inference.
**Impact:** Runtime client automatically handles this via `_runtime_busy_scope` context manager.
**Reference:** `brain/runtime_client.py` file:line 91-108

### 5.5 Tool MINTING Removal
**Issue:** Tool MINTING was removed in the rebuild; AUTO should NOT mint new tools.
**Impact:** AUTO only calls EXISTING tools from the catalog. MINT_TOOL path is legacy DELETE TARGET.
**Reference:** `core/symphony_runner.py` file:line 2827-2868 (legacy mint path)

---

## 6. PROFILE MAPPING SUMMARY

| AUTO Profile | RuntimeCapability | Runtime Method | Notes |
|-------------|-------------------|----------------|-------|
| TEXT_REASONING | CODING_MODEL | `rt.generate_completion` | High max_tokens, enable_thinking=False |
| TEXT_FAST | CODING_MODEL | `rt.generate_completion` | Low max_tokens, enable_thinking=False |
| IMAGE_GENERATION | IMAGE | `rt.image_generate` | Returns bytes |
| TOOL_EXECUTOR | CODING_MODEL | `rt.generate_completion` | With tools param |

---

## 7. FILE:LINE ANCHOR INDEX

| Component | File | Key Lines |
|-----------|------|-----------|
| Runtime client | `brain/runtime_client.py` | 168-818 |
| Capability broker | `core/runtime_capability_broker.py` | 17-477 |
| Model client | `brain/phases/_model_client.py` | 77-246 |
| Tool authority | `guardian/tool_authority.py` | 11-490 |
| Tool catalog | `metacognition/tool_catalog.py` | 14-74 |
| Tool dispatch | `core/tool_dispatch.py` | 22-108 |
| Web fetch tool | `core/schema/tools/web_fetch.py` | 358-503 |
| Memory API | `memory/api.py` | 267-366 |
| Recall lanes | `memory/recall_lanes.py` | 66-515 |
| Legacy AUTO entry | `core/symphony_runner.py` | 2729-2899 |
| Auto runner adapter | `core/loop_adapters.py` | 298-389 |
| Autonomous loop | `autonomous/autonomous_loop/loop.py` | 22-104 |

---

**Generated:** 2026-06-04
**Work order:** `#bde701b8`
**Agent:** devin
**Mode:** READ-ONLY mapping
