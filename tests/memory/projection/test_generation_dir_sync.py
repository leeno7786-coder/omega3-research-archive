"""Cut 13 Task A2 — the `_sync_dir` platform contract (D-13-24): POSIX fsync of the
directory fd at every durability point; Windows branch via ctypes (mocked); any other
platform ⇒ refuse (never silently skip)."""

from __future__ import annotations

import os
import sys

import pytest

from memory.projection import generation as G
from tests.memory.projection._fixtures import clock, make_candidate


@pytest.mark.skipif(os.name != "posix", reason="exercises the POSIX directory-fd fsync branch; Windows takes _sync_dir_windows")
def test_posix_sync_dir_fsyncs_the_directory_fd(tmp_path, monkeypatch):
    fsynced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (fsynced.append(fd), real_fsync(fd))[1])
    assert G.sync_dir(tmp_path) == "posix_fsync"
    assert fsynced, "directory fd was not fsynced"


def test_sync_dir_called_at_every_publication_durability_point(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    calls: list[str] = []
    real = G.sync_dir
    monkeypatch.setattr(G, "sync_dir", lambda p: (calls.append(str(p)), real(p))[1])
    import memory.projection.registry_authority as RA
    monkeypatch.setattr(RA, "sync_dir", lambda p: (calls.append(str(p)), real(p))[1])
    cand = make_candidate(root, clock(100))
    proj = G.projection_root(root)
    with G.projection_publication_authority(root) as auth:
        G.publish_generation(root, cand, authority=auth)
    joined = calls
    assert any(c == str(cand.candidate_dir) for c in joined)                    # candidate synced (receipt + files)
    assert sum(1 for c in joined if c == str(proj)) >= 5                        # seal, intent, rename, row, manifest, intent clear
    assert any(c.endswith("registry_authority") for c in joined)                # authority store synced


def test_windows_branch_uses_flushfilebuffers(tmp_path, monkeypatch):
    calls = {}

    class K:
        def CreateFileW(self, *a):
            calls["create"] = a; return 42
        def FlushFileBuffers(self, h):
            calls["flush"] = h; return 1
        def CloseHandle(self, h):
            calls["close"] = h; return 1

    import ctypes
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", type("W", (), {"kernel32": K()})(), raising=False)
    assert G.sync_dir(tmp_path) == "windows_flushfilebuffers"
    assert calls["flush"] == 42 and calls["close"] == 42


def test_unavailable_platform_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "java")
    monkeypatch.setattr(sys, "platform", "jython")
    with pytest.raises(G.GenerationIntegrityError):
        G.sync_dir(tmp_path)
