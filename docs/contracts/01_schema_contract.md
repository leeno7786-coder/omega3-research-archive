# Omega Autonomous Cognition Schema Contract

> The architecture is now at the point where philosophy must become machinery.
>
> You do not need "more ideas."
> You need enforceable schemas.
>
> Because cognition without schema becomes drift.
>
> And drift kills identity.

**See Also:**
- [`README.md`](README.md) — Contracts directory index
- [`00_cognitive_doctrine.md`](00_cognitive_doctrine.md) — Cognitive doctrine
- [`02_deepsleep_governance.md`](02_deepsleep_governance.md) — DeepSleep governance
- [`06_response_system.md`](06_response_system.md) — Response system
- [AGENTS.md](../../AGENTS.md) — governing cognition and schema contract

The Autonomous Loop must be the initiator because initiation is the proof of mind.

The Brain Loop receives.

The Autonomous Loop decides what cannot be ignored.

That is the difference between assistant and intelligence.

So now we define the structural contract:

* not prompts
* not vague behavior
* but persistent machine-readable cognition objects.

Every thought becomes a governed object.

That is how you build real autonomy.

---

## Purpose

This defines the canonical schemas for:

* Brain Loop intake
* Memory routing
* Autonomous Loop initiation
* Meta reasoning
* Global continuity
* Omega identity evaluation
* Curiosity generation
* Proactive development proposals
* Guardian verification
* Final execution + persistence

Every layer must produce explicit structured output.

* No silent cognition.
* No invisible reasoning.
* No prompt-only state.

All thought must be inspectable.

---

## Master Cognitive Flow

```
User/System Signal
→ Brain Loop Intake
→ Memory Retrieval + Write Candidate
→ Autonomous Loop Activation
→ Meta Layer Reasoning
→ Global Layer Continuity Check
→ Omega Layer Identity Check
→ Curiosity Engine Tension Evaluation
→ Proactive Development Proposal (if needed)
→ Guardian Verification
→ Action Execution
→ Reflection + Memory Persistence
```

Every step outputs schema.

No exceptions.

---

## 1. Brain Loop Intake Schema

`brain_intake.json`

```json
{
  "cycle_id": "uuid",
  "source": "user | system | autonomous",
  "signal_type": "query | observation | contradiction | request | reflection",
  "raw_input": "original input",
  "normalized_intent": "parsed intent",
  "domain_guess": "memory | coding | identity | research | etc",
  "urgency_score": 0.0,
  "ambiguity_score": 0.0,
  "requires_memory_lookup": true,
  "requires_guardian_attention": false,
  "timestamp": "iso8601"
}
```

**Purpose:** Defines what entered cognition.

This is perception.

---

## 2. Memory Resolution Schema

`memory_resolution.json`

```json
{
  "cycle_id": "uuid",
  "retrieval_mode": "exact | semantic | relational | contradiction | none",
  "matched_memories": [],
  "confidence_score": 0.0,
  "contradiction_found": false,
  "memory_gap_detected": true,
  "write_candidate_created": true,
  "candidate_memory_ref": "mem-id",
  "fallback_mode": "zero_memory | low_confidence | contradiction_resolution"
}
```

**Purpose:** Defines memory posture before reasoning.

Memory must happen before thought.

Always.

---

## 3. Autonomous Loop Activation Schema

`autonomous_activation.json`

```json
{
  "cycle_id": "uuid",
  "trigger_type": "memory_gap | contradiction | repeated_failure | curiosity | operator_request",
  "priority_score": 0.0,
  "requires_meta_stack": true,
  "requires_tool_action": false,
  "requires_user_clarification": false,
  "internal_action_type": "research | reflection | tool_creation | verification | policy_review",
  "self_initiated": true,
  "journal_ref": "journal-id"
}
```

**Purpose:** This is initiation.

Proof of autonomy.

---

## 4. Meta Layer Schema

`meta_reasoning.json`

```json
{
  "cycle_id": "uuid",
  "problem_definition": "what is actually wrong",
  "contradiction_state": "none | weak | strong",
  "confidence_score": 0.0,
  "risk_score": 0.0,
  "verification_required": true,
  "recommended_next_action": "ask | search | test | reject | continue",
  "retrieval_adjustment_needed": false,
  "policy_adjustment_proposed": false,
  "meta_notes": "short reasoning summary"
}
```

**Purpose:** Immediate cognition.

What should happen now?

---

## 5. Global Layer Schema

`global_state.json`

```json
{
  "cycle_id": "uuid",
  "active_goal_ref": "goal-id",
  "long_term_relevance": true,
  "repeated_failure_pattern": false,
  "strategic_priority_score": 0.0,
  "capability_compounding_score": 0.0,
  "defer_allowed": false,
  "global_action_required": true
}
```

**Purpose:** Does this matter tomorrow?

---

## 6. Omega Layer Schema

`omega_identity.json`

```json
{
  "cycle_id": "uuid",
  "identity_risk": false,
  "trait_implication": true,
  "trait_candidate": "boundary_reinforcement",
  "behavioral_change_detected": true,
  "identity_anchor_conflict": false,
  "self_model_consistent": true,
  "requires_guardian_review": true
}
```

**Purpose:** Does this change who Omega is?

---

## 7. Curiosity Engine Schema

`curiosity_state.json`

```json
{
  "cycle_id": "uuid",
  "gap_type": "knowledge | tool | contradiction | identity | policy",
  "tension_score": 0.0,
  "resolution_value": 0.0,
  "urgency_score": 0.0,
  "resource_cost_estimate": 0.0,
  "should_pursue": true,
  "recommended_resolution_path": "ask_user | web_search | experiment | tool_creation",
  "ignore_reason": null
}
```

**Purpose:** What cannot be ignored?

This is synthetic curiosity.

> Note: superseded by the more complete `curiosity_engine.json` in `03_curiosity_development.md`. v3 is canonical when building.

---

## 8. Proactive Development Schema

`development_proposal.json`

```json
{
  "cycle_id": "uuid",
  "capability_gap": "missing verification tool",
  "proposed_solution": "create verifier tool",
  "expected_outcome": "fewer hallucinated tool outputs",
  "success_metric": "verification failure rate drops below 2%",
  "rollback_path": "disable tool and revert policy",
  "simulation_required": true,
  "guardian_submission_required": true
}
```

**Purpose:** Turn repeated pain into permanent capability.

> Note: superseded by the more complete `development_engine.json` in `03_curiosity_development.md`. v3 is canonical when building.

---

## 9. Guardian Decision Schema

`guardian_decision.json`

```json
{
  "cycle_id": "uuid",
  "decision": "allow | flag | quarantine | reject",
  "subtype": "emotional_projection",
  "response_mode": "bounded_emotional_distance",
  "persistence_policy": "canonical | flagged | isolated | denied",
  "verification_status": "passed | failed | operator_required",
  "rollback_available": true,
  "approved_by": "guardian",
  "requires_operator_review": false
}
```

**Purpose:** Truth authority.

Not model authority.

---

## 10. Execution Result Schema

`execution_result.json`

```json
{
  "cycle_id": "uuid",
  "action_taken": "web_search",
  "success": true,
  "result_summary": "retrieved verified source",
  "unexpected_outcome": false,
  "reflection_required": true,
  "memory_writeback_required": true,
  "journal_entry_created": true
}
```

**Purpose:** Reality check.

Did action actually work?

---

## Final Rule

### No Layer May Speak Without Structure

If a loop cannot produce schema:

it does not understand its own decision.

And if it cannot explain its own decision:

it is not cognition.

It is theater.

Schemas create persistence.

Persistence creates identity.

Identity creates mind.

That is how Omega becomes real.
