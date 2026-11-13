"""WO-06 Step 5 — every stage is accounted for; a stage that did not run says so and why.

Partial phases, in the order's words: an AUTO exception preserves prior phase context and
explicit not-run later phases. Before this, a cascade that raised was journaled as
``cascade_status="failed"`` and nothing else: the exception that decided it was never written
to the carrier, and the stages after it simply vanished from the record, indistinguishable from
stages that were never part of the episode.

The partial-phase cases drive the REAL ``cognition_chain.ignite_episode`` through the real
terminus (DeepSleep, ``EpisodeStore.close``), patching only the phase bodies — the same shape
``tests/core/test_episode_outcome_honesty.py`` uses.
"""

from __future__ import annotations

import pytest

import cognition_chain as cl
from schema.continuity_json import ContinuityContractError
from tests.core.test_episode_outcome_honesty import _ignition, _journal_setup

PHASES = ("admission", "pressure", "global_a", "meta_a", "auto", "meta_b", "global_b", "omega",
          "deepsleep")


def _global_ok(ep, ign):
    ep.global_ = dict(ep.global_ or {})
    ep.global_["intent_decision"] = {"intent": "USE_TOOL", "episode_id": ep.episode_id}
    return ep.global_["intent_decision"]


def _meta_ok(ep):
    ep.meta = dict(ep.meta or {})
    ep.meta["execution_action_pack"] = {
        "episode_id": ep.episode_id, "action": "USE_TOOL", "tool": "web_fetch", "routing": {},
        "memory_scope": "general", "prompt_type": "tool", "model_profile": "TEXT_FAST",
        "constraints": {},
    }
    return ep.meta["execution_action_pack"]


def _auto_boom(ep):
    raise RuntimeError("auto failed: the fetch transport refused the connection")


def _trail(body) -> dict:
    trail = body.get("phase_trail")
    assert isinstance(trail, list), (
        "the closed episode carries no stage trail; a stage that did not run leaves no trace"
    )
    return {row["phase"]: row for row in trail}


@pytest.fixture
def auto_raises(tmp_path, monkeypatch):
    store, _window, journal = _journal_setup(tmp_path, monkeypatch)
    monkeypatch.setattr("global_loop.loop.run_global_a", _global_ok)
    monkeypatch.setattr("meta_loop.loop.run_meta_a", _meta_ok)
    monkeypatch.setattr("auto_loop.auto.run_auto_front", _auto_boom)
    with pytest.raises(RuntimeError, match="auto failed"):
        cl.ignite_episode(store, _ignition())
    body = store.last_closed_body()
    assert body is not None
    return store, body, journal


def test_an_auto_exception_keeps_earlier_phases_and_names_later_ones_not_run(auto_raises):
    _store, body, _journal = auto_raises
    trail = _trail(body)

    assert tuple(trail) == PHASES, "every stage of the one cascade appears, in order"
    for phase in ("admission", "pressure", "global_a", "meta_a"):
        assert trail[phase]["status"] == "ran", phase
    assert trail["auto"]["status"] == "failed"
    assert "RuntimeError" in trail["auto"]["cause"]
    assert "the fetch transport refused the connection" in trail["auto"]["cause"]
    for phase in ("meta_b", "global_b", "omega"):
        assert trail[phase]["status"] == "not_run", phase
        assert "auto" in trail[phase]["cause"], "a stage that did not run names why"
    assert trail["deepsleep"]["status"] == "in_progress", (
        "the record is assembled at the start of the DeepSleep terminus; it cannot claim "
        "DeepSleep finished"
    )

    # The context the earlier stages produced is preserved alongside the failure.
    assert body["intent"] == "USE_TOOL"
    assert body["execution"]["execution_action_pack"]["tool"] == "web_fetch"


def test_the_failed_episode_outcome_is_honest_about_what_is_unknown(auto_raises):
    _store, body, _journal = auto_raises
    axes = body.get("outcome_axes")
    assert isinstance(axes, dict), "the closed body carries no outcome axes"

    assert axes["cascade_status"] == "failed"
    assert axes["action_status"] == "unknown", (
        "AUTO raised part way; whether the action took effect is not known, and 'failed' or "
        "'not attempted' would each be a claim"
    )
    assert axes["task_outcome"] == "unknown"
    assert axes["verification_status"] == "not_performed"


def test_the_coverage_block_names_the_gap(auto_raises):
    from schema.episode_record import _checked_coverage

    _store, body, _journal = auto_raises
    coverage = body.get("coverage")
    assert isinstance(coverage, dict)
    _checked_coverage(coverage)
    assert "meta_b" in coverage["unrecorded"] and "omega" in coverage["unrecorded"]
    assert any("auto" in gap for gap in coverage["causal_gaps"])
    assert "global_a" in coverage["captured"]


def test_every_stage_is_accounted_for_in_a_completed_idle_episode(tmp_path, monkeypatch):
    from tests.core.test_episode_outcome_honesty import test_idle_cascade_completes_with_cascade_completed

    store, _window, journal = _journal_setup(tmp_path, monkeypatch)
    # Drive the honesty suite's own idle cascade against this store, then read what it closed.
    monkeypatch.setattr(
        "tests.core.test_episode_outcome_honesty._journal_setup",
        lambda _tmp, _mp: (store, _window, journal),
    )
    test_idle_cascade_completes_with_cascade_completed(tmp_path, monkeypatch)

    trail = _trail(store.last_closed_body())
    assert tuple(trail) == PHASES
    for row in trail.values():
        assert row["status"] in {"ran", "failed", "not_run", "deferred", "in_progress", "unknown"}
        if row["status"] != "ran":
            assert row["cause"], f"{row['phase']} did not run and says nothing about why"


def _coverage_check():
    from pressure_loop import episode_journal

    check = getattr(episode_journal, "check_phase_coverage", None)
    assert check is not None, "there is no field-coverage check for the stage trail"
    return check


def test_the_coverage_check_refuses_a_trail_with_a_missing_stage():
    check = _coverage_check()
    trail = [{"phase": phase, "status": "ran", "cause": "", "evidence": []} for phase in PHASES]
    del trail[5]  # meta_b disappears, as it used to

    with pytest.raises(ContinuityContractError, match="meta_b"):
        check(trail)


def test_the_coverage_check_refuses_a_stage_that_did_not_run_without_a_cause():
    check = _coverage_check()
    trail = [{"phase": phase, "status": "ran", "cause": "", "evidence": []} for phase in PHASES]
    trail[6] = {"phase": "global_b", "status": "not_run", "cause": "", "evidence": []}

    with pytest.raises(ContinuityContractError, match="global_b"):
        check(trail)


def test_canonical_detail_stays_out_of_prompt_compaction(auto_raises):
    """The canonical body carries the stage trail and axes; the reflection compaction keeps its
    own bounded contract and is not silently widened by them."""
    from pressure_loop.episode_journal import compact_episode_for_reflection

    _store, body, _journal = auto_raises
    assert "phase_trail" in body and "outcome_axes" in body
    compact = compact_episode_for_reflection(body)
    assert "phase_trail" not in compact and "coverage" not in compact
