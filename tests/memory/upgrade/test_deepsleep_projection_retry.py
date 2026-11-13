"""The real DeepSleep terminus retries projection without replaying canonical intake."""

from __future__ import annotations

import pytest

from core.required_substrate import RequiredSubstrateError
from deepsleep_loop.loop import run_deepsleep_for_episode
from memory import api, episode_sram, intake_journal
from tests.core.deepsleep_window_fixtures import (
    active_store_from_episode_body,
    episode_entry,
    patch_window_deepsleep_paths,
)


def test_program0_failure_keeps_episode_retryable_and_does_not_rewrite_intake(
    tmp_path, monkeypatch
):
    patch_window_deepsleep_paths(monkeypatch, tmp_path)
    body = episode_entry("ep-real-reindex-retry", intent="IDLE", target="Python")
    store, episode = active_store_from_episode_body(body)
    episode_sram.open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    envelope = api.CanonicalEnvelope(
        cycle_id=episode.episode_id,
        coord=[2, 0, 0],
        type="concept",
        id="2.00.reindex_retry",
        payload={"text": "canonical before projection"},
        source="tests.memory.projection_retry",
    )
    intake = api.intake_memory(envelope, event_id="real-reindex-retry-event")
    writes: list[str] = []
    write_memory = api.write_memory
    from memory.projection.dispatch import projection_dispatch

    def observed_write(value, **kwargs):
        writes.append(str(value.id or ""))
        return write_memory(value, **kwargs)

    monkeypatch.setattr(api, "write_memory", observed_write)
    monkeypatch.setattr(
        "memory.projection.dispatch.projection_dispatch",
        lambda _root, *, reason: (_ for _ in ()).throw(RuntimeError("Program 0 offline")),
    )

    with pytest.raises(RequiredSubstrateError, match="Program 0 offline") as raised:
        run_deepsleep_for_episode(store)

    assert raised.value.component == "memory_projection"
    assert raised.value.operation == "dispatch_execution"
    assert store.current() is episode
    assert episode.state == "open"
    held = episode_sram.pending(episode.episode_id)
    assert len(held) == 1 and held[0].kind == "event"
    failure = held[0].event
    assert failure.event_kind == "deepsleep.phase_failed"
    assert "Program 0 offline" in failure.payload["reason"]
    assert episode_sram.is_open(episode.episode_id)
    record = intake_journal.record_by_id(intake.journal_record_id)
    assert record is not None and record.status == "committed"
    assert writes.count(envelope.id) == 1

    # The next attempt uses the actual committed files and real publisher. Its failure
    # becomes a published supplement while the original intake is not written again.
    monkeypatch.setattr("memory.projection.dispatch.projection_dispatch", projection_dispatch)
    outcome = run_deepsleep_for_episode(store)

    assert outcome is not None
    assert store.current() is None
    assert writes.count(envelope.id) == 1
    assert not episode_sram.is_open(episode.episode_id)
    assert not intake_journal.pending_records(owner_episode_id=episode.episode_id)
    from memory.episode_commit_view import episode_record_ref, resolve_episode_commit
    from tests.continuity.test_episode_commit_view import _snapshot

    committed = resolve_episode_commit(episode_record_ref(episode.episode_id), snapshot=_snapshot())
    assert committed.memory_commit_status == "recall_published"
    assert [event.to_dict() for event in committed.supplemental_events] == [failure.to_dict()]
