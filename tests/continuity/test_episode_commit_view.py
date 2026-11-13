"""WO-07 Steps 2 / 5 — the commit view is a read-only join, and every status is earned.

``memory_commit_status`` is not a field of the sealed record (WO-02 decision 5); it is exposed
here, joined from the authorities that already exist: the intake journal (recorded intent and
its terminal outcome), the canonical filesystem (the record by its id and content hash) and the
served recall projection (actual membership in the compiled graph, never a watermark).

* a recorded intent is not a committed effect — a sealed record still held in SRAM is
  ``pending``;
* canonical durability is not recall publication — a record on disk that the served graph does
  not hold is ``canonical_committed``, even though the canonical writer already updated the
  coord index at write time;
* a later wall clock or a greater high-water mark proves nothing — only membership does;
* an as-of read needs the retained snapshot; a recompiled legacy graph is
  ``HistoricalSnapshotUnavailable``, never a read of today's contents.
"""

from __future__ import annotations

import json

import pytest

from pressure_loop.episode import EpisodeStore
from schema.continuity_json import CanonicalRef, HistoricalSnapshotUnavailable
from schema.episode_record import MEMORY_COMMIT_STATUSES
from tests.helpers import continuity_terminus as ct


@pytest.fixture(autouse=True)
def _terminus(tmp_path, monkeypatch):
    from memory import episode_sram

    ct.isolate(monkeypatch, tmp_path)
    yield
    episode_sram.reset()


def _view():
    import importlib

    try:
        return importlib.import_module("memory.episode_commit_view")
    except ModuleNotFoundError:
        pytest.fail("memory.episode_commit_view does not exist: there is no commit view to read")


def _snapshot(**overrides):
    from core.timekeeper import last_coverage_clock
    from tests.helpers.continuity_records import make_snapshot_ref

    clock = last_coverage_clock()
    fields = {
        "generation_id": _view().published_projection_id(),
        "coverage_clock": {"clock_epoch_id": clock.clock_epoch_id, "commit_seq": clock.commit_seq},
    }
    fields.update(overrides)
    return make_snapshot_ref(**fields)


def _close_learn():
    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    ct.close(store)
    return carrier


def _served_baseline():
    """Close one ordinary episode first, so a recall graph is being served to pin a snapshot to.
    A profile that has never compiled one has no snapshot, and the view says so."""
    return _close_learn()


def test_the_view_type_carries_exactly_one_contract_status():
    view = _view()
    carrier = _close_learn()
    commit = view.resolve_episode_commit(view.episode_record_ref(carrier.episode_id),
                                         snapshot=_snapshot())
    assert isinstance(commit, view.EpisodeCommitView)
    assert commit.memory_commit_status in MEMORY_COMMIT_STATUSES
    assert commit.reason


def test_a_fully_closed_episode_is_recall_published():
    view = _view()
    carrier = _close_learn()
    commit = view.resolve_episode_commit(view.episode_record_ref(carrier.episode_id),
                                         snapshot=_snapshot())
    assert commit.memory_commit_status == "recall_published"
    assert commit.publication["member"] is True


def test_a_recorded_intent_is_not_a_committed_effect(monkeypatch):
    """The terminus fails AFTER the sealed record reached SRAM and before the drain wrote it."""
    view = _view()
    _served_baseline()
    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))

    import deepsleep_loop.loop as loop

    def refuse(context_id):
        raise RuntimeError("injected: the drain did not run")

    monkeypatch.setattr(loop, "_flush_episode_sram", refuse)
    with pytest.raises(RuntimeError, match="injected"):
        ct.close(store)

    pending = view.pending_episode_record_ref(carrier.episode_id)
    assert pending is not None, "the sealed record was never recorded as an intent"
    commit = view.resolve_episode_commit(pending, snapshot=_snapshot())
    assert commit.memory_commit_status == "pending"
    assert commit.record is None, "a pending intent must not be served as a committed record"
    assert view.episode_record_ref(carrier.episode_id) is None


def test_canonical_durability_is_not_recall_publication(monkeypatch):
    """The canonical record committed; Program 0 publication then failed."""
    view = _view()
    _served_baseline()
    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))

    import deepsleep_loop.loop as loop

    def fail_reindex(**_kwargs):
        raise RuntimeError("injected: reindex failed after the canonical commit")

    monkeypatch.setattr(loop, "_publish_projection_after_deepsleep", fail_reindex)
    with pytest.raises(RuntimeError, match="injected"):
        ct.close(store)

    ref = view.episode_record_ref(carrier.episode_id)
    assert ref is not None, "the canonical write committed but cannot be found by its episode"
    commit = view.resolve_episode_commit(ref, snapshot=_snapshot())
    assert commit.memory_commit_status == "canonical_committed"
    assert commit.publication["member"] is False
    assert commit.record is not None and commit.record.content_sha256() == ref.content_sha256


def test_a_greater_watermark_without_membership_is_not_publication(monkeypatch):
    view = _view()
    _served_baseline()
    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))

    import deepsleep_loop.loop as loop

    monkeypatch.setattr(
        loop, "_publish_projection_after_deepsleep",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("injected")),
    )
    with pytest.raises(RuntimeError):
        ct.close(store)
    ref = view.episode_record_ref(carrier.episode_id)
    record_seq = view.resolve_episode_commit(ref, snapshot=_snapshot()).record.temporal[
        "finish_line"]["commit_seq"]

    from core.timekeeper import last_coverage_clock

    clock = last_coverage_clock()
    inflated = _snapshot(coverage_clock={"clock_epoch_id": clock.clock_epoch_id,
                                         "commit_seq": record_seq + 1000})
    commit = view.resolve_episode_commit(ref, snapshot=inflated)
    assert commit.memory_commit_status == "canonical_committed", (
        "a high-water mark past the record is not proof the record was published"
    )


def test_a_rejected_required_record_is_failed_and_visible(monkeypatch):
    view = _view()
    _served_baseline()
    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    record_id = view_record_id(carrier.episode_id)
    _reject_writes_of(monkeypatch, record_id)

    with pytest.raises(Exception):
        ct.close(store)

    rejected = view.pending_episode_record_ref(carrier.episode_id, include_terminal=True)
    commit = view.resolve_episode_commit(rejected, snapshot=_snapshot())
    assert commit.memory_commit_status == "failed"
    assert "reject" in commit.reason.lower()
    assert any(row["memory_id"] == record_id or row.get("record_id") == record_id
               for row in commit.rejected_intake)


def test_an_unknown_reference_is_unknown_not_empty():
    view = _view()
    _close_learn()
    stranger = CanonicalRef(record_id="episode.never_existed", record_version=1,
                            content_sha256="c" * 64)
    commit = view.resolve_episode_commit(stranger, snapshot=_snapshot())
    assert commit.memory_commit_status == "unknown"
    assert commit.record is None and commit.reason


def test_a_recompiled_legacy_graph_is_historical_snapshot_unavailable():
    view = _view()
    first = _close_learn()
    old_snapshot = _snapshot()
    _close_learn()  # the served graph is recompiled: the old snapshot is no longer retained
    with pytest.raises(HistoricalSnapshotUnavailable):
        view.resolve_episode_commit(view.episode_record_ref(first.episode_id),
                                    snapshot=old_snapshot)


def test_resolving_performs_no_writes():
    from core import paths as core_paths
    from memory import intake_journal

    view = _view()
    carrier = _close_learn()
    ref = view.episode_record_ref(carrier.episode_id)
    journal_before = intake_journal.journal_path().read_bytes()
    tree_before = _tree_digest(core_paths.MEMORY_ROOT)
    for _ in range(2):
        view.resolve_episode_commit(ref, snapshot=_snapshot())
    assert intake_journal.journal_path().read_bytes() == journal_before
    assert _tree_digest(core_paths.MEMORY_ROOT) == tree_before


def test_rejected_intake_stays_visible_in_the_view(monkeypatch):
    """A rejected NON-required item does not block the close, and it does not vanish either."""
    view = _view()
    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    _reject_writes_where(monkeypatch, lambda envelope: list(envelope.coord)[:2] == [6, 0])

    ct.close(store)
    commit = view.resolve_episode_commit(view.episode_record_ref(carrier.episode_id),
                                         snapshot=_snapshot())
    assert commit.memory_commit_status == "recall_published"
    assert commit.rejected_intake, "a rejected intake item vanished from the experience"
    assert all(row["status"] == "rejected" and row["reason"] for row in commit.rejected_intake)


# ── helpers ─────────────────────────────────────────────────────────────────


def view_record_id(episode_id: str) -> str:
    from memory.episode_record_seal import episode_record_id

    return episode_record_id(episode_id)


def _reject_writes_where(monkeypatch, predicate) -> None:
    """Make the drain's canonical writer REJECT (not raise) the envelopes *predicate* selects."""
    import memory.api as api

    real = api.write_memory

    def selective(envelope, *args, **kwargs):
        if predicate(envelope):
            return api.WriteReceipt(
                cycle_id=str(getattr(envelope, "cycle_id", "") or ""),
                staging_ref=None,
                path=None,
                validation_status="rejected",
                message="injected schema rejection",
            )
        return real(envelope, *args, **kwargs)

    monkeypatch.setattr(api, "write_memory", selective)


def _reject_writes_of(monkeypatch, record_id: str) -> None:
    _reject_writes_where(monkeypatch, lambda envelope: str(getattr(envelope, "id", "") or "")
                         == record_id)


def _tree_digest(root) -> str:
    import hashlib
    from pathlib import Path

    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_the_json_view_round_trips():
    view = _view()
    carrier = _close_learn()
    commit = view.resolve_episode_commit(view.episode_record_ref(carrier.episode_id),
                                         snapshot=_snapshot())
    data = commit.to_dict()
    json.dumps(data)  # JSON-ready
    assert data["memory_commit_status"] == commit.memory_commit_status
    assert data["record_ref"] == commit.record_ref.to_dict()
