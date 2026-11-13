"""A compile lock whose holder is dead is reclaimed at once (live 2026-09-06).

The brain was force-killed mid-compile, its pid stayed in .compile.lock, and the
next boot died on the 120 s lock timeout — the age rule alone needs ten minutes.
"""

from __future__ import annotations

import os
import time

import pytest

from memory.taxonomy import compile_lock as cl


def _dead_pid() -> int:
    pid = 4_000_000
    while cl._pid_alive(pid):
        pid += 7
    return pid


def test_a_lock_held_by_a_dead_process_is_reclaimed(tmp_path, monkeypatch):
    monkeypatch.setattr(cl.P, "compiled_root", lambda root=None: tmp_path)
    lock = cl.lock_path(tmp_path)
    lock.write_text(str(_dead_pid()), encoding="ascii")
    started = time.monotonic()
    with cl.compile_lock(tmp_path, timeout_s=5.0):
        assert lock.read_text(encoding="ascii").strip() == str(os.getpid())
    assert time.monotonic() - started < 2.0
    assert not lock.exists()


def test_a_lock_held_by_a_live_process_is_respected(tmp_path, monkeypatch):
    monkeypatch.setattr(cl.P, "compiled_root", lambda root=None: tmp_path)
    lock = cl.lock_path(tmp_path)
    # The detached full-suite runner may outlive its launcher, so the parent
    # process is not a stable live-holder fixture. The pytest process itself is.
    lock.write_text(str(os.getpid()), encoding="ascii")
    lock_mtime = lock.stat().st_mtime
    with pytest.raises(TimeoutError):
        with cl.compile_lock(tmp_path, timeout_s=0.3):
            pass
    assert lock.exists() and lock.stat().st_mtime == lock_mtime


def test_our_own_pid_is_alive():
    assert cl._pid_alive(os.getpid()) is True
    assert cl._pid_alive(_dead_pid()) is False
