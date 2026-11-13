import json
from types import SimpleNamespace

import pytest

from organ.promptorgan.packs.foldback import foldback_spec_for
from tests.omega_loop.test_foldback_prompt_dispatch import packet_fixture


def done_control():
    return {"assessment": {**dict.fromkeys(
        ("who", "what", "when", "where", "why", "how", "desired_state", "observed_state"),
        "observed episode evidence"), "gap": "", "evidence": ["receipt"]},
        "decision": "DONE", "pressure": None, "synthesis": "Objective evidenced complete."}


WINDOW_REFLECTION = {"reflection": {"summary": "Two lookups circling one gap.",
                                    "reasoning": {"what": "looked things up", "why": "the gap recurred"}}}


@pytest.mark.parametrize("lane", ["chat", "agent", "browser", "window"])
def test_every_lane_validates_control_and_ignores_legacy_authority(lane):
    packet = packet_fixture(lane)
    spec = foldback_spec_for(packet)
    control = done_control()
    # The window lane's reflection is a required lane payload; the other lanes have none.
    lane_payload = WINDOW_REFLECTION if lane == "window" else {}
    result = spec.parse({"control": control, "pressure_params": {"execution_debt_delta": 1},
                         "intent_decision": {"intent": "APPLY"}, **lane_payload}, packet)
    assert result.control_decision == control
    assert result.pressure_params == {}
    assert result.intent_decision is None
    assert result.response_text is None
    if lane == "window":
        with pytest.raises(ValueError, match="'reflection' object"):
            spec.parse(control, packet)
    else:
        assert spec.parse(control, packet).control_decision == control
    with pytest.raises(ValueError):
        spec.parse({"control": {**control, "extra": True}, **lane_payload}, packet)
    with pytest.raises(ValueError):
        spec.parse({"synthesis": "no control", **lane_payload}, packet)


@pytest.mark.parametrize("lane", ["chat", "agent", "browser", "window"])
def test_common_monitoring_contract(lane):
    system, _ = foldback_spec_for(packet_fixture(lane)).build(packet_fixture(lane))
    for term in ("Nelson", "5W1H", "desired_state", "observed_state", "resolution_conditions",
                 "does not execute tools", "low-level tool arguments", '"control"'):
        assert term in system
    assert '"pressure_params"' not in system
    assert '"intent_decision"' not in system


def test_browser_policy_judges_the_agent_run_from_page_evidence():
    system, _ = foldback_spec_for(packet_fixture("browser")).build(packet_fixture("browser"))
    for term in ("live browser/tool observation", "persistent memory", "browser_run", "final_page",
                 "ask_operator", "budget_exhausted", "never PRESSURE intent=BROWSE",
                 "proven by the page"):
        assert term in system
    assert "plan_id" not in system and "step_transition" not in system


def test_chat_identity_and_agent_acceptance_survive_control_parsing():
    identity = {"trait": "evidence_first", "pattern": "I examine evidence first",
                "evidence": "The reply cited the receipt", "status": "confirmed"}
    packet = packet_fixture("chat")
    result = foldback_spec_for(packet).parse({"control": done_control(),
        "identity_patterns": [identity], "mastery": {"knowledge_mastery": 0.7,
        "application_mastery": 0.5, "rationale": "receipt"}}, packet)
    assert result.identity_patterns == (identity,)
    assert result.mastery_effects[0].knowledge_mastery == 0.7
    packet = packet_fixture("agent")
    assert foldback_spec_for(packet).parse({"control": done_control(),
                                          "build_accepted": True}, packet).build_accepted is True


def browser_run(status="budget_exhausted", **fields):
    run = {"status": status, "steps": 2, "actions": [
        {"step": 1, "action": "navigate", "summary": "navigate https://huggingface.co/models", "ok": True,
         "error": "", "url": "https://huggingface.co/models"},
        {"step": 2, "action": "scroll", "summary": "scroll down 600", "ok": True, "error": "", "url": ""}],
        "final_page": {"url": "https://huggingface.co/models", "title": "Models", "text": "Models under 9B"},
        "summary": "", "question": "", "stopped_reason": "2 actions taken", "operator_notes_seen": 0}
    run.update(fields)
    return run


def browser_packet(status="budget_exhausted"):
    return packet_fixture("browser", original_objective="Browse models under 9B", browser_run=browser_run(status))


def browse_control(**pressure):
    control = done_control()
    control["assessment"]["gap"] = "Need a readable post-action view"
    control.update(decision="PRESSURE", pressure={"intent": "BROWSE", "confidence": 0.8,
        "reason": "Filter the catalog to models under 9B", "setpoints": {"verification_debt_delta": 0.9},
        "desired_state": "Result visible", "observed_state": "Catalog open",
        "resolution_conditions": ["Read the resulting page"], **pressure})
    return control


@pytest.mark.parametrize("packet,match", [
    (browser_packet("blocked"), "blocked browser run"),
    (browser_packet("inference_failed"), "inference_failed browser run"),
    (browser_packet("note_queued"), "note_queued browser run"),
    (packet_fixture("agent"), "browser lane"),
])
def test_browse_control_cannot_continue_a_run_that_could_not_act(packet, match):
    with pytest.raises(ValueError, match=match):
        foldback_spec_for(packet).parse({"control": browse_control()}, packet)


@pytest.mark.parametrize("status", ["done", "budget_exhausted", "ask_operator"])
def test_browse_control_may_continue_a_run_that_acted(status):
    packet = browser_packet(status)
    control = browse_control()
    assert foldback_spec_for(packet).parse({"control": control}, packet).control_decision == control


def test_browser_pressure_roundtrip_and_strict_nested_validation():
    packet = browser_packet()
    control = browse_control()
    spec = foldback_spec_for(packet)
    result = spec.parse({"control": control}, packet)
    assert result.control_decision == control
    assert result.pressure_params == {"verification_debt_delta": 0.9}
    assert result.intent_decision["intent"] == "BROWSE"
    control["pressure"]["tool_arguments"] = {"selector": "button"}
    with pytest.raises(ValueError):
        spec.parse({"control": control}, packet)


def test_runner_contains_exhausted_control_repair(monkeypatch):
    from omega_loop.episode import run

    packet = packet_fixture("browser")
    monkeypatch.setattr(run, "_with_current_identity", lambda value: value)
    attempts = []

    def reason(system, user):
        attempts.append(user)
        return "{}"

    episode = SimpleNamespace(episode_id=packet.episode_id,
                              global_={"omega_packet": packet.to_dict()}, temporal={})
    with pytest.raises(run.EpisodeCognitionParseError):
        run.run_omega_for_episode(episode, reason_fn=reason)
    assert len(attempts) == 2


@pytest.mark.parametrize("action", ["IDLE", "ASK_OPERATOR"])
def test_runner_repairs_control_without_action_shortcuts(action, monkeypatch):
    from dataclasses import replace
    from omega_loop.episode import run

    packet = replace(packet_fixture(), action=action)
    monkeypatch.setattr(run, "_with_current_identity", lambda value: value)
    attempts = []

    def reason(system, user):
        attempts.append(user)
        return json.dumps({"control": done_control()} if len(attempts) == 2 else {})

    episode = SimpleNamespace(episode_id=packet.episode_id,
                              global_={"omega_packet": packet.to_dict()}, temporal={})
    result = run.run_omega_for_episode(episode, reason_fn=reason)
    assert result.control_decision == done_control()
    assert len(attempts) == 2
    assert "REPAIR REQUIRED" in attempts[1]


# ---------------------------------------------------------------------------
# EARLY-V01, 2026-09-21. Across 27 recorded window reflections, ONE was rejected
# outright by validate_window_payload and its whole conclusion was lost:
#
#   invalid reflection.trajectory.decision 'empty' -- expected one of
#   ['adjust', 'complete', 'continue', 'establish'] or an empty string
#
# The model did not misbehave. WINDOW_POLICY advertises the trajectory decision as
# "establish|continue|adjust|complete, or empty", so a reader that takes the list
# literally writes the string "empty" -- which the validator refuses, because it wants
# the EMPTY STRING. Prompt and validator disagree about what "empty" names.
#
# The validator is right to be strict (law section 2: no fallbacks, no compatibility
# shims), so the fix belongs in the prompt's wording. This test is the round-trip law
# section 2.1 point 3 asks for: every decision token the prompt advertises must be one
# the validator accepts, so the two cannot drift apart again in silence.


def _advertised_trajectory_decisions() -> set[str]:
    """The decision tokens WINDOW_POLICY actually offers the model, read off the policy.

    Parsed from the policy text rather than restated here: a copy of the vocabulary in
    the test would keep passing while the prompt drifted, which is the failure mode this
    test exists to catch.
    """
    import re

    from organ.promptorgan.packs.meta_omega.window import WINDOW_POLICY

    match = re.search(r'"decision"\s*:\s*"([^"]*)"', WINDOW_POLICY)
    assert match, "WINDOW_POLICY no longer shows a trajectory decision field to the model"
    offered = match.group(1)
    tokens: set[str] = set()
    for chunk in offered.split("|"):
        for piece in re.split(r",\s*or\s+|,\s*", chunk):
            token = piece.strip().strip('"').strip()
            if token:
                tokens.add(token.lower())
    return tokens


def test_every_trajectory_decision_the_prompt_offers_is_one_the_validator_accepts():
    """Prompt and validator, one vocabulary."""
    from omega_loop.window.run import _TRAJECTORY_DECISIONS

    offered = _advertised_trajectory_decisions()
    accepted = set(_TRAJECTORY_DECISIONS)

    # A word the prompt uses to DESCRIBE the empty string is not itself a token.
    unaccepted = {t for t in offered if t not in accepted}

    assert unaccepted == set(), (
        f"WINDOW_POLICY offers {sorted(unaccepted)} as a trajectory decision, which "
        f"validate_window_payload rejects (it accepts {sorted(accepted)} or the empty "
        "string). A model that takes the prompt at its word loses its entire reflection; "
        "this is measured in docs/superpowers/evidence/2026-09-21-early-v01."
    )


def test_the_prompt_asks_for_an_empty_string_not_the_word_empty():
    """The positive half: the 'changed nothing' case must still be reachable.

    Removing the ambiguous word must not remove the OPTION. An empty decision is legal
    and means "this window changed nothing", so the prompt has to keep offering it --
    just unambiguously.
    """
    from omega_loop.window.run import validate_window_payload
    from organ.promptorgan.packs.meta_omega.window import WINDOW_POLICY

    assert '""' in WINDOW_POLICY, (
        "the prompt must show the empty string explicitly, or a model has no way to say "
        "'this window changed nothing' without guessing a word for it"
    )

    payload = {
        "reflection": {
            "summary": "Ten episodes circling one gap.",
            "reasoning": {"what": "planned repeatedly", "why": "no execution followed",
                          "identity": ""},
            "trajectory": {"decision": "", "target_domain": "", "target_capability": "",
                           "reason": ""},
        }
    }
    validate_window_payload(payload)  # must not raise


# ---------------------------------------------------------------------------
# EARLY-V01 recommendation 2. With the spine widened, the window view now carries
# receipts; the policy has to tell the model to COMPARE them across episodes, because
# that is what separated "a stall" from "iteration" in the trial.
#
# What is and is not pinned here. The trial measured the benefit empirically: arms B and
# C both reached 9/9 on the verdict, but C -- the arm with this addendum -- named the exact
# receipt it relied on in 6/6 stalled samples against B's 3/6, and twice drew the
# conclusion "a stall, not iteration" that B left implicit. That is a model-behaviour
# result and is not reproducible in a unit test; its evidence is
# docs/superpowers/evidence/2026-09-21-early-v01/adjudication.md.
#
# What IS checkable, and is the reason this test exists, is the AGREEMENT between the two
# sides: a policy that tells the model to read `touched_files` while the spine has stopped
# projecting it would send the model looking for evidence that is not there, and nothing
# would fail. So the test asserts the field names on both sides, in both directions --
# never the prose, which would be a tautology since I wrote it.


def _spine_receipt_keys() -> set[str]:
    """Every key a real widened spine projects, including inside ``agent_run``."""
    from omega_loop.window.run import _episode_spine

    body = {
        "episode_id": "ep-1", "intent": "USE_TOOL", "action": "RESPONSE",
        "cascade_status": "completed", "work_kind": "autonomous",
        "omega_result": {
            "control_decision": {"decision": "PRESSURE", "synthesis": "s"},
            "intent_decision": {"reason": "r", "confidence": 0.9},
            "pressure_params": {"application_debt_delta": 0.1},
        },
        "outputs": {"agent_run": {"job_id": "j", "status": "batch_ran", "steps": 1,
                                  "project_files": [], "touched_files": [],
                                  "stopped_reason": ""}},
    }
    spine = _episode_spine(body, 0)
    keys = set(spine)
    keys |= set(spine["agent_run"] or {})
    return keys


# The receipts EARLY-V01 measured as decisive for telling a stall from iteration. Named
# here because the trial named them, not because the policy happens to mention them.
_DECISIVE_RECEIPTS = {"job_id", "touched_files", "pressure_params"}


def test_the_window_policy_directs_the_model_at_the_receipts_that_decide_a_stall():
    from organ.promptorgan.packs.meta_omega.window import WINDOW_POLICY

    missing = sorted(r for r in _DECISIVE_RECEIPTS if r not in WINDOW_POLICY)

    assert missing == [], (
        f"WINDOW_POLICY never mentions {missing}. The spine now projects these and they are "
        "what separated a stalled build from a productive one in EARLY-V01 (one job id, "
        "nothing touched, debt accumulating); a policy that does not point at them leaves "
        "the model to notice them by luck."
    )


def test_the_window_policy_names_no_receipt_the_spine_does_not_project():
    """The other direction: no instruction to look for evidence that is not in the view."""
    import re

    from organ.promptorgan.packs.meta_omega.window import WINDOW_POLICY

    projected = _spine_receipt_keys()
    # Only snake_case identifiers, so ordinary prose is not mistaken for a field name.
    candidates = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", WINDOW_POLICY))
    # Words the policy legitimately uses that are contract fields of the PAYLOAD it asks
    # the model to return, not fields of the episode view it reads.
    payload_fields = {
        "target_domain", "target_capability", "trajectory_update", "desired_state",
        "observed_state", "emergent_observations", "repeated_behaviors", "stable_patterns",
    }
    referenced = {c for c in candidates if c not in payload_fields}

    unknown = sorted(r for r in referenced if r not in projected)

    assert unknown == [], (
        f"WINDOW_POLICY refers to {unknown}, which the spine does not project. The model "
        "would be told to weigh evidence the view never carries, and no test or run would "
        "notice. Either project the field or stop naming it."
    )
