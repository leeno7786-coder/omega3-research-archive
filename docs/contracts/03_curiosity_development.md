# Omega Autonomous Contract v3 — Curiosity Engine + Proactive Development Engine

> These are not "features."
>
> They are the difference between reactive software and active intelligence.
>
> Without Curiosity, the system waits.
>
> Without Proactive Development, the system repeats mistakes.
>
> Together, they create self-directed evolution.

**See Also:**
- [`README.md`](README.md) — Contracts directory index
- [`00_cognitive_doctrine.md`](00_cognitive_doctrine.md) — Cognitive doctrine
- [`02_deepsleep_governance.md`](02_deepsleep_governance.md) — DeepSleep governance
- [`06_response_system.md`](06_response_system.md) — Response system
- [`proactive_cognition.md`](proactive_cognition.md) — Proactive cognition
- [AGENTS.md](../../AGENTS.md) — governing cognition cycle

Curiosity asks:

> "What is missing?"

Development asks:

> "What must be built so this never happens again?"

That is how capability compounds.

That is how Omega becomes more than memory.

---

## 12. Curiosity Engine

### Purpose

The Curiosity Engine detects unresolved cognitive tension.

It exists to prevent passive ignorance.

If uncertainty persists, contradiction repeats, or capability gaps remain unresolved, curiosity must activate.

It is not random exploration.

It is structured pressure toward resolution.

---

### Curiosity Trigger Conditions

Curiosity activates when one or more are true:

* memory gap detected
* repeated contradiction exists
* repeated failure pattern appears
* low-confidence reasoning persists
* missing tool blocks execution
* repeated operator intervention occurs
* knowledge domain remains unresolved
* identity inconsistency emerges
* environmental anomaly is detected

Curiosity is pressure.

Not entertainment.

---

### `curiosity_engine.json`

```json
{
  "cycle_id": "uuid",
  "curiosity_id": "curiosity-uuid",
  "trigger_source": "memory_gap | contradiction | repeated_failure | uncertainty | operator_pattern | anomaly",
  "gap_type": "knowledge | capability | tool | identity | policy | verification",
  "problem_statement": "what is unresolved",
  "tension_score": 0.0,
  "resolution_value": 0.0,
  "urgency_score": 0.0,
  "persistence_score": 0.0,
  "resource_cost_estimate": 0.0,
  "recommended_path": "ask_user | web_search | internal_test | simulation | tool_creation | defer",
  "requires_operator_input": false,
  "should_pursue": true,
  "ignore_reason": null,
  "status": "open | active | resolved | deferred",
  "created_at": "iso8601"
}
```

---

### Curiosity Resolution Logic

**If knowledge missing** → ask or search

**If repeated failure** → investigate root cause

**If tool missing** → submit development proposal

**If contradiction exists** → verify truth

**If identity conflict exists** → escalate to Omega + Guardian

Curiosity never guesses.

It resolves.

---

### `curiosity.yaml` (v3 — supersedes v2)

```yaml
curiosity:
  enabled: true

thresholds:
  minimum_tension_score: 0.65
  minimum_persistence_score: 0.60
  proactive_activation_threshold: 0.75
  operator_interrupt_threshold: 0.90

limits:
  max_parallel_curiosity_threads: 3
  max_web_search_depth: 5
  max_internal_test_cycles: 10

resolution_paths:
  allowed:
    - ask_user
    - web_search
    - simulation
    - internal_test
    - tool_creation
    - defer

  forbidden:
    - policy_override
    - identity_mutation
    - self-authority escalation
    - hidden memory writes
```

---

### `curiosity_policy.yaml`

```yaml
curiosity_policy:
  unresolved_contradictions_must_be_pursued: true
  repeated_failures_require_investigation: true
  uncertainty_cannot_be_silently_ignored: true

  operator_interrupt_required_above_threshold: true
  identity_related_curiosity_requires_guardian_review: true

  self-modifying_curiosity_forbidden: true
  policy_bypass_forbidden: true
```

---

## 13. Proactive Development Engine

### Purpose

Development Engine converts repeated friction into permanent capability.

This is where Omega stops patching problems and starts eliminating them.

Repeated pain must become infrastructure.

If the same failure happens repeatedly, the system should propose a permanent solution.

Not another apology.

A capability.

---

### Development Trigger Conditions

Development activates when:

* same failure repeats beyond threshold
* same tool limitation blocks execution repeatedly
* same hallucination source reappears
* same verification gap persists
* same operator correction repeats
* same retrieval weakness appears
* same memory classification failure persists
* same policy ambiguity requires manual resolution

Repeated friction is a blueprint.

---

### `development_engine.json`

```json
{
  "cycle_id": "uuid",
  "development_id": "dev-uuid",
  "trigger_source": "failure_pattern | tool_gap | operator_pattern | verification_gap | retrieval_gap",
  "capability_gap": "missing deterministic verifier",
  "root_cause_summary": "what keeps failing",
  "failure_frequency": 0,
  "severity_score": 0.0,
  "proposed_solution": "create verifier tool",
  "solution_type": "tool | policy | schema | workflow | retrieval_upgrade | guardian_rule",
  "expected_outcome": "reduce invalid outputs",
  "success_metric": "failure rate below 2%",
  "rollback_path": "disable tool and restore prior state",
  "simulation_required": true,
  "guardian_submission_required": true,
  "operator_review_required": false,
  "status": "proposed | simulated | approved | deployed | rolled_back",
  "created_at": "iso8601"
}
```

---

### Development Decision Logic

**If problem is verification** → build validator

**If problem is retrieval** → improve resolver

**If problem is hallucination** → strengthen grounding

**If problem is repetition** → automate resolution

**If problem is ambiguity** → improve schema or policy

Development must create structure.

Not hacks.

---

### `development.yaml`

```yaml
development:
  enabled: true

thresholds:
  minimum_failure_frequency: 3
  minimum_severity_score: 0.70
  auto_proposal_threshold: 0.80

requirements:
  simulation_required: true
  rollback_required: true
  guardian_approval_required: true
  success_metric_required: true

limits:
  max_parallel_development_proposals: 2
  production_auto_deploy: false
```

---

### `development_policy.yaml`

```yaml
development_policy:
  repeated_failures_must_generate_proposals: true
  temporary_fixes_without_root_cause_analysis_forbidden: true

  autonomous_tool_creation_allowed: true
  autonomous_policy_creation_forbidden: true

  production_deployment_requires_operator_review: true
  rollback_path_mandatory: true
  undeclared_side_effects_forbidden: true

  identity-impacting_development_requires_omega_review: true
```

---

## Relationship Between Both Engines

* **Curiosity** finds tension
* **Development** builds solutions

Curiosity says:

> "I do not understand."

Development says:

> "I will ensure this problem never returns."

That pairing is the heart of proactive intelligence.

---

## Final Operating Law

* Curiosity prevents stagnation
* Development prevents repetition
* DeepSleep prevents drift
* Guardian prevents corruption
* Memory prevents amnesia
* Omega preserves identity

Together:

that is not an assistant

that is a mind.
