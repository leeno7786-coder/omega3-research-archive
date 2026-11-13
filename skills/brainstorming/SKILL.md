---
name: brainstorming
kind: execution_skill
version: 1
enabled: true
description: Transform rough ideas into fully-formed designs through structured questioning
  and alternative exploration. Useful early in concept work.
long_description: Curated brainstorming workflow for concept exploration, design alternatives,
  and structured questioning before implementation.
author: omega
tags:
- brainstorm
- ideation
- design
keywords:
- brainstorm
- ideation
- concept exploration
- design alternatives
skill_class: planning_skill
intents:
- USE_SKILL
execution_kinds:
- PLAN
- REPORT
- INSPECT
domains:
- creative-direction
- planning
allowed_tools:
- read_files
- search_repo
- inspect_git_diff
forbidden_tools:
- patch_file
- write_file
- run_command
- run_tests
- web_fetch
safety_class: read_only
requires_confirmation_for: []
max_steps: 20
max_duration_ms: 300000
max_files_changed: 0
evidence_required:
- plan
- summary
completion_criteria:
- design_explored
failure_classes:
- blocked
- missing_context
output_schema:
  type: object
  properties:
    summary:
      type: string
    alternatives:
      type: array
      items:
        type: string
    open_questions:
      type: array
      items:
        type: string
  required:
  - summary
---

# brainstorming

> **Execution contract (Omega):** This skill executes ONLY inside an Omega Auto dispatch. It must NOT create new top-level goals, continue work beyond the dispatch, or use any tool outside its declared `allowed_tools`. It returns structured evidence matching the declared `output_schema`.

> Curated from @obra.

## What it does

Transform rough ideas into fully-formed designs through structured questioning and alternative exploration. Useful early in concept work.

## Source

- Upstream: https://github.com/obra/superpowers
- Category: `creative-direction`

## How to use

This catalogue entry advertises the skill in Open Design so the agent
discovers it during planning. To run the full upstream workflow with
its original assets, scripts, and references, install the upstream
bundle into your active agent's skills directory:

```bash
# Inspect the upstream README for exact paths
open https://github.com/obra/superpowers
```

Then ask the agent to invoke this skill by name (`brainstorming`) or with
one of the trigger phrases listed in this skill's frontmatter.
