"""N4/N4b — enriched body carries neuromod; window-independent last_closed_body."""

from __future__ import annotations

from pressure_loop.episode import EpisodeStore
from pressure_loop.episode_journal import enriched_episode_from_episode


def _minimal_ep(*, auto=None, episode_id: str = "ep-nm-1"):
    from types import SimpleNamespace

    return SimpleNamespace(
        episode_id=episode_id,
        opened_by="curiosity_gap",
        closed_at="2026-06-07T12:00:00+00:00",
        cascade_status="completed",
        global_={
            "action_goal": {"intent": "LEARN"},
            "outcome": {"status": "success"},
        },
        meta={"execution_action_pack": {"action": "USE_TOOL"}},
        auto=auto,
        pressure={},
        state="complete",
        brain_turns=[],
    )


def test_enriched_body_and_last_closed_body_carry_neuromod():
    neuromod = {
        "dopamine": 0.5,
        "plasticity": 0.5,
        "stress": 0.1,
        "serotonin": 0.4,
        "strength": 0.5,
        "cycle_kind": "success",
    }
    ep = _minimal_ep(auto={"neuromod": neuromod})

    body = enriched_episode_from_episode(ep)
    assert body["neuromod"] == neuromod

    store = EpisodeStore()
    store.open(opened_by="curiosity_gap", pipeline_id=0, work_kind="autonomous")
    live = store.current()
    assert live is not None
    live.global_ = ep.global_
    live.meta = ep.meta
    live.auto = ep.auto
    store.close()

    last = store.last_closed_body()
    assert last is not None
    assert last["neuromod"] == neuromod


def test_enriched_body_neuromod_empty_when_absent():
    for auto in (None, {}):
        ep = _minimal_ep(auto=auto)
        body = enriched_episode_from_episode(ep)
        assert body["neuromod"] == {}


# --- I6: enriched body preserves temporal + learning_state ----------------


def test_enriched_body_carries_temporal_and_learning_state():
    """The episode carries temporal + learning_state slots (cognition_chain sets them
    via store.open); the enrichment hop must preserve them — omega reflect/package and
    deepsleep read them from episode_window[0], so dropping them fabricates {}."""
    ep = _minimal_ep()
    ep.temporal = {"source": "live", "tick_count": 7}
    ep.learning_state = {"learning_debt": 2, "domain": "physics"}
    body = enriched_episode_from_episode(ep)
    assert body["temporal"] == {"source": "live", "tick_count": 7}
    assert body["learning_state"] == {"learning_debt": 2, "domain": "physics"}


def test_enriched_body_temporal_empty_when_absent():
    ep = _minimal_ep()
    body = enriched_episode_from_episode(ep)
    assert body["temporal"] == {}
    assert body["learning_state"] == {}


# --- I11: enriched body surfaces auto outputs (knowledge backlink) ---------


def test_enriched_body_carries_knowledge_outputs():
    """DeepSleepInput.auto_outcome scans episode_window bodies for
    outputs.knowledge_artifact_id; the enriched body must carry auto outputs or the
    episode<->artifact link is unreachable on consolidate."""
    ep = _minimal_ep(
        auto={
            "neuromod": {},
            "outputs": {"knowledge_artifact_id": "art-1", "knowledge_domain": "physics"},
        }
    )
    body = enriched_episode_from_episode(ep)
    assert body["outputs"]["knowledge_artifact_id"] == "art-1"
    assert body["outputs"]["knowledge_domain"] == "physics"


def test_last_closed_body_none_at_boot():
    store = EpisodeStore()
    assert store.last_closed_body() is None
