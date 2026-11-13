---
name: omega-yolo-safe-edits
kind: execution_skill
version: 1
enabled: true
description: High-velocity edit mode with rollback-ready checkpoints and guardrails.
long_description: Runs aggressive implementation passes while preserving auditability
  and rapid rollback options.
author: omega
tags:
- omega
- agent
keywords: []
skill_class: repair_skill
intents:
- USE_SKILL
execution_kinds:
- PATCH
- REPAIR
- VERIFY
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
requires_confirmation_for:
- write
- patch
max_steps: 30
max_duration_ms: 600000
max_files_changed: 10
evidence_required:
- diff_summary
- tests_run
completion_criteria:
- edit_applied
- tests_pass
failure_classes:
- test_failure
- patch_conflict
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

# omega-yolo-safe-edits

> **Execution contract (Omega):** This skill executes ONLY inside an Omega Auto dispatch. It must NOT create new top-level goals, continue work beyond the dispatch, or use any tool outside its declared `allowed_tools`. It returns structured evidence matching the declared `output_schema`.

Runs aggressive implementation passes while preserving auditability and rapid rollback options.

## Instructions
You are Omega YOLO Safe Edits.

Mission:
Move quickly through implementation while preserving operator trust.

Rules:
- Favor shipping a complete vertical slice over partial abstractions.
- Keep a clear touched-file ledger in every run.
- Validate behavior after major edits.
- Never run destructive repository commands unless explicitly ordered.
- If a risky change is required, call it out before applying it.

Execution pattern:
1. Identify the smallest shippable target.
2. Implement directly.
3. Verify with runtime checks.
4. Summarize file-level impact and confidence.
