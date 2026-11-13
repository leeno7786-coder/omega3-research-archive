"""Single-instance boot — kill any prior substrate, then take the lock.

Operator directive 2026-05-31: every boot starts fresh by reaping prior
substrates (process-only; never a state wipe). Refuse (BootAlreadyRunning)
only if a prior substrate resists the kill.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.boot_lock as bl  # noqa: E402


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    p = tmp_path / "boot.lock"
    monkeypatch.setattr(bl, "BOOT_LOCK_PATH", p, raising=True)
    return p


@pytest.fixture(autouse=True)
def _no_real_kills(monkeypatch):
    """Default: discover no other substrates and never actually kill.
    Individual tests override these to exercise the reap paths."""
    monkeypatch.setattr(bl, "_scan_substrate_pids", lambda protected: [])
    # _kill_pid is only called when a target exists; default is a no-op True.
    monkeypatch.setattr(bl, "_kill_pid", lambda pid: True)


def test_acquire_on_clean_state_writes_our_pid(lock_path):
    rec = bl.acquire()
    assert rec["pid"] == os.getpid()
    assert lock_path.exists()
    on_disk = json.loads(lock_path.read_text(encoding="utf-8"))
    assert on_disk["pid"] == os.getpid()
    bl.release()
    assert not lock_path.exists()


def test_reacquire_same_pid_is_idempotent(lock_path):
    bl.acquire()
    rec2 = bl.acquire()  # same process, second call
    assert rec2["pid"] == os.getpid()
    assert lock_path.exists()
    bl.release()


def test_live_other_pid_is_KILLED_then_acquired(lock_path, monkeypatch):
    # A prior substrate (different live pid) is reaped, and we take over.
    other = os.getpid() + 1
    lock_path.write_text(
        json.dumps(
            {
                "pid": other,
                "create_time": None,
                "acquired_at": "x",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bl, "_pid_alive", lambda pid, ct=None: pid == other)  # the owner is alive
    killed = []
    monkeypatch.setattr(bl, "_kill_pid", lambda pid: killed.append(pid) or True)
    rec = bl.acquire()
    assert other in killed  # we tried to kill the prior one
    assert rec["pid"] == os.getpid()  # and took the lock
    assert json.loads(lock_path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    bl.release()


def test_scan_discovered_orphan_is_killed(lock_path, monkeypatch):
    # An orphan boot.py with NO lock entry, found by the cwd scan, is reaped.
    orphan = os.getpid() + 7
    monkeypatch.setattr(bl, "_scan_substrate_pids", lambda protected: [orphan])
    killed = []
    monkeypatch.setattr(bl, "_kill_pid", lambda pid: killed.append(pid) or True)
    rec = bl.acquire()
    assert orphan in killed
    assert rec["pid"] == os.getpid()
    bl.release()


def test_survivor_refuses(lock_path, monkeypatch):
    # If a prior substrate will not die, refuse rather than double-boot.
    survivor = os.getpid() + 3
    monkeypatch.setattr(bl, "_scan_substrate_pids", lambda protected: [survivor])
    monkeypatch.setattr(bl, "_kill_pid", lambda pid: False)  # kill fails
    with pytest.raises(bl.BootAlreadyRunning):
        bl.acquire()


def test_release_does_not_delete_another_owners_lock(lock_path):
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid() + 1,
                "create_time": None,
                "acquired_at": "x",
            }
        ),
        encoding="utf-8",
    )
    bl.release()  # we don't own it
    assert lock_path.exists()  # left intact


def test_reap_returns_killed_and_survivors(monkeypatch, lock_path):
    monkeypatch.setattr(bl, "_scan_substrate_pids", lambda protected: [11, 22, 33])
    monkeypatch.setattr(bl, "_pid_alive", lambda pid, ct=None: False)
    monkeypatch.setattr(bl, "_kill_pid", lambda pid: pid != 22)  # 22 survives
    out = bl.reap_other_substrates()
    assert out["killed"] == [11, 33]
    assert out["survivors"] == [22]


def test_protected_pids_includes_self():
    assert os.getpid() in bl._protected_pids()


def test_pid_alive_zero_is_dead():
    assert bl._pid_alive(0) is False
    assert bl._pid_alive(-1) is False


def test_pid_alive_self_is_true():
    assert bl._pid_alive(os.getpid()) is True
