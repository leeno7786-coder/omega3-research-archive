"""Slice 3 of the cognition+CLI merge — APPLY grounds application_mastery.

The Omega fold-back must (a) SEE the APPLY execution evidence in the packet and
(b) fold it through the agent lane, which grounds application_mastery from it.
Without this the fold-back skips APPLY entirely and application_mastery can never rise.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omega_loop.episode.prompts import prompt_spec_for  # noqa: E402
from omega_loop.episode.types import MasteryView, OmegaEpisodeInput  # noqa: E402


def _apply_input(
    applied: dict | None = None, domain: str = "python", prior=None
) -> OmegaEpisodeInput:
    return OmegaEpisodeInput(
        episode_id="ep-apply-1",
        intent="APPLY",
        action="APPLY",
        tool=None,
        domain=domain,
        operator_text=None,
        outcome="success",
        failure_reason=None,
        raw_tool_result=None,
        recall=(),
        recall_error=None,
        prior_mastery=prior,
        omega_lane="agent",
        applied=applied
        or {
            "status": "success",
            "summary": "added a retry test",
            "has_diff": True,
            "error": None,
        },
    )


def _with_done_control(payload: dict) -> dict:
    assessment = dict.fromkeys(
        ("who", "what", "when", "where", "why", "how", "desired_state", "observed_state"),
        "execution evidence",
    )
    control = {"assessment": {**assessment, "gap": "", "evidence": ["execution outcome"]},
               "decision": "DONE", "pressure": None, "synthesis": "execution reviewed"}
    return {**payload, "control": control}


def test_apply_folds_through_the_agent_lane():
    assert prompt_spec_for(_apply_input()).name == "agent"


def test_apply_prompt_includes_execution_outcome_and_grounds_application():
    system, user = prompt_spec_for(_apply_input()).build(_apply_input())
    assert "application_mastery" in system
    assert "added a retry test" in user
    assert "success" in user


def test_apply_parse_emits_an_application_mastery_effect():
    pkt = _apply_input(prior=MasteryView("python", 0.6, 0.1, 0.8))
    payload = {
        "mastery": {
            "knowledge_mastery": 0.6,
            "application_mastery": 0.55,
            "rationale": "applied it",
        },
    }
    res = prompt_spec_for(pkt).parse(_with_done_control(payload), pkt)
    assert len(res.mastery_effects) == 1
    eff = res.mastery_effects[0]
    assert eff.domain == "python"
    assert eff.application_mastery == 0.55
    assert res.knowledge_to_ingest == ()  # applying is not learning


def test_pending_approval_apply_does_not_lower_application_mastery():
    # A staged-gate hold (Phase 5x) never ran any code — it must be NEUTRAL for
    # mastery, never grounded as a failed/empty run that lowers application. If
    # it lowered mastery, the safety gate would actively degrade the domain it
    # is protecting (perverse incentive).
    pkt = _apply_input(
        applied={"status": "pending_approval", "summary": "", "has_diff": False, "error": None},
        prior=MasteryView("python", 0.6, 0.4, 0.8),
    )
    payload = {
        "mastery": {
            "knowledge_mastery": 0.6,
            "application_mastery": 0.05,
            "rationale": "model tried to lower",
        },
    }
    res = prompt_spec_for(pkt).parse(_with_done_control(payload), pkt)
    assert res.mastery_effects == ()  # prior application_mastery (0.4) holds — no lowering


def test_blocked_apply_does_not_lower_application_mastery():
    # Same neutrality for the structural fail-closed (apply_unsafe -> status="blocked").
    pkt = _apply_input(
        applied={"status": "blocked", "summary": "", "has_diff": False, "error": "guards unwired"},
        prior=MasteryView("python", 0.6, 0.4, 0.8),
    )
    payload = {"mastery": {"knowledge_mastery": 0.6, "application_mastery": 0.0}}
    res = prompt_spec_for(pkt).parse(_with_done_control(payload), pkt)
    assert res.mastery_effects == ()


# --- the packet must surface the APPLY execution evidence -------------------


def _hermetic_apply_episode(eid: str = "ep-apply-pkt"):
    return SimpleNamespace(
        episode_id=eid,
        pressure={"opened_by": "execution_gap", "contributors": []},
        global_={
            "intent_decision": {
                "episode_id": eid,
                "intent": "APPLY",
                "confidence": 0.9,
                "reason": "test",
            }
        },
        meta={
            "execution_action_pack": {
                "episode_id": eid,
                "action": "APPLY",
                "tool": None,
                "execution_intent": "plan apply",
                "execution_inputs": {"directive": "add a retry test", "domain": "python"},
                "source_action_goal": {
                    "episode_id": eid,
                    "intent": "APPLY",
                    "confidence": 0.9,
                    "reason": "test",
                },
            }
        },
        auto={
            "cycle_kind": "success",
            "memory_status": "skipped",
            "memory": {
                "memory_required": False,
                "memory_status": "skipped",
                "memory_reason": "",
                "memory_scope": [],
                "items": [],
                "error": None,
            },
            "outputs": {
                "apply_status": "success",
                "apply_result": {
                    "git_diff": "",
                    "ledger": {"touched_files": ["retry_test.py"], "action_trail": []},
                    "summary": "added a retry test",
                },
            },
            "telemetry": {"warnings": []},
        },
    )


def test_packet_surfaces_the_apply_execution_evidence(monkeypatch):
    from global_loop.global_b import run_global_b_front
    from meta_loop.meta_b import run_meta_b_front
    from omega_loop.episode.types import OmegaEpisodeInput as OEI

    monkeypatch.setattr("omega_loop.episode.packet.load_mastery", lambda path=None: {})

    def _boom(*_a, **_k):
        raise AssertionError("invoke_runtime_generate must not run for packet build")

    monkeypatch.setattr("runtime.socket.invoke_runtime_generate", _boom, raising=False)

    ep = _hermetic_apply_episode()
    run_meta_b_front(ep)
    run_global_b_front(ep, ep.meta["reflection"])
    raw = dict((ep.global_ or {}).get("omega_packet") or {})
    raw["recall"] = tuple(raw.get("recall") or ())
    pkt = OEI(**raw)

    assert pkt.action == "APPLY"
    assert pkt.applied is not None
    assert pkt.applied["status"] == "success"
    assert pkt.applied["summary"] == "added a retry test"
    assert pkt.applied["has_diff"] is True
    assert pkt.applied["files_touched"] == 1
