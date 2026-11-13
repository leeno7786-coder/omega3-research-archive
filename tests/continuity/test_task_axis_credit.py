"""Operator-authorized addition to WO-08: DeepSleep credits mastery from the TASK axis only.

WO-06 separated cascade completion from task success and left one reader behind:
``deepsleep_loop._deepsleep_state._episode_entry_success`` still counted a nested ``completed``
status as success, so an action that completed while its own acceptance check FAILED still earned
mastery credit — the exact misreading the five outcome axes exist to prevent. The operator assigned
it to WO-08 (2026-09-22) alongside the migration, which is where it belongs: WO-08 forbids a runtime
dual-shape accessor, so the credit rule reads the new shape (``outcome_axes``) alone and an old body
is an explicit migration requirement, converted offline — never guessed from the old nested pack.

Every body below is produced by the real producer DeepSleep reads
(``pressure_loop.episode_journal.enriched_episode_from_episode``) from a carrier folded by the real
Meta B and Global B, with WO-05's real verification receipts.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from deepsleep_loop._deepsleep_state import DeepSleepInput
from pressure_loop.episode import EpisodeStore
from schema.continuity_json import CanonicalEpisodeUnavailable
from tests.helpers import continuity_terminus as ct

DOMAIN = "parsing"


@pytest.fixture(autouse=True)
def _terminus(tmp_path, monkeypatch):
    from memory import episode_sram

    ct.isolate(monkeypatch, tmp_path)
    yield
    episode_sram.reset()


def _build_body(*, passed: bool) -> dict:
    """A build whose ACTION succeeded; its acceptance check passed or failed."""
    from global_loop.loop import run_global_b
    from memory.auto_evidence import capture_artifact_revision, capture_verification
    from meta_loop.meta_b import run_meta_b_front
    from pressure_loop.episode_journal import enriched_episode_from_episode

    store = EpisodeStore()
    carrier = ct.open_carrier(store)
    eid = carrier.episode_id
    capture_artifact_revision(carrier, path="parser.py", revision=1, content_sha256=ct.SHA_A,
                              change_kind="changed")
    capture_verification(carrier, criterion=ct.CRITERION, path="parser.py", revision=1,
                         content_sha256=ct.SHA_A, passed=passed)
    goal = {"episode_id": eid, "intent": "USE_SKILL", "confidence": 0.9, "reason": "wo08",
            "omega_lane": "agent"}
    carrier.global_ = {"intent_decision": goal}
    carrier.meta = {
        "execution_action_pack": {"episode_id": eid, "action": "USE_SKILL", "tool": "skill",
                                  "execution_intent": "build", "source_action_goal": goal,
                                  "execution_inputs": {}},
        "motor_program": ct._program(eid),
    }
    carrier.auto = {
        "episode_id": eid,
        "action": "USE_SKILL",
        "source_action_goal": deepcopy(goal),
        "execution_pack": deepcopy(carrier.meta["execution_action_pack"]),
        "cycle_kind": "success", "memory_status": "skipped", "outputs": {},
        "telemetry": {"warnings": []},
    }
    assert run_meta_b_front(carrier) is not None
    run_global_b(carrier, carrier.meta["reflection"])
    body = enriched_episode_from_episode(carrier)
    # Omega grounded a knowledge gain backed by a COMMITTED receipt — the receipt-backed credit
    # path DeepSleep takes for a motor episode.
    body["omega_result"] = {
        "mastery_effects": [{"domain": DOMAIN, "knowledge_mastery": 0.6,
                             "application_mastery": 0.0}],
        "knowledge_receipts": [{"status": "committed", "domain": DOMAIN}],
    }
    return body


def _input(body: dict) -> DeepSleepInput:
    return DeepSleepInput(window_id=str(body["episode_id"]), window_reflection={},
                          episode_window=[body], trajectory_snapshot={})


def test_a_completed_action_that_failed_its_acceptance_check_earns_no_mastery():
    from deepsleep_loop._deepsleep_state import _episode_entry_success
    from deepsleep_loop.mastery.run import update_mastery
    from deepsleep_loop.mastery_store import load_mastery

    body = _build_body(passed=False)
    assert body["cascade_status"] == "completed"
    assert body["outcome_axes"]["task_outcome"] == "failure"
    assert body["outcome_axes"]["action_status"] == "succeeded"

    assert _episode_entry_success(body) is False, (
        "a completed action whose acceptance check failed was counted a success"
    )
    assert update_mastery(_input(body)) == []
    assert "parsing" not in load_mastery()


def test_a_verified_task_success_is_credited():
    from deepsleep_loop._deepsleep_state import _episode_entry_success
    from deepsleep_loop.mastery.run import update_mastery

    body = _build_body(passed=True)
    assert body["outcome_axes"]["task_outcome"] == "success"
    assert _episode_entry_success(body) is True
    records = update_mastery(_input(body))
    assert [record.domain_id for record in records] == ["parsing"]


def test_a_body_without_outcome_axes_is_a_migration_requirement_not_a_verdict():
    from deepsleep_loop._deepsleep_state import _episode_entry_success

    old_shape = {"episode_id": "ep-old", "cascade_status": "completed",
                 "outcome": {"outcome": {"status": "completed"}}}
    with pytest.raises(CanonicalEpisodeUnavailable, match="migration required"):
        _episode_entry_success(old_shape)


def test_a_real_learn_episode_is_still_credited_through_the_terminus():
    from deepsleep_loop.mastery_store import load_mastery

    store = EpisodeStore()
    ct.dress_learn(ct.open_carrier(store), domain="quantum_physics")
    ct.close(store)
    assert load_mastery()["quantum-physics"].evidence_count == 1
