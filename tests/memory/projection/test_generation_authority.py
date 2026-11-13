"""Cut 13 Task A2 — the ONE OS-held publication authority (D-13-9/33): one open handle,
exclusive nonblocking, no age reclamation, released by the OS on close/death; a REAL
second process is refused at every stage; every stage asserts the same token."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from memory.projection import generation as G
from tests.memory.projection._fixtures import clock, make_candidate

REPO = Path(__file__).resolve().parents[3]

CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    sys.path.insert(0, %r)
    from memory.projection import generation as G
    root = Path(sys.argv[1]); mode = sys.argv[2]
    if mode == "hold":
        with G.projection_publication_authority(root):
            print("HELD", flush=True)
            time.sleep(60)
    else:
        try:
            with G.projection_publication_authority(root):
                print("ACQUIRED", flush=True)
        except G.PublicationAuthorityBusy:
            print("BUSY", flush=True)
    """
)


def _child(root: Path, mode: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", CHILD % str(REPO), str(root), mode],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_second_in_process_acquisition_is_refused_and_released_on_exit(tmp_path):
    root = tmp_path / "mem"
    with G.projection_publication_authority(root) as auth:
        assert auth.live and (G.projection_root(root) / G.PUBLICATION_LOCK_FILE).exists()
        with pytest.raises(G.PublicationAuthorityBusy):
            with G.projection_publication_authority(root):
                pass
    assert not auth.live
    with G.projection_publication_authority(root):
        pass


def test_no_age_reclamation_a_held_lock_is_never_reclaimed(tmp_path):
    root = tmp_path / "mem"
    proj = G.projection_root(root)
    with G.projection_publication_authority(root):
        lock = proj / G.PUBLICATION_LOCK_FILE
        old = time.time() - 700                              # "held > 600 s" — the compile_lock pattern would reclaim; we never do
        os.utime(lock, (old, old))
        with pytest.raises(G.PublicationAuthorityBusy):
            with G.projection_publication_authority(root):
                pass


def test_real_second_process_refused_while_held_then_acquires_after_release(tmp_path):
    root = tmp_path / "mem"
    G.projection_root(root).mkdir(parents=True)
    with G.projection_publication_authority(root):
        p = _child(root, "try")
        out, _ = p.communicate(timeout=30)
        assert out.strip() == "BUSY"
    p = _child(root, "try")
    out, _ = p.communicate(timeout=30)
    assert out.strip() == "ACQUIRED"


def test_killing_the_holder_releases_the_os_lock(tmp_path):
    root = tmp_path / "mem"
    G.projection_root(root).mkdir(parents=True)
    holder = _child(root, "hold")
    assert holder.stdout.readline().strip() == "HELD"
    with pytest.raises(G.PublicationAuthorityBusy):
        with G.projection_publication_authority(root):
            pass
    holder.kill(); holder.wait(timeout=10)
    with G.projection_publication_authority(root):
        pass


def test_every_stage_asserts_the_same_live_token(tmp_path):
    root = tmp_path / "mem"
    cand = make_candidate(root, clock(100))
    with G.projection_publication_authority(root) as auth:
        for bad in (None, object()):
            with pytest.raises(G.PublicationAuthorityRequired):
                G.recover_generation_intents(root, authority=bad)
            with pytest.raises(G.PublicationAuthorityRequired):
                G.publish_generation(root, cand, authority=bad)
        from memory.projection import registry_authority as RA
        with pytest.raises(G.PublicationAuthorityRequired):
            RA.persist_authority(G.projection_root(root), b"{}", authority=None, introduced_by_candidate_id=None, selected_at="t")
        res = G.publish_generation(root, cand, authority=auth)
        assert res.outcome == "published_new"
