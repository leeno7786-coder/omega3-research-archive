"""Cut 13 Task A3/A3b — GenerationView (D-13-2), the reentrant projection-read
transaction (D-13-19/28), OS-held shared leases (D-13-37/75) incl. REAL subprocesses,
read-side immutability (never repair / rebuild / write)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from memory.projection import generation as G
from tests.memory.projection._fixtures import ARTIFACT_BYTES, clock, dir_state, make_candidate, publish

REPO = Path(__file__).resolve().parents[3]


def _proj(root):
    return G.projection_root(root)


def test_manifest_state_modes(tmp_path):
    root = tmp_path / "mem"
    proj = _proj(root); proj.mkdir(parents=True)
    assert G.manifest_state(proj).mode == "legacy"
    r = publish(root, clock(100))
    st = G.manifest_state(proj)
    assert st.mode == "generation" and st.manifest.generation_id == r.generation_id
    (proj / G.MANIFEST_FILE).write_bytes(b"{corrupt")
    st = G.manifest_state(proj)
    assert st.mode == "unavailable" and "manifest_invalid" in st.reason        # invalid ≠ legacy (D-13-16)


def test_view_captures_one_generation_and_holds_a_shared_lease(tmp_path):
    root = tmp_path / "mem"
    r = publish(root, clock(100))
    proj = _proj(root)
    with G.projection_read_transaction(root) as ctx:
        assert ctx.mode == "generation" and ctx.view.generation_id == r.generation_id
        view = ctx.view
        assert view.graph_sqlite == proj / f"gen-{r.generation_id}" / "graph.sqlite"
        assert set(view.plane_paths()) == {"hop_plane_1", "hop_plane_2"}
        assert G.generation_is_leased(proj, r.generation_id)                    # lease file present + shared lock held
        assert view.read_bytes("sram.bin") == ARTIFACT_BYTES["sram.bin"]
        # G+1 publishes mid-read: the in-flight transaction stays on G
        r2 = publish(root, clock(101), compiler_version="v8")
        assert G.current_view() is view and view.generation_id == r.generation_id
        with G.projection_read_transaction(root) as inner:                       # reentrant: same context, same view
            assert inner is ctx and inner.depth == 1 and inner.view is view
        assert ctx.depth == 0
        assert view.read_bytes("sram.bin") == ARTIFACT_BYTES["sram.bin"]         # G's bytes, still readable (leased ⇒ not GC'd)
    assert not G.generation_is_leased(proj, r.generation_id)                    # released after the transaction
    with G.projection_read_transaction(root) as ctx2:                            # a NEW transaction is on G+1
        assert ctx2.view.generation_id == r2.generation_id


def test_resolve_view_or_legacy_modes(tmp_path):
    root = tmp_path / "mem"
    _proj(root).mkdir(parents=True)
    assert G.resolve_view_or_legacy(root) == ("legacy", None)                   # no manifest ⇒ legacy names
    publish(root, clock(100))
    with pytest.raises(G.GenerationViewRequired):                                # manifest + no ambient view ⇒ refused
        G.resolve_view_or_legacy(root)
    with G.projection_read_transaction(root) as ctx:
        mode, view = G.resolve_view_or_legacy(root)
        assert mode == "generation" and view is ctx.view
    (_proj(root) / G.MANIFEST_FILE).write_bytes(b"{corrupt")
    with pytest.raises(G.GenerationUnavailable):
        G.resolve_view_or_legacy(root)
    with G.projection_read_transaction(root) as ctx:                             # unavailable lane: yields, never aborts
        assert ctx.mode == "unavailable" and ctx.view is None and ctx.reason
        with pytest.raises(G.GenerationUnavailable):
            G.resolve_view_or_legacy(root)


def test_reads_verify_bytes_and_never_repair(tmp_path):
    root = tmp_path / "mem"
    r = publish(root, clock(100))
    proj = _proj(root)
    gen_dir = proj / f"gen-{r.generation_id}"
    with G.projection_read_transaction(root) as ctx:
        view = ctx.view
        (gen_dir / "sram.bin").write_bytes(b"corrupt")
        (gen_dir / "planes" / "hop_plane_1.bin").unlink()
        (gen_dir / "graph_snapshot.json").write_bytes(b"{}")
        before = dir_state(gen_dir)
        with pytest.raises(G.GenerationUnavailable):
            view.read_bytes("sram.bin")
        with pytest.raises(G.GenerationUnavailable):
            view.verified_path("hop_plane_1")
        with pytest.raises(G.GenerationUnavailable):
            view.read_bytes("graph_snapshot.json")
        assert dir_state(gen_dir) == before                                      # nothing created / repaired by the reader
        assert view.verified_path("graph.sqlite").is_file()
        (gen_dir / "graph.sqlite").write_bytes(b"corrupt-different-size")
        with pytest.raises(G.GenerationUnavailable):
            view.verified_path("graph.sqlite")                                   # size change ⇒ re-verified ⇒ corrupt


def test_missing_receipt_or_directory_makes_the_lane_unavailable_not_legacy(tmp_path):
    root = tmp_path / "mem"
    r = publish(root, clock(100))
    proj = _proj(root)
    (proj / f"gen-{r.generation_id}" / G.RECEIPT_FILE).unlink()
    st = G.manifest_state(proj)
    assert st.mode == "unavailable" and st.reason == "generation_receipt_missing"
    import shutil
    shutil.rmtree(proj / f"gen-{r.generation_id}")
    assert G.manifest_state(proj).reason == "generation_directory_missing"


CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    sys.path.insert(0, %r)
    from memory.projection import generation as G
    root = Path(sys.argv[1])
    with G.projection_read_transaction(root) as ctx:
        print("LEASED", ctx.view.generation_id, flush=True)
        time.sleep(60)
    """
)


def test_real_child_reader_lease_blocks_gc_and_replacement_until_released(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = _proj(root)
    child = subprocess.Popen([sys.executable, "-c", CHILD % str(REPO), str(root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = child.stdout.readline().strip()
    assert line.startswith("LEASED")
    try:
        assert G.generation_is_leased(proj, r1.generation_id)
        publish(root, clock(101), compiler_version="v8")
        publish(root, clock(102), compiler_version="v9")
        assert (proj / f"gen-{r1.generation_id}").is_dir()                          # GC deferred while the child reads G1
        # same-id repair of the LEASED generation is refused, bounded
        (proj / f"gen-{r1.generation_id}" / "sram.bin").write_bytes(b"corrupt")
        cand = make_candidate(root, clock(103))
        G.LEASE_WAIT_SECONDS, saved = 0.3, G.LEASE_WAIT_SECONDS
        try:
            with G.projection_publication_authority(root) as auth:
                with pytest.raises(G.PublicationBlockedOnLeases):
                    G.publish_generation(root, cand, authority=auth)
        finally:
            G.LEASE_WAIT_SECONDS = saved
    finally:
        child.kill(); child.wait(timeout=10)
    deadline = time.monotonic() + 5
    while G.generation_is_leased(proj, r1.generation_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not G.generation_is_leased(proj, r1.generation_id)                     # killing the child releases deterministically
    publish(root, clock(104), compiler_version="v10")
    assert not (proj / f"gen-{r1.generation_id}").exists()                        # GC proceeds


def test_reset_holding_the_namespace_gate_refuses_new_enrolment(tmp_path):
    root = tmp_path / "mem"
    publish(root, clock(100))
    proj = _proj(root)
    fd = G._open_lock_handle(proj / G.LEASE_NAMESPACE_LOCK_FILE)
    assert G._try_lock(fd, exclusive=True)
    G._LEASE_NAMESPACE_WAIT, saved = 0.2, G._LEASE_NAMESPACE_WAIT
    try:
        with G.projection_read_transaction(root) as ctx:                             # yields unavailable, never blocks forever
            assert ctx.mode == "unavailable" and "namespace" in ctx.reason
    finally:
        G._LEASE_NAMESPACE_WAIT = saved
        G._unlock(fd); os.close(fd)
    with G.projection_read_transaction(root) as ctx:
        assert ctx.mode == "generation"
