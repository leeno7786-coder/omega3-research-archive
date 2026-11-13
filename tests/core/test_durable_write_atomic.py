"""I9 — atomic store writes: a crash mid-write must never truncate the target.

The deepsleep consolidation stores load fail-hard (corrupt JSON -> CRASH by
doctrine), so an in-place truncate-then-write that is killed mid-flight leaves a
truncated file that permanently bricks boot. write_text_atomic replaces the file
via a temp sibling + os.replace so the prior complete file always survives.
"""

from __future__ import annotations

import threading

import pytest


def test_write_text_atomic_writes_and_replaces(tmp_path):
    from pressure_loop.durable_write import write_text_atomic

    p = tmp_path / "store.json"
    p.write_text("OLD", encoding="utf-8")
    write_text_atomic(p, "NEW")
    assert p.read_text(encoding="utf-8") == "NEW"
    # no temp sibling left behind after a successful replace
    assert not list(tmp_path.glob("store.json.tmp*"))


def test_write_text_atomic_preserves_original_on_crash(tmp_path, monkeypatch):
    """A crash at the replace step leaves the prior complete file intact (never a
    truncated half-write a fail-hard loader would brick boot on); temp is cleaned."""
    from pressure_loop import durable_write

    p = tmp_path / "store.json"
    p.write_text("OLD-COMPLETE", encoding="utf-8")

    def boom(*_a, **_k):
        raise RuntimeError("crash before replace")

    monkeypatch.setattr(durable_write.os, "replace", boom)
    with pytest.raises(RuntimeError):
        durable_write.write_text_atomic(p, "NEW")
    assert p.read_text(encoding="utf-8") == "OLD-COMPLETE"
    assert not list(tmp_path.glob("store.json.tmp*"))


def test_write_text_atomic_concurrent_writers_use_independent_temp_files(tmp_path, monkeypatch):
    """Same-process writers must not collide on the temp source path.

    DeepSleep store writes can be triggered by concurrent HUD/runtime work in the
    same Python process. If two calls share ``<target>.tmp-<pid>``, one replace
    can consume the temp file while the other call is still trying to replace it.
    """
    from pressure_loop import durable_write

    p = tmp_path / "store.json"
    p.write_text("OLD", encoding="utf-8")

    real_replace = durable_write.os.replace
    replace_barrier = threading.Barrier(2)
    first_attempt_synced = threading.Event()

    def synchronized_replace(src, dst):
        # Force the FIRST attempt from each thread to land at the same instant (the race
        # window that triggers the Windows MoveFileEx PermissionError). A production RETRY
        # by the loser must NOT re-sync — its partner has already finished, so a second
        # barrier wait would deadlock until timeout. Sync once, then let retries through.
        if not first_attempt_synced.is_set():
            try:
                replace_barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
            first_attempt_synced.set()
        return real_replace(src, dst)

    monkeypatch.setattr(durable_write.os, "replace", synchronized_replace)

    errors = []

    def write_payload(payload: str) -> None:
        try:
            durable_write.write_text_atomic(p, payload)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=write_payload, args=(f"NEW-{idx}",)) for idx in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    assert p.read_text(encoding="utf-8") in {"NEW-0", "NEW-1"}
    assert not list(tmp_path.glob("store.json.tmp*"))
