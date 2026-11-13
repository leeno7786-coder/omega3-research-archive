"""WO-10 — history is read as of the sealed snapshot, never from today's files.

Acceptance cases covered here:

* **Retained versions** — an unavailable historic revision is reported; current contents cannot
  impersonate it. The one projection path (AGENTS.md §2) retains nothing: once the graph is
  recompiled, the snapshot that named the previous one reads as unavailable rather than as
  today's contents dressed as the past.
* **Authority chronology** — a changed operator direction is represented by its real revision,
  not diagnosed as a failure.

The pinned-replay cases this file used to open with — an earlier snapshot read under its own
retained generation, held alive by a reader's lease — depended on the generation owners the
one-path rule retired. They asserted a capability no shipped build ever had, because the
environment variables that selected those owners always shipped unset. Retained-history replay
is therefore work owed, not covered here.

History episodes close on their own ``EpisodeStore`` (a separate collecting window that never
fills), so the cohort being assessed holds exactly its ten members and everything else is
history in the same canonical memory and the same recall projection.
"""

from __future__ import annotations

import json

import pytest

from pressure_loop.episode import EpisodeStore
from schema.continuity_json import HistoricalSnapshotUnavailable
from tests.helpers import continuity_history as ch
from tests.helpers import continuity_terminus as ct


@pytest.fixture(autouse=True)
def _terminus(tmp_path, monkeypatch):
    from memory import episode_sram

    monkeypatch.setenv("OMEGA_WINDOW_LANE", "1")
    ct.isolate(monkeypatch, tmp_path)
    yield
    episode_sram.reset()


def _history_store(tmp_path):
    return EpisodeStore(reflection_window_path=tmp_path / "history_window.json")


# ── retained versions ────────────────────────────────────────────────────────


def test_a_recompiled_snapshot_is_unavailable_not_impersonated(tmp_path):
    history_store = _history_store(tmp_path)
    ch.close_learn(history_store)
    store = EpisodeStore()
    ch.close_members(store, 10)
    _window, manifest, records = ch.seal_staged()

    assert ch.collect(manifest, records).primary_support, "the served snapshot is readable"
    ch.close_learn(history_store)          # the graph is recompiled; nothing retains the old one
    with pytest.raises(HistoricalSnapshotUnavailable):
        ch.collect(manifest, records)


# ── authority chronology ─────────────────────────────────────────────────────


def test_a_changed_direction_is_its_real_revision_with_its_real_authority():
    from pressure_loop.reflection_window import trajectory_revision

    first = ch.trajectory("first", operator=True)
    second = ch.trajectory("second", operator=True)
    third = ch.trajectory("third", operator=False)
    plan = [first] * 4 + [second] * 3 + [third] * 3

    store = EpisodeStore()
    members = [ch.admit_and_close(store, trajectory=traj, cycle=f"bubble-dir-{i}", arrival=8000 + i)
               for i, traj in enumerate(plan)]
    _window, manifest, records = ch.seal_staged()

    bundle = ch.collect(manifest, records)
    bundle_dict = bundle.to_dict()
    changes = list(bundle.intervening_direction_changes)
    assert [(row["from_revision"], row["to_revision"]) for row in changes] == [
        (trajectory_revision(first), trajectory_revision(second)),
        (trajectory_revision(second), trajectory_revision(third)),
    ]
    assert [row["effective_episode_id"] for row in changes] == [
        members[4].episode_id, members[7].episode_id,
    ]
    assert [row["authority"] for row in changes] == ["operator", "omega"]
    assert all(row["kind"] == "trajectory" and row["evidence_refs"] for row in changes)
    text = json.dumps(bundle_dict["intervening_direction_changes"])
    assert "drift" not in text and "fail" not in text, (
        "following a new direction was described as a failure"
    )
