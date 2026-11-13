"""Deterministic overlap at Program 0's real canonical read/copy seams."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from core import cognition_lock as locks, timekeeper as TK
from memory import encoding, retina
from memory.engine import validate_write as writer
from memory.projection import build as B, generation as G
from tests.helpers.blueprint_driver import _ModelFreeProjectionEncoder
from tests.helpers.memory_isolation import redirect_memory_to_tmp
from tests.memory.projection._fixtures import redirect_timekeeper, write_record


@pytest.fixture
def profile(tmp_path, monkeypatch):
    redirect_memory_to_tmp(tmp_path, monkeypatch, stub_encoding=False)
    redirect_timekeeper(tmp_path)
    monkeypatch.setattr(retina, "encoder", lambda: _ModelFreeProjectionEncoder())
    root = Path(writer.MEMORY_ROOT).parent
    write_record(root, "k1", title="Snapshot record", summary="A complete source-bound record.")
    seeded = root / "memory" / "200_knowledge" / "k1.json"
    meta = json.loads(seeded.read_text(encoding="utf-8"))
    meta["provenance"]["projection_receipt_required"] = True
    # Route the fixture through the real writer before either participant starts.
    target = Path(writer.atomic_write(meta, update_index=False, stamp_observed=False))
    assert target != seeded
    seeded.unlink()
    # The initial writer stamps provenance, so bind the fixture's receipt to
    # those exact bytes before exercising the production receipt finalizer.
    from memory.projection_receipt import receipt_path, write_projection_receipt

    receipt_path("k1", root=root).unlink()
    write_projection_receipt(meta, root=root)
    yield root, target, meta
    TK.reset_for_tests()


def _build(root):
    with G.projection_publication_authority(root) as authority:
        return B.build_generation(root, reason="snapshot-exclusion", authority=authority)


@pytest.mark.parametrize("boundary", ["capture_read", "capture_copy", "closure_read"])
def test_canonical_writer_waits_for_snapshot_io(profile, monkeypatch, boundary):
    root, target, meta = profile
    read_open = threading.Event()
    release_read = threading.Event()
    writer_progress = threading.Event()
    writer_waited = threading.Event()
    state = {"scan": 0, "gated": False}
    outcomes = {}
    initial_clock = TK.last_coverage_clock()
    real_open = Path.open
    real_scan = B.scan_canonical_membership
    real_link = B._link_or_copy
    real_emit = locks._emit_lock_event

    def wait_here():
        if state["gated"]:
            return
        state["gated"] = True
        read_open.set()
        assert release_read.wait(20), "controller did not release canonical snapshot read"

    def tracked_scan(memory_root):
        state["scan"] += 1
        return real_scan(memory_root)

    def opened(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        expected_scan = 1 if boundary == "capture_read" else 2
        if (threading.current_thread().name == "snapshot-reader"
                and path == target and boundary != "capture_copy"
                and state["scan"] == expected_scan and not state["gated"]):
            # Keep the actual Windows read handle open while the writer tries to enter.
            try:
                wait_here()
            except BaseException:
                handle.close()
                raise
        return handle

    def linked(src, dst):
        if boundary == "capture_copy" and src == target:
            wait_here()
        return real_link(src, dst)

    def emit(event, lock, **kwargs):
        if (event == "lock_wait" and lock is locks.commit_lock()
                and threading.current_thread().name == "snapshot-writer"):
            writer_waited.set()
            writer_progress.set()
        return real_emit(event, lock, **kwargs)

    monkeypatch.setattr(B, "scan_canonical_membership", tracked_scan)
    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(B, "_link_or_copy", linked)
    monkeypatch.setattr(locks, "_emit_lock_event", emit)

    def build():
        try:
            outcomes["candidate"] = _build(root)
        except BaseException as exc:
            outcomes["build_error"] = exc

    def finish_receipt():
        try:
            with locks.commit_lock():
                outcomes["crossed_snapshot"] = not release_read.is_set()
                outcomes["encoding"] = encoding._bind_claim_receipt(
                    meta, str(target), encoding.EncodingResult(ok=True)
                )
        except BaseException as exc:
            outcomes["write_error"] = exc
        finally:
            writer_progress.set()

    reader = threading.Thread(target=build, name="snapshot-reader")
    committer = threading.Thread(target=finish_receipt, name="snapshot-writer")
    reader.start()
    try:
        assert read_open.wait(20), outcomes
        committer.start()
        assert writer_progress.wait(20), "writer neither blocked nor completed"
    finally:
        release_read.set()
        reader.join(30)
        if committer.ident is not None:
            committer.join(30)
    assert not reader.is_alive() and not committer.is_alive()
    assert "write_error" not in outcomes, outcomes
    assert outcomes["encoding"].ok, outcomes["encoding"].failure_reason
    assert writer_waited.is_set(), "canonical writer entered while snapshot I/O was open"
    assert outcomes["crossed_snapshot"] is False
    assert "build_error" not in outcomes, outcomes
    candidate = outcomes["candidate"]
    assert isinstance(candidate, G.Candidate), candidate
    G.validate_candidate(candidate.candidate_dir, candidate.manifest)
    from memory.projection_receipt import verify_projection_receipt

    committed = json.loads(target.read_text(encoding="utf-8"))
    assert committed["provenance"]["projection_receipt_required"] is True
    assert verify_projection_receipt(committed, root=root).ok
    assert TK.last_coverage_clock() == initial_clock, "snapshot reads must not mint commit stamps"


def test_compilation_leaves_commit_lock_available(profile, monkeypatch):
    from memory.taxonomy import compiler

    root, _target, _meta = profile
    initial_clock = TK.last_coverage_clock()
    real_compile = compiler.compile_memory_graph
    acquired = []

    def compile_with_writer(**kwargs):
        def writer_attempt():
            lock = locks.commit_lock()
            held = lock.acquire(blocking=False)
            acquired.append(held)
            if held:
                lock.release()

        worker = threading.Thread(target=writer_attempt)
        worker.start()
        worker.join(10)
        assert not worker.is_alive()
        assert acquired[-1], "long compiler holds the canonical commit lock"
        return real_compile(**kwargs)

    monkeypatch.setattr(compiler, "compile_memory_graph", compile_with_writer)
    result = _build(root)
    assert isinstance(result, G.Candidate), result
    assert acquired
    assert TK.last_coverage_clock() == initial_clock


def test_captured_compiler_read_does_not_block_live_canonical_replacement(profile, monkeypatch):
    from memory.taxonomy import compiler

    root, target, meta = profile
    real_compile = compiler.compile_memory_graph
    outcomes = []
    compile_calls = []

    def compile_with_captured_reader(**kwargs):
        compile_calls.append(kwargs)
        if len(compile_calls) > 1:
            return real_compile(**kwargs)
        captured = Path(kwargs["source_root"]) / target.relative_to(root)
        assert captured.is_file()
        captured_bytes = captured.read_bytes()

        def complete_live_write():
            try:
                with locks.commit_lock():
                    meta["provenance"]["confidence"] = 0.6
                    outcomes.append(encoding._bind_claim_receipt(
                        meta, str(target), encoding.EncodingResult(ok=True)
                    ))
            except BaseException as exc:
                outcomes.append(exc)

        # The compiler reads its captured profile while another lane finishes
        # receipt binding on the live canonical path, using the real Windows IO.
        with captured.open("rb"):
            worker = threading.Thread(target=complete_live_write)
            worker.start()
            worker.join(10)
        assert not worker.is_alive(), "compiler prevented a concurrent canonical commit"
        assert outcomes and isinstance(outcomes[-1], encoding.EncodingResult), outcomes
        assert outcomes[-1].ok, outcomes[-1].failure_reason
        assert captured.read_bytes() == captured_bytes, "captured bytes changed with the live write"
        assert target.read_bytes() != captured_bytes
        return real_compile(**kwargs)

    monkeypatch.setattr(compiler, "compile_memory_graph", compile_with_captured_reader)
    result = _build(root)
    assert isinstance(result, G.Candidate), result
    assert outcomes
    assert len(compile_calls) == 2, "closure must reject the moved live snapshot and rebuild"
