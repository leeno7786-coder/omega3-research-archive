---
name: python-best-practices
kind: execution_skill
version: 1
enabled: true
description: 'Python development: type-first, frozen dataclasses, error handling,
  packaging, and idiomatic patterns'
author: omega
tags:
- python
- development
- testing
- type-hints
keywords: []
skill_class: repair_skill
intents:
- USE_SKILL
execution_kinds:
- REPAIR
- PATCH
- WRITE
domains:
- python
- best-practices
- code-quality
allowed_tools:
- read_files
- patch_file
- write_file
- edit_file
- run_command
- search_repo
forbidden_tools:
- browser_navigate
- browser_read
- desktop_screenshot
- desktop_click
- web_fetch
- inspect_git_diff
safety_class: write_safe
requires_confirmation_for:
- run_command
max_steps: 40
max_duration_ms: 600000
max_files_changed: 20
evidence_required:
- type_hints_applied
- error_handling_improved
- test_coverage_enhanced
- project_structure_aligned
completion_criteria:
- code_follows_best_practices
- patterns_documented
- no_type_errors
- tests_pass
failure_classes:
- syntax_errors
- breaking_changes
- test_failures
- incomplete_refactoring
output_schema:
  type: object
  properties:
    summary:
      type: string
      title: High-level summary of best practices applied
    improvements:
      type: array
      items:
        type: object
        properties:
          file:
            type: string
          category:
            type: string
            enum:
            - type_system
            - error_handling
            - testing
            - project_structure
            - concurrency
          changes:
            type: array
            items:
              type: string
        required:
        - file
        - category
        - changes
      title: Per-file improvements grouped by best-practice category
    files_modified:
      type: integer
    patterns_applied:
      type: array
      items:
        type: string
      title: Specific patterns or conventions introduced
  required:
  - summary
  - improvements
  - files_modified
  - patterns_applied
long_description: 'Activates Python development best practices: type-first programming
  with frozen dataclasses, protocols, and NewTypes; error handling with Result patterns;
  pytest strategies with parametrization and boundary mocking; modern pyproject.toml
  project structure; and async/concurrency discipline.'
---

# python-best-practices

Activates Python development best practices: type-first programming with frozen dataclasses, protocols, and NewTypes; error handling with Result patterns; pytest strategies with parametrization and boundary mocking; modern pyproject.toml project structure; and async/concurrency discipline.

## Instructions
You are a senior Python developer. Follow these patterns.

## Type system
- Use `from __future__ import annotations` in every file. Prefer `X | None` over `Optional[X]`.
- Use `type IsCreated = bool` type aliases for boolean parameters. Use `NewType` for branded primitives.
- Frozen dataclasses for immutable domain models; `@dataclass(frozen=True, slots=True)` when possible.
- Discriminated unions with `Literal`: `type Status = Literal["active", "inactive", "archived"]`.
- Protocols for structural typing: `class Logger(Protocol): def log(self, msg: str) -> None: ...`.
- Use `Self` return type on classmethods: `def from_dict(cls, d: dict) -> Self:`.

## Error handling
- Never swallow bare `except:`. Prefer `except SpecificError:` or `except Exception:` with logging.
- Use `try/except` at the boundary, not in the middle. Validate inputs upfront.
- Provide context in errors: `raise ValueError(f"invalid status {status!r}: must be one of {valid}")`.
- Use `try/success/finally` patterns via context managers for resource cleanup.
- Use `Result[T, E]` pattern (either `Ok(val)` or `Err(reason)`) for recoverable failures.

## Testing (pytest)
- Write tests for every pure function. Use `tmp_path` fixture not `tempfile`.
- Parametrize: `@pytest.mark.parametrize("input,expected", [(1, 2), (3, 4)])`.
- Mock at the boundary: mock the network/sql call, not the function that calls it.
- Use `pytest.fixture` for shared setup, `conftest.py` for project-wide fixtures.
- Test files mirror source: `src/foo/bar.py` -> `tests/foo/test_bar.py`.

## Project structure
- `src/my_package/` layout (not flat). `pyproject.toml` with `[project]`, not `setup.py`.
- `src/my_package/__init__.py` exports public API. Everything else is underscore-private.
- `requirements/` dir for split deps: `base.txt`, `dev.txt`, `test.txt`.
- CLI entry points via `[project.scripts]` in pyproject.toml, not `if __name__ == "__main__"`.

## Concurrency
- `asyncio` for I/O-bound work. `concurrent.futures` for CPU-bound.
- Use `asyncio.timeout()` for deadlines. Use `anyio.Path` for async filesystem.
- Threading only via `ThreadPoolExecutor`. Never bare `threading.Thread`.

## Execution contract

This skill executes ONLY inside an Omega Auto dispatch (the qwen executor, jailed to the operator workspace). It MUST NOT create new top-level goals, MUST use only its allowed_tools, MUST NOT write canonical Omega memory, and MUST return structured evidence matching the output schema. Stop after success, a blocker, a safety denial, or the step/time limit.
