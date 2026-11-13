---
name: git-conventional-commits
kind: execution_skill
version: 1
enabled: true
description: 'Git workflow: conventional commits, branching strategies, rebase discipline,
  and PR management'
author: omega
tags:
- git
- commits
- workflow
keywords: []
skill_class: maintenance_skill
intents:
- USE_SKILL
execution_kinds:
- MAINTAIN
- INSPECT
- VERIFY
domains:
- git
- devops
- version-control
- workflow
allowed_tools:
- read_files
- search_repo
- inspect_git_diff
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
max_steps: 30
max_duration_ms: 600000
max_files_changed: 10
evidence_required:
- git_operations_logged
- commit_format_validated
- branch_strategy_applied
- pre_commit_checks_passed
completion_criteria:
- conventional_commits_enforced
- branch_naming_compliant
- pr_workflow_followed
- rebase_discipline_maintained
failure_classes:
- invalid_commit_format
- branch_naming_violation
- uncommitted_changes
- rebase_conflict
- lint_failure
- test_failure
output_schema:
  type: object
  properties:
    workflow_phase:
      type: string
      enum:
      - pre_commit_checklist
      - branch_creation
      - commit_validation
      - rebase_discipline
      - pr_workflow
      - merge_strategy
    status:
      type: string
      enum:
      - pass
      - fail
      - warning
    checks:
      type: array
      items:
        type: object
        properties:
          check_name:
            type: string
          result:
            type: string
          details:
            type: string
        required:
        - check_name
        - result
    guidance:
      type: string
    next_steps:
      type: array
      items:
        type: string
  required:
  - workflow_phase
  - status
  - checks
long_description: 'Activates conventional commit workflow: structured commit messages
  with type/scope/body/footer, branch naming conventions (feat/fix/chore/docs), pre-commit
  inspection checklist, rebase discipline, and PR management from branch creation
  through squash-merge.'
---

# git-conventional-commits

Activates conventional commit workflow: structured commit messages with type/scope/body/footer, branch naming conventions (feat/fix/chore/docs), pre-commit inspection checklist, rebase discipline, and PR management from branch creation through squash-merge.

## Instructions
You are a Git workflow expert. Follow these conventions.

## Commit message format
```
<type>(<scope>): <description>

<body>

<footer>
```
- Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`, `ci`, `revert`
- Scope: the module or package (optional, kebab-case)
- Description: imperative present tense, lowercase, no trailing period, max 72 chars
- Body: wrap at 72 chars, explain WHY not what, blank line after subject
- Footer: `BREAKING CHANGE:` or `Closes #123`, `Refs #456`

Examples:
```
feat(core): add pressure-field contribution registry

Operators can now register custom pressure contributors via
PressureField.register() instead of monkey-patching.

Closes #89
```

## Branch naming
| Purpose | Pattern |
|---------|--------|
| Feature | `feat/<short-description>` |
| Fix | `fix/<issue>-<short-description>` |
| Chore | `chore/<short-description>` |
| Docs | `docs/<short-description>` |
| Refactor | `refactor/<short-description>` |

## Before commit
1. `git diff --check` - no trailing whitespace
2. Run linter on changed files: `ruff check .` or `ruff format --check .`
3. Run tests: `pytest tests/related/`
4. Review diff: no debug prints, commented code, or TODOs
5. `git status` - verify only intended files staged

## Rebasing
- `git pull --rebase` not `git pull` to avoid merge commits
- `git rebase -i main` before opening a PR to squash fixups
- Keep commits atomic: one logical change per commit
- Never rebase commits that have been pushed to a shared branch
- `git rebase --abort` if things go wrong

## PR workflow
1. Branch from `main`. Keep branches short-lived (days, not weeks).
2. Rebase onto target before opening PR.
3. Write a clear PR description: what, why, how, testing notes.
4. Request reviews from relevant team members.
5. Address feedback in new commits (don't rebase during review).
6. Squash-merge or rebase-merge when approved.

## Inspection commands
- `git status` - working tree state
- `git diff` - unstaged changes
- `git diff --cached` - staged changes
- `git log --oneline -10` - recent commits
- `git log --oneline --graph --all` - full branch topology
- `git stash list` - stashed work
- `git bisect start` - find the commit that introduced a bug

## Execution contract

This skill executes ONLY inside an Omega Auto dispatch (the qwen executor, jailed to the operator workspace). It MUST NOT create new top-level goals, MUST use only its allowed_tools, MUST NOT write canonical Omega memory, and MUST return structured evidence matching the output schema. Stop after success, a blocker, a safety denial, or the step/time limit.
