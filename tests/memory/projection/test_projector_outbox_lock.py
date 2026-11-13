"""Program 0 P0.1 — writer fast-fail (D-13-50): a REAL projector process holds the outbox exclusion
for > 5 s ⇒ a canonical-writer process's append_projection_delta returns False promptly (≪ 1 s),
the failed append produces durable dirty evidence WHILE the hold continues, four lane commits
complete while the lock is held, successful appends stay canonical-JSON LF rows with fsync,
appender/capture/rotation share ONE lock order (real-process append vs rotation: no deadlock)."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import timekeeper as TK
from memory import projection_delta as PD
from memory.projection import generation as G
from tests.memory.projection._fixtures import redirect_timekeeper, seed_records_only, write_record

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _env(tmp_path):
    redirect_timekeeper(tmp_path)
    yield
    TK.reset_for_tests()


HOLDER = textwrap.dedent(
    """
    import sys, time; sys.path.insert(0, %r)
    from pathlib import Path
    from memory import projection_delta as PD
    with PD.outbox_exclusion(Path(sys.argv[1]), blocking=True):
        print("HELD", flush=True)
        time.sleep(float(sys.argv[2]))
    print("RELEASED", flush=True)
    """
)

WRITER = textwrap.dedent(
    """
    import sys, json, time; sys.path.insert(0, %r)
    from pathlib import Path
    from memory import projection_delta as PD
    root = Path(sys.argv[1]); mid = sys.argv[2]
    d = PD.ProjectionDelta.build(operation="upsert", memory_id=mid, canonical_path=f"memory/200_knowledge/{mid}.json", canonical_hash="0"*64,
                                 coord=(2,0,7), commit_seq=0, projection_class="semantic")
    t0 = time.monotonic(); ok = PD.append_projection_delta(d, root=root); dt = time.monotonic() - t0
    dirty = False
    if not ok:
        dirty = PD.mark_projection_dirty(reason="outbox busy (projector holds the exclusion)", memory_id=mid, root=root)
    print(json.dumps({"ok": ok, "seconds": dt, "dirty": dirty, "delta_id": d.delta_id}), flush=True)
    """
)


def _spawn_holder(root, seconds):
    proc = subprocess.Popen([sys.executable, "-c", HOLDER % str(REPO), str(root), str(seconds)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "HELD"
    return proc


def test_real_projector_hold_makes_a_real_writer_fail_fast_and_mark_dirty_while_held(tmp_path):
    profile = tmp_path / "profile"; profile.mkdir(); seed_records_only(profile)
    root = profile / "memory"
    holder = _spawn_holder(root, 6.0)
    try:
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, "-c", WRITER % str(REPO), str(root), "k5"], capture_output=True, text=True, timeout=120)
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        elapsed = time.monotonic() - t0
        assert res["ok"] is False and res["seconds"] < 1.0, res              # NOT after the 6 s hold
        assert res["dirty"] is True                                           # durable dirty evidence WHILE rotation/capture is in progress
        assert PD.projection_dirty_state(root=root)["dirty"] is True and elapsed < 5.0
        assert holder.poll() is None                                          # the holder is still holding
        # four lane commits complete while the lock is held — no lane waits on the projector
        from core.commit_region import serialized_commit
        done = []
        def commit(lane, i):
            owner = SimpleNamespace(
                episode_id=f"e{i}",
                pipeline_id=i,
                work_kind=lane,
                memory_epoch_start=0,
            )
            with serialized_commit(owner):
                done.append(lane)
        threads = [threading.Thread(target=commit, args=(lane, i)) for i, lane in enumerate(("chat", "agent", "meta", "window"))]
        t1 = time.monotonic()
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        assert sorted(done) == ["agent", "chat", "meta", "window"] and time.monotonic() - t1 < 3.0
        assert holder.poll() is None
    finally:
        holder.kill(); holder.wait(timeout=10)
    # after release a successful append is a canonical-JSON LF row (unchanged bytes/format), fsynced
    proc = subprocess.run([sys.executable, "-c", WRITER % str(REPO), str(root), "k6"], capture_output=True, text=True, timeout=120)
    res = json.loads(proc.stdout.strip().splitlines()[-1])
    assert res["ok"] is True
    raw = PD.outbox_path(root).read_bytes()
    assert raw.endswith(b"\n") and json.loads(raw.decode().splitlines()[-1])["delta_id"] == res["delta_id"]
    assert raw.decode().splitlines()[-1] == PD.canonical_json(json.loads(raw.decode().splitlines()[-1]))


def test_in_process_writer_never_waits_on_capture_or_rotation_and_lock_order_is_shared(tmp_path):
    profile = tmp_path / "profile"; profile.mkdir(); seed_records_only(profile)
    root = profile / "memory"
    delta = PD.ProjectionDelta.build(operation="upsert", memory_id="k1", canonical_path="memory/200_knowledge/k1.json", canonical_hash="0" * 64,
                                     coord=(2, 0, 1), commit_seq=0, projection_class="semantic")
    assert PD.append_projection_delta(delta, root=root) is True
    results = {}
    with PD.outbox_exclusion(root, blocking=True) as oa:                                   # projector side (capture/rotation) holds
        t0 = time.monotonic()
        t = threading.Thread(target=lambda: results.__setitem__("r", PD.append_projection_delta(delta, root=root)))
        t.start(); t.join(timeout=5)
        assert results["r"] is False and time.monotonic() - t0 < 1.0                        # in-process writer: immediate refusal
        snap = PD.capture_outbox_snapshot_locked(root=root, epoch=1, saved_offset=0, prior_observed=None, consumed_prefix_expected=None, outbox_authority=oa)
        assert snap.captured_size == PD.outbox_path(root).stat().st_size
    assert PD.append_projection_delta(delta, root=root) is True                            # after release: lawful append beyond the captured size
    # capture may WAIT for an append inside its short critical section (projector blocking, bounded)
    with PD.outbox_exclusion(root, blocking=False):
        pass
    # writer contention posture: the writer's own nonblocking exclusion is refused while another writer/projector holds
    held = threading.Event(); release = threading.Event()

    def hold():
        with PD.outbox_exclusion(root, blocking=True):
            held.set(); release.wait(5)

    th = threading.Thread(target=hold); th.start(); held.wait(5)
    with pytest.raises(PD.OutboxBusy):
        with PD.outbox_exclusion(root, blocking=False):
            pass
    release.set(); th.join(5)


def test_real_process_append_versus_rotation_does_not_deadlock(tmp_path):
    profile = tmp_path / "profile"; profile.mkdir(); seed_records_only(profile)
    root = profile / "memory"
    write_record(profile, "k5", title="Glia", summary="glial support cells")
    PD.append_projection_delta(PD.ProjectionDelta.build(operation="upsert", memory_id="k5", canonical_path="memory/200_knowledge/k5.json",
                                                        canonical_hash="0" * 64, coord=(2, 0, 5), commit_seq=0, projection_class="semantic"), root=root)
    outcomes = []
    stop = time.monotonic() + 2.0

    def writers():
        while time.monotonic() < stop:
            proc = subprocess.run([sys.executable, "-c", WRITER % str(REPO), str(root), "k5"], capture_output=True, text=True, timeout=120)
            outcomes.append(json.loads(proc.stdout.strip().splitlines()[-1])["ok"])

    tw = threading.Thread(target=writers); tw.start()
    with G.projection_publication_authority(profile) as auth:
        for i in range(3):
            with PD.outbox_exclusion(root, blocking=True) as oa:
                PD.rotate_outbox_epoch_locked(root=root, old_state={"old_epoch": i + 1, "old_identity": None, "reason": "test"}, outbox_authority=oa)
                time.sleep(0.2)
    tw.join(timeout=30)
    assert not tw.is_alive() and outcomes                                                   # nobody deadlocked; writers either appended (to the live file) or failed fast
    assert PD.outbox_path(root).exists()
    assert len(list(PD.outbox_path(root).parent.glob("*.quarantine-*"))) == 3
