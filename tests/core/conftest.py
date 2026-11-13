"""Shared fixtures for rebuild-focused core tests.

``guardian.terminal_feedback`` was removed from the product tree; these tests
must not resurrect it. Install a no-op stub in ``sys.modules`` before any
import of ``memory.api`` or ``runtime.runtime_capability_broker`` preflight paths.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest


def _install_terminal_feedback_stub() -> None:
    if "guardian.terminal_feedback" in sys.modules:
        return

    import importlib

    # Bind the REAL guardian package (repo_root/guardian), displacing any namespace
    # package formed from tests/guardian (which has no __init__.py): during early
    # conftest import only tests/ is on sys.path, so an unguarded `import guardian`
    # caches a namespace shadow and every guardian.* submodule becomes unimportable.
    _repo_root = str(Path(__file__).resolve().parents[2])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    _g = sys.modules.get("guardian")
    if _g is not None and getattr(_g, "__file__", None) is None:
        sys.modules.pop("guardian", None)
    importlib.import_module("guardian")

    try:
        importlib.import_module("guardian.terminal_feedback")
        return
    except ImportError:
        pass

    mod = ModuleType("guardian.terminal_feedback")

    def emit_phase(*_a, **_k) -> None:
        return None

    def emit_signal(*_a, **_k) -> None:
        return None

    def emit_memory(*_a, **_k) -> None:
        return None

    @contextmanager
    def phase_span(*_a, **_k):
        class _Span:
            def set(self, **_kw) -> None:
                pass

            def skip(self, _summary: str) -> None:
                pass

        yield _Span()

    mod.emit_phase = emit_phase
    mod.emit_signal = emit_signal
    mod.emit_memory = emit_memory
    mod.phase_span = phase_span
    mod._publish_event = lambda _e: None

    sys.modules["guardian.terminal_feedback"] = mod
    setattr(sys.modules["guardian"], "terminal_feedback", mod)


_install_terminal_feedback_stub()


@pytest.fixture(autouse=True)
def _isolate_active_motor_planning(request, monkeypatch):
    """Keep legacy cascade tests independent from execution-contract binding.

    Tests named ``test_motor_*`` (plus the emergent-pressure transport suite)
    exercise execution contracts explicitly.  Older cascade-shape tests build
    hermetic stub packs; projecting those into motor programs would change what
    AUTO and the foldback validate.  Disable only the contract module's alias for
    those non-motor tests—the governed policy functions remain unmodified and
    policy-wiring tests still inspect the real config.
    """
    basename = request.node.fspath.basename if request.node.fspath else ""
    if basename.startswith("test_motor_") or basename == "test_emergent_pressure_transport.py":
        yield
        return
    monkeypatch.setattr("meta_loop.execution_contract.motor_programs_enabled", lambda *_a: False)
    yield


def _redirect_memory_to_tmp(tmp_path, monkeypatch) -> None:
    """Hermetic memory store — never touch the real memory/ tree."""
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    redirect_memory_to_tmp(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def _isolate_memory_root(request, tmp_path, monkeypatch):
    """Isolate memory writes per test — no real lobe-9 persistence or :11434 embed on episode close."""
    basename = request.node.fspath.basename if request.node.fspath else ""
    _redirect_memory_to_tmp(tmp_path, monkeypatch)
    yield


@pytest.fixture(autouse=True)
def _reset_memory_salience():
    from autonomic.symphony_neuromod import set_current_memory_salience

    set_current_memory_salience(None)
    yield
    set_current_memory_salience(None)


@pytest.fixture(autouse=True)
def _isolate_omega_overlays():
    """Per-test Omega overlay stores — a setpoint/decision leaking across tests
    (the shadow writes in _apply_omega_episode_result populate both process-wide
    stores) would otherwise survive. Defense-in-depth alongside the per-step fixtures."""
    from pressure_loop import pressure as _pressure

    _pressure.reset_mind_primacy()
    yield
    _pressure.reset_mind_primacy()


@pytest.fixture(autouse=True)
def _isolate_proactive_lifecycle(tmp_path, monkeypatch):
    """Keep durable proactive policy events out of the live guardian state."""
    import pressure_loop.proactive_lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle,
        "_ledger_path",
        lambda: tmp_path / "proactive_lifecycle.jsonl",
    )
    lifecycle._reset_cache_for_tests()
    yield
    lifecycle._reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _isolate_operator_queue(tmp_path, monkeypatch):
    """Per-test operator input queue box — a queued input leaking across tests
    would make any later evaluate() pop an unexpected operator episode."""
    import pressure_loop.cognition_queue as queue_mod

    monkeypatch.setattr(queue_mod, "_queue_path", lambda: tmp_path / "cognition_queue.json")
    queue_mod._reset_cache_for_tests()
    yield
    queue_mod._reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _isolate_system_messages(tmp_path, monkeypatch):
    """Per-test text app store (system queries to the operator)."""
    import pressure_loop.system_messages as sm

    monkeypatch.setattr(sm, "_store_path", lambda: tmp_path / "system_messages.json")
    sm._reset_cache_for_tests()
    yield
    sm._reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _isolate_reflection_window(tmp_path, monkeypatch):
    """Isolate the reflection-window + trajectory on-disk state per test.

    ``EpisodeStore()`` with no path args falls back to the shared
    ``guardian/state/reflection_window.json``. Without isolation, episodes
    ignited across the WHOLE test session accumulate into one on-disk window;
    when the 10th lands, ``ignite_episode`` synchronously fires the Omega
    cascade — which, with no model in a unit test, fails hard (by design). Point
    the window + trajectory at a per-test tmp dir so cross-test accumulation can
    never trip Omega. Tests that pass EXPLICIT paths are unaffected (explicit
    path wins over this module-level fallback). Production uses the real path.
    """
    import pressure_loop.reflection_window as rw

    window = tmp_path / "reflection_window.json"
    traj = tmp_path / "current_trajectory.json"
    monkeypatch.setattr(rw, "reflection_window_path", lambda: window)
    monkeypatch.setattr(rw, "current_trajectory_path", lambda: traj)
    yield
