"""WO-09 — a window hydrates the exact canonical revisions it was sealed with.

Before this order ``pressure_loop/temporal_store.hydrate_reflection_window`` scanned today's
lobe-9 subdomain and kept, per episode id, whichever document had the greatest
``(closed_at, id)``. So a sealed window did not reflect on what it was sealed with: any later
document naming one of its episodes silently replaced that member's evidence
(BOUNDARY_MAP L12). Hydration now reads each member by the ``CanonicalRef`` the commit region
pinned when the member joined, and verifies the content hash before anything downstream sees it.

Acceptance case covered here:

* **Exact hydration** — a duplicate memory version resolves only to the manifest-pinned
  ``CanonicalRef``, never to latest-by-clock; the canonical reference travels with the derived
  evidence.
"""

from __future__ import annotations

import json

import pytest

from pressure_loop.episode import EpisodeStore
from schema.continuity_json import CanonicalRef
from tests.helpers import continuity_terminus as ct
from tests.helpers import continuity_window as cw

_LATER = "LATER-DUPLICATE-VERSION"


@pytest.fixture(autouse=True)
def _terminus(tmp_path, monkeypatch):
    from memory import episode_sram

    monkeypatch.setenv("OMEGA_WINDOW_LANE", "1")
    ct.isolate(monkeypatch, tmp_path)
    yield
    episode_sram.reset()


def _sealed_cohort():
    m = cw.membership()
    store = EpisodeStore()
    carriers = cw.close_cohort(store)
    staged = cw.staged_cohort()
    return store, carriers, staged, m.seal_window(staged, snapshot=m.window_snapshot(staged))


def _write_later_duplicate(record_id: str) -> str:
    """A second lobe-9 document naming the same episode, closed LATER, with other content.

    This is the shape latest-by-clock hydration could not survive: nothing about it is invalid,
    it simply is not the revision the window was sealed with.
    """
    from core import paths as core_paths
    from memory.engine.reindex import build_index
    from memory.projection_delta import mark_projection_dirty

    original = cw.canonical_document_path(record_id)
    document = json.loads(original.read_text(encoding="utf-8"))
    body = json.loads(document["text"])
    body["duplicate_marker"] = _LATER
    body["episode_record"]["identity"]["thread_id"] = "thread-later-version"
    duplicate_id = f"{record_id}-later"
    document["id"] = duplicate_id
    document["text"] = json.dumps(body)
    enrichment = dict(document.get("enrichment") or {})
    enrichment["closed_at"] = "2099-01-01T00:00:00.000000+00:00"
    document["enrichment"] = enrichment
    (original.parent / f"{original.stem}-later.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    # This test injects a second canonical version directly to reproduce the historical
    # latest-by-clock hazard. Tell Program 0 its canonical snapshot is dirty so the immutable
    # served index is rebuilt to include that injected version.
    assert mark_projection_dirty(
        reason="test injected a later duplicate episode record",
        memory_id=duplicate_id,
        root=core_paths.MEMORY_ROOT,
    )
    build_index()
    return duplicate_id


def _served_index():
    from core import paths as core_paths
    from memory.projection.generation import open_generation_view

    view = open_generation_view(core_paths.MEMORY_ROOT)
    try:
        return json.loads(view.read_bytes("coord_index.snapshot.json").decode("utf-8"))
    finally:
        view.close()


def test_hydration_returns_the_exact_pinned_records_in_manifest_order():
    m = cw.membership()
    _store, carriers, _staged, manifest = _sealed_cohort()

    records = m.hydrate_window(manifest)
    assert isinstance(records, tuple) and len(records) == 10
    assert [record.episode_id for record in records] == [c.episode_id for c in carriers]
    assert [record.content_sha256() for record in records] == [
        ref.content_sha256 for ref in manifest.member_refs
    ]
    assert [record.temporal["finish_line"]["commit_seq"] for record in records] == [
        row["finish_line_commit_seq"] for row in manifest.members
    ]


def test_the_consumption_seam_never_hydrates_a_later_duplicate_by_clock():
    """The seed of L12, through the seam the reflection reads — no new API involved."""
    from memory.episode_commit_view import episode_record_ref
    from pressure_loop import window_lane
    from schema.continuity_json import WindowMembershipError

    store = EpisodeStore()
    carriers = cw.close_cohort(store)
    victim = carriers[3].episode_id
    duplicate_id = _write_later_duplicate(episode_record_ref(victim).record_id)
    assert duplicate_id in _served_index(), "the duplicate never reached the served Program 0 index"

    staged = cw.staged_cohort()
    manifest = cw.membership().seal_window(staged, snapshot=cw.membership().window_snapshot(staged))
    statuses = {row["episode_id"]: row["memory_commit_status"] for row in manifest.members}
    assert manifest.complete is False
    assert statuses[victim] == "canonical_committed", (
        "the current graph stopped proving the pinned revision's publication"
    )
    assert window_lane.pressure_contributor() is None, (
        "the cohort was released after its pinned revision left the served graph"
    )
    with pytest.raises(WindowMembershipError, match="not complete"):
        window_lane.restore_window(ct.dress_learn(cw.open_window_episode(store)))
    assert window_lane.pressure_contributor() is None, (
        "the incomplete cohort became eligible after hydration failed"
    )


def test_a_later_duplicate_version_never_displaces_the_pinned_revision():
    m = cw.membership()
    _store, _carriers, _staged, manifest = _sealed_cohort()
    duplicate_id = _write_later_duplicate(manifest.member_refs[3].record_id)
    assert duplicate_id in _served_index(), "the duplicate never reached the served Program 0 index"

    records = m.hydrate_window(manifest)
    assert records[3].content_sha256() == manifest.member_refs[3].content_sha256
    assert records[3].identity["thread_id"] != "thread-later-version"


def test_restore_carries_the_manifest_records_and_bodies_with_their_canonical_refs():
    from pressure_loop import window_lane

    store = EpisodeStore()
    carriers = cw.close_cohort(store)
    staged = cw.staged_cohort()
    window = window_lane.restore_window(ct.dress_learn(cw.open_window_episode(store)))

    manifest = window.manifest
    assert manifest is not None and manifest.complete is True
    assert manifest.member_ids == tuple(staged.episodes)
    assert [record.episode_id for record in window.episode_records] == [
        c.episode_id for c in carriers
    ]
    assert len(window.episode_bodies) == 10
    for body, ref in zip(window.episode_bodies, manifest.member_refs):
        assert CanonicalRef.from_dict(body["canonical_ref"]) == ref, (
            "the derived evidence lost the canonical reference it was read from"
        )
    deltas = {row["episode_id"] for row in window.episode_mastery_deltas}
    assert deltas <= set(manifest.member_ids)


def test_hydration_performs_no_writes():
    import hashlib
    from pathlib import Path

    from core import paths as core_paths
    from memory import intake_journal

    def tree_digest(root) -> str:
        digest = hashlib.sha256()
        for path in sorted(Path(root).rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()

    m = cw.membership()
    _store, _carriers, _staged, manifest = _sealed_cohort()
    journal_before = intake_journal.journal_path().read_bytes()
    tree_before = tree_digest(core_paths.MEMORY_ROOT)
    for _ in range(2):
        m.hydrate_window(manifest)
    assert intake_journal.journal_path().read_bytes() == journal_before
    assert tree_digest(core_paths.MEMORY_ROOT) == tree_before
