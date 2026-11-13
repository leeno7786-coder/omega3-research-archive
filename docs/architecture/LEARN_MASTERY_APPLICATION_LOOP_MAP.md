# Learn → Store → Know-Enough → Apply Loop Map

> **Status:** EXHAUSTIVE READ-ONLY AUDIT (Devin)
> **Date:** 2026-06-14
> **Branch:** main
> **Purpose:** Prove whether the learning loop closes — learn → store → know-enough → apply

---

## Executive Verdict

**THE LOOP DOES NOT CLOSE.** The current implementation has three critical breaks:

1. **Mastery is not grounded in memory content** — it's a free counter incremented by LEARN/USE_TOOL intent, not computed from actual stored knowledge count, coverage, or recall confidence.
2. **No learn-side recall-first gate** — autonomous LEARN does NOT recall existing knowledge on the topic before acquiring; it learns blindly.
3. **No execution-side memory read** — AUTO/execution does NOT read/recall the relevant subdomain knowledge to apply it; application_mastery rises from successful tool usage, not from demonstrating knowledge application.

The loop has a write path (knowledge gets stored) but the read-back paths are missing or incomplete. A mastery number of 1.0 means "10 LEARN episodes occurred" not "Omega has comprehensive coverage of this topic and can apply it."

---

## 1. MASTERY GROUNDING

### Files

- `deepsleep_loop/mastery_store.py` (lines 1-91)
- `deepsleep_loop/mastery/run.py` (lines 1-221)
- `deepsleep_loop/_deepsleep_state.py` (lines 303-330, MasteryRecord dataclass)
- `pressure_loop/policy.py` (lines 1392-1445, mastery config)

### MasteryRecord Fields

```python
@dataclass(frozen=True)
class MasteryRecord:
    domain_id: str           # slugified topic name (via normalize_domain_id)
    domain_name: str         # human-readable topic
    knowledge_mastery: float = 0.0  # FREE COUNTER
    application_mastery: float = 0.0 # FREE COUNTER
    confidence: float = 0.0  # UNUSED in current implementation
    evidence_count: int = 0  # counts successful episodes
    subdomains: dict = field(default_factory=dict)  # UNUSED
    target_mastery: float = 0.70  # default from config
    status: str = "learning"
```

### Current Behavior: FREE COUNTER INCREMENT

**Location:** `deepsleep_loop/mastery/run.py::_apply_mastery_for_episode` (lines 125-170)

```python
def _apply_mastery_for_episode(...) -> MasteryRecord | None:
    domain_id = normalize_domain_id(domain_name)
    prior_store = load_mastery(resolved_path)
    prior = prior_store.get(domain_id)

    knowledge = float(prior.knowledge_mastery) if prior else 0.0
    application = float(prior.application_mastery) if prior else 0.0
    evidence_count = int(prior.evidence_count) if prior else 0
    target_mastery = _target_mastery(action_goal)

    knowledge_inc = deepsleep_mastery_knowledge_increment()  # config value, default 0.10
    application_inc = deepsleep_mastery_application_increment()  # config value, default 0.10

    if intent == "LEARN":
        knowledge = min(1.0, knowledge + knowledge_inc)  # FREE INCREMENT
        evidence_count += 1
    elif intent == "USE_TOOL":
        application = min(1.0, application + application_inc)  # FREE INCREMENT
        evidence_count += 1
    else:
        return None
```

**Key finding:** Mastery is computed as `prior_mastery + fixed_increment`. There is NO:
- Count of actual memory records in the topic's subdomain
- Coverage metric (e.g., % of concepts covered)
- Recall confidence from memory system
- Read of stored knowledge content

### Target Mastery & Thresholds

**Location:** `pressure_loop/policy.py` (lines 1437-1445)

```python
def deepsleep_mastery_default_target(policy: dict[str, Any] | None = None) -> float:
    mastery = _deepsleep_mastery_section(policy)
    raw = mastery.get("default_target", 0.70)
    return max(0.0, min(1.0, float(raw)))  # default 0.70
```

**Severe gap threshold:** `cognition/topic_resolution/resolve_topic.py` line 22
```python
SEVERE_MASTERY_GAP_THRESHOLD = 0.5  # hard-coded default
```

**Status thresholds:** `pressure_loop/policy.py` lines 1531-1545
```python
def deepsleep_mastery_status_thresholds(...) -> dict[str, float]:
    # Default: operational=0.80, learning=0.60
    # Computed from: mastery.get("status_thresholds") or defaults
```

### Where Mastery Is WRITTEN

**Location:** `deepsleep_loop/mastery/run.py::update_mastery` (lines 173-221)

Called during DeepSleep consolidation (Phase 3 of deepsleep loop). Inputs:
- Episode window (list of episode entries)
- Intent extraction from episodes
- Target from action_goal

**Keying:** Mastery records are keyed by `domain_id` (slugified topic name) via `normalize_domain_id` which calls `knowledge_extractor.derive.run._slug`.

**Verdict:** **PARTIAL** — mastery persistence works, but the values are not grounded in actual memory content. They are free counters.

---

## 2. LEARN RECALL-FIRST (Memory-First Learning)

### Files

- `meta_loop/tool/run.py` (lines 1-179)
- `cognition/topic_resolution/resolve_topic.py` (lines 51-80, LEARN path)
- `auto_loop/execute/run.py` (lines 143-185, knowledge extraction)

### Current Behavior: NO RECALL-BEFORE-LEARN

**Location:** `meta_loop/tool/run.py::run` (lines 51-80)

```python
elif intent == "LEARN":
    from cognition.topic_resolution.resolve_topic import (
        NO_TOPIC_AVAILABLE,
        resolve_learning_topic,
    )

    resolved = resolve_learning_topic(state)
    if resolved is None or resolved.topic == NO_TOPIC_AVAILABLE:
        state.tool = None
        state.execution_inputs = {}
        state.execution_intent = "SKIP_NO_TOPIC"
        # ... abort LEARN
    else:
        state.tool = _learn_default_tool()
        state.tool_reason = f"Acquire knowledge on topic: {resolved.topic}"
        state.execution_inputs = {
            "query": resolved.topic,
            "domain": resolved.domain,
            "topic_source": resolved.source,
        }
        # ... proceed to acquire knowledge
```

**Key finding:** There is NO memory recall before the tool is called. The LEARN intent:
1. Resolves a topic (from mastery gap, trajectory, etc.)
2. Directly invokes the learn tool with the topic name
3. Does NOT check what Omega already knows on this topic

### Missing Gate: `_prior_history_re` Style Condition

The work order notes a `_prior_history_re`-style condition was expected. **This gate does not exist in the current code.**

There is no call to:
- `memory.api.read_memory` before tool invocation
- Any recall of the topic's lobe/subdomain to assess existing knowledge
- Any gap computation between existing knowledge and what's being acquired

**Verdict:** **GAP** — autonomous LEARN never recalls existing knowledge before acquiring. It learns blindly, potentially duplicating knowledge.

---

## 3. TOPIC RESOLUTION <-> MASTERY

### Files

- `cognition/topic_resolution/resolve_topic.py` (lines 200-226, _resolve_mastery_gap)
- `deepsleep_loop/mastery_store.py` (lines 47-70, load_mastery)
- `deepsleep_loop/mastery_store.py` (lines 25-29, normalize_domain_id)

### Current Behavior

**Location:** `cognition/topic_resolution/resolve_topic.py::_resolve_mastery_gap` (lines 200-226)

```python
def _resolve_mastery_gap(_state=None) -> ResolvedTopic | None:
    try:
        records = load_mastery()  # loads all mastery records keyed by domain_id
    except Exception:
        return None
    if not records:
        return None

    threshold = _severe_mastery_gap_threshold()  # default 0.5
    best_name = ""
    best_gap = -1.0
    for record in records.values():
        if str(record.status or "").lower() == "mastered":
            continue
        target = float(record.target_mastery)
        knowledge_gap = target - float(record.knowledge_mastery)
        application_gap = target - float(record.application_mastery)
        gap = max(knowledge_gap, application_gap)
        if gap > best_gap:
            best_gap = gap
            best_name = str(record.domain_name or record.domain_id or "").strip()

    if best_gap < threshold:
        return None  # gap not severe enough
    if _is_concrete_topic(best_name):
        return _resolved(best_name, "mastery_gap", confidence=min(1.0, best_gap))
    return None
```

### Mastery Record Keys

**Location:** `deepsleep_loop/mastery_store.py::normalize_domain_id` (lines 25-29)

```python
def normalize_domain_id(target: str) -> str:
    """Normalize a human topic into a stable domain_id key (matches engine _slug)."""
    from knowledge_extractor.derive.run import _slug
    return _slug(str(target or ""))
```

**Keying schema:** `domain_id = _slug(topic_name)` — lowercases, replaces non-alphanumeric with hyphens, strips leading/trailing hyphens.

**Example mapping:**
- "Python async" → "python-async"
- "Machine Learning" → "machine-learning"
- "React hooks" → "react-hooks"

### Gap Math

```python
knowledge_gap = target_mastery - current_knowledge_mastery
application_gap = target_mastery - current_application_mastery
gap = max(knowledge_gap, application_gap)
```

**Threshold:** If `gap < 0.5` (default), the topic is NOT resolved as a learning target.

### Subdomain Name Mismatch

**Critical finding:** Mastery record keys are slugified topic names (e.g., "python-async"). Memory subdomain dirs are:
- Static taxonomy from `memory/dewey_map.py` (e.g., Y=15 for "python" in lobe 8)
- Or dynamically allocated Y values

The mastery `domain_id` is a slug, not a memory subdirectory name. There is NO verification that:
- A mastery record's `domain_id` corresponds to an actual subdomain directory
- The subdomain where knowledge is stored matches the mastery key

**Verdict:** **PARTIAL** — topic resolution reads mastery correctly, but the mastery keys are not grounded in actual subdomain structure. A 0-mastery record with high target DOES resolve as a LEARN topic, but the key may not map to a real subdomain.

---

## 4. KNOWLEDGE STORAGE ON LEARN

### Files

- `auto_loop/execute/run.py` (lines 143-185, _maybe_extract_knowledge)
- `knowledge_extractor/pipeline.py` (extraction pipeline)
- `knowledge_extractor/derive/run.py` (lines 180-211, artifact derivation)
- `knowledge_extractor/store/run.py` (lines 21-35, JSONL buffer write)
- `deepsleep_loop/consolidate/run.py` (lines 51-71, knowledge artifact consume)
- `deepsleep_loop/consolidate_memory/run.py` (lines 101-156, mastery envelopes)
- `memory/api.py` (write_memory entry point)

### Current Behavior

**Step 1: Tool execution** — `auto_loop/execute/run.py::_maybe_extract_knowledge` (lines 143-185)

```python
def _maybe_extract_knowledge(pack, outputs, inputs, order_domain, tool, warnings, *, episode_id=""):
    if str((pack.get("source_action_goal") or {}).get("intent") or "") != "LEARN":
        return
    from pressure_loop.policy import knowledge_learn_integration_wired

    if not knowledge_learn_integration_wired():  # CONFIG GATE
        return
    try:
        from knowledge_extractor import Source, extract

        art = extract(Source(
            kind="tool_output",
            content=str(outputs.get("tool_result") or ""),
            source_ref=str(inputs.get("query") or order_domain or tool),
            meta={"tool": tool, "domain": order_domain},
        ))
        outputs["knowledge_artifact_id"] = art.artifact_id
        outputs["knowledge_domain"] = art.domain
    except Exception as exc:
        # ... failure-soft logging
```

**Step 2: Artifact derivation** — `knowledge_extractor/derive/run.py::run` (lines 180-211)

```python
def run(normalized: dict[str, Any]) -> dict[str, Any]:
    domain = _domain_from_normalized(normalized)  # slugified domain name
    concepts = _salient_concepts(text)
    facts = _declarative_facts(text)
    relationships = _co_occurrence_relationships(text, concepts)
    mastery_tags = _mastery_tags(domain, source_kind)
    confidence = _confidence(facts=facts, concepts=concepts, ...)
    artifact_id = _artifact_id(domain, source_ref, text)

    return {
        "domain": domain,
        "concepts": concepts,
        "facts": facts,
        "relationships": relationships,
        "mastery_tags": mastery_tags,
        "confidence": confidence,
        "artifact_id": artifact_id,
    }
```

**Step 3: Buffer write** — `knowledge_extractor/store/run.py::run` (lines 21-35)

Appends to JSONL buffer at `policy.knowledge_artifact_store_path()` (default: `knowledge_extractor/state/artifacts.jsonl`).

**Step 4: Consolidation consume** — `deepsleep_loop/consolidate/run.py` (lines 51-71)

```python
if deepsleep_knowledge_consume_on_consolidate():  # CONFIG GATE
    try:
        from knowledge_extractor.consume import knowledge_artifact_for_episode

        knowledge_row = knowledge_artifact_for_episode(ds_input.auto_outcome or {})
        if knowledge_row:
            artifacts = dict(artifacts)
            artifacts["knowledge"] = knowledge_row
    except Exception as exc:
        # ... failure-soft
```

**Step 5: Memory write** — `deepsleep_loop/consolidate_memory/run.py::_mastery_envelopes` (lines 101-156)

```python
def _mastery_envelopes(episode_id: str, mastery_effects: dict) -> list[CanonicalEnvelope]:
    envelopes = []
    for _domain_id, record in (mastery_effects or {}).items():
        domain_name = str(record.get("domain_name") or _domain_id)
        # ... mastery progress envelope
        coord = _knowledge_coord(domain_name)  # RESOLVES TO MEMORY COORD
        envelopes.append(CanonicalEnvelope(
            cycle_id=episode_id,
            coord=coord,  # [2, Y, 0] for knowledge lobe
            type="mastery_progress",
            # ... payload
        ))
```

**Critical:** `_knowledge_coord` (lines 16-23) resolves domain name to memory coord:

```python
def _knowledge_coord(domain: str) -> list[int]:
    """Resolve a domain name to a 200_knowledge coord [2, Y, 0]."""
    from memory.dewey_map import SUBDOMAIN_MAP

    rev = {name: y for (x, y), name in SUBDOMAIN_MAP.items() if x == 2}
    key = (domain or "").strip().lower().replace(" ", "_")
    y = rev.get(key, 24)  # DEFAULT Y=24 IF NOT FOUND
    return [2, y, 0]
```

**Key finding:** Knowledge storage uses STATIC `dewey_map.SUBDOMAIN_MAP` to resolve Y-coordinate. This is the fixed taxonomy that the dynamic-subdomain plan (OMEGA_FIRST_WAKE_DYNAMIC_SUBDOMAIN_PLAN.md) aims to retire.

### Storage Location Mismatch

- **Mastery key:** Slugified topic name (e.g., "python-async")
- **Storage subdomain:** Static `SUBDOMAIN_MAP` lookup (e.g., Y=15 for "python")
- **Result:** Knowledge for "python-async" is stored in the generic "python" subdomain (Y=15), not a "python-async" subdomain

**Verdict:** **PARTIAL** — knowledge gets stored, but the storage subdomain is resolved via static taxonomy, not the mastery record's domain_id. The thing learned, the place it's stored, and the thing mastery tracks are NOT the same identifier.

---

## 5. EXECUTION-APPLY READ

### Files

- `auto_loop/recall/run.py` (lines 1-67, memory envelope)
- `auto_loop/execute/run.py` (lines 187-556, execute phase)
- `auto_loop/mril_tools/run.py` (MRIL memory tools)

### Current Behavior: NO EXECUTION-SIDE MEMORY READ

**Location:** `auto_loop/recall/run.py::memory_envelope_before_execute` (lines 10-63)

```python
def memory_envelope_before_execute(execution_pack: Mapping[str, Any]) -> MemoryEnvelope:
    pack = dict(execution_pack)
    required = bool(pack.get("memory_required"))
    reason = pack.get("memory_reason")
    scope = tuple(pack.get("memory_scope") or ())

    if not required:
        envelope = MemoryEnvelope(
            memory_required=False,
            memory_status="skipped",
            # ... no read
        )
    else:
        envelope = MemoryEnvelope(
            memory_required=True,
            memory_status="pending",
            # ... actual read happens in execute phase via MRIL tools
        )
    return envelope
```

**Location:** `auto_loop/execute/run.py::execute` (lines 353-364)

```python
if memory_recall_required(pack) and delivery_mode != "inference_tool":
    if not _run_mril_recall():
        return {
            "failure_kind": "memory_continuity_substrate_invalid",
            # ... fail-hard if recall fails
        }
```

**Key finding:** Memory recall in execution is GATED by `memory_recall_required(pack)`. This is controlled by the execution pack, NOT automatically tied to the topic's mastery or subdomain.

### Application Mastery Rise

**Location:** `deepsleep_loop/mastery/run.py::_apply_mastery_for_episode` (lines 125-170)

```python
elif intent == "USE_TOOL":
    application = min(1.0, application + application_inc)  # FREE INCREMENT
    evidence_count += 1
```

**Key finding:** `application_mastery` rises from successful `USE_TOOL` intent, NOT from:
- Recalling the relevant subdomain knowledge
- Using recalled knowledge in execution
- Demonstrating knowledge application

There is NO execution-side memory read that is:
- Scoped to the topic's subdomain
- Used to inform the tool execution
- Linked to the application_mastery increment

**Verdict:** **GAP** — execution does NOT read the subdomain knowledge to apply it. Application mastery is a free counter incremented by tool usage success, not grounded in demonstrating knowledge recall/application.

---

## 6. RECALL PATHS INVENTORY

### Response-Side Recall (WIRED)

**Location:** `memory/api.py::read_memory` (lines 270-500)

- **Trigger:** Operator queries, RESPOND intent
- **Path:** `memory.api.read_memory` → `recall_compiler.compile_recall_plan` → `recall_lanes.run_recall_lanes` → walker query
- **Scope:** Configured by `QuerySpec` (allowed_lobes, denied_lobes, allowed_subdomains)
- **Status:** ✅ WIRED

### LEARN Recall-First (NOT WIRED)

- **Trigger:** LEARN intent before tool invocation
- **Expected:** Recall existing knowledge on the topic to learn only the gap
- **Actual:** NO recall happens; tool is invoked directly with topic name
- **Status:** ❌ NOT WIRED

### Execution/AUTO Recall (CONDITIONALLY WIRED)

- **Trigger:** USE_TOOL/execution when `memory_recall_required(pack)` is true
- **Path:** `auto_loop.execute.run` → MRIL tools → `memory.api.read_memory`
- **Scope:** Controlled by execution pack's `memory_scope` and `memory_required` flags
- **Status:** ⚠️ CONDITIONALLY WIRED (not automatic per topic/subdomain)

### DeepSleep Consolidation Read (WIRED)

- **Trigger:** DeepSleep consolidation phase
- **Path:** `deepsleep_loop.consolidate_memory.run` → memory writes (no reads)
- **Status:** ⚠️ CONSOLIDATION WRITES ONLY (no read-side)

### Summary

| Path | Trigger | Status | Notes |
|------|---------|--------|-------|
| Response recall | Operator query | ✅ WIRED | Full recall pipeline |
| LEARN recall-first | LEARN intent | ❌ NOT WIRED | Missing gate |
| Execution recall | USE_TOOL (when required) | ⚠️ CONDITIONAL | Not automatic per topic |
| DeepSleep read | Consolidation | ⚠️ WRITE-ONLY | No read in consolidation |

**Verdict:** **GAP** — Only response-side recall is fully wired. The critical learn-side and execution-side reads are missing or conditional.

---

## 7. LOOP CLOSURE VERDICT

### Intended Loop

```
learn → store-in-subdomain → knowledge_mastery rises (grounded in content)
       ↓
   gap closes → move on
       ↓
store → execution recalls → applies → application_mastery rises (grounded in usage)
```

### Actual Loop

```
learn → (NO recall-first) → tool invocation → JSONL buffer write
       ↓
   consolidation → mastery record: knowledge_mastery += 0.10 (FREE COUNTER)
       ↓
   gap closes (target - current) when counter reaches threshold
       ↓
store → (NO execution read) → tool usage → application_mastery += 0.10 (FREE COUNTER)
```

### Break Points

| Break | Location | Issue |
|-------|----------|-------|
| **Mastery not grounded** | `deepsleep_loop/mastery/run.py:146-154` | Mastery is free counter, not computed from memory content |
| **No learn-side recall** | `meta_loop/tool/run.py:51-80` | LEARN does not recall existing knowledge before acquiring |
| **Storage key mismatch** | `deepsleep_loop/consolidate_memory/run.py:16-23` | Storage uses static taxonomy, mastery uses slugified topic name |
| **No execution read** | `auto_loop/execute/run.py:353-364` | Execution does not automatically recall subdomain knowledge |
| **Application mastery disconnected** | `deepsleep_loop/mastery/run.py:152-154` | Application mastery rises from tool success, not knowledge application |

### Evidence from Baseline Omega Reflection

The work order notes: "the baseline Omega reflection self-reported 'knowledge_mastery 1.0 / application_mastery 0.0 — knowledge storage without operationalization'"

This is consistent with the mapped behavior:
- `knowledge_mastery 1.0` = 10 LEARN episodes occurred (0.10 × 10 = 1.0)
- `application_mastery 0.0` = no USE_TOOL episodes on that topic
- The numbers do NOT reflect actual knowledge content or ability to apply it

### Final Verdict

**THE LOOP DOES NOT CLOSE.** The current implementation has write paths but lacks the critical read-back paths that would make mastery numbers meaningful and enable true knowledge application. The system accumulates mastery counters without grounding them in actual memory content or demonstrating knowledge application.

**Required fixes to close the loop:**
1. Ground mastery in actual memory content (count, coverage, recall confidence)
2. Add learn-side recall-first gate to learn only gaps
3. Align storage subdomain with mastery record keys (dynamic-Y allocation)
4. Add execution-side memory read scoped to topic subdomain
5. Ground application_mastery in demonstrated knowledge application
