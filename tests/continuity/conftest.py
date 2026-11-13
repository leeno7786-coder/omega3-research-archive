"""Isolation for the episode-continuity contract suite (WO-02).

Reuses the repository's shared redirect (`tests/helpers/memory_isolation.py`) rather than
adding a second one, and closes the gap WO-01 measured on top of it:
`pressure_loop/reflection_window.py` resolves `reflection_window_path()` and
`staged_window_path()` through its OWN `_repo_root()`, not
`core.paths.GUARDIAN_STATE_ROOT`, so neither the session conftest nor
`redirect_memory_to_tmp` reaches them. A contract test that constructs a window record
must never be one bug away from the operator's live reflection state.

See BOUNDARY_MAP.md section 7, gap I1.
"""

from __future__ import annotations

from typing import Generator

import pytest

from tests.helpers.memory_isolation import redirect_memory_to_tmp


@pytest.fixture(autouse=True)
def _isolate_canonical_memory_for_every_suite(tmp_path, monkeypatch):
    """Override the parent suite's encoding stub for real continuity commits."""
    # Continuity retries verify the durable canonical claim. Keep the real
    # encoding step that issues its receipt; an OK-only stub is not a commit.
    redirect_memory_to_tmp(tmp_path, monkeypatch, stub_encoding=False)


@pytest.fixture(autouse=True)
def _isolate_continuity_state(tmp_path, monkeypatch) -> Generator[None, None, None]:
    import pressure_loop.reflection_window as reflection_window

    state = tmp_path / "guardian" / "state"
    state.mkdir(parents=True, exist_ok=True)
    window_path = state / "reflection_window.json"
    monkeypatch.setattr(reflection_window, "reflection_window_path", lambda: window_path)
    monkeypatch.setattr(
        reflection_window,
        "staged_window_path",
        lambda path=None: state / "reflection_window_staged.json",
    )
    monkeypatch.setattr(
        reflection_window, "reflection_lease_path", lambda: state / "reflection_lease.json"
    )
    monkeypatch.setattr(
        reflection_window, "current_trajectory_path", lambda: state / "current_trajectory.json"
    )
    yield
