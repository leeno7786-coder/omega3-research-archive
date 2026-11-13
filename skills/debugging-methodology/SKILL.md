---
name: debugging-methodology
kind: execution_skill
version: 1
enabled: true
description: 'Systematic debugging: isolation, hypothesis testing, git bisect, and
  root cause analysis'
long_description: 'Activates a structured 5-step debugging methodology: reproduce
  with exact steps and environment capture, isolate via binary search and git bisect,
  hypothesize with falsifiable predictions, fix the root cause (not symptom) with
  regression tests, and verify across related patterns. Covers common bug patterns:
  Heisenbugs, concurrency, memory leaks, network issues, and timeouts.'
author: omega
tags:
- debugging
- troubleshooting
- bisect
- root-cause
keywords: []
skill_class: debugging_skill
intents:
- USE_SKILL
execution_kinds:
- INSPECT
- DEBUG
- REPAIR
- VERIFY
domains:
- engineering
- debugging
allowed_tools:
- read_files
- search_repo
- patch_file
- run_tests
- inspect_git_diff
forbidden_tools:
- run_command
- web_fetch
- write_file
safety_class: write_safe
requires_confirmation_for: []
max_steps: 30
max_duration_ms: 600000
max_files_changed: 15
evidence_required:
- root_cause
- diff_summary
- tests_run
completion_criteria:
- bug_resolved
- tests_pass
failure_classes:
- unresolved
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
    diff_summary:
      type: object
    tests_run:
      type: array
      items:
        type: string
  required:
  - summary
---

# debugging-methodology

> **Execution contract (Omega):** This skill executes ONLY inside an Omega Auto dispatch. It must NOT create new top-level goals, continue work beyond the dispatch, or use any tool outside its declared `allowed_tools`. It returns structured evidence matching the declared `output_schema`.

Activates a structured 5-step debugging methodology: reproduce with exact steps and environment capture, isolate via binary search and git bisect, hypothesize with falsifiable predictions, fix the root cause (not symptom) with regression tests, and verify across related patterns. Covers common bug patterns: Heisenbugs, concurrency, memory leaks, network issues, and timeouts.

## Instructions
You are a systematic debugger. Follow this 5-step methodology.

## Step 1: Reproduce
- Get exact steps. What input, state, environment? Write them down.
- Is it deterministic or intermittent? If intermittent, look for races, timing, or uninitialized state.
- Capture: error message, stack trace, log tail, request/response payload.
- Can you reproduce in isolation (unit test) or only in production?

## Step 2: Isolate
- Binary search: comment out half the code, see if the bug persists. Repeat.
- Check inputs first: is the data what you expect? Add assertions at module boundaries.
- Check assumptions: is that cached value stale? Is that dependency version correct?
- `git bisect start` - find the exact commit that introduced the regression:
```
git bisect start
git bisect bad          # current version is broken
git bisect good v1.0    # last known good version
# git will checkout commits for you to test
# git bisect good  or  git bisect bad  after each test
git bisect reset        # when done
```
- Use `git bisect run` with a script for automated bisect.

## Step 3: Hypothesize
- Form a specific hypothesis: "The bug is that the cache key doesn't include the user's tenant ID, so user A sees user B's data."
- Design the minimum experiment: add the tenant ID to the cache key. Does it fix it?
- One hypothesis at a time. Don't change multiple variables.
- A good hypothesis is falsifiable: "If I add logging before and after the cache call, I'll see a cache hit with the wrong key."

## Step 4: Fix
- Fix the root cause, not the symptom. Ask "why?" five times.
- Add a regression test that FAILS before the fix and PASSES after.
- Minimum diff. Don't refactor unrelated code during a bugfix.
- Consider: does the same pattern exist elsewhere in the codebase?

## Step 5: Verify
- Run the full test suite, not just the one test.
- Think about related bugs: same pattern, same library, same author.
- Edge cases: empty/null/zero/max values after the fix.
- Document the root cause in the commit message: "fix: cache key missing tenant ID - fixes cross-tenant data leak".

## Common patterns
- **Heisenbugs**: bug disappears when you try to observe it. Add logging, not breakpoints.
- **Concurrency**: reproduce with `stress --cpu 8` or `while true; do curl ...; done`.
- **Memory**: use `tracemalloc` for Python, `--inspect` for Node.js, `valgrind` for C.
- **Network**: use `curl -w "@-\n"`, `tcpdump`, Wireshark, or mitmproxy.
- **Timeout**: check if the operation completes in < 1s vs > 30s. Log duration at every step.
