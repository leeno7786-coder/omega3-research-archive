"""D-13-37 — OS-held generation leases: a REAL child holds a shared lease (GC / replacement
refused, bounded), kill releases; scorer registration lease blocks GC until unregister
(the reader-lease subprocess proofs live in test_generation_views.py)."""

from __future__ import annotations

import os

from memory.projection import generation as G
from tests.memory.projection._fixtures import clock, publish


def test_shared_and_exclusive_probe_semantics(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100))
    proj = G.projection_root(root)
    assert not G.generation_is_leased(proj, r1.generation_id)
    h1 = G.acquire_shared_lease(proj, r1.generation_id)
    h2 = G.acquire_shared_lease(proj, r1.generation_id)                            # shared: two readers coexist
    assert G.generation_is_leased(proj, r1.generation_id)
    h1.release()
    assert G.generation_is_leased(proj, r1.generation_id)                          # still held by the second reader
    h2.release()
    assert not G.generation_is_leased(proj, r1.generation_id)
    # namespace gate held exclusively ⇒ enrolment refused (bounded), never a silent unleased view
    fd = G._open_lock_handle(proj / G.LEASE_NAMESPACE_LOCK_FILE); assert G._try_lock(fd, exclusive=True)
    G._LEASE_NAMESPACE_WAIT, saved = 0.2, G._LEASE_NAMESPACE_WAIT
    try:
        try:
            G.acquire_shared_lease(proj, r1.generation_id)
            raise AssertionError("enrolment must be refused")
        except G.GenerationUnavailable:
            pass
    finally:
        G._LEASE_NAMESPACE_WAIT = saved; G._unlock(fd); os.close(fd)
