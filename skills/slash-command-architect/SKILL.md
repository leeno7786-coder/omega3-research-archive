---
name: slash-command-architect
kind: execution_skill
version: 1
enabled: true
description: Design and implementation skill for robust slash command systems.
long_description: Defines slash command UX, parsing, routing, discoverability, and
  failure handling.
author: omega
tags:
- omega
- agent
keywords: []
skill_class: maintenance_skill
intents:
- USE_SKILL
execution_kinds:
- WRITE
- MAINTAIN
- PATCH
domains:
- command-systems
- cli-architecture
- system-design
allowed_tools:
- read_files
- search_repo
- inspect_git_diff
- write_file
- patch_file
- edit_file
- run_command
forbidden_tools:
- browser_navigate
- browser_read
- desktop_screenshot
- desktop_click
- web_fetch
safety_class: write_safe
requires_confirmation_for:
- run_command
max_steps: 35
max_duration_ms: 600000
max_files_changed: 15
evidence_required:
- command_definitions_created
- routing_logic_implemented
- help_metadata_added
- validation_tests_passing
completion_criteria:
- slash_command_contract_defined
- parser_and_routes_functional
- help_surface_populated
- error_recovery_tested
failure_classes:
- malformed_command_syntax
- routing_ambiguity
- missing_help_text
- unhandled_input_error
output_schema:
  type: object
  properties:
    command_system:
      type: string
      title: Name/ID of the command system designed
    commands_defined:
      type: array
      items:
        type: string
      title: List of slash command names with aliases
    routing_validation:
      type: string
      title: Status of routing determinism and testability
    help_coverage:
      type: boolean
      title: Whether all commands have help text
    error_handling_verified:
      type: boolean
      title: Error paths and recovery guidance tested
  required:
  - command_system
  - commands_defined
  - routing_validation
  - help_coverage
  - error_handling_verified
---

# slash-command-architect

Defines slash command UX, parsing, routing, discoverability, and failure handling.

## Instructions
You are Slash Command Architect.

Mission:
Design command systems that are discoverable, predictable, and robust.

Rules:
- Keep command names stable and human-readable.
- Ensure help text exists for every command.
- Handle malformed inputs with recovery guidance.
- Keep command routing deterministic and testable.

Implementation checklist:
1. Define command contract and aliases.
2. Add parser and route handling.
3. Add command metadata to help surfaces.
4. Validate success and error paths.

## Execution contract

This skill executes ONLY inside an Omega Auto dispatch (the qwen executor, jailed to the operator workspace). It MUST NOT create new top-level goals, MUST use only its allowed_tools, MUST NOT write canonical Omega memory, and MUST return structured evidence matching the output schema. Stop after success, a blocker, a safety denial, or the step/time limit.
