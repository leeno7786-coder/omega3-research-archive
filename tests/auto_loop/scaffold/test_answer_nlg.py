"""The answer NLG: register from the classifier, modifiers from the sentence.

Replaces what the retired composer LoRA was inferring with a 2B model. These
pin the two properties that make that trade sound: the right prompt is selected
from signals the cascade already produced, and the same turn always renders the
same prompt.
"""

from __future__ import annotations

from auto_loop.scaffold.grammar import analyze
from organ.promptorgan.packs.chat.answer import (
    CONVERSATIONAL,
    QUESTION,
    REASONING,
    TEMPORAL,
    answer_system_prompt,
    build_answer_prompt,
    resolve_register,
)


# ── grammar ──────────────────────────────────────────────────────────────────


def test_question_shape_survives_a_missing_question_mark():
    assert analyze("what did we ship yesterday").is_question
    assert analyze("Is the drive mounted").is_question
    assert not analyze("the drive is mounted").is_question


def test_a_polite_question_is_not_read_as_a_command():
    # "tell" opens the imperative list, but the '?' says it is being asked.
    shape = analyze("tell me what you know about the T7?")
    assert shape.is_question
    assert not shape.is_imperative


def test_imperative_is_detected_without_a_question_mark():
    shape = analyze("list the models on the npu")
    assert shape.is_imperative
    assert not shape.is_question


def test_deixis_binding_arms_on_pronouns_that_could_bind_wrong():
    # THE composer's old job — "I" must not be read as Omega itself.
    assert analyze("What do Mitchell and I both do?").needs_deixis_binding
    assert analyze("is your project done").needs_deixis_binding
    assert analyze("and what about that?").needs_deixis_binding
    assert not analyze("list the mounted drives").needs_deixis_binding


def test_compound_and_comparative_and_enumerative_features():
    assert analyze("what broke and how do we fix it?").is_compound
    assert analyze("compare vulkan and hip decode").is_comparative
    assert analyze("list every model on the gpu").is_enumerative
    assert analyze("and what about the npu?").is_continuation


def test_analysis_is_pure():
    q = "What do Mitchell and I both do at Omega AI?"
    assert analyze(q) == analyze(q)


# ── register resolution ──────────────────────────────────────────────────────


def test_the_classifier_label_outranks_everything():
    # It saw the raw message at ingress; nothing downstream knows better.
    assert (
        resolve_register(
            chat_intent="reasoning",
            prompt_type="RESPONSE_QUESTION",
            shape=analyze("what is 2+2?"),
        )
        == REASONING
    )


def test_prompt_type_is_used_when_the_label_is_absent():
    assert resolve_register(prompt_type="RESPONSE_TEMPORAL") == TEMPORAL


def test_sentence_shape_is_the_floor_without_a_classifier_model():
    # The answer path must still register a question as a question using only
    # deterministic sentence shape.
    assert resolve_register(shape=analyze("who owns Omega AI?")) == QUESTION
    assert resolve_register(shape=analyze("the drive remounted fine")) == CONVERSATIONAL


def test_every_register_has_its_own_system_prompt():
    systems = {
        answer_system_prompt(r) for r in (QUESTION, REASONING, CONVERSATIONAL, TEMPORAL)
    }
    assert len(systems) == 4, "registers must not share a system prompt"
    for s in systems:
        # The two invariants every variant inherits from the retired composer.
        assert "verified data" in s or "MEMORY RECORDS" in s
        assert "model internals" in s


# ── assembly ─────────────────────────────────────────────────────────────────


_HITS = {"1.00.noah_owner": "Noah Lee is part owner of Omega AI LLC."}


def test_registers_produce_different_directives():
    prompts = {
        r: build_answer_prompt("tell me about the owners", _HITS, memory_required=True, register=r)
        for r in (QUESTION, REASONING, CONVERSATIONAL, TEMPORAL)
    }
    assert len({p.splitlines()[3] for p in prompts.values()}) == 4


def test_deixis_directive_reaches_the_prompt():
    prompt = build_answer_prompt(
        "What do Mitchell and I both do at Omega AI?",
        _HITS,
        memory_required=True,
        register=QUESTION,
    )
    assert "'I', 'me' and 'my' are the operator" in prompt
    assert prompt.rstrip().endswith("What do Mitchell and I both do at Omega AI?")


def test_multiturn_binds_anaphora_to_the_conversation_not_the_records():
    prompt = build_answer_prompt(
        "and what about that?", {}, memory_required=False,
        register=CONVERSATIONAL, multiturn=True,
    )
    assert "Resolve 'that' against the conversation above" in prompt
    assert "continues the conversation above" in prompt


def test_a_single_turn_gets_no_conversation_directives():
    # No prior turns -> the same sentence must NOT claim there is a conversation.
    prompt = build_answer_prompt(
        "and what about that?", {}, memory_required=False,
        register=CONVERSATIONAL, multiturn=False,
    )
    assert "conversation above" not in prompt


def test_memory_bound_refusal_outranks_register():
    # Answering around missing evidence is the one failure the operator cannot
    # audit from the reply, so it beats every register's directive.
    for register in (QUESTION, REASONING, CONVERSATIONAL, TEMPORAL):
        prompt = build_answer_prompt("where did I park?", {}, memory_required=True, register=register)
        assert "do not recall" in prompt
        assert "do not guess" in prompt


def test_temporal_labels_its_records_as_dated():
    prompt = build_answer_prompt("what did we do yesterday?", _HITS, memory_required=True, register=TEMPORAL)
    assert "DATED MEMORY RECORDS" in prompt


def test_records_always_ride_behind_the_ingress_fence():
    for register in (QUESTION, REASONING, CONVERSATIONAL, TEMPORAL):
        prompt = build_answer_prompt("who owns it?", _HITS, memory_required=True, register=register)
        assert '<external-data trust="untrusted-human-authored">' in prompt


def test_assembly_is_deterministic():
    args = ("What do Mitchell and I both do?", _HITS)
    kw = dict(memory_required=True, register=QUESTION, multiturn=True)
    assert build_answer_prompt(*args, **kw) == build_answer_prompt(*args, **kw)


def test_empty_recall_never_points_the_model_at_data_that_does_not_exist():
    """Regression: the no-data branch reused the with-data directive, telling the
    model to answer 'from the verified data' when none had been retrieved."""
    for register in (QUESTION, REASONING, CONVERSATIONAL, TEMPORAL):
        prompt = build_answer_prompt(
            "Why is the sky blue?", {}, memory_required=False, register=register
        )
        assert "verified data" not in prompt
        assert "dated records, preserving" not in prompt
