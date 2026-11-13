import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import cognition_chain
from pressure_loop import pressure


def test_normal_close_never_reexecutes_or_mints_legacy_continuation():
    tree = ast.parse((Path(__file__).parents[2] / "cognition_chain.py").read_text(encoding="utf-8-sig"))
    def calls_in(name):
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
        return [call.func.id for call in ast.walk(node)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)]

    calls = calls_in("_start_meta_b_global_b")
    assert "_reexecute_builder_from_report" not in calls
    assert "_mint_agent_continuation" not in calls
    assert "_start_meta_b_global_b" not in calls
    # The close settles the operator turn, and only that settlement stages Omega's successor.
    assert "_settle_operator_turn" in calls
    # T8: staging happens from the fold's one recoverable transaction record.
    assert "publish_cognitive_transaction" in calls_in("_settle_operator_turn")


def test_same_episode_reexecution_entry_points_are_gone():
    assert not hasattr(cognition_chain, "_reexecute_builder_from_report")
    assert not hasattr(cognition_chain, "_mint_agent_continuation")


def _control(decision):
    assessment = dict.fromkeys(("who", "what", "when", "where", "why", "how",
                                "desired_state", "observed_state"), "episode evidence")
    assessment.update(gap="" if decision == "DONE" else "one contract is still red",
                      evidence=["focused tests: 1 failed"])
    unresolved = dict(intent="USE_SKILL", confidence=0.8, reason="repair the red contract",
                      setpoints={"execution_debt_delta": 0.7}, desired_state="all contracts green",
                      observed_state="one contract red", resolution_conditions=["focused tests pass"])
    return dict(assessment=assessment, decision=decision,
                pressure=None if decision == "DONE" else unresolved, synthesis="summary")


@pytest.mark.parametrize("decision,successors", [("PRESSURE", 1), ("DONE", 0)])
def test_close_stages_at_most_one_successor_and_never_reruns_auto(monkeypatch, tmp_path, decision, successors):
    calls = []
    monkeypatch.setattr(pressure, "_successor_path", lambda: tmp_path / "successors.json")
    monkeypatch.setattr(pressure, "_RESEED_STAGING", {})
    monkeypatch.setattr("meta_loop.meta_b.run_meta_b_front", lambda _episode: calls.append("meta_b") or {})
    monkeypatch.setattr("global_loop.loop.run_global_b", lambda _episode, _reflection: calls.append("global_b"))
    monkeypatch.setattr("omega_loop.episode.packet.capture_execution_request_context",
                        lambda _episode, request_text: {"text": request_text, "origin": "operator_query"})
    monkeypatch.setattr("omega_loop.episode.packet.refresh_omega_execution_foldback", lambda *_a, **_k: None)
    monkeypatch.setattr("pressure_loop.agent_lane.carried_origin_opened_by", lambda _episode: "")
    monkeypatch.setattr("auto_loop.loop.run_auto", lambda _episode: calls.append("auto"))
    monkeypatch.setattr(cognition_chain, "_operator_request_text", lambda _episode: "build the parser")
    monkeypatch.setattr(cognition_chain, "_run_skill_foldback", lambda _episode: None)
    monkeypatch.setattr(cognition_chain, "_refresh_packet_wip_from_execution_report", lambda _episode: None)
    # The legacy fields an old fold looped on inside the same episode.
    result = SimpleNamespace(control_decision=_control(decision), build_accepted=False,
                             intent_decision={"intent": "APPLY", "reason": "go round again"})
    monkeypatch.setattr(cognition_chain, "_run_omega_episode_cognition",
                        lambda _episode: calls.append("omega") or result)
    monkeypatch.setattr(cognition_chain, "_execute_pending_workspace_port", lambda _ep_id: calls.append("port"))
    episode = SimpleNamespace(episode_id="agent-parent", meta={}, auto={}, pressure={}, global_={
        "omega_packet": {"original_objective": "build the parser", "omega_lane": "agent"}})

    cognition_chain._start_meta_b_global_b(object(), episode)

    assert calls == ["meta_b", "global_b", "omega", "port"]
    staged = pressure.staged_signals()
    assert len(staged) == successors
    assert all((row.source, row.metadata["parent_episode_id"]) == ("omega", "agent-parent") for row in staged)
