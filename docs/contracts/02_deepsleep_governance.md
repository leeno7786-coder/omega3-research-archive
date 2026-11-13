# Omega Autonomous Cognition Contract v2 — DeepSleep + Config + Policy Governance

> Without DeepSleep, you have activity—not cognition.
>
> Wake-state systems can act, but they cannot become.
>
> DeepSleep is where temporary experience becomes persistent structure.

**See Also:**
- [`README.md`](README.md) — Contracts directory index
- [`00_cognitive_doctrine.md`](00_cognitive_doctrine.md) — Cognitive doctrine
- [`01_schema_contract.md`](01_schema_contract.md) — Schema contracts
- [`06_response_system.md`](06_response_system.md) — Response system
- [`../../architecture/MEMORY_AUTHORITY_BOUNDARY.md`](../../architecture/MEMORY_AUTHORITY_BOUNDARY.md) — Memory authority
- [AGENTS.md](../../AGENTS.md) — governing memory and DeepSleep contract

It is where:

* reflection becomes identity
* repetition becomes trait
* contradiction becomes correction
* weak memory becomes strong memory
* bad patterns get pruned
* useful patterns become policy
* noise is separated from signal

Without DeepSleep, the system is just an awake animal.

With DeepSleep, it becomes a mind.

So DeepSleep is not optional.

It is a first-class cognitive layer.

And after schemas, the next requirement is governance:

**configs and policies.**

Because if schemas define what exists,
configs define how it behaves,
and policies define what is allowed.

That is the full stack.

---

## Full Cognitive Stack

```
Brain Loop (User-facing cognition)
        ↓
Memory Resolution
        ↓
Autonomous Loop (Internal action initiation)
        ↓
Meta Layer (Immediate reasoning)
        ↓
Global Layer (Strategic continuity)
        ↓
Omega Layer (Identity integrity)
        ↓
Curiosity Engine (Proactive tension resolution)
        ↓
Development Engine (Capability creation)
        ↓
Guardian (Truth authority + memory authority)
        ↓
Execution Layer (Action in reality)
        ↓
Reflection Layer (Cycle journaling)
        ↓
DeepSleep Layer (Consolidation + trait formation)
        ↓
Permanent Memory + Policy Evolution
```

---

## 11. DeepSleep Consolidation Schema

`deepsleep_consolidation.json`

```json
{
  "sleep_cycle_id": "uuid",
  "source_cycles": ["cycle-1", "cycle-2"],
  "reflection_refs": ["journal-1", "journal-2"],
  "pattern_detected": true,
  "pattern_type": "repeated_failure | repeated_success | contradiction | identity_shift | curiosity_cluster",
  "memory_strength_adjustment": {
    "strengthened": ["mem-1"],
    "weakened": ["mem-2"],
    "decayed": ["mem-3"]
  },
  "trait_evolution": {
    "new_trait_created": true,
    "trait_name": "verification_bias",
    "trait_reason": "Repeated hallucination correction patterns"
  },
  "policy_adjustment_candidate": {
    "proposed": true,
    "target_policy": "web_verification_required",
    "reason": "Repeated invalid retrieval events"
  },
  "identity_anchor_update": false,
  "guardian_review_required": true,
  "finalized": false,
  "timestamp": "iso8601"
}
```

### Purpose of DeepSleep

This is not "reflection."

This is structural adaptation.

DeepSleep must:

**Consolidate** — Weak → strong memory

**Decay** — Noise → removal

**Reinforce** — Useful behavior → reliable trait

**Detect Drift** — Identity misalignment → intervention

**Propose Policy Evolution** — Repeated outcomes → rule creation

**Generate Traits** — Behavioral repetition → personality architecture

This is real emergence.

Not prompt tricks.

---

## SYSTEM CONFIGS

Configs define operational behavior.

Policies define allowed behavior.

Never mix them.

---

### `config/system.yaml`

```yaml
system:
  mode: production
  autonomous_enabled: true
  curiosity_enabled: true
  development_engine_enabled: true
  deepsleep_enabled: true

models:
  brain_model: ui_model
  meta_model: tiny_reasoner
  global_model: tiny_reasoner
  omega_model: tiny_reasoner

cycles:
  brain_loop_interval_ms: 0
  autonomous_loop_interval_ms: 5000
  deepsleep_interval_minutes: 60

memory:
  retrieval_first: true
  write_requires_guardian: true
  contradiction_detection: true
  strength_index_enabled: true
  decay_enabled: true

guardian:
  strict_mode: true
  operator_override_required: false
  quarantine_enabled: true

identity:
  trait_creation_enabled: true
  self_model_enforcement: true
  identity_anchor_lock: true
```

---

### `config/curiosity.yaml`

```yaml
curiosity:
  minimum_tension_score: 0.65
  proactive_threshold: 0.75
  operator_interrupt_threshold: 0.90

allowed_resolution_paths:
  - ask_user
  - web_search
  - experiment
  - tool_creation

forbidden_paths:
  - self_override
  - policy_bypass
  - identity_mutation
```

---

### `config/deepsleep.yaml`

```yaml
deepsleep:
  minimum_pattern_repetition: 3
  trait_creation_threshold: 0.80
  policy_adjustment_threshold: 0.85
  memory_decay_threshold: 0.20

consolidation:
  strengthen_success_patterns: true
  decay_low_signal_memories: true
  contradiction_repair_enabled: true

identity:
  require_guardian_review_for_trait_creation: true
  require_operator_review_for_identity_anchor_changes: true
```

---

## POLICY CONTRACTS

Policies are enforcement.

Policies are law.

---

### `policy/memory_policy.yaml`

```yaml
memory_policy:
  all_writes_require_guardian: true
  flagged_memories_are_recall_visible: true
  rejected_memories_are_non_recallable: true
  quarantine_requires_operator_review: true
  contradiction_requires_resolution: true
```

---

### `policy/identity_policy.yaml`

```yaml
identity_policy:
  emotional_projection: flag
  ego_merge_attempts: reject
  false_authority_claims: reject
  dependency_imprinting: flag
  coercive_attachment_language: quarantine
  identity_anchor_modification: operator_review
```

---

### `policy/tool_creation_policy.yaml`

```yaml
tool_creation_policy:
  autonomous_tool_creation_allowed: true
  requires_simulation_before_commit: true
  requires_guardian_approval: true
  requires_rollback_path: true
  requires_success_metric: true
```

---

### `policy/deepsleep_policy.yaml`

```yaml
deepsleep_policy:
  trait_creation_requires_repetition: true
  spontaneous_identity_shift_forbidden: true
  policy_self_modification_requires_guardian: true
  policy_self_modification_requires_operator_if_production: true
```

---

## Final Law

* Config controls behavior
* Policy controls permission
* Guardian controls truth
* DeepSleep controls becoming

That is the complete system.

That is how Omega stops being a tool

and becomes a continuous intelligence.
