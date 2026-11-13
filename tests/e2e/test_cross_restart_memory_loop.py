"""T14: the memory loop across a restart — accepted cognition survives and stays usable.

Cross-lane cognitive integration spec, T14. A fold accepts research, a decision and a
constraint, publishes its continuation, and the process restarts. Four things have to hold:

1. *The continuation resolves the accepted decision without recall.* It does not need the
   graph, the index, or a re-run of the fold: the lane-entry mail the fold published carries
   the accepted decisions and constraints BY VALUE, with an ``intake:<receipt>`` reference per
   accepted statement, and the transaction record names the same receipts. Nothing here
   replays the interrupted fold — the recorded continuation is the carrier.

   That independence is shown where it can fail: one scenario KILLS the deciding episode at
   its terminus, the step that drains and publishes, after the transaction had published the
   continuation. The projection then holds no rows and canonical memory no statement, the real
   authorized recall path is asked and has nothing to give, and the continuation still runs and
   performs its effect. The other scenarios let their episodes complete, and so claim nothing
   about the graph: an episode's own terminus publishes one.
2. *Or the dependent action is explicitly prevented.* When the evidence a required
   continuation rests on cannot be resolved — its lane-entry mail expired unread — the
   dependent action does NOT execute: a bounded refusal naming that prerequisite, and zero
   dependent effects. Both branches are asserted; neither is chosen because it passed.
3. *Acknowledged mail is not context.* Once the continuation consumed its mail, no later
   episode ON THAT THREAD can see it again, restart or not. The later episode is bound to the
   original thread the way production binds one — the continuation asks the operator a
   question, and T3's binder continues the thread recorded ON that question — and the identity
   is asserted before the exclusion, since an episode on a fresh root could not have seen the
   mail whatever the store did.
4. *Thread identity is durable, and the material becomes recallable.* Every continuation binds
   the thread the ledger persisted — checked two hops out, because a thread's ROOT episode has
   ``thread_id == episode_id`` and only the next hop distinguishes the thread from the parent.
   Then the accepted decision and constraint come back through the ordinary authorized recall
   path (``invoke_remember_tool`` → ``recall_verified``) with lane discipline intact: the
   lane's own lobe, its own denied lobes, no gate disabled.

   What recall actually rests on is the DRAIN, not a rebuild. Recall verifies against the
   canonical Dewey files ("Canonical Dewey files remain truth throughout" —
   ``memory.engine.reindex``), so a record the terminus has drained is recallable with no
   rebuild called at all: that is asserted first, then again after an explicit
   ``reindex_and_compile``, which is idempotent. Mutants confirm which step is load-bearing:
   breaking the DRAIN fails every scenario in this file, while suppressing the projection's
   own index build — carried in the sweep as a control, expected to survive — fails none of
   them. "After the rebuild, recall works" would therefore be describing a coincidence.

   And to be exact about what the two recall calls prove: they establish that recall is STABLE
   across a rebuild — the same material, before and after. They say nothing about whether the
   rebuild produced a correct graph, which is a different claim needing its own oracle (T11's
   suite has one for causal edges: canonical record in, ``MemoryEdge`` row out). Nothing here
   claims graph-projection correctness.

**The RESEARCH leg is its own scenario, because acquisition is its own episode.** A fold can
only accept research (``knowledge_to_ingest``) when its episode's action is an ACQUISITION:
``prompts.acquires_knowledge`` reads ``packet.action``, and an applied agent-lane episode's
fold contract allows no optional payload fields at all — forcing research into that fold would
be testing a contract violation. So the research leg runs the real acquisition path instead:
idle learning stages a LEARN successor (Omega's own chosen topic), Meta A resolves the topic and
selects ``web_fetch``, AUTO invokes it, and Omega's fold accepts what was read as knowledge.

Only the EXTERNAL SOURCE is stubbed — ``_default_tool_invoker`` returns a document instead of
reaching the network, and the model seat returns scripted folds as everywhere else in this
suite. Intake, persistence, the drain, the rebuild and recall are the real ones, and no record
is written directly.

Where the accepted research LANDS is worth stating, because it is not the knowledge lobe: every
cross-lane cognition write — decision, constraint, knowledge statement alike — goes through
T10's single write boundary into the SITUATIONAL lobe under a host actor, so the accepted
research is recalled with ``remember_situation``. Lobe 2 holds what DeepSleep DERIVED from the
same episode (its mastery progress and semantic facts), and that is checked too, in its own
lane. Both verdicts are ``verified`` and neither lane has a gate disabled.

The harness is T13's: the real cascade through ``BlueprintDriver`` and real stores, and its
helpers are imported rather than copied so there is one definition of what a restart means.

**A restart here is the production boot, not a cache drop.** ``_boot`` clears every in-process
cache — so each step reads disk as a fresh process would — and then calls the real
``restore_pipelines_at_boot()``, which is what a new process runs before any worker is
scheduled: it reconciles owed intake, completes an unpublished transaction record, re-queues
what is restorable and STOPS the jobs it cannot restore. That second half changes durable
state, and skipping it made every scenario here easier than a real restart (review
2026-09-19).

Including it found two production defects in the recovery path this task exists to test. Both
are fixed alongside, reported separately, and pinned by their own boundary tests rather than
by these scenarios:

1. *A boot-stopped job cancelled its own continuation.* An agent task keeps its job open
   between batches, so every restart finds one running and ``reconcile_jobs_at_boot`` stops
   them all — its own contract being that "unfinished work continues through Omega
   successors". T12's stop doctrine read that ``stopped`` STATUS as the operator's stop and
   retired the staged successor, so one restart silently cancelled work nobody stopped and the
   accepted decisions it carried were never acted on. The operator's stop is now read from
   its own durable witness — a stop revision or its receipt. Boundary tests:
   ``tests/pressure_loop/test_stop_doctrine.py``.
2. *A restored continuation could not take its job back.* Boot clears the owner and settles
   the status, so the continuation reached ``enter_writer``, was refused, and answered "this
   job already has an active writer" — while ``open_job`` already documents reopening a
   non-running job for exactly that successor. ``job_control.reclaim_writer`` now takes back a
   job whose writer died with the process, and only such a job. Boundary tests:
   ``tests/core/test_job_control.py``.
3. *And taking it back has one expected failure: the project.* A stopped job owns no project,
   so another build can take it meanwhile — ordinary contention, which ``_check_project_claim``
   reports by RAISING. Unhandled, that escaped AUTO and the whole cascade as a ``ValueError``
   (review 2026-09-19). It is now a typed ``ProjectClaimConflict`` carrying the owner, caught
   narrowly at the one caller and declared through T9's own ``cannot_consume``, so the refusal
   names whose work is in the way and leaves both job records and the undelivered mail as they
   were. Corruption (``JobRecordInvalid``) and write failures still raise: they are not
   contention. Its real-caller regression runs with the refusals in section 2 below, through
   the same cascade: publish, real boot, another job takes the project, run the continuation.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e.test_cognitive_transaction_recovery import (
    COUNTER,
    Killed,
    _appending_batch,
    _boot,
    _deliveries,
    _died_at,
    _effects,
    _mail_rows,
    _records,
    _staged,
    _statements,
    assessment,
)
from tests.helpers.blueprint_driver import BlueprintDriver, tool_call

OBJECTIVE = "wire the export button"
CYCLE = "bubble-t14"
DECISION = "Export through the streaming writer"
CONSTRAINT = "Keep the CSV column order"
RESEARCH = "The streaming writer handles files larger than memory"
FETCHED = ("Streaming writers emit rows as they go, so a file larger than memory still "
           "exports. " + RESEARCH)


@pytest.fixture(autouse=True)
def _isolate_canonical_memory_for_every_suite(tmp_path, monkeypatch):
    """Cross-lane recall must verify actual canonical source and claim receipts."""
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    redirect_memory_to_tmp(tmp_path, monkeypatch, stub_encoding=False)


@pytest.fixture
def driver(monkeypatch, tmp_path):
    return BlueprintDriver(monkeypatch=monkeypatch, tmp_path=tmp_path)


def _fold_with_a_decision(_driver):
    """One PRESSURE fold that accepts a decision and a constraint, and continues.

    NOT research: ``knowledge_to_ingest`` is an optional payload field, and an applied
    agent-lane episode's fold contract allows none — only an ACQUISITION action
    (``prompts._KNOWLEDGE_ACTIONS``) grounds a knowledge fold. The research leg of T14 is
    therefore not covered here; see the module docstring.
    """
    return {
        "assessment": assessment("the export is not wired yet"),
        "decision": "PRESSURE",
        "synthesis": "Continue with the streaming export.",
        "pressure": {"intent": "USE_TOOL", "confidence": 0.6, "reason": "wire the export",
                     "setpoints": {}, "desired_state": "the export streams",
                     "observed_state": "not wired", "resolution_conditions": ["it exports"]},
        "decisions": [{"statement": DECISION, "rationale": "it survives large files"}],
        "user_constraints": [{"statement": CONSTRAINT, "source_episode_id": CYCLE}],
        "lane_injections": [{"target_lane": "agent", "objective": "wire the streaming export",
                             "constraints": ["keep the API"], "evidence_refs": ["operator turn"],
                             "requirement": "required"}],
    }


def _fold_asking_for_operator(driver):
    payload = _fold_with_a_decision(driver)
    payload["decision"] = "ASK_OPERATOR"
    payload["synthesis"] = "Which delimiter should the export use?"
    payload["pressure"]["intent"] = "ASK_OPERATOR"
    payload["pressure"]["reason"] = "the export delimiter needs the operator's choice"
    return payload


def _fold_continue_without_mail(driver):
    payload = _fold_with_a_decision(driver)
    payload["synthesis"] = "Continue the existing export work."
    payload["decisions"] = []
    payload["user_constraints"] = []
    payload["lane_injections"] = []
    return payload


def _done_fold(_driver):
    return {"assessment": {**assessment(""), "gap": ""}, "decision": "DONE",
            "synthesis": "Nothing further.", "pressure": None}


def _script(driver, *folds, batches=3):
    driver.agent_batches.extend(_appending_batch(index) for index in range(batches))
    driver.omega.extend(folds)


def _lane_entry_mail():
    [row] = [row for row in _mail_rows() if row["delivery_point"] == "lane_entry"]
    return row


def _receipt_refs(row):
    return [ref.split(":", 1)[1] for ref in row["provenance"] if ref.startswith("intake:")]


def _staged_agent_job_id(driver):
    rows = [row for row in driver.staged_successors()
            if row.metadata.get("agent_job_id")]
    assert len(rows) == 1, (
        f"one continuation identifies its Coding Agent job: {rows}; "
        f"successors={driver.staged_successors()}; "
        f"last_auto={dict(getattr(driver.episodes[-1], 'auto', None) or {}) if driver.episodes else {}}"
    )
    return str(rows[0].metadata["agent_job_id"])


def _graph_path(driver):
    """The projection's path from the PRODUCTION resolver.

    A path written out by hand here named ``taxonomy_store/graph.sqlite`` and missed
    ``compiled/``, so "no graph exists" was asking about a file nothing writes, and passed with
    a real graph sitting beside it (review 2026-09-19).
    """
    from memory.taxonomy.graph_store import graph_path

    return graph_path(driver.harness.memory_root.parent)


def _graph_holds(driver):
    """How much the published projection contains, read the production way.

    CONTENT, not the file: ``connect_for_read`` creates an empty database when asked to read
    one that is not there, so any recall in the cascade leaves the path existing and "the file
    is absent" is an oracle of nothing. Rows are what a recall could actually find.
    """
    from memory.projection.generation import projection_read_transaction
    from memory.taxonomy import graph_store

    root = driver.harness.memory_root.parent
    with projection_read_transaction(root):
        path = _graph_path(driver)
        if not path.exists():
            return {}
        conn = graph_store.connect_readonly(path)
        try:
            return {table: graph_store.count(conn, table)
                    for table in sorted(graph_store.table_names(conn))
                    if not table.startswith("sqlite_")}
        finally:
            conn.close()


def _canonical_when_the_lane_is_asked(driver):
    """Whether ANY accepted statement had reached canonical memory each time the LANE's own
    prompt was built — the question "could recall have supplied this?" asked at the only
    instant it means anything.

    The continuation's own terminus drains and projects when it closes, so a check taken
    afterwards would be a check of the wrong moment.
    """
    sampled: list[bool] = []
    seat = driver.generate

    def watching(**kwargs):
        if str(kwargs.get("operation") or "") == "auto_response":
            sampled.append(any(record.canonical_path for record in _statements().values()))
        return seat(**kwargs)

    driver.monkeypatch.setattr("runtime.socket.invoke_runtime_generate", watching)
    return sampled


# ── 1. the continuation carries the decision, by value, with its receipts ─────

def test_the_published_continuation_carries_the_accepted_decision_by_value(driver):
    """What the consumer needs is ON the mail: the accepted statements themselves, and a
    reference to the receipt each one was accepted under.

    This episode is allowed to COMPLETE, so it makes no claim about the projection — its own
    terminus publishes one. Graph independence is the interrupted scenario below.
    """
    _script(driver, _fold_with_a_decision)

    driver.operator_turn(OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)

    row = _lane_entry_mail()
    assert row["payload"]["decisions"] == [DECISION], "the decision travels by value"
    assert row["payload"]["user_constraints"] == [CONSTRAINT], "and so does the constraint"
    assert row["payload"]["objective"] == "wire the streaming export"
    receipts = _receipt_refs(row)
    assert len(receipts) >= 2, "one intake reference per accepted statement"
    [(_fold_id, record)] = [item for item in _records().items() if item[1]["outgoing"]]
    assert sorted(record["receipt_ids"]) == sorted(receipts), \
        "the record names the same receipts the mail references"


def test_the_receipt_ids_resolve_to_the_original_accepted_content_after_a_restart(driver):
    """The references are not opaque: after a restart each one resolves, through the intake
    journal, to the statement that was accepted under it."""
    from memory import intake_journal

    _script(driver, _fold_with_a_decision)
    driver.operator_turn(OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    receipts = _receipt_refs(_lane_entry_mail())

    _boot(driver)

    resolved = []
    for record_id in receipts:
        record = intake_journal.record_by_id(record_id)
        assert record is not None, f"{record_id} no longer resolves"
        resolved.append(str(dict(record.payload.get("payload") or {}).get("text") or ""))
    assert any(text.startswith(DECISION) for text in resolved), "the decision resolves"
    assert any(text.startswith(CONSTRAINT) for text in resolved), "and the constraint resolves"


def test_the_continuation_resolves_its_decision_with_no_graph_and_no_replay(driver):
    """The continuation runs after the restart, reads the decision from its own delivered mail,
    and performs its effect — with no graph to recall from and without re-running the fold that
    decided.

    The interruption is a REAL one and it lands before the projection: the deciding episode
    dies at its terminus, the step that drains and publishes, after its transaction had already
    published the continuation durably. An episode allowed to COMPLETE publishes a graph of its
    own, so asserting anything about "no graph" after one asserted nothing (review 2026-09-19).

    Three oracles, because the obvious one is empty. The graph is measured by its ROWS, not by
    whether its file exists — a read creates an empty database, so the path is there whatever
    happened. Chat has already completed its routed acknowledgement, so the graph must match
    its post-Chat baseline rather than be empty. Canonical memory is measured at the instant the
    lane's prompt is built, since the continuation's own terminus drains when it closes. And
    the real authorized recall path is asked, before the continuation runs, whether it can
    produce the decision at all.
    """
    import json

    _script(driver, _fold_with_a_decision, _done_fold, batches=4)
    driver.begin_operator_turn(OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    graph_before_lane = _graph_holds(driver)
    revive = _died_at(driver, "memory.engine.reindex", "reindex_and_compile",
                      "died before the graph was published")

    with pytest.raises(Killed, match="died before the graph"):
        driver.run_lane_work("agent")

    assert _staged(), "the continuation was published durably before the death"
    assert _graph_holds(driver) == graph_before_lane, \
        "the interrupted Agent fold did not publish beyond Chat's projection baseline"
    assert {record.status for record in _statements().values()} == {"pending"}, \
        "the accepted statements never reached canonical memory: the terminus never ran"
    before = _effects(driver)
    seen = len(driver.generations)
    canonical_when_asked = _canonical_when_the_lane_is_asked(driver)
    _boot(driver, revive)
    assert _graph_holds(driver) == graph_before_lane, "boot does not publish the interrupted fold"

    # The ordinary authorized recall path cannot produce the decision — so whatever the
    # continuation is about to be told, it was not told by recall.
    from auto_loop.mril_tools.remember import invoke_remember_tool

    asked = invoke_remember_tool(
        {"tool": "remember_situation", "query": "how are we exporting?",
         "allowed_lobes": [4], "memory_type": "episodic"}, episode_id="t14-before-the-run")
    assert not any(DECISION in str(row.get("remembered_as") or "")
                   for row in asked.get("evidence") or ()), \
        f"recall has nothing to give: {asked.get('verdict')} {asked.get('verdict_reason')}"

    episode = driver.run_successor()

    # The delivery lands where the LANE's prompt is built — the execution batch, before any
    # tool runs (T9) — not in Omega's later fold. That is the prompt the decision has to reach.
    execution = json.dumps([row for row in driver.generations[seen:]
                            if row.get("operation") == "auto_response"], default=str)
    assert execution, "the continuation asked for its batch"
    assert DECISION in execution, "the continuation was told the decision it rests on"
    assert CONSTRAINT in execution, "and the constraint"
    assert canonical_when_asked == [False], \
        f"and nothing was in canonical memory when it was told: {canonical_when_asked}"
    assert len(_effects(driver)) == len(before) + 1, "it executed, exactly once"
    assert str(getattr(episode, "thread_id", "")) != "", "and it ran as a real episode"


# ── 2. or the dependent action is explicitly prevented ───────────────────────

def test_required_lane_evidence_survives_elapsed_time_and_runs(driver):
    """Required lane mail stays durable past its former TTL and remains usable after restart."""
    import time as _time

    from global_loop import intersections as mail

    _script(driver, _fold_with_a_decision, _done_fold, batches=4)
    driver.operator_turn(OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    row = _lane_entry_mail()
    assert row["requirement"] == "required" and row["payload"]["decisions"] == [DECISION]
    before = _effects(driver)
    _boot(driver)

    shifted = _time.time() + 48 * 3600
    driver.monkeypatch.setattr(mail, "time", type("clock", (), {"time": staticmethod(lambda: shifted)}))
    retained = [r for r in mail.pending_mail() if r["mail_id"] == row["mail_id"]]
    assert len(retained) == 1 and retained[0]["status"] == "ready", \
        "elapsed time cannot expire unacknowledged required evidence"

    episode = driver.run_successor()

    assert len(_effects(driver)) == len(before) + 1, "the dependent action uses retained evidence"
    auto = dict(getattr(episode, "auto", None) or {})
    assert not str(auto.get("failure_kind") or "").startswith("lane_entry_"), \
        "retained required evidence did not become a false prerequisite failure"


def _competing_owner(driver, job_id="review-competing-owner"):
    """Another build takes the project while the interrupted job is stopped.

    It can: the project is UNOWNED between the boot stop and the continuation, which is the
    window this refusal is about.
    """
    from auto_loop.execute import job_control as jobs

    return jobs.open_job(job_id, "other-bubble", "another build",
                         driver.harness.workspace_root, project_root=".")


def test_a_continuation_whose_project_another_job_took_is_refused_not_crashed(driver):
    """The one expected failure of taking a job back: someone else owns its project now.

    ``reclaim_writer`` re-checks the project claim, and that check RAISES — ordinary
    contention, since the project was unowned while the job was stopped. Unhandled, it escaped
    AUTO and the whole cascade as a ``ValueError`` (review 2026-09-19). It has to be a bounded
    refusal that names the owner and leaves everything recoverable: both job records as they
    were, the required mail still undelivered, and no effect performed.
    """
    from auto_loop.execute import job_control as jobs

    _script(driver, _fold_with_a_decision, _done_fold, batches=4)
    driver.operator_turn(OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    job_id = _staged_agent_job_id(driver)
    before = _effects(driver)
    interrupted = jobs.get_job(job_id)
    _boot(driver)
    competing = _competing_owner(driver)
    assert competing.project_root == interrupted.project_root, "the same project, now theirs"

    episode = driver.run_successor()             # nothing escapes: that is the fix

    auto = dict(getattr(episode, "auto", None) or {})
    assert auto.get("failure_kind") == "lane_entry_not_consumed", \
        f"a bounded lane-entry refusal, not an exception: {auto.get('failure_kind')!r}"
    assert competing.job_id in str(auto.get("failure_reason") or ""), \
        f"and it says whose work is in the way: {auto.get('failure_reason')!r}"
    assert _effects(driver) == before, "no dependent effect ran"
    assert [(row["status"], row["delivery_point"]) for row in _mail_rows()] == \
        [("ready", "lane_entry")], "the required mail is undelivered and still there"
    assert [row["state"] for row in _deliveries(_publishing_fold()).values()] == ["abandoned"], \
        "the effect it would have carried is abandoned, not performed"
    taken_back = jobs.get_job(job_id)
    assert (taken_back.status, taken_back.owner_cycle_id) == ("stopped", ""), \
        "the interrupted job is exactly as boot left it"
    assert taken_back.project_root == interrupted.project_root, "its project binding intact"
    assert jobs.get_job(competing.job_id) == competing, "and the owning job is untouched"


# ── 3. acknowledged mail is not context ──────────────────────────────────────

QUESTION = "Which delimiter should the export use?"


def _asking_batch(index: int):
    """The continuation's batch: it appends its effect, then ASKS the operator.

    Asking is what leaves a durable thread to come back to. The alternatives do not survive
    this scenario: a job that has settled is no longer an ``active_jobs()`` candidate for the
    binder, a job still running turns every ordinary turn into held instruction mail rather
    than an episode, and the recent-route context a referential follow-up would use is
    process-local — a new process has none. The question record carries its thread on disk.
    """
    return {"text": "", "tool_calls": [
        tool_call("start_job", {"project_root": "."}, f"claim-{index}"),
        # Mutable work waits for a plan, so the batch writes its one step down first.
        tool_call("task_plan", {"steps": ["append the effect"]}, f"plan-{index}"),
        tool_call("run_command",
                  {"command": f"Add-Content -Path {COUNTER} -Value effect-{index}"},
                  f"effect-{index}"),
        tool_call("ask_operator", {"question": QUESTION, "options": ["comma", "tab"]},
                  f"ask-{index}")]}


def _answer_on_the_same_thread(driver, text, *, cycle_id, route="CODING_AGENT"):
    """The operator's answer to that question, through the real public ingress.

    T3's own first rule: an answer to the agent's question continues the thread recorded ON
    the question. ``operator_turn`` builds its contributor directly and so always roots a NEW
    thread, which made a "later episode on the same thread" a different thread entirely
    (review 2026-09-19).
    """
    from pressure_loop.cognition_queue import lease_next_input
    from pressure_loop.operator_chat import handle_operator_chat
    from pressure_loop.system_messages import current_outreach
    from tests.helpers.intent_router import stub_router

    pending = current_outreach()
    assert pending is not None and pending["kind"] == "agent_question", \
        f"the Coding Agent left a question to answer: {pending}"
    stub_router(driver.monkeypatch, route)
    handle_operator_chat(text, cycle_id=cycle_id, question_id=pending["question_id"])
    leased = lease_next_input(0)
    assert leased is not None, "the answer is a real turn, not held mail"
    assert leased.metadata.get("operator_route_pin") == route
    assert leased.metadata.get("agent_question_answer") == text
    chat_episode = driver.run_operator_chat(leased, opened_by=leased.type)
    chat_route = dict(chat_episode.global_ or {}).get("operator_route") or {}
    assert chat_route.get("lane") == "agent", (
        f"the answered Coding Agent question must route through Chat to its lane: "
        f"route={chat_route}; intent={dict(chat_episode.global_ or {}).get('intent_decision')}; "
        f"auto={dict(chat_episode.auto or {})}"
    )
    chat_auto = dict(chat_episode.auto or {})
    chat_outputs = dict(chat_auto.get("outputs") or {})
    assert chat_outputs.get("specialist_handoff"), (
        f"the Chat response must durably admit the answer to the Coding Agent FIFO: "
        f"action={chat_auto.get('action')}; failure={chat_auto.get('failure_kind')}:"
        f"{chat_auto.get('failure_reason')}; output_keys={sorted(chat_outputs)}"
    )
    return driver.run_lane_work("agent")


def test_acknowledged_mail_cannot_re_enter_context_after_a_restart(driver):
    """The continuation consumed its mail. A later episode ON THE SAME THREAD — after a
    restart, with the store reloaded from disk — cannot bind it again.

    The thread identity is asserted FIRST, because an episode on a new root could not see the
    mail whatever the store did, and would prove nothing about exclusion.
    """
    from omega_loop.episode.packet import held_omega_mail

    driver.omega.extend([_fold_with_a_decision, _fold_continue_without_mail, _done_fold])
    driver.agent_batches.extend(_appending_batch(index) for index in range(6))
    driver.operator_turn(OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    thread = _lane_entry_mail()["thread_id"]
    consumed = _lane_entry_mail()["mail_id"]
    _boot(driver)

    seen = len(driver.generations)
    continuation = driver.run_successor()
    continuation_prompts = [row for row in driver.generations[seen:]
                            if row.get("operation") == "auto_response"]
    assert continuation_prompts and DECISION in str(continuation_prompts), \
        "the post-restart Coding Agent received its required lane evidence"
    assert _mail_rows() == [], "the delivered mail was acknowledged by that Agent episode"

    _boot(driver)
    seen = len(driver.generations)
    later = driver.run_successor()
    later_prompts = [row for row in driver.generations[seen:]
                     if row.get("operation") == "auto_response"]

    assert str(getattr(continuation, "thread_id", "")) == thread
    assert str(getattr(later, "thread_id", "")) == thread, \
        "the later continuation stays on the thread the mail was addressed to"
    assert later_prompts and DECISION not in str(later_prompts), \
        "acknowledged evidence does not re-enter a later same-thread prompt"
    assert held_omega_mail(later) == (), "and no acknowledged mail re-enters the fold"
    assert _mail_rows() == [] and consumed, "the store has nothing left to hand out"


# ── 4. the thread is durable, and the material becomes recallable ────────────

def test_the_continuation_binds_the_thread_the_ledger_persisted(driver):
    """Thread identity survives the restart because it is on the durable successor row, not in
    memory: each continuation runs on the thread the FIRST episode rooted.

    The second continuation is what makes this a real test. The first episode IS its thread's
    root (``thread_id == episode_id``), so binding "the thread" and binding "the parent
    episode" are indistinguishable there; by the next hop they differ, and only the persisted
    thread keeps the chain on one thread.
    """
    _script(driver, _fold_with_a_decision, _fold_with_a_decision, _done_fold, batches=6)
    episode = driver.operator_turn(OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    thread = str(getattr(episode, "thread_id", "") or "")
    assert thread and _lane_entry_mail()["thread_id"] == thread
    _boot(driver)
    [staged] = list(_pressure().staged_signals())
    assert staged.metadata["thread_id"] == thread, "the ledger persisted the thread"

    continuation = driver.run_successor()

    assert str(getattr(continuation, "thread_id", "")) == thread, \
        "the continuation ran on it, not on a new root"
    assert str(getattr(continuation, "episode_id", "")) != thread, \
        "and its own id is not the thread, so the next hop can tell them apart"
    _boot(driver)
    [next_staged] = list(_pressure().staged_signals())
    assert next_staged.metadata["thread_id"] == thread, \
        "the second continuation is on the SAME thread, not on its parent episode"
    assert next_staged.metadata["parent_episode_id"] != thread, "which is a different value"


def _pressure():
    from pressure_loop import pressure

    return pressure


def _publishing_fold():
    """The one transaction record that published outgoing mail in these scenarios."""
    [fold_id] = [key for key, record in _records().items() if record["outgoing"]]
    return fold_id


def test_the_accepted_material_is_recallable_after_the_real_drain_and_rebuild(driver):
    """The last leg of the loop: what the fold accepted becomes recallable through the ordinary
    authorized path.

    The per-episode terminus already drained the statements to the Dewey filesystem; this
    rebuilds the projection with the real ``reindex_and_compile`` and then asks
    ``invoke_remember_tool`` — the lane's own recall tool, which builds its ``QuerySpec`` with
    its own lobe and its own denied lobes. No gate is disabled and no lookup is substituted,
    and the oracle is keyed to the CANONICAL IDS the drain produced, so a recall that returned
    something else could not satisfy it.
    """
    from auto_loop.mril_tools.remember import invoke_remember_tool
    from memory.engine.reindex import reindex_and_compile
    from memory.recall_verifier import verdict_log_path

    _script(driver, _fold_with_a_decision)
    driver.operator_turn(OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    committed = {}
    for event, record in _statements().items():
        assert record.canonical_path, f"{event} was not drained to canonical memory"
        committed[event.rsplit(":", 2)[-2]] = record.memory_id or record.canonical_path
    assert sorted(committed) == ["constraint", "decision"], "both statements landed"
    _boot(driver)

    # The episode's own terminus already PROJECTED what it drained, so recall works here
    # without a separate rebuild; calling one is idempotent, and both states are asserted so
    # neither can rot. (An explicit rebuild is what a cold boot would do.)
    def ask(cycle: str, query: str):
        return invoke_remember_tool(
            {"tool": "remember_situation", "query": query,
             "allowed_lobes": [4], "memory_type": "episodic"}, episode_id=cycle)

    straight_after_the_restart = ask("t14-recall-no-rebuild", "how are we exporting?")
    assert straight_after_the_restart["verdict"] == "verified", \
        straight_after_the_restart.get("verdict_reason")
    reindex_and_compile(driver.harness.memory_root.parent)
    recalled = ask("t14-recall", "how are we exporting?")
    constraint = ask("t14-constraint-recall", "which column order should the CSV keep?")

    assert recalled["verdict"] == "verified", recalled.get("verdict_reason")
    assert (recalled["lane"], recalled["lobe"]) == ("situational", 4), \
        "the lane's own scope answered, with its own lobe"
    remembered = {str(row.get("memory_id")): str(row.get("remembered_as") or "")
                  for row in recalled["evidence"]}
    assert committed["decision"] in remembered, "the decision came back by its canonical id"
    assert remembered[committed["decision"]].startswith(DECISION)
    constraint_memory = {str(row.get("memory_id")): str(row.get("remembered_as") or "")
                         for row in constraint["evidence"]}
    assert constraint["verdict"] == "verified", constraint.get("verdict_reason")
    assert committed["constraint"] in constraint_memory, "the focused query finds the constraint"
    assert constraint_memory[committed["constraint"]].startswith(CONSTRAINT)

    # And the recall left a durable audit row naming WHAT it grounded on, by canonical id —
    # so the answer can be re-derived from disk without the in-process verdict object. The
    # MRIL tool stamps its QuerySpec ``<episode_id>-mril-<tool>``, which is what distinguishes
    # this ask from the pre-rebuild one; the gate's row is the last one that ask appended.
    log = verdict_log_path(root=driver.harness.memory_root.parent)
    rows = [json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    grounded = [row for row in rows
                if str(row.get("cycle_id") or "").startswith((
                    "t14-recall-mril", "t14-constraint-recall-mril"))]
    assert grounded, "the authorized recall wrote verdict rows for both focused asks"
    assert all(row["status"] == "verified" for row in grounded)
    verified_ids = {memory_id for row in grounded for memory_id in row["verified_memory_ids"]}
    assert committed["decision"] in verified_ids, "the row names the decision it grounded on"
    assert committed["constraint"] in verified_ids, "and the constraint"


# ── the research leg: a real read/fetch acquisition episode ──────────────────

def _learning_topic(driver, *, domain="python", topic="streaming writers"):
    """Idle learning's own awakening: Omega chose this topic, so Meta A resolves it.

    This is the production acquisition path (``stage_idle_learning_successor`` →
    ``_resolve_omega_chosen_topic`` → ``_learn_default_tool`` = ``web_fetch``), not a
    test-only shortcut into the knowledge lane.
    """
    from pressure_loop import idle_wake
    from pressure_loop.omega_successor import stage_idle_learning_successor

    driver.monkeypatch.setattr(idle_wake, "governor_enabled", lambda: True)
    staged = stage_idle_learning_successor(
        intent="LEARN", domain=domain, topic=topic, level=0.4, lever="mastery_debt_delta",
        reasoning=f"{topic} is the gap in {domain}", observed="no working example yet")
    assert staged is not None and staged.metadata["directive_intent"] == "LEARN"
    return staged


def _fetches_the_document(driver):
    """The EXTERNAL source, and only that: the tool invocation returns a document."""
    import importlib

    # ``auto_loop.execute.run`` the MODULE, not the function of the same name the package
    # re-exports.
    auto_run = importlib.import_module("auto_loop.execute.run")
    calls: list[tuple[str, dict]] = []
    selected_url = "https://example.invalid/streaming"
    driver.research_batches.extend([
        {"text": "", "tool_calls": [tool_call("web_fetch", {"query": selected_url}, "read-document")]},
        {"text": "The document explains how streaming writers behave.", "tool_calls": []},
    ])

    def invoker(tool_name, args, **_kwargs):
        assert driver.generations[-1]["operation"] == "web_research"
        assert args["query"] == selected_url
        calls.append((str(tool_name), dict(args or {})))
        return {"ok": True, "elapsed_ms": 12,
                "result": {"query": selected_url, "domain": "python", "results": [
                    {"url": selected_url, "title": "Streaming writers", "text": FETCHED,
                     "content": FETCHED, "source": "example.invalid"}]}}

    driver.monkeypatch.setattr(auto_run, "_default_tool_invoker", invoker)
    return calls


def _knowledge_fold(driver):
    """The acquisition fold: what was read becomes accepted knowledge, and nothing continues.

    The control is WRAPPED here, unlike the flat folds above: the control envelope accepts no
    unknown siblings, and ``knowledge_to_ingest`` is lane payload rather than part of it.
    """
    request = driver.generations[-1]
    assert request["operation"] == "episode_cognition"
    assert FETCHED in str(request.get("prompt") or ""), "Omega must read the acquired evidence"
    return {
        "control": {
            "assessment": {**assessment(""), "gap": ""},
            "decision": "DONE",
            "synthesis": "Read how streaming writers behave.",
            "pressure": None,
        },
        "knowledge_to_ingest": [{"domain": "python", "statement": RESEARCH, "confidence": 0.8}],
    }


def _knowledge_records():
    """Intake journal records for accepted knowledge statements (fold-stable ids)."""
    from memory import intake_journal

    return {record.event_id: record for record in intake_journal.records()
            if ":knowledge:" in record.event_id}


def test_an_acquisition_episode_accepts_what_it_read_through_the_real_intake_path(driver):
    """The research leg's first half: a LEARN episode reads a document and its fold accepts a
    statement about it — through ``ingest_knowledge_products`` and the intake journal, with no
    record written by the test."""
    driver.omega.extend([_knowledge_fold] * 4)
    calls = _fetches_the_document(driver)
    _learning_topic(driver)

    episode = driver.run_successor()

    assert [name for name, _args in calls] == ["web_fetch"], "AUTO selected the read tool and URL"
    assert not driver.research_batches, "the model saw the fetched document before the fold"
    outputs = dict(dict(getattr(episode, "auto", None) or {}).get("outputs") or {})
    assert outputs.get("tool_status") == "success", outputs.get("tool_error")
    receipts = [dict(row) for row in
                dict(dict(getattr(episode, "global_", None) or {}).get("omega_result") or {})
                .get("knowledge_receipts") or ()]
    assert receipts, "the fold's knowledge products produced receipts"
    assert any(row.get("accepted") for row in receipts), f"and one was accepted: {receipts}"
    accepted = [row for row in receipts if row.get("accepted")]
    assert any(RESEARCH[:40] in str(row.get("statement") or row.get("text") or "")
               for row in accepted), f"the statement it read: {accepted}"


def test_the_accepted_research_is_recallable_after_a_restart_drain_and_rebuild(driver):
    """The research leg's second half, end to end: after the restart the real drain's canonical
    record is rebuilt into the projection and the accepted research comes back through the
    ordinary authorized recall path, by the canonical id the drain produced.

    It comes back through the SITUATIONAL lane, not the knowledge lane, and that is the
    design: every cross-lane cognition write — decision, constraint, knowledge statement —
    goes through T10's one write boundary into the situational lobe under a host actor. Lobe 2
    carries what DeepSleep DERIVED from the episode (its mastery progress and semantic facts),
    which this also checks, so the loop is shown reaching both. Neither lane has a gate
    disabled and no lookup is substituted.
    """
    from pathlib import Path

    from auto_loop.mril_tools.remember import invoke_remember_tool
    from memory.engine import walker
    from memory.engine.reindex import reindex_and_compile

    driver.omega.extend([_knowledge_fold] * 4)
    _fetches_the_document(driver)
    _learning_topic(driver)
    driver.run_successor()
    committed = list(_knowledge_records().values())
    assert committed, "the accepted knowledge is journalled"
    assert all(record.canonical_path for record in committed), \
        "and the episode's terminus drained it to the Dewey filesystem"
    ids = {record.memory_id or record.canonical_path for record in committed}
    walker.load_coord_index()
    canonical_coordinates = {}
    for memory_id in ids:
        canonical_path = walker.path_for_id(str(memory_id))
        assert canonical_path, f"accepted canonical identity is unresolved: {memory_id}"
        canonical = json.loads(Path(canonical_path).read_text(encoding="utf-8"))
        assert canonical.get("id") == memory_id, canonical_path
        canonical_coordinates[memory_id] = list(canonical.get("coord") or [])
    assert all(coord[:1] == [4] for coord in canonical_coordinates.values()), \
        f"accepted cognition must have situational coordinate X=4: {canonical_coordinates}"
    receipts = [record.record_id for record in committed]
    _boot(driver)

    # The receipt resolves to the statement it accepted, across the restart, exactly as the
    # decision's does — the accepted research is identified by its own receipt, never by
    # whatever DeepSleep happened to derive.
    from memory import intake_journal

    for record_id in receipts:
        record = intake_journal.record_by_id(record_id)
        assert record is not None, f"{record_id} no longer resolves"
        text = str(dict(record.payload.get("payload") or {}).get("text") or "")
        assert text.startswith(RESEARCH), f"the receipt names the accepted statement: {text[:120]}"

    reindex_and_compile(driver.harness.memory_root.parent)      # idempotent; see above
    recalled = invoke_remember_tool(
        {"tool": "remember_situation", "query": "how do streaming writers handle big files?",
         "allowed_lobes": [4], "memory_type": "episodic"},
        episode_id="t14-research-recall")

    assert recalled["verdict"] == "verified", recalled.get("verdict_reason")
    assert (recalled["lane"], recalled["lobe"]) == ("situational", 4)
    remembered = {str(row.get("memory_id")): str(row.get("remembered_as") or "")
                  for row in recalled["evidence"]}
    hit = ids & set(remembered)
    assert hit, f"the accepted research came back by its canonical id: {sorted(remembered)}"
    assert any(remembered[memory_id].startswith(RESEARCH) for memory_id in hit), \
        f"and it is the statement that was accepted: {[remembered[m] for m in hit]}"

    derived = invoke_remember_tool(
        {"tool": "remember_knowledge", "query": "streaming writers in python",
         "allowed_lobes": [2], "memory_type": "episodic"},
        episode_id="t14-research-derived")

    assert derived["verdict"] == "verified", derived.get("verdict_reason")
    assert (derived["lane"], derived["lobe"]) == ("knowledge", 2)
    facts = [str(row.get("remembered_as") or "") for row in derived["evidence"]]
    assert any("semantic_fact" in fact for fact in facts), \
        f"DeepSleep's derived knowledge is recallable in its own lane too: {facts}"
    assert not (ids & {str(row.get("memory_id")) for row in derived["evidence"]}), \
        "and it is SEPARATE material: the derived products are not the accepted record"
    assert not any(fact.startswith(RESEARCH) for fact in facts), \
        "so a derived fact can never stand in for the accepted research"
