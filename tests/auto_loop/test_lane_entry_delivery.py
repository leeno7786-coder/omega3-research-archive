"""T9: lane-entry delivery — Omega's injection reaches the next lane before it infers.

Cross-lane cognitive integration spec, sections 2, 4 and 6. BOUNDARY tests: the parent fold
is published by the REAL cognitive transaction (real parser, intake journal, mail store and
successor ledger), and the continuation is driven through the REAL AUTO gate and the REAL
``execute`` inference boundary. Pinned:

1. naming — the successor names exactly the mail its own lane will be handed, by value on
   the transaction record.
2. the claim — by explicit ids; a row bound to another continuation is refused; every named
   id that was not leased is accounted for under the claim's own lock.
3. prerequisites — a ``required`` message that is bound elsewhere, unreadable or
   unrenderable means the dependent action DOES NOT EXECUTE; ``preferred`` mail never blocks.
4. delivery — the block is in the prompt of the lane's FIRST inference; the built prompt is
   recorded against the transaction record BEFORE the mail is acknowledged; a prompt that
   does not carry the block is never sent.
5. effects — redelivery resolves to the same ``effect_id`` and is refused unless the record
   shows the effect has not run.
6. the window lane accepts every descriptive mail purpose.

Scope, stated rather than tested around: Omega never stages a WINDOW successor today, so the
window consumer has no production producer; and mail a fold pins for a lane its successor
does not enter (or pins while deciding DONE) is named by no continuation and remains pending.
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import cognition_chain as chain
from auto_loop import lane_entry as gate
from auto_loop import loop as auto_loop
from auto_loop._auto_state import MemoryEnvelope, PromptPack
from auto_loop.execute.run import execute
from global_loop import intersections as mail
from global_loop.assemble.run import global_b_input_from_episode
from memory import episode_sram, intake_journal
from omega_loop.episode import transaction as tx
from omega_loop.episode.packet import attach_omega_packet
from omega_loop.episode.run import _packet_from_episode
from organ.promptorgan.packs.meta_omega import agent as agent_pack
from pressure_loop import cognition_queue as cq
from pressure_loop import pressure as ledger
from schema.omega_control_decision import OmegaControlDecision

THREAD = "t-build"
FAR = 4102444800.0      # a reservation deadline far in the future


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    redirect_memory_to_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(intake_journal, "journal_root", lambda: tmp_path / "intake")
    monkeypatch.setattr(ledger, "_successor_path", lambda: tmp_path / "successors.json")
    monkeypatch.setattr(ledger, "_RESEED_STAGING", {})
    intake_journal.reset_cache_for_tests()
    episode_sram.reset()
    yield
    episode_sram.reset()
    intake_journal.reset_cache_for_tests()


# ── the parent fold, published for real ───────────────────────────────────────

def _control(decision="PRESSURE", intent="USE_TOOL"):
    assessment = dict.fromkeys(("who", "what", "when", "where", "why", "how",
                                "desired_state", "observed_state"), "fact")
    assessment.update(gap="" if decision == "DONE" else "unfinished", evidence=[])
    pressure = None if decision == "DONE" else dict(
        intent=intent, confidence=0.5, reason="finish", setpoints={}, desired_state="complete",
        observed_state="partial", resolution_conditions=["verified"])
    return dict(assessment=assessment, decision=decision, pressure=pressure, synthesis="summary")


def _parent(episode_id="ep-parent"):
    goal = {"episode_id": episode_id, "intent": "RESPOND", "omega_lane": "agent"}
    pack = {"episode_id": episode_id, "action": "RESPONSE", "tool": "",
            "source_action_goal": goal, "execution_inputs": {}}
    return SimpleNamespace(
        episode_id=episode_id, thread_id=THREAD, opened_by="operator_query", pipeline_id=0,
        global_={"intent_decision": goal,
                 "omega_packet": {"original_objective": "build it", "omega_lane": "agent"}},
        pressure={"contributors": [{"id": "operator_query", "type": "operator_query",
                                    "detail": {"cycle_id": "cycle-1", "job_cycle_id": "cycle-owner",
                                               "message": "build it", "arrival_seq": 7}}]},
        meta={"execution_action_pack": pack},
        auto={"episode_id": episode_id, "execution_pack": pack, "source_action_goal": goal,
              "outputs": {"agent_run": {"status": "batch_ran", "steps": 1, "job_id": "job-a"}}})


def _injection(requirement="required", lane="agent", objective="Switch storage to SQLite"):
    return {"target_lane": lane, "objective": objective, "constraints": ["keep the API"],
            "evidence_refs": ["operator note"], "requirement": requirement}


def _publish(injections, *, episode_id="ep-parent", intent="USE_TOOL"):
    """Fold the parent with the REAL parser and publish its REAL transaction."""
    episode = _parent(episode_id)
    attach_omega_packet(episode, global_b_input_from_episode(episode, {}), {})
    packet = _packet_from_episode(episode)
    payload = {"control": _control(intent=intent),
               "decisions": [{"statement": "Store todos in SQLite", "rationale": "asked"}],
               "lane_injections": list(injections)}
    result = chain._effective_fold_result(episode, agent_pack.parse(payload, packet))
    global_ = dict(episode.global_)
    global_["omega_result"] = result.to_dict()
    episode.global_ = global_

    def fold_on_the_parents_own_pipeline():
        # The parent folds on ITS pipeline thread, as in production: opening its SRAM bucket
        # binds that thread, and the continuation later runs on another one. Folding on the
        # test thread left it bound to the parent while AUTO ran the continuation, a state the
        # cascade never produces (and one event capture rightly refuses).
        episode_sram.open_bucket(episode.episode_id, pipeline_id=0)
        with episode_sram.bind_episode(episode.episode_id, pipeline_id=0):
            chain._ingest_fold_knowledge(episode, result, global_)
        control = OmegaControlDecision.from_dict(global_["omega_result"]["control_decision"])
        return tx.publish_cognitive_transaction(episode, control)

    successor = _on_its_own_thread(fold_on_the_parents_own_pipeline)
    return episode, successor


def _on_its_own_thread(work):
    import threading

    box: dict = {}

    def run():
        try:
            box["value"] = work()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread
            box["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _continuation(successor, *, episode_id="ep-next", action="RESPONSE", inputs=None):
    """The episode the staged successor opens, and its execution pack."""
    episode = SimpleNamespace(
        episode_id=episode_id, thread_id=THREAD, opened_by="omega_successor", pipeline_id=2,
        pressure={"contributors": [{"id": "omega_successor", "weight": 1.0,
                                    "detail": json.loads(json.dumps(successor.metadata))}]},
        global_={}, meta={}, auto={})
    pack = {"episode_id": episode_id, "action": action,
            "execution_inputs": {"job_id": "job-a", **(inputs or {})}}
    return episode, pack


def _lane_mail():
    return {row["mail_id"]: row for row in mail.all_mail() if row["delivery_point"] == "lane_entry"}


def _deliveries(parent):
    return ledger.transaction(tx.transaction_fold_id(parent))["deliveries"]


def _restart():
    mail.reset_cache_for_tests()
    intake_journal.reset_cache_for_tests()
    episode_sram.reset()
    cq._reset_cache_for_tests()


# ── driving the REAL execute ──────────────────────────────────────────────────

class _Runtime:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"ok": True, "text": "done."}

    def text(self, index=0):
        call = self.calls[index]
        return gate.prompt_text(call.get("system"), call.get("prompt"), call.get("messages"))


def _execute(entry, runtime, *, inputs=None, user="carry on with the build",
             prompt_type="RESPONSE_QUESTION", blocks=None):
    prompt_pack = PromptPack(prompt_type=prompt_type, system_prompt="answer",
                             developer_prompt=None, user_prompt=user,
                             context_blocks=dict(blocks or {}), response_mode=None,
                             tool_call_spec=None, image_spec=None)
    envelope = MemoryEnvelope(memory_required=False, memory_status="skipped", memory_reason=None,
                              memory_scope=(), items=(), error=None)
    with patch("runtime.socket.invoke_runtime_generate", runtime):
        return execute(prompt_pack, "TEXT_FAST",
                       {"action": "RESPONSE", "execution_inputs": dict(inputs or {})},
                       envelope, "ep-next", opened_by="omega_successor", thread_id=THREAD,
                       lane_entry=entry)


# ── 1. naming ─────────────────────────────────────────────────────────────────

def test_the_successor_names_the_mail_its_continuation_must_be_handed():
    parent, successor = _publish([_injection()])

    named = successor.metadata["lane_entry"]
    [pinned] = _lane_mail().values()
    record = ledger.transaction(tx.transaction_fold_id(parent))
    assert named == {"fold_id": record["fold_id"], "continuation_key": record["continuation_key"],
                     "lane": "agent",
                     "mail": [{"mail_id": pinned["mail_id"], "effect_id": pinned["effect_id"],
                               "requirement": "required"}]}
    assert pinned["continuation_key"] == named["continuation_key"] != ""
    # By value on the record, so a B2 recovery stages the SAME naming.
    assert record["successor"]["metadata"]["lane_entry"] == named


def test_only_mail_for_the_lane_the_successor_enters_is_pinned_and_named():
    parent, successor = _publish([_injection(lane="browser"), _injection(lane="agent")])

    [pinned] = _lane_mail().values()
    assert pinned["target_lane"] == "agent"
    assert [row["mail_id"] for row in successor.metadata["lane_entry"]["mail"]] == [pinned["mail_id"]]
    [withheld] = ledger.transaction(tx.transaction_fold_id(parent))["withheld"]
    assert (withheld["disposition"], withheld["target_lane"]) == ("no_consumer", "browser")


def test_a_successor_with_no_mail_for_its_lane_names_none():
    _parent_episode, successor = _publish([_injection(lane="browser")])

    assert "lane_entry" not in successor.metadata


def test_naming_leaves_successor_identity_valid():
    from pressure_loop.omega_successor import validate_successor

    _parent_episode, successor = _publish([_injection()])

    validate_successor(successor.metadata)
    assert ledger.staged_signals() == (successor,)


# ── 2. the claim ──────────────────────────────────────────────────────────────

def test_the_claim_takes_exactly_the_named_ids():
    _publish([_injection()])
    bystander = mail.pin_mail(thread_id=THREAD, delivery_point="lane_entry",
                              purpose="omega_objective", requirement="required",
                              source_actor="omega", source_episode_id="ep-other",
                              payload={"objective": "someone else's"}, target_lane="agent",
                              continuation_key="other")
    [named_id] = [one for one in _lane_mail() if one != bystander["mail_id"]]
    key = _lane_mail()[named_id]["continuation_key"]

    outcome = mail.claim_lane_entry(lane="agent", thread_id=THREAD, episode_id="ep-next",
                                    mail_ids=[named_id], continuation_key=key, lease_s=60,
                                    job_id="job-a")

    assert [row["mail_id"] for row in outcome["claimed"]] == [named_id]
    assert outcome["undelivered"] == []
    assert _lane_mail()[bystander["mail_id"]]["status"] == "ready"


def test_a_row_bound_to_another_continuation_is_refused_not_leased():
    _publish([_injection()])
    [row] = _lane_mail().values()

    outcome = mail.claim_lane_entry(lane="agent", thread_id=THREAD, episode_id="ep-next",
                                    mail_ids=[row["mail_id"]], continuation_key="not-this-one",
                                    lease_s=60, job_id="job-a")

    assert outcome["claimed"] == []
    assert outcome["undelivered"] == [{"mail_id": row["mail_id"], "requirement": "required",
                                       "purpose": "omega_objective",
                                       "reason": "it is bound to another continuation"}]
    assert _lane_mail()[row["mail_id"]]["status"] == "ready"


@pytest.mark.parametrize("how, reason", [
    ("unknown", "it is unknown to the store"),
    ("leased", "it is leased"),
    ("other_lane", "it is not addressed to this consumer"),
])
def test_every_named_id_that_was_not_leased_is_accounted_for(how, reason):
    _publish([_injection()])
    [row] = _lane_mail().values()
    mail_id, lane = row["mail_id"], "agent"
    if how == "unknown":
        mail_id = "never-pinned"
    elif how == "leased":
        mail.claim_lane_entry(lane="agent", thread_id=THREAD, episode_id="ep-rival",
                              mail_ids=[mail_id], continuation_key=row["continuation_key"],
                              lease_s=600, job_id="job-a")
    elif how == "other_lane":
        lane = "chat"

    outcome = mail.claim_lane_entry(lane=lane, thread_id=THREAD, episode_id="ep-next",
                                    mail_ids=[mail_id], continuation_key=row["continuation_key"],
                                    lease_s=60, job_id="job-a")

    assert outcome["claimed"] == []
    assert [(one["mail_id"], one["reason"]) for one in outcome["undelivered"]] == [(mail_id, reason)]


# ── 3. prerequisites ──────────────────────────────────────────────────────────

def test_a_continuation_without_lane_entry_mail_opens_nothing():
    _parent_episode, successor = _publish([])
    episode, pack = _continuation(successor)

    assert gate.open_lane_entry(episode, pack) is None


@pytest.mark.parametrize("opened_by, row_id", [
    ("operator_query", "omega_successor"),      # a successor row on an episode it did not open
    ("omega_successor", "operator_query"),      # the opener's name on somebody else's row
])
def test_only_the_successor_that_opened_the_episode_can_name_lane_entry_mail(opened_by, row_id):
    """Any other contributor's detail can hold operator-supplied fields: never host ids."""
    _parent_episode, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    episode.opened_by = opened_by
    episode.pressure = {"contributors": [{"id": row_id, "detail": dict(successor.metadata)}]}

    assert gate.open_lane_entry(episode, pack) is None
    assert all(row["status"] == "ready" for row in _lane_mail().values())


def test_a_rejected_required_injection_refuses_the_dependent_action():
    _parent_episode, successor = _publish([_injection("required")])
    episode, pack = _continuation(successor)
    [row] = _lane_mail().values()
    state = mail._candidate()
    state["mail"][row["mail_id"]]["status"] = "rejected"
    mail._publish(state)

    with pytest.raises(gate.LaneEntryRefused) as refusal:
        gate.open_lane_entry(episode, pack)

    assert refusal.value.kind == "lane_entry_prerequisite_missing"
    assert "rejected" in refusal.value.reason


def test_old_preferred_mail_stays_deliverable(monkeypatch):
    parent, successor = _publish([_injection("preferred")])
    episode, pack = _continuation(successor)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 365 * 24 * 3600)

    entry = gate.open_lane_entry(episode, pack)

    assert len(entry.rows) == 1, "mail age does not remove its delivery eligibility"
    assert "Switch storage to SQLite" in entry.block
    assert [row["state"] for row in _deliveries(parent).values()] == ["started"]
    monkeypatch.setattr(time, "time", real)
    entry.enter()
    assert entry.state == "claimed", "the prompt becomes delivered at the inference boundary"
    assert [row["state"] for row in _deliveries(parent).values()] == ["started"]


def test_a_missing_required_message_releases_what_was_claimed_beside_it(monkeypatch):
    _parent_episode, successor = _publish(
        [_injection("required", objective="first"), _injection("required", objective="second")])
    episode, pack = _continuation(successor)
    second = successor.metadata["lane_entry"]["mail"][1]["mail_id"]
    state = mail._candidate()
    state["mail"][second]["status"] = "rejected"
    mail._publish(state)

    with pytest.raises(gate.LaneEntryRefused) as refusal:
        gate.open_lane_entry(episode, pack)

    assert "rejected" in refusal.value.reason
    first = successor.metadata["lane_entry"]["mail"][0]["mail_id"]
    assert _lane_mail()[first]["status"] == "ready", "never held by a continuation that will not run"
    assert {row["state"] for row in _deliveries(_parent_episode).values()} == {"abandoned"}


def test_an_unreadable_store_refuses_the_dependent_action(monkeypatch):
    _parent_episode, successor = _publish([_injection("required")])
    episode, pack = _continuation(successor)
    mail.mail_path().write_text("{ not json", encoding="utf-8")
    mail.reset_cache_for_tests()

    with pytest.raises(gate.LaneEntryRefused) as refusal:
        gate.open_lane_entry(episode, pack)

    assert refusal.value.kind == "lane_entry_prerequisite_missing"


def test_required_mail_over_the_budget_refuses_rather_than_being_cut(monkeypatch):
    _parent_episode, successor = _publish([_injection("required")])
    episode, pack = _continuation(successor)
    monkeypatch.setattr(gate, "PAYLOAD_BUDGET", 10)

    with pytest.raises(gate.LaneEntryRefused) as refusal:
        gate.open_lane_entry(episode, pack)

    assert "budget" in refusal.value.reason
    assert all(row["status"] == "ready" for row in _lane_mail().values())


@pytest.mark.parametrize("requirement, refused", [("required", True), ("preferred", False)])
def test_a_step_whose_prompt_cannot_carry_the_mail(requirement, refused):
    parent, successor = _publish([_injection(requirement)])
    episode, pack = _continuation(successor, action="USE_TOOL")

    if refused:
        with pytest.raises(gate.LaneEntryRefused):
            gate.open_lane_entry(episode, pack)
        assert [row["state"] for row in _deliveries(parent).values()] == ["abandoned"]
    else:
        entry = gate.open_lane_entry(episode, pack)
        assert entry.rows == [] and entry.state == "reserved", "runs, as this continuation's effect"
    assert all(row["status"] == "ready" for row in _lane_mail().values()), "never claimed"


def test_auto_does_not_execute_a_refused_continuation(monkeypatch):
    """The REAL AUTO movement: a refusal is a bounded failure and ``execute`` never runs."""
    _parent_episode, successor = _publish([_injection("required")])
    episode, pack = _continuation(successor)
    [row] = _lane_mail().values()
    state = mail._candidate()
    state["mail"][row["mail_id"]]["status"] = "rejected"
    mail._publish(state)
    goal = {"episode_id": "ep-next", "intent": "RESPOND", "omega_lane": "agent"}
    pack.update(source_action_goal=goal, tool="")
    episode.meta = {"execution_action_pack": pack}
    episode.global_ = {"intent_decision": goal}
    # Nothing else may be what stops it: validation passes, and execute is a tripwire.
    monkeypatch.setattr(auto_loop.validate_phase, "validate_for_execution",
                        lambda _pack: (True, None, None))
    monkeypatch.setattr(auto_loop.execute_phase, "execute",
                        lambda *a, **k: pytest.fail("a refused continuation executed"))

    outcome = auto_loop.run_auto(episode)

    assert episode.auto["failure_kind"] == "lane_entry_prerequisite_missing"
    assert episode.auto["outputs"] == {} and outcome is not None


# ── 4. delivery ───────────────────────────────────────────────────────────────

def test_the_injection_is_in_the_prompt_of_the_lanes_first_inference():
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)
    runtime = _Runtime()

    _execute(entry, runtime)
    entry.close(ran=True)

    first = runtime.text(0)
    assert entry.block in first
    for shown in ("Switch storage to SQLite", "keep the API", "Store todos in SQLite", "[required]"):
        assert shown in first
    assert _lane_mail() == {}, "acknowledged: the lifecycle ended"
    [delivery] = _deliveries(parent).values()
    assert (delivery["state"], delivery["episode_id"]) == ("performed", "ep-next")
    assert delivery["prompt_chars"] == len(first) and len(delivery["prompt_digest"]) == 64


def test_the_terminal_agent_prompt_carries_the_injection():
    _parent_episode, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)
    runtime = _Runtime()

    _execute(entry, runtime, inputs={"operator_route": "agent"})

    assert "TERMINAL AGENT TASK" in runtime.text(0) and entry.block in runtime.text(0)


def test_the_mail_is_acknowledged_only_after_the_built_prompt_is_recorded(monkeypatch):
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)
    order = []
    real_advance, real_ack = ledger.advance_lane_entry, mail.acknowledge_mail
    monkeypatch.setattr(ledger, "advance_lane_entry",
                        lambda *a, **k: (order.append(k["to"]), real_advance(*a, **k))[1])

    def acknowledge(*args, **kwargs):
        [row] = _deliveries(parent).values()
        order.append(f"acknowledge-while-{row['state']}")
        return real_ack(*args, **kwargs)

    monkeypatch.setattr(mail, "acknowledge_mail", acknowledge)
    assert [row["status"] for row in _lane_mail().values()] == ["leased"], "claimed, not consumed"

    entry.deliver("SYSTEM\n" + entry.block)

    assert order == ["executing", "acknowledge-while-executing"]


def test_a_failed_record_acknowledges_nothing(monkeypatch):
    _parent_episode, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)
    monkeypatch.setattr(ledger, "advance_lane_entry", lambda *a, **k: False)

    with pytest.raises(gate.LaneEntryRefused):
        entry.deliver(entry.block)

    assert [row["status"] for row in _lane_mail().values()] == ["ready"]


def test_a_prompt_that_does_not_carry_the_block_is_never_sent(monkeypatch):
    """A lane pack that drops its context blocks: the host catches it AT the inference
    boundary, whatever the lane does with the refusal."""
    from dataclasses import replace

    import organ.promptorgan as promptorgan

    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)
    real_phrase = promptorgan.phrase
    monkeypatch.setattr(promptorgan, "phrase",
                        lambda context: replace(real_phrase(context), user="a prompt without it"))
    runtime = _Runtime()

    try:
        _execute(entry, runtime)
    except gate.LaneEntryRefused:
        pass

    assert runtime.calls == [], "no inference after a refusal"
    assert entry.refusal is not None and entry.refusal.kind == "lane_entry_not_rendered"
    assert [row["status"] for row in _lane_mail().values()] == ["ready"]
    assert [row["state"] for row in _deliveries(parent).values()] == ["abandoned"]
    with pytest.raises(gate.LaneEntryRefused):
        entry.deliver(entry.block)      # refused for good: never a later pass either


@pytest.mark.parametrize("requirement, ran, mail_status, delivery", [
    ("required", True, "ready", "abandoned"),     # never shown: nothing acknowledged, nothing run
    ("required", False, "ready", "abandoned"),
    ("preferred", True, "ready", "abandoned"),    # no tool or inference precedes delivery
    ("preferred", False, "ready", "abandoned"),
])
def test_mail_the_lane_never_showed_is_never_acknowledged(requirement, ran, mail_status, delivery):
    parent, successor = _publish([_injection(requirement)])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)

    entry.close(ran=ran)

    assert [row["status"] for row in _lane_mail().values()] == [mail_status]
    assert [row["state"] for row in _deliveries(parent).values()] == [delivery]


def test_the_block_is_never_truncated():
    long = "x" * 3000
    rows = [{"requirement": "required",
             "payload": {"objective": long, "constraints": [long], "decisions": [long],
                         "user_constraints": [long], "evidence_refs": [long]}}]

    assert gate.render_lane_entry_block(rows).count(long) == 5


def test_the_browser_runs_first_inference_carries_the_direction_uncut():
    """The REAL browser loop: its brief is the lane's prompt, and nothing in it is cut."""
    from browser_agent.agent_loop import BrowserAgentRun
    from tests.browser_agent.test_agent_loop import FakeController, Seat, call

    direction = gate.BLOCK_HEADER + "\n1. [required] Objective: " + "y" * 6000
    seat = Seat(call("done", summary="done", evidence="seen"))

    BrowserAgentRun(seat, FakeController(), objective="find pricing", direction=direction).run()

    assert direction in gate.prompt_text(messages=seat.requests[0]["messages"])


def test_auto_hands_the_browser_run_its_direction(monkeypatch):
    import importlib

    execute_run = importlib.import_module("auto_loop.execute.run")

    seen = {}
    monkeypatch.setattr(execute_run, "_execute_browser_run",
                        lambda generate, pack, attachment, **kwargs: seen.update(kwargs) or
                        {"outputs": {}, "failure_kind": None})
    entry = SimpleNamespace(block="THE BLOCK", deliver=lambda prompt: None,
                            require_delivered=lambda: None)
    envelope = MemoryEnvelope(memory_required=False, memory_status="skipped", memory_reason=None,
                              memory_scope=(), items=(), error=None)

    execute(None, None, {"action": "BROWSER_ACTION", "execution_inputs": {}}, envelope, "ep-next",
            lane_entry=entry)

    assert seen["direction"] == "THE BLOCK"
    assert seen["before_action"] is entry.require_delivered, "the gate no browser action may bypass"


# ── 5. effects ────────────────────────────────────────────────────────────────

def test_a_delivery_taken_by_a_dead_process_before_it_inferred_is_redelivered(monkeypatch):
    """B3: claimed, then the process died. The record shows nothing ran."""
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor, episode_id="ep-died")
    monkeypatch.setattr(gate, "LEASE_S", 0.05)
    gate.open_lane_entry(episode, pack)
    [effect_before] = [row["effect_id"] for row in _deliveries(parent).values()]
    _restart()
    time.sleep(0.1)

    retry, retry_pack = _continuation(successor, episode_id="ep-retry")
    entry = gate.open_lane_entry(retry, retry_pack)

    [delivery] = _deliveries(parent).values()
    assert entry is not None and delivery["episode_id"] == "ep-retry"
    assert delivery["effect_id"] == effect_before == entry.rows[0]["effect_id"], "the same effect"


def test_a_delivery_interrupted_after_its_prompt_was_recorded_is_not_repeated(monkeypatch):
    """The process died between recording the prompt and acknowledging: the mail comes back,
    and nothing on record shows the effect did not run."""
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor, episode_id="ep-died")
    monkeypatch.setattr(gate, "LEASE_S", 0.05)
    entry = gate.open_lane_entry(episode, pack)
    assert entry._advance("executing", prompt_digest="d" * 64, prompt_chars=1)
    _restart()
    time.sleep(0.1)

    retry, retry_pack = _continuation(successor, episode_id="ep-retry")
    with pytest.raises(gate.LaneEntryRefused) as refusal:
        gate.open_lane_entry(retry, retry_pack)

    assert refusal.value.kind == "lane_entry_effect_unproven" and "interrupted" in refusal.value.reason
    assert [row["episode_id"] for row in _deliveries(parent).values()] == ["ep-died"]
    # The record is consulted FIRST: a refused replay never so much as claims the mail.
    assert all(row["claimed_by"] != "ep-retry" for row in _lane_mail().values())


def test_a_performed_effect_is_never_performed_again():
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor, episode_id="ep-first")
    first = gate.open_lane_entry(episode, pack)
    assert first._advance("executing", prompt_digest="d" * 64, prompt_chars=1)
    assert first._advance("performed")
    mail.release_mail(first.rows, episode_id="ep-first")     # the acknowledgement was lost

    retry, retry_pack = _continuation(successor, episode_id="ep-retry")
    with pytest.raises(gate.LaneEntryRefused) as refusal:
        gate.open_lane_entry(retry, retry_pack)

    assert refusal.value.kind == "lane_entry_effect_already_performed"
    assert _lane_mail() == {}, "consumed: the record is the durable outcome"


def test_a_lapsed_lease_cannot_let_two_episodes_perform_one_effect(monkeypatch):
    """The lease lapses while the first episode is still before its inference. Its
    reservation lapsed with it: the record shows nothing ran, so the next episode takes the
    delivery — and the first is refused at its own inference. Exactly one performs."""
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor, episode_id="ep-slow")
    monkeypatch.setattr(gate, "LEASE_S", 0.05)
    slow = gate.open_lane_entry(episode, pack)
    time.sleep(0.1)
    monkeypatch.setattr(gate, "LEASE_S", 600.0)

    rival, rival_pack = _continuation(successor, episode_id="ep-rival")
    taken = gate.open_lane_entry(rival, rival_pack)
    assert taken is not None and taken.rows[0]["effect_id"] == slow.rows[0]["effect_id"]

    runtime = _Runtime()
    moves = []
    real_advance = ledger.advance_lane_entry
    monkeypatch.setattr(ledger, "advance_lane_entry",
                        lambda *a, **k: (moves.append((k["episode_id"], k["to"])), real_advance(*a, **k))[1])
    with pytest.raises(gate.LaneEntryRefused) as late:
        _execute(slow, runtime)

    assert late.value.kind == "lane_entry_ownership_lost" and runtime.calls == []
    assert moves == [("ep-slow", "abandoned")], "no prompt is recorded for mail it no longer owns"
    [delivery] = _deliveries(parent).values()
    assert (delivery["episode_id"], delivery["state"]) == ("ep-rival", "started"), "untouched by the loser"

    _execute(taken, runtime)
    taken.close(ran=True)
    assert len(runtime.calls) == 1
    assert [row["state"] for row in _deliveries(parent).values()] == ["performed"]


def test_a_reservation_holds_only_while_its_lease_could_still_deliver():
    parent, _successor = _publish([_injection()])
    fold_id = tx.transaction_fold_id(parent)
    [real] = ledger.transaction(fold_id)["outgoing"]
    begin = lambda episode, until: ledger.begin_lane_entry(  # noqa: E731
        fold_id, episode_id=episode, process_epoch="p", at="now", effects=[real], reserved_until=until)

    assert begin("ep-a", FAR) == "started"
    assert begin("ep-b", FAR) == "held_elsewhere", "a live holder whose lease has not lapsed"
    assert begin("ep-a", time.time() - 1) == "started", "the holder itself may renew"
    assert begin("ep-b", FAR) == "started", "the reservation lapsed with its lease"
    assert ledger.advance_lane_entry(fold_id, episode_id="ep-b", process_epoch="p", at="now",
                                     effect_ids=[real["effect_id"]], to="executing")
    assert begin("ep-c", FAR) == "held_elsewhere", "past its first inference it is no reservation"


def test_a_failure_on_the_way_to_the_lane_hands_the_mail_back(monkeypatch):
    """Recall, prompt-pack and routing run between the gate and the lane, and can raise."""
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    goal = {"episode_id": "ep-next", "intent": "RESPOND", "omega_lane": "agent"}
    pack.update(source_action_goal=goal, tool="")
    episode.meta = {"execution_action_pack": pack}
    episode.global_ = {"intent_decision": goal}

    def broken(*_args, **_kwargs):
        raise RuntimeError("prompt pack failed")

    monkeypatch.setattr(auto_loop.validate_phase, "validate_for_execution",
                        lambda _pack: (True, None, None))
    monkeypatch.setattr(auto_loop.promptpack_phase, "build_prompt_pack", broken)

    with pytest.raises(RuntimeError, match="prompt pack failed"):
        auto_loop.run_auto(episode)

    assert [row["status"] for row in _lane_mail().values()] == ["ready"]
    assert [row["state"] for row in _deliveries(parent).values()] == ["abandoned"]


def test_a_delivery_is_refused_when_its_folds_record_is_gone():
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    data = ledger._read_successors()
    data["transactions"].clear()
    ledger._write_successors(data)

    with pytest.raises(gate.LaneEntryRefused) as refusal:
        gate.open_lane_entry(episode, pack)

    assert "unknown_record" in refusal.value.reason
    assert [row["status"] for row in _lane_mail().values()] == ["ready"]


def test_only_an_effect_the_fold_published_can_be_delivered():
    parent, _successor = _publish([_injection()])
    fold_id = tx.transaction_fold_id(parent)
    [real] = ledger.transaction(fold_id)["outgoing"]

    for forged in ({"mail_id": real["mail_id"], "effect_id": "not-the-folds"},
                   {"mail_id": "not-the-folds", "effect_id": real["effect_id"]}):
        assert ledger.begin_lane_entry(fold_id, episode_id="ep-next", process_epoch="p", at="now",
                                       effects=[forged], reserved_until=FAR) == "unknown_effect"
    assert _deliveries(parent) == {}


def test_only_the_owner_moves_a_delivery_and_only_forward():
    parent, _successor = _publish([_injection()])
    fold_id = tx.transaction_fold_id(parent)
    [real] = ledger.transaction(fold_id)["outgoing"]
    mine = dict(episode_id="ep-next", process_epoch="p", effect_ids=[real["effect_id"]], at="now")
    assert ledger.begin_lane_entry(fold_id, episode_id="ep-next", process_epoch="p", at="now",
                                   effects=[real], reserved_until=FAR) == "started"

    assert not ledger.advance_lane_entry(fold_id, **{**mine, "episode_id": "ep-other"}, to="executing")
    assert not ledger.advance_lane_entry(fold_id, **{**mine, "process_epoch": "q"}, to="executing")
    assert not ledger.advance_lane_entry(fold_id, **mine, to="performed"), "never skipping the prompt"
    assert ledger.advance_lane_entry(fold_id, **mine, to="executing", prompt_digest="d", prompt_chars=3)
    assert ledger.advance_lane_entry(fold_id, **mine, to="performed")
    assert not ledger.advance_lane_entry(fold_id, **mine, to="abandoned"), "a run is never un-run"
    [row] = _deliveries(parent).values()
    assert (row["state"], row["prompt_digest"], row["prompt_chars"]) == ("performed", "d", 3)


def test_auto_closes_the_lane_entry_once_the_lane_has_run(monkeypatch):
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)

    def lane(*_args, lane_entry, **_kwargs):
        assert lane_entry.entered, "marked BEFORE control passes to the lane"
        lane_entry.deliver("PROMPT " + lane_entry.block)
        return {"outputs": {"response_text": "ok"}, "failure_kind": None}

    monkeypatch.setattr(auto_loop.execute_phase, "execute", lane)

    result = auto_loop._execute_with_lane_entry(entry)(None, None, {}, None)

    assert result["failure_kind"] is None and result["outputs"] == {"response_text": "ok"}
    assert [row["state"] for row in _deliveries(parent).values()] == ["performed"]


def test_a_refusal_the_lane_swallowed_is_still_autos_failure(monkeypatch):
    _parent_episode, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)

    def lane(*_args, lane_entry, **_kwargs):
        try:
            lane_entry.deliver("a prompt without the block")
        except Exception:  # noqa: BLE001 - a lane treating it as one more failed inference
            pass
        return {"outputs": {"response_text": "carried on anyway"}, "failure_kind": None}

    monkeypatch.setattr(auto_loop.execute_phase, "execute", lane)

    result = auto_loop._execute_with_lane_entry(entry)(None, None, {}, "ENVELOPE")

    assert result["failure_kind"] == "lane_entry_not_rendered"
    assert result["outputs"] == {}, "nothing produced past a refusal is an outcome"


def test_a_continuation_that_fails_validation_hands_its_mail_back(monkeypatch):
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    goal = {"episode_id": "ep-next", "intent": "RESPOND", "omega_lane": "agent"}
    pack.update(source_action_goal=goal, tool="")
    episode.meta = {"execution_action_pack": pack}
    episode.global_ = {"intent_decision": goal}
    monkeypatch.setattr(auto_loop.validate_phase, "validate_for_execution",
                        lambda _pack: (False, "invalid_pack", "not executable"))

    auto_loop.run_auto(episode)

    assert episode.auto["failure_kind"] == "invalid_pack"
    assert [row["status"] for row in _lane_mail().values()] == ["ready"]
    assert [row["state"] for row in _deliveries(parent).values()] == ["abandoned"]


def test_a_ledger_written_before_deliveries_existed_still_loads():
    parent, _successor = _publish([_injection()])
    data = json.loads(ledger._successor_path().read_text(encoding="utf-8"))
    for record in data["transactions"].values():
        del record["deliveries"]
    ledger._successor_path().write_text(json.dumps(data), encoding="utf-8")

    assert ledger.transaction(tx.transaction_fold_id(parent))["deliveries"] == {}


# ── 6. the window lane ────────────────────────────────────────────────────────

@pytest.mark.parametrize("purpose", ["operator_instruction", "stop_job"])
def test_mail_purpose_is_descriptive_for_the_window_lane(purpose):
    row = mail.pin_mail(thread_id=THREAD, delivery_point="lane_entry", purpose=purpose,
                        requirement="required", source_actor="operator", source_episode_id="ep",
                        payload={"objective": "review the active window"}, target_lane="window",
                        continuation_key="window-continuation")

    outcome = mail.claim_lane_entry(lane="window", thread_id=THREAD, episode_id="ep-window",
                                    mail_ids=[row["mail_id"]],
                                    continuation_key="window-continuation", lease_s=60)

    assert [claimed["mail_id"] for claimed in outcome["claimed"]] == [row["mail_id"]]
    assert outcome["undelivered"] == []


@pytest.mark.parametrize("purpose", ["omega_objective", "lane_evidence",
                                     "operator_instruction", "stop_job"])
def test_the_window_claim_does_not_filter_by_mail_purpose(purpose):
    actor = "operator" if purpose in {"operator_instruction", "stop_job"} else "omega"
    row = mail.pin_mail(thread_id=THREAD, delivery_point="lane_entry", purpose=purpose,
                        requirement="required", source_actor=actor, source_episode_id="ep",
                        payload={"objective": "look at the stalls"}, continuation_key="k")

    outcome = mail.claim_lane_entry(lane="window", thread_id=THREAD, episode_id="ep-window",
                                    mail_ids=[row["mail_id"]], continuation_key="k", lease_s=60)

    assert [claimed["mail_id"] for claimed in outcome["claimed"]] == [row["mail_id"]]
    assert outcome["undelivered"] == []


def test_the_reflection_pack_renders_the_window_lanes_block():
    from organ.promptorgan.packs.meta_omega import window as window_pack

    packet = SimpleNamespace(window_context={"window_id": "w", "episodes": []}, foldback_packet={},
                             cross_lane_mail=(), lane_entry_block=gate.BLOCK_HEADER + "\n1. x")

    _system, user = window_pack.build(packet)

    assert packet.lane_entry_block in user
    packet.lane_entry_block = ""
    assert gate.BLOCK_HEADER not in window_pack.build(packet)[1]


def test_a_window_episodes_mail_is_delivered_at_omegas_window_fold(monkeypatch):
    """AUTO infers nothing for a window: the reflection IS the lane's first inference."""
    from dataclasses import replace

    from omega_loop.episode import run as omega_run
    from tests.omega_loop.test_foldback_prompt_dispatch import packet_fixture
    from tests.omega_loop.test_omega_prompt_contracts import done_control
    from tests.omega_loop.test_window_reflection import REFLECTION

    parent, _successor = _publish([_injection()])
    row = mail.pin_mail(thread_id=THREAD, delivery_point="lane_entry", purpose="omega_objective",
                        requirement="required", source_actor="omega", source_episode_id="ep-parent",
                        payload={"objective": "watch for stalled builds"}, target_lane="window",
                        continuation_key="k")
    outcome = mail.claim_lane_entry(lane="window", thread_id=THREAD, episode_id="ep-window",
                                    mail_ids=[row["mail_id"]], continuation_key="k", lease_s=60)
    # Delivery is recorded against the fold that published the effect.
    data = ledger._read_successors()
    record = data["transactions"][tx.transaction_fold_id(parent)]
    record["outgoing"].append({"mail_id": row["mail_id"], "effect_id": row["effect_id"], "pin": {}})
    ledger._write_successors(data)
    assert ledger.begin_lane_entry(record["fold_id"], episode_id="ep-window", at="now",
                                   process_epoch=mail.process_epoch(), reserved_until=FAR,
                                   effects=outcome["claimed"]) == "started"
    entry = gate.LaneEntry(fold_id=record["fold_id"], episode_id="ep-window",
                           rows=outcome["claimed"], effect_ids=[row["effect_id"]])
    packet = replace(packet_fixture("window"), episode_id="ep-window")
    monkeypatch.setattr(omega_run, "_with_current_identity", lambda value: value)
    episode = SimpleNamespace(episode_id="ep-window", global_={"omega_packet": packet.to_dict()},
                              temporal={}, _lane_entry=entry)
    prompts = []

    def reason(system, user):
        prompts.append(user)
        assert entry.state == "delivered", "delivered BEFORE the inference"
        return json.dumps({"control": done_control(), **REFLECTION})

    omega_run.run_omega_for_episode(episode, reason_fn=reason)

    assert "watch for stalled builds" in prompts[0] and entry.block in prompts[0]
    assert entry.state == "performed"
    assert row["mail_id"] not in {one["mail_id"] for one in mail.all_mail()}


def test_an_agent_continuations_tool_batch_carries_the_injection():
    """THE production path: an Omega successor continuing the operator's terminal-agent task
    runs its next tool batch through the operator tool loop."""
    _parent_episode, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)
    runtime = _Runtime()

    _execute(entry, runtime, inputs={"operator_route": "agent", "agent_continuation": True,
                                     "agent_next_step": "switch the storage layer"})

    first = runtime.text(0)
    assert "CONTINUING THE OPERATOR'S TASK" in first and entry.block in first
    assert entry.state == "delivered" and _lane_mail() == {}


@pytest.mark.parametrize("action, closed", [("RESPONSE", True), ("WINDOW_REFLECTION", False)])
def test_auto_keeps_a_window_episodes_entry_for_omegas_fold(action, closed, monkeypatch):
    """AUTO returns every other lane's unconsumed claim; a window's first inference is later."""
    calls = []
    episode = SimpleNamespace(meta={"execution_action_pack": {"action": action}},
                              _lane_entry=SimpleNamespace(close=lambda *, ran: calls.append(ran)))
    monkeypatch.setattr(auto_loop, "_run_auto", lambda *a, **k: None)

    auto_loop.run_auto(episode)

    assert calls == ([False] if closed else []), "handed back as NOT run"


# ── 7. review round 2 (2026-09-17): the gate protects EXECUTION ────────────────

def _runnable(episode, pack):
    goal = {"episode_id": episode.episode_id, "intent": "RESPOND", "omega_lane": "agent"}
    pack.update(source_action_goal=goal, tool="")
    episode.meta = {"execution_action_pack": pack}
    episode.global_ = {"intent_decision": goal}
    return episode


@pytest.mark.parametrize("requirement", ["preferred", "required"])
def test_a_replayed_continuation_is_recognised_without_its_mail(requirement):
    """Reviewer's probe: deliver, perform, restart, replay. The mail is GONE (acknowledged),
    so nothing can be claimed — the record alone must stop the second run."""
    parent, successor = _publish([_injection(requirement)])
    episode, pack = _continuation(successor, episode_id="ep-first")
    first = gate.open_lane_entry(episode, pack)
    runtime = _Runtime()
    _execute(first, runtime)
    first.close(ran=True)
    assert _lane_mail() == {} and len(runtime.calls) == 1
    _restart()

    replay, replay_pack = _continuation(successor, episode_id="ep-replay")
    with pytest.raises(gate.LaneEntryRefused) as refusal:
        gate.open_lane_entry(replay, replay_pack)

    assert refusal.value.kind == "lane_entry_effect_already_performed"
    assert [row["episode_id"] for row in _deliveries(parent).values()] == ["ep-first"]


def test_a_continuation_that_ran_without_its_optional_mail_is_not_run_twice(monkeypatch):
    """A rejected preferred message does not block the action or permit replay."""
    _parent_episode, successor = _publish([_injection("preferred")])
    [row] = _lane_mail().values()
    state = mail._candidate()
    state["mail"][row["mail_id"]]["status"] = "rejected"
    mail._publish(state)
    episode, pack = _continuation(successor, episode_id="ep-first")
    monkeypatch.setattr(auto_loop.execute_phase, "execute",
                        lambda *a, **k: {"outputs": {"response_text": "done"}, "failure_kind": None})
    auto_loop._execute_with_lane_entry(gate.open_lane_entry(episode, pack))(None, None, {}, None)
    _restart()

    replay, replay_pack = _continuation(successor, episode_id="ep-replay")
    with pytest.raises(gate.LaneEntryRefused) as refusal:
        gate.open_lane_entry(replay, replay_pack)

    assert refusal.value.kind == "lane_entry_effect_already_performed"


def _tool_spy(monkeypatch):
    from auto_loop.execute import brain_skill_executor as executor

    calls = []
    real = executor.run_operator_tool

    def spy(name, arguments, ctx):
        calls.append(name)
        return real(name, arguments, ctx)

    monkeypatch.setattr(executor, "run_operator_tool", spy)
    return calls


_INSPECT = dict(user="list the files in the folder",
                inputs={"operator_route": "agent", "agent_continuation": True,
                        "agent_next_step": "look at what is there"})


def test_no_deterministic_tool_runs_for_mail_the_episode_no_longer_owns(monkeypatch, tmp_path):
    """Reviewer's probe: the lease lapsed after admission, and the forced directory
    inspection ran BEFORE the first inference discovered it. Through the real run_auto-side
    wrapper and the real execute: zero tools, zero inference, one bounded failure."""
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    monkeypatch.setattr(gate, "LEASE_S", 0.05)
    entry = gate.open_lane_entry(episode, pack)
    time.sleep(0.1)
    tools, runtime = _tool_spy(monkeypatch), _Runtime()
    monkeypatch.setattr(auto_loop.execute_phase, "execute",
                        lambda *a, **k: _execute(k["lane_entry"], runtime, **_INSPECT))

    result = auto_loop._execute_with_lane_entry(entry)(None, None, {}, None)

    assert tools == [] and runtime.calls == []
    assert result["failure_kind"] == "lane_entry_ownership_lost" and result["outputs"] == {}
    assert [row["state"] for row in _deliveries(parent).values()] == ["abandoned"]


def test_a_forced_inspection_runs_after_delivery(monkeypatch):
    """The same route with its mail in hand: delivered where the prompt is built, THEN the
    deterministic tool — which is why delivery cannot wait for the first inference."""
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)
    states = []
    from auto_loop.execute import brain_skill_executor as executor

    real = executor.run_operator_tool
    monkeypatch.setattr(executor, "run_operator_tool",
                        lambda *a, **k: (states.append(entry.state), real(*a, **k))[1])
    runtime = _Runtime()

    _execute(entry, runtime, **_INSPECT)

    assert states and set(states) == {"delivered"}
    assert _lane_mail() == {} and entry.block in runtime.text(0)


def test_a_tool_reached_before_delivery_is_refused_not_performed(monkeypatch):
    """The backstop under the tool loop itself, for any route that reaches a tool first."""
    import importlib

    execute_run = importlib.import_module("auto_loop.execute.run")
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)       # claimed, NOT delivered
    entry.deliver = lambda _prompt: None              # a route that skipped delivery
    tools, runtime = _tool_spy(monkeypatch), _Runtime()

    execute_run._operator_tools_reply(
        runtime, system="s", prompt="p " + entry.block, objective="list the folder",
        episode_id="ep-next", max_tokens=64, enable_thinking=False, timeout=None,
        chat_cycle_id="", warnings=[], forced_tool="list_directory", operator_route="agent",
        lane_entry=entry)

    assert tools == []
    assert entry.refusal is not None and entry.refusal.kind == "lane_entry_action_before_delivery"
    assert [row["state"] for row in _deliveries(parent).values()] == ["abandoned"]


@pytest.mark.parametrize("how", ["acknowledgement", "deterministic", "remember"])
def test_a_reply_that_builds_no_prompt_cannot_pass_required_mail(how, monkeypatch):
    """Reviewer's probe: "Acknowledged — on it." came back as a SUCCESS past a required
    injection. The action name says RESPONSE; the path shows nothing."""
    parent, successor = _publish([_injection("required")])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)
    runtime = _Runtime()
    shape = {"acknowledgement": dict(prompt_type="RESPONSE_ACK"),
             "deterministic": dict(blocks={"deterministic_response_text": "It is 4pm."}),
             "remember": dict(user="remember that my favourite colour is green")}[how]
    monkeypatch.setattr(auto_loop.execute_phase, "execute",
                        lambda *a, **k: _execute(k["lane_entry"], runtime, **shape))
    answered = []
    monkeypatch.setattr(episode_sram, "prepare_persona_intake_for_action",
                        lambda text: answered.append(text) or ["a fact"])

    result = auto_loop._execute_with_lane_entry(entry)(None, None, {}, None)

    assert result["failure_kind"] == "lane_entry_not_consumed" and result["outputs"] == {}
    assert runtime.calls == [] and answered == [], "refused BEFORE the reply did anything"
    assert entry.refusal is not None, "declared by the path itself, not only caught afterwards"
    assert [row["status"] for row in _lane_mail().values()] == ["ready"]
    assert [row["state"] for row in _deliveries(parent).values()] == ["abandoned"]


def test_a_reply_that_builds_no_prompt_hands_optional_mail_back():
    parent, successor = _publish([_injection("preferred")])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)

    result = _execute(entry, _Runtime(), prompt_type="RESPONSE_ACK")
    entry.close(ran=True)

    assert result["outputs"]["response_text"] == "Acknowledged — on it."
    assert [row["status"] for row in _lane_mail().values()] == ["ready"]
    assert [row["state"] for row in _deliveries(parent).values()] == ["performed"]


def test_a_lane_that_returns_without_showing_required_mail_is_autos_failure(monkeypatch):
    """The backstop for a no-prompt path nobody declared."""
    parent, successor = _publish([_injection("required")])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)
    monkeypatch.setattr(auto_loop.execute_phase, "execute",
                        lambda *a, **k: {"outputs": {"response_text": "did it anyway"},
                                         "failure_kind": None})

    result = auto_loop._execute_with_lane_entry(entry)(None, None, {}, "ENVELOPE")

    assert result["failure_kind"] == "lane_entry_not_consumed" and result["outputs"] == {}
    assert result["memory_envelope"] == "ENVELOPE"
    assert [row["state"] for row in _deliveries(parent).values()] == ["abandoned"]


def test_a_refusal_inside_the_lane_is_autos_outcome_not_an_exception(monkeypatch):
    """Reviewer's probe, through the REAL run_auto and the REAL execute: ownership lapses
    while the prompt is being prepared. AUTO folds a bounded failure; nothing escapes, so
    Meta B, Global B and Omega still reflect on it."""
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    _runnable(episode, pack)
    # Everything else is real; only the pack's own schema check is stood down, because this
    # pack is hand-built rather than Meta A's.
    monkeypatch.setattr(auto_loop.validate_phase, "validate_for_execution",
                        lambda _pack: (True, None, None))
    monkeypatch.setattr(gate, "LEASE_S", 0.05)
    real_build = auto_loop.promptpack_phase.build_prompt_pack

    def slow_build(*args, **kwargs):
        time.sleep(0.1)                  # the lease lapses on the way to the lane
        return real_build(*args, **kwargs)

    monkeypatch.setattr(auto_loop.promptpack_phase, "build_prompt_pack", slow_build)
    runtime = _Runtime()

    with patch("runtime.socket.invoke_runtime_generate", runtime):
        outcome = auto_loop.run_auto(episode)

    assert outcome is not None and runtime.calls == []
    assert episode.auto["failure_kind"] == "lane_entry_ownership_lost"
    assert episode.auto["status"] != "success"
    assert not any(episode.auto["outputs"].values()), "the fold's schema, with nothing in it"
    assert [row["state"] for row in _deliveries(parent).values()] == ["abandoned"]


def test_a_refused_window_delivery_is_a_contained_fold_failure(monkeypatch):
    from dataclasses import replace

    from omega_loop.episode import run as omega_run
    from tests.omega_loop.test_foldback_prompt_dispatch import packet_fixture

    packet = replace(packet_fixture("window"), episode_id="ep-window")
    monkeypatch.setattr(omega_run, "_with_current_identity", lambda value: value)
    refusal = gate.LaneEntryRefused("lane_entry_ownership_lost", "gone")

    def refuse(_prompt):
        raise refusal

    entry = SimpleNamespace(state="claimed", block="", deliver=refuse, enter=lambda: None,
                            close=lambda **k: None)
    episode = SimpleNamespace(episode_id="ep-window", global_={"omega_packet": packet.to_dict()},
                              temporal={}, _lane_entry=entry)

    with pytest.raises(omega_run.EpisodeCognitionFoldError, match="lane_entry_ownership_lost"):
        omega_run.run_omega_for_episode(
            episode, reason_fn=lambda *_: pytest.fail("inferred past a refusal"))


def test_an_effect_taken_without_mail_is_given_back_when_the_lane_was_never_reached(monkeypatch):
    parent, successor = _publish([_injection("preferred")])
    [row] = _lane_mail().values()
    state = mail._candidate()
    state["mail"][row["mail_id"]]["status"] = "rejected"
    mail._publish(state)
    episode, pack = _continuation(successor, episode_id="ep-first")
    gate.open_lane_entry(episode, pack).close(ran=False)
    assert [row["state"] for row in _deliveries(parent).values()] == ["abandoned"]

    retry, retry_pack = _continuation(successor, episode_id="ep-retry")
    assert gate.open_lane_entry(retry, retry_pack) is not None, "nothing ran, so it still may"


# ── 8. review round 3 (2026-09-17) ────────────────────────────────────────────

def _failing_completion(monkeypatch, failures):
    """The ledger writer fails on the next ``failures`` PERFORMED writes."""
    real_write, left = ledger._write_successors, [failures]

    def write(data):
        performing = any(row["state"] == "performed" for record in data["transactions"].values()
                         for row in record["deliveries"].values())
        if performing and left[0] > 0:
            left[0] -= 1
            raise OSError("disk full")
        return real_write(data)

    monkeypatch.setattr(ledger, "_write_successors", write)
    # Switched off rather than undone: undoing the monkeypatch would also undo the fixtures
    # that keep this test off the live ledger.
    return lambda: left.__setitem__(0, 0)


def _run_through_auto(entry, runtime, episode):
    """The wrapper, then the outer cleanup — exactly as ``run_auto`` does."""
    import importlib

    execute_run = importlib.import_module("auto_loop.execute.run")
    real = execute_run.execute
    try:
        return auto_loop._execute_with_lane_entry(entry)(None, None, {}, None)
    finally:
        entry.close(ran=False)
        assert execute_run.execute is real


@pytest.mark.parametrize("requirement", ["preferred", "required"])
@pytest.mark.parametrize("failures, standing", [(1, "performed"), (99, "executing")])
def test_a_failed_completion_write_never_makes_executed_work_replayable(
        requirement, failures, standing, monkeypatch):
    """Reviewer's probe: the PERFORMED write failed after a successful inference, the outer
    cleanup then durably abandoned the entry, and after a restart it executed again. One
    failure is retried; a persistent one leaves the record 'executing' — never abandoned."""
    parent, successor = _publish([_injection(requirement)])
    episode, pack = _continuation(successor, episode_id="ep-first")
    entry = gate.open_lane_entry(episode, pack)
    runtime = _Runtime()
    monkeypatch.setattr(auto_loop.execute_phase, "execute",
                        lambda *a, **k: _execute(k["lane_entry"], runtime))
    recover = _failing_completion(monkeypatch, failures)

    _run_through_auto(entry, runtime, episode)

    assert len(runtime.calls) == 1
    assert [row["state"] for row in _deliveries(parent).values()] == [standing]
    recover()
    _restart()
    replay, replay_pack = _continuation(successor, episode_id="ep-replay")
    with pytest.raises(gate.LaneEntryRefused):
        gate.open_lane_entry(replay, replay_pack)
    assert len(runtime.calls) == 1, "executed exactly once"


def test_cleanup_cannot_abandon_an_entry_the_lane_was_handed():
    parent, successor = _publish([_injection("preferred")])
    episode, pack = _continuation(successor, action="USE_TOOL")     # taken without mail
    entry = gate.open_lane_entry(episode, pack)
    entry.enter()

    entry.close(ran=False)

    assert [row["state"] for row in _deliveries(parent).values()] == ["performed"]


def test_a_batch_that_was_never_admitted_consumes_nothing(monkeypatch, tmp_path):
    """Reviewer's probe: the job already had a writer. The real path answered "This job
    already has an active writer.", inferred nothing — and had already deleted the required
    instruction and marked its effect performed."""
    from auto_loop.execute import job_control as jobs

    workspace = tmp_path / "workspace"
    (workspace / "app").mkdir(parents=True)
    jobs.open_job("job-a", "cycle-owner", "build it", workspace, project_root=workspace / "app",
                  thread_id=THREAD)
    assert jobs.enter_writer("job-a", "cycle-owner")           # somebody else is writing
    parent, successor = _publish([_injection("required")])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)
    tools, runtime = _tool_spy(monkeypatch), _Runtime()
    inputs = {"operator_route": "agent", "agent_continuation": True, "job_id": "job-a",
              "agent_next_step": "switch the storage layer"}
    monkeypatch.setattr(auto_loop.execute_phase, "execute",
                        lambda *a, **k: _execute(k["lane_entry"], runtime, inputs=inputs))

    result = _run_through_auto(entry, runtime, episode)

    assert runtime.calls == [] and tools == []
    assert result["failure_kind"] == "lane_entry_not_consumed" and result["outputs"] == {}
    assert [row["status"] for row in _lane_mail().values()] == ["ready"], "the instruction survives"
    assert [row["state"] for row in _deliveries(parent).values()] == ["abandoned"]

    # ...and once the writer is free, the SAME instruction reaches the batch.
    jobs.leave_writer("job-a", "cycle-owner")
    retry, retry_pack = _continuation(successor, episode_id="ep-retry")
    again = gate.open_lane_entry(retry, retry_pack)
    assert again is not None and "Switch storage to SQLite" in again.block


def test_delivery_follows_admission_and_precedes_every_tool(monkeypatch):
    """The order inside the tool loop: own the job, THEN deliver, THEN the forced tool."""
    from auto_loop.execute import brain_skill_executor as executor
    from auto_loop.execute import job_control as jobs

    _parent_episode, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)
    order = []
    real_deliver, real_tool, real_get = entry.deliver, executor.run_operator_tool, jobs.get_job
    entry.deliver = lambda prompt: (order.append("deliver"), real_deliver(prompt))[1]
    monkeypatch.setattr(executor, "run_operator_tool",
                        lambda *a, **k: (order.append("tool"), real_tool(*a, **k))[1])
    monkeypatch.setattr(jobs, "get_job", lambda job_id: (order.append("admission"), real_get(job_id))[1])

    try:
        _execute(entry, _Runtime(), user=_INSPECT["user"],
                 inputs={**_INSPECT["inputs"], "job_id": "job-missing"})
    except Exception:  # noqa: BLE001 - an unknown job fails admission; the ORDER is the point
        pass

    assert order[:1] == ["admission"] and "deliver" not in order and "tool" not in order
    assert [row["status"] for row in _lane_mail().values()] == ["leased"], "not consumed"


def test_a_record_that_cannot_be_written_refuses_delivery_as_autos_outcome(monkeypatch):
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    entry = gate.open_lane_entry(episode, pack)
    real = ledger.advance_lane_entry

    def advance(*args, **kwargs):
        if kwargs["to"] == "executing":
            raise OSError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(ledger, "advance_lane_entry", advance)
    runtime = _Runtime()
    monkeypatch.setattr(auto_loop.execute_phase, "execute",
                        lambda *a, **k: _execute(k["lane_entry"], runtime))

    result = _run_through_auto(entry, runtime, episode)

    assert result["failure_kind"] == "lane_entry_record_failed" and runtime.calls == []
    assert [row["status"] for row in _lane_mail().values()] == ["ready"], "nothing acknowledged"
    assert [row["state"] for row in _deliveries(parent).values()] == ["abandoned"]


# ── 9. review round 4 (2026-09-17): failures at the gate are AUTO's outcomes too ───────

def _failing_ledger_writes(monkeypatch, when):
    real = ledger._write_successors

    def write(data):
        states = {row["state"] for record in data["transactions"].values()
                  for row in record["deliveries"].values()}
        if when(states):
            raise OSError("disk full")
        return real(data)

    monkeypatch.setattr(ledger, "_write_successors", write)


def test_a_failed_reservation_write_is_autos_bounded_failure(monkeypatch):
    """Reviewer's probe, through the REAL run_auto: it raised OSError out of AUTO."""
    _parent_episode, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    _runnable(episode, pack)
    monkeypatch.setattr(auto_loop.validate_phase, "validate_for_execution",
                        lambda _pack: (True, None, None))
    monkeypatch.setattr(auto_loop.execute_phase, "execute",
                        lambda *a, **k: pytest.fail("executed without a reservation"))
    _failing_ledger_writes(monkeypatch, lambda states: "started" in states)

    outcome = auto_loop.run_auto(episode)

    assert outcome is not None
    assert episode.auto["failure_kind"] == "lane_entry_record_failed"
    assert "disk full" in episode.auto["failure_reason"], "the reason survives"
    assert [row["status"] for row in _lane_mail().values()] == ["ready"], "nothing was claimed"


def test_a_cleanup_failure_never_masks_the_refusal(monkeypatch):
    """Reviewer's probe: an unreadable mail store, then a failed abandonment write. The
    prerequisite refusal must be what AUTO folds — not the cleanup's exception — and the
    standing left on the record is the conservative one: a reservation, which lapses."""
    parent, successor = _publish([_injection()])
    episode, pack = _continuation(successor)
    _runnable(episode, pack)
    monkeypatch.setattr(auto_loop.validate_phase, "validate_for_execution",
                        lambda _pack: (True, None, None))
    monkeypatch.setattr(auto_loop.execute_phase, "execute",
                        lambda *a, **k: pytest.fail("executed past a refusal"))
    monkeypatch.setattr(mail, "claim_lane_entry",
                        lambda **_k: (_ for _ in ()).throw(ValueError("cross-lane mail is unreadable")))
    _failing_ledger_writes(monkeypatch, lambda states: "abandoned" in states)

    outcome = auto_loop.run_auto(episode)

    assert outcome is not None
    assert episode.auto["failure_kind"] == "lane_entry_prerequisite_missing"
    assert "unreadable" in episode.auto["failure_reason"]
    assert [row["state"] for row in _deliveries(parent).values()] == ["started"]


def test_a_missing_prerequisite_still_releases_its_mail_when_cleanup_cannot_be_written(monkeypatch):
    parent, successor = _publish(
        [_injection("required", objective="first"), _injection("required", objective="second")])
    episode, pack = _continuation(successor)
    second = successor.metadata["lane_entry"]["mail"][1]["mail_id"]
    state = mail._candidate()
    state["mail"][second]["status"] = "rejected"
    mail._publish(state)
    _failing_ledger_writes(monkeypatch, lambda states: "abandoned" in states)

    with pytest.raises(gate.LaneEntryRefused) as refusal:
        gate.open_lane_entry(episode, pack)

    assert refusal.value.kind == "lane_entry_prerequisite_missing" and "rejected" in refusal.value.reason
    first = successor.metadata["lane_entry"]["mail"][0]["mail_id"]
    assert _lane_mail()[first]["status"] == "ready", "released BEFORE the cleanup that failed"


def test_a_continuation_taken_without_mail_is_only_a_reservation_until_its_lane(monkeypatch):
    """Taken without mail, then a failure on the way to the lane, then a cleanup write that
    fails too. Moved to 'executing' at the gate, that stranded a continuation which never ran
    as unprovable for good; as a reservation it simply lapses."""
    parent, successor = _publish([_injection("preferred")])
    episode, pack = _continuation(successor, action="USE_TOOL", episode_id="ep-first")
    monkeypatch.setattr(gate, "LEASE_S", 0.05)
    entry = gate.open_lane_entry(episode, pack)
    _failing_ledger_writes(monkeypatch, lambda states: "abandoned" in states)

    entry.close(ran=False)                              # AUTO never reached the lane

    assert [row["state"] for row in _deliveries(parent).values()] == ["started"]
    time.sleep(0.1)
    retry, retry_pack = _continuation(successor, action="USE_TOOL", episode_id="ep-retry")
    assert gate.open_lane_entry(retry, retry_pack) is not None, "nothing ran, so it still may"


def test_a_continuation_taken_without_mail_runs_exactly_once_through_auto(monkeypatch):
    parent, successor = _publish([_injection("preferred")])
    episode, pack = _continuation(successor, action="USE_TOOL", episode_id="ep-first")
    seen = []

    def lane(*_args, lane_entry, **_kwargs):
        seen.append((lane_entry.state, [row["state"] for row in _deliveries(parent).values()]))
        return {"outputs": {"tool_status": "success"}, "failure_kind": None}

    monkeypatch.setattr(auto_loop.execute_phase, "execute", lane)

    auto_loop._execute_with_lane_entry(gate.open_lane_entry(episode, pack))(None, None, {}, None)

    assert seen == [("delivered", ["executing"])], "the running effect BEFORE the lane acts"
    assert [row["state"] for row in _deliveries(parent).values()] == ["performed"]


# ── browser: read-only observation before delivery; no action bypasses the gate ──

def test_a_gated_browser_run_only_looks_before_its_first_inference():
    from browser_agent.agent_loop import BrowserAgentRun
    from tests.browser_agent.test_agent_loop import FakeController, Seat, call

    def run(gated):
        controller = FakeController()
        controller.locked_window = "omega-window"           # a surface is already attached
        banners = []
        seat = Seat(call("done", summary="done", evidence="seen"))
        real = seat.__call__

        class Watching:
            requests = seat.requests

            def __call__(self, **kwargs):
                banners.append(controller.banner_shown)
                return real(**kwargs)

        BrowserAgentRun(Watching(), controller, objective="find pricing",
                        before_action=(lambda: None) if gated else None).run()
        return banners[0], [one[0] for one in controller.calls]

    banner, calls = run(gated=True)
    assert banner == 0, "nothing is drawn into the page before the mail is delivered"
    assert not {"navigate", "click", "type", "key", "script", "back", "switch_tab"} & set(calls)
    assert run(gated=False)[0] >= 1, "an ungated run is unchanged"


def test_a_refused_browser_action_ends_the_run_without_touching_the_page():
    """Reviewer's probe, with a window ALREADY attached: the navigation was blocked, but the
    observation that followed drew the task banner. Banner writes have their own counter."""
    from browser_agent.agent_loop import BrowserAgentRun
    from tests.browser_agent.test_agent_loop import FakeController, Seat, call

    def refuse():
        raise gate.LaneEntryRefused("lane_entry_action_before_delivery", "not delivered")

    controller = FakeController()
    controller.locked_window = "omega-window"
    seat = Seat(call("click", x=100, y=100, target="a link"),
                call("done", summary="carried on", evidence=""))

    receipt = BrowserAgentRun(seat, controller, objective="find pricing", before_action=refuse).run()

    assert controller.banner_shown == 0, "nothing was drawn into the page"
    assert not {"navigate", "click", "type", "key", "script", "back", "switch_tab"} & {
        one[0] for one in controller.calls}
    assert receipt["status"] == "blocked" and "refused" in receipt["stopped_reason"]
    assert len(seat.requests) == 1, "the run ended: no second inference, no second action"
    assert [(row["action"], row["ok"]) for row in receipt["actions"]] == [("click", False)]


def test_a_gated_run_draws_its_banner_once_the_gate_has_passed():
    from browser_agent.agent_loop import BrowserAgentRun
    from tests.browser_agent.test_agent_loop import FakeController, Seat, call

    controller = FakeController()
    controller.locked_window = "omega-window"
    seat = Seat(call("click", x=100, y=100, target="a link"),
                call("done", summary="done", evidence="seen"))

    BrowserAgentRun(seat, controller, objective="find pricing", before_action=lambda: None).run()

    assert controller.banner_shown >= 1 and any(one[0] == "click" for one in controller.calls)
