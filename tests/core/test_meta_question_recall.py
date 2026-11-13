"""Per-intent recall grounding — one owner lobe per ordinary question.

The deterministic MRIL router selects the memory lobe that owns the answer.  The
answer composer receives the verified candidates from that lane instead of an
eleven-lobe fan-out padded with unrelated identity, code, and telemetry rows.
Explicit temporal/freeform contracts retain their deliberately broader bundles.
"""

from __future__ import annotations

from meta_loop._meta_a_state import MetaAState
from meta_loop.memory.run import run


def _state(intent: str, text: str, meta: dict) -> MetaAState:
    return MetaAState(
        mi=None,
        episode_id="ep-question",
        intent=intent,
        action="RESPONSE",
        source={},
        text=text,
        target_str=text,
        constraints={},
        meta=dict(meta),
    )


def test_question_routes_to_single_identity_lane():
    out = run(_state("RESPOND", "who is your creator?", {"chat_intent": "question"}))
    assert out.memory_required is True
    tools = [t.get("tool") for t in out.memory_tools if isinstance(t, dict)]
    assert tools == ["remember_identity"]


def test_live_epistemic_request_shape_still_recalls_operator_persona():
    """The ingress classifier and Meta-A memory contract use different vocabularies.

    Live onboarding regression (2026-07-28): the HUD classified this exact turn as
    ``epistemic_request``.  Meta-A treated that non-empty value as authoritative but
    did not recognize it as ``question``, so the deterministic question-shaped floor
    was skipped and the Noah persona lane never ran.
    """

    text = "Omega, what do you remember about Noah?"
    out = run(
        _state(
            "RESPOND",
            text,
            {
                "chat_intent": "epistemic_request",
                "ingress_class": "epistemic_request",
            },
        )
    )

    assert out.memory_required is True
    by_tool = {
        tool["tool"]: tool
        for tool in out.memory_tools
        if isinstance(tool, dict) and tool.get("tool")
    }
    assert set(by_tool) == {"remember_persona"}
    assert text in by_tool["remember_persona"]["query"]


def test_named_people_organization_role_question_routes_only_to_persona():
    """A precise person-role question must not fan out across every memory lobe.

    The persona lobe owns named-person and organization-role facts.  Sending the
    same query through knowledge, code, vision, workflow, and the remaining
    graph lanes adds unrelated candidates and repeats the full recall pipeline.
    """

    text = "What do Mitchell and Noah do at Omega AI?"
    out = run(_state("RESPOND", text, {"chat_intent": "question"}))

    assert out.memory_required is True
    assert [tool["tool"] for tool in out.memory_tools] == ["remember_persona"]
    assert out.memory_tools[0]["query"] == text
    assert "subdomain" not in out.memory_tools[0]


def test_question_recall_query_is_the_question_text():
    out = run(_state("RESPOND", "what is the capital of France?", {"chat_intent": "question"}))
    assert out.memory_required is True
    assert "capital of France" in (out.memory_query or "")


def test_question_identity_recall_spans_all_subdirectories():
    # The creator seed lives in lobe-0 subdomain 0 (constitution). "who is your creator?" must NOT
    # narrow identity recall to subdomain 6 (the "your" second-person rule) — that excluded it. The
    # identity tool stays whole-lobe so the creator surfaces; knowledge may still narrow by query.
    out = run(_state("RESPOND", "who is your creator?", {"chat_intent": "question"}))
    id_tool = [t for t in out.memory_tools if t.get("tool") == "remember_identity"][0]
    assert "subdomain" not in id_tool, (
        f"identity recall narrowed to subdomain {id_tool.get('subdomain')}"
    )


def test_freeform_still_recalls_identity_and_persona():
    # STEP 1 expanded: freeform recalls the full Omega grounding bundle.
    out = run(_state("RESPOND", "hey there", {"chat_intent": "freeform"}))
    tools = [t.get("tool") for t in out.memory_tools if isinstance(t, dict)]
    for name in (
        "remember_identity",
        "remember_persona",
        "remember_knowledge",
        "remember_code",
        "remember_time",
    ):
        assert name in tools
    assert "remember_situation" not in tools
    assert "remember_journal" not in tools


def test_question_routes_to_one_owner_lobe():
    out = run(
        _state(
            "RESPOND", "how does the recall pipeline rank candidates?", {"chat_intent": "question"}
        )
    )
    assert out.memory_required is True
    tools = [t.get("tool") for t in out.memory_tools if isinstance(t, dict)]
    assert tools == ["remember_workflow"]


def test_question_self_lanes_are_whole_lobe_exact():
    # Creator identity is whole-lobe so the constitution seed cannot be narrowed away.
    out = run(_state("RESPOND", "who is your creator?", {"chat_intent": "question"}))
    by_tool = {t["tool"]: t for t in out.memory_tools if isinstance(t, dict)}
    assert set(by_tool) == {"remember_identity"}
    assert "subdomain" not in by_tool["remember_identity"]


def test_question_body_lanes_keyed_to_the_question_text():
    # The selected owner lane is keyed to the exact operator question.
    q = "what bug did we fix in the asyncio retry loop?"
    out = run(_state("RESPOND", q, {"chat_intent": "question"}))
    assert len(out.memory_tools) == 1
    assert out.memory_tools[0]["query"] == q


def test_temporal_chat_intent_grounds_dated_and_learned_recall():
    """Live 2026-07-19: 'what was the last thing you learned?' classified
    temporal — and the temporal label had NO branch here, so memory_required
    stayed False and the reply came from the base model with zero recall."""
    out = run(
        _state("RESPOND", "what was the last thing you learned?", {"chat_intent": "temporal"})
    )
    assert out.memory_required is True
    tools = [t for t in out.memory_tools if isinstance(t, dict)]
    names = [t.get("tool") for t in tools]
    assert "remember_time" in names
    assert "remember_knowledge" in names
    situation = [t for t in tools if t.get("tool") == "remember_situation"]
    assert len(situation) == 1
    assert tuple(situation[0].get("allowed_subdomains") or ()) == (1,)  # 1 = "what"


def test_temporal_recall_sweeps_the_coding_lobe_for_learned_material():
    # Learned comprehension routes coding-adjacent fetches to lobe 8 (live
    # 2026-07-19: kubernetes fetched_knowledge landed in coding) — "what did
    # you learn?" must reach it.
    out = run(_state("RESPOND", "what did you learn today?", {"chat_intent": "temporal"}))
    names = [t.get("tool") for t in out.memory_tools if isinstance(t, dict)]
    assert "remember_code" in names


def test_temporal_question_about_built_work_sweeps_the_dev_lobe():
    """Live 2026-09-06: "Do you remember what I built yesterday?" classified
    temporal, swept lobes 9/4/2/8 and never lobe 11 where the builder artifacts
    live, and the answer model read DeepSleep trait-promotion episodes as things
    the operator had built."""
    out = run(_state("RESPOND", "Do you remember what I built yesterday?", {"chat_intent": "temporal"}))
    tools = [t.get("tool") for t in out.memory_tools if isinstance(t, dict)]
    assert "remember_dev" in tools and tools[0] == "remember_time"
    assert "dev" in {str(s).lower() for s in out.memory_scope}
    assert out.memory_answer_required is True


def test_temporal_question_without_built_work_keeps_the_dev_lobe_out():
    out = run(_state("RESPOND", "What happened yesterday?", {"chat_intent": "temporal"}))
    tools = [t.get("tool") for t in out.memory_tools if isinstance(t, dict)]
    assert "remember_dev" not in tools
