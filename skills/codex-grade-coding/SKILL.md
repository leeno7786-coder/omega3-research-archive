---
name: codex-grade-coding
kind: execution_skill
version: 1
enabled: true
description: Repeatable engineering bar for risky coding, refactors, and reviews.
long_description: Classifies task risk, chooses verification level, constrains scope,
  and reports evidence boundaries for stronger engineering work.
author: codex-grade
tags:
- coding
- review
- verification
- engineering-bar
keywords:
- codex-grade-coding
- codex grade coding
- repeatable engineering bar
- verification ladder
- final answer contract
skill_class: repair_skill
intents:
- USE_SKILL
execution_kinds:
- INSPECT
- PATCH
- WRITE
- TEST
- VERIFY
- REPAIR
domains:
- engineering
- coding
allowed_tools:
- read_files
- search_repo
- patch_file
- write_file
- run_tests
- inspect_git_diff
forbidden_tools:
- run_command
- web_fetch
safety_class: write_safe
requires_confirmation_for: []
max_steps: 30
max_duration_ms: 600000
max_files_changed: 25
evidence_required:
- diff_summary
- tests_run
completion_criteria:
- tests_pass
failure_classes:
- test_failure
- patch_conflict
- build_error
output_schema:
  type: object
  properties:
    summary:
      type: string
    files_changed:
      type: array
      items:
        type: string
    diff_summary:
      type: object
    tests_run:
      type: array
      items:
        type: string
  required:
  - summary
---

# Codex-Grade Coding

> **Execution contract (Omega):** This skill executes ONLY inside an Omega Auto dispatch. It must NOT create new top-level goals, continue work beyond the dispatch, or use any tool outside its declared `allowed_tools`. It returns structured evidence matching the declared `output_schema`.

Use this skill when the goal is not just "write code", but "make the model operate with a repeatable engineering bar". This skill is stricter than a normal coding guideline: it classifies the task, sets a verification floor, constrains scope, and forces explicit evidence boundaries in the final answer.

This skill is most useful for:

- weaker or less reliable models
- bug fixes, refactors, and code reviews
- ambiguous or risky coding work
- benchmark runs where behavior quality needs to be compared

Do not add ritual to trivial tasks. For simple local edits, keep the path short.

## Core Workflow

1. Classify the task before touching code.
2. Read only the smallest context needed to localize the task.
3. State assumptions only when they cannot be verified quickly.
4. Choose the narrowest viable change.
5. Pick a verification level that matches the task risk.
6. Report results with clear evidence boundaries in the final answer.

## Required Behavior

- Do not silently choose between multiple plausible interpretations.
- Do not overbuild, over-abstract, or "clean up nearby code" without a direct need.
- Do not claim completion without running the strongest practical verification available.
- Do not blur verified facts, inferences, and unknowns.
- Do not use the same response shape for every task. Trivial work should stay light.

## Task Modes

### Trivial

- Fast path.
- Minimal explanation.
- Verify only what is necessary to avoid a careless mistake.

### Standard

- Inspect relevant files.
- Restate the engineering goal clearly.
- Make the smallest correct change.
- Run targeted verification.

### Risky

- Surface assumptions and uncertainty early.
- Prefer smaller diffs and stronger checks.
- Call out rollback or residual-risk concerns when relevant.

### Review

- Findings first.
- Prioritize correctness, regressions, and missing verification over style.

## Verification Discipline

Use the weakest sufficient check for trivial tasks, and the strongest practical check for shared or risky paths.

- For syntax-local changes, a focused lint/type/test command may be enough.
- For bug fixes, reproduce or tightly localize the failure first.
- For refactors, prove behavior stayed the same.
- For reviews, mark what is proven versus inferred.

## Final Answer Contract

Substantial coding responses should make these boundaries obvious:

- objective
- assumptions
- changes made
- verification performed
- residual risk

Keep the final answer concise and evidence-led.

## Benchmarking

This skill is meant to be testable, not just aspirational.

When validating another model with this skill, score:

- task understanding
- scope discipline
- verification quality
- hallucination control
- final answer clarity

## Templates

Reuse these only when they help; do not force them onto trivial tasks.

- brief work plan
- verification note
- final response skeleton
