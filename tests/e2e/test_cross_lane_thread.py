"""T15: one thread carries research into discussion, implementation and verification.

Cross-lane cognitive integration spec, T15 and acceptance item 1. Four episodes, three lanes,
ONE thread, through the real cascade (``BlueprintDriver``) with only the model seat and the
browser session stubbed:

1. *Browser research.* An operator turn routed to the browser lane opens the documentation
   page and Omega settles it. The episode roots the thread (``thread_id == episode_id``).
2. *Chat discussion.* The operator's explicit follow-up ("now build it ...") is bound to the
   same thread by T3's binder through the recent-route context the browser reply stamped.
   The chat fold binds the browser's evidence (``lane_evidence`` mail on the thread), records
   a decision and the operator's constraint, and hands the agent lane a required injection.
   Omega routes the intention between lanes: PRESSURE with a USE_TOOL intent stages an AGENT
   successor on the thread.
3. *Agent implementation.* The continuation receives the decision, the constraint and the
   evidence reference in its own prompt BEFORE any tool runs (T9), writes the file, and its
   verification is red.
4. *Verification.* Omega continues once more, the verification passes, Omega settles DONE.

Provenance is asserted where it is durable: the transaction record (thread, receipts, the
bound evidence, the outgoing mail), the intake journal (each statement's ``derived_from``),
the causal facts (``informed`` from the browser's evidence to the decision, ``continued`` from
each fold into its continuation) and the graph the real rebuild projects from them.

"Why did we build it this way" is then answered from those records: the ordinary authorized
recall path returns the decision by its canonical id with lane discipline intact, and the
graph's edges lead from the browser's evidence to that decision and from the deciding fold
into the continuation that built it. Nothing in the answer is a synthesis the model wrote.

The browser's evidence is published by the production cognitive transaction, by value and
under the browser episode's identity. No test pins evidence. Only the external browser and
model seat are substituted; binding, publication, delivery and capture are production paths.

The last test exercises T4 ownership: two folds of one thread on the same logical
continuation, with different parent episodes, must stage exactly one successor.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.e2e.test_cognitive_transaction_recovery import (
    _appending_batch,
    _mail_rows,
    _records,
    _staged,
)
from tests.e2e.test_cross_restart_memory_loop import _answer_on_the_same_thread, _asking_batch
from tests.helpers.blueprint_driver import (
    BlueprintDriver,
    agent_decision,
    assessment,
    browser_action,
    browser_decision,
    tool_call,
)
from tests.helpers.intent_router import accepted_decision

RESEARCH = "open the documentation and compare streaming CSV writers for the export"
RESEARCH_CYCLE = "bubble-t15-research"
DOCS = "https://docs.invalid/streaming-writers"
EVIDENCE = "The streaming writer handles files larger than memory"
FOLLOW_UP = "now build it with the streaming writer and keep the CSV column order"
DISCUSSION_CYCLE = "bubble-t15-discussion"
REPLY = "The streaming writer is the right choice and I will keep the column order."
DECISION = "Export through the streaming writer"
CONSTRAINT = "Keep the CSV column order"
OBJECTIVE = "wire the streaming export"
SOURCE = ("def export_rows(rows, sink):\n"
          "    for row in rows:\n"
          "        sink.write(','.join(row) + '\\n')\n")


@pytest.fixture(autouse=True)
def _isolate_canonical_memory_for_every_suite(tmp_path, monkeypatch):
    """Cross-lane recall must verify actual canonical source and claim receipts."""
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    redirect_memory_to_tmp(tmp_path, monkeypatch, stub_encoding=False)


@pytest.fixture
def driver(monkeypatch, tmp_path):
    return BlueprintDriver(monkeypatch=monkeypatch, tmp_path=tmp_path)


def _route_by_message(monkeypatch, routes):
    """Seat Global A's router stub keyed by the operator's words, at the same seam
    ``tests.helpers.intent_router.stub_router`` uses.

    Unlike ``stub_router`` it does NOT clear the recent-route context: a follow-up in the
    middle of a scenario has to find the thread the previous reply stamped there.
    """
    from global_loop.intent import router as intent_router

    calls: list[tuple[str, str]] = []

    def route_turn(text, context=""):
        calls.append((str(text), str(context)))
        route = routes.get(" ".join(str(text or "").split()))
        assert route, f"an operator turn the scenario did not route: {text!r}"
        return accepted_decision(route)

    monkeypatch.setattr(intent_router, "get_router",
                        lambda **_: SimpleNamespace(route=route_turn))
    return calls


def _browser_research(driver):
    """Episode 1: the browser lane reads the documentation page and Omega settles it."""
    driver.browser.pages[DOCS] = "Streaming writers"
    driver.browser_actions.extend([
        browser_action("navigate", url=DOCS),
        browser_action("done", summary=EVIDENCE, evidence="the documentation page says so"),
    ])
    driver.omega.append(browser_decision("DONE", synthesis=EVIDENCE + "."))

    episode = driver.operator_turn(RESEARCH, route="BROWSER_AGENT", cycle_id=RESEARCH_CYCLE)

    view = driver.view(episode)
    assert (view.intent, view.lane) == ("BROWSE", "browser")
    assert view.run["status"] == "done" and view.run["final_page"]["url"] == DOCS
    assert view.control["decision"] == "DONE"
    reply = driver.reply(RESEARCH_CYCLE)
    assert "Browser lane" in reply["text"]
    assert reply["lane_work"]["lane"] == "browser"
    assert reply["lane_work"]["status"] == "completed"
    return episode


def _browser_evidence(research):
    """Read the evidence the browser's real fold published for its thread."""
    from global_loop import intersections as mail

    [evidence] = [row for row in mail.all_mail()
                  if row["purpose"] == "lane_evidence"
                  and row["source_episode_id"] == str(research.episode_id)]
    assert evidence["payload"]["text"] == EVIDENCE
    assert evidence["payload"]["url"] == DOCS
    return evidence


def _discussion_fold(_driver):
    """Episode 2's fold: a decision resting on the evidence, the operator's constraint, and
    a required injection for the agent lane, which Omega enters through a USE_TOOL
    continuation."""
    return {
        "control": {
            "assessment": assessment("the export is discussed but not built"),
            "decision": "PRESSURE",
            "synthesis": "Build the export with the streaming writer.",
            "pressure": {"intent": "USE_TOOL", "confidence": 0.8, "reason": OBJECTIVE,
                         "setpoints": {}, "desired_state": "the export streams and is verified",
                         "observed_state": "discussed only",
                         "resolution_conditions": ["the export verification passes"]},
        },
        "decisions": [{"statement": DECISION,
                       "rationale": "the documentation shows it handles files larger than memory"}],
        "user_constraints": [{"statement": CONSTRAINT}],
        "lane_injections": [{"target_lane": "agent", "objective": OBJECTIVE,
                             "constraints": [CONSTRAINT], "evidence_refs": [DOCS],
                             "requirement": "required"}],
    }


def _operator_follow_up(driver, text, *, cycle_id):
    """An operator turn through the real public ingress, so T3's binder decides its thread."""
    from pressure_loop.cognition_queue import lease_next_input
    from pressure_loop.operator_chat import handle_operator_chat

    result = handle_operator_chat(text, cycle_id=cycle_id)
    assert result["ok"] and result["queued"], f"an ordinary turn, not held mail: {result}"
    leased = lease_next_input(0)
    assert leased is not None and leased.type == "operator_query"
    return driver._ignite(leased, opened_by=leased.type)


def _implementation_batch():
    """Episode 3's batch: claim the project, write the export, run a RED verification."""
    return {"text": "", "tool_calls": [
        tool_call("start_job", {"project_root": "."}, "claim-implement"),
        tool_call("task_plan", {"steps": ["write the export", "verify it"]}, "plan-implement"),
        tool_call("write_file", {"path": "src/export.py", "content": SOURCE}, "write-export"),
        tool_call("run_command", {"command": "exit 1"}, "verify-red")]}


def _verification_batch():
    """Episode 4's batch: the verification, green this time."""
    return {"text": "", "tool_calls": [
        tool_call("start_job", {"project_root": "."}, "claim-verify"),
        tool_call("run_command", {"command": "exit 0"}, "verify-green")]}


def _lane_requests_as_sent(driver):
    """Every agent-lane request, sampled the instant it is sent, beside whether the export
    file existed yet — so "delivered before execution" is measured at the only moment it
    means anything."""
    sampled: list[tuple[str, bool]] = []
    seat = driver.generate
    source = driver.harness.workspace_root / "src" / "export.py"

    def watching(**kwargs):
        if str(kwargs.get("operation") or "") == "auto_response":
            sampled.append((json.dumps(kwargs, default=str), source.exists()))
        return seat(**kwargs)

    driver.monkeypatch.setattr("runtime.socket.invoke_runtime_generate", watching)
    return sampled


def _receipt(episode, kind):
    """The accepted delta receipt of one kind (``decision`` / ``user_constraint``)."""
    result = dict(dict(getattr(episode, "global_", None) or {}).get("omega_result") or {})
    [row] = [dict(record) for record in result.get("delta_receipts") or () if record.get("kind") == kind]
    assert row["accepted"] and row["journal_record_id"], row
    return row


def _facts(episode, kind):
    from memory.causal_edges import causal_facts

    return [(fact["source"], fact["target"], dict(fact["detail"])) for fact in causal_facts(episode)
            if fact["kind"] == kind]


def _fact_record(kind, source, target):
    """A causal fact's OWN intake journal record, by its fold-stable event id."""
    from memory import intake_journal
    from memory.causal_edges import fact_id_for

    event_id = f"causal:{fact_id_for(kind, source, target)}"
    return next((record for record in intake_journal.records() if record.event_id == event_id), None)


def _edges(driver):
    """The published graph's edges, read the production way: ``(edge_type, source label,
    target label) -> evidence_event_id``."""
    from memory.taxonomy import graph_store
    from memory.taxonomy.graph_store import graph_path
    from memory.projection.generation import projection_read_transaction

    with projection_read_transaction(driver.harness.root):
        conn = graph_store.connect_readonly(graph_path(driver.harness.root))
        try:
            rows = conn.execute(
                "SELECT e.edge_type, s.label, t.label, e.evidence_event_id FROM edges e "
                "JOIN nodes s ON s.node_id = e.source_id "
                "JOIN nodes t ON t.node_id = e.target_id").fetchall()
        finally:
            conn.close()
    return {(str(kind), str(source), str(target)): str(evidence)
            for kind, source, target, evidence in rows}


def _derivation(record_id):
    from memory import intake_journal
    from memory.cognition_write import DERIVED_FROM_PREFIX

    record = intake_journal.record_by_id(record_id)
    assert record is not None, f"{record_id} does not resolve"
    refs = [str(ref)[len(DERIVED_FROM_PREFIX):]
            for ref in dict(record.payload or {}).get("provenance") or ()
            if str(ref).startswith(DERIVED_FROM_PREFIX)]
    return record, refs


def test_one_thread_carries_research_into_discussion_implementation_and_verification(driver):
    """Acceptance item 1, end to end on one thread, with "why" answered from the record."""
    from auto_loop.mril_tools.remember import invoke_remember_tool
    from global_loop.intent.context import previous_thread
    from memory import intake_journal

    research = _browser_research(driver)
    thread = str(research.thread_id)
    chat_root = next(row for row in driver.episodes if row.episode_id != research.episode_id)
    assert thread == str(chat_root.episode_id), "the Chat handoff episode roots the thread"
    evidence = _browser_evidence(research)
    assert previous_thread() == thread, \
        "the browser reply stamped the recent-route context with its thread (T3)"

    _route_by_message(driver.monkeypatch, {FOLLOW_UP: "FREEFORM_CHAT"})
    driver.agent_batches.append({"text": REPLY, "tool_calls": []})
    driver.omega.append(_discussion_fold)

    discussion = _operator_follow_up(driver, FOLLOW_UP, cycle_id=DISCUSSION_CYCLE)

    assert driver.view(discussion).lane == "chat"
    assert str(discussion.thread_id) == thread, "the follow-up continues the research thread"
    assert str(discussion.episode_id) != thread, "as a continuation, not a new root"
    reply = driver.reply(DISCUSSION_CYCLE)
    assert reply["text"] == REPLY, (
        f"reply={reply}; route={discussion.global_.get('operator_route')}; "
        f"auto_action={dict(discussion.auto or {}).get('action')}; "
        f"auto_outputs={dict(dict(discussion.auto or {}).get('outputs') or {})}; "
        f"remaining_agent_batches={len(driver.agent_batches)}"
    )
    binding = dict(discussion.global_["omega_mail"])
    assert binding["disposition"] == "acknowledged"
    assert binding["bound_mail_ids"] == [evidence["mail_id"]], "the fold bound the browser's evidence"
    assert EVIDENCE in driver.folds[-1]["prompt"]
    assert driver.folds[-1]["prompt"].count(evidence["mail_id"]) == 1
    decision = _receipt(discussion, "decision")
    constraint = _receipt(discussion, "user_constraint")
    assert (f"mail:{evidence['mail_id']}", f"intake:{decision['journal_record_id']}",
            {"purpose": "lane_evidence", "statement_kind": "decision"}) in _facts(discussion, "informed")
    [(fold_id, record)] = [(identity, row) for identity, row in _records().items()
                           if row["episode_id"] == str(discussion.episode_id)]
    assert (record["episode_id"], record["thread_id"]) == (str(discussion.episode_id), thread)
    assert (record["staging"], record["published"]) == ("staged", True)
    assert sorted(record["receipt_ids"]) == sorted(
        [decision["journal_record_id"], constraint["journal_record_id"]])
    assert record["bound_mail_ids"] == [evidence["mail_id"]]
    [outgoing] = record["outgoing"]
    [first_successor] = _staged()
    assert record["successor"]["metadata"]["successor_id"] == first_successor
    assert record["successor"]["metadata"]["thread_id"] == thread
    [lane_entry] = _mail_rows()
    assert (lane_entry["delivery_point"], lane_entry["mail_id"]) == ("lane_entry", outgoing["mail_id"])
    assert (lane_entry["thread_id"], lane_entry["target_lane"]) == (thread, "agent")
    assert lane_entry["requirement"] == "required"
    assert lane_entry["payload"]["decisions"] == [DECISION]
    assert lane_entry["payload"]["user_constraints"] == [CONSTRAINT]
    assert lane_entry["payload"]["evidence_refs"] == [DOCS]
    assert set(lane_entry["provenance"]) >= {
        f"episode:{discussion.episode_id}", f"intake:{decision['journal_record_id']}",
        f"intake:{constraint['journal_record_id']}"}, "provenance travels with the mail"

    driver.agent_batches.append(_implementation_batch())
    driver.omega.append(agent_decision("PRESSURE", reason="the verification is red; rerun it"))
    requests = _lane_requests_as_sent(driver)

    implementation = driver.run_successor()

    assert str(implementation.thread_id) == thread
    implemented = driver.view(implementation)
    assert implemented.lane == "agent"
    [(request, existed)] = requests
    assert DECISION in request and CONSTRAINT in request and DOCS in request, \
        "the lane was told the decision, the constraint and the evidence"
    assert existed is False, "before it ran anything"
    assert [(record["tool_name"], record["status"]) for record in implemented.outputs["operator_tool_calls"]] == [
        ("start_job", "success"), ("task_plan", "success"), ("write_file", "success"),
        ("run_command", "failure")]
    export = driver.harness.workspace_root / "src" / "export.py"
    assert export.read_text(encoding="utf-8") == SOURCE
    assert implemented.control["decision"] == "PRESSURE"
    assert _mail_rows() == [], "the delivered injection was acknowledged"
    assert _records()[fold_id]["deliveries"][outgoing["effect_id"]]["state"] == "performed"
    [(source, target, detail)] = _facts(implementation, "continued")
    assert (source, target) == (f"episode:{discussion.episode_id}", f"continuation:{first_successor}")
    assert (detail["fold_id"], detail["continuation_key"]) == (fold_id, record["continuation_key"])
    [second_successor] = _staged()
    assert second_successor != first_successor

    driver.agent_batches.append(_verification_batch())
    driver.omega.append(agent_decision(
        "DONE", synthesis="The streaming export is wired and its verification passes."))

    verification = driver.run_successor()

    assert str(verification.thread_id) == thread
    verified = driver.view(verification)
    assert verified.outputs.get("operator_tool_calls") is not None, verified.outputs
    assert [(record["tool_name"], record["status"]) for record in verified.outputs["operator_tool_calls"]] == [
        ("start_job", "success"), ("run_command", "success")]
    assert verified.control["decision"] == "DONE"
    assert driver.staged_successors() == [] and _staged() == []
    [(source, target, _detail)] = _facts(verification, "continued")
    assert (source, target) == (f"episode:{implementation.episode_id}",
                                f"continuation:{second_successor}")

    staged = [record for record in _records().values() if record["staging"] == "staged"]
    keys = [record["continuation_key"] for record in staged]
    ids = [record["successor"]["metadata"]["successor_id"] for record in staged]
    assert len(keys) == 2 and len(set(keys)) == 2, "two continuations, two keys"
    assert sorted(ids) == sorted([first_successor, second_successor]), "one successor each"
    assert sorted(record["parent_episode_id"] for record in
                  (record["successor"]["metadata"] for record in staged)) == sorted(
        [str(discussion.episode_id), str(implementation.episode_id)])

    for row in (decision, constraint):
        journal, rests_on = _derivation(row["journal_record_id"])
        assert journal.status == intake_journal.COMMITTED and journal.canonical_path
        assert f"mail:{evidence['mail_id']}" in rests_on, "the statement rests on the browser's evidence"
        assert f"episode:{discussion.episode_id}" in rests_on, "and names the fold it arose in"
    for kind, source, target in (
            ("informed", f"mail:{evidence['mail_id']}", f"intake:{decision['journal_record_id']}"),
            ("continued", f"episode:{discussion.episode_id}", f"continuation:{first_successor}"),
            ("continued", f"episode:{implementation.episode_id}", f"continuation:{second_successor}")):
        fact = _fact_record(kind, source, target)
        assert fact is not None, f"no durable {kind} fact {source} -> {target}"
        assert fact.status == intake_journal.COMMITTED and fact.canonical_path, \
            f"the {kind} fact reached canonical memory through the real drain"

    recalled = invoke_remember_tool(
        {"tool": "remember_situation", "query": "why export through the streaming writer?",
         "allowed_lobes": [4], "memory_type": "episodic"}, episode_id="t15-why")
    assert recalled["verdict"] == "verified", recalled.get("verdict_reason")
    assert (recalled["lane"], recalled["lobe"]) == ("situational", 4), "lane discipline intact"
    remembered = {str(row.get("memory_id")): str(row.get("remembered_as") or "")
                  for row in recalled["evidence"]}
    decision_memory = intake_journal.record_by_id(decision["journal_record_id"]).memory_id
    assert decision_memory in remembered, "the decision came back by its canonical id"
    assert remembered[decision_memory].startswith(DECISION)

    edges = _edges(driver)
    informed = ("INFORMED", f"mail:{evidence['mail_id']}", f"intake:{decision['journal_record_id']}")
    first_hop = ("CONTINUED", f"episode:{discussion.episode_id}", f"continuation:{first_successor}")
    second_hop = ("CONTINUED", f"episode:{implementation.episode_id}",
                  f"continuation:{second_successor}")
    assert edges[informed] == f"episode-{discussion.episode_id}", \
        "the graph relates the browser's evidence to the decision, evidenced by the deciding fold"
    assert edges[first_hop] == f"episode-{implementation.episode_id}", \
        "and the deciding fold to the continuation that built it, evidenced by that continuation"
    assert edges[second_hop] == f"episode-{verification.episode_id}"



    assert detail["fold_id"] == fold_id
    assert decision["journal_record_id"] in _records()[fold_id]["receipt_ids"]
    assert _records()[fold_id]["bound_mail_ids"] == [evidence["mail_id"]]
    assert EVIDENCE + "." not in remembered.values(), \
        "the browser fold's synthesis is not what answers the question"


REASON = "rerun the verification once the delimiter is settled"


def test_two_folds_of_one_thread_on_one_logical_continuation_stage_exactly_one(driver):
    """Spec section 8 and acceptance item 3, for this thread.

    Two parent episodes of one thread — the build's first batch, which asks the operator a
    question, and the episode that carries the operator's answer back into the same job — both
    fold PRESSURE on the same work: one thread, one job, one intent, one set of resolution
    conditions. Their ``continuation_key`` is identical (asserted, and true today) and their
    parent episode ids differ. T4 must stage exactly one and decline the other.

    The precondition assertions are the scenario's own: if the answer's batch did not resume
    the same job, this test is failing for a harness reason and not for T4.
    """
    from auto_loop.execute import job_control as jobs

    driver.agent_batches.extend([_asking_batch(0), _appending_batch(1)])
    driver.omega.extend([agent_decision("PRESSURE", reason=REASON),
                         agent_decision("PRESSURE", reason=REASON)])

    first = driver.operator_turn("wire the export button", route="CODING_AGENT",
                                 cycle_id="bubble-t15-ask")
    thread = str(first.thread_id)
    job = driver.view(first).outputs["agent_run"]["job_id"]
    assert jobs.get_job(job).status == "running"
    [first_successor] = _staged()

    answer = _answer_on_the_same_thread(driver, "comma", cycle_id="bubble-t15-answer")

    assert str(answer.thread_id) == thread, "the answer continues the thread on the question"
    assert str(answer.episode_id) != str(first.episode_id)
    run = dict(driver.view(answer).outputs.get("agent_run") or {})
    assert (run.get("job_id"), run.get("status")) == (job, "batch_ran"), \
        f"precondition: the answer's batch resumed the same job, got {run}"
    parent_episodes = {str(first.episode_id), str(answer.episode_id)}
    records = [record for record in _records().values()
               if record["episode_id"] in parent_episodes]
    assert {record["episode_id"] for record in records} == parent_episodes
    keys = {record["continuation_key"] for record in records}
    assert len(keys) == 1 and "" not in keys, \
        "the same logical continuation: one thread, one job, one intent, one resolution"

    assert len(_staged()) == 1, "exactly one successor per logical continuation"
    assert sum(record["staging"] == "staged" for record in records) == 1, \
        "one fold staged it; the other declined"
    assert first_successor in _staged(), "the first claim holds; the later fold is the loser"


@pytest.mark.parametrize("paused", [False, True])
def test_the_batch_after_an_answer_keeps_the_jobs_owner_not_the_answers_bubble(driver, paused):
    from auto_loop.execute import job_control as jobs
    from pressure_loop import cognition_queue, system_messages
    from pressure_loop.operator_chat import handle_operator_chat

    def ask(current):
        payload = agent_decision("PRESSURE")(current)
        payload["control"]["decision"] = "ASK_OPERATOR"
        payload["control"]["pressure"]["intent"] = "ASK_OPERATOR"
        return payload

    driver.agent_batches.extend([_asking_batch(0), _appending_batch(1), _appending_batch(2)])
    driver.omega.extend([ask, agent_decision("PRESSURE"), agent_decision("DONE")])
    first = driver.operator_turn("wire the export button", route="CODING_AGENT", cycle_id="owner-cycle")
    job_id = driver.view(first).outputs["agent_run"]["job_id"]
    if paused:
        driver.monkeypatch.setattr("pressure_loop.pressure_loop_pause.is_pressure_loop_paused", lambda: True)
        question_id = system_messages.current_outreach()["question_id"]
        accepted = handle_operator_chat("comma", cycle_id="different-answer-bubble", question_id=question_id)
        assert accepted["ok"] and accepted["queued"]
        assert system_messages.current_outreach(question_id=question_id) is None
        driver.monkeypatch.setattr("pressure_loop.pressure_loop_pause.is_pressure_loop_paused", lambda: False)
        queued = cognition_queue.lease_next_input(0)
        assert queued.metadata["chat_bubble_waiting"]
        driver.run_operator_chat(queued, opened_by=queued.type)
        answer = driver.run_lane_work("agent")
    else:
        answer = _answer_on_the_same_thread(driver, "comma", cycle_id="different-answer-bubble")
    [successor] = driver.staged_successors()
    assert successor.metadata["original_cycle_id"] == "different-answer-bubble"
    continued = driver.run_successor()
    assert str(continued.thread_id) == str(answer.thread_id) == str(first.thread_id)
    # start_job, task_plan, run_command: mutable work waits for the task's steps.
    assert len(driver.view(continued).outputs.get("operator_tool_calls") or ()) == 3, driver.view(continued).outputs
    assert driver.view(continued).outputs["agent_run"]["status"] == "batch_ran"
    assert all(row["status"] == "success" for row in driver.view(continued).outputs["operator_tool_calls"])
    assert jobs.get_job(job_id).status == "completed"
    assert driver.reply("different-answer-bubble")["text"]
    later = handle_operator_chat("what next?", cycle_id="later-chat")
    assert later["ok"] and later["queued"]
    queued = cognition_queue.lease_next_input(0)
    assert not queued.metadata.get("agent_question_answer")
