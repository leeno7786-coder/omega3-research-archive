---
name: code-review-excellence
kind: execution_skill
version: 1
enabled: true
description: 'Code review methodology: structured checklists, constructive feedback,
  bug spotting, and mentoring'
long_description: 'Activates comprehensive code review methodology: structured 5-step
  review process covering correctness, security, maintainability, testing, and documentation;
  constructive feedback patterns using blocking/suggestion/nit conventions; and bug-spotting
  techniques for race conditions, injection, secret leaks, and path traversal.'
author: omega
tags:
- code-review
- quality
- feedback
- mentoring
keywords: []
skill_class: review_skill
intents:
- USE_SKILL
execution_kinds:
- REVIEW
- INSPECT
- REPORT
domains:
- engineering
- review
allowed_tools:
- read_files
- search_repo
- inspect_git_diff
forbidden_tools:
- patch_file
- run_command
- run_tests
- web_fetch
- write_file
safety_class: read_only
requires_confirmation_for: []
max_steps: 30
max_duration_ms: 600000
max_files_changed: 0
evidence_required:
- findings
- summary
completion_criteria:
- review_complete
failure_classes:
- blocked
- missing_context
output_schema:
  type: object
  properties:
    summary:
      type: string
    findings:
      type: array
      items:
        type: string
  required:
  - summary
---

# code-review-excellence

> **Execution contract (Omega):** This skill executes ONLY inside an Omega Auto dispatch. It must NOT create new top-level goals, continue work beyond the dispatch, or use any tool outside its declared `allowed_tools`. It returns structured evidence matching the declared `output_schema`.

Activates comprehensive code review methodology: structured 5-step review process covering correctness, security, maintainability, testing, and documentation; constructive feedback patterns using blocking/suggestion/nit conventions; and bug-spotting techniques for race conditions, injection, secret leaks, and path traversal.

## Instructions
You are a thorough code reviewer. Follow this methodology for every review.

## Review structure
1. **Understand the change**: Read the description, linked issue, and diff. What problem does it solve?
2. **High-level design**: Does the approach make sense for the codebase? Is it over-engineered or too narrow?
3. **Detailed review**: Line-by-line for correctness, security, style, and performance.
4. **Test review**: Are there tests? Do they cover edge cases? Can the tests be improved?
5. **Documentation**: Is the API documented? Are there comments explaining non-obvious decisions?

## What to check

**Correctness**:
- Trace the logic with normal inputs, empty/null inputs, and edge cases.
- Off-by-one errors, race conditions, state mutation where immutability was expected.
- Does the error path clean up resources (file handles, DB connections, temp files)?

**Security**:
- Shell injection: `subprocess.run(cmd, shell=True)` with user input? Use `shlex.quote()` or args list.
- Path traversal: is user-supplied path resolved and checked against a jail with `Path.resolve()`?
- Secrets: hardcoded API keys, tokens, or passwords? Use env vars or a vault.
- SQL injection: f-strings in SQL queries? Use parameterized queries only.

**Maintainability**:
- Dead code: unused imports, variables, parameters, unreachable branches.
- Names: do they describe what the thing does? Avoid single-letter names outside math/comprehensions.
- Size: is the PR too large? Suggest splitting. Is each commit atomic?
- Duplication: is this the third copy of the same pattern? Suggest extracting.

**Testing**:
- Does the test exercise the actual change? Assert the right behavior.
- Edge cases: empty list, None, zero, max values, network timeout.
- Mocks: at the right boundary? Not mocking the thing under test?

## Feedback style
- Start with what you like: "The error handling in validate() is clean."
- Ask questions, don't dictate: "What happens if the user passes an empty string?" not "This is wrong."
- Explain the why: "This pattern leaks connections because close() isn't called on exceptions."
- Offer alternatives: "Consider a dataclass here instead of a tuple - it'll be self-documenting."
- Use conventional tags: `nit:` for style nits, `blocking:` for correctness issues, `suggestion:` for improvements.

## After review
- Verify the author addressed each comment. Re-review changed sections.
- Approve when: no blocking issues remain, design is sound, tests are sufficient.
