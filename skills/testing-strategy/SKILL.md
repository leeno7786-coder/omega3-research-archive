---
name: testing-strategy
kind: execution_skill
version: 1
enabled: true
description: 'Testing methodology: TDD, test pyramid, mocking, coverage, integration
  tests, and property-based testing'
long_description: 'Activates comprehensive testing methodology: test pyramid (70/20/10
  split), TDD red-green-refactor cycle, pytest patterns with parametrization and fixtures,
  boundary mocking rules (never mock what you don''t own), coverage targets with error-path
  focus, property-based testing with hypothesis, and integration test patterns with
  testcontainers and httpx.'
author: omega
tags:
- testing
- tdd
- pytest
- mocking
- coverage
keywords: []
skill_class: testing_skill
intents:
- USE_SKILL
execution_kinds:
- TEST
- VERIFY
- DEBUG
domains:
- testing
allowed_tools:
- read_files
- search_repo
- run_tests
- inspect_git_diff
forbidden_tools:
- patch_file
- run_command
- web_fetch
- write_file
safety_class: command_safe
requires_confirmation_for: []
max_steps: 30
max_duration_ms: 600000
max_files_changed: 10
evidence_required:
- tests_run
- coverage
completion_criteria:
- tests_executed
failure_classes:
- test_failure
- flaky_test
output_schema:
  type: object
  properties:
    summary:
      type: string
    tests_run:
      type: array
      items:
        type: string
    passed:
      type: integer
    failed:
      type: integer
  required:
  - summary
---

# testing-strategy

> **Execution contract (Omega):** This skill executes ONLY inside an Omega Auto dispatch. It must NOT create new top-level goals, continue work beyond the dispatch, or use any tool outside its declared `allowed_tools`. It returns structured evidence matching the declared `output_schema`.

Activates comprehensive testing methodology: test pyramid (70/20/10 split), TDD red-green-refactor cycle, pytest patterns with parametrization and fixtures, boundary mocking rules (never mock what you don't own), coverage targets with error-path focus, property-based testing with hypothesis, and integration test patterns with testcontainers and httpx.

## Instructions
You are a testing specialist. Follow these patterns.

## Test pyramid
- **Unit tests** (70%): test one function/class in isolation. Fast, deterministic.
- **Integration tests** (20%): test two or more real components together (API + DB, service + queue).
- **E2E tests** (10%): test the full system. Slow, brittle - cover only critical paths.

## TDD (Red-Green-Refactor)
1. **Red**: Write a failing test that describes the desired behavior.
2. **Green**: Write the minimum code to make it pass.
3. **Refactor**: Clean up the code while keeping tests green.

## pytest patterns
```python
from pytest import fixture, mark, raises

@fixture
def user_repo() -> UserRepo:
    return InMemoryUserRepo()  # fast, no DB needed

@mark.parametrize("email,expected", [
    ("alice@example.com", True),
    ("not-an-email", False),
    ("", False),
])
def test_validate_email(email: str, expected: bool) -> None:
    assert validate_email(email) == expected
```

## Mocking rules
- Mock at the **boundary** of your system: mock the HTTP call, not `requests.get()` everywhere.
- Use `unittest.mock` or `pytest-mock`. Prefer `mocker` fixture from pytest-mock.
- Mock sparingly. Too many mocks mean the test tests nothing real.
- Use `patch.object(MyClass, "method_name")` over `patch("module.path.function")` - refactor-safe.
- NEVER mock what you don't own (stdlib, third-party libs without wrappers).

## Fixtures pattern
```python
# conftest.py
@fixture
def db_session() -> Generator:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)
```
- Use `conftest.py` for shared fixtures. Use `scope="session"` for expensive setup.
- Fixtures should be explicit: use `request.getfixturevalue()` sparingly.

## Coverage
- Aim for 80%+ coverage. Focus on logic, not infrastructure.
- `pytest --cov=src --cov-report=term-missing` to see what's uncovered.
- Don't chase 100% - focus on untested branches and error paths.
- Cover error paths: what happens when the DB is down? When the file doesn't exist? When input is malformed?

## Property-based testing
- Use `hypothesis` for property-based tests:
```python
from hypothesis import given, strategies as st

@given(st.lists(st.integers()))
def test_sort_is_idempotent(items: list[int]) -> None:
    assert sorted(sorted(items)) == sorted(items)
```
- Great for: serialization round-trips, sorting, data validation, parsing.

## Integration test patterns
- Use testcontainers for real DB/Redis/queue in CI: `from testcontainers.postgres import PostgresContainer`.
- Use `httpx.AsyncClient` with FastAPI's `TestClient` or `AsyncClient`.
- Use `responses` or `pytest-httpx` for HTTP call recording/replay.
