"""Consolidation failures remain distinct from an empty store or no new traits."""

from __future__ import annotations

import json

import pytest

from deepsleep_loop import failure_store, mastery_store, semantic_store, trajectory_store
from deepsleep_loop._deepsleep_state import (
    DeepSleepInput, FailurePattern, MasteryRecord, SemanticEntry, TrajectoryRecord,
)
from deepsleep_loop.identity.run import update_identity


STORES = [
    (mastery_store.load_mastery, mastery_store.upsert_mastery,
     MasteryRecord("parsing", "Parsing")),
    (failure_store.load_failures, failure_store.upsert_failure,
     FailurePattern("parse", "parsing", domain_id="parsing")),
    (semantic_store.load_semantic, semantic_store.upsert_semantic,
     SemanticEntry("parsing", "A synthetic statement")),
    (trajectory_store.load_trajectory, trajectory_store.upsert_trajectory,
     TrajectoryRecord("parsing", "recognize tokens")),
]


@pytest.mark.parametrize("reader,writer,record", STORES, ids=["mastery", "failure", "semantic", "trajectory"])
@pytest.mark.parametrize("invalid", [[], None, {"bad-row": 42}, {"bad-row": []}])
def test_invalid_store_schema_fails_read_and_upsert_without_overwrite(tmp_path, reader, writer, record, invalid):
    path = tmp_path / "invalid-store.json"
    original = json.dumps(invalid).encode("utf-8")
    path.write_bytes(original)
    with pytest.raises(ValueError, match="store|record|row"):
        reader(path=path)
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="store|record|row"):
        writer(record, path=path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("reader,writer,record", STORES, ids=["mastery", "failure", "semantic", "trajectory"])
def test_missing_and_valid_stores_remain_distinct_from_corruption(tmp_path, reader, writer, record):
    path = tmp_path / "store.json"
    assert reader(path=path) == {}
    writer(record, path=path)
    assert list(reader(path=path).values()) == [record]


@pytest.mark.parametrize("reader,writer,record", STORES, ids=["mastery", "failure", "semantic", "trajectory"])
def test_existing_directory_is_not_a_missing_store(tmp_path, reader, writer, record):
    path = tmp_path / "store.json"
    path.mkdir()
    with pytest.raises(OSError):
        reader(path=path)


def _pattern_input(episode_id):
    return DeepSleepInput(
        window_id=episode_id, window_reflection={}, trajectory_snapshot={},
        episode_window=[{
            "episode_id": episode_id,
            "omega_result": {"identity_patterns": [{
                "trait": "synthetic_observer", "pattern": "Synthetic observation",
                "evidence": "Synthetic evidence", "status": "new",
            }]},
        }],
    )


@pytest.mark.parametrize("operation", ["record_trait_observation", "load_trait_evidence"])
def test_identity_storage_error_preserves_original_cause(tmp_path, monkeypatch, operation):
    from memory import episode_sram

    episode_sram.open_bucket("identity-storage-failure", pipeline_id=0)
    failure = OSError("synthetic identity storage unavailable")

    calls = []

    def unavailable(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise failure
        return {}

    monkeypatch.setattr(f"deepsleep_loop.identity_store.{operation}", unavailable)
    with pytest.raises(OSError) as caught:
        update_identity(_pattern_input("identity-storage-failure"), identity_path=tmp_path / "identity.json")
    assert caught.value is failure


def test_identity_storage_error_reaches_durable_episode_failure_boundary(tmp_path, monkeypatch):
    from deepsleep_loop import loop
    from memory import episode_sram, intake_journal
    from memory.episode_events import read_episode_events
    from tests.core.deepsleep_window_fixtures import (
        active_store_from_episode_body, episode_entry, patch_window_deepsleep_paths,
    )

    patch_window_deepsleep_paths(monkeypatch, tmp_path)
    body = episode_entry("identity-durable-failure", intent="LEARN", target="Parsing")
    store, episode = active_store_from_episode_body(body)
    episode_sram.open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    failure = OSError("synthetic identity storage unavailable")

    def unavailable(**kwargs):
        raise failure

    def identity_core(*args, **kwargs):
        return update_identity(_pattern_input(episode.episode_id), identity_path=tmp_path / "identity.json")

    monkeypatch.setattr("deepsleep_loop.identity_store.record_trait_observation", unavailable)
    monkeypatch.setattr(loop, "_run_deepsleep_episode_core", identity_core)
    with pytest.raises(OSError) as caught:
        loop.run_deepsleep_for_episode(store)
    assert caught.value is failure
    assert store.current() is episode
    assert episode_sram.is_open(episode.episode_id)
    failed = [event for event in read_episode_events(episode.episode_id)
              if event.event_kind == "deepsleep.phase_failed"]
    assert len(failed) == 1
    assert "OSError: synthetic identity storage unavailable" in failed[0].payload["reason"]
    pending = intake_journal.pending_records(owner_episode_id=episode.episode_id)
    assert failed[0].event_id in {row.event_id for row in pending}
