"""CONDUCT: the window's trajectory, joined with mastery, mapped onto Omega's four lanes.

Operator, 2026-09-21: "the per episode chooses the next intent and it needs to use the available
ones, the browser agent, the agent, the webfetch, or the chat lane to send a chat. the ten windows
purpose is purely for trajectory decisions and those decisions should affect omegas overall intent
choices based on what was observed. map the larning and application mastery system into the 4
intents named above and that is how omega will conduct itself autonomously". Enforcement: guide
only.

What these tests pin, and why each one exists:

* The trajectory reached Omega's per-episode lanes only as raw JSON: the agent and browser lanes
  dump ``foldback_packet`` into their evidence, the chat lane shows it nowhere, and the formatted
  ``TRAJECTORY INFLUENCE`` line lived in ``_build_knowledge``/``_build_apply``'s system half, which
  the live agent lane discards (``_, user = ...``); that block has since been retired. So nothing
  told the mind what its own trajectory means for the choice it is about to make.
* The mastery arc already has one stopping rule — ``_trajectory_goal_still_learning`` decides
  whether the LEARN topic cascade keeps drawing from the trajectory. The prompt must name the same
  phase that rule acts on, or Omega is told "apply" while the cascade still feeds it LEARN.
* The four lanes are the intents the operator named: web fetch = LEARN (the model-owned web
  research rail), browser agent = BROWSE, agent = USE_SKILL (autonomous practice; USE_TOOL only
  continues an operator's agent task), chat = ASK_OPERATOR.
* Guide only: nothing here narrows what Omega may choose.
"""

from __future__ import annotations

import pytest

from omega_loop.episode.types import OmegaEpisodeInput

DOMAIN = "python_web_builds"
CAPABILITY = "finishing a CRUD route before claiming it is done"
REASON = "ten episodes planned the same CRUD work and touched no file"


def _record(knowledge: float, application: float, target: float = 0.70):
    from deepsleep_loop._deepsleep_state import MasteryRecord

    return MasteryRecord(
        domain_id=DOMAIN,
        domain_name=DOMAIN,
        knowledge_mastery=knowledge,
        application_mastery=application,
        target_mastery=target,
        status="learning",
    )


@pytest.fixture
def mastery(monkeypatch):
    """The one mastery store the topic cascade reads, faked the way its own tests fake it."""
    from knowledge_extractor.derive.run import _slug

    store: dict = {}
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_mastery", lambda path=None: store
    )

    def put(knowledge: float, application: float, target: float = 0.70) -> None:
        store[_slug(DOMAIN)] = _record(knowledge, application, target)

    return put


# (knowledge, application) -> the phase the mastery arc names
ARC = [
    ((0.20, 0.00), "learn"),
    ((0.49, 0.30), "learn"),
    ((0.50, 0.00), "apply"),
    ((0.65, 0.40), "apply"),
    ((0.90, 0.70), "achieved"),
    # application at target is the goal met, whatever knowledge says
    ((0.30, 0.80), "achieved"),
]


# ── One source of truth for the arc ────────────────────────────────────────


@pytest.mark.parametrize(("levels", "phase"), ARC)
def test_mastery_phase_names_where_the_trajectory_domain_sits(mastery, levels, phase):
    from cognition.topic_resolution.resolve_topic import mastery_phase

    mastery(*levels)
    view = mastery_phase(DOMAIN)

    assert view is not None, "a domain with a mastery record has a phase"
    assert view["phase"] == phase
    assert view["knowledge"] == pytest.approx(levels[0])
    assert view["application"] == pytest.approx(levels[1])
    assert view["target_mastery"] == pytest.approx(0.70)
    assert view["apply_bar"] == pytest.approx(0.5)


def test_a_domain_with_no_record_has_no_phase(mastery):
    from cognition.topic_resolution.resolve_topic import mastery_phase

    assert mastery_phase(DOMAIN) is None, (
        "no record is not 'learn at zero': it is a domain Omega has never touched, and the "
        "topic cascade already treats it differently (it keeps driving LEARN)"
    )


@pytest.mark.parametrize(("levels", "phase"), ARC)
def test_the_prompt_and_the_topic_cascade_read_the_same_phase(mastery, levels, phase):
    """``_trajectory_goal_still_learning`` decides whether LEARN keeps drawing from the trajectory.

    If the prompt named a different phase than the one that rule acts on, Omega would be told
    "apply" while the cascade still feeds it the trajectory as its next LEARN topic.
    """
    from cognition.topic_resolution.resolve_topic import (
        _trajectory_goal_still_learning,
        mastery_phase,
    )

    mastery(*levels)
    assert _trajectory_goal_still_learning(DOMAIN) is (mastery_phase(DOMAIN)["phase"] == "learn")


# ── The packet carries the joined view ─────────────────────────────────────


def _influencer(domain: str = DOMAIN) -> dict:
    return {
        "active_domain": domain,
        "active_goal": CAPABILITY,
        "last_decision": "deepen_capability",
        "confidence": 0.82,
        "reason": REASON,
    }


def test_the_foldback_packet_joins_the_trajectory_with_its_mastery(mastery, monkeypatch):
    from omega_loop.episode import packet

    mastery(0.20, 0.00)
    monkeypatch.setattr(packet, "_trajectory_influencer_block", _influencer)

    conduct = packet._assemble_foldback_packet(object(), {}, None, None, "")["conduct"]

    assert conduct["domain"] == DOMAIN
    assert conduct["capability"] == CAPABILITY
    assert conduct["reason"] == REASON
    assert conduct["phase"] == "learn"
    assert conduct["knowledge"] == pytest.approx(0.20)
    assert conduct["application"] == pytest.approx(0.00)
    # The v2 outcome-shape guard forbids legacy "goal"/"goal_state"/"target" keys anywhere in
    # episode.global_, and this packet rides under global_["omega_packet"].
    assert not {"goal", "goal_state", "target"} & set(conduct)


def test_a_trajectory_domain_with_no_mastery_record_is_new(mastery, monkeypatch):
    from omega_loop.episode import packet

    monkeypatch.setattr(packet, "_trajectory_influencer_block", _influencer)

    conduct = packet._assemble_foldback_packet(object(), {}, None, None, "")["conduct"]

    assert conduct["phase"] == "new"
    assert conduct["domain"] == DOMAIN


def test_no_trajectory_means_no_conduct(mastery, monkeypatch):
    from omega_loop.episode import packet

    monkeypatch.setattr(packet, "_trajectory_influencer_block", lambda: {})

    assert packet._assemble_foldback_packet(object(), {}, None, None, "")["conduct"] == {}


# ── Every per-episode lane renders it; the window lane does not ─────────────

PER_EPISODE_LANES = ("chat", "agent", "browser")


def _conduct(phase: str, knowledge: float = 0.20, application: float = 0.00) -> dict:
    return {
        "domain": DOMAIN,
        "capability": CAPABILITY,
        "reason": REASON,
        "phase": phase,
        "knowledge": knowledge,
        "application": application,
        "target_mastery": 0.70,
        "apply_bar": 0.5,
    }


def _packet(lane: str, conduct: dict | None) -> OmegaEpisodeInput:
    from tests.omega_loop.test_foldback_prompt_dispatch import packet_fixture

    fp = None if conduct is None else {"conduct": conduct}
    return packet_fixture(lane, foldback_packet=fp)


def _system(lane: str, conduct: dict | None) -> str:
    from organ.promptorgan.packs.foldback import foldback_spec_for

    packet = _packet(lane, conduct)
    system, _user = foldback_spec_for(packet).build(packet)
    return system


def _fit_line(system: str) -> str:
    lines = [line for line in system.splitlines() if line.startswith("LANES THAT FIT")]
    assert len(lines) == 1, f"expected exactly one fitting-lanes line, got {lines!r}"
    return lines[0]


@pytest.mark.parametrize("lane", PER_EPISODE_LANES)
def test_every_per_episode_lane_shows_the_trajectory_and_its_phase(lane):
    system = _system(lane, _conduct("learn"))

    assert "CONDUCT" in system, (
        f"the {lane} lane chooses Omega's next intent without being shown what its own "
        "trajectory means for that choice"
    )
    assert DOMAIN in system and CAPABILITY in system and REASON in system
    assert "LEARN phase" in system
    assert "knowledge 0.20" in system and "application 0.00" in system
    assert "apply bar 0.50" in system and "target 0.70" in system


# phase -> the intents whose lanes fit it
PHASE_LANES = {
    "new": {"LEARN"},
    "learn": {"LEARN", "BROWSE"},
    "apply": {"USE_SKILL", "BROWSE"},
    "achieved": {"ASK_OPERATOR"},
}
ALL_INTENTS = {"LEARN", "BROWSE", "USE_SKILL", "ASK_OPERATOR"}


@pytest.mark.parametrize(("phase", "fitting"), sorted(PHASE_LANES.items()))
def test_each_phase_names_the_lanes_that_fit_it(phase, fitting):
    fit = _fit_line(_system("chat", _conduct(phase)))

    for intent in fitting:
        assert f"intent={intent}" in fit, f"{phase}: {intent} fits this phase: {fit!r}"
    for intent in ALL_INTENTS - fitting:
        assert f"intent={intent}" not in fit, f"{phase}: {intent} does not fit: {fit!r}"


def test_the_four_lanes_are_named_as_the_intents_that_reach_them():
    system = _system("agent", _conduct("apply", 0.60, 0.20))

    assert "web fetch = LEARN" in system
    assert "browser agent = BROWSE" in system
    assert "agent = USE_SKILL" in system
    assert "chat = ASK_OPERATOR" in system


def test_the_window_lane_is_not_steered_by_the_trajectory_it_sets():
    """The window's purpose is purely the trajectory decision (operator 2026-09-21)."""
    assert "CONDUCT" not in _system("window", _conduct("learn"))


@pytest.mark.parametrize("lane", PER_EPISODE_LANES + ("window",))
def test_no_conduct_leaves_every_prompt_byte_identical(lane):
    assert _system(lane, {}) == _system(lane, None)


# ── Guide only ─────────────────────────────────────────────────────────────


def test_guide_only_says_the_choice_is_omegas():
    system = _system("chat", _conduct("learn"))

    assert "guidance, not a command" in system
    assert "never overrides the operator's request or this episode's evidence" in system


def test_guide_only_every_intent_stays_available():
    from schema.omega_control_decision import PRESSURE_INTENTS

    assert PRESSURE_INTENTS == {"BROWSE", "USE_SKILL", "USE_TOOL", "LEARN", "ASK_OPERATOR"}


def test_guide_only_a_lane_that_does_not_fit_the_phase_is_still_accepted():
    """Omega may choose against the guidance; the host validates the contract, not the fit."""
    from organ.promptorgan.packs.meta_omega.core import parse_lane

    packet = _packet("agent", _conduct("learn"))
    control = {
        "assessment": {
            "who": "Omega", "what": "practice the route", "when": "now", "where": "workspace",
            "why": "the operator asked for finished routes", "how": "build it",
            "desired_state": "a finished CRUD route", "observed_state": "none built",
            "gap": "no route exists", "evidence": ["no files touched"],
        },
        "decision": "PRESSURE",
        "pressure": {
            "intent": "USE_SKILL", "confidence": 0.8, "reason": "build the route now",
            "setpoints": {}, "desired_state": "a finished CRUD route",
            "observed_state": "none built", "resolution_conditions": ["the route exists"],
            "domain": DOMAIN,
        },
        "synthesis": "building instead of studying",
    }

    result = parse_lane({"control": control}, packet)

    assert result.intent_decision["intent"] == "USE_SKILL"
