---
name: pytest
kind: execution_skill
version: 1
enabled: true
description: 'Write and run robust pytest suites with fixtures, parametrization, and coverage'
long_description: 'Authors and runs pytest suites for Python code. Inspects existing tests, writes focused test modules, and verifies behavior with deterministic evidence.'
author: omega
tags:
- testing
- pytest
- python
- coverage
keywords:
- pytest
- unit tests
- test suite
skill_class: testing_skill
intents:
- USE_SKILL
execution_kinds:
- TEST
- WRITE
- DEBUG
domains:
- python
- testing
- development
allowed_tools:
- read_files
- search_repo
- inspect_git_diff
- run_tests
- write_file
- edit_file
forbidden_tools:
- run_command
- web_fetch
- browser_navigate
- browser_read
- desktop_screenshot
- desktop_click
safety_class: write_safe
requires_confirmation_for: []
max_steps: 40
max_duration_ms: 600000
max_files_changed: 40
evidence_required:
- summary
- files_changed
- test_results
completion_criteria:
- tests_pass
- review_complete
failure_classes:
- blocked
- missing_context
- test_failure
output_schema:
  type: object
  properties:
    summary:
      type: string
    files_changed:
      type: array
      items:
        type: string
    test_results:
      type: string
    tests_passed:
      type: boolean
  required:
  - summary
---

# pytest

> **Execution contract (Omega):** This skill executes only inside an Omega Auto dispatch. It must return structured evidence matching the declared output schema.

Use this skill when the task requires authoring, debugging, or running Python pytest suites.

## Workflow

1. Inspect the existing tests and the implementation before editing.
2. Prefer focused fixtures and parametrization over duplicated cases.
3. Add the smallest regression test that proves the behavior.
4. Run the focused test first, then the relevant broader suite.
5. Report the test commands, results, and files changed.
