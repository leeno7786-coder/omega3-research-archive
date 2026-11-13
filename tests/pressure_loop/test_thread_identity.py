"""T2: every episode carries the identity of the cognitive thread it belongs to.

Cross-lane cognitive integration spec, section 1. ``thread_id`` is host-generated
and immutable for the life of a thread: its value is the episode id of the
thread's FIRST episode, it is inherited verbatim by successors, and it is
persisted on the journal row so a thread survives a restart.

Inheritance rides the channel the cascade already has. ``cognition_chain`` builds
``pressure["contributors"][i]["detail"] = dict(contributor.metadata)`` before
``EpisodeStore.open``, which is the same path ``stage_omega_successor`` reads its
origin back out of, so no new plumbing and no signature change is needed.
"""

from types import SimpleNamespace

import pytest

from pressure_loop import pressure
from pressure_loop.episode import EpisodeStore
from pressure_loop.episode_journal import PriorEpisode, prior_episode_from_episode
from pressure_loop.omega_successor import stage_omega_successor
from schema.omega_control_decision import OmegaControlDecision


def _control(intent="USE_SKILL", decision="PRESSURE"):
    assessment = dict.fromkeys(("who", "what", "when", "where", "why", "how",
                                "desired_state", "observed_state"), "fact")
    assessment.update(gap="unfinished", evidence=[])
    return OmegaControlDecision.from_dict(dict(
        assessment=assessment, decision=decision,
        pressure=dict(intent=intent, confidence=0.0, reason="finish", setpoints={},
                      desired_state="complete", observed_state="partial",
                      resolution_conditions=["verified"]),
        synthesis="summary"))


def _pressure_snapshot(detail, *, opened_by="operator_query"):
    """The exact shape cognition_chain passes to store.open."""
    return {"opened_by": opened_by,
            "contributors": [{"id": opened_by, "weight": 1.0, "detail": dict(detail)}]}


@pytest.fixture(autouse=True)
def _isolated_successors(monkeypatch, tmp_path):
    monkeypatch.setattr(pressure, "_successor_path", lambda: tmp_path / "successors.json")
    monkeypatch.setattr(pressure, "_RESEED_STAGING", {})


def test_first_episode_of_a_thread_is_its_own_thread_root():
    store = EpisodeStore()

    ep = store.open(opened_by="operator_query", pipeline_id=0, work_kind="operator_turn",
                    pressure=_pressure_snapshot({"cycle_id": "turn-1"}))

    assert ep.thread_id == ep.episode_id, "a new thread is rooted at its first episode"


def test_an_unrelated_turn_starts_its_own_thread():
    store = EpisodeStore()

    first = store.open(opened_by="operator_query", pipeline_id=0, work_kind="operator_turn",
                       pressure=_pressure_snapshot({"cycle_id": "turn-1"}))
    store.close(cascade_status="completed")
    second = store.open(opened_by="operator_query", pipeline_id=0, work_kind="operator_turn",
                        pressure=_pressure_snapshot({"cycle_id": "turn-2"}))

    assert second.thread_id == second.episode_id
    assert second.thread_id != first.thread_id


def test_successor_metadata_carries_the_parent_thread():
    parent = SimpleNamespace(
        episode_id="parent-ep", thread_id="thread-root",
        global_={"omega_packet": {"original_objective": "original task", "omega_lane": "agent"}},
        opened_by="operator_query",
        pressure={"contributors": [{"id": "operator_query", "detail": {"cycle_id": "bubble"}}]})

    row = stage_omega_successor(parent, _control())

    assert row.metadata["thread_id"] == "thread-root"


def test_a_parent_without_a_thread_becomes_the_root_of_one():
    """A pre-T2 parent has no thread; the host roots the thread at that parent."""
    parent = SimpleNamespace(
        episode_id="legacy-parent",
        global_={"omega_packet": {"original_objective": "original task", "omega_lane": "agent"}},
        opened_by="operator_query",
        pressure={"contributors": [{"id": "operator_query", "detail": {"cycle_id": "bubble"}}]})

    row = stage_omega_successor(parent, _control())

    assert row.metadata["thread_id"] == "legacy-parent"


def test_a_successor_episode_inherits_the_thread_verbatim():
    store = EpisodeStore()

    ep = store.open(opened_by="omega_successor", pipeline_id=0, work_kind="autonomous",
                    pressure=_pressure_snapshot({"thread_id": "thread-root"},
                                                opened_by="omega_successor"))

    assert ep.thread_id == "thread-root"
    assert ep.thread_id != ep.episode_id, "a continuation is not a new thread root"


def test_journal_row_persists_and_round_trips_the_thread():
    ep = SimpleNamespace(episode_id="ep-1", thread_id="thread-root", opened_by="operator_query",
                         pressure={}, global_={}, meta={}, omega={},
                         pipeline_id=0, work_kind="operator_turn", cascade_status="completed")

    row = prior_episode_from_episode(ep)

    assert row.thread_id == "thread-root"
    assert PriorEpisode.from_dict(row.to_dict()).thread_id == "thread-root"


def test_a_legacy_row_without_a_thread_reads_as_no_thread():
    """Rows written before T2 carry no thread. Empty is truthful; it is not a thread."""
    legacy = PriorEpisode.from_dict({"episode_id": "old", "ignition_reason": "operator_query"})

    assert legacy.thread_id == ""
