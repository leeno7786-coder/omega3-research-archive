"""The SRAM episode bucket — fresh memory held IN the episode, consolidated at close.

Perception before consolidation, done structurally (operator 2026-07-16): the operator's
stated facts and conversation turns for a turn are HELD in a per-episode bucket during the
episode and only written to system memory at DeepSleep/close — never before that episode's
recall. So recall for turn N can never surface turn N's own intake ("Omega remembers
something before it's told"). This tests the bucket mechanics + the default consolidator.
"""

from __future__ import annotations

from types import SimpleNamespace

import memory.episode_sram as sram
import pytest


def setup_function(_):
    sram.reset()


def teardown_function(_):
    sram.reset()


def test_open_starts_empty():
    sram.open_bucket("ep-1", pipeline_id=0)
    assert sram.is_open("ep-1")
    assert sram.current_episode_id() == "ep-1"
    assert sram.pending() == []


def test_hold_perception_and_turn_accumulate():
    sram.open_bucket("ep-1", pipeline_id=0)
    citation = {"source_id": "src-chat-1", "span_hash": "span-chat-1"}
    sram.hold_perception(
        "I have a sister named Miranda.",
        speaker="operator",
        ingress_class="phatic",
        chat_intent="freeform",
        source_citation=citation,
    )
    sram.hold_turn("user", "I have a sister named Miranda.")
    sram.hold_turn("assistant", "Noted — Miranda, your sister.")
    items = sram.pending()
    assert [it.kind for it in items] == ["perception", "turn", "turn"]
    assert items[0].text == "I have a sister named Miranda."
    assert items[0].speaker == "operator"
    assert items[0].ingress_class == "phatic"
    assert items[0].chat_intent == "freeform"
    assert items[0].source_citation == citation
    assert items[2].role == "assistant"


def test_remember_action_marks_the_held_perception_after_admitting_records(monkeypatch):
    sram.open_bucket("ep-remember", pipeline_id=0)
    citation = {"source_id": "src-chat-remember"}
    sram.hold_perception(
        "Remember that my university is UIC.",
        source_citation=citation,
    )
    calls = []

    def fake_ingest(text, *, speaker, source_citation):
        calls.append((text, speaker, source_citation))
        return SimpleNamespace(
            records=[SimpleNamespace(kind="operator_attribute", facet="university", value="UIC")]
        )

    monkeypatch.setattr("memory.persona_ingest.ingest_persona_statement", fake_ingest)

    assert sram.prepare_persona_intake_for_action(
        "Remember that my university is UIC."
    ) == ["university=UIC"]
    assert calls == [("Remember that my university is UIC.", "operator", citation)]
    item = sram.pending()[0]
    assert item.kind == "perception_processed"
    assert item.write_options["record_count"] == 1


def test_verified_recall_for_wrong_episode_is_failure_visible(caplog):
    """Scheduling maintenance against an episode with no bucket drops it — the
    warning must name BOTH the requested episode and the buckets that are
    actually open, or a lane pointing at the wrong episode is indistinguishable
    from an episode that simply closed. This is the per-lane form of the
    ``open=`` field the single-``_current`` implementation logged."""
    sram.open_bucket("ep-owner", pipeline_id=0)

    with caplog.at_level("WARNING", logger="omega.memory.episode_sram"):
        scheduled = sram.hold_verified_recall_for_episode("ep-other", ["memory-1"])

    assert scheduled == 0
    assert "ep-other" in caplog.text
    assert "ep-owner" in caplog.text


def test_verified_recall_with_no_open_bucket_says_so_explicitly(caplog):
    with caplog.at_level("WARNING", logger="omega.memory.episode_sram"):
        scheduled = sram.hold_verified_recall_for_episode("ep-gone", ["memory-1"])

    assert scheduled == 0
    assert "ep-gone" in caplog.text
    assert "open=none" in caplog.text


def test_flush_consolidates_held_items_and_drains():
    sram.open_bucket("ep-1", pipeline_id=0)
    sram.hold_perception("fact one")
    sram.hold_turn("user", "fact one")
    seen = {}

    def _consolidate(items):
        seen["items"] = list(items)
        return {"ingested": ["x"], "turns": ["y"], "errors": []}

    result = sram.flush("ep-1", consolidate=_consolidate)
    assert [it.kind for it in seen["items"]] == ["perception", "turn"]
    assert result["ingested"] == ["x"]
    # drained: a second flush sees nothing and does not re-consolidate.
    again = {"called": False}

    def _c2(items):
        again["called"] = True
        return {}

    sram.flush("ep-1", consolidate=_c2)
    assert again["called"] is False
    assert sram.pending() == []


def test_failed_flush_retains_items_for_exactly_one_retry():
    """A consolidator exception must not remove intake from the episode bucket."""
    sram.open_bucket("ep-retry", pipeline_id=0)
    sram.hold_perception("I rely on durable retries.")
    sram.hold_turn("user", "I rely on durable retries.")

    def _fails(_items):
        raise RuntimeError("filesystem unavailable")

    with pytest.raises(RuntimeError, match="filesystem unavailable"):
        sram.flush("ep-retry", consolidate=_fails)

    assert [item.kind for item in sram.pending()] == ["perception", "turn"]

    calls = []

    def _succeeds(items):
        calls.append([item.kind for item in items])
        return {"ingested": ["memory-1"], "turns": ["turn-1"]}

    result = sram.flush("ep-retry", consolidate=_succeeds)
    assert calls == [["perception", "turn"]]
    assert result["ingested"] == ["memory-1"]
    assert sram.pending() == []


def test_flush_only_matches_the_named_episode():
    sram.open_bucket("ep-1", pipeline_id=0)
    sram.hold_perception("held for ep-1")
    called = {"n": 0}

    def _c(items):
        called["n"] += 1
        return {}

    # a flush naming a DIFFERENT episode must not drain ep-1's bucket
    sram.flush("ep-OTHER", consolidate=_c)
    assert called["n"] == 0
    assert len(sram.pending()) == 1
    # the matching flush drains it
    sram.flush("ep-1", consolidate=_c)
    assert called["n"] == 1


def test_close_safety_flushes_then_clears():
    sram.open_bucket("ep-1", pipeline_id=0)
    sram.hold_perception("survives via close even when deepsleep never flushed")
    drained = {}

    def _c(items):
        drained["items"] = list(items)
        return {}

    sram.close_bucket("ep-1", consolidate=_c)
    assert len(drained["items"]) == 1  # close is the durability safety net
    assert not sram.is_open()
    assert sram.current_episode_id() is None


def test_open_refuses_to_flush_a_stranded_bucket_outside_deepsleep():
    sram.open_bucket("ep-1", pipeline_id=0)
    sram.hold_perception("stranded from ep-1")

    with pytest.raises(RuntimeError, match="ep-1"):
        sram.open_bucket("ep-2", pipeline_id=0)

    assert sram.current_episode_id() == "ep-1"
    assert [item.text for item in sram.pending()] == ["stranded from ep-1"]


def test_default_consolidator_runs_persona_ingest_and_hot_buffer(monkeypatch):
    ingested: list[str] = []
    turns: list[tuple[str, str]] = []

    import memory.persona_ingest as pi
    import memory.hot_buffer as hb

    monkeypatch.setattr(
        pi,
        "ingest_persona_statement",
        lambda text, speaker="operator", source_citation=None: (
            ingested.append(text) or SimpleNamespace(records=[SimpleNamespace(id="memory-1")])
        ),
    )
    monkeypatch.setattr(hb, "append_turn", lambda role, text, **k: turns.append((role, text)))

    sram.open_bucket("ep-1", pipeline_id=0)
    sram.hold_perception("I study cybersecurity.", speaker="operator")
    sram.hold_turn("user", "I study cybersecurity.")
    sram.hold_turn("assistant", "Cybersecurity — noted.")
    result = sram.flush("ep-1")  # default consolidator

    assert ingested == ["I study cybersecurity."]
    assert turns == [("user", "I study cybersecurity."), ("assistant", "Cybersecurity — noted.")]
    assert result["enabled"] is True


def test_default_consolidator_admits_explicit_third_person_memory_command(monkeypatch):
    """An explicit remember directive is factual intake, even when the facts are
    about a named third person instead of beginning with I/my/we/our.
    """
    import memory.persona_ingest as pi

    remembered: list[tuple[str, dict]] = []

    def _ingest(text, *, speaker="operator", source_citation=None):
        remembered.append((text, dict(source_citation or {})))
        return SimpleNamespace(records=[SimpleNamespace(id="memory-mitchell")])

    monkeypatch.setattr(pi, "ingest_persona_statement", _ingest)
    citation = {"source_id": "src-chat-mitchell", "span_hash": "span-mitchell"}
    text = "Remember Mitchell Ray. He is Noahs best friend and business partner. He is also married"

    sram.open_bucket("ep-remember-mitchell", pipeline_id=0)
    sram.hold_perception(
        text,
        ingress_class="operator_command",
        chat_intent="command",
        source_citation=citation,
    )
    result = sram.flush("ep-remember-mitchell")

    assert remembered == [(text, citation)]
    assert result["ingested"] == [text]
    assert result["skipped"] == []


def test_default_consolidator_only_promotes_assertions_and_reports_real_commits(monkeypatch):
    import memory.persona_ingest as pi

    calls: list[tuple[str, dict]] = []

    def _ingest(text, *, speaker="operator", source_citation=None):
        calls.append((text, dict(source_citation or {})))
        memory_id = None if text == "I prefer teal." else f"mem-{len(calls)}"
        return SimpleNamespace(records=[SimpleNamespace(id=memory_id)])

    monkeypatch.setattr(pi, "ingest_persona_statement", _ingest)

    citation = {"source_id": "src-chat-assert", "span_hash": "span-assert"}
    sram.open_bucket("ep-assertions", pipeline_id=0)
    sram.hold_perception(
        "I study cybersecurity.",
        ingress_class="phatic",
        chat_intent="freeform",
        source_citation=citation,
    )
    sram.hold_perception(
        "What do I study?",
        ingress_class="epistemic_request",
        chat_intent="question",
        source_citation=citation,
    )
    sram.hold_perception(
        "Build a profile page",
        ingress_class="operator_command",
        source_citation=citation,
    )
    sram.hold_perception(
        "Work through why this failed",
        ingress_class="epistemic_request",
        chat_intent="reasoning",
        source_citation=citation,
    )
    sram.hold_perception(
        "I do not have a sister named Miranda.",
        ingress_class="phatic",
        chat_intent="freeform",
        source_citation=citation,
    )
    sram.hold_perception(
        "If I lived in Paris, I would bike.",
        ingress_class="phatic",
        chat_intent="freeform",
        source_citation=citation,
    )
    sram.hold_perception(
        "I prefer teal.",
        ingress_class="phatic",
        chat_intent="freeform",
        source_citation=citation,
    )

    result = sram.flush("ep-assertions")

    assert calls == [
        ("I study cybersecurity.", citation),
        ("I prefer teal.", citation),
    ]
    assert result["ingested"] == ["I study cybersecurity."]
    assert any(row["text"] == "I prefer teal." for row in result["skipped"])


def test_default_consolidator_failure_retains_the_whole_batch(monkeypatch):
    import memory.persona_ingest as pi
    import memory.hot_buffer as hb

    def _boom(*_a, **_k):
        raise RuntimeError("ingest exploded")

    turns: list[tuple[str, str]] = []
    monkeypatch.setattr(pi, "ingest_persona_statement", _boom)
    monkeypatch.setattr(hb, "append_turn", lambda role, text, **k: turns.append((role, text)))

    sram.open_bucket("ep-1", pipeline_id=0)
    sram.hold_perception("I study something that makes ingest raise.")
    sram.hold_turn("user", "but this turn still lands")
    with pytest.raises(RuntimeError, match="ingest exploded"):
        sram.flush("ep-1")

    assert turns == []
    assert [item.kind for item in sram.pending()] == ["perception", "turn"]


def test_reply_shape_label_cannot_discard_a_self_assertion(monkeypatch):
    import memory.persona_ingest as pi

    ingested: list[str] = []
    monkeypatch.setattr(
        pi,
        "ingest_persona_statement",
        lambda text, **_kwargs: (
            ingested.append(text) or SimpleNamespace(records=[SimpleNamespace(id="memory-intro")])
        ),
    )
    introduction = (
        "I am Lee, I live in Chicago, I build portable cognitive systems, "
        "and I prefer direct answers grounded in verified evidence every time."
    )
    sram.open_bucket("ep-labelled-ack", pipeline_id=0)
    sram.hold_perception(
        introduction,
        ingress_class="phatic",
        chat_intent="ack",
    )

    result = sram.flush("ep-labelled-ack")

    assert ingested == [introduction]
    assert result["ingested"] == [introduction]
    assert result["skipped"] == []


def test_hold_without_open_fails_instead_of_creating_detached_state():
    with pytest.raises(RuntimeError, match="no episode SRAM bucket"):
        sram.hold_perception("must enter through pressure first")
    assert sram.pending() == []


def test_graduate_turns_now_moves_the_exchange_to_the_hot_buffer_in_order():
    """Live 2026-09-06: turns graduated only at DeepSleep, so a fast follow-up
    ("what did I ask you about earlier?") saw a conversation block that ended one
    exchange too early. At delivery the exchange graduates immediately."""
    from memory import hot_buffer

    hot_buffer._reset_for_tests()
    sram.open_bucket("ep-g", pipeline_id=0)
    sram.hold_turn("user", "What is my dog's name?")
    sram.hold_turn("assistant", "Biscuit")
    assert sram.graduate_turns_now("ep-g") == 2
    block = hot_buffer.render_recent_block()
    assert "[operator]" in block and "[omega] Biscuit" in block
    assert block.index("dog") < block.index("Biscuit")
    assert [(it.kind, it.graduated) for it in sram.pending()] == [("turn", True), ("turn", True)]
    hot_buffer._reset_for_tests()
    sram.flush("ep-g", consolidate=lambda items: {"turns": [i.text for i in items if i.kind == "turn"]})
    assert hot_buffer.render_recent_block() == ""  # flush does not re-append a graduated turn
    assert sram.graduate_turns_now("ep-g") == 0
    assert sram.graduate_turns_now("no-such-episode") == 0
    hot_buffer._reset_for_tests()
