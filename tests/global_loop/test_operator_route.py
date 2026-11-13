"""Global A routes operator turns: the intent router decides, or Global A reasons over its abstention."""

import json
from types import SimpleNamespace

import pytest

from global_loop.intent import context as route_context
from global_loop.intent import router as intent_router
from global_loop.intent.route import resolve_operator_route, routes_operator_turn
from global_loop.loop import run_global_a
from pressure_loop.contributors import EpisodeIgnition, make_contributor
from schema.omega_control_decision import omega_lane_for_intent


def _decision(route, reason="accepted"):
    lead = route or "CODING_AGENT"
    return intent_router.RouteDecision(
        route=route, reason=reason, confidence=0.99 if route else 0.51, margin=0.98 if route else 0.04,
        primary={r: (0.99 if route else 0.51) if r == lead else 0.003 for r in intent_router.ROUTES},
        intents=(lead,), intent_scores={r: float(r == lead) for r in intent_router.ROUTES},
        certainty={"known": 1.0 if route else 0.2, "ambiguous": 0.0 if route else 0.8, "out_of_domain": 0.0})


def _ignition(message="hello", *, kind="operator_query", source="operator", **metadata):
    contributor = make_contributor(source, kind, 1.0, {"message": message, **metadata})
    return EpisodeIgnition("ep-route", 1.0, contributor.type, contributor, (contributor,), None)


def _episode(episode_id="ep-route"):
    return SimpleNamespace(episode_id=episode_id, global_={}, pressure={})


@pytest.fixture(autouse=True)
def _fresh_context():
    route_context.reset()
    yield
    route_context.reset()


@pytest.fixture
def router(monkeypatch):
    state = {"decision": _decision("FREEFORM_CHAT")}
    calls = []

    def route(text, context=""):
        calls.append((text, context))
        return state["decision"]

    monkeypatch.setattr(intent_router, "get_router", lambda **_: SimpleNamespace(route=route))
    return state, calls


@pytest.mark.parametrize("route,intent,lane,target_lane", [
    ("FREEFORM_CHAT", "RESPOND", "chat", "chat"),
    ("CODING_AGENT", "RESPOND", "chat", "agent"),
    ("DOCUMENT_CREATION", "DOCUMENT_CREATE", "chat", "chat"),
])
def test_confident_router_keeps_operator_episode_in_chat_and_records_target_lane(
        router, monkeypatch, route, intent, lane, target_lane):
    state, calls = router
    state["decision"] = _decision(route)
    monkeypatch.setattr("runtime.socket.invoke_runtime_generate",
                        lambda **_: pytest.fail("a confident route must not call the reasoning model"))
    episode = _episode()

    decision = run_global_a(episode, _ignition("hello omega"))

    assert (decision.intent, decision.omega_lane) == (intent, lane)
    routed = episode.global_["operator_route"]
    assert (routed["route"], routed["source"], routed["lane"], routed["request"]) == (
        route, "router", target_lane, "hello omega")
    assert calls == [("hello omega", "")]


def test_abstention_escalates_to_global_a_reasoning(router, monkeypatch):
    state, _ = router
    state["decision"] = _decision(None, "certainty_ambiguous")
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        return {"text": json.dumps({"route": "CODING_AGENT", "reason": "the previous action was project work"})}

    monkeypatch.setattr("runtime.socket.invoke_runtime_generate", generate)
    route_context.remember_action("CODING_AGENT", "run the test suite")
    episode = _episode("ep-escalate")

    decision = run_global_a(episode, _ignition("fix that"))

    assert (decision.intent, decision.omega_lane) == ("RESPOND", "chat")
    routed = episode.global_["operator_route"]
    assert (routed["route"], routed["source"], routed["reason"]) == (
        "CODING_AGENT", "escalation", "the previous action was project work")
    assert routed["router"]["reason"] == "certainty_ambiguous"
    assert len(calls) == 1
    assert calls[0]["operation"] == "intent_escalation"
    assert calls[0]["tools"] is None
    schema = calls[0]["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["route"]["enum"] == list(intent_router.ROUTES)
    evidence = json.loads(calls[0]["prompt"].split(": ", 1)[1])
    assert evidence["operator_message"] == "fix that"
    assert evidence["previous_assistant_action"] == 'CODING_AGENT: worked in the operator\'s project on "run the test suite"'


@pytest.mark.parametrize("text", [
    '{"route": "SHELL", "reason": "unknown"}',
    '{"route": "CODING_AGENT"}',
    '{"route": "CODING_AGENT", "reason": "x", "extra": 1}',
    "not json",
])
def test_invalid_escalation_fails_hard(router, monkeypatch, text):
    state, _ = router
    state["decision"] = _decision(None, "below_margin")
    monkeypatch.setattr("runtime.socket.invoke_runtime_generate", lambda **_: {"text": text})

    with pytest.raises(ValueError):
        run_global_a(_episode(), _ignition("do it"))


@pytest.mark.parametrize("message,kind,metadata", [
    ("a bright star filled galaxy", "operator_command", {"directive_intent": "IMAGE_CREATE"}),
    ("write my resume", "operator_command", {"directive_intent": "DOCUMENT_CREATE"}),
    ("what's on my calendar", "operator_query", {"plugin_operation": {"plugin_id": "calendar"}}),
    ("learn about rust lifetimes", "operator_command", {"operator_command_kind": "learn"}),
    ("yes, go with the second option", "operator_response", {}),
])
def test_explicit_selections_never_reach_the_router(message, kind, metadata):
    assert routes_operator_turn(_ignition(message, kind=kind, **metadata)) is False


@pytest.mark.parametrize("directive", ["BROWSE", "USE_SKILL"])
def test_explicit_operator_directives_keep_the_chat_lane(directive, monkeypatch):
    monkeypatch.setattr(
        "runtime.socket.invoke_runtime_generate",
        lambda **_: pytest.fail("an explicit directive must not invoke route reasoning"),
    )
    episode = _episode()

    decision = run_global_a(
        episode,
        _ignition("run the selected action", directive_intent=directive),
    )

    expected = {
        "BROWSE": ("BROWSE", "browser", "BROWSER_AGENT"),
        "USE_SKILL": ("RESPOND", "agent", "CODING_AGENT"),
    }[directive]
    assert (decision.intent, decision.omega_lane) == (expected[0], "chat")
    assert episode.global_["operator_route"]["lane"] == expected[1]
    assert episode.global_["operator_route"]["route"] == expected[2]


def test_autonomous_pressure_never_reaches_the_router():
    assert routes_operator_turn(_ignition("mastery review", kind="self_wake", source="autonomous")) is False


def test_previous_action_reaches_the_router_and_expires(router):
    _, calls = router
    route_context.remember_action("BROWSER_AGENT", "open huggingface and search models", now=1000.0)
    assert route_context.previous_action(now=1060.0) == (
        'BROWSER_AGENT: worked in the browser on "open huggingface and search models"')
    assert route_context.previous_action(now=1000.0 + route_context.CONTEXT_TTL_SECONDS + 1) == ""

    route_context.remember_action("DOCUMENT_CREATION", "make my resume a pdf")
    resolve_operator_route(_episode(), _ignition("now a docx too"))

    assert calls[-1] == ("now a docx too", 'DOCUMENT_CREATION: created a document for "make my resume a pdf"')


def test_document_creation_is_a_chat_lane_episode():
    assert omega_lane_for_intent("DOCUMENT_CREATE") == "chat"
    assert omega_lane_for_intent("IMAGE_CREATE") == "agent"
