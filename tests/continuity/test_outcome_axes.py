"""WO-06 — cascade completion is not task success; five outcome axes from real producers.

The live defect this order exists for: ``pressure_loop.episode._prior_episode_success`` answered
"did this episode succeed?" with ``cascade_status == "completed"``. A build that ran to the end
of the cognition cascade and FAILED its own acceptance criterion therefore counted as a success,
and ``EpisodeStore.recent_success_rate`` fed that into the neuromodulation baseline — reporting a
neutral 0.5 whenever there was nothing to measure, so "no data" read as a measurement.

Every carrier here is driven through the REAL phase producers: the real Meta B front
(``meta_loop.meta_b.run_meta_b_front``) and the real Global B chain
(``global_loop.loop.run_global_b``), with verification receipts captured through WO-05's real
``capture_verification`` / ``capture_artifact_revision`` into the episode's SRAM events. The
carrier shape is the one ``tests/core/test_slice4_acceptance.py`` drives through the same
producers; the stopped-dispatch result is the literal ``brain_skill_executor`` returns when the
operator stops a build.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from schema.continuity_json import ContinuityContractError

EID = "ep1"  # the motor-program fixture below is bound to episode "ep1"
THREAD = "thread-wo06"
CRITERION = "pytest passes"  # that fixture program's one acceptance criterion
SHA_A = "a" * 64
SHA_B = "b" * 64


# ── real-producer carriers ──────────────────────────────────────────────────


def _program() -> dict:
    from tests.omega_loop.test_motor_pressure_resolution import _program_dict

    return _program_dict()


def _auto(cycle_kind: str, **extra) -> dict:
    auto = {
        "cycle_kind": cycle_kind,
        "memory_status": "skipped",
        "outputs": {},
        "telemetry": {"warnings": []},
    }
    auto.update(extra)
    return auto


def _dress(carrier, *, intent: str, action: str, tool, lane: str, auto: dict, motor: bool):
    """Put the Global A / Meta A / AUTO slots on a carrier exactly as the cascade leaves them."""
    eid = str(carrier.episode_id)
    goal = {"episode_id": eid, "intent": intent, "confidence": 0.9, "reason": "wo06",
            "omega_lane": lane}
    carrier.global_ = dict(getattr(carrier, "global_", None) or {})
    carrier.global_["intent_decision"] = goal
    pack = {"episode_id": eid, "action": action, "tool": tool, "execution_intent": "build",
            "source_action_goal": goal, "execution_inputs": {}}
    carrier.meta = dict(getattr(carrier, "meta", None) or {})
    carrier.meta["execution_action_pack"] = pack
    if motor:
        carrier.meta["motor_program"] = _program()
    # The real AUTO fold echoes the source and executed plan independently of
    # the expected Global A / Meta A slots, for successful and failed actions.
    carrier.auto = {
        "episode_id": eid,
        "action": action,
        "source_action_goal": deepcopy(goal),
        "execution_pack": deepcopy(pack),
        **deepcopy(auto),
    }
    return carrier


class Carrier:
    """The carrier fields Meta B, Global B and the journal read."""

    def __init__(self, **dress):
        self.episode_id = EID
        self.thread_id = THREAD
        self.opened_by = "operator_command"
        self.pressure = {
            "opened_by": "operator_command",
            "contributors": [{"type": "operator_command", "source": "operator",
                              "metadata": {"message": "build and test the parser"}}],
        }
        self.global_: dict = {}
        self.meta: dict = {}
        self.auto: dict = {}
        self.cascade_status = "completed"
        self.pipeline_id = 0
        self.work_kind = "operator_turn"
        _dress(self, **dress)


def build(cycle_kind="success", **auto_extra) -> Carrier:
    return Carrier(intent="USE_SKILL", action="USE_SKILL", tool="skill", lane="agent",
                   auto=_auto(cycle_kind, **auto_extra), motor=True)


def reply(cycle_kind="success") -> Carrier:
    return Carrier(intent="RESPOND", action="RESPONSE", tool=None, lane="chat",
                   auto=_auto(cycle_kind, outputs={"response_text": "Here is the answer."}),
                   motor=False)


def idle() -> Carrier:
    return Carrier(intent="IDLE", action="IDLE", tool=None, lane="chat",
                   auto=_auto("idle"), motor=False)


def fold(carrier):
    """The real post-action producers: Meta B, then the whole Global B chain."""
    from global_loop.loop import run_global_b
    from meta_loop.meta_b import run_meta_b_front

    assert run_meta_b_front(carrier) is not None
    run_global_b(carrier, carrier.meta["reflection"])
    return carrier


@pytest.fixture
def bucket():
    """This thread owns the episode's SRAM bucket, as the pipeline does during a cascade."""
    from memory import episode_sram

    episode_sram.open_bucket(EID, pipeline_id=0)
    yield
    episode_sram.reset()


def revision(rev: int, sha: str, kind: str = "changed") -> None:
    from memory.auto_evidence import capture_artifact_revision

    capture_artifact_revision(SimpleNamespace(episode_id=EID), path="parser.py", revision=rev,
                              content_sha256=sha, change_kind=kind)


def verified(rev: int, sha: str, *, passed: bool, criterion: str = CRITERION) -> None:
    from memory.auto_evidence import capture_verification

    capture_verification(SimpleNamespace(episode_id=EID), criterion=criterion, path="parser.py",
                         revision=rev, content_sha256=sha, passed=passed)


def outcome_of(carrier):
    """The new outcome extractor. Absent before WO-06, which is the failure that says so."""
    from pressure_loop import episode_journal

    extract = getattr(episode_journal, "episode_outcome_from_episode", None)
    assert extract is not None, (
        "there is no outcome extractor: nothing separates 'the cascade finished' from 'the "
        "task succeeded' from 'a check passed'"
    )
    return extract(carrier)


def checks_of(carrier) -> dict:
    checks = carrier.meta["reflection"].get("checks")
    assert isinstance(checks, dict), (
        "Meta B recorded no criteria checks, so a failed acceptance criterion is invisible to "
        "everything after it"
    )
    return checks


# ── Step 1: the RED seed ────────────────────────────────────────────────────


def test_a_completed_cascade_with_a_failed_objective_is_never_a_success(bucket):
    """The order's seed: a real GlobalOutcomePack with nested status failed, cascade completed,
    and a failed criterion receipt."""
    from pressure_loop.episode import _prior_episode_success
    from pressure_loop.episode_journal import prior_episode_from_episode

    revision(1, SHA_A)
    verified(1, SHA_A, passed=False)
    carrier = fold(build("fail", failure_kind="tests_failed", failure_reason="2 tests failed"))
    carrier.cascade_status = "completed"

    assert carrier.global_["outcome"]["outcome"]["status"] == "failed", "the real nested pack"
    row = prior_episode_from_episode(carrier)
    assert _prior_episode_success(row) is False, (
        "the cognition cascade finished and the task failed its acceptance criterion; the "
        "success helper still counts it as a success because it reads cascade completion"
    )

    outcome = outcome_of(carrier)
    assert outcome.cascade_status == "completed"
    assert outcome.task_outcome == "failure"
    assert outcome.verification_status == "failed"


# ── Step 2: deterministic Meta B checks, bound to revisions ─────────────────


def test_a_failed_check_names_its_criterion_and_the_revision_it_checked(bucket):
    revision(1, SHA_A)
    verified(1, SHA_A, passed=False)
    carrier = fold(build("fail"))

    checks = checks_of(carrier)
    assert checks["method"] == "meta_b.criteria_check"
    assert checks["method_version"] == 1
    assert checks["criteria_revision"], "a check must name the contract revision it applied"
    (row,) = checks["results"]
    assert row["criterion"] == CRITERION and row["status"] == "failed"
    assert (row["checked_revision"], row["checked_sha256"]) == (1, SHA_A)
    assert (row["current_revision"], row["current_sha256"]) == (1, SHA_A)
    assert row["evidence_event_ids"], "the check must cite the receipt it read"

    assessment = outcome_of(carrier).assessments["verification_status"]
    assert assessment["criteria_revision"] == checks["criteria_revision"]
    assert SHA_A in assessment["artifact_revision"] and "parser.py@1" in assessment["artifact_revision"]
    assert list(assessment["check_refs"]) == row["evidence_event_ids"]


def test_a_passed_check_on_the_current_revision_is_a_verified_success(bucket):
    revision(1, SHA_A)
    verified(1, SHA_A, passed=True)
    carrier = fold(build("success"))

    outcome = outcome_of(carrier)
    assert (outcome.action_status, outcome.task_outcome, outcome.verification_status) == (
        "succeeded", "success", "passed")
    assert outcome.assessments["verification_status"]["author"] == "meta_b", (
        "a verified status must be attributed to the host check, never to the model"
    )


def test_a_check_of_an_earlier_revision_does_not_verify_the_current_one(bucket):
    revision(1, SHA_A)
    verified(1, SHA_A, passed=True)
    revision(2, SHA_B)  # the file changed after it was checked
    carrier = fold(build("success"))

    (row,) = checks_of(carrier)["results"]
    assert row["status"] == "stale"
    assert (row["checked_revision"], row["current_revision"]) == (1, 2)
    outcome = outcome_of(carrier)
    assert outcome.task_outcome == "success"
    assert outcome.verification_status == "not_performed", (
        "the pass belonged to bytes that no longer exist; the current revision is unchecked"
    )


def test_a_criterion_with_no_check_is_not_performed_never_passed(bucket):
    carrier = fold(build("success"))

    (row,) = checks_of(carrier)["results"]
    assert row["status"] == "not_performed"
    assert outcome_of(carrier).verification_status == "not_performed"


def test_an_unrecognised_receipt_shape_fails_with_attribution(bucket):
    from memory.auto_evidence import VERIFICATION_RESULT
    from memory.episode_events import record_event

    event = record_event(EID, operation_id="malformed-check", phase="auto",
                         event_kind=VERIFICATION_RESULT,
                         payload={"episode_id": EID, "path": "parser.py"})
    carrier = build("success")

    from meta_loop.meta_b import run_meta_b_front

    with pytest.raises(ContinuityContractError, match=event.event_id):
        run_meta_b_front(carrier)


# ── Step 1 cases: honesty across the other outcomes ─────────────────────────


def test_an_unverified_reply_is_a_success_but_not_a_verified_one():
    carrier = fold(reply("success"))

    outcome = outcome_of(carrier)
    assert (outcome.action_status, outcome.task_outcome) == ("succeeded", "success")
    assert outcome.verification_status == "not_performed"


def test_a_legitimate_idle_episode_is_not_applicable_never_a_failure():
    carrier = fold(idle())

    outcome = outcome_of(carrier)
    assert outcome.action_status == "not_attempted"
    assert outcome.task_outcome == "not_applicable"
    assert outcome.verification_status == "not_applicable"


#: What ``brain_skill_executor`` returns when the operator stops a dispatch.
STOPPED_DISPATCH = {
    "job_status": "stopped",
    "status": "partial",
    "summary": "Stopped at the operator's request.",
    "tool_calls": [],
    "unresolved_blockers": ["operator interrupted the dispatch"],
}


def test_a_valid_cancellation_is_cancelled_never_a_failure(bucket):
    carrier = fold(build("partial", outputs={"qwen_skill_result": dict(STOPPED_DISPATCH)}))

    outcome = outcome_of(carrier)
    assert outcome.action_status == "cancelled"
    assert outcome.task_outcome == "cancelled"
    assert outcome.verification_status == "not_applicable"


def test_a_partial_result_is_partial(bucket):
    carrier = fold(build("partial"))

    outcome = outcome_of(carrier)
    assert (outcome.action_status, outcome.task_outcome) == ("partial", "partial")


def test_a_task_waiting_on_the_operator_is_blocked(bucket):
    carrier = fold(build("pending_input"))

    outcome = outcome_of(carrier)
    assert (outcome.action_status, outcome.task_outcome) == ("blocked", "blocked")
    assert outcome.verification_status == "not_performed"


def test_an_unknown_cascade_value_is_refused_with_attribution():
    carrier = fold(reply("success"))
    carrier.cascade_status = "mostly_done"

    with pytest.raises(ContinuityContractError, match="mostly_done"):
        outcome_of(carrier)


# ── Step 6: typed populations, never a measured 0.5 ──────────────────────────


def _journal(tmp_path, monkeypatch):
    from pressure_loop.episode import EpisodeStore

    journal = tmp_path / "episode_journal.jsonl"
    monkeypatch.setattr("pressure_loop.policy.episode_journal_path", lambda policy=None: journal)
    store = EpisodeStore(reflection_window_path=tmp_path / "window.json",
                         trajectory_path=tmp_path / "trajectory.json")
    return store, journal


def _close_folded(store, *, intent, action, tool, lane, auto):
    from tests.conftest import close_episode_with_window_rules

    ep = store.open(opened_by="operator_command", pipeline_id=0, work_kind="operator_turn")
    ep.thread_id = THREAD
    fold(_dress(ep, intent=intent, action=action, tool=tool, lane=lane, auto=auto, motor=False))
    close_episode_with_window_rules(store, ep, cascade_status="completed")


def _population(store):
    population_of = getattr(store, "recent_outcome_population", None)
    assert population_of is not None, (
        "the store still reports one success RATE, which cannot say how many cases it is "
        "made of or that it measured nothing"
    )
    return population_of(10)


def test_a_failed_task_in_a_completed_cascade_counts_zero_verified_successes(tmp_path, monkeypatch):
    store, _ = _journal(tmp_path, monkeypatch)
    _close_folded(store, intent="USE_TOOL", action="USE_TOOL", tool="web_fetch", lane="agent",
                  auto=_auto("fail", failure_kind="fetch_failed"))

    population = _population(store)
    assert population.verified_successes == 0
    assert population.task_failures == 1
    assert population.task_success_rate == 0.0


def test_no_eligible_verified_case_is_an_undefined_rate_with_counts(tmp_path, monkeypatch):
    store, _ = _journal(tmp_path, monkeypatch)
    _close_folded(store, intent="USE_TOOL", action="USE_TOOL", tool="web_fetch", lane="agent",
                  auto=_auto("fail"))
    _close_folded(store, intent="RESPOND", action="RESPONSE", tool=None, lane="chat",
                  auto=_auto("success", outputs={"response_text": "ok"}))
    _close_folded(store, intent="IDLE", action="IDLE", tool=None, lane="chat", auto=_auto("idle"))

    population = _population(store)
    assert population.total == 3
    assert population.verified_success_rate is None, "no check ran, so there is no verified rate"
    assert population.verification_performed == 0
    assert (population.task_successes, population.task_failures) == (1, 1)
    assert population.unverified_successes == 1
    assert population.not_applicable == 1
    assert population.task_success_rate == pytest.approx(0.5)


def test_an_empty_journal_is_an_undefined_rate_never_one_half(tmp_path, monkeypatch):
    store, _ = _journal(tmp_path, monkeypatch)

    population = _population(store)
    assert population.total == 0
    assert population.task_success_rate is None
    assert population.verified_success_rate is None


def test_a_legacy_row_without_outcome_axes_counts_as_unknown_never_success(tmp_path, monkeypatch):
    import json

    store, journal = _journal(tmp_path, monkeypatch)
    legacy = {"episode_id": "ep-legacy", "ignition_reason": "operator_command", "intent": "LEARN",
              "action": "USE_TOOL", "outcome": {"status": "success"}, "trajectory_update": None,
              "cascade_status": "completed"}
    journal.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    population = _population(store)
    assert population.total == 1
    assert population.unknown == 1
    assert population.task_successes == 0


def test_the_neuromod_expectation_is_labelled_a_prior_when_nothing_was_measured():
    """The one live consumer. An RPE needs an expectation; with no measured outcomes it uses a
    stated prior, and says so, rather than presenting 0.5 as something the journal measured."""
    import cognition_chain
    from pressure_loop import episode as episode_module

    OutcomePopulation = getattr(episode_module, "OutcomePopulation", None)
    expectation = getattr(cognition_chain, "_neuromod_expectation", None)
    assert OutcomePopulation is not None and expectation is not None, (
        "the neuromod consumer has no typed-population expectation"
    )

    undefined = OutcomePopulation.from_axes(())
    value, source = expectation(undefined)
    assert source == "prior" and value == pytest.approx(0.5)

    measured = OutcomePopulation.from_axes(
        ({"task_outcome": "success", "verification_status": "not_performed"},
         {"task_outcome": "failure", "verification_status": "failed"},
         {"task_outcome": "failure", "verification_status": "not_performed"}))
    value, source = expectation(measured)
    assert source == "measured" and value == pytest.approx(1 / 3)
