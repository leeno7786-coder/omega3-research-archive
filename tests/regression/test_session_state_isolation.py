"""Every suite's reflection-window state resolves outside the operator's live tree.

WO-01 recorded gap I1: ``pressure_loop/reflection_window.py`` resolves its state through its OWN
``_repo_root()``, which ``GUARDIAN_STATE_ROOT`` redirects never reach. The session fixture in
``tests/conftest.py`` redirects the window, trajectory and archive paths, and staging follows the
window, but the reflection LEASE (the crash marker written while a window reflection is in flight)
was only redirected inside ``tests/continuity``. Everywhere else a test that ran a window reflection
wrote the live ``guardian/state/reflection_lease.json``, and a test that died mid-reflection could
leave a crash marker behind for the live brain to find at boot.

This suite has no conftest of its own, so it sees exactly what the session fixture provides.
Resolution only — nothing is written.
"""

from __future__ import annotations

from pathlib import Path


def test_no_reflection_window_state_path_resolves_into_the_live_tree():
    from core.paths import OMEGA_ROOT
    from pressure_loop import reflection_window as rw

    live = Path(OMEGA_ROOT).resolve()
    paths = {
        "reflection_window_path": rw.reflection_window_path(),
        "staged_window_path": rw.staged_window_path(),
        "current_trajectory_path": rw.current_trajectory_path(),
        "reflection_window_archive_dir": rw.reflection_window_archive_dir(),
        "reflection_lease_path": rw.reflection_lease_path(),
    }
    leaking = {name: str(p) for name, p in paths.items() if Path(p).resolve().is_relative_to(live)}
    assert not leaking, f"reflection-window state resolves into the live tree: {leaking}"


def test_proactive_chat_store_resolves_outside_the_live_tree():
    """A boot-greeting test wrote a pending greeting into the live proactive store (2026-09-25)."""
    from core.paths import OMEGA_ROOT
    from pressure_loop import proactive_chat_messages as pcm

    store = Path(pcm._store_path()).resolve()
    assert not store.is_relative_to(Path(OMEGA_ROOT).resolve()), f"proactive store is live: {store}"
