"""Phase 6L — Omega/DeepSleep skill consolidation + capability learning rules."""

from __future__ import annotations

from deepsleep_loop.skill_consolidation import (
    consolidate_skill_episode,
    derive_capability_signal,
)
from meta_loop.skill.foldback import fold_skill_result


def _result(status="success", **over):
    base = {
        "type": "qwen_skill_execution_result",
        "episode_id": "ep_1",
        "dispatch_id": "dispatch_x",
        "skill_name": "codex-grade-coding",
        "skill_id": "5.01.qwenskill-codex-grade-coding",
        "execution_kind": "REPAIR",
        "status": status,
        "summary": f"{status} summary",
        "tool_calls": [],
        "files_changed": [],
        "commands_run": [],
        "tests_run": [],
        "diff_summary": {},
        "artifacts": [],
        "evidence": {},
        "unresolved_blockers": [],
        "failure_class": None,
        "confidence": 0.0,
        "guard_status": {},
        "duration_ms": 0,
    }
    base.update(over)
    return base


class _Spy:
    def __init__(self):
        self.calls = []

    def reinforce(self, name, delta, domain, scope):
        self.calls.append(("reinforce", name, round(delta, 4)))
        return 0.55

    def weaken(self, name, delta, domain, scope, reason):
        self.calls.append(("weaken", name, round(delta, 4)))
        return 0.45

    def record_failure(self, kind, context, episode_id=""):
        self.calls.append(("failure", kind, context))
        return True


def _consolidate(status, *, objective="fix the bug", materially_advanced=False, **over):
    r = _result(status, **over)
    folded = fold_skill_result(r, objective=objective)
    spy = _Spy()
    out = consolidate_skill_episode(
        folded,
        r,
        objective=objective,
        objective_materially_advanced=materially_advanced,
        reinforce_fn=spy.reinforce,
        weaken_fn=spy.weaken,
        record_failure_fn=spy.record_failure,
    )
    return out, spy


def _kinds(spy):
    return [c[0] for c in spy.calls]


# ── acceptance: success reinforces selected capability ────────────────────────
def test_grounded_success_reinforces_capability():
    out, spy = _consolidate("success", files_changed=["a.py"])
    assert out["applied"]["reinforced"] is True and out["applied"]["new_strength"] == 0.55
    assert out["record"]["capability_signal"]["signal"] == "reinforce"
    assert _kinds(spy) == ["reinforce"]


def test_ungrounded_success_is_neutral_not_reinforced():
    out, spy = _consolidate("success")  # no files/tools/evidence
    assert out["applied"]["reinforced"] is False
    assert out["record"]["capability_signal"]["signal"] == "neutral"
    assert spy.calls == []


# ── acceptance: failed records failure memory ─────────────────────────────────
def test_failure_records_failure_memory():
    out, spy = _consolidate("failure", failure_class="tool_error")
    assert out["applied"]["failure_recorded"] is True and out["applied"]["reinforced"] is False
    assert ("failure", "skill_failure", "tool_error") in spy.calls


# ── acceptance: blocked records safety memory (not capability failure) ─────────
def test_blocked_records_safety_memory_not_capability():
    out, spy = _consolidate("blocked", guard_status={"reason": "self_modification denied"})
    assert out["applied"]["safety_recorded"] is True
    assert out["applied"]["reinforced"] is False and out["applied"]["weakened"] is False
    assert _kinds(spy) == ["failure"] and spy.calls[0][1] == "safety"


# ── acceptance: pending approval is neutral ───────────────────────────────────
def test_pending_approval_is_neutral():
    out, spy = _consolidate("requires_confirmation")
    a = out["applied"]
    assert a["reinforced"] is False and a["weakened"] is False
    assert a["failure_recorded"] is False and a["safety_recorded"] is False
    assert spy.calls == []  # neither rewarded nor punished
    assert out["record"]["operator_confirmation_status"] == "pending"


# ── acceptance: malformed does not reinforce (or weaken) the skill ────────────
def test_malformed_weakens_bridge_not_skill():
    out, spy = _consolidate("malformed_result")
    assert out["applied"]["bridge_recorded"] is True
    assert out["applied"]["reinforced"] is False and out["applied"]["weakened"] is False
    assert (
        _kinds(spy) == ["failure"] and spy.calls[0][1] == "bridge"
    )  # bridge memory, no capability touch


# ── forbidden-tool weakens skill + records failure ────────────────────────────
def test_forbidden_tool_weakens_skill():
    out, spy = _consolidate("failure", failure_class="forbidden_tool_use:run_command")
    assert out["record"]["capability_signal"]["signal"] == "weaken_skill"
    assert out["applied"]["weakened"] is True and out["applied"]["new_strength"] == 0.45
    assert "weaken" in _kinds(spy) and "failure" in _kinds(spy)


# ── partial success ───────────────────────────────────────────────────────────
def test_partial_materially_advanced_reinforces_smaller_delta():
    out, spy = _consolidate("partial", materially_advanced=True, files_changed=["a.py"])
    assert out["record"]["capability_signal"]["signal"] == "reinforce_partial"
    assert out["applied"]["reinforced"] is True
    assert spy.calls == [
        ("reinforce", "codex-grade-coding", 0.02)
    ]  # smaller than the 0.05 full credit


def test_partial_without_advance_is_neutral():
    out, spy = _consolidate("partial", files_changed=["a.py"])
    assert out["record"]["capability_signal"]["signal"] == "neutral"
    assert spy.calls == []


# ── repair pressure flag ──────────────────────────────────────────────────────
def test_repair_pressure_flag_on_failure_not_success():
    assert _consolidate("failure", failure_class="x")[0]["applied"]["repair_pressure"] is True
    assert _consolidate("success", files_changed=["a.py"])[0]["applied"]["repair_pressure"] is False


# ── operator confirmation provenance ──────────────────────────────────────────
def test_operator_confirmation_status_confirmed_and_not_required():
    confirmed, _ = _consolidate(
        "success", files_changed=["a.py"], guard_status={"confirmation_required": True}
    )
    assert confirmed["record"]["operator_confirmation_status"] == "confirmed"
    plain, _ = _consolidate("success", files_changed=["a.py"])
    assert plain["record"]["operator_confirmation_status"] == "not_required"


# ── DeepSleep summary: selected skill + result + why (recall) ─────────────────
def test_deepsleep_summary_has_skill_and_why():
    # a terminal verification (TEST) success closes the objective
    out, _ = _consolidate(
        "success", execution_kind="TEST", files_changed=["a.py"], tests_run=["t.py::ok"]
    )
    s = out["summary"]
    assert s["type"] == "deepsleep_skill_summary"
    assert (
        s["selected_skill"] == "codex-grade-coding"
        and s["skill_id"] == "5.01.qwenskill-codex-grade-coding"
    )
    assert s["objective"] == "fix the bug" and s["outcome"] == "skill_success"
    assert (
        "codex-grade-coding" in s["why"] and "fix the bug" in s["why"]
    )  # recall: what skill + why
    assert s["closure"] == "closed" and s["tests_run"] == ["t.py::ok"]


def test_workflow_candidate_only_on_grounded_success():
    ok, _ = _consolidate(
        "success", files_changed=["a.py"], tool_calls=[{"tool_name": "patch_file"}]
    )
    cand = ok["record"]["workflow_memory_candidate"]
    assert (
        cand
        and cand["skill_name"] == "codex-grade-coding"
        and cand["tool_sequence"] == ["patch_file"]
    )
    fail, _ = _consolidate("failure", failure_class="x")
    assert fail["record"]["workflow_memory_candidate"] is None


def test_blocked_success_does_not_reinforce_or_create_workflow_memory():
    blocked, spy = _consolidate(
        "success",
        files_changed=["a.py"],
        unresolved_blockers=["evidence_unsatisfied:tests"],
    )
    assert blocked["record"]["capability_signal"]["signal"] == "neutral"
    assert blocked["record"]["workflow_memory_candidate"] is None
    assert spy.calls == []


# ── signal contract validates ─────────────────────────────────────────────────
def test_derived_signals_validate_clean():
    for status, over in [
        ("success", {"files_changed": ["a"]}),
        ("failure", {"failure_class": "x"}),
        ("blocked", {"guard_status": {"reason": "r"}}),
        ("malformed_result", {}),
        ("requires_confirmation", {}),
        ("timeout", {}),
    ]:
        r = _result(status, **over)
        meta_b = fold_skill_result(r, objective="o")["meta_b"]
        sig = derive_capability_signal(meta_b, r)
        assert sig.validate() == []


def test_weaken_bridge_rejects_positive_delta():
    from schema.skill_consolidation_record import CapabilitySignal

    bad = CapabilitySignal("weaken_bridge", "s", "5.01.qwenskill-s", 0.05, "bridge_reliability")
    assert any("negative delta" in e for e in bad.validate())
