"""T13: the three crash boundaries of the cognitive transaction, through the real paths.

Cross-lane cognitive integration spec, section 6. A fold accepts statements into memory, opens
one durable transaction record, publishes its mail and stages its continuation. A crash can
land between any two of those, and section 6 names three boundaries:

- **B1 — after acceptance, before the transaction record.** No mail is published and no
  successor is staged. The accepted intake survives via ``reconcile_at_boot`` →
  ``claim_released_records`` → a later drain. The conclusion may be re-derived; no external
  effect occurred.
- **B2 — after the record, before staging.** The record exists with staging ``pending``. Boot
  finds it and the successor is staged from the record exactly once. Published mail keeps its
  ``effect_id``, so the external action is not repeated.
- **B3 — after staging, before acknowledgement.** The lease expires and the mail returns to
  ``ready``; redelivery resolves to the same ``effect_id``, so the lane may rebuild its prompt
  but will not re-perform the effect.

**Real paths, not helper calls.** Every scenario drives the actual cascade
(``cognition_chain.ignite_episode`` through ``BlueprintDriver``) against the real intake
journal, the real mail store, the real successor ledger and the real pipeline envelopes. The
fault is injected by making a real call site fail.

**What "the process died" means here, exactly.** A cascade exception is NOT a process death:
the chain's ``finally`` still runs the DeepSleep terminus (by design — a failed fold still
closes its episode, and the pressure loop's fatal relay crashes the brain on its next tick),
and that terminus drains the accepted intake straight to canonical. A dead process runs
neither the boundary nor the terminus, so ``_died_at`` injects both: the boundary call raises
(a ``Killed`` ``BaseException``, so no handler tidies up), and the terminus does not complete.
The durable state that survives is then what a killed process leaves — accepted intake still
owed in the journal, its bucket gone. ``_restart()`` drops every in-process cache, so the next
step reads disk as a fresh process would.

At B3 even that is not enough, because ``LaneEntry.close`` runs from a ``finally`` in AUTO: any
unwinding abandons the effect and hands the mail back, which is a different durable state from
a death. That boundary therefore uses a REAL process: ``_t13_dying_child`` drives the lane
entry against this test's own stores, blocks inside the acknowledgement, and the test KILLS it
there — no unwinding, no cleanup, the lease still held.

**An observable effect, not an inference count.** The lane's batches call ``run_command``
through the real operator tool, the portable shell and a real subprocess, APPENDING a line to
a file in the workspace. Recovery assertions count those lines. Model requests would only
prove the lane was invoked, and a rerun that repeated an action would look identical to one
that refused it.

**Two distinct mechanisms, never conflated.** ``restore_pipelines_at_boot()`` is the
AUTOMATIC PRODUCTION recovery: it reconciles owed intake, completes an unpublished transaction
record (B2), and re-queues only what is restorable. Re-running a *fold* is not wired to
anything in production — once AUTO has acted, ``mark_acted`` drops the continuation — so where
section 6 says a conclusion "may be re-derived", that replay is TEST-DRIVEN: the test re-ignites
an episode on the same contributor identity itself. Each test says which of the two it is
exercising.

**What these tests do NOT establish** (kept explicit rather than implied by green tests):

1. *No production replay trigger.* Nothing in the runtime re-runs an interrupted fold. B1's
   "the conclusion may be re-derived" is therefore verified as a PERMITTED replay on the same
   identity, not as recovery the system performs by itself. Spec section 6 says the same.
2. *Whole-result effect idempotence is unproven.* These tests assert that the effects a
   boundary can repeat are not repeated — mail pins, staged successors, canonical records,
   lane deliveries, job revisions. They do not prove that re-applying a frozen result is
   idempotent across every side effect it can reach (the motor-attempt record, the build
   report, the trajectory write run outside the ownership fence, per section 6).
3. *A replay cannot recover an agent-lane CONTINUATION, only its acceptance.* Boot stops the
   job of an episode it cannot restore (AUTO had acted, so no continuation was kept), and a
   replayed batch's ``start_job`` is then refused as a job-reopen thread conflict — a blocked
   batch, which cannot be continued. So B1's replay re-derives the conclusion and keeps every
   accepted statement, but stages nothing. That is asserted, not assumed, and it is the same
   gap section 6 already names: re-running an interrupted fold "needs its own task".
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import pipeline_state

from tests.helpers.blueprint_driver import BlueprintDriver, assessment, tool_call

OBJECTIVE = "add a delete button"
CYCLE = "bubble-t13"
DECISION = "Put the delete button in the toolbar"
CONSTRAINT = "Keep the toolbar layout"


@pytest.fixture
def driver(monkeypatch, tmp_path):
    return BlueprintDriver(monkeypatch=monkeypatch, tmp_path=tmp_path)


def _delta_fold(_driver):
    """One PRESSURE fold with statements, a required injection and a continuation."""
    return {
        "assessment": assessment("the button is not built yet"),
        "decision": "PRESSURE",
        "synthesis": "Continue with the delete button.",
        "pressure": {"intent": "USE_TOOL", "confidence": 0.6, "reason": "finish the button",
                     "setpoints": {}, "desired_state": "the button works",
                     "observed_state": "not started", "resolution_conditions": ["it renders"]},
        "decisions": [{"statement": DECISION, "rationale": "the operator asked for it there"}],
        "user_constraints": [{"statement": CONSTRAINT, "source_episode_id": CYCLE}],
        "lane_injections": [{"target_lane": "agent", "objective": "build the delete button",
                             "constraints": ["keep the API"], "evidence_refs": ["operator turn"],
                             "requirement": "required"}],
    }


def _other_delta_fold(_driver):
    """A DIFFERENT conclusion from the same fold identity — what a replay must never publish."""
    fold = _delta_fold(_driver)
    fold["decisions"] = [{"statement": "Put the delete button in a menu",
                          "rationale": "a second look says a menu"}]
    fold["synthesis"] = "Actually, a menu."
    return fold


def _done_fold(_driver):
    """A carrier episode that concludes and decides nothing of its own."""
    return {
        "assessment": {**assessment(""), "gap": ""},
        "decision": "DONE",
        "synthesis": "Nothing further.",
        "pressure": None,
    }


COUNTER = "effects.txt"


def _appending_batch(index: int):
    """A batch whose tool call APPENDS a line through the real execution boundary.

    An external effect has to be counted where it lands, not where it was asked for: model
    requests prove the lane was invoked, and a rerun that repeats an action would look
    identical to one that refused it. ``run_command`` goes out through the operator tool, the
    portable shell and a real subprocess, and appending is the one shape that cannot hide a
    second run behind an identical result (review 2026-09-19).
    """
    return {"text": "", "tool_calls": [
        tool_call("start_job", {"project_root": "."}, f"claim-{index}"),
        # Mutable work waits for a plan, so the batch writes its one step down first.
        tool_call("task_plan", {"steps": ["append the effect"]}, f"plan-{index}"),
        tool_call("run_command",
                  {"command": f"Add-Content -Path {COUNTER} -Value effect-{index}"},
                  f"effect-{index}")]}


def _replay_batch(driver):
    """A batch for a REPLAYED episode: it says something and touches nothing.

    A replay must add no external effect, so the effect counter stays the original attempt's —
    and a replayed batch cannot re-claim the project a crashed episode still owns, which would
    block it and hide the cognition the replay is meant to re-derive.
    """
    driver.agent_batches.append({"text": "nothing further to run", "tool_calls": []})


def _effects(driver):
    """Every line the lane's real tool calls appended — the external effects that happened."""
    counter = driver.harness.workspace_root / COUNTER
    if not counter.is_file():
        return []
    return [line for line in counter.read_text(encoding="utf-8").splitlines() if line.strip()]


def _script(driver, *folds, batches=2):
    driver.agent_batches.extend(_appending_batch(index) for index in range(batches))
    driver.omega.extend(folds)


class Killed(BaseException):
    """What a killed process "raises": nothing downstream handles it.

    Deliberately a ``BaseException``. The code under test is careful about failures — a
    delivery whose acknowledgement cannot be written REFUSES, abandons its effect and hands
    the mail back; a fold that fails still closes its episode through the terminus. Those are
    orderly failures with their own (correct) durable states. A process death is not one of
    them: it runs no handler, no cleanup and no terminus, and the state it leaves behind is
    what section 6's boundaries are about. An ``Exception`` here would measure the orderly
    path and call it a crash.
    """


def _died_at(driver, module_path: str, attribute: str, note: str = "the process died"):
    """Model a process death at one boundary: that call never lands, and neither does the
    terminus a dead process would never have reached.

    Returns a switch — ``revive()`` — because the process that comes back after the crash is a
    HEALTHY one. Never ``monkeypatch.undo()``: that would also revert the driver's own model
    seat and store isolation, and the recovery would run against a brain with no model.
    """
    import cognition_chain

    dying = {"on": True}

    def kill(target, attribute, suffix=""):
        *owners, attribute = attribute.split(".")
        for owner in owners:
            target = getattr(target, owner)
        real = getattr(target, attribute)

        def dead(*args, **kwargs):
            if dying["on"]:
                raise Killed(f"{note}{suffix}")
            return real(*args, **kwargs)

        driver.monkeypatch.setattr(target, attribute, dead)

    kill(__import__(module_path, fromlist=[attribute.split(".")[0]]), attribute)
    kill(cognition_chain, "_run_per_episode_terminus", ": the terminus never ran")
    return lambda: dying.update(on=False)


def _leased_turn(driver, text: str, *, route: str, cycle_id: str):
    """Finish Chat routing, then lease the target Agent item without igniting it."""
    from pressure_loop.cognition_queue import lease_next_input

    driver.begin_operator_turn(text, route=route, cycle_id=cycle_id)
    leased = lease_next_input(2)
    assert leased is not None and leased.metadata.get("lane") == "agent"
    return leased


def _requeue_for_test_replay(turn):
    """Re-admit the same agent identity for the explicitly test-driven replay."""
    from pressure_loop.cognition_queue import enqueue_restore, lease_next_input

    assert enqueue_restore(turn)
    replay = lease_next_input(2)
    assert replay is not None and replay.queue_id == turn.queue_id
    return replay


def _bind_replay_to_initial_job(turn):
    """Give the test-driven replay the same run identity the interrupted episode created."""
    from auto_loop.execute.job_control import active_jobs

    thread_id = str(turn.metadata.get("thread_id") or "")
    matches = [job for job in active_jobs() if job.thread_id == thread_id]
    assert len(matches) == 1, f"expected one initial Agent job for thread {thread_id!r}"
    turn.metadata["agent_job_id"] = matches[0].job_id
    turn.metadata["job_cycle_id"] = matches[0].cycle_id


def _restart(driver=None):
    """The process died: every in-process cache goes, the durable stores stay.

    A ``driver`` also gets a FRESH episode store, as a new process would build: the crashed
    episode was never closed, so the old store still owns its pipeline and no next episode
    could open one.
    """
    from core import pipeline_state
    from core.cognition_lock import release_held_locks_for_tests
    from global_loop import intersections as mail
    from memory import episode_sram, intake_journal
    from pressure_loop import cognition_queue as cq, system_messages
    from pressure_loop.episode import EpisodeStore

    release_held_locks_for_tests()
    intake_journal.reset_cache_for_tests()
    mail.reset_cache_for_tests()
    cq._reset_cache_for_tests()
    system_messages._reset_cache_for_tests()
    episode_sram.reset()
    pipeline_state._CACHE.clear()
    if driver is not None:
        driver.store = driver.harness.store = EpisodeStore(
            reflection_window_path=driver.harness.root / "state" / "reflection_window.json",
            trajectory_path=driver.harness.root / "state" / "trajectory.json",
        )


def _boot(driver=None, revive=None):
    """A restart the way production performs one: caches gone, then the REAL boot.

    ``restore_pipelines_at_boot`` is what a new process runs before any worker is scheduled —
    it reconciles owed intake, completes an unpublished transaction record, re-queues what is
    restorable and stops the jobs it cannot restore. A test that only cleared caches was
    skipping the half of recovery that changes durable state (review 2026-09-19).

    ``revive`` is ``_died_at``'s switch: the process that comes back after a crash is a
    HEALTHY one, so it is released here, between the caches going and the boot running.
    """
    from core import pipeline_state

    _restart(driver)
    if revive is not None:
        revive()
    return pipeline_state.restore_pipelines_at_boot()


# ── reading the durable stores ────────────────────────────────────────────────

def _statements():
    from memory import intake_journal

    return {record.event_id: record for record in intake_journal.records()
            if ":decision:" in record.event_id or ":constraint:" in record.event_id}


def _mail_rows():
    from global_loop import intersections as mail

    return [dict(row) for row in mail.all_mail()]


def _transaction_mail_rows():
    """Mail effects named by a cognitive transaction record, excluding lane admission mail.

    Operator turns have already crossed the Chat-to-specialist mailbox before the specialist
    episode starts. That handoff is durable routing input, not the specialist fold's outgoing
    transaction effect. Crash-boundary assertions below concern the latter.
    """
    named = {
        (str(item.get("mail_id") or ""), str(item.get("effect_id") or ""))
        for record in _records().values()
        for item in record.get("outgoing") or ()
        if isinstance(item, dict)
    }
    return [
        row for row in _mail_rows()
        if (str(row.get("mail_id") or ""), str(row.get("effect_id") or "")) in named
    ]


def _handoff_mail_rows():
    """Chat-originated specialist admission mail, separate from transaction effects."""
    return [
        row for row in _mail_rows()
        if dict(row.get("origin") or {}).get("ingress_class") == "specialist_lane_handoff"
    ]


def _staged():
    from pressure_loop import pressure

    return [row.metadata["successor_id"] for row in pressure.staged_signals()]


def _records():
    from pressure_loop import pressure

    return dict(pressure._read_successors()["transactions"])


def _agent_records(driver, *, required=False):
    receipt_ids = {
        record.record_id for record in _statements().values()
    }
    selected = {
        fold_id: record for fold_id, record in _records().items()
        if record.get("successor") is not None
        and receipt_ids.intersection(record.get("receipt_ids") or ())
    }
    if required and not selected:
        observed = [(fold_id, record.get("receipt_ids")) for fold_id, record in _records().items()]
        raise AssertionError(f"Agent transaction for its accepted receipts is missing; found {observed!r}")
    return selected


def _persisted(driver, events) -> None:
    """Every one of these accepted statements is committed, bound to a canonical record, and
    on the Dewey filesystem EXACTLY once.

    Stated as presence and not only as absence: an oracle that just forbids duplicates is
    satisfied by an empty memory, and a substituted empty canonical view passed it (review
    2026-09-19).
    """
    from memory import intake_journal

    records = _statements()
    texts = _canonical_texts(driver)
    for event in events:
        record = records.get(event)
        assert record is not None, f"{event} vanished from the intake journal"
        assert record.status == intake_journal.COMMITTED, (
            f"{event} is {record.status}, not committed; owner_pipeline={record.owner_pipeline_id}, "
            f"owner_episode={record.owner_episode_id}, adoptable={record.adoptable}"
        )
        assert record.canonical_path, f"{event} committed without a canonical record"
    assert _landed(texts, DECISION) == 1, "the decision is on the Dewey filesystem exactly once"
    assert _landed(texts, CONSTRAINT) == 1, "and so is the constraint"


def _landed(texts, statement: str) -> int:
    """How many canonical records carry this statement — its text LEADS the record's body,
    which also carries the rationale, so this is a prefix count and not an equality."""
    return sum(1 for text in texts if text.startswith(statement))


def _canonical_texts(driver):
    """Every canonical record on the Dewey filesystem, by its text."""
    import json

    root = driver.harness.memory_root
    texts = []
    for path in root.rglob("*.json"):
        if path.parent.name == "indexes":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - not a canonical record
            continue
        if isinstance(payload, dict) and payload.get("text"):
            texts.append(str(payload["text"]))
    return texts


# ── B1 — after acceptance, before the transaction record ──────────────────────

def test_b1_a_crash_before_the_record_keeps_the_acceptance_and_publishes_nothing(driver):
    """The fold's statements were accepted into memory; the process died before the record.

    Section 6: no mail is published, no successor is staged, and no external effect occurred.
    """
    _script(driver, _delta_fold)
    turn = _leased_turn(driver, OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    _died_at(driver, "pressure_loop.pressure", "open_transaction", "died before the record")
    with pytest.raises(Killed, match="died before the record"):
        driver._ignite(turn, opened_by=turn.type)

    held = _statements()
    assert sorted(event.rsplit(":", 2)[-2] for event in held) == ["constraint", "decision"], \
        "both statements are durable under their fold-stable identities"
    assert {record.status for record in held.values()} == {"pending"}, "accepted, not yet canonical"
    [handoff] = _handoff_mail_rows()
    assert handoff["payload"]["original_cycle_id"] == CYCLE, \
        "the specialist admission remains durable across the fold crash"
    assert _transaction_mail_rows() == [], "the fold published no transaction effect"
    assert _staged() == [], "no continuation was staged"
    assert _agent_records(driver) == {}, "and no Agent transaction record was opened"


def test_b1_boot_releases_the_accepted_intake_and_a_later_drain_commits_it_once(driver):
    """The survival path section 6 names, end to end and through the REAL production recovery:
    ``restore_pipelines_at_boot`` → the next episode's bucket adopts → its terminus drains."""
    from memory import intake_journal

    _script(driver, _delta_fold, _done_fold, batches=4)  # a carrier episode follows the crash
    turn = _leased_turn(driver, OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    revive = _died_at(driver, "pressure_loop.pressure", "open_transaction", "died before the record")
    with pytest.raises(Killed):
        driver._ignite(turn, opened_by=turn.type)
    owed = sorted(_statements())
    _restart(driver)
    revive()                                                     # the process that comes back is healthy

    report = pipeline_state.restore_pipelines_at_boot()          # automatic production recovery

    assert len(owed) == 2, "the fold accepted a decision and a constraint"
    released = sum(int(row["intake"]["released"]) for row in report["pipelines"].values())
    assert released >= len(owed), "boot released the owed intake (the episode's other intake too)"
    for event in owed:
        record = _statements()[event]
        assert (record.status, record.adoptable) == (intake_journal.PENDING, True), \
            f"{event} is released for the next episode on its pipeline to adopt"
    assert report["completed_transactions"] == [], "there was no record to complete"

    # A later episode on that pipeline adopts the released records and its own terminus drains
    # them — no test-only drain call anywhere in this path.
    driver.operator_turn("anything else?", route="CODING_AGENT", cycle_id="bubble-later")

    assert sorted(_statements()) == owed, "the same records, not new ones"
    _persisted(driver, owed)
    assert _transaction_mail_rows() == [] and _staged() == [], \
        "recovery did not publish a transaction effect or continuation"


def test_b1_a_replay_on_the_same_identity_re_derives_without_repeating_any_effect(driver):
    """Section 6's "the conclusion may be re-derived", verified as what it is: a PERMITTED
    replay, driven by this test.

    Nothing in production re-runs an interrupted fold (``mark_acted`` dropped its
    continuation), so the test re-ignites the turn on the SAME contributor identity itself.
    What the replay may do is re-derive: the model folds again, and because the fold identity
    is that contributor identity plus its arrival order, its statements resolve to the records
    the first attempt already accepted — nothing doubled, nothing lost, no external effect
    repeated.

    Because B1 has no transaction record to preserve, the test-driven replay may open a new
    record and stage its continuation. The replay batch is bounded and produces no repeated
    external effect. Nothing in production triggers this replay.
    """
    _script(driver, _delta_fold, _delta_fold, _delta_fold, _delta_fold, batches=1)
    turn = _leased_turn(driver, OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    revive = _died_at(driver, "pressure_loop.pressure", "open_transaction", "died before the record")
    with pytest.raises(Killed):
        driver._ignite(turn, opened_by=turn.type)
    first = sorted(_statements())
    effects = _effects(driver)
    assert len(effects) == 1, "the first attempt's batch landed exactly one effect"
    assert _agent_records(driver) == {}, "B1 died before the Agent transaction was opened"
    _bind_replay_to_initial_job(turn)
    _restart(driver)
    revive()
    pipeline_state.restore_pipelines_at_boot()
    folded = len(driver.folds)

    driver.agent_batches.extend({"text": "resume is blocked", "tool_calls": [
        tool_call("start_job", {"project_root": "."}, "replay-claim"),
        tool_call("task_plan", {"steps": ["do not resume"]}, "replay-plan"),
    ]} for _ in range(1))
    driver.agent_batches.extend({"text": "nothing further to run", "tool_calls": []} for _ in range(4))
    replay_turn = _requeue_for_test_replay(turn)
    replayed = driver._ignite(replay_turn, opened_by=replay_turn.type)  # test-driven replay, same identity

    assert len(driver.folds) > folded, "the conclusion was re-derived (the model folded again)"
    assert sorted(_statements()) == first, "and it resolved to the same accepted statements"
    _persisted(driver, first)
    assert _effects(driver) == effects, "no external effect was repeated"
    run = dict(dict(getattr(replayed, "auto", None) or {}).get("outputs") or {}).get("agent_run") or {}
    assert run.get("status") in {"batch_ran", "blocked"}, "the replay batch returned no status"
    assert len(_agent_records(driver)) == 1, "the re-derived fold opened one transaction"
    assert len(_staged()) == 1, "the re-derived continuation was staged exactly once"
    assert len(_transaction_mail_rows()) == 1, \
        "the continuation published its one required transaction lane entry"


# ── B2 — after the record, before staging ─────────────────────────────────────

def test_b2_a_crash_before_staging_leaves_one_pending_record_and_its_mail(driver):
    """The record is open and its mail is pinned; the process died before the successor was
    staged. Section 6: the record exists with staging ``pending``, and the published mail keeps
    its ``effect_id``."""
    _script(driver, _delta_fold)
    turn = _leased_turn(driver, OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    _died_at(driver, "pressure_loop.pressure", "complete_transaction", "died before staging")
    with pytest.raises(Killed, match="died before staging"):
        driver._ignite(turn, opened_by=turn.type)

    [(fold_id, record)] = _agent_records(driver, required=True).items()
    assert (record["staging"], record["published"], record["closed"]) == ("pending", False, False)
    assert record["successor"] is not None, "the continuation is carried by value, not staged"
    assert _staged() == [], "and nothing is staged yet"
    [named] = record["outgoing"]
    [row] = _transaction_mail_rows()
    assert (row["mail_id"], row["effect_id"]) == (named["mail_id"], named["effect_id"]), \
        "the mail was published under the identities the record minted for it"
    assert fold_id and record["receipt_ids"], "the statements it rests on are named on the record"


def test_b2_boot_stages_the_recorded_successor_exactly_once(driver):
    """The AUTOMATIC production recovery: ``restore_pipelines_at_boot`` completes the record
    (``reconcile_transactions_at_boot``). Booting again stages nothing more, and the mail is
    never pinned a second time — the external action is not repeated."""
    _script(driver, _delta_fold)
    turn = _leased_turn(driver, OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    revive = _died_at(driver, "pressure_loop.pressure", "complete_transaction", "died before staging")
    with pytest.raises(Killed):
        driver._ignite(turn, opened_by=turn.type)
    [(fold_id, record)] = _agent_records(driver, required=True).items()
    [before] = _transaction_mail_rows()
    _restart(driver)
    revive()

    report = pipeline_state.restore_pipelines_at_boot()

    assert report["completed_transactions"] == [fold_id], "boot completed the one open record"
    staged = _staged()
    assert len(staged) == 1, "the recorded successor was staged exactly once"
    assert staged == [record["successor"]["metadata"]["successor_id"]], "and it is the recorded one"
    completed = _agent_records(driver)[fold_id]
    assert (completed["staging"], completed["published"]) == ("staged", True)
    [after] = _transaction_mail_rows()
    assert (after["mail_id"], after["effect_id"]) == (before["mail_id"], before["effect_id"]), \
        "the same mail, not a second pin: the effect_id is the external action's identity"

    _restart(driver)
    again = pipeline_state.restore_pipelines_at_boot()

    assert again["completed_transactions"] == [], "a published record is not completed twice"
    assert _staged() == staged and len(_transaction_mail_rows()) == 1, "and nothing is duplicated"


def test_b2_the_statements_the_record_rests_on_reach_canonical_after_recovery(driver):
    """The record names the receipts its conclusion rests on. Those statements were accepted
    before the crash and are still owed, so recovery has to carry them all the way: boot
    releases them, the next episode's bucket adopts them, and its own terminus drains them.

    Asserted as PRESENCE — committed receipt, canonical path, exactly one persisted record
    each — because an oracle that only forbids duplicates is satisfied by an empty memory.
    """
    _script(driver, _delta_fold, _done_fold, batches=3)
    turn = _leased_turn(driver, OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    revive = _died_at(driver, "pressure_loop.pressure", "complete_transaction", "died before staging")
    with pytest.raises(Killed):
        driver._ignite(turn, opened_by=turn.type)
    [(fold_id, record)] = _agent_records(driver, required=True).items()
    owed = sorted(_statements())
    assert sorted(record["receipt_ids"]) == sorted(
        _statements()[event].record_id for event in owed), "the record rests on exactly these"
    _restart(driver)
    revive()

    pipeline_state.restore_pipelines_at_boot()
    driver.operator_turn("anything else?", route="CODING_AGENT", cycle_id="bubble-later")

    _persisted(driver, owed)
    assert _agent_records(driver)[fold_id]["staging"] == "staged", "and the continuation was staged from it"
    assert len(_staged()) == 1 and len(_transaction_mail_rows()) == 1, "once, with its one effect"


def test_b2_a_replay_that_concluded_differently_cannot_replace_the_record(driver):
    """Section 6's guard: the FIRST record stands. The crash left a record open; the fold is
    then replayed on the same identity and returns a DIFFERENT conclusion — it must be honoured
    rather than replaced, so what publishes is what the original fold decided.

    The replay is TEST-DRIVEN (no production path re-runs a fold); the record's survival is
    the production guarantee under test.
    """
    _script(driver, _delta_fold, _other_delta_fold, _done_fold, batches=2)
    _replay_batch(driver)
    _replay_batch(driver)
    turn = _leased_turn(driver, OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    revive = _died_at(driver, "pressure_loop.pressure", "complete_transaction", "died before staging")
    with pytest.raises(Killed):
        driver._ignite(turn, opened_by=turn.type)
    [(fold_id, record)] = _agent_records(driver, required=True).items()
    original = json.loads(json.dumps(record))
    _bind_replay_to_initial_job(turn)
    _restart(driver)
    revive()

    replay_turn = _requeue_for_test_replay(turn)
    driver._ignite(replay_turn, opened_by=replay_turn.type)       # same identity, new conclusion

    kept = _agent_records(driver)[fold_id]
    for field in ("decision_digest", "receipt_ids", "continuation_key", "outgoing"):
        assert kept[field] == original[field], f"{field} is the original fold's, unchanged"
    assert kept["successor"]["metadata"]["omega_control"] == \
        original["successor"]["metadata"]["omega_control"], "the original continuation, not the replay's"
    assert len(_agent_records(driver)) == 1 and len(_staged()) == 1 \
        and len(_transaction_mail_rows()) == 1
    texts = _canonical_texts(driver)
    assert _landed(texts, "Put the delete button in a menu") == 0, \
        "the replay's own conclusion was never accepted in place of the original"


# ── B3 — after staging, before acknowledgement ────────────────────────────────

def _publishing_fold():
    """The fold whose record carries the outgoing lane-entry mail."""
    [(fold_id, record)] = [(fold_id, record) for fold_id, record in _records().items()
                           if record["outgoing"]]
    return fold_id, record


def _deliveries(fold_id):
    """A transaction record's delivery ledger, per outgoing effect (T9)."""
    return {effect: dict(row) for effect, row in _records()[fold_id]["deliveries"].items()}


def _pressure():
    from pressure_loop import pressure

    return pressure


def _replay_continuation(driver, successor):
    """Admit the SAME continuation a second time, on its own identity — the replay section 6
    permits and nothing in production triggers."""
    from pressure_loop.cognition_queue import enqueue_input, lease_next_input

    assert enqueue_input(successor, operator_activity=False)
    from core.pipelines import lane_for_contributor, pipeline_for_lane

    leased = lease_next_input(pipeline_for_lane(lane_for_contributor(successor)))
    assert leased is not None and leased.type == "omega_successor"
    return driver._ignite(leased, opened_by=leased.type)


def _batches_run(driver):
    """How many agent batches the model was ASKED for. Never the effect count: see
    ``_effects``, which reads what actually landed."""
    return sum(1 for row in driver.generations if row.get("operation") == "auto_response")


def _lapse_mail_leases(driver):
    """Real lease expiry: the store's own clock moves past the lease and the store releases."""
    import time as _time

    from global_loop import intersections as mail

    shifted = _time.time() + 4 * 3600
    driver.monkeypatch.setattr(mail, "time", type("clock", (), {"time": staticmethod(lambda: shifted)}))
    return mail.release_expired_leases()


def test_b3_an_acknowledgement_that_cannot_be_written_abandons_the_effect_and_returns_the_mail(driver):
    """The successor was staged and took its lane-entry mail; the prompt was recorded against
    the transaction record and then the acknowledgement could not be written.

    What the system actually does here is STRONGER than section 6's B3 text, and this is the
    assertion: it refuses the delivery, records the effect ``abandoned``, and hands the mail
    straight back as ``ready`` under the SAME ``effect_id`` — so nothing waits for a lease to
    lapse, the batch never ran, and a later consumer resolves the one effect rather than a
    second one. Section 6 describes the weaker guarantee (the lease expires, then the mail is
    claimable); see the module docstring for the part of B3 an in-process test cannot reach.
    """
    from global_loop import intersections as mail

    _script(driver, _delta_fold, _done_fold, batches=6)
    driver.operator_turn(OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    fold_id, record = _publishing_fold()
    [effect] = [row["effect_id"] for row in record["outgoing"]]
    before = _effects(driver)
    _died_at(driver, "global_loop.intersections", "acknowledge_mail", "died before acknowledgement")

    with pytest.raises(Killed):
        driver.run_successor()

    assert _deliveries(fold_id)[effect]["state"] == "abandoned", \
        "nothing ran, and the record says so rather than leaving the effect owned"
    assert _effects(driver) == before, "and no external effect landed"
    [row] = mail.all_mail()
    assert (row["status"], row["effect_id"]) == ("ready", effect), \
        "the mail is claimable again immediately, under the same effect identity"


def test_b3_a_lane_entry_lease_that_lapses_returns_the_mail_under_the_same_effect(driver):
    """The mechanism section 6's B3 rests on, over the real mail store: a lane-entry row that
    was CLAIMED and whose lease then lapsed returns to ``ready`` with its ``effect_id``
    unchanged — the identity a redelivery resolves to."""
    import time as _time

    from global_loop import intersections as mail

    _script(driver, _delta_fold, _done_fold, batches=6)
    driver.operator_turn(OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    [row] = mail.all_mail()
    effect, mail_id = row["effect_id"], row["mail_id"]

    claimed = mail.claim_lane_entry(lane="agent", thread_id=row["thread_id"],
                                    episode_id="ep-that-dies", mail_ids=[mail_id],
                                    continuation_key=row["continuation_key"], lease_s=60,
                                    job_id=row["target_job_id"])
    assert [entry["mail_id"] for entry in claimed["claimed"]] == [mail_id]
    assert mail.all_mail()[0]["status"] == "leased", "held by the episode that is about to die"

    shifted = _time.time() + 4 * 3600
    driver.monkeypatch.setattr(mail, "time", type("clock", (), {"time": staticmethod(lambda: shifted)}))
    assert mail.release_expired_leases() == 1, "the lease lapsed on the store's own clock"

    [after] = mail.all_mail()
    assert (after["status"], after["effect_id"], after["mail_id"]) == ("ready", effect, mail_id)


def test_b3_a_performed_effect_is_not_performed_again_even_when_its_mail_comes_back(driver):
    """B3's actual risk, and T9's rule that answers it: the dedup rests on the RECORD, never on
    the mail having been consumed.

    So the scenario puts both in play at once — the effect ran and is recorded ``performed``,
    and its mail is nevertheless claimable again (its acknowledgement never landed, and the
    lease then lapsed). A redelivery must consume that mail without running the effect a second
    time. The replay is TEST-DRIVEN; the refusal is the production guarantee.
    """
    import time as _time

    from global_loop import intersections as mail

    _script(driver, _delta_fold, _done_fold, _done_fold, batches=8)
    driver.operator_turn(OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    fold_id, record = _publishing_fold()
    [effect] = [row["effect_id"] for row in record["outgoing"]]
    [successor] = _pressure().staged_signals()
    # The acknowledgement silently does nothing: the effect still RUNS and is recorded
    # performed, but its mail is never consumed — the state a lost acknowledgement leaves.
    real_acknowledge = mail.acknowledge_mail
    lost = {"on": True}
    driver.monkeypatch.setattr(
        mail, "acknowledge_mail",
        lambda *a, **k: None if lost["on"] else real_acknowledge(*a, **k))

    driver.run_successor()

    assert _deliveries(fold_id)[effect]["state"] == "performed", "the effect ran, and once"
    landed = _effects(driver)
    assert landed, "the continuation's tool call really appended something"
    shifted = _time.time() + 4 * 3600
    driver.monkeypatch.setattr(mail, "time", type("clock", (), {"time": staticmethod(lambda: shifted)}))
    mail.release_expired_leases()
    assert [row["status"] for row in mail.all_mail()] == ["ready"], "its mail is claimable again"
    lost["on"] = False          # the acknowledgement works again; only the effect must not re-run

    _replay_continuation(driver, successor)

    assert _effects(driver) == landed, \
        "the external effect was NOT performed a second time — the same lines, unchanged"
    assert _deliveries(fold_id)[effect]["state"] == "performed", "one performed effect, still"
    assert mail.all_mail() == [], "and the mail whose effect already ran was consumed, not re-run"


# ── B3 through a REAL process death ───────────────────────────────────────────

def _kill_at_the_boundary(driver, tmp_path):
    """Drive the lane-entry boundary in a CHILD process and kill it there.

    Returns the scenario. The child records its prompt against the transaction record and then
    blocks inside the acknowledgement; this kills it with no unwinding — the only way to reach
    section 6's B3 state, since in-process ``LaneEntry.close`` runs from a ``finally`` and any
    unwinding abandons the effect instead.
    """
    import json
    import subprocess
    import sys
    import time

    [successor] = _pressure().staged_signals()
    [row] = _transaction_mail_rows()
    scenario = {"episode_id": "child-continuation", "thread_id": successor.metadata["thread_id"],
                "job_id": str(row["target_job_id"]),
                "successor": json.loads(json.dumps(successor.metadata))}
    scenario_path = tmp_path / "t13_scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    ready = tmp_path / "t13_at_boundary"
    child = subprocess.Popen(
        [sys.executable, "-m", "tests.e2e._t13_dying_child", str(tmp_path), str(scenario_path),
         str(ready)],
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if ready.is_file():
                break
            if child.poll() is not None:
                out, err = child.communicate()
                raise AssertionError(f"the child exited before the boundary: {err or out}")
            time.sleep(0.05)
        else:
            raise AssertionError("the child never reached the boundary")
        assert ready.read_text(encoding="utf-8") == "at the boundary"
    finally:
        child.kill()                     # no unwinding: the process is simply gone
        child.wait(timeout=30)
    return scenario


def test_b3_a_process_killed_at_the_boundary_leaves_the_effect_unproven(driver, tmp_path):
    """Section 6's B3, with a real death: a process that recorded its prompt and was killed
    before acknowledging leaves the effect ``executing`` — "it may have run" — and the mail
    leased to an episode that no longer exists.

    A fresh process then recovers: the lease lapses, the mail returns to ``ready`` under the
    same ``effect_id``, and the continuation is REFUSED rather than executed again, because no
    durable record shows the effect did not run.
    """
    import time as _time

    from auto_loop.lane_entry import LaneEntryRefused, open_lane_entry
    from global_loop import intersections as mail

    _script(driver, _delta_fold, _done_fold, batches=3)
    driver.operator_turn(OBJECTIVE, route="CODING_AGENT", cycle_id=CYCLE)
    fold_id, record = _publishing_fold()
    [effect] = [row["effect_id"] for row in record["outgoing"]]
    landed = _effects(driver)

    scenario = _kill_at_the_boundary(driver, tmp_path)

    _restart(driver)                     # a FRESH process reads what the dead one left
    assert _deliveries(fold_id)[effect]["state"] == "executing", \
        "the prompt was recorded and the acknowledgement never landed: the effect is unproven"
    [row] = mail.all_mail()
    assert (row["status"], row["effect_id"]) == ("leased", effect), \
        "and its mail is still leased to the episode that died"
    assert _effects(driver) == landed, "the killed batch performed nothing of its own"

    shifted = _time.time() + 4 * 3600
    driver.monkeypatch.setattr(mail, "time", type("clock", (), {"time": staticmethod(lambda: shifted)}))
    assert mail.release_expired_leases() == 1, "the lease lapses on the store's own clock"
    [after] = mail.all_mail()
    assert (after["status"], after["effect_id"]) == ("ready", effect), \
        "the mail returns claimable, under the same effect identity"

    episode = SimpleNamespace(episode_id="after-the-crash", thread_id=scenario["thread_id"],
                              opened_by="omega_successor",
                              pressure={"contributors": [{"id": "omega_successor", "weight": 1.0,
                                                          "detail": scenario["successor"]}]})
    with pytest.raises(LaneEntryRefused) as refusal:
        open_lane_entry(episode, {"action": "RESPONSE",
                                  "execution_inputs": {"job_id": scenario["job_id"]}})

    assert "effect has not run" in str(refusal.value), \
        "an effect that may have run is never run again"
    assert "interrupted" in str(refusal.value), \
        "the reservation of a process that is gone reads as interrupted, not as free"
    assert _effects(driver) == landed, "so nothing was performed a second time"
    assert _deliveries(fold_id)[effect]["state"] == "executing", "and the record still says so"


def test_a_fold_born_at_or_before_the_replay_horizon_publishes_nothing(driver, monkeypatch):
    """T13 named surface for spec section 6: horizon refusal.

    Evict closed transactions until ``transaction_horizon`` advances, then replay a fold
    born at that horizon with no record left. It must bind nothing and publish nothing,
    rather than being treated as a fresh fold.
    """
    from global_loop import intersections as mail
    from memory import intake_journal
    from omega_loop.episode import transaction as tx
    from pressure_loop import pressure as ledger
    from tests.omega_loop.test_cognitive_transaction import DELTA, _episode, _fold, _lane_entry

    monkeypatch.setattr(ledger, "_MAX_CLOSED_TRANSACTIONS", 0)
    old = _episode("ep-horizon-old", cycle_id="c-old", arrival_seq=5)
    tx.publish_cognitive_transaction(old, _fold(old, DELTA))
    for record_id in ledger.transaction(tx.transaction_fold_id(old))["receipt_ids"]:
        intake_journal.mark_consumed(record_id, reason="drained in T13 horizon test")
    tx.settle_cognitive_transaction(old)
    [pinned] = _lane_entry()
    [claimed] = mail.claim_mail(delivery_point="lane_entry", lane="agent", thread_id="t-build",
                                episode_id="ep-consumer", lease_s=60, job_id="job-a",
                                mail_ids=[pinned["mail_id"]])
    mail.acknowledge_mail([claimed], episode_id="ep-consumer")
    newer = _episode("ep-horizon-new", cycle_id="c-new", arrival_seq=9, pipeline_id=1,
                     job_id="job-b")
    tx.publish_cognitive_transaction(newer, _fold(newer, DELTA))
    assert ledger.transaction(tx.transaction_fold_id(old)) is None
    assert ledger.transaction_horizon() == 5
    _restart(driver)

    replay = _episode("ep-horizon-replay", cycle_id="c-old", arrival_seq=5)
    assert tx.publish_cognitive_transaction(replay, _fold(replay, DELTA)) is None
    assert ledger.transaction(tx.transaction_fold_id(replay)) is None
    assert [row["source_episode_id"] for row in _lane_entry()] == ["ep-horizon-new"]
