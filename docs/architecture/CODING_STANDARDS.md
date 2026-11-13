# Omega 3.0 Coding Standards

## Architectural Boundaries

### Guardian vs Memory Responsibilities

**Guardian** focuses on:
- Security policy and decision authority
- Response class registry and selection
- Terminal feedback and telemetry emitters
- HMAC ledger for audit trails
- Production mode enforcement

**Memory** focuses on:
- Memory write validation (purity checks via `memory.write_guard`)
- Memory storage and retrieval
- Memory promotion and staging
- Memory consistency and integrity

**Key principle:** Memory controls its own write validation. Guardian does not control memory activity directly.

## Error Handling Doctrine

### Principle: Fail-Closed for Security, Fail-Soft for Infrastructure

Omega 3.0 uses a **dual error handling doctrine** based on the nature of the operation:

### 1. Fail-Hard (Cognition Paths)

**When to use:** Cognition operations, security checks, data integrity

**Behavior:** Exceptions propagate and halt the operation

**Rationale:** 
- Operator directive: "cognition crashes the system"
- Better to halt than to run on corrupted data
- Security: never open gates on failure

**Examples:**
```python
# cognition_chain.py - Cognition cascade
def ignite_episode(store, ignition):
    return _ignite_episode_cascade(store, ignition)
    # No try/except - failures propagate

# memory/write_guard.py - Security checks
def guard_raw_text(text: str, *, lobe_id: Optional[int] = None) -> bool:
    if not result.allowed:
        log.warning("write_guard raw-text rejected on lobe %d", lobe)
        return False  # Explicit rejection, not exception
    # Security violations are logged and rejected, not passed through
```

**Pattern:**
```python
# DO: Let cognition failures propagate
def run_cognition_phase(input_data):
    result = process(input_data)
    return result  # Exceptions propagate to caller

# DON'T: Swallow cognition errors
def run_cognition_phase(input_data):
    try:
        result = process(input_data)
        return result
    except Exception:
        return None  # BAD: runs on corrupted data
```

### 2. Fail-Soft (Infrastructure/External Calls)

**When to use:** Network calls, file I/O, external services, optional features

**Behavior:** Log warning and return degraded/default value

**Rationale:**
- Infrastructure should not halt cognition
- Degraded operation is better than no operation
- Allows system to continue with reduced functionality

**Examples:**
```python
# runtime/client.py - HTTP client
def _post(self, path: str, json_body: dict, *, op: str, timeout: Optional[float] = None) -> Optional[dict]:
    try:
        r = self._ensure_session().post(url, json=json_body, timeout=timeout)
    except (requests.ConnectionError, requests.Timeout) as exc:
        log.warning("%s connection/timeout: %s", op, exc)
        return None  # Degraded path
    # Return None on failure, caller handles degraded path

# guardian/terminal_feedback.py - Telemetry
def _route_telemetry(event: dict[str, Any]) -> None:
    try:
        from guardian.telemetry import write_telemetry
        write_telemetry(...)
    except Exception as exc:
        log.debug("telemetry route failed: %s", exc)
        # Telemetry failure is non-fatal
```

**Pattern:**
```python
# DO: Handle infrastructure failures gracefully
def fetch_external_data(url: str) -> Optional[dict]:
    try:
        response = requests.get(url, timeout=5)
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("Failed to fetch %s: %s", url, exc)
        return None  # Caller handles None

# DON'T: Let infrastructure errors halt cognition
def fetch_external_data(url: str) -> dict:
    response = requests.get(url, timeout=5)
    return response.json()  # BAD: crashes on network failure
```

### 3. Conditional Imports (Optional Features)

**When to use:** Truly optional dependencies, features that can degrade

**Behavior:** Try import, log warning on failure, continue without feature

**Examples:**
```python
# memory/write_guard.py - Optional memory_purity
try:
    from guardian.memory_purity import PurityDecision
    mp = _purity_singleton()
    purity = mp.check_leaf(text=text, lobe=lobe_name, source=source)
except Exception as exc:
    log.debug("memory_purity unavailable (%s) — passing", exc)
    # Continue without purity check (documented degraded path)
```

**Pattern:**
```python
# DO: Use conditional imports for optional features
def enhanced_feature(data: str) -> dict:
    try:
        from optional_lib import advanced_processor
        return advanced_processor(data)
    except ImportError:
        log.warning("optional_lib unavailable, using basic processor")
        return basic_processor(data)

# DON'T: Use conditional imports for core dependencies
def core_feature(data: str) -> dict:
    try:
        from core_lib import processor
        return processor(data)
    except ImportError:
        return {}  # BAD: core dependency should be top-level import
```

### Decision Matrix

| Context | Strategy | Example |
|---------|----------|---------|
| Cognition phase execution | Fail-hard | `cognition_chain.py` |
| Security checks | Fail-hard (reject) | `memory/write_guard.py` |
| Data integrity validation | Fail-hard | `memory/api.py` |
| HTTP/network calls | Fail-soft | `runtime/client.py` |
| File I/O | Fail-soft | `config/loader.py` |
| Telemetry/logging | Fail-soft | `guardian/terminal_feedback.py` |
| Optional features | Conditional import | `guardian/memory_purity` |
| Core dependencies | Top-level import | All main modules |

### Documentation Requirement

Every module must document its error handling strategy in its docstring:

```python
"""
Module docstring should include:

Error Handling:
    - Fail-hard for cognition operations (exceptions propagate)
    - Fail-soft for external calls (returns None on failure)
    - See CODING_STANDARDS.md for doctrine
"""
```

---

## Import Strategy Guide

### Principle: Lazy for Heavy, Top-Level for Core

### 1. Lazy Imports (Heavy/Optional Dependencies)

**When to use:**
- Cognition modules (auto_loop, omega_loop, deepsleep_loop, etc.)
- Heavy dependencies that slow startup
- Optional features that may not be needed
- Circular dependency avoidance

**Pattern:**
```python
# cognition_chain.py - Lazy import of cognition modules
def _ignite_episode_cascade(store, ignition):
    from global_loop.ignition.run import run as run_ignition
    # Import inside function to avoid circular dependencies
```

**Rationale:**
- Reduces startup time
- Avoids circular dependencies
- Only loads what's actually needed

### 2. Top-Level Imports (Core Dependencies)

**When to use:**
- Lightweight utility modules
- Standard library modules
- Core infrastructure (logging, pathlib, typing)
- Dependencies used throughout the module

**Pattern:**
```python
# guardian/terminal_feedback.py - Top-level imports
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional
# Standard library and lightweight imports at top level
```

**Rationale:**
- Clearer dependencies
- Faster IDE resolution
- No runtime import overhead

### 3. Conditional Imports (Optional Features)

**When to use:**
- Truly optional features
- Features that can degrade gracefully
- Development/debugging tools

**Pattern:**
```python
# memory/write_guard.py - Conditional import
try:
    from guardian.memory_purity import PurityDecision
    mp = _purity_singleton()
except Exception as exc:
    log.debug("memory_purity unavailable (%s) — passing", exc)
    # Continue without the feature
```

**Rationale:**
- Allows system to run without optional features
- Clear degraded path documentation
- Non-fatal to core functionality

### Decision Matrix

| Dependency Type | Strategy | Example |
|----------------|----------|---------|
| Standard library | Top-level | `logging`, `pathlib`, `typing` |
| Core infrastructure | Top-level | `guardian.terminal_feedback` |
| Cognition modules | Lazy | `auto_loop.execute`, `omega_loop.reason` |
| Optional features | Conditional | `guardian.memory_purity` |
| Heavy third-party | Lazy | Large ML libraries, heavy frameworks |
| Circular-prone | Lazy | Cross-slice imports |

### Anti-Patterns

```python
# ❌ BAD: Lazy import of standard library
def process_data(data: str):
    import json  # Should be top-level
    return json.loads(data)

# ❌ BAD: Top-level import of heavy cognition module
import auto_loop.execute.run  # Should be lazy in cognition_chain

# ❌ BAD: Conditional import of core dependency
def core_function():
    try:
        from core.module import essential_func
        return essential_func()
    except ImportError:
        return None  # Core dependency should be top-level
```

---

## Logging Style Guide

### Principle: Structured Observability for Cognition, Simple for Infrastructure

### 1. Cognition Phases - Use `phase_span`

**When to use:** All cognition phase executions (enter/exit/fail)

**Pattern:**
```python
from guardian.terminal_feedback import phase_span

def run_cognition_phase(episode):
    ep_id = str(episode.episode_id)
    with phase_span("auto", "execute", episode_id=ep_id) as span:
        result = execute(episode)
        span.set(tool=result.get("tool"), latency_ms=result.get("latency"))
    # Automatically emits enter/exit/fail events
```

**Benefits:**
- Automatic enter/exit timing
- Consistent phase naming
- Structured telemetry emission
- Automatic failure tracking

### 2. Data Events - Use `emit_data`

**When to use:** Structured data events, state changes, metrics

**Pattern:**
```python
from guardian.terminal_feedback import emit_data

def report_outcome(outcome: dict):
    emit_data(
        "auto",
        "auto_outcome_pack",
        episode_id=outcome.get("episode_id"),
        status=outcome.get("status"),
        cycle_kind=outcome.get("cycle_kind"),
        ok=outcome.get("success", False),
    )
```

**Benefits:**
- Structured key-value data
- Consistent event naming
- Easy filtering/analysis
- Automatic telemetry routing

### 3. Simple Logging - Use `log` for Non-Cognition

**When to use:** Infrastructure events, warnings, debug info

**Pattern:**
```python
import logging

log = logging.getLogger("omega.module.name")

def infra_operation():
    log.info("Starting infrastructure operation")
    try:
        result = do_work()
        log.info("Operation completed successfully")
    except Exception as exc:
        log.warning("Operation failed: %s", exc)
```

**Benefits:**
- Simple and familiar
- Good for one-off messages
- No structured overhead
- Standard Python logging

### 4. Inference Events - Use `emit_inference`

**When to use:** Model inference calls (runtime, classification, embedding)

**Pattern:**
```python
from guardian.terminal_feedback import emit_inference

def classify_text(text: str):
    result = runtime_client.classify_hierarchical(text)
    emit_inference(
        "memory",
        "classify_hierarchical",
        latency_ms=result.latency_ms,
        ok=result.ok,
        status="ok" if result.ok else "fail",
        tokens=result.tokens,
    )
    return result
```

**Benefits:**
- Specialized for inference tracking
- Token/latency metrics
- Model performance monitoring
- Cost tracking

### Decision Matrix

| Event Type | Function | When to Use |
|------------|----------|-------------|
| Cognition phase enter/exit | `phase_span` | All cognition phases |
| Structured data events | `emit_data` | State changes, metrics |
| Model inference | `emit_inference` | Runtime API calls |
| Infrastructure events | `log.info/warning/error` | Non-cognition events |
| Debug messages | `log.debug` | Development debugging |

### Log Level Guidelines

| Level | When to Use | Example |
|-------|-------------|---------|
| DEBUG | Detailed diagnostics | "Processing item 5 of 100" |
| INFO | Normal operations | "Starting pressure tick" |
| WARNING | Degraded but continuing | "Runtime unavailable, using fallback" |
| ERROR | Operation failed (non-fatal) | "Failed to write to disk" |
| CRITICAL | System-level failure | "Cannot start pressure loop" |

### Anti-Patterns

```python
# ❌ BAD: Using log for cognition phases
def run_phase():
    log.info("Starting phase")  # Should use phase_span
    result = execute()
    log.info("Phase completed")

# ❌ BAD: Using phase_span for infrastructure
def infra_task():
    with phase_span("infra", "task"):  # Should use log.info
        do_work()

# ❌ BAD: Emitting unstructured data
emit_data("module", "event", {"data": "unstructured string"})
# Should use structured key-value pairs
```

---

## Testing Standards

### Principle: Pytest for All Tests, Descriptive Names, Isolated State

### 1. Use Pytest Style

**Pattern:**
```python
import pytest

def test_subdomain_for_identity_omega_second_person():
    assert subdomain_for("what is your identity", 0) == 6

def test_subdomain_returns_correct_lobe():
    result = subdomain_for("quantum physics", 2)
    assert result in {0, 1, 2}  # Valid lobe IDs
```

**Benefits:**
- Standard tooling support
- Fixture ecosystem
- Parallel execution
- Rich assertion output

### 2. Descriptive Test Names

**Pattern:**
```python
# ✅ GOOD: Descriptive
def test_subdomain_for_identity_omega_second_person():
    assert subdomain_for("what is your identity", 0) == 6

# ❌ BAD: Vague
def test_subdomain():
    assert subdomain_for("what is your identity", 0) == 6
```

### 3. Test Isolation

**Pattern:**
```python
# ✅ GOOD: Isolated with fixtures
@pytest.fixture
def clean_memory_store():
    store = MemoryStore()
    yield store
    store.clear()  # Cleanup

def test_memory_write(clean_memory_store):
    clean_memory_store.write("test", "data")
    assert clean_memory_store.read("test") == "data"

# ❌ BAD: Shared state
STORE = MemoryStore()  # Shared across tests

def test_memory_write():
    STORE.write("test", "data")
    assert STORE.read("test") == "data"
```

### 4. Test Categories

Use pytest markers for test categories:

```python
import pytest

@pytest.mark.unit
def test_pure_function():
    assert add(1, 2) == 3

@pytest.mark.integration
def test_memory_write():
    store = MemoryStore()
    store.write("test", "data")

@pytest.mark.slow
def test_large_dataset():
    process_large_dataset()
```

---

## Type Hint Standards

### Principle: Type All Public APIs, Optional for Internal Helpers

### 1. Public APIs - Full Type Hints

**Pattern:**
```python
from typing import Optional

def read_memory(
    query: QuerySpec,
    *,
    cycle_id: str,
    max_results: int = 10,
) -> RetrievalResult:
    """Read memory with full type hints."""
    pass
```

### 2. Internal Helpers - Minimal Type Hints

**Pattern:**
```python
def _ep_id(episode) -> str:
    """Internal helper - minimal typing."""
    return str(getattr(episode, "episode_id", "") or "no-episode")
```

### 3. Use `from __future__ import annotations`

**Pattern:**
```python
from __future__ import annotations

# Allows forward references in type hints
def process(data: DataClass) -> ResultClass:
    pass
```

---

## Docstring Standards

### Principle: Purpose, API, Contracts, Architectural Rules

### 1. Module Docstrings

**Pattern:**
```python
"""
Module purpose — one-line summary.

Detailed description of what this module does.

Public API:
    function_name(param) -> return_type

Contract guarantees:
    - Precondition 1
    - Precondition 2

Architectural rules:
    - Rule 1
    - Rule 2

Error Handling:
    - Fail-hard for X
    - Fail-soft for Y
"""
```

### 2. Function Docstrings

**Pattern:**
```python
def function_name(param1: str, param2: int) -> bool:
    """One-line summary.

    Detailed description if needed.

    Args:
        param1: Description
        param2: Description

    Returns:
        Description of return value

    Raises:
        ValueError: When param1 is invalid
    """
```

---

## Configuration Standards

### Principle: Single Source of Truth per Domain

### 1. Configuration Hierarchy

```
config/
├── runtime.yaml          # Runtime endpoint configuration
├── pressure.yaml         # Pressure loop configuration
├── guardian_response_classes.yaml  # Guardian response classes
└── ...                    # One governed config per live subsystem

policy/
├── symphony.policy.yaml   # Explicit cascade/pressure overlay
└── shared_state.policy.yaml  # SharedState persistence policy
```

### 2. No Overlap

Each configuration domain should have a single source of truth. Avoid duplicating configuration across files.

---

## Migration Tracking

### Transitional Code Markers

For code marked as "transitional" or "TODO", track in `LOOP_REBUILD_LEDGER.md`:

```python
# NOTE: transitional home. This relocates as the per-layer loops/slices are moved
# into their own directories.
# See LOOP_REBUILD_LEDGER.md #MIGRATION-001
```

---

## Review Checklist

Before committing code, verify:

- [ ] Error handling follows doctrine (fail-hard vs fail-soft)
- [ ] Imports follow strategy (lazy vs top-level)
- [ ] Logging follows style guide (phase_span vs log vs emit_data)
- [ ] Tests use pytest style with descriptive names
- [ ] Public APIs have type hints
- [ ] Docstrings follow standards
- [ ] No configuration overlap
- [ ] Transitional code is tracked
- [ ] Security checks fail-closed
- [ ] Infrastructure failures fail-soft
