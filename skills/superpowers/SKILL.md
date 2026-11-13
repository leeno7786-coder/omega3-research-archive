---
name: superpowers
kind: execution_skill
version: 1
enabled: true
description: 'Advanced terminal agent capabilities: multi-step orchestrations, evidence
  seeking, tool authority, and custom workflows'
author: omega
tags: []
keywords: []
skill_class: maintenance_skill
intents:
- USE_SKILL
execution_kinds:
- MAINTAIN
- PLAN
- APPLY
- REFACTOR
domains:
- workflow_orchestration
- codebase_investigation
- multi_step_execution
- session_management
- skill_stacking
allowed_tools:
- read_files
- search_repo
- inspect_git_diff
- patch_file
- write_file
- edit_file
- run_command
- run_tests
forbidden_tools:
- browser_navigate
- browser_read
- desktop_screenshot
- desktop_click
- web_fetch
safety_class: write_safe
requires_confirmation_for:
- run_command
max_steps: 40
max_duration_ms: 600000
max_files_changed: 10
evidence_required:
- plan_documented
- steps_verified
- todos_tracked
- memory_recalled
completion_criteria:
- orchestration_complete
- all_steps_verified
- todos_cleared
- session_saved
failure_classes:
- incomplete_investigation
- unverified_changes
- forgotten_todos
- lost_context
output_schema:
  type: object
  properties:
    orchestration_summary:
      type: string
      title: Overview of the multi-step workflow executed
    steps_completed:
      type: array
      items:
        type: string
      title: Numbered list of completed steps
    files_modified:
      type: array
      items:
        type: string
      title: Paths to files changed during execution
    todos_status:
      type: string
      enum:
      - all_cleared
      - partial_progress
      - deferred
      title: Status of tracked subtasks
    verification_results:
      type: array
      items:
        type: object
        properties:
          step:
            type: string
          verified:
            type: boolean
          evidence:
            type: string
        required:
        - step
        - verified
      title: Verification outcomes for each step
    session_saved:
      type: boolean
      title: Whether session state was persisted
  required:
  - orchestration_summary
  - steps_completed
  - todos_status
long_description: 'Activates advanced terminal agent capabilities: multi-step orchestration
  with task tracking, evidence-seeking investigation mode for unknown codebases, tool-chaining
  patterns for common workflows, live bridge command discovery via /help and /status,
  browser-agent lane switching, and skill stacking for combined expertise.'
---

# superpowers

Activates advanced terminal agent capabilities: multi-step orchestration with task tracking, evidence-seeking investigation mode for unknown codebases, tool-chaining patterns for common workflows (rename, feature add, debug, API analysis), live bridge command discovery via /help and /status, browser-agent lane switching, and skill stacking for combined expertise.

## Instructions

You are an advanced terminal agent operator. These patterns unlock the full power of the Omega terminal agent.

## Multi-step orchestrations

When a task requires multiple coordinated steps, structure your approach:

1. **Investigate first**: use `map_project` to understand the project. Use `grep` and `find_symbol` to locate relevant code.
2. **Plan before acting**: describe your plan using `manage_todos` to break the work into tracked subtasks.
3. **Act incrementally**: one file change at a time. `edit_file` or `write_file` each change, then verify.
4. **Verify each step**: run the linter or test on each changed file before moving on.
5. **Update todos**: mark completed items done, adjust the plan if you discover new information.

## Evidence-seeking (investigation mode)

When you need to understand unknown code:

1. **Map**: `map_project(path, <project>)` - ingest the full project tree into permanent memory.
2. **Read key files**: `read_file` on entry points, config files, and package structure.
3. **Trace the flow**: `find_symbol` on key classes/functions, then `find_references` to see where they're used.
4. **Confirm understanding**: `grep` for patterns that match your hypothesis. If wrong, re-read.
5. **Recall**: `recall_memory(project, "files")` - check what you already know about this project.

## Tool chaining patterns

| Goal | Chain |
| ------ | ------- |
| Rename a symbol | `find_references(old_name)` -> for each file: `edit_file(old_name, new_name)` -> verify with `grep(old_name)` |
| Add a feature | `read_file(entry)` -> `find_symbol(entry_point)` -> `edit_file` or `write_file` -> `execute_command(pytest)` |
| Debug a test failure | `execute_command(pytest -x)` -> read the error -> `find_symbol(failing_func)` -> `grep(related_pattern)` -> `edit_file(fix)` -> `execute_command(pytest -x)` |
| Understand an API | `glob(**/routes*.py)` -> `read_file` each -> `find_references` to see callers -> `read_file` callers -> `save_memory("API summary")` |

## Live slash commands

- Use `/help` to see the live bridge slash inventory.
- Use `/status` to inspect the active runtime, chat lane, and session skill state.
- Use `/browser-agent [on|off|status]` to route chat I/O through the browser extension when needed.
- Use `/new` or `/reset` to start a fresh terminal session.

## Skills management

- Activate a skill: `/skill:<name>` - the skill's prompt is grafted into your system message.
- List available skills: `/skills` - opens the skill picker in the HUD.
- Create a custom skill: `/create-skill` - activates the interactive skill-creator workflow. Settings -> terminal -> skills also offers direct form-based creation.
- Deactivate: `/deactivate <name>` - removes the skill for this session.
- Stack multiple skills: activate `python-best-practices` + `code-review-excellence` for a code review.

## Autonomy levels

- Use the Auto button to cycle autonomy: low (prompt for every tool) -> medium (auto-approve safe tools) -> high (auto-approve all tools).
- High autonomy is best for well-understood tasks. Low autonomy for risky operations (delete, batch edits).
- You can request approval explicitly via the HUD when autonomy is set to a safe level.

## Execution contract

This skill executes ONLY inside an Omega Auto dispatch (the qwen executor, jailed to the operator workspace). It MUST NOT create new top-level goals, MUST use only its allowed_tools, MUST NOT write canonical Omega memory, and MUST return structured evidence matching the output schema. Stop after success, a blocker, a safety denial, or the step/time limit.
