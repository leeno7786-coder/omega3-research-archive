"""WO-04 — Global A's intent and Meta A's plan are recorded on the RUNTIME path, before AUTO.

These drive the cascade's own handoffs — `cognition_chain._start_global_a`, `_start_meta_a` and
`_bind_execution_contract` — against a real admitted episode, and read the result back through
WO-03's `read_episode_events`. Only the NEXT phase is stubbed, never the producer under test, so
the real `run_global_a`, `run_meta_a` and `bind_execution_contract` all execute. Calling a capture
helper directly would prove the helper and not the integration, which is the distinction this file
exists to respect.

The property: **the bar exists before the work.** Acceptance criteria captured after a result could
be trimmed to fit it, so the criteria event has to be durable at AUTO ENTRY — asserted from inside
the stub that stands in for AUTO, which is the only place that ordering can be observed.

No live model is invoked: the intent router is replaced with a deterministic stand-in, the same
seam `tests/global_loop/test_operator_route.py` uses.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import cognition_chain as chain
from core.cognition_lock import pipeline_cognition_lock
from global_loop.intent import context as route_context
from global_loop.intent import router as intent_router
from memory import episode_sram
from memory.episode_events import read_episode_events
from pressure_loop import cognition_queue as cq
from pressure_loop.contributors import EpisodeIgnition, make_contributor
from pressure_loop.episode import EpisodeStore

THREAD = "thread-plan-sentinel"
TURN = "turn-plan-sentinel"
ARRIVAL = 77


@pytest.fixture(autouse=True)
def _fresh_route_context():
    route_context.reset()
    yield
    route_context.reset()


@pytest.fixture
def router(monkeypatch):
    """A deterministic router stand-in; never a live model."""
    state = {"decision": None}
    monkeypatch.setattr(intent_router, "get_router",
                        lambda **_: SimpleNamespace(route=lambda text, context="": state["decision"]))
    return state


def _route_decision(route="FREEFORM_CHAT", *, confidence=0.99, margin=0.98):
    lead = route or "CODING_AGENT"
    return intent_router.RouteDecision(
        route=route, reason="accepted", confidence=confidence, margin=margin,
        primary={r: confidence if r == lead else 0.003 for r in intent_router.ROUTES},
        intents=(lead,), intent_scores={r: float(r == lead) for r in intent_router.ROUTES},
        certainty={"known": 1.0, "ambiguous": 0.0, "out_of_domain": 0.0},
    )


@pytest.fixture
def admitted():
    """A real admitted episode: real queue row, real store, real admission seam."""
    stores: list[EpisodeStore] = []

    def run(message="plan this", *, kind="operator_query", source="operator",
            pipeline_id=0, work_kind="operator_turn", **metadata):
        contributor = make_contributor(source, kind, 1.0, {
            "message": message, "cycle_id": "bubble-plan", "arrival_seq": ARRIVAL,
            "thread_id": THREAD, "turn_id": TURN, **metadata,
        })
        assert cq.enqueue_input(contributor, operator_activity=False)
        leased = cq.lease_next_input(pipeline_id)
        assert leased is not None
        ignition = EpisodeIgnition(
            episode_id="ig-plan", aggregate_pressure=1.0, opened_by=leased.type,
            dominant_contributor=leased, contributors=(leased,),
            trajectory_snapshot={"trajectory_id": "traj-plan"}, pipeline_id=pipeline_id,
            work_kind=work_kind,
        )
        lock = pipeline_cognition_lock(pipeline_id)
        lock.acquire()
        try:
            store = EpisodeStore()
            stores.append(store)
            return (store, *chain._admit_episode(store, ignition))
        finally:
            lock.release()

    yield run
    for store in stores:
        try:
            store.close(cascade_status="failed")
        except Exception:
            pass
    episode_sram.reset()


def _global_a(monkeypatch, store, episode, ignition):
    """The real Global A handoff, with only the NEXT phase stubbed.

    Bound to THIS episode's owner, the way the cascade runs it: each episode has its own
    pipeline, and WO-03A R1 refuses a caller holding another episode's binding.
    """
    with monkeypatch.context() as patched:
        patched.setattr(chain, "_start_meta_a", lambda *_a, **_k: None)
        with episode_sram.bind_episode(episode.episode_id, pipeline_id=episode.pipeline_id):
            chain._start_global_a(store, episode, ignition)


def _meta_a(monkeypatch, store, episode):
    """The real Meta A handoff, with only the NEXT phase stubbed."""
    with monkeypatch.context() as patched:
        patched.setattr(chain, "_bind_execution_contract", lambda *_a, **_k: None)
        chain._start_meta_a(store, episode)


def _bind_contract(monkeypatch, store, episode, *, at_auto=None):
    """The real contract handoff; `at_auto` observes the state AUTO would be entered with."""
    with monkeypatch.context() as patched:
        patched.setattr(chain, "_start_auto",
                        lambda *_a, **_k: at_auto() if at_auto is not None else None)
        chain._bind_execution_contract(store, episode)


def _events(episode_id, kind):
    return [e for e in read_episode_events(episode_id) if e.event_kind == kind]


def _one(episode_id, kind):
    found = _events(episode_id, kind)
    assert len(found) == 1, f"expected exactly one {kind} event, got {len(found)}"
    return found[0]


# ── the admitted identities reach every later phase event ─────────────────────


def test_every_phase_event_carries_the_admitted_identities(admitted, router, monkeypatch):
    """A Global A or Meta A event must be tied to the request that caused it."""
    router["decision"] = _route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("what is the status?")
    _global_a(monkeypatch, store, episode, ignition)
    _meta_a(monkeypatch, store, episode)
    _bind_contract(monkeypatch, store, episode)

    recorded = read_episode_events(episode.episode_id)
    assert len(recorded) >= 5, [e.event_kind for e in recorded]
    for event in recorded:
        assert event.arrival_seq == ARRIVAL, event.event_kind
        assert event.turn_id == TURN, event.event_kind
        assert event.thread_id == THREAD, event.event_kind
        assert event.episode_id == episode.episode_id


@pytest.mark.parametrize("phase", ["meta_b", "global_b"])
def test_meta_b_and_global_b_raises_are_captured_before_they_propagate(
    admitted, monkeypatch, phase
):
    store, episode, _ = admitted("complete the report")
    failure = RuntimeError(f"{phase} substrate failed")

    def raise_failure(*_args, **_kwargs):
        raise failure

    if phase == "meta_b":
        monkeypatch.setattr("meta_loop.meta_b.run_meta_b_front", raise_failure)
    else:
        monkeypatch.setattr("meta_loop.meta_b.run_meta_b_front", lambda *_a, **_k: {})
        monkeypatch.setattr("global_loop.loop.run_global_b", raise_failure)

    with episode_sram.bind_episode(episode.episode_id, pipeline_id=episode.pipeline_id):
        with pytest.raises(RuntimeError) as caught:
            chain._start_meta_b_global_b(store, episode)

    assert caught.value is failure
    event = _one(episode.episode_id, f"{phase}.phase_failed")
    assert event.payload["phase"] == phase
    assert event.payload["planning_status"] == "failed"
    assert "substrate failed" in event.payload["reason"]


def test_contained_omega_fold_failure_is_captured(admitted, monkeypatch):
    from omega_loop.episode.run import EpisodeCognitionFoldError

    _, episode, _ = admitted("complete the report")
    failure = EpisodeCognitionFoldError("the fold returned no valid decision")

    def raise_failure(_episode):
        raise failure

    monkeypatch.setattr("omega_loop.episode.run.run_omega_for_episode", raise_failure)
    with episode_sram.bind_episode(episode.episode_id, pipeline_id=episode.pipeline_id):
        assert chain._run_omega_episode_cognition(episode) is None

    event = _one(episode.episode_id, "omega.phase_failed")
    assert event.payload["phase"] == "omega"
    assert event.payload["planning_status"] == "failed"
    assert "no valid decision" in event.payload["reason"]


def test_one_episodes_identities_never_leak_into_another(admitted, router, monkeypatch):
    """Two admitted episodes, planned in turn: neither wears the other's request identity."""
    router["decision"] = _route_decision("FREEFORM_CHAT")
    store_a, episode_a, ignition_a = admitted("first request")
    store_b, episode_b, ignition_b = admitted(
        "second request", source="omega", kind="lane_work_dispatch", pipeline_id=1,
        work_kind="operator_lane_work", lane="browser", cycle_id="bubble-other", arrival_seq=ARRIVAL + 1,
        thread_id="thread-other", turn_id="turn-other",
    )
    assert episode_a.episode_id != episode_b.episode_id

    _global_a(monkeypatch, store_b, episode_b, ignition_b)
    _global_a(monkeypatch, store_a, episode_a, ignition_a)

    for event in read_episode_events(episode_a.episode_id):
        assert (event.arrival_seq, event.turn_id, event.thread_id) == (ARRIVAL, TURN, THREAD)
    for event in read_episode_events(episode_b.episode_id):
        assert (event.arrival_seq, event.turn_id, event.thread_id) == (
            ARRIVAL + 1, "turn-other", "thread-other")


# ── Global A: intent, routing revision, real confidence ───────────────────────


def test_global_a_records_its_selected_intent_and_route(admitted, router, monkeypatch):
    router["decision"] = _route_decision("CODING_AGENT")
    store, episode, ignition = admitted("build me a parser")

    _global_a(monkeypatch, store, episode, ignition)

    payload = _one(episode.episode_id, "global_a.intent_selected").payload
    decision = episode.global_["intent_decision"]
    assert payload["intent"] == decision["intent"]
    assert payload["omega_lane"] == decision["omega_lane"]
    assert payload["reason"] == decision["reason"]
    # WO-04A finding B: intent_confidence is an Observation, so an absent score can be declared.
    assert payload["intent_confidence"]["state"] == "observed"
    assert payload["intent_confidence"]["value"] == pytest.approx(decision["confidence"])
    assert payload["route"]["state"] == "observed"
    assert payload["route"]["value"]["route"] == "CODING_AGENT"
    assert payload["route"]["value"]["source"] == "router"


def test_global_a_records_the_confidence_the_router_actually_returned(admitted, router, monkeypatch):
    router["decision"] = _route_decision("DOCUMENT_CREATION", confidence=0.87, margin=0.8)
    store, episode, ignition = admitted("write the memo")

    _global_a(monkeypatch, store, episode, ignition)

    confidence = _one(episode.episode_id, "global_a.intent_selected").payload["router_confidence"]
    assert confidence["state"] == "observed"
    assert confidence["value"] == pytest.approx(0.87)


def test_an_absent_router_confidence_is_declared_never_invented(admitted, router, monkeypatch):
    """Missing signal: no fabricated 0.0 or 0.5 for a classifier that did not score."""
    store, episode, ignition = admitted(
        "hmm", kind="curiosity", source="autonomous", lane="agent", pipeline_id=2
    )

    _global_a(monkeypatch, store, episode, ignition)

    payload = _one(episode.episode_id, "global_a.intent_selected").payload
    for name in ("router_confidence", "route", "routing_revision"):
        signal = payload[name]
        assert signal["state"] in {"not_applicable", "unavailable"}, (name, signal)
        assert signal["value"] is None, name
        assert signal["reason"], name


def test_the_routing_revision_is_read_or_declared_never_minted(admitted, router, monkeypatch):
    router["decision"] = _route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("hello")

    _global_a(monkeypatch, store, episode, ignition)

    revision = _one(episode.episode_id, "global_a.intent_selected").payload["routing_revision"]
    if revision["state"] == "observed":
        assert isinstance(revision["value"], str) and revision["value"]
    else:
        assert revision["state"] == "unavailable" and revision["value"] is None
        assert "pin" in revision["reason"], revision["reason"]


# ── the runtime correction boundary ──────────────────────────────────────────


def test_a_reselected_intent_is_a_new_linked_event_on_the_runtime_path(
    admitted, router, monkeypatch
):
    """Global A running again with a changed route supersedes, and never overwrites or crashes."""
    router["decision"] = _route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("actually, code it")
    _global_a(monkeypatch, store, episode, ignition)
    original = _one(episode.episode_id, "global_a.intent_selected")

    route_context.reset()
    router["decision"] = _route_decision("DOCUMENT_CREATION")
    _global_a(monkeypatch, store, episode, ignition)

    revision = _one(episode.episode_id, "global_a.intent_revised")
    assert revision.payload["supersedes_event_id"] == original.event_id
    assert revision.payload["intent"] == episode.global_["intent_decision"]["intent"]
    assert revision.payload["intent"] != original.payload["intent"], "the decision really changed"
    assert revision.payload["reason"]

    surviving = _one(episode.episode_id, "global_a.intent_selected")
    assert surviving.sealed_sha256() == original.sealed_sha256(), (
        "the superseded decision is preserved exactly, not rewritten"
    )
    assert revision.event_seq > original.event_seq, "and the order of the two is recorded"
    assert revision.arrival_seq == ARRIVAL, "a revision still carries the admitted identity"


def test_an_unchanged_reselection_replays_the_original_occurrence(admitted, router, monkeypatch):
    router["decision"] = _route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("hello again")
    _global_a(monkeypatch, store, episode, ignition)
    original = _one(episode.episode_id, "global_a.intent_selected")

    route_context.reset()
    _global_a(monkeypatch, store, episode, ignition)

    assert _events(episode.episode_id, "global_a.intent_revised") == [], "nothing changed"
    assert _one(episode.episode_id, "global_a.intent_selected").sealed_sha256() == \
        original.sealed_sha256()


# ── Meta A: the action pack, with truthful provenance ────────────────────────


def test_meta_a_records_its_selected_action_pack(admitted, router, monkeypatch):
    router["decision"] = _route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("what is the status?")
    _global_a(monkeypatch, store, episode, ignition)

    _meta_a(monkeypatch, store, episode)

    payload = _one(episode.episode_id, "meta_a.action_pack").payload
    pack = episode.meta["execution_action_pack"]
    assert payload["action"] == pack["action"]
    assert payload["memory_required"] == bool(pack.get("memory_required"))
    assert payload["intent"] == episode.global_["intent_decision"]["intent"]
    assert payload["planning_status"] == "planned"
    assert isinstance(payload["memory_scope"], (list, tuple))


def test_the_action_pack_provenance_is_a_real_digest_not_a_minted_revision(
    admitted, router, monkeypatch
):
    """The pack declares no revision, so none is claimed as observed; its digest is real."""
    from schema.continuity_json import content_hash

    router["decision"] = _route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("status please")
    _global_a(monkeypatch, store, episode, ignition)
    _meta_a(monkeypatch, store, episode)

    payload = _one(episode.episode_id, "meta_a.action_pack").payload
    revision = payload["action_pack_revision"]
    assert revision["state"] == "not_applicable", revision
    assert revision["value"] is None, "an invented revision must never wear `observed`"

    digest = payload["action_pack_content_sha256"]
    assert digest["state"] == "observed"
    assert digest["value"] == content_hash(episode.meta["execution_action_pack"]), (
        "the recorded provenance is a digest OF the producer's pack, recomputable from it"
    )


# ── the bar before the work, observed at AUTO entry ──────────────────────────


def test_the_acceptance_criteria_are_durable_at_auto_entry(admitted, router, monkeypatch):
    """Criteria timing, asserted from inside the stub standing in for AUTO."""
    router["decision"] = _route_decision("CODING_AGENT")
    store, episode, ignition = admitted("build the parser and verify it")
    _global_a(monkeypatch, store, episode, ignition)
    _meta_a(monkeypatch, store, episode)

    seen: dict[str, object] = {}

    def at_auto():
        recorded = read_episode_events(episode.episode_id)
        seen["kinds"] = [event.event_kind for event in recorded]

    _bind_contract(monkeypatch, store, episode, at_auto=at_auto)

    assert "meta_a.acceptance_criteria" in seen["kinds"], (
        "AUTO was entered before the bar was durable — the ordering WO-04 exists to establish"
    )
    assert "auto.action" not in seen["kinds"], "and no action event exists yet"


def test_the_recorded_criteria_cannot_be_moved_afterwards(admitted, router, monkeypatch):
    """The sealed occurrence refuses a retroactive edit of the bar."""
    from memory.cognition_capture import capture_acceptance_criteria
    from schema.continuity_json import EventIdentityConflict

    router["decision"] = _route_decision("CODING_AGENT")
    store, episode, ignition = admitted("build it and verify it")
    _global_a(monkeypatch, store, episode, ignition)
    _meta_a(monkeypatch, store, episode)
    _bind_contract(monkeypatch, store, episode)
    before = _one(episode.episode_id, "meta_a.acceptance_criteria")

    with pytest.raises(EventIdentityConflict):
        capture_acceptance_criteria(episode, objective="something easier",
                                    criteria=["anything passes"], status="contracted")

    assert _one(episode.episode_id, "meta_a.acceptance_criteria").sealed_sha256() == \
        before.sealed_sha256()


def test_blocked_planning_is_preserved_as_an_observation(admitted, router, monkeypatch):
    """Successful capture is not proof of successful planning — on the runtime path."""
    from meta_loop import execution_contract

    router["decision"] = _route_decision("CODING_AGENT")
    store, episode, ignition = admitted("build it")
    _global_a(monkeypatch, store, episode, ignition)
    _meta_a(monkeypatch, store, episode)

    with monkeypatch.context() as patched:
        patched.setattr(execution_contract, "motor_programs_enabled", lambda: False)
        _bind_contract(monkeypatch, store, episode)

    payload = _one(episode.episode_id, "meta_a.acceptance_criteria").payload
    assert payload["status"] == "uncontracted", payload
    assert payload["reason"], "the planning outcome says why"
    assert payload["objective"]["value"] is None, "no objective was invented"
    assert payload["criteria"]["value"] is None
    assert payload["criteria_revision"]["value"] is None


def test_a_contract_that_cannot_form_is_recorded_as_blocked(admitted, router, monkeypatch):
    """A capability whose own terms cannot contract is `blocked`, with the real reason."""
    from meta_loop import execution_contract

    router["decision"] = _route_decision("CODING_AGENT")
    store, episode, ignition = admitted("build it")
    _global_a(monkeypatch, store, episode, ignition)
    _meta_a(monkeypatch, store, episode)

    def refuse(_episode):
        raise execution_contract.ExecutionContractUnavailable("skill 'x' has no objective")

    with monkeypatch.context() as patched:
        patched.setattr(execution_contract, "motor_programs_enabled", lambda: True)
        patched.setattr(execution_contract, "execution_contract_for", refuse)
        _bind_contract(monkeypatch, store, episode)

    payload = _one(episode.episode_id, "meta_a.acceptance_criteria").payload
    assert payload["status"] == "blocked"
    assert "no objective" in payload["reason"]
    assert payload["objective"]["value"] is None


# ── the capture path introduces no inference ─────────────────────────────────


def test_capture_adds_no_model_pass_and_no_second_writer():
    import ast
    import inspect

    from memory import cognition_capture

    source = inspect.getsource(cognition_capture)
    for banned in ("invoke_runtime_generate", "runtime.socket", "write_memory",
                   "sqlite3", "threading.Thread", "open("):
        assert banned not in source, banned
    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("memory")
        for alias in node.names
    }
    assert imported <= {"record_event", "read_episode_events", "capture_context"}, imported


# ── intent lineage: the chain, not just one correction ───────────────────────


def _selection_chain(episode_id):
    """Every intent decision for this episode, in recorded order, selections and revisions."""
    return [
        (event.event_kind, event.payload["intent"], event.event_id,
         event.payload.get("supersedes_event_id", ""))
        for event in read_episode_events(episode_id)
        if event.event_kind in ("global_a.intent_selected", "global_a.intent_revised")
    ]


def test_intent_lineage_across_three_distinct_selections(admitted, router, monkeypatch):
    """A -> B -> C: each correction supersedes the one it actually replaced."""
    router["decision"] = _route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("first")
    _global_a(monkeypatch, store, episode, ignition)

    route_context.reset()
    router["decision"] = _route_decision("DOCUMENT_CREATION")
    _global_a(monkeypatch, store, episode, ignition)

    route_context.reset()
    router["decision"] = _route_decision("BROWSER_AGENT")
    _global_a(monkeypatch, store, episode, ignition)

    rows = _selection_chain(episode.episode_id)
    assert [row[0] for row in rows] == ["global_a.intent_selected",
                                        "global_a.intent_revised",
                                        "global_a.intent_revised"], rows
    first, second, third = rows
    assert second[3] == first[2], "B supersedes A"
    assert third[3] == second[2], "C supersedes B, not A"
    assert len({row[1] for row in rows}) == 3, "three genuinely different intents"


def test_intent_lineage_when_a_correction_returns_to_the_first_choice(admitted, router, monkeypatch):
    """A -> B -> A: returning to the original intent is still a CHANGE from B, and is recorded."""
    router["decision"] = _route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("first")
    _global_a(monkeypatch, store, episode, ignition)

    route_context.reset()
    router["decision"] = _route_decision("DOCUMENT_CREATION")
    _global_a(monkeypatch, store, episode, ignition)

    route_context.reset()
    router["decision"] = _route_decision("FREEFORM_CHAT")
    _global_a(monkeypatch, store, episode, ignition)

    rows = _selection_chain(episode.episode_id)
    assert [row[0] for row in rows] == ["global_a.intent_selected",
                                        "global_a.intent_revised",
                                        "global_a.intent_revised"], rows
    first, second, third = rows
    assert third[1] == first[1], "the third decision is the first intent again"
    assert third[3] == second[2], (
        "and it supersedes B - the decision it actually replaced, not the original selection"
    )
    assert third[2] != first[2], "it is its own occurrence, not a replay of the first"


def test_an_unchanged_retry_after_a_correction_records_nothing_new(admitted, router, monkeypatch):
    """Re-running with the CURRENT intent is a replay, not a fourth decision."""
    router["decision"] = _route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("first")
    _global_a(monkeypatch, store, episode, ignition)
    route_context.reset()
    router["decision"] = _route_decision("DOCUMENT_CREATION")
    _global_a(monkeypatch, store, episode, ignition)
    before = _selection_chain(episode.episode_id)

    route_context.reset()
    _global_a(monkeypatch, store, episode, ignition)

    assert _selection_chain(episode.episode_id) == before, "an unchanged retry adds no event"


# ── the producer's real nested shape ─────────────────────────────────────────


def test_required_capabilities_are_read_from_the_producers_actual_path(admitted, router, monkeypatch):
    """required_capabilities lives under execution_inputs.emergent_pressure.proposed_action.

    That is the path `meta_loop.execution_contract` itself reads. Reading one level shallower
    silently records an empty list for a pack that really declared capabilities.
    """
    router["decision"] = _route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("status")
    _global_a(monkeypatch, store, episode, ignition)

    declared = ["filesystem.read", "shell.run"]
    conditions = ["the suite passes", "no new lint findings"]

    def _planned(ep):
        ep.meta = dict(getattr(ep, "meta", None) or {})
        ep.meta["execution_action_pack"] = {
            "episode_id": ep.episode_id,
            "action": "EXECUTE_MOTOR_PROGRAM",
            "tool": None,
            "execution_inputs": {
                "motor_transport": "USE_TOOL",
                "emergent_pressure": {
                    "proposed_action": {"required_capabilities": declared},
                    "resolution_conditions": conditions,
                },
            },
        }
        return ep.meta["execution_action_pack"]

    with monkeypatch.context() as patched:
        patched.setattr("meta_loop.loop.run_meta_a", _planned)
        _meta_a(monkeypatch, store, episode)

    payload = _one(episode.episode_id, "meta_a.action_pack").payload
    assert list(payload["required_capabilities"]) == declared
    assert list(payload["preconditions"]) == conditions
    assert payload["motor_transport"]["value"] == "USE_TOOL"


def test_a_present_but_unhashable_pack_is_unavailable_not_absent(admitted, router, monkeypatch):
    """Two different facts must not share one answer.

    An ABSENT pack and a pack that is present but cannot be digested are different states: the
    first is not_applicable, the second is unavailable and names the failure. Collapsing them
    would report "no pack" for a pack that really existed.
    """
    router["decision"] = _route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("status")
    _global_a(monkeypatch, store, episode, ignition)

    def _unhashable(ep):
        ep.meta = dict(getattr(ep, "meta", None) or {})
        ep.meta["execution_action_pack"] = {
            "episode_id": ep.episode_id, "action": "RESPOND", "tool": None,
            "execution_inputs": {}, "unrepresentable": {1, 2},
        }
        return ep.meta["execution_action_pack"]

    with monkeypatch.context() as patched:
        patched.setattr("meta_loop.loop.run_meta_a", _unhashable)
        _meta_a(monkeypatch, store, episode)

    payload = _one(episode.episode_id, "meta_a.action_pack").payload
    digest = payload["action_pack_content_sha256"]
    assert digest["state"] == "unavailable", digest
    assert digest["value"] is None
    assert "digest" in digest["reason"] or "represent" in digest["reason"], digest["reason"]
    assert payload["planning_status"] == "planned", "the pack WAS produced"
    assert payload["action"] == "RESPOND"


# ── a failed phase is recorded, and stops the cascade ────────────────────────


def test_a_global_a_failure_is_recorded_and_the_next_phase_does_not_run(
    admitted, router, monkeypatch
):
    """The original failure survives, nothing downstream runs, and the absence is on record."""
    store, episode, ignition = admitted("plan this")
    reached: list[str] = []

    with monkeypatch.context() as patched:
        patched.setattr("global_loop.loop.run_global_a", lambda *_a, **_k: None)
        patched.setattr(chain, "_start_meta_a", lambda *_a, **_k: reached.append("meta_a"))
        with pytest.raises(RuntimeError, match="no IntentDecision"):
            with episode_sram.bind_episode(episode.episode_id, pipeline_id=episode.pipeline_id):
                chain._start_global_a(store, episode, ignition)

    assert reached == [], "Meta A must not run after Global A failed"
    # WO-04A closure F1: a phase that decided nothing is recorded as a FAILURE, with its own kind,
    # so it can never supersede a real decision or become the decision in force.
    failure = _one(episode.episode_id, "global_a.phase_failed")
    assert failure.payload["planning_status"] == "failed", failure.payload
    assert failure.payload["intent"] == "", "no intent was selected, so none is claimed"
    assert failure.payload["reason"], "the failure says what happened"
    assert _events(episode.episode_id, "global_a.intent_selected") == []
    assert _events(episode.episode_id, "global_a.intent_revised") == []
    assert _events(episode.episode_id, "meta_a.action_pack") == []


def test_a_meta_a_failure_is_recorded_and_the_contract_phase_does_not_run(
    admitted, router, monkeypatch
):
    router["decision"] = _route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("plan this")
    _global_a(monkeypatch, store, episode, ignition)
    reached: list[str] = []

    with monkeypatch.context() as patched:
        patched.setattr("meta_loop.loop.run_meta_a", lambda _ep: None)
        patched.setattr(chain, "_bind_execution_contract",
                        lambda *_a, **_k: reached.append("contract"))
        with pytest.raises(RuntimeError, match="no ExecutionActionPack"):
            chain._start_meta_a(store, episode)

    assert reached == [], "the contract phase must not run after Meta A failed"
    # WO-04A closure F1: recorded under the failure kind, not the pack's, so a later failure can
    # never collide with an earlier successful pack's operation token.
    payload = _one(episode.episode_id, "meta_a.phase_failed").payload
    assert payload["planning_status"] == "failed", payload
    assert payload["reason"], "the failure says what happened"
    assert payload["intent"] == ""
    assert _events(episode.episode_id, "meta_a.action_pack") == []
    assert _events(episode.episode_id, "meta_a.acceptance_criteria") == []


# ── WO-04A: both confidence states, because zero is data ─────────────────────


def test_a_decision_that_really_scored_zero_is_recorded_as_observed_zero(
    admitted, router, monkeypatch
):
    """The fix for an invented 0.0 must not become a ban on a real 0.0.

    Global A derives its own confidence from the dominant contributor's weight. A contributor with
    weight 0.0 produces a genuine zero, and a genuine zero is an observation with a value — the
    opposite of "nobody scored this".
    """
    from pressure_loop.contributors import EpisodeIgnition, make_contributor

    router["decision"] = _route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("no pressure at all")
    zero = make_contributor("autonomous", "curiosity", 0.0, {"cycle_id": "bubble-zero"})
    zeroed = EpisodeIgnition(
        episode_id=ignition.episode_id, aggregate_pressure=0.0, opened_by=zero.type,
        dominant_contributor=zero, contributors=(zero,), trajectory_snapshot=None,
        pipeline_id=episode.pipeline_id, work_kind="autonomous",
    )

    _global_a(monkeypatch, store, episode, zeroed)

    confidence = _one(episode.episode_id, "global_a.intent_selected").payload["intent_confidence"]
    assert confidence["state"] == "observed", confidence
    assert confidence["value"] == 0.0, "a real zero is data, not an absence"
    assert confidence["reason"] == "", "an observed value carries no absence reason"


def test_a_raised_global_a_failure_keeps_both_causes_when_the_capture_also_fails(
    admitted, monkeypatch
):
    """The producer error must reach the caller even if recording it fails.

    Replacing it with the capture failure would hide what actually went wrong; dropping the capture
    failure would hide that the evidence is missing. Both stay visible.
    """
    import memory.cognition_capture as capture

    store, episode, ignition = admitted("raise and fail to record")
    producer = OSError("the real producer failure")
    recording = RuntimeError("the journal could not take it")

    with monkeypatch.context() as patched:
        patched.setattr("global_loop.loop.ignition_phase.run",
                        lambda *_a, **_k: (_ for _ in ()).throw(producer))
        patched.setattr(capture, "capture_intent_selected",
                        lambda *_a, **_k: (_ for _ in ()).throw(recording))
        patched.setattr(chain, "_start_meta_a",
                        lambda *_a, **_k: pytest.fail("the next phase must not run"))
        with episode_sram.bind_episode(episode.episode_id, pipeline_id=episode.pipeline_id):
            with pytest.raises(OSError) as caught:
                chain._start_global_a(store, episode, ignition)

    assert caught.value is producer, "the producer failure is what propagates"
    # WO-04A closure F4. This assertion previously required the recording failure on `__cause__`,
    # which is exactly the defect: writing there DESTROYS a producer's own root cause. The
    # recording failure now rides `__context__`, `__cause__` is left to the producer, and neither
    # is reachable from the other in a cycle.
    assert caught.value.__context__ is recording, "the capture failure stays inspectable"
    assert caught.value.__cause__ is None, "this producer had no cause, and none was invented"
    assert recording.__context__ is not caught.value, "no cause/context cycle was introduced"


def test_meta_b_producer_failure_reaches_deepsleep_failure_learning(
    admitted, monkeypatch, tmp_path
):
    """The real phase wrapper's capture event reaches phase_trail and failure learning."""
    from deepsleep_loop.failure.run import consolidate_failures
    from pressure_loop.episode_journal import phase_trail
    from tests.core.deepsleep_window_fixtures import episode_entry, window_input

    import meta_loop.meta_b as meta_b

    store, episode, _ignition = admitted("reflect the completed action")
    monkeypatch.setattr(
        meta_b,
        "run_meta_b_front",
        lambda _episode: (_ for _ in ()).throw(RuntimeError("reflection producer failed")),
    )
    with episode_sram.bind_episode(episode.episode_id, pipeline_id=episode.pipeline_id):
        with pytest.raises(RuntimeError, match="reflection producer failed"):
            chain._start_meta_b_global_b(store, episode)

    episode.cascade_status = "failed"
    trail = phase_trail(episode)
    failed_meta_b = next(row for row in trail if row["phase"] == "meta_b")
    assert failed_meta_b["status"] == "failed"
    assert "reflection producer failed" in failed_meta_b["cause"]
    assert failed_meta_b["evidence"], "the trail identifies its captured failure event"

    entry = episode_entry(
        episode.episode_id, target="compile the report", success=True
    )
    entry["cascade_status"] = "failed"
    entry["phase_trail"] = trail
    path = tmp_path / "failure-patterns.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_failure_path", lambda policy=None: path
    )
    patterns = consolidate_failures(
        window_input(window_id=episode.episode_id, episodes=[entry])
    )

    assert len(patterns) == 1
    assert patterns[0].kind == "response"
    assert patterns[0].context == "compile the report"
    assert patterns[0].last_episode == episode.episode_id
