"""RED-first: a LEARN topic that arrives as a slug ('data-pipelines') must be
de-slugged into a real search phrase ('data pipelines') before it becomes the
web_fetch query. The live incident fetched PHYSICAL pipeline content for the slug
'data-pipelines' and distilled it into the data-pipelines mastery domain. The
routing 'domain' key keeps the slug; only the human-facing search query is
de-slugged.
"""

from __future__ import annotations

from meta_loop.tool.run import _search_phrase


def test_deslugs_hyphenated_topic():
    assert _search_phrase("data-pipelines") == "data pipelines"
    assert _search_phrase("real-time-systems") == "real time systems"
    assert _search_phrase("model_evaluation") == "model evaluation"


def test_human_phrase_unchanged():
    assert _search_phrase("Data Pipelines") == "Data Pipelines"
    assert _search_phrase("reinforcement learning") == "reinforcement learning"
    assert _search_phrase("React") == "React"


def test_empty_is_safe():
    assert _search_phrase("") == ""
    assert _search_phrase("   ") == ""


def test_learn_execution_query_is_deslugged(monkeypatch):
    """Integration: a LEARN whose resolved topic is a slug puts the DE-SLUGGED
    phrase in execution_inputs['query'] while 'domain' keeps the slug key."""
    from meta_loop._meta_a_state import MetaAState
    from cognition.topic_resolution.resolve_topic import ResolvedTopic
    import cognition.topic_resolution.resolve_topic as rt
    from meta_loop.tool.run import run as meta_a_run

    resolved = ResolvedTopic(
        topic="data-pipelines", domain="data-pipelines", source="mastery_gap", confidence=0.8
    )
    monkeypatch.setattr(rt, "resolve_learning_topic", lambda _state: resolved)

    state = MetaAState(
        mi=None,
        episode_id="ep",
        intent="LEARN",
        action="LEARN",
        source={},
        text="",
        target_str="",
        constraints={},
        meta={},
    )
    out = meta_a_run(state)

    assert out.execution_inputs["query"] == "data pipelines"
    assert out.execution_inputs["domain"] == "data-pipelines"  # routing key unchanged
    assert out.execution_inputs["web_research"] is True
    assert out.execution_inputs["web_catalog"]
    assert "source" not in out.execution_inputs


def test_learn_execution_query_excludes_operator_rationale():
    """A learning rationale must not become part of the web search subject."""
    from meta_loop._meta_a_state import MetaAState
    from meta_loop.tool.run import run as meta_a_run

    message = "Learn Python, it is the universal coding language."
    state = MetaAState(
        mi=None,
        episode_id="ep-operator-learn",
        intent="LEARN",
        action="LEARN",
        source={},
        text=message,
        target_str=message,
        constraints={},
        meta={"directive_intent": "learn"},
    )

    out = meta_a_run(state)

    assert out.execution_inputs["query"] == "Python"
    assert out.execution_inputs["domain"] == "python"
    assert out.execution_inputs["web_research"] is True
    assert any(
        "docs.python.org" in str(row.get("url") or "")
        for row in out.execution_inputs["web_catalog"]
    )
    assert "source" not in out.execution_inputs
