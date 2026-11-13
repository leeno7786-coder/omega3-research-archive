"""Medium #19 — a qwen 'success' was accepted on its self-reported status alone.

The skill dispatch declares evidence_required (and completion_criteria / output_schema) and pushes
them to qwen, but the foldback never checked the RESULT against them — so qwen could report
status='success' with no evidence and be folded as a satisfied objective (honest-signals slip).
A success is now SATISFIED only if the dispatch's required evidence is actually present; otherwise
it carries an evidence_unsatisfied blocker and Global B owes verification.
"""

from __future__ import annotations

from meta_loop.skill.foldback import classify_meta_b_skill_outcome, fold_skill_result


def _success(evidence=None, **kw):
    r = {
        "status": "success",
        "episode_id": "ep",
        "dispatch_id": "d1",
        "skill_name": "s",
        "skill_id": "5.01.qwenskill-s",
        "execution_kind": "REPAIR",
        "summary": "done",
        "evidence": evidence or {},
    }
    r.update(kw)
    return r


def test_success_without_required_evidence_records_blocker():
    mbo = classify_meta_b_skill_outcome(
        _success(evidence={}), evidence_required=("summary", "tests_passed")
    )
    assert mbo.outcome_kind == "skill_success"  # qwen DID execute
    # ...but the required evidence is absent, so a blocker is recorded (Global B owes verification)
    assert any("evidence_unsatisfied" in b for b in mbo.unresolved_blockers)


def test_success_with_required_evidence_has_no_blocker():
    mbo = classify_meta_b_skill_outcome(
        _success(evidence={"summary": "patched x", "tests_passed": "3/3"}),
        evidence_required=("summary", "tests_passed"),
    )
    assert mbo.outcome_kind == "skill_success"
    assert not any("evidence_unsatisfied" in b for b in mbo.unresolved_blockers)


def test_no_required_evidence_keeps_success():
    # backward-compatible: no requirements declared -> status drives satisfaction
    mbo = classify_meta_b_skill_outcome(_success(evidence={}))
    assert mbo.satisfied_dispatch is True


def test_global_b_owes_verification_when_evidence_unsatisfied():
    folded = fold_skill_result(
        _success(evidence={}), objective="fix the bug", evidence_required=("summary",)
    )
    gb = folded["global_b"]
    assert gb["intent_satisfied"] is False
    assert "verification_needed" in gb["continuation_signals"]


def _normalized_success(*, tool_calls=(), artifacts=(), **claims):
    return _success(
        type="qwen_skill_execution_result",
        schema_version="omega.qwen_skill_execution_result.v1",
        tool_calls=list(tool_calls),
        artifacts=list(artifacts),
        **claims,
    )


def test_normalized_research_summary_and_findings_require_real_read_fact():
    result = _normalized_success(
        tool_calls=(
            {
                "tool_call_id": "read-1",
                "tool_name": "read_files",
                "args": {"path": "design.md"},
                "status": "success",
                "stdout": "",
                "result": {"content": "three evidence-backed findings"},
            },
        ),
    )

    mbo = classify_meta_b_skill_outcome(
        result,
        evidence_required=("findings", "summary"),
    )

    assert not any("evidence_unsatisfied" in row for row in mbo.unresolved_blockers)


def test_live_read_result_with_unknown_status_and_payload_is_still_a_fact():
    result = _normalized_success(
        tool_calls=(
            {
                "tool_call_id": "read-live-1",
                "tool_name": "read_files",
                "args": {"path": "design.md"},
                "status": "unknown",
                "stdout": "primary-source content",
                "stderr": "",
                "result": {"value": "primary-source content"},
            },
        ),
    )

    mbo = classify_meta_b_skill_outcome(
        result,
        evidence_required=("findings", "summary"),
    )

    assert not any("evidence_unsatisfied" in row for row in mbo.unresolved_blockers)


def test_normalized_plan_requires_digest_bearing_plan_artifact():
    result = _normalized_success(
        artifacts=(
            {
                "path": "plans/approach.md",
                "kind": "plan",
                "sha256": "a" * 64,
            },
        ),
    )

    mbo = classify_meta_b_skill_outcome(result, evidence_required=("plan", "summary"))

    assert not any("evidence_unsatisfied" in row for row in mbo.unresolved_blockers)


def test_normalized_raw_prose_and_self_attested_aliases_never_satisfy_contract():
    result = _normalized_success(
        summary="claimed summary",
        evidence={"summary": True, "findings": ["claimed"], "plan": "claimed"},
    )

    mbo = classify_meta_b_skill_outcome(
        result,
        evidence_required=("summary", "findings", "plan"),
    )

    assert "evidence_unsatisfied:summary,findings,plan" in mbo.unresolved_blockers


def test_canonical_motor_evidence_names_map_only_to_successful_normalized_facts():
    result = _normalized_success(
        files_changed=["src/model.py"],
        tool_calls=(
            {
                "tool_call_id": "pytest-green",
                "tool_name": "execute_command",
                "args": {"command": "python -m pytest -q"},
                "status": "success",
                "stdout": "1 passed",
                "result": {"exit_code": 0},
            },
        ),
    )

    mbo = classify_meta_b_skill_outcome(
        result,
        evidence_required=("files changed", "pytest"),
    )

    assert not any("evidence_unsatisfied" in row for row in mbo.unresolved_blockers)


def test_canonical_pytest_requirement_rejects_red_tool_fact():
    result = _normalized_success(
        files_changed=["src/model.py"],
        tool_calls=(
            {
                "tool_call_id": "pytest-red",
                "tool_name": "execute_command",
                "args": {"command": "pytest -q"},
                "status": "failure",
                "stdout": "1 failed",
                "result": {"exit_code": 1},
            },
        ),
    )

    mbo = classify_meta_b_skill_outcome(result, evidence_required=("pytest",))

    assert "evidence_unsatisfied:pytest" in mbo.unresolved_blockers


def test_free_form_motor_requirements_use_typed_factual_lanes():
    provenance = {
        "source": "qwen_tool_call",
        "tool_call_id": "check-1",
        "result_digest": "b" * 24,
    }
    result = _normalized_success(
        artifacts=(
            {"path": "plan.md", "kind": "plan", "sha256": "a" * 64},
            {
                "path": "gradient.py",
                "kind": "derivation_implementation",
                "sha256": "c" * 64,
            },
        ),
        sources_consulted=["src-primary-1"],
        verification_results=[
            {
                "requested": "gradient check",
                "status": "passed",
                "executor_provenance": provenance,
            },
            {
                "requested": "chain rule steps present",
                "status": "passed",
                "executor_provenance": provenance,
            },
        ],
    )

    mbo = classify_meta_b_skill_outcome(
        result,
        evidence_required=(
            "plan",
            "derivation",
            "implementation",
            "source_id",
            "gradient check",
            "chain rule steps present",
        ),
    )

    assert not any("evidence_unsatisfied" in row for row in mbo.unresolved_blockers)


def test_free_form_motor_requirements_reject_raw_and_self_attested_claims():
    result = _normalized_success(
        summary="plan, derivation, implementation, source and checks all complete",
        evidence={
            "plan": True,
            "derivation": True,
            "implementation": True,
            "source_id": "claimed-source",
            "gradient check": "passed",
            "chain rule steps present": True,
        },
        artifacts=[{"kind": "derivation_implementation"}],
        sources_consulted=[],
        verification_results=[{"requested": "gradient check", "status": "passed"}],
    )

    mbo = classify_meta_b_skill_outcome(
        result,
        evidence_required=(
            "plan",
            "derivation",
            "implementation",
            "source_id",
            "gradient check",
            "chain rule steps present",
        ),
    )

    assert (
        "evidence_unsatisfied:plan,derivation,implementation,source_id,gradient check,chain rule steps present"
        in mbo.unresolved_blockers
    )
