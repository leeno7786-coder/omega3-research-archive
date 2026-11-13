"""Verified motor mastery and pathway-specific neuromodulation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from autonomic.pathway_credit import compute_pathway_credit, finalize_pathway_outcome
from deepsleep_loop._deepsleep_state import DeepSleepInput, MasteryRecord
from deepsleep_loop.mastery.run import update_mastery
from pressure_loop.episode_journal import enriched_episode_from_episode
from schema.execution_evidence import ExecutionEvidence
from schema.motor_program import MotorProgram
from tests.core.deepsleep_window_fixtures import producer_outcome_axes


def _attempt_record(*, verdict: str, claimed_delta: float = 0.0) -> dict:
    return {
        "program_id": "mp1",
        "pathway_id": "derive-implement-verify",
        "pathway_revision": 1,
        "episode_id": "ep1",
        "outcome_id": "out1",
        "task_context": "learn backpropagation",
        "domain": "machine-learning",
        "verification_verdict": verdict,
        "claimed_knowledge_delta": claimed_delta,
        "claimed_application_delta": claimed_delta,
        "pre_mastery": {"knowledge_mastery": 0.2, "application_mastery": 0.1},
        "expected_success": 0.4,
        "novelty": 0.8,
        "repeated_failure": False,
        "failure_fingerprint": "",
    }


def _updated_mastery(*, knowledge: float, application: float) -> MasteryRecord:
    return MasteryRecord(
        domain_id="machine-learning",
        domain_name="machine-learning",
        knowledge_mastery=knowledge,
        application_mastery=application,
    )


def test_unverified_success_cannot_gain_mastery_or_pathway_credit():
    outcome = finalize_pathway_outcome(
        _attempt_record(verdict="unverified", claimed_delta=0.4),
        [_updated_mastery(knowledge=0.6, application=0.5)],
    )
    assert outcome is not None
    assert outcome.knowledge_delta == 0.0
    assert outcome.application_delta == 0.0
    assert outcome.post_mastery == outcome.pre_mastery
    assert outcome.neuromodulator_response["dopamine"] <= 0.0


def test_verified_outcome_derives_real_mastery_delta_not_model_claim():
    outcome = finalize_pathway_outcome(
        _attempt_record(verdict="verified", claimed_delta=0.7),
        [_updated_mastery(knowledge=0.35, application=0.15)],
    )
    assert outcome is not None
    assert outcome.knowledge_delta == pytest.approx(0.15)
    assert outcome.application_delta == pytest.approx(0.05)
    assert outcome.episode_id == "ep1"
    assert outcome.outcome_id == "out1"


def test_surprising_verified_success_gets_positive_pathway_rpe():
    credit = compute_pathway_credit(
        actual_success=True,
        expected_success=0.2,
        novelty=0.9,
        repeated_failure=False,
    )
    assert credit["dopamine"] > 0.0
    assert credit["plasticity"] >= 0.8


def test_neuromodulation_does_not_mutate_mastery_store(monkeypatch):
    writes = []
    monkeypatch.setattr(
        "deepsleep_loop.mastery_store.upsert_mastery",
        lambda *args, **kwargs: writes.append(args),
    )
    compute_pathway_credit(True, 0.1, 1.0, False)
    assert writes == []


def test_unverified_motor_attempt_is_not_admitted_to_mastery(tmp_path):
    entry = {
        "episode_id": "ep1",
        "outcome": {"status": "success"},
        "outcome_axes": producer_outcome_axes(success=True),
        "motor_program": {"program_id": "mp1"},
        "motor_attempt_assessment": _attempt_record(verdict="unverified", claimed_delta=0.4),
        "omega_result": {
            "mastery_effects": [
                {
                    "domain": "machine-learning",
                    "knowledge_mastery": 0.6,
                    "application_mastery": 0.5,
                }
            ]
        },
    }
    ds_input = DeepSleepInput("ep1", {}, [entry], {})
    mastery_path = tmp_path / "mastery.json"

    assert update_mastery(ds_input, mastery_path=mastery_path) == []
    assert not mastery_path.exists()


def test_motor_mastery_uses_bounded_factual_evidence_not_huge_model_claim(tmp_path):
    from deepsleep_loop.mastery_store import upsert_mastery

    mastery_path = tmp_path / "mastery.json"
    upsert_mastery(
        _updated_mastery(knowledge=0.2, application=0.1),
        path=mastery_path,
    )
    attempt = _attempt_record(verdict="verified", claimed_delta=0.8)
    attempt["execution_evidence"] = {
        "program_id": "mp1",
        "episode_id": "ep1",
        "status": "success",
        "sources_consulted": ["https://example.test/paper"],
        "artifacts": ["derivation notes"],
        "files_changed": ["src/model.py"],
        "tests_run": ["pytest -q"],
        "verification_results": ["tests pass"],
    }
    entry = {
        "episode_id": "ep1",
        "outcome": {"status": "success"},
        "outcome_axes": producer_outcome_axes(success=True),
        "motor_program": {"program_id": "mp1"},
        "motor_attempt_assessment": attempt,
        "omega_result": {
            "mastery_effects": [
                {
                    "domain": "machine-learning",
                    "knowledge_mastery": 0.99,
                    "application_mastery": 0.99,
                }
            ]
        },
    }

    records = update_mastery(DeepSleepInput("ep1", {}, [entry], {}), mastery_path=mastery_path)

    assert records[0].knowledge_mastery == pytest.approx(0.25)
    assert records[0].application_mastery == pytest.approx(0.15)


def test_verified_web_evidence_credits_knowledge_not_application(tmp_path):
    from deepsleep_loop.mastery_store import upsert_mastery

    mastery_path = tmp_path / "mastery.json"
    upsert_mastery(
        _updated_mastery(knowledge=0.2, application=0.1),
        path=mastery_path,
    )
    attempt = _attempt_record(verdict="verified")
    attempt["execution_evidence"] = {
        "program_id": "mp1",
        "episode_id": "ep1",
        "status": "success",
        "sources_consulted": ["primary source https://example.test/paper"],
        "artifacts": ["article extract"],
        "verification_results": ["source verified"],
    }
    entry = {
        "episode_id": "ep1",
        "outcome": {"status": "success"},
        "outcome_axes": producer_outcome_axes(success=True),
        "motor_program": {"program_id": "mp1"},
        "motor_attempt_assessment": attempt,
        "omega_result": {
            "mastery_effects": [
                {
                    "domain": "machine-learning",
                    "knowledge_mastery": 1.0,
                    "application_mastery": 1.0,
                }
            ]
        },
    }

    records = update_mastery(DeepSleepInput("ep1", {}, [entry], {}), mastery_path=mastery_path)

    assert records[0].knowledge_mastery == pytest.approx(0.25)
    assert records[0].application_mastery == pytest.approx(0.1)


def _canonical_program() -> MotorProgram:
    return MotorProgram.from_dict(
        {
            "program_id": "mp1",
            "parent_program_id": "",
            "outcome_id": "out1",
            "episode_id": "ep1",
            "attempt_number": 1,
            "created_at": "2026-07-11T00:00:00Z",
            "outcome_contract": {
                "objective": "implement and verify",
                "acceptance_criteria": ["tests pass"],
            },
            "pathway": {
                "pathway_id": "derive-implement-verify",
                "revision": 1,
                "name": "derive implement verify",
                "method": "derive then test",
                "operational_rationale": "ground the result",
            },
            "execution_plan": {
                "capability_class": "coding",
                "operations": ["implement"],
            },
            "evidence_contract": {
                "required_artifacts": ["src/model.py"],
                "required_checks": ["pytest"],
                "completion_signals": ["tests pass"],
                "disqualifying_failures": ["verification failed"],
            },
        }
    )


def test_execution_success_missing_required_check_is_not_verified():
    from pressure_loop import episode_journal

    evidence = ExecutionEvidence.from_dict(
        {
            "program_id": "mp1",
            "episode_id": "ep1",
            "status": "success",
            "files_changed": ["src/model.py"],
        }
    )

    assert not episode_journal._evidence_contract_admitted(_canonical_program(), evidence)


def test_completion_signal_requires_verification_result_not_command_text():
    from pressure_loop import episode_journal

    evidence = ExecutionEvidence.from_dict(
        {
            "program_id": "mp1",
            "episode_id": "ep1",
            "status": "success",
            "files_changed": ["src/model.py"],
            "tests_run": ["pytest --note 'tests pass'"],
        }
    )

    assert not episode_journal._evidence_contract_admitted(_canonical_program(), evidence)


def test_complete_code_and_web_evidence_satisfies_contract():
    from pressure_loop import episode_journal

    code_evidence = ExecutionEvidence.from_dict(
        {
            "program_id": "mp1",
            "episode_id": "ep1",
            "status": "success",
            "files_changed": ["src/model.py"],
            "tests_run": ["python -m pytest -q"],
            "verification_results": [
                {
                    "requested": "pytest",
                    "status": "passed",
                    "executor_provenance": {
                        "source": "qwen_tool_call",
                        "tool_call_id": "call-test-1",
                    },
                }
            ],
        }
    )
    assert episode_journal._evidence_contract_admitted(_canonical_program(), code_evidence)

    web_program = MotorProgram.from_dict(
        {
            **_canonical_program().to_dict(),
            "execution_plan": {
                "capability_class": "web_research",
                "operations": ["research primary source"],
            },
            "evidence_contract": {
                "required_artifacts": ["article extract"],
                "required_source_proof": ["primary source"],
                "completion_signals": ["source verified"],
            },
        }
    )
    web_evidence = ExecutionEvidence.from_dict(
        {
            "program_id": "mp1",
            "episode_id": "ep1",
            "status": "success",
            "artifacts": ["article extract"],
            "sources_consulted": ["primary source https://example.test/paper"],
            "verification_results": ["source verified"],
        }
    )
    assert episode_journal._evidence_contract_admitted(web_program, web_evidence)


def _motor_episode(status: str = "success", *, prior_attempts=None) -> SimpleNamespace:
    program = {
        "program_id": "mp1",
        "parent_program_id": "",
        "outcome_id": "out1",
        "episode_id": "ep1",
        "attempt_number": 1,
        "created_at": "2026-07-11T00:00:00Z",
        "outcome_contract": {
            "objective": "learn backpropagation",
            "acceptance_criteria": ["tests pass"],
        },
        "pathway": {
            "pathway_id": "derive-implement-verify",
            "revision": 1,
            "name": "derive implement verify",
            "method": "derive then test",
            "operational_rationale": "ground the result",
        },
        "execution_plan": {
            "capability_class": "coding",
            "operations": ["implement"],
        },
        "evidence_contract": {
            "required_checks": ["pytest"],
            "completion_signals": ["tests pass"],
        },
    }
    return SimpleNamespace(
        episode_id="ep1",
        opened_by="mastery_gap",
        pressure={},
        state="closed",
        meta={
            "motor_program": program,
            "mastery_state": {
                "machine-learning": {
                    "knowledge_mastery": 0.9,
                    "application_mastery": 0.9,
                }
            },
            "execution_action_pack": {
                "action": "EXECUTE_MOTOR_PROGRAM",
                "execution_inputs": {"domain": "machine-learning"},
            },
            **({} if prior_attempts is None else {"prior_motor_attempts": list(prior_attempts)}),
        },
        auto={
            "outputs": {
                "qwen_skill_result": {
                    "status": status,
                    "progress_digest": "work remains",
                    "failure_class": "",
                }
            }
        },
        global_={
            "intent_decision": {"intent": "LEARN"},
            "outcome": {"status": "partial"},
            "omega_packet": {
                "prior_mastery": {
                    "domain": "machine-learning",
                    "knowledge_mastery": 0.2,
                    "application_mastery": 0.1,
                    "target_mastery": 0.7,
                }
            },
            "omega_result": {
                "episode_assessment": {"novelty_score": 0.8},
                "mastery_effects": [
                    {
                        "domain": "machine-learning",
                        "knowledge_mastery": 0.6,
                        "application_mastery": 0.5,
                    }
                ],
            },
        },
        omega={},
        brain_turns=[],
        temporal={},
        learning_state={},
    )


@pytest.mark.parametrize("status", ["partial", "yielded", "success"])
def test_episode_journal_preserves_nonterminal_attempt_as_unverified(status):
    body = enriched_episode_from_episode(_motor_episode(status))
    attempt = body["motor_attempt_assessment"]
    assert attempt["program_id"] == "mp1"
    assert attempt["verification_verdict"] == "unverified"
    assert attempt["pre_mastery"] == {
        "knowledge_mastery": 0.2,
        "application_mastery": 0.1,
    }
    assert attempt["novelty"] == pytest.approx(0.8)
    assert attempt["completion_signals"] == ["tests pass"]


def test_unverified_fetch_with_committed_receipts_banks_knowledge_only(tmp_path):
    """Live 2026-07-19: every LEARN episode carries a USE_TOOL motor program
    whose fetch can never be 'verified', so the unadmitted-motor `continue`
    discarded the receipt-backed kubernetes knowledge forever (evidence_count
    stayed 0 across three LEARN episodes). A COMMITTED knowledge receipt is the
    proof the knowledge landed in canonical memory — knowledge banks; the
    application component stays motor-evidence-owned (pinned to stored)."""
    entry = {
        "episode_id": "ep-learn",
        "outcome": {"status": "success"},
        "outcome_axes": producer_outcome_axes(success=True),
        "motor_program": {"program_id": "mp1"},
        "motor_attempt_assessment": _attempt_record(verdict="unverified", claimed_delta=0.4),
        "omega_result": {
            "mastery_effects": [
                {
                    "domain": "kubernetes",
                    "knowledge_mastery": 0.1,
                    "application_mastery": 0.7,  # prose — must NOT land
                }
            ],
            "knowledge_receipts": [
                {
                    "coord": [4, 1, 0],
                    "domain": "kubernetes",
                    "path": "4.01.afdaa3ed",
                    "statement": "K8s is an orchestration system.",
                    "status": "committed",
                }
            ],
        },
    }
    mastery_path = tmp_path / "mastery.json"

    records = update_mastery(DeepSleepInput("ep-learn", {}, [entry], {}), mastery_path=mastery_path)

    assert len(records) == 1
    assert records[0].domain_id == "kubernetes"
    assert records[0].knowledge_mastery == pytest.approx(0.1)
    assert records[0].application_mastery == pytest.approx(0.0)
    assert records[0].evidence_count == 1


def test_uncommitted_receipts_do_not_unlock_prose_mastery(tmp_path):
    """A receipt that did not commit is not proof — the entry stays inert
    exactly like the pinned unverified-motor doctrine test."""
    entry = {
        "episode_id": "ep-learn",
        "outcome": {"status": "success"},
        "outcome_axes": producer_outcome_axes(success=True),
        "motor_program": {"program_id": "mp1"},
        "motor_attempt_assessment": _attempt_record(verdict="unverified", claimed_delta=0.4),
        "omega_result": {
            "mastery_effects": [
                {
                    "domain": "kubernetes",
                    "knowledge_mastery": 0.6,
                    "application_mastery": 0.5,
                }
            ],
            "knowledge_receipts": [{"domain": "kubernetes", "status": "rejected"}],
        },
    }
    mastery_path = tmp_path / "mastery.json"

    assert (
        update_mastery(DeepSleepInput("ep-learn", {}, [entry], {}), mastery_path=mastery_path) == []
    )
    assert not mastery_path.exists()


def test_a_first_attempt_records_its_expectation_as_a_prior_not_a_measurement():
    """WO-06 carry-over: with no prior attempts the producer wrote a bare 0.5, which reads exactly
    like a measured coin flip. The value is still the stated prior, but it now says so."""
    attempt = enriched_episode_from_episode(_motor_episode())["motor_attempt_assessment"]
    assert attempt["expected_success"] == pytest.approx(0.5)
    assert attempt["expected_success_source"] == "prior"
    assert attempt["expected_success_attempts"] == 0


def test_a_measured_expectation_names_the_attempts_it_rests_on():
    priors = [{"verification_verdict": "verified"}, {"verification_verdict": "unverified"}]
    attempt = enriched_episode_from_episode(
        _motor_episode(prior_attempts=priors)
    )["motor_attempt_assessment"]
    assert attempt["expected_success"] == pytest.approx(0.5)
    assert attempt["expected_success_source"] == "measured"
    assert attempt["expected_success_attempts"] == 2


def test_an_attempt_record_without_an_expectation_is_refused_not_read_as_zero():
    """A missing expectation defaulted to 0.0, so the attempt earned the whole reward as a
    surprise. It is a malformed record, not an expectation of certain failure."""
    record = _attempt_record(verdict="verified")
    del record["expected_success"]
    with pytest.raises(ValueError, match="expected_success"):
        finalize_pathway_outcome(record, [_updated_mastery(knowledge=0.35, application=0.15)])
