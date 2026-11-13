"""The fold's verdict is emitted so the execution-report decision is auditable.

Live finding 2026-07-02 (episode 384e9e9c): an operator build's skill_success carried an
evidence_unsatisfied blocker, so Global B silently took the verification_needed path and the
completion notice never fired — with NOTHING in the terminal log to say why (the fold span was
green, no branch errored). One DATA line carrying the verdict makes the branch auditable.
"""

from types import SimpleNamespace

import pytest


def _episode(qr, *, evidence_required=(), opened_by="operator_command"):
    return SimpleNamespace(
        episode_id=qr["episode_id"],
        opened_by=opened_by,
        pressure={},
        meta={
            "motor_program": {
                "shadow": True,
                "outcome_contract": {
                    "acceptance_criteria": ["requested behavior is verified"],
                },
            },
            "execution_action_pack": {
                "action": "USE_SKILL",
                "execution_inputs": {
                    "objective": "rebuild the csv_stats project",
                    "skill_execution_pack": {"evidence_required": list(evidence_required)},
                },
            }
        },
        auto={"outputs": {"skill_result": qr, "skill_objective": "rebuild the csv_stats project"}},
        global_={},
    )


def _qr(**over):
    qr = {
        "status": "success",
        "summary": "done",
        "progress_digest": "",
        "files_changed": [],
        "episode_id": "ep-verdict",
        "dispatch_id": "d-verdict",
        "skill_name": "testing-strategy",
        "skill_id": "i",
        "execution_kind": "TEST",
        "evidence": {},
    }
    qr.update(over)
    return qr


@pytest.fixture()
def harness(monkeypatch):
    import cognition_chain as cc  # noqa: F401 — imported for the callers below
    import deepsleep_loop.skill_consolidation as dsc
    import guardian.terminal_feedback as tf
    from memory import episode_sram
    import pressure_loop.proactive_chat_messages as pcm

    emits = []
    monkeypatch.setattr(
        tf,
        "emit_data",
        lambda system, phase, **kw: emits.append({"system": system, "phase": phase, **kw}),
    )
    monkeypatch.setattr(pcm, "append_update", lambda **kw: None)
    monkeypatch.setattr(dsc, "consolidate_skill_episode", lambda *a, **kw: {})
    import core.build_verification as bv

    monkeypatch.setattr(
        bv,
        "verify_build_against_request",
        lambda **kw: {"checks": [], "all_green": False, "summary": "red: request_coverage"},
    )
    episode_sram.reset()
    episode_sram.open_bucket("ep-verdict", pipeline_id=0)
    yield emits
    episode_sram.reset()


def _verdicts(emits):
    return [e for e in emits if e["phase"] == "skill_foldback_verdict"]


def test_clean_success_emits_objective_complete(harness):
    import cognition_chain as cc

    cc._run_skill_foldback(_episode(_qr()))
    v = _verdicts(harness)
    assert len(v) == 1
    assert v[0]["outcome_kind"] == "skill_success"
    assert v[0]["objective_complete"] is True
    assert v[0]["more_work_needed"] is False


def test_evidence_gated_success_emits_the_blocker(harness):
    # the exact live case: TEST dispatch demands tests_run+coverage, qwen returned evidence {}.
    import cognition_chain as cc

    cc._run_skill_foldback(_episode(_qr(), evidence_required=("tests_run", "coverage")))
    v = _verdicts(harness)
    assert len(v) == 1
    assert v[0]["objective_complete"] is False
    assert v[0]["more_work_needed"] is True
    assert "evidence_unsatisfied:tests_run,coverage" in v[0]["blockers"]


def test_yield_emits_in_progress_verdict(harness):
    import cognition_chain as cc

    cc._run_skill_foldback(_episode(_qr(status="yielded")))
    v = _verdicts(harness)
    assert len(v) == 1
    assert v[0]["outcome_kind"] == "skill_yielded"
    assert v[0]["objective_complete"] is False
