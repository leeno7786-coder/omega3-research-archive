"""Post-chat identity reflection: prompt, parse, runner, and pattern collection."""

from types import SimpleNamespace

import pytest

from deepsleep_loop._deepsleep_state import Trait
from deepsleep_loop.identity.run import _collect_pattern_observations
from omega_loop.episode.prompts import (
    _chat_reflection_user,
    _filter_identity_patterns,
    _parse_chat_reflection,
)
from omega_loop.episode.run import (
    EpisodeCognitionParseError,
    _with_current_identity,
    run_omega_for_episode,
)
from omega_loop.episode.types import OmegaEpisodeInput, OmegaEpisodeResult


def _packet(**over):
    base = dict(
        episode_id="ep1",
        intent="RESPOND",
        action="RESPONSE",
        tool=None,
        domain=None,
        operator_text="how do transformers handle long context?",
        outcome="none",
        failure_reason=None,
        raw_tool_result=None,
        recall=(),
        recall_error=None,
        prior_mastery=None,
        response_text="They use attention with positional encoding; ...",
    )
    base.update(over)
    return OmegaEpisodeInput(**base)


# ── contract additivity ────────────────────────────────────────────────────────
def test_result_identity_patterns_default_and_input_response_text_default():
    r = OmegaEpisodeResult(
        episode_id="e",
        cognized=True,
        knowledge_to_ingest=(),
        mastery_effects=(),
        pressure_params={},
        response_text=None,
        synthesis=None,
        reflection=None,
    )
    assert r.identity_patterns == ()
    assert not r.to_dict()["identity_patterns"]  # asdict -> (), json -> []; empty either way
    assert _packet().response_text.startswith("They use attention")
    assert (
        OmegaEpisodeInput(
            episode_id="e",
            intent="RESPOND",
            action="RESPONSE",
            tool=None,
            domain=None,
            operator_text="q",
            outcome="none",
            failure_reason=None,
            raw_tool_result=None,
            recall=(),
            recall_error=None,
            prior_mastery=None,
        ).response_text
        is None
    )  # default


# ── filter ─────────────────────────────────────────────────────────────────────
def test_filter_identity_patterns_drops_bad_and_coerces_status():
    rows = _filter_identity_patterns(
        [
            {
                "trait": "surface_assumptions",
                "pattern": "I surface the assumption beneath a question",
                "evidence": "did it here",
                "status": "new",
            },
            {"pattern": "  ", "evidence": "x", "status": "new"},  # empty pattern -> dropped
            {
                "pattern": "I hedge under uncertainty",
                "status": "weird",
            },  # bad status -> coerced; slug derived
            "not a dict",  # dropped
        ]
    )
    assert [r["pattern"] for r in rows] == [
        "I surface the assumption beneath a question",
        "I hedge under uncertainty",
    ]
    assert rows[0]["trait"] == "surface_assumptions"  # explicit slug passes through
    assert rows[1]["trait"] == "i_hedge_under_uncertainty"  # derived from the pattern
    assert rows[1]["status"] == "new"
    assert rows[0]["evidence"] == "did it here"


def test_filter_identity_patterns_non_list_is_empty():
    assert _filter_identity_patterns(None) == ()
    assert _filter_identity_patterns({"pattern": "x"}) == ()


# ── build ──────────────────────────────────────────────────────────────────────
def test_chat_reflection_turns_inward_with_both_sides():
    from organ.promptorgan.packs.foldback import foldback_spec_for

    pkt = _packet(
        omega_lane="chat",
        current_identity=(
            {
                "trait": "structural_analogy",
                "pattern": "I reach for structure",
                "evidence_count": 1,
            },
        ),
    )
    # The chat lane builds its own system prompt; the reflection builder supplies the user half.
    system, user = foldback_spec_for(pkt).build(pkt)
    assert "the user's message" in user
    assert "your AUTO response" in user
    assert "They use attention" in user
    assert "private post-action self-reflection" in system
    assert "Do not output JSON" in system
    assert "Trait candidates:" in system
    assert "stable_trait_key" in system
    assert "control envelope" in system
    assert "how do transformers handle long context" in user  # the user's message
    assert "They use attention" in user  # Omega's own reply
    assert "YOUR CURRENT SELF-MODEL" in user and "structural_analogy" in user  # self-model fed in


def test_chat_reflection_empty_self_model_is_explicit():
    user = _chat_reflection_user(_packet())  # no current_identity
    assert "no traits evidenced yet" in user


def test_identity_injector_separates_candidates_from_promoted_preferences(monkeypatch):
    """Regression: candidate evidence must be matchable without becoming self-model truth."""
    monkeypatch.setattr(
        "deepsleep_loop.identity_store.load_identity",
        lambda: {"direct": Trait("direct", strength=0.4, evidence_count=2)},
    )
    monkeypatch.setattr(
        "deepsleep_loop.identity_store.load_trait_evidence",
        lambda: {
            "candidate": SimpleNamespace(
                name="candidate",
                evidence_count=1,
                observation_count=1,
                pattern="I try a structural analogy",
                evidence="one reply",
                status="new",
            )
        },
    )

    packet = _with_current_identity(_packet())

    assert [row["trait"] for row in packet.current_identity] == ["direct"]
    assert [row["trait"] for row in getattr(packet, "candidate_identity", ())] == ["candidate"]
    user = _chat_reflection_user(packet)
    assert "CANDIDATE PATTERNS" in user
    assert "candidate" in user
    assert "not preferences" in user


def test_candidate_load_failure_does_not_hide_promoted_self_model(monkeypatch):
    """Candidate evidence is optional; promoted identity continuity is not."""
    monkeypatch.setattr(
        "deepsleep_loop.identity_store.load_identity",
        lambda: {"direct": Trait("direct", strength=0.4, evidence_count=2)},
    )
    monkeypatch.setattr(
        "deepsleep_loop.identity_store.load_trait_evidence",
        lambda: (_ for _ in ()).throw(OSError("candidate store unavailable")),
    )

    packet = _with_current_identity(_packet())

    assert [row["trait"] for row in packet.current_identity] == ["direct"]
    assert packet.candidate_identity == ()


# ── parse ──────────────────────────────────────────────────────────────────────
def test_parse_chat_reflection_extracts_patterns_never_reemits_reply():
    payload = {
        "mastery": {"knowledge_mastery": 0.6, "application_mastery": 0.4, "rationale": "flowed"},
        "synthesis": "I met them as a peer and reached for structure.",
        "identity_patterns": [
            {
                "trait": "reach_for_structure",
                "pattern": "I reach for structure when explaining",
                "evidence": "listed mechanisms",
                "status": "confirmed",
            },
        ],
        "pressure_params": {},
        "intent_decision": None,
    }
    res = _parse_chat_reflection(payload, _packet(domain="ml"))
    assert res.response_text is None  # NEVER re-emit the delivered reply
    assert res.synthesis.startswith("I met them")
    assert len(res.identity_patterns) == 1
    assert res.identity_patterns[0]["trait"] == "reach_for_structure"
    assert res.identity_patterns[0]["status"] == "confirmed"
    assert res.mastery_effects and res.mastery_effects[0].domain == "ml"


# ── runner (injected model) ────────────────────────────────────────────────────
def test_response_episode_uses_the_inward_reflection_spec():
    pkt = _packet()
    episode = SimpleNamespace(episode_id="ep1", global_={"omega_packet": pkt.to_dict()})
    reflection = (
        "I noticed the question was about mechanisms, so I answered with attention and "
        "positional encoding before adding a broad summary.\n\n"
        "Trait candidates:\n"
        "- ask_first — I clarify the question's mechanism before generalizing "
        "| evidence: I connected attention and positional encoding to the asked topic "
        "| status: new"
    )
    res = run_omega_for_episode(episode, reason_fn=lambda _s, _u: reflection)
    assert res.cognized is True
    assert res.response_text is None
    assert res.reflection == reflection
    assert res.control_decision["decision"] == "DONE"
    assert res.identity_patterns[0]["trait"] == "ask_first"
    assert res.identity_patterns[0]["pattern"] == "I clarify the question's mechanism before generalizing"


def test_response_episode_accepts_private_freeform_reflection_and_trait_candidates():
    pkt = _packet()
    episode = SimpleNamespace(episode_id="ep1", global_={"omega_packet": pkt.to_dict()})
    reflection = (
        "I answered by naming the mechanism first, then connecting it to the question. "
        "I should keep that explanation direct while marking uncertainty when evidence is thin.\n\n"
        "Trait candidates:\n"
        "- evidence_first_explanation — I ground explanations in mechanisms before generalizing "
        "| evidence: I explained attention and positional encoding before summarizing "
        "| status: new"
    )
    captured = {}
    calls = []

    def reflect(system, user):
        calls.append((system, user))
        captured["system"] = system
        return reflection

    result = run_omega_for_episode(episode, reason_fn=reflect)

    assert len(calls) == 1
    assert "Do not output JSON" in captured["system"]
    assert result.cognized is True
    assert result.response_text is None
    assert result.reflection == reflection
    assert result.control_decision["decision"] == "DONE"
    assert result.identity_patterns == (
        {
            "trait": "evidence_first_explanation",
            "pattern": "I ground explanations in mechanisms before generalizing",
            "evidence": "I explained attention and positional encoding before summarizing",
            "status": "new",
        },
    )


def test_response_episode_rejects_json_instead_of_treating_it_as_reflection():
    pkt = _packet()
    episode = SimpleNamespace(episode_id="ep1", global_={"omega_packet": pkt.to_dict()})
    payload = '{"reflection":"this must not be JSON"}'
    with pytest.raises(EpisodeCognitionParseError, match="freeform prose, not JSON"):
        run_omega_for_episode(episode, reason_fn=lambda _s, _u: payload)


def test_chat_response_reflection_uses_bounded_nonthinking_generation(monkeypatch):
    import runtime.socket

    pkt = _packet()
    episode = SimpleNamespace(episode_id="ep1", global_={"omega_packet": pkt.to_dict()})
    captured = {}

    def invoke(**kwargs):
        captured.update(kwargs)
        return {"text": "I explained the mechanism directly and grounded the answer in the question."}

    monkeypatch.setattr(runtime.socket, "invoke_runtime_generate", invoke)
    result = run_omega_for_episode(episode, runtime_client=object())

    assert result.reflection.startswith("I explained the mechanism")
    assert captured["enable_thinking"] is False
    assert captured["max_tokens"] == 512
    assert captured["timeout"] == 30.0


# ── window collection ──────────────────────────────────────────────────────────
def test_collect_pattern_observations_keeps_independent_episode_evidence():
    # Rewordings share a trait key, but separate foldbacks remain independent evidence.
    ds = SimpleNamespace(
        episode_window=[
            {
                "episode_id": "ep-1",
                "omega_result": {
                    "identity_patterns": [
                        {
                            "trait": "surface_assumptions",
                            "pattern": "I surface assumptions",
                            "evidence": "a",
                            "status": "new",
                        }
                    ]
                },
            },
            {
                "episode_id": "ep-2",
                "omega_result": {
                    "identity_patterns": [
                        {
                            "trait": "surface_assumptions",
                            "pattern": "I name the hidden premise first",
                            "evidence": "b",
                            "status": "confirmed",
                        },
                        {
                            "trait": "reach_for_analogy",
                            "pattern": "I reach for analogy",
                            "evidence": "c",
                            "status": "new",
                        },
                    ]
                },
            },
            {"no_omega": True},
        ]
    )
    rows = _collect_pattern_observations(ds)
    surface = [r for r in rows if r["trait"] == "surface_assumptions"]
    assert len(surface) == 2
    assert {r["observation_id"] for r in surface} == {
        "episode:ep-1:surface_assumptions",
        "episode:ep-2:surface_assumptions",
    }
    assert len([r for r in rows if r["trait"] == "reach_for_analogy"]) == 1


def test_trait_evidence_status_roundtrip():
    t = Trait(
        name="i_ask_first", strength=0.3, evidence_count=2, evidence="did it", status="confirmed"
    )
    d = t.to_dict()
    assert Trait.from_dict(d) == t
