"""Presented results post like system questions and fold only on the grade.

Operator directive 2026-07-12: a proactive result lands in the QUESTIONS PANEL
(system_messages), counts in the badge, and stays pending until the operator
responds. The response is the teacher's grade — only THAT triggers
fold_presented_result. The mind never grades its own homework at presentation.
"""

from types import SimpleNamespace

from pressure_loop.proactive_result_renderer import render_proactive_result
from schema.proactive_lifecycle import ApplicationRow, ProactiveResult


def _result(*, status="weak"):
    return ProactiveResult(
        result_id="result-1",
        thread_id="thread-1",
        episode_id="ep-apply",
        origin_prompt_id="prompt-1",
        policy_id="policy-1",
        application_id="app-1",
        title="Distribution scenario classification",
        summary="Completed the requested pass.",
        practiced=("Binomial vs Poisson",),
        learned=(),
        application=(
            ApplicationRow(
                prompt="Emails in an hour",
                answer="Binomial",
                expected="Poisson",
                correct=False,
                explanation="I missed the interval count.",
                evidence=("executor:row-1",),
            ),
        ),
        evidence_refs=("executor:ep-apply",),
        confidence=0.55,
        mastery_status=status,
        mastery_evidence=() if status != "passed" else ("1/1 verified",),
        mistakes=("Scenario 1",),
        weak_patterns=("fixed trials were not checked first",),
        next_step="Practice Binomial vs Poisson boundary cases.",
        operator_input_needed=False,
        status="ready",
    )


def _episode(result):
    return SimpleNamespace(
        episode_id=result.episode_id,
        opened_by="proactive_update",
        global_={
            "intent_decision": {"intent": "PROACTIVE_RESPONSE"},
            "omega_result": {"proactive_result": result.to_dict(), "mastery_effects": []},
            "omega_packet": {"prior_mastery": {"application_mastery": 0.45}},
            "outcome": {"artifacts": {"response_text": "model prose"}},
        },
        meta={"execution_action_pack": {"action": "PROACTIVE_RESPONSE"}},
        auto={"status": "completed", "outputs": {}},
        pressure={"contributors": []},
    )


def test_weak_renderer_exposes_diagnostic_state():
    text = render_proactive_result(_result())
    assert "Mastery status: Not yet passed" in text
    assert "Confidence:" in text
    assert "Weakness:" in text
    assert "Next corrective step:" in text
    assert "Emails in an hour" in text


def test_presentation_posts_to_questions_panel_and_never_auto_folds(monkeypatch):
    """The result is a pending panel record — no fold until the operator grades."""
    import cognition_chain as chain

    events = []
    result = _result()
    monkeypatch.setattr(
        "pressure_loop.system_messages.append_result_presentation",
        lambda **kw: events.append(("present", kw)) or {"id": "sysmsg-1"},
    )
    monkeypatch.setattr(
        "pressure_loop.proactive_lifecycle.mark_result_presented",
        lambda *_a, **_k: events.append(("marked", {})),
    )
    monkeypatch.setattr(
        "pressure_loop.proactive_lifecycle.fold_presented_result",
        lambda *_a, **_k: events.append(("folded", {})),
    )

    chain._record_proactive_chat(_episode(result))

    names = [name for name, _ in events]
    assert names == ["present", "marked"], "fold must wait for the operator's grade"
    present_kwargs = events[0][1]
    assert present_kwargs["result_id"] == "result-1"
    assert present_kwargs["thread_id"] == "thread-1"
    assert "Mastery status" in present_kwargs["text"]


def test_presentation_failure_does_not_mark_presented(monkeypatch):
    import cognition_chain as chain

    result = _result()
    monkeypatch.setattr(
        "pressure_loop.system_messages.append_result_presentation",
        lambda **_kw: None,
    )
    marked = []
    monkeypatch.setattr(
        "pressure_loop.proactive_lifecycle.mark_result_presented",
        lambda *_a, **_k: marked.append(True),
    )
    chain._record_proactive_chat(_episode(result))
    assert marked == []


def test_panel_record_persists_pending_and_dedups_by_result(tmp_path, monkeypatch):
    """The presentation behaves exactly like a question: pending in the panel,
    badge-counted, restart-idempotent by result_id."""
    import pressure_loop.system_messages as sm

    monkeypatch.setattr(sm, "_store_path", lambda: tmp_path / "system_messages.json")
    sm._reset_cache_for_tests()

    text = render_proactive_result(_result())
    first = sm.append_result_presentation(
        text=text,
        episode_id="ep-apply",
        opened_by="proactive_update",
        result_id="result-1",
        thread_id="thread-1",
        mastery_delta=0.05,
    )
    dup = sm.append_result_presentation(
        text=text,
        episode_id="ep-apply-2",
        opened_by="proactive_update",
        result_id="result-1",
        thread_id="thread-1",
    )

    assert first is not None and dup is not None
    assert dup["id"] == first["id"], "same result never presents twice"
    assert first["kind"] == "result_presentation"
    assert first["status"] == sm.STATUS_PENDING
    assert first["mastery_delta"] == 0.05
    assert sm.unread_count() == 1, "presentation counts in the questions badge"


def test_operator_grade_triggers_fold_with_stashed_delta(tmp_path, monkeypatch):
    """Responding to the presentation IS the fold trigger — panel path."""
    import pressure_loop.system_messages as sm

    monkeypatch.setattr(sm, "_store_path", lambda: tmp_path / "system_messages.json")
    sm._reset_cache_for_tests()
    monkeypatch.setattr(sm, "_attach_journal_answer", lambda *_a, **_k: None)
    enqueued = []
    monkeypatch.setattr(
        "pressure_loop.cognition_queue.enqueue_input",
        lambda contributor, **kwargs: enqueued.append(contributor),
    )
    folds = []
    monkeypatch.setattr(
        "pressure_loop.proactive_lifecycle.fold_presented_result",
        lambda result_id, *, mastery_delta=0.0: (
            folds.append((result_id, mastery_delta)) or SimpleNamespace(foldback_id="fb-1")
        ),
    )
    bound = []
    monkeypatch.setattr(
        "pressure_loop.proactive_lifecycle.bind_operator_policy",
        lambda **kw: bound.append(kw),
    )

    rec = sm.append_result_presentation(
        text="Omega result: pass",
        episode_id="ep-apply",
        opened_by="proactive_update",
        result_id="result-1",
        thread_id="thread-1",
        mastery_delta=0.07,
    )
    updated = sm.respond_to_message(rec["id"], "Good work — 9/10, revisit scenario 2.")

    assert updated is not None and updated["status"] == sm.STATUS_RESPONDED
    assert folds == [("result-1", 0.07)], "grade folds with the stashed delta"
    assert bound == [], "a grade is not a new policy binding"
    assert enqueued, "the grade reaches the mind as an operator contributor"


def test_chat_box_grade_also_folds(tmp_path, monkeypatch):
    import pressure_loop.system_messages as sm

    monkeypatch.setattr(sm, "_store_path", lambda: tmp_path / "system_messages.json")
    sm._reset_cache_for_tests()
    monkeypatch.setattr(sm, "_attach_journal_answer", lambda *_a, **_k: None)
    folds = []
    monkeypatch.setattr(
        "pressure_loop.proactive_lifecycle.fold_presented_result",
        lambda result_id, *, mastery_delta=0.0: (
            folds.append(result_id) or SimpleNamespace(foldback_id="fb-1")
        ),
    )

    rec = sm.append_result_presentation(
        text="Omega result: pass",
        episode_id="ep-apply",
        opened_by="proactive_update",
        result_id="result-9",
        thread_id="thread-9",
    )
    updated = sm.attach_chat_answer(rec["id"], "graded: solid", answer_episode_id="cyc-1")

    assert updated is not None and updated["status"] == sm.STATUS_RESPONDED
    assert folds == ["result-9"]
