"""Slice 6a B1 — DeepSleep types + journal store."""

from __future__ import annotations

import pytest

from deepsleep_loop._deepsleep_state import (
    DeepSleepInput,
    DeepSleepOutcomePack,
    JournalEntry,
)
from tests.core.deepsleep_window_fixtures import minimal_input
from deepsleep_loop.journal_store import append_journal_entry, read_journal


def _sample_journal_entry() -> JournalEntry:
    return JournalEntry(
        episode_id="ep-ds-1",
        timestamp="2026-06-05T12:00:00Z",
        intent="LEARN",
        action="web_fetch",
        outcome={"goal_state": "completed"},
        trajectory_snapshot={"target_domain": "quantum_physics"},
        omega_reasoning={"what": "done"},
        observations={"response_text": True},
        artifacts={"response_text": "summary"},
    )


def test_deepsleep_input_round_trip():
    original = minimal_input()
    restored = DeepSleepInput.from_dict(original.to_dict())
    assert restored == original


def test_journal_entry_round_trip():
    original = _sample_journal_entry()
    restored = JournalEntry.from_dict(original.to_dict())
    assert restored == original


def test_deepsleep_outcome_pack_round_trip():
    journal = _sample_journal_entry().to_dict()
    original = DeepSleepOutcomePack(
        episode_id="ep-ds-1",
        journal_entry=journal,
        updated_memory=journal,
        updated_identity={"persistent_learner": {"name": "persistent learner"}},
        updated_mastery={"quantum_physics": {"knowledge_mastery": 0.4}},
        updated_open_queries={"q-1": {"status": "open"}},
        updated_trajectory={"quantum_physics::foundational understanding": {"status": "active"}},
        pressure_seed=[{"type": "trajectory_gap"}],
        projection_state={"identity_projection": {}, "mastery_projection": {}},
        episode_closed=True,
        pressure_reset=True,
    )
    restored = DeepSleepOutcomePack.from_dict(original.to_dict())
    assert restored == original


def test_append_journal_entry_then_read_last_two(tmp_path):
    path = tmp_path / "journal.jsonl"
    entries = [
        _sample_journal_entry(),
        JournalEntry(
            episode_id="ep-ds-2",
            timestamp="2026-06-05T12:01:00Z",
            intent="IDLE",
            action=None,
            outcome=None,
            trajectory_snapshot={},
            omega_reasoning={},
            observations={},
            artifacts={},
        ),
    ]
    for entry in entries:
        append_journal_entry(entry, path=path)

    window = read_journal(2, path=path)
    assert len(window) == 2
    assert window[0].episode_id == "ep-ds-1"
    assert window[1].episode_id == "ep-ds-2"


def test_read_journal_missing_path_returns_empty(tmp_path):
    missing = tmp_path / "does_not_exist.jsonl"
    assert read_journal(5, path=missing) == []


def test_append_journal_entry_write_error_halts(monkeypatch):
    """Fail-hard: the journal is THE continuity record — a write failure CRASHES,
    it is not swallowed (silently dropping a journal line corrupts continuity)."""

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "deepsleep_loop.journal_store._resolve_path",
        _boom,
    )
    with pytest.raises(OSError, match="disk full"):
        append_journal_entry(_sample_journal_entry(), path="ignored")
