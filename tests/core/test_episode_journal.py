from __future__ import annotations

from types import SimpleNamespace

import pytest

from pressure_loop.episode_journal import (
    PriorEpisode,
    archive_episode,
    journal_window,
    prior_episode_from_episode,
)


def test_prior_episode_from_episode_extraction_and_roundtrip():
    ep = SimpleNamespace(
        episode_id="ep-1",
        opened_by="operator_command",
        pressure={"opened_by": "operator_command"},
        global_={"action_goal": {"intent": "LEARN"}, "outcome": {"status": "success"}},
        meta={"execution_action_pack": {"action": "USE_TOOL"}},
        omega={},
    )

    pe = prior_episode_from_episode(ep)
    assert pe.episode_id == "ep-1"
    assert pe.ignition_reason == "operator_command"
    assert pe.intent == "LEARN"
    assert pe.action == "USE_TOOL"
    assert pe.outcome == {"status": "success"}
    assert pe.trajectory_update is None
    # reasoning prose is never persisted to the journal
    assert "omega_reflection" not in pe.to_dict()

    # JSONL round-trip shape stability
    pe2 = PriorEpisode.from_dict(pe.to_dict())
    assert pe2 == pe


def _stub_episode(
    episode_id: str,
    *,
    intent: str = "LEARN",
    action: str = "USE_TOOL",
    outcome: dict | None = None,
    opened_by: str = "operator_command",
):
    return SimpleNamespace(
        episode_id=episode_id,
        opened_by=opened_by,
        pressure={"opened_by": opened_by},
        global_={
            "action_goal": {"intent": intent},
            "outcome": outcome or {"status": "success"},
        },
        meta={"execution_action_pack": {"action": action}},
        omega={},
    )


def test_archive_episode_and_journal_window_reads_last_n_chronological(tmp_path):
    path = tmp_path / "episode_journal.jsonl"

    archive_episode(_stub_episode("ep-1"), path=path)
    archive_episode(_stub_episode("ep-2"), path=path)
    archive_episode(_stub_episode("ep-3"), path=path)

    window = journal_window(2, path=path)
    assert [pe.episode_id for pe in window] == ["ep-2", "ep-3"]

    assert window[0].intent == "LEARN"
    assert window[0].action == "USE_TOOL"
    assert window[0].outcome == {"status": "success"}


def test_journal_window_missing_path_returns_empty(tmp_path):
    missing = tmp_path / "does_not_exist.jsonl"
    assert journal_window(path=missing) == []
    assert journal_window(2, path=missing) == []


def test_a_corrupt_journal_line_fails_loudly_instead_of_reading_as_less_history(tmp_path):
    """AGENTS.md section 2.1: skipping a line that cannot be read makes "could not read" look like
    "that episode never happened". The reader fails, naming the file, the line and the cause."""
    from pressure_loop.episode_journal import EpisodeJournalUnreadable

    path = tmp_path / "episode_journal.jsonl"
    path.write_text("this is not json\n", encoding="utf-8")
    archive_episode(_stub_episode("ep-1"), path=path)

    with pytest.raises(EpisodeJournalUnreadable, match=r"line 1") as caught:
        journal_window(10, path=path)
    assert path.name in str(caught.value)
    assert isinstance(caught.value.__cause__, ValueError)


def test_a_row_that_is_not_a_prior_episode_fails_loudly(tmp_path):
    from pressure_loop.episode_journal import EpisodeJournalUnreadable

    path = tmp_path / "episode_journal.jsonl"
    archive_episode(_stub_episode("ep-1"), path=path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("[1, 2, 3]\n")

    with pytest.raises(EpisodeJournalUnreadable, match=r"line 2"):
        journal_window(10, path=path)


def test_undecodable_journal_bytes_fail_loudly(tmp_path):
    from pressure_loop.episode_journal import EpisodeJournalUnreadable

    path = tmp_path / "episode_journal.jsonl"
    path.write_bytes(b"\xff\xfe\x00 not utf-8\n")

    with pytest.raises(EpisodeJournalUnreadable) as caught:
        journal_window(10, path=path)
    assert isinstance(caught.value.__cause__, UnicodeDecodeError)


def test_a_journal_path_that_exists_but_is_not_a_file_is_not_an_empty_history(tmp_path):
    from pressure_loop.episode_journal import EpisodeJournalUnreadable

    path = tmp_path / "episode_journal.jsonl"
    path.mkdir()

    with pytest.raises(EpisodeJournalUnreadable, match="not a file"):
        journal_window(10, path=path)
