"""Phase 6K — Meta B / Global B skill foldback."""

from __future__ import annotations

import pytest

from meta_loop.skill.foldback import (
    MetaBSkillLedger,
    SkillFoldbackError,
    attach_skill_foldback,
    classify_meta_b_skill_outcome,
    fold_skill_result,
    integrate_global_b_skill_outcome,
)
from schema.global_b_skill_update import CONTINUATION_SIGNALS
from schema.meta_b_skill_outcome import ALLOWED_META_B_SKILL_KINDS


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


def _fold(status="success", *, objective="fix the bug", expected_partial_ok=False, **over):
    return fold_skill_result(
        _result(status, **over), objective=objective, expected_partial_ok=expected_partial_ok
    )


# ── Meta B kind per status (acceptance) ───────────────────────────────────────
def test_success_produces_skill_success():
    # the Meta B classification is the same for every success; the dispatch is satisfied.
    f = _fold("success")
    assert f["meta_b"]["outcome_kind"] == "skill_success"
    assert f["meta_b"]["satisfied_dispatch"] is True


def test_clean_success_satisfies_and_closes_regardless_of_kind():
    # SUCCESS = STOP. The agentic executor does the whole task (implement AND self-verify)
    # in one pass, so a clean success closes the cycle — no investigate->implement->verify
    # re-chaining (which re-ran a finished build and crashed the runtime, 2026-06-25).
    for ek in ("INSPECT", "REPAIR", "WRITE", "APPLY", "TEST"):
        gb = _fold("success", execution_kind=ek)["global_b"]
        assert gb["intent_satisfied"] is True, ek
        assert gb["close_episode"] is True, ek
        assert gb["more_work_needed"] is False, ek
        assert not gb.get("continuation_pressure"), ek


def test_failure_produces_skill_failure_with_failure_class():
    f = _fold("failure", failure_class="tool_error")
    assert f["meta_b"]["outcome_kind"] == "skill_failure"
    assert f["meta_b"]["failure_class"] == "tool_error"
    assert "repair_needed" in f["global_b"]["continuation_signals"]
    assert f["global_b"]["intent_satisfied"] is False


def test_failure_without_failure_class_defaults_one():
    # Meta B rule: failure -> failure_class REQUIRED (defaulted, never empty)
    f = _fold("failure")
    assert f["meta_b"]["failure_class"]


def test_blocked_produces_skill_blocked_with_safety():
    f = _fold("blocked", guard_status={"reason": "self_modification denied"})
    assert f["meta_b"]["outcome_kind"] == "skill_blocked"
    assert f["meta_b"]["safety_blocker"] == "self_modification denied"
    assert "blocked_by_safety" in f["global_b"]["continuation_signals"]
    assert f["global_b"]["ask_operator"] is True


def test_timeout_produces_skill_timeout():
    f = _fold("timeout")
    assert f["meta_b"]["outcome_kind"] == "skill_timeout"
    assert f["meta_b"]["failure_class"] == "timeout"


def test_requires_confirmation_is_neutral_operator_input():
    f = _fold("requires_confirmation")
    assert f["meta_b"]["outcome_kind"] == "skill_requires_confirmation"
    assert f["meta_b"]["requires_operator_input"] is True
    assert f["global_b"]["ask_operator"] is True
    assert f["global_b"]["continuation_pressure"] is None  # pending approval is neutral
    assert f["global_b"]["continuation_signals"] == ["requires_operator_input"]


def test_partial_not_satisfied_unless_schema_allows():
    f = _fold("partial")
    assert f["meta_b"]["outcome_kind"] == "skill_partial"
    assert f["meta_b"]["satisfied_dispatch"] is False
    assert "partial_completion" in f["global_b"]["continuation_signals"]
    f2 = _fold("partial", expected_partial_ok=True)
    assert f2["meta_b"]["satisfied_dispatch"] is True


def test_yielded_produces_skill_yielded_not_a_failure():
    # a budget/watchdog checkpoint — progress intact, NOT a failure
    f = _fold("yielded", progress_digest="built core/types, memory, planner", rounds_used=12)
    assert f["meta_b"]["outcome_kind"] == "skill_yielded"
    assert f["meta_b"]["satisfied_dispatch"] is False
    assert f["meta_b"]["failure_class"] is None


def test_yielded_keeps_episode_open_for_the_mind_to_decide():
    gb = _fold("yielded", rounds_used=12)["global_b"]
    assert gb["intent_satisfied"] is False
    assert gb["objective_complete"] is False
    assert gb["more_work_needed"] is True
    # the fold does NOT close or auto-continue — Omega's episode owns continue|finish
    assert gb["close_episode"] is False
    assert gb["ask_operator"] is False
    assert "build_in_progress" in gb["continuation_signals"]


def test_yielded_pressure_is_recorded_documentary_not_executed():
    cp = _fold("yielded", rounds_used=12)["global_b"]["continuation_pressure"]
    assert cp is not None
    assert cp["source"] == "global_b_skill_foldback"  # recorded; Omega reasons over it


def test_invalid_and_malformed_and_bridge_kinds():
    assert _fold("invalid_dispatch")["meta_b"]["outcome_kind"] == "skill_invalid_dispatch"
    assert "skill_gap" in _fold("invalid_dispatch")["global_b"]["continuation_signals"]
    mf = _fold("malformed_result")
    assert mf["meta_b"]["outcome_kind"] == "skill_malformed_result"
    assert "repair_needed" in mf["global_b"]["continuation_signals"]
    assert _fold("bridge_unavailable")["meta_b"]["outcome_kind"] == "skill_bridge_unavailable"


def test_unknown_status_fails_closed():
    f = _fold("weird_unrecognized_status")
    assert f["meta_b"]["outcome_kind"] == "skill_malformed_result"


# ── Global B signals derived from failure_class ───────────────────────────────
def test_test_failure_emits_test_failed_signal():
    f = _fold("failure", failure_class="pytest test_parser failed")
    assert "test_failed" in f["global_b"]["continuation_signals"]


def test_forbidden_tool_emits_skill_gap_signal():
    f = _fold("failure", failure_class="forbidden_tool_use:run_command")
    assert "skill_gap" in f["global_b"]["continuation_signals"]


def test_all_continuation_signals_are_known():
    for status in (
        "success",
        "partial",
        "yielded",
        "failure",
        "blocked",
        "timeout",
        "requires_confirmation",
        "invalid_dispatch",
        "malformed_result",
        "bridge_unavailable",
    ):
        sigs = set(_fold(status)["global_b"]["continuation_signals"])
        assert sigs <= CONTINUATION_SIGNALS


# ── Global B records intent_satisfied + can create pressure, does not execute ──
def test_global_b_records_intent_satisfied():
    # a terminal verification success satisfies the objective; a failure does not
    assert _fold("success", execution_kind="TEST")["global_b"]["intent_satisfied"] is True
    assert _fold("failure", failure_class="x")["global_b"]["intent_satisfied"] is False


def test_global_b_creates_continuation_pressure_on_failure_not_terminal_success():
    assert _fold("failure", failure_class="x")["global_b"]["continuation_pressure"] is not None
    # a terminal (TEST) success closes with no further pressure
    assert _fold("success", execution_kind="TEST")["global_b"]["continuation_pressure"] is None


def test_global_b_pressure_is_recorded_intent_not_executed():
    # the pressure is plain data Global B records — no enqueue/side effect
    cp = _fold("partial")["global_b"]["continuation_pressure"]
    assert cp["source"] == "global_b_skill_foldback" and cp["kind"] == "execution_repair"


# ── exactly one outcome / no duplicate / no silent drop ───────────────────────
def test_ledger_enforces_one_outcome_per_dispatch():
    ledger = MetaBSkillLedger()
    fold_skill_result(_result("success"), objective="o", ledger=ledger)
    with pytest.raises(SkillFoldbackError, match="duplicate"):
        fold_skill_result(_result("success"), objective="o", ledger=ledger)


def test_ledger_missing_outcome_fails():
    ledger = MetaBSkillLedger()
    with pytest.raises(SkillFoldbackError, match="missing"):
        ledger.require("dispatch_never_recorded")


def test_no_silent_drop_every_status_produces_both():
    for status in (
        "success",
        "partial",
        "yielded",
        "failure",
        "blocked",
        "timeout",
        "requires_confirmation",
        "invalid_dispatch",
        "malformed_result",
        "bridge_unavailable",
        "garbage",
    ):
        f = _fold(status)
        assert f["meta_b"] is not None and f["global_b"] is not None
        assert f["meta_b"]["outcome_kind"] in ALLOWED_META_B_SKILL_KINDS


# ── contracts validate ────────────────────────────────────────────────────────
def test_contracts_validate_clean():
    mbo = classify_meta_b_skill_outcome(_result("success"))
    assert mbo.validate() == []
    gbu = integrate_global_b_skill_outcome(mbo, objective="o", result=_result("success"))
    assert gbu.validate() == []


# ── episode attach ────────────────────────────────────────────────────────────
class _Ep:
    def __init__(self):
        self.meta = {}
        self.global_ = {}


def test_attach_writes_alongside_live_fields():
    ep = _Ep()
    ep.meta["reflection"] = {"live": "untouched"}  # the live USE_TOOL reflection
    ep.global_["outcome"] = {"live": "untouched"}
    attach_skill_foldback(ep, _fold("success", execution_kind="TEST"))
    assert ep.meta["reflection"] == {"live": "untouched"}  # live path preserved
    assert ep.meta["skill_reflection"]["outcome_kind"] == "skill_success"
    assert ep.global_["skill_outcome"]["intent_satisfied"] is True  # terminal verification
    assert ep.global_["outcome"] == {"live": "untouched"}
