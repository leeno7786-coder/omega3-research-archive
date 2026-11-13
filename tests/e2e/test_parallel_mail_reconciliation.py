"""T16: parallel non-preemption — four lanes alive, one instruction, one owner, one effect.

Cross-lane cognitive integration spec, T16 and acceptance item 2. One coordinated scenario:

- an agent build is running its second batch on pipeline 2 (the continuation of an operator
  build whose job is still open),
- a browser research turn runs on pipeline 1,
- an ordinary chat turn runs on pipeline 0,
- a window reflection runs on pipeline 3,

all four open at once on the real ``EpisodeStore`` — proven by a snapshot taken when every
lane is inside its first inference — and while the agent batch is in flight the operator
sends an instruction.

What has to hold, and where each claim is measured:

1. *The batch in flight never sees it.* The batch made exactly one inference and it was sent
   before the operator spoke, the words appear in none of its tool calls, and the job takes
   no new revision (T7: ordinary chat is mail, never job control).
2. *Exactly one fold owns it.* The instruction is held as ``operator_instruction`` mail for
   the build's thread and job; only the agent continuation's Global B binds it, it renders in
   that fold's prompt exactly once, and no other fold on any lane binds, renders or edges it.
3. *Omega commits it as a user constraint with an accepted receipt*, durable in the intake
   journal and named on the transaction record.
4. *Acceptance before publication.* The record is opened only after the constraint's journal
   record exists, and the lane-entry mail is pinned only after the record exists — observed
   at the real ledger and mail-store seams as they are called.
5. *The next batch receives it before execution.* The continuation's own request carries the
   constraint, sampled before its first tool ran; then exactly one more effect lands.
6. *The other three lanes are unaffected*: they bind nothing, their prompts never mention
   the instruction, and their outcomes are what they would have been alone.
7. *No payload is applied twice*: one binding, one fold prompt, one delivery, one effect, and
   no mail left over.
8. *The causal record is durable*: operator ->modified-> decision, browser ->informed->
   decision, and the deciding fold ->continued-> the agent continuation, each as its own
   committed intake record and as an edge the real rebuild projects.

Concurrency is coordinated with a ``threading.Barrier`` and an event inside the model seat,
never with sleeps: every lane blocks at its first inference until all four are there; the
browser lane then publishes its evidence and signals; the agent lane sends the instruction
and waits for that signal before its batch returns. Both therefore land before the agent
continuation's Global B cutoff, deterministically.

Browser evidence is published by the production cognitive transaction; the test observes
the real pin and never substitutes a producer. The window reflection is a locked window
staged the way the lock seam stages one, with its ten bodies supplied through
``hydrate_reflection_window`` as the window-lane suites do; its cascade and terminus are real.

The harness is T13's ``BlueprintDriver`` with a pipeline-aware model seat: every request the
runtime socket receives carries its ``pipeline_id``, so each lane's scripted replies are keyed
by the pipeline it runs on.
"""

from __future__ import annotations

import json
import threading
from collections import deque

import pytest

from pressure_loop.contributors import EpisodeIgnition
from tests.architecture.test_window_canonical_cascade import _window_fold
from tests.e2e.test_cognitive_transaction_recovery import (
    _appending_batch,
    _effects,
    _mail_rows,
    _records,
    _staged,
    _statements,
)
from tests.e2e.test_cross_lane_thread import (
    _edges,
    _fact_record,
    _facts,
    _receipt,
    _route_by_message,
)
from tests.helpers.blueprint_driver import (
    BlueprintDriver,
    _chat_done_fold,
    agent_decision,
    assessment,
    browser_action,
    browser_decision,
)

OBJECTIVE = "build it with an export button"
BUILD_CYCLE = "bubble-t16-build"
INSTRUCTION = "also keep it in CSV column order"
INSTRUCTION_CYCLE = "bubble-t16-instruction"
BROWSE = "open it again to research streaming writers"
BROWSE_CYCLE = "bubble-t16-browse"
CHAT = "hello omega, how is your day going?"
CHAT_CYCLE = "bubble-t16-chat"
CHAT_REPLY = "Quiet so far; a build is running."
DOCS = "https://docs.invalid/streaming-writers"
EVIDENCE = "The streaming writer handles files larger than memory"
DECISION = "Export through the streaming writer"
CONSTRAINT = "Keep the CSV column order"
WINDOW_ID = "win-t16"
AGENT, BROWSER, CHAT_LANE, WINDOW = 2, 1, 0, 3


@pytest.fixture(autouse=True)
def _isolate_canonical_memory_for_every_suite(tmp_path, monkeypatch):
    """Cross-lane recall must verify actual canonical source and claim receipts."""
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    redirect_memory_to_tmp(tmp_path, monkeypatch, stub_encoding=False)


@pytest.fixture
def driver(monkeypatch, tmp_path):
    return BlueprintDriver(monkeypatch=monkeypatch, tmp_path=tmp_path)


class _LaneSeat:
    """The model seat for four lanes at once, keyed by the ``pipeline_id`` every request
    carries. Replies are the same shapes ``BlueprintDriver.generate`` returns; every request
    lands in ``driver.generations`` and every fold in ``driver.folds`` with its pipeline.

    ``gate`` is a barrier each lane waits at once, on its first request. ``hooks`` run after
    the gate, before the reply, keyed by ``(pipeline_id, operation)``.
    """

    def __init__(self, driver):
        self.driver = driver
        self.scripts: dict[int, dict[str, deque]] = {}
        self.hooks: dict[tuple[int, str], object] = {}
        self.gate = None
        self._arrived: set[int] = set()
        self._lock = threading.Lock()

    def script(self, pipeline_id, *, browser=(), batches=(), folds=()):
        self.scripts[pipeline_id] = {"auto_browser_agent": deque(browser),
                                     "auto_response": deque(batches),
                                     "episode_cognition": deque(folds)}

    def __call__(self, **kwargs):
        driver = self.driver
        operation = str(kwargs.get("operation") or "")
        pipeline_id = kwargs.get("pipeline_id")
        assert pipeline_id in self.scripts, \
            f"a request the scenario did not script: {operation} on pipeline {pipeline_id!r}"
        driver.generations.append(kwargs)
        with self._lock:
            first = pipeline_id not in self._arrived
            self._arrived.add(pipeline_id)
        if first and self.gate is not None:
            self.gate.wait()
        hook = self.hooks.get((pipeline_id, operation))
        if hook is not None:
            hook(kwargs)
        queue = self.scripts[pipeline_id].get(operation)
        assert queue, f"pipeline {pipeline_id} asked for {operation} the scenario did not script"
        scripted = queue.popleft()
        if operation == "auto_browser_agent":
            return {"ok": True, "text": "", "tool_calls": [
                {"id": f"act-{len(driver.generations)}",
                 "function": {"name": scripted["name"], "arguments": json.dumps(scripted["args"])}}]}
        if operation == "auto_response":
            return {"ok": True, **scripted}
        payload = scripted(driver) if callable(scripted) else scripted
        driver.folds.append({"system": kwargs.get("system"), "prompt": kwargs.get("prompt"),
                             "payload": payload, "pipeline_id": pipeline_id})
        return {"text": json.dumps(payload)}


def _reconciling_fold(_driver):
    """The agent continuation's fold: the operator's words become a constraint, the browser's
    evidence a decision, and the next batch gets a required injection carrying both."""
    return {
        "control": {
            "assessment": assessment("the export is not finished"),
            "decision": "PRESSURE",
            "synthesis": "Finish the export and keep the column order.",
            "pressure": {"intent": "USE_TOOL", "confidence": 0.7, "reason": "finish the export",
                         "setpoints": {}, "desired_state": "the export is wired and verified",
                         "observed_state": "one more batch is needed",
                         "resolution_conditions": ["the export verification passes"]},
        },
        "decisions": [{"statement": DECISION,
                       "rationale": "the documentation shows it handles files larger than memory"}],
        "user_constraints": [{"statement": CONSTRAINT, "source_episode_id": INSTRUCTION_CYCLE}],
        "lane_injections": [{"target_lane": "agent", "objective": "finish the export",
                             "constraints": [CONSTRAINT], "evidence_refs": [DOCS],
                             "requirement": "required"}],
    }


def _done_fold(_driver):
    return {"control": {"assessment": {**assessment(""), "gap": ""}, "decision": "DONE",
                        "synthesis": "Nothing further.", "pressure": None}}


def _stage_window(driver):
    """A locked ten-episode window, staged as the lock seam stages one, with its bodies
    hydrated the way the window-lane suites hydrate them. Returns the synthesized pressure."""
    from pressure_loop import temporal_store, window_lane
    from pressure_loop.reflection_window import WINDOW_SIZE, ReflectionWindow
    from tests.core.deepsleep_window_fixtures import episode_entry

    bodies = [episode_entry(f"t16-ep-{index}", intent="LEARN", target="Python", success=True,
                            knowledge_mastery=0.4) for index in range(WINDOW_SIZE)]
    served_episode_ids: set[str] = set()

    def unpublished(episode_ids, *, member_refs=None):
        members = tuple(dict.fromkeys(str(item) for item in episode_ids if str(item)))
        return tuple(episode_id for episode_id in members if episode_id not in served_episode_ids)

    # The bodies below are synthetic; model publication through the same explicit
    # served-membership seam as the focused window-lane tests.
    driver.monkeypatch.setattr(
        "memory.episode_commit_view.window_members_unpublished", unpublished
    )
    window = ReflectionWindow(
        window_id=WINDOW_ID, started_at_episode=bodies[0]["episode_id"],
        trajectory_snapshot={"target_domain": "python"},
        episodes=[body["episode_id"] for body in bodies],
        episode_bodies=[dict(body) for body in bodies],
        status="locked", locked_at="2026-09-19T00:00:00Z")
    assert window_lane.stage_locked_window(window)

    def hydrate(staged):
        staged.episode_bodies = [dict(body) for body in bodies]
        return staged

    driver.monkeypatch.setattr(temporal_store, "hydrate_reflection_window", hydrate)
    assert window_lane.pressure_contributor() is None, "an unpublished synthetic cohort stays held"
    served_episode_ids.update(window.episodes)
    contributor = window_lane.pressure_contributor()
    assert contributor is not None and contributor.type == window_lane.WINDOW_REFLECTION_READY
    return contributor


def _ignite_on(driver, row, pipeline_id):
    """``BlueprintDriver._ignite`` on a chosen physical pipeline."""
    import cognition_chain
    from core.pipeline_state import mark_ready
    from pressure_loop.cognition_queue import acknowledge_input, work_kind_for_contributor

    ignition = EpisodeIgnition(
        pipeline_id=pipeline_id, work_kind=work_kind_for_contributor(row),
        episode_id=f"t16-pipeline-{pipeline_id}", aggregate_pressure=1.0, opened_by=row.type,
        dominant_contributor=row, contributors=(row,), trajectory_snapshot=None)
    mark_ready(pipeline_id, ignition)
    assert acknowledge_input(row, pipeline_id), "the opened episode owns its exact queue item"
    episode = cognition_chain.ignite_episode(driver.store, ignition)
    assert episode is not None
    driver.episodes.append(episode)
    return episode


def _run_lanes(driver, rows, *, finished=None):
    """Ignite one leased row per pipeline, each on its own thread, and wait for all."""
    outcomes: dict[int, object] = {}

    def run(pipeline_id, row):
        try:
            outcomes[pipeline_id] = _ignite_on(driver, row, pipeline_id)
        except BaseException as exc:
            outcomes[pipeline_id] = exc
        finally:
            if finished and pipeline_id in finished:
                finished[pipeline_id].set()

    threads = [threading.Thread(target=run, args=(pipeline_id, row),
                                name=f"t16-pipeline-{pipeline_id}", daemon=True)
               for pipeline_id, row in rows.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)
    assert not any(thread.is_alive() for thread in threads), "a lane never finished"
    failures = {pipeline_id: outcome for pipeline_id, outcome in outcomes.items()
                if isinstance(outcome, BaseException)}
    if failures:
        pipeline_id, failure = next(iter(failures.items()))
        raise AssertionError(f"pipeline {pipeline_id} failed: {failure!r}") from failure
    return outcomes


def _lane(episode):
    return str(dict(dict(getattr(episode, "global_", None) or {}).get("intent_decision") or {})
               .get("omega_lane") or "")


def _mail_record(episode):
    return dict(dict(getattr(episode, "global_", None) or {}).get("omega_mail") or {})


def _watch_publication(driver):
    """At the two production seams — the ledger's ``open_transaction`` and the store's
    ``pin_mail`` — record which statements memory had already accepted and which records
    already existed at the instant each was called."""
    from global_loop import intersections as mail
    from pressure_loop import pressure

    seen: dict[str, list] = {"opened": [], "pinned": []}
    real_open, real_pin = pressure.open_transaction, mail.pin_mail

    def opening(record):
        seen["opened"].append((record["fold_id"], sorted(_statements())))
        return real_open(record)

    def pinning(**kwargs):
        seen["pinned"].append((str(kwargs.get("mail_id") or ""), list(_records())))
        return real_pin(**kwargs)

    driver.monkeypatch.setattr(pressure, "open_transaction", opening)
    driver.monkeypatch.setattr(mail, "pin_mail", pinning)
    return seen


def test_an_instruction_sent_mid_batch_is_reconciled_once_and_reaches_the_next_batch_first(driver):
    """Acceptance item 2, with all four lanes live."""
    from auto_loop.execute import job_control as jobs
    from global_loop import intersections as mail
    from memory import intake_journal
    from memory.causal_edges import causal_facts
    from memory.cognition_write import DERIVED_FROM_PREFIX
    from pressure_loop import reflection_window, window_lane
    from pressure_loop.cognition_queue import enqueue_input, lease_next_input
    from pressure_loop.operator_chat import handle_operator_chat
    from global_loop.intent.context import remember_action

    driver.agent_batches.append({"text": "A streaming writer would suit the CSV export.", "tool_calls": []})
    driver.omega.append(_done_fold)
    root = driver.operator_turn("discuss a streaming CSV export", route="FREEFORM_CHAT", cycle_id="t16-root")
    thread = str(root.thread_id)
    # Specialist intent is classified inside each pressure-opened Chat episode. Run those
    # Chat turns first so their target lane handoffs are durable before leasing the workers.
    specialist_chat_episodes = {}
    for text, route, cycle in (
        (OBJECTIVE, "CODING_AGENT", BUILD_CYCLE),
        (BROWSE, "BROWSER_AGENT", BROWSE_CYCLE),
    ):
        specialist_chat_episodes[route] = driver.begin_operator_turn(
            text, route=route, cycle_id=cycle, thread_id=thread)

    # Leave the ordinary Chat turn queued so it can overlap the three other lane episodes.
    _route_by_message(driver.monkeypatch, {CHAT: "FREEFORM_CHAT"})
    accepted = handle_operator_chat(CHAT, cycle_id=CHAT_CYCLE)
    assert accepted["ok"] and accepted["queued"], accepted
    first_build = lease_next_input(AGENT)
    browser_turn = lease_next_input(BROWSER)
    chat_turn = lease_next_input(CHAT_LANE)
    assert first_build.metadata["thread_id"] == browser_turn.metadata["thread_id"] == thread
    assert not chat_turn.metadata.get("thread_id")
    driver.agent_batches.append(_appending_batch(0))
    driver.omega.append(agent_decision("PRESSURE", reason="continue the export"))
    build = driver._ignite(first_build, opened_by=first_build.type)
    assert str(build.thread_id) == thread
    job = driver.view(build).outputs["agent_run"]["job_id"]
    job_before = jobs.get_job(job)
    assert (job_before.status, job_before.thread_id) == ("running", thread)
    [continuation] = driver.staged_successors()
    assert continuation.metadata["thread_id"] == thread
    effects_before = _effects(driver)
    assert len(effects_before) == 1

    driver.generations.clear()
    driver.folds.clear()
    seat = _LaneSeat(driver)
    alive: list = []
    seat.gate = threading.Barrier(
        4, action=lambda: alive.extend(driver.store.active_episodes()), timeout=120)
    evidence_published = threading.Event()
    chat_first_closed = threading.Event()
    instruction_chat_opened = threading.Event()
    published: dict = {}
    held: dict = {}
    batch_request: dict = {}
    active_at_instruction_open: list = []

    real_open = driver.store.open

    def observe_open(*args, **kwargs):
        episode = real_open(*args, **kwargs)
        if kwargs.get("pipeline_id") == CHAT_LANE and chat_first_closed.is_set():
            active_at_instruction_open.extend(driver.store.active_episodes())
            instruction_chat_opened.set()
        return episode

    driver.monkeypatch.setattr(driver.store, "open", observe_open)

    def wait_for_instruction_chat(_kwargs):
        assert instruction_chat_opened.wait(60), "Chat classified the queued operator turn"

    seat.hooks[(BROWSER, "auto_browser_agent")] = wait_for_instruction_chat
    seat.hooks[(WINDOW, "episode_cognition")] = wait_for_instruction_chat

    def operator_speaks_mid_batch(kwargs):
        batch_request["sent"] = json.dumps(kwargs, default=str)
        remember_action("CODING_AGENT", OBJECTIVE, thread_id=thread,
                        episode_id=str(build.episode_id))
        held["result"] = handle_operator_chat(INSTRUCTION, cycle_id=INSTRUCTION_CYCLE)
        held["job"] = jobs.get_job(job)
        assert held["result"]["ok"] and held["result"]["queued"], held["result"]
        assert chat_first_closed.wait(60), "the active Chat episode closes before its next FIFO turn"
        instruction_turn = lease_next_input(CHAT_LANE)
        assert instruction_turn is not None
        assert instruction_turn.metadata["cycle_id"] == INSTRUCTION_CYCLE
        chat_outcome: dict = {}

        def classify_on_chat_lane():
            try:
                chat_outcome["episode"] = driver.run_operator_chat(
                    instruction_turn, opened_by=instruction_turn.type
                )
            except BaseException as exc:
                chat_outcome["failure"] = exc

        chat_worker = threading.Thread(
            target=classify_on_chat_lane, name="t16-mid-batch-chat", daemon=True
        )
        chat_worker.start()
        chat_worker.join(timeout=300)
        assert not chat_worker.is_alive(), "the queued Chat turn did not finish"
        if "failure" in chat_outcome:
            raise AssertionError(f"the mid-batch Chat route failed: {chat_outcome['failure']!r}") \
                from chat_outcome["failure"]
        held["chat_episode"] = chat_outcome["episode"]
        held["specialist_handoff"] = driver.view(held["chat_episode"]).outputs[
            "specialist_handoff"
        ]
        held["chat_mail"] = _mail_record(held["chat_episode"])
        assert evidence_published.wait(60), "the browser lane published its evidence"

    seat.hooks[(AGENT, "auto_response")] = operator_speaks_mid_batch
    seat.script(AGENT, batches=[_appending_batch(1)], folds=[_reconciling_fold])
    seat.script(BROWSER,
                browser=[browser_action("navigate", url=DOCS),
                         browser_action("done", summary=EVIDENCE)],
                folds=[browser_decision("DONE", synthesis=EVIDENCE + ".")])
    seat.script(CHAT_LANE, batches=[{"text": CHAT_REPLY, "tool_calls": []}],
                folds=[_done_fold, _chat_done_fold])
    seat.script(WINDOW, folds=[lambda _driver: _window_fold()])
    driver.browser.pages[DOCS] = "Streaming writers"
    _route_by_message(driver.monkeypatch, {
        BROWSE: "BROWSER_AGENT", CHAT: "FREEFORM_CHAT", INSTRUCTION: "CODING_AGENT",
    })
    driver.monkeypatch.setattr("runtime.socket.invoke_runtime_generate", seat)
    watched = _watch_publication(driver)
    pin = mail.pin_mail

    def watch_evidence(**kwargs):
        row = pin(**kwargs)
        if row["purpose"] == "specialist_lane_handoff":
            published["instruction"] = row
        if row["source_actor"] == "browser" and row["purpose"] == "lane_evidence":
            published["episode_id"] = row["source_episode_id"]
            published["evidence"] = row
            evidence_published.set()
        return row

    driver.monkeypatch.setattr(mail, "pin_mail", watch_evidence)
    window = _stage_window(driver)

    rows = {BROWSER: browser_turn, CHAT_LANE: chat_turn}
    for pipeline_id, row in ((AGENT, continuation), (WINDOW, window)):
        assert enqueue_input(row, operator_activity=False)
        leased = lease_next_input(pipeline_id)
        assert leased is not None and leased.type == row.type, \
            f"pipeline {pipeline_id} leased {leased!r}"
        rows[pipeline_id] = leased

    episodes = _run_lanes(driver, rows, finished={CHAT_LANE: chat_first_closed})
    agent, browser, chat, reflection = (episodes[pipeline] for pipeline in (AGENT, BROWSER, CHAT_LANE, WINDOW))

    assert len({episode.pipeline_id for episode in alive}) == 4, alive
    assert sorted(_lane(episode) for episode in alive) == ["agent", "browser", "chat", "window"]
    assert sorted(episode.pipeline_id for episode in active_at_instruction_open) == [0, 1, 2, 3], \
        "Chat classifies the new turn while the other three lanes still own their episodes"
    assert str(agent.thread_id) == thread
    assert str(browser.thread_id) == thread
    assert str(browser.episode_id) != thread
    assert str(chat.thread_id) == str(chat.episode_id) != thread

    result = held["result"]
    assert result["ok"] and result["queued"] is True, result
    chat_handoff = held["specialist_handoff"]
    assert chat_handoff["status"] == "queued" and chat_handoff["lane"] == "agent"
    assert str(held["chat_episode"].thread_id) == thread
    assert driver.reply(INSTRUCTION_CYCLE)["text"].startswith("I’ve queued this for the Coding Agent")
    instruction_id = chat_handoff["mail_id"]
    instruction_row = published["instruction"]
    assert not held["chat_mail"].get("bound_mail_ids"), held["chat_mail"]
    assert instruction_row["purpose"] == "specialist_lane_handoff"
    assert instruction_row["target_job_id"] == job and instruction_row["target_lane"] == "agent"
    assert instruction_row["payload"]["text"] == INSTRUCTION
    assert instruction_row["payload"]["run_memory"]["status"] in {"verified", "empty"}
    agent_batches = [generation for generation in driver.generations
                     if generation.get("operation") == "auto_response" and generation.get("pipeline_id") == AGENT]
    assert len(agent_batches) == 1, "the batch made one inference, and it was already sent"
    assert INSTRUCTION not in batch_request["sent"]
    assert INSTRUCTION not in json.dumps(agent_batches[0], default=str)
    ran = driver.view(agent)
    assert INSTRUCTION not in json.dumps(ran.outputs["operator_tool_calls"], default=str)
    assert [(record["tool_name"], record["status"]) for record in ran.outputs["operator_tool_calls"]] == [
        ("start_job", "success"), ("task_plan", "success"),
        ("run_command", "success")], "the batch ran as scripted"
    assert (held["job"].latest_revision, held["job"].applied_revision) == \
        (job_before.latest_revision, job_before.applied_revision), "no job revision was written"
    assert jobs.get_job(job).latest_revision == job_before.latest_revision
    assert _effects(driver) == effects_before + ["effect-1"], "the batch's one effect, unchanged"

    binding = _mail_record(agent)
    assert binding["disposition"] == "acknowledged"
    assert sorted(binding["bound_mail_ids"]) == sorted(
        [instruction_id, published["evidence"]["mail_id"]]), \
        "the agent continuation's Global B bound the instruction and the evidence"
    agent_folds = [fold for fold in driver.folds if fold["pipeline_id"] == AGENT]
    agent_prompt = agent_folds[0]["prompt"] if len(agent_folds) == 1 else ""
    instruction_positions = [i for i in range(len(agent_prompt))
                             if agent_prompt.startswith(INSTRUCTION, i)]
    instruction_contexts = [agent_prompt[max(0, i - 100):i + len(INSTRUCTION) + 100]
                             for i in instruction_positions]
    assert len(agent_folds) == 1 and len(instruction_positions) == 2, \
        f"the next run query and its one mail row carry the instruction: {instruction_contexts!r}"
    assert agent_prompt.count(f'"text": "{INSTRUCTION}"') == 1
    assert agent_prompt.count(instruction_id) == 1
    for fold in driver.folds:
        if fold["pipeline_id"] != AGENT:
            assert instruction_id not in fold["prompt"]
            if fold["pipeline_id"] != CHAT_LANE:
                assert INSTRUCTION not in fold["prompt"]
    browser_handoff_id = driver.view(
        specialist_chat_episodes["BROWSER_AGENT"]
    ).outputs["specialist_handoff"]["mail_id"]
    assert _mail_record(browser)["bound_mail_ids"] == [browser_handoff_id]
    for other in (chat, reflection):
        assert not _mail_record(other).get("bound_mail_ids"), _mail_record(other)
        assert _mail_record(other).get("disposition") in ("", "none"), _mail_record(other)
    for other in (browser, chat, reflection):
        for fact in causal_facts(other):
            assert instruction_id not in fact["source"] and instruction_id not in fact["target"]

    constraint = _receipt(agent, "user_constraint")
    decision = _receipt(agent, "decision")
    assert constraint["status"] == "held", "accepted at the fold: durable, not yet canonical"
    journal = intake_journal.record_by_id(constraint["journal_record_id"])
    assert journal is not None
    assert str(dict(journal.payload.get("payload") or {}).get("text") or "").startswith(CONSTRAINT)
    rests_on = [str(ref)[len(DERIVED_FROM_PREFIX):]
                for ref in dict(journal.payload or {}).get("provenance") or ()
                if str(ref).startswith(DERIVED_FROM_PREFIX)]
    assert f"mail:{instruction_id}" in rests_on, "the constraint rests on the operator's words"
    assert journal.status == intake_journal.COMMITTED and journal.canonical_path, \
        "and the agent episode's own terminus drained it"

    [(fold_id, record)] = [(identity, row) for identity, row in _records().items()
                           if row["episode_id"] == str(agent.episode_id)]
    assert (record["episode_id"], record["thread_id"]) == (str(agent.episode_id), thread)
    assert set(record["receipt_ids"]) == {constraint["journal_record_id"],
                                          decision["journal_record_id"]}
    assert sorted(record["bound_mail_ids"]) == sorted(binding["bound_mail_ids"])
    assert (record["staging"], record["published"]) == ("staged", True)
    [(_fold, accepted_then)] = [(fact, snapshot) for fact, snapshot in watched["opened"] if fact == fold_id]
    assert constraint["event_id"] in accepted_then and decision["event_id"] in accepted_then, \
        "both statements were journalled before the transaction record was opened"
    [outgoing] = record["outgoing"]
    [(_mail_id, records_then)] = [(mail_id, record) for mail_id, record in watched["pinned"]
                                  if mail_id == outgoing["mail_id"]]
    assert fold_id in records_then, "the lane-entry mail was pinned after the record existed"
    assert f"intake:{constraint['journal_record_id']}" in outgoing["pin"]["provenance"]
    [lane_entry] = _mail_rows()
    assert (lane_entry["delivery_point"], lane_entry["mail_id"]) == ("lane_entry", outgoing["mail_id"])
    assert lane_entry["payload"]["user_constraints"] == [CONSTRAINT]
    assert lane_entry["payload"]["decisions"] == [DECISION]
    assert (lane_entry["thread_id"], lane_entry["target_job_id"]) == (thread, job)
    assert lane_entry["continuation_key"] == record["continuation_key"]

    browsed = driver.view(browser)
    assert browsed.lane == "browser" and browsed.run["status"] == "done"
    assert browsed.run["final_page"]["url"] == DOCS and browsed.control["decision"] == "DONE"
    assert driver.reply(BROWSE_CYCLE)["text"].startswith("I’ve queued this for the Browser")
    from pressure_loop.operator_chat_responses import get_lane_updates

    browser_updates = get_lane_updates(BROWSE_CYCLE)
    assert browser_updates is not None and browser_updates["status"] == "completed"
    assert browser_updates["updates"][-1]["text"] == EVIDENCE + "."
    chatted = driver.view(chat)
    assert chatted.lane == "chat" and chatted.control["decision"] == "DONE"
    assert driver.reply(CHAT_CYCLE)["text"] == CHAT_REPLY
    reflected = driver.view(reflection)
    assert reflected.lane == "window"
    assert reflected.outputs["window_reflection"]["window_id"] == WINDOW_ID
    assert window_lane.pressure_contributor() is None, "the window was reflected and unstaged"
    archived = json.loads((reflection_window.reflection_window_archive_dir()
                           / f"{WINDOW_ID}.json").read_text(encoding="utf-8"))
    assert archived["omega_outcome"]["reflection"]["summary"] == "Ten shallow lookups."
    [(observed_source, observed_target, _detail)] = _facts(reflection, "observed")
    assert (observed_source, observed_target) == (f"episode:{reflection.episode_id}",
                                                 f"window:{WINDOW_ID}")

    modified = (f"mail:{instruction_id}", f"intake:{decision['journal_record_id']}")
    informed = (f"mail:{published['evidence']['mail_id']}",
                f"intake:{decision['journal_record_id']}")
    assert (*modified, {"purpose": "specialist_lane_handoff", "statement_kind": "decision"}) \
        in _facts(agent, "modified")
    assert (*informed, {"purpose": "lane_evidence", "statement_kind": "decision"}) \
        in _facts(agent, "informed")
    for kind, (source, target) in (("modified", modified), ("informed", informed)):
        fact = _fact_record(kind, source, target)
        assert fact is not None, f"no durable {kind} fact"
        assert fact.status == intake_journal.COMMITTED and fact.canonical_path

    [next_successor] = _staged()
    assert next_successor == record["successor"]["metadata"]["successor_id"]
    sampled: list[tuple[str, list[str]]] = []
    seat.hooks[(AGENT, "auto_response")] = lambda kwargs: sampled.append(
        (json.dumps(kwargs, default=str), _effects(driver)))
    seat.script(AGENT, batches=[_appending_batch(2)], folds=[_done_fold])
    seat.gate = None
    effects_mid = _effects(driver)

    from pressure_loop.cognition_queue import acknowledge_input, forget_handoff_receipts

    absorbed = lease_next_input(AGENT)
    assert absorbed is not None and absorbed.type == "lane_work_dispatch"
    assert absorbed.metadata.get("mail_absorbed") is True, absorbed.metadata
    assert acknowledge_input(absorbed, AGENT), "the absorbed update does not start a duplicate run"
    forget_handoff_receipts([str(absorbed.metadata.get("handoff_id") or "")])

    continued = driver.run_successor()

    assert str(continued.thread_id) == thread
    [(request, effects_then)] = sampled
    assert CONSTRAINT in request and DECISION in request, \
        "the continuation was told the constraint and the decision"
    assert effects_then == effects_mid, "before it ran anything"
    assert _effects(driver) == effects_mid + ["effect-2"], "then exactly one more effect"
    assert _records()[fold_id]["deliveries"][outgoing["effect_id"]]["state"] == "performed"
    assert driver.view(continued).control["decision"] == "DONE"

    assert _mail_rows() == [], "instruction, evidence and injection: all consumed, none left"
    assert sum(1 for generation in driver.generations
               if generation.get("operation") == "auto_response" and generation.get("pipeline_id") == AGENT) == 2
    assert sum(fold["prompt"].count(f'"text": "{INSTRUCTION}"')
               for fold in driver.folds) == 1
    assert sum(fold["prompt"].count(instruction_id) for fold in driver.folds) == 1
    assert sum(fold["prompt"].count(CONSTRAINT) for fold in driver.folds
               if fold["pipeline_id"] != AGENT) == 0
    assert _staged() == [] and driver.staged_successors() == []
    assert len(_effects(driver)) == 3

    [(source, target, detail)] = _facts(continued, "continued")
    assert (source, target) == (f"episode:{agent.episode_id}", f"continuation:{next_successor}")
    assert detail["fold_id"] == fold_id, "the continuation names the fold that decided"
    assert decision["journal_record_id"] in _records()[fold_id]["receipt_ids"], \
        "and that fold's record names the decision it rests on"
    fact = _fact_record("continued", source, target)
    assert fact is not None and fact.status == intake_journal.COMMITTED and fact.canonical_path

    edges = _edges(driver)
    assert edges[("MODIFIED", *modified)] == f"episode-{agent.episode_id}"
    assert edges[("INFORMED", *informed)] == f"episode-{agent.episode_id}"
    assert edges[("CONTINUED", source, target)] == f"episode-{continued.episode_id}"
