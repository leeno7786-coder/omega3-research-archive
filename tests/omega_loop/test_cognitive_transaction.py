"""T8: the cognitive delta, acceptance, and the recoverable transaction record.

Cross-lane cognitive integration spec, sections 5 and 6, plus the section 3 amendment that
moved restart-stable replay here. BOUNDARY tests over the real parser, the real intake
journal and episode SRAM, the real mail store, the real successor ledger, the real Global B
binder and the real chain settlement. Six boundaries are pinned:

1. the delta — every lane may return decisions, user constraints and lane injections;
   malformed rows are dropped, and the host owns every identity in them.
2. acceptance — the delta commits through ``ingest_knowledge_products``; receipts carry the
   journal identity; a ``held`` receipt IS acceptance; a replayed fold records nothing twice.
3. the record — opened BEFORE any mail is pinned or successor staged, naming everything it
   publishes; a DONE fold that publishes mail records staging ``none``; a rejected
   statement publishes no dependent mail; effects are performed once under one effect id.
4. restart replay — a new episode with the same fold identity binds the identical mail set,
   including after a crash between the claim and the record; acknowledged mail is never
   context again; a partly available set is abandoned, never silently changed.
5. ownership at publication — a fold whose lease lapsed publishes nothing: no memory
   commit, no lane-entry mail, no continuation.
6. recovery — a record left unpublished by a crash (B2) is completed from the record at
   boot exactly once; the record closes when its receipts are canonical.

Scope, stated rather than tested around: no production path re-runs a fold after AUTO has
acted (``pipeline_state.mark_acted`` drops the continuation), so restart replay is exercised
by igniting a NEW episode on the same contributor identity — the mechanism T13's fault
injection will drive. Lane-entry CONSUMPTION, and effect deduplication at the execution
boundary, are T9's.
"""

import json
from types import SimpleNamespace

import pytest

import cognition_chain as chain
from global_loop import intersections as mail
from global_loop.assemble.run import global_b_input_from_episode
from memory import episode_sram, intake_journal
from memory.knowledge_ingest import ingest_knowledge_products
from omega_loop.episode import transaction as tx
from omega_loop.episode.packet import attach_omega_packet, fold_identity
from omega_loop.episode.run import _packet_from_episode
from organ.promptorgan.packs.meta_omega import agent as agent_pack
from organ.promptorgan.packs.meta_omega import core
from pressure_loop import cognition_queue as cq
from pressure_loop import pressure as ledger
from schema.omega_control_decision import OmegaControlDecision

THREAD = "t-build"
CYCLE = "cycle-operator-1"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(intake_journal, "journal_root", lambda: tmp_path / "intake")
    monkeypatch.setattr(ledger, "_successor_path", lambda: tmp_path / "successors.json")
    monkeypatch.setattr(ledger, "_RESEED_STAGING", {})
    intake_journal.reset_cache_for_tests()
    episode_sram.reset()
    yield
    episode_sram.reset()
    intake_journal.reset_cache_for_tests()


def _control(decision="PRESSURE", intent="USE_TOOL"):
    assessment = dict.fromkeys(("who", "what", "when", "where", "why", "how",
                                "desired_state", "observed_state"), "fact")
    assessment.update(gap="" if decision == "DONE" else "unfinished", evidence=[])
    pressure = None if decision == "DONE" else dict(
        intent=intent, confidence=0.5, reason="finish", setpoints={}, desired_state="complete",
        observed_state="partial", resolution_conditions=["verified"])
    return dict(assessment=assessment, decision=decision, pressure=pressure, synthesis="summary")


def _episode(episode_id="ep-1", *, cycle_id=CYCLE, thread_id=THREAD, job_id="job-a", arrival_seq=7,
             pipeline_id=0):
    goal = {"episode_id": episode_id, "intent": "RESPOND", "omega_lane": "agent"}
    pack = {"episode_id": episode_id, "action": "RESPONSE", "tool": "",
            "source_action_goal": goal, "execution_inputs": {}}
    outputs = {"agent_run": {"status": "batch_ran", "steps": 1, "job_id": job_id}}
    return SimpleNamespace(
        episode_id=episode_id, thread_id=thread_id, opened_by="operator_query",
        pipeline_id=pipeline_id,
        global_={"intent_decision": goal,
                 "omega_packet": {"original_objective": "build it", "omega_lane": "agent"}},
        pressure={"contributors": [{"id": "operator_query", "type": "operator_query",
                                    "detail": {"cycle_id": cycle_id,
                                               "job_cycle_id": f"job-cycle-{job_id}",
                                               "message": "build it",
                                               "arrival_seq": arrival_seq}}]},
        meta={"execution_action_pack": pack},
        auto={"episode_id": episode_id, "execution_pack": pack, "source_action_goal": goal,
              "outputs": outputs})


def _attach(episode):
    attach_omega_packet(episode, global_b_input_from_episode(episode, {}), {})
    return _packet_from_episode(episode)


def _pin_instruction(text="use sqlite", job_id="job-a"):
    return mail.pin_mail(thread_id=THREAD, delivery_point="omega_reason",
                         purpose="operator_instruction", requirement="required",
                         source_actor="operator", source_episode_id="note-cycle",
                         payload={"text": text}, target_job_id=job_id)


DELTA = {
    "decisions": [{"statement": "Store todos in SQLite", "rationale": "the operator asked"}],
    "user_constraints": [{"statement": "Use SQLite", "source_episode_id": "note-cycle"}],
    "lane_injections": [{"target_lane": "agent", "objective": "Switch storage to SQLite",
                         "constraints": ["keep the API"], "evidence_refs": ["operator note"],
                         "requirement": "required"}],
}


def _fold(episode, payload, *, decision="PRESSURE"):
    """Parse a model payload with the REAL lane parser, apply it to the episode the way the
    chain does, and return the validated control."""
    from omega_loop.episode.packet import FoldReplayRefused

    packet = _attach(episode)
    result = agent_pack.parse({"control": _control(decision), **payload}, packet)
    try:
        # As the chain does FIRST: resolve the fold's one effective result (frozen whole in
        # its manifest), so a replay acts on the ORIGINAL everywhere.
        result = chain._effective_fold_result(episode, result)
    except FoldReplayRefused:
        # The chain discards such a fold outright; the gates below are still exercised.
        pass
    global_ = dict(episode.global_)
    global_["omega_result"] = result.to_dict()
    episode.global_ = global_
    episode_sram.open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    with episode_sram.bind_episode(episode.episode_id, pipeline_id=episode.pipeline_id):
        chain._ingest_fold_knowledge(episode, result, global_)
    # As the chain does: the control it settles is the one on the carrier, which for a replay
    # is the fold's ORIGINAL, frozen with its statements.
    return OmegaControlDecision.from_dict(global_["omega_result"]["control_decision"])


def _lane_entry():
    return [row for row in mail.all_mail() if row["delivery_point"] == "lane_entry"]


def _restart():
    mail.reset_cache_for_tests()
    intake_journal.reset_cache_for_tests()
    episode_sram.reset()
    cq._reset_cache_for_tests()


# ── 1. the delta ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bare", [False, True], ids=["wrapped-control", "bare-control"])
def test_every_lane_parser_accepts_the_cognitive_delta(bare):
    packet = _attach(_episode())
    payload = {**_control(), **DELTA} if bare else {"control": _control(), **DELTA}

    result = core.parse_lane(payload, packet)

    assert result.decisions == ({"statement": "Store todos in SQLite",
                                 "rationale": "the operator asked"},)
    assert result.lane_injections[0]["target_lane"] == "agent"
    assert result.control_decision["decision"] == "PRESSURE"


def test_malformed_delta_rows_are_dropped_not_fatal():
    packet = _attach(_episode())
    payload = {"control": _control(),
               "decisions": [{"statement": "no rationale"}, "text", {"statement": "", "rationale": "x"}],
               "user_constraints": "not a list",
               "lane_injections": [{"target_lane": "kernel", "objective": "x"},
                                   {"target_lane": "agent", "objective": "ok", "requirement": "urgent"},
                                   {"target_lane": "chat", "objective": "fine"}]}

    result = core.parse_lane(payload, packet)

    assert result.decisions == () and result.user_constraints == ()
    assert [row["target_lane"] for row in result.lane_injections] == ["chat"]
    assert result.lane_injections[0]["requirement"] == "preferred"


def test_cognitive_delta_honours_the_row_cap():
    from omega_loop.episode.types import COGNITIVE_DELTA_ROWS

    packet = _attach(_episode())
    payload = {"control": _control(),
               "decisions": [{"statement": f"decision {index}", "rationale": "why"}
                             for index in range(COGNITIVE_DELTA_ROWS + 4)],
               "lane_injections": [{"target_lane": "agent", "objective": f"do {index}",
                                    "requirement": "optional"}
                                   for index in range(COGNITIVE_DELTA_ROWS + 2)]}

    result = core.parse_lane(payload, packet)

    assert len(result.decisions) == COGNITIVE_DELTA_ROWS
    assert result.decisions[-1]["statement"] == f"decision {COGNITIVE_DELTA_ROWS - 1}"
    assert len(result.lane_injections) == COGNITIVE_DELTA_ROWS


def test_cognitive_delta_text_is_truncated_to_the_field_cap_not_dropped():
    from omega_loop.episode.types import _DELTA_TEXT

    packet = _attach(_episode())
    statement = "keep " * 400
    compacted = " ".join(statement.split())
    assert len(compacted) > _DELTA_TEXT
    payload = {"control": _control(),
               "decisions": [{"statement": statement, "rationale": "because it is long"}]}

    result = core.parse_lane(payload, packet)

    assert len(result.decisions) == 1
    assert result.decisions[0]["statement"] == compacted[:_DELTA_TEXT]
    assert len(result.decisions[0]["statement"]) == _DELTA_TEXT


def test_the_host_owns_a_constraints_source_identity():
    """The model may only echo a source it was SHOWN; anything else becomes this episode."""
    _pin_instruction()
    packet = _attach(_episode())
    payload = {"control": _control(), "user_constraints": [
        {"statement": "Use SQLite", "source_episode_id": "note-cycle"},
        {"statement": "Invented source", "source_episode_id": "ep-the-model-made-up"}]}

    result = core.parse_lane(payload, packet)

    assert [row["source_episode_id"] for row in result.user_constraints] == ["note-cycle", "ep-1"]


def test_the_prompt_tells_every_lane_about_the_delta():
    for lane in ("chat", "agent", "browser", "window"):
        assert "OPTIONAL COGNITIVE DELTA" in core.metacognitive_core(lane)


# ── 2. acceptance ─────────────────────────────────────────────────────────────

def test_receipts_carry_the_journal_identity_and_held_is_acceptance():
    episode_sram.open_bucket("ep-1", pipeline_id=0)
    with episode_sram.bind_episode("ep-1", pipeline_id=0):
        [receipt] = ingest_knowledge_products(
            "ep-1", [{"kind": "decision", "statement": "Store todos in SQLite",
                      "rationale": "the operator asked", "domain": "coding"}])

    assert (receipt["status"], receipt["accepted"], receipt["kind"]) == ("held", True, "decision")
    record = intake_journal.record_by_id(receipt["journal_record_id"])
    assert record is not None and record.event_id == receipt["event_id"]
    assert "Rationale: the operator asked" in record.payload["payload"]["text"]


def test_the_delta_commits_through_the_fold_ingest_and_stamps_its_receipts():
    episode = _episode()

    _fold(episode, DELTA)

    receipts = episode.global_["omega_result"]["delta_receipts"]
    assert [row["kind"] for row in receipts] == ["decision", "user_constraint"]
    assert all(row["accepted"] and row["journal_record_id"] for row in receipts)
    assert "knowledge_receipts" not in episode.global_["omega_result"]


def test_a_replayed_fold_records_nothing_twice_and_the_first_statement_stands():
    first = _episode("ep-1")
    _fold(first, DELTA)
    before = {row.record_id for row in intake_journal.records()}
    _restart()

    replay = _episode("ep-2")                       # same contributor identity, new episode
    reworded = {**DELTA, "decisions": [{"statement": "Persist todos with SQLite instead",
                                        "rationale": "as the operator said"}]}
    _fold(replay, reworded)

    assert {row.record_id for row in intake_journal.records()} == before
    [decision] = [row for row in replay.global_["omega_result"]["delta_receipts"]
                  if row["kind"] == "decision"]
    assert decision["accepted"] and decision["statement"] == "Store todos in SQLite"


# ── 3. the record ─────────────────────────────────────────────────────────────

def test_the_record_is_opened_before_anything_is_published(monkeypatch):
    _pin_instruction()
    episode = _episode()
    control = _fold(episode, DELTA)
    order: list[str] = []
    real_open, real_pin, real_complete = ledger.open_transaction, mail.pin_mail, ledger.complete_transaction
    monkeypatch.setattr(ledger, "open_transaction", lambda r: (order.append("record"), real_open(r))[1])
    monkeypatch.setattr(mail, "pin_mail", lambda **k: (order.append("pin"), real_pin(**k))[1])
    monkeypatch.setattr(ledger, "complete_transaction",
                        lambda f: (order.append("stage"), real_complete(f))[1])

    successor = tx.publish_cognitive_transaction(episode, control)

    assert order == ["record", "pin", "stage"]
    record = ledger.transaction(tx.transaction_fold_id(episode))
    binding = episode.global_["omega_mail"]
    [out] = record["outgoing"]
    [pinned] = _lane_entry()
    assert record["decision_digest"] and len(record["receipt_ids"]) == 2
    assert (record["mail_cutoff_seq"], record["bound_mail_ids"]) == (
        binding["cutoff_seq"], binding["bound_mail_ids"])
    assert (out["mail_id"], out["effect_id"]) == (pinned["mail_id"], pinned["effect_id"])
    assert record["continuation_key"] == pinned["continuation_key"] != ""
    assert (record["staging"], record["published"]) == ("staged", True)
    assert record["pending_canonical"] == record["receipt_ids"], "held: the drain is still owed"
    assert ledger.staged_signals() == (successor,)


def test_lane_entry_mail_carries_its_content_by_value_with_receipt_provenance():
    episode = _episode()
    control = _fold(episode, DELTA)

    tx.publish_cognitive_transaction(episode, control)

    [row] = _lane_entry()
    assert row["payload"] == {"objective": "Switch storage to SQLite", "constraints": ["keep the API"],
                              "evidence_refs": ["operator note"],
                              "decisions": ["Store todos in SQLite"],
                              "user_constraints": ["Use SQLite"]}
    receipts = episode.global_["omega_result"]["delta_receipts"]
    assert {f"intake:{r['journal_record_id']}" for r in receipts} <= set(row["provenance"])
    assert (row["purpose"], row["source_actor"], row["target_lane"], row["target_job_id"],
            row["requirement"], row["thread_id"]) == (
        "omega_objective", "omega", "agent", "job-a", "required", THREAD)


def test_a_done_fold_withholds_its_injections_as_no_consumer():
    """Amended 2026-09-17 (T9 review decision). Lane-entry mail is claimed by explicit id, by
    the continuation that names it; a DONE fold stages none, so its injections could only be
    pinned to expire unread. They are WITHHELD with an explicit disposition — conclusion and
    provenance kept by value — and the statements the fold committed are untouched."""
    episode = _episode()
    control = _fold(episode, DELTA, decision="DONE")

    assert tx.publish_cognitive_transaction(episode, control) is None

    record = ledger.transaction(tx.transaction_fold_id(episode))
    assert (record["staging"], record["successor"], record["published"]) == ("none", None, True)
    assert _lane_entry() == [] and record["outgoing"] == [] and ledger.staged_signals() == ()
    [withheld] = record["withheld"]
    receipts = episode.global_["omega_result"]["delta_receipts"]
    assert withheld["disposition"] == "no_consumer" and "stages no continuation" in withheld["reason"]
    assert (withheld["target_lane"], withheld["requirement"]) == ("agent", "required")
    assert withheld["injection"] == {"index": 0, "objective": "Switch storage to SQLite",
                                     "constraints": ["keep the API"],
                                     "evidence_refs": ["operator note"]}
    assert {f"intake:{row['journal_record_id']}" for row in receipts} <= set(withheld["provenance"])
    # Withholding the injection erased NOTHING the fold accepted into memory.
    assert len(record["receipt_ids"]) == 2 and all(row["accepted"] for row in receipts)
    assert all(intake_journal.record_by_id(one) is not None for one in record["receipt_ids"])


def test_an_injection_for_a_lane_the_successor_does_not_enter_has_no_consumer():
    """The continuation enters the agent lane: a browser injection is withheld, the agent one
    is pinned under the identity its position in the RESULT gives it, and — having no
    dependent action at all — even a REQUIRED no_consumer injection declines nothing."""
    episode = _episode()
    payload = dict(DELTA, lane_injections=[
        {"target_lane": "browser", "objective": "Check the docs", "constraints": [],
         "evidence_refs": [], "requirement": "required"},
        DELTA["lane_injections"][0]])
    control = _fold(episode, payload)

    successor = tx.publish_cognitive_transaction(episode, control)

    fold_id = tx.transaction_fold_id(episode)
    record = ledger.transaction(fold_id)
    [pinned] = _lane_entry()
    assert successor is not None and record["staging"] == "staged"
    assert pinned["target_lane"] == "agent"
    assert pinned["mail_id"] == tx._digest({"fold": fold_id, "lane_entry": 1})[:16], "not renamed"
    [withheld] = record["withheld"]
    assert (withheld["disposition"], withheld["target_lane"]) == ("no_consumer", "browser")
    assert "enters the agent lane" in withheld["reason"] and withheld["injection"]["index"] == 0


def test_a_fold_with_nothing_to_record_opens_no_transaction():
    episode = _episode()
    control = _fold(episode, {}, decision="DONE")

    assert tx.publish_cognitive_transaction(episode, control) is None
    assert ledger.transactions() == {}


def test_a_rejected_statement_publishes_no_dependent_mail():
    episode = _episode()
    control = _fold(episode, DELTA)
    receipts = episode.global_["omega_result"]["delta_receipts"]
    receipts[0].update(status="rejected", accepted=False)          # the verdict refused it

    successor = tx.publish_cognitive_transaction(episode, control)

    assert _lane_entry() == []
    record = ledger.transaction(tx.transaction_fold_id(episode))
    assert record["outgoing"] == [] and record["receipt_ids"] == [receipts[1]["journal_record_id"]]
    # The withheld injection was REQUIRED: the continuation may not run without it.
    assert successor is None and ledger.staged_signals() == ()
    assert (record["staging"], record["successor"]) == ("none", None)
    assert [(row["requirement"], row["disposition"]) for row in record["withheld"]] == [
        ("required", "undeliverable")]


def test_republishing_a_fold_performs_each_effect_once_even_after_consumption():
    episode = _episode()
    control = _fold(episode, DELTA)
    tx.publish_cognitive_transaction(episode, control)
    [row] = _lane_entry()
    [claimed] = mail.claim_mail(delivery_point="lane_entry", lane="agent", thread_id=THREAD,
                                episode_id="ep-consumer", lease_s=60, job_id="job-a",
                                mail_ids=[row["mail_id"]])
    mail.acknowledge_mail([claimed], episode_id="ep-consumer")     # consumed

    tx.publish_cognitive_transaction(episode, control)
    tx.reconcile_transactions_at_boot()

    assert _lane_entry() == [], "a consumed effect is never pinned again"
    assert len(ledger.staged_signals()) == 1


def test_a_minted_mail_id_cannot_be_reused_for_another_effect():
    mail.pin_mail(thread_id=THREAD, delivery_point="lane_entry", purpose="omega_objective",
                  requirement="optional", source_actor="omega", source_episode_id="ep-1",
                  payload={"objective": "x"}, mail_id="m-fixed", effect_id="effect-1")

    with pytest.raises(ValueError, match="another effect"):
        mail.pin_mail(thread_id=THREAD, delivery_point="lane_entry", purpose="omega_objective",
                      requirement="optional", source_actor="omega", source_episode_id="ep-1",
                      payload={"objective": "x"}, mail_id="m-fixed", effect_id="effect-2")


# ── 4. restart replay ─────────────────────────────────────────────────────────

def test_a_replay_after_a_restart_binds_the_identical_set_under_the_recorded_cutoff():
    first_mail = _pin_instruction("use sqlite")
    second_mail = _pin_instruction("keep the API")
    first = _episode("ep-1")
    packet = _attach(first)
    bound = [row["mail_id"] for row in packet.cross_lane_mail]
    assert bound == [first_mail["mail_id"], second_mail["mail_id"]]
    _pin_instruction("pinned after the cutoff")
    _restart()                                   # the process dies holding its leases

    replay = _episode("ep-2")
    assert fold_identity(replay) == fold_identity(first)
    replayed = _attach(replay)

    assert [row["mail_id"] for row in replayed.cross_lane_mail] == bound
    record = replay.global_["omega_mail"]
    assert record["replayed"] is True and record["cutoff_seq"] == first.global_["omega_mail"]["cutoff_seq"]
    assert {row["claimed_by"] for row in mail.all_mail() if row["mail_id"] in bound} == {"ep-2"}


def test_a_crash_between_the_claim_and_the_record_still_replays_the_identical_set():
    """The binding intent is durable ATOMICALLY with the claim, in the mail store itself —
    no transaction record was ever written here."""
    _pin_instruction()
    first = _episode("ep-1")
    bound = [row["mail_id"] for row in _attach(first).cross_lane_mail]
    assert ledger.transactions() == {}
    on_disk = json.loads(mail.mail_path().read_text(encoding="utf-8"))
    assert on_disk["bindings"][fold_identity(first)]["mail_ids"] == bound
    _restart()

    assert [row["mail_id"] for row in _attach(_episode("ep-2")).cross_lane_mail] == bound


def test_acknowledged_mail_is_never_context_again_and_the_replay_binds_nothing_new():
    _pin_instruction()
    first = _episode("ep-1")
    _attach(first)
    from omega_loop.episode.packet import acknowledge_omega_mail

    assert acknowledge_omega_mail(first) == 1
    _pin_instruction("newer words")
    _restart()

    replay = _episode("ep-2")

    assert _attach(replay).cross_lane_mail == ()
    assert replay.global_["omega_mail"]["disposition"] == "reconciled"
    assert [row["status"] for row in mail.all_mail()] == ["ready"], "newer words stay for a real fold"


def test_acknowledging_specialist_mail_settles_its_lane_handoff_receipt(monkeypatch):
    handoff_id = "b" * 64
    marked = []
    calls = []
    episode = SimpleNamespace(
        episode_id="specialist-episode", thread_id=THREAD,
        global_={"omega_mail": {
            "disposition": "bound",
            "thread_id": THREAD,
            "receipts": [{"mail_id": "specialist-mail", "lease_id": "lease", "generation": 1}],
            "mail": [{"mail_id": "specialist-mail", "purpose": "specialist_lane_handoff",
                      "payload": {"handoff_id": handoff_id}}],
        }},
    )
    monkeypatch.setattr(cq, "prepare_handoff_mail_absorption",
                        lambda identities: calls.append(("prepare", tuple(identities)) or tuple(identities)))
    monkeypatch.setattr(mail, "acknowledge_mail",
                        lambda _receipts, *, episode_id: calls.append(("mail_ack", episode_id)) or 1)
    monkeypatch.setattr(cq, "complete_handoff_mail_absorption",
                        lambda identities: calls.append(("complete", tuple(identities)) or tuple(identities)))
    monkeypatch.setattr(cq, "mark_handoff_mail_acknowledged",
                        lambda ids: calls.append(("mark_acknowledged", tuple(ids))
                                                 ) or marked.extend(ids) or tuple(ids))

    from omega_loop.episode.packet import acknowledge_omega_mail

    assert acknowledge_omega_mail(episode) == 1
    assert calls == [
        ("prepare", (handoff_id,)),
        ("mail_ack", "specialist-episode"),
        ("complete", (handoff_id,)),
        ("mark_acknowledged", (handoff_id,)),
    ]
    assert marked == [handoff_id]


def test_the_store_abandons_a_replay_when_another_consumer_holds_part_of_the_set():
    a, b = _pin_instruction("use sqlite"), _pin_instruction("keep the API")
    got = mail.claim_for_fold(fold_id="fold-1", lane="agent", thread_id=THREAD, episode_id="ep-1",
                              lease_s=1800, job_id="job-a")
    assert got["disposition"] == "bound"
    mail.reset_cache_for_tests()                                   # restart
    state = json.loads(mail.mail_path().read_text(encoding="utf-8"))
    state["mail"][a["mail_id"]].update(claimed_by="ep-other")      # another fold took one row
    mail.mail_path().write_text(json.dumps(state), encoding="utf-8")
    mail.reset_cache_for_tests()

    replay = mail.claim_for_fold(fold_id="fold-1", lane="agent", thread_id=THREAD,
                                 episode_id="ep-2", lease_s=1800, job_id="job-a")

    assert (replay["disposition"], replay["claimed"]) == ("abandoned", [])
    held = {row["mail_id"]: row["claimed_by"] for row in mail.all_mail()}
    assert held == {a["mail_id"]: "ep-other", b["mail_id"]: "ep-1"}, "nothing taken"


def test_a_binding_held_in_this_process_is_never_taken_over():
    _pin_instruction()
    mail.claim_for_fold(fold_id="fold-1", lane="agent", thread_id=THREAD, episode_id="ep-1",
                        lease_s=1800, job_id="job-a")

    other = mail.claim_for_fold(fold_id="fold-1", lane="agent", thread_id=THREAD,
                                episode_id="ep-2", lease_s=1800, job_id="job-a")

    assert (other["disposition"], other["claimed"]) == ("held_elsewhere", [])


def test_a_fold_that_released_its_mail_leaves_no_binding_behind():
    _pin_instruction()
    first = _episode("ep-1")
    _attach(first)
    from omega_loop.episode.packet import release_omega_mail

    release_omega_mail(first)
    _restart()

    assert [row["payload"]["text"] for row in _attach(_episode("ep-2")).cross_lane_mail] == ["use sqlite"]


# ── 5. ownership at publication ───────────────────────────────────────────────

def _lapse(monkeypatch):
    import time

    later = time.time() + 3600
    monkeypatch.setattr(mail, "time", SimpleNamespace(time=lambda: later))


def test_a_fold_whose_lease_lapsed_publishes_nothing(monkeypatch):
    _pin_instruction()
    episode = _episode()
    control = _fold(episode, DELTA)
    _lapse(monkeypatch)

    assert tx.publish_cognitive_transaction(episode, control) is None

    assert ledger.transactions() == {} and _lane_entry() == [] and ledger.staged_signals() == ()


def test_a_lease_that_lapsed_during_inference_discards_the_fold_before_any_commit(monkeypatch):
    """Through the chain's own fold step: no memory commit, no result applied."""
    _pin_instruction()
    episode = _episode()
    _attach(episode)
    applied: list = []
    monkeypatch.setattr(chain, "_apply_omega_episode_result", lambda *a: applied.append(a))

    def inference(_episode):
        _lapse(monkeypatch)                        # the lease runs out while the model thinks
        return SimpleNamespace(cognized=True)

    monkeypatch.setattr("omega_loop.episode.run.run_omega_for_episode", inference)

    assert chain._run_omega_episode_cognition(episode) is None
    assert applied == [] and intake_journal.records() == ()
    assert "no longer its own" in episode.meta["omega_fold_failure"]


def test_publication_and_a_competing_claim_cannot_interleave(monkeypatch):
    """Ownership is proven and everything published in one critical section of the store."""
    import threading

    _pin_instruction()
    episode = _episode()
    control = _fold(episode, DELTA)
    inside, resume = threading.Event(), threading.Event()
    real_open = ledger.open_transaction

    def pause(record):
        inside.set()
        assert resume.wait(10)
        return real_open(record)

    monkeypatch.setattr(ledger, "open_transaction", pause)
    publisher = threading.Thread(target=tx.publish_cognitive_transaction, args=(episode, control))
    publisher.start()
    assert inside.wait(10)
    rival = threading.Thread(target=mail.release_expired_leases)
    rival.start()
    rival.join(0.3)
    assert rival.is_alive(), "the store is held for the whole publication"
    resume.set()
    publisher.join(10)
    rival.join(10)
    assert len(_lane_entry()) == 1


# ── 6. recovery ───────────────────────────────────────────────────────────────

def test_browser_evidence_is_recovered_by_value_after_b2(monkeypatch):
    episode = _episode()
    episode.auto["outputs"] = {"browser_run": {
        "status": "done", "summary": "stream the rows instead of buffering",
        "final_page": {"url": "https://example.test/csv", "title": "CSV reference",
                       "text": "streaming uses bounded memory"}}}
    control = OmegaControlDecision.from_dict(_control(decision="DONE"))
    real_pin = mail.pin_mail
    interrupted = True

    def pin(**kwargs):
        record = ledger.transaction(tx.transaction_fold_id(episode))
        assert record is not None and not record["published"]
        assert record["outgoing"][0]["pin"]["payload"]["text"] == "stream the rows instead of buffering"
        if interrupted:
            raise OSError("crash before browser evidence pin")
        return real_pin(**kwargs)

    monkeypatch.setattr(mail, "pin_mail", pin)
    with pytest.raises(OSError, match="browser evidence"):
        tx.publish_cognitive_transaction(episode, control)
    record = ledger.transaction(tx.transaction_fold_id(episode))
    assert mail.all_mail() == []
    episode.auto["outputs"]["browser_run"]["summary"] = "a later page must not replace the record"
    interrupted = False
    mail.reset_cache_for_tests()
    tx.reconcile_transactions_at_boot()
    [evidence] = mail.all_mail()
    assert evidence["payload"] == record["outgoing"][0]["pin"]["payload"]
    assert evidence["source_actor"] == "browser" and evidence["purpose"] == "lane_evidence"
    claims = mail.claim_mail(delivery_point="omega_reason", lane="chat", thread_id=THREAD,
                             episode_id="consumer", lease_s=60)
    assert mail.acknowledge_mail(claims, episode_id="consumer") == 1
    tx._complete(record["fold_id"])
    assert mail.all_mail() == []


def test_browser_evidence_over_the_payload_bound_is_withheld_not_cut():
    episode = _episode()
    text = "observed " * 10000
    episode.auto["outputs"] = {"browser_run": {
        "status": "done", "summary": text, "final_page": {}}}
    tx.publish_cognitive_transaction(episode, OmegaControlDecision.from_dict(_control(decision="DONE")))
    record = ledger.transaction(tx.transaction_fold_id(episode))
    assert record["outgoing"] == [] and mail.all_mail() == []
    assert record["withheld"][0]["payload"]["text"] == text
    assert record["withheld"][0]["disposition"] == "undeliverable"


def test_a_crash_after_the_record_and_before_staging_is_completed_from_the_record_once(monkeypatch):
    """Boundary B2."""
    episode = _episode()
    control = _fold(episode, DELTA)
    real_pin, crashing = mail.pin_mail, [True]

    def pin(**kwargs):
        if crashing[0]:
            raise OSError("the process died after the record, before publishing")
        return real_pin(**kwargs)

    monkeypatch.setattr(mail, "pin_mail", pin)
    with pytest.raises(OSError):
        tx.publish_cognitive_transaction(episode, control)
    record = ledger.transaction(tx.transaction_fold_id(episode))
    assert (record["staging"], record["published"]) == ("pending", False)
    assert ledger.staged_signals() == () and _lane_entry() == []
    crashing[0] = False
    _restart()

    assert tx.reconcile_transactions_at_boot() == [record["fold_id"]]
    assert tx.reconcile_transactions_at_boot() == []

    assert len(ledger.staged_signals()) == 1 and len(_lane_entry()) == 1
    assert ledger.staged_signals()[0].metadata == record["successor"]["metadata"]


def test_a_replayed_fold_honours_the_first_record_instead_of_staging_again():
    first = _episode("ep-1")
    tx.publish_cognitive_transaction(first, _fold(first, DELTA))
    [staged] = ledger.staged_signals()
    _restart()

    replay = _episode("ep-2")
    successor = tx.publish_cognitive_transaction(replay, _fold(replay, DELTA))

    assert ledger.staged_signals() == (staged,) and successor == staged
    assert len(_lane_entry()) == 1
    assert ledger.transaction(tx.transaction_fold_id(replay))["episode_id"] == "ep-1"


def test_the_record_closes_once_its_receipts_are_canonical():
    episode = _episode()
    tx.publish_cognitive_transaction(episode, _fold(episode, DELTA))
    fold_id = tx.transaction_fold_id(episode)

    tx.settle_cognitive_transaction(episode)
    assert ledger.transaction(fold_id)["closed"] is False, "the drain is still owed"

    for record_id in ledger.transaction(fold_id)["receipt_ids"]:
        intake_journal.mark_consumed(record_id, reason="drained in test")
    tx.settle_cognitive_transaction(episode)

    record = ledger.transaction(fold_id)
    assert record["pending_canonical"] == [] and record["closed"] is True


def test_a_ledger_written_before_transactions_existed_still_loads():
    ledger._successor_path().parent.mkdir(parents=True, exist_ok=True)
    ledger._successor_path().write_text(json.dumps({"pending": {}, "admitted": {}}), encoding="utf-8")

    assert ledger.transactions() == {} and ledger.staged_signals() == ()


def test_a_pre_v4_mail_store_written_by_t7_loads_and_upgrades():
    row = _pin_instruction()
    state = json.loads(mail.mail_path().read_text(encoding="utf-8"))
    legacy_mail = state["mail"]
    legacy_mail[row["mail_id"]]["ttl_s"] = 3600
    mail.mail_path().write_text(json.dumps({"version": 2, "mail": legacy_mail}), encoding="utf-8")
    mail.reset_cache_for_tests()

    assert [one["mail_id"] for one in mail.all_mail()] == [row["mail_id"]]
    mail.claim_mail(delivery_point="omega_reason", lane="agent", thread_id=THREAD,
                    episode_id="ep-1", lease_s=60, job_id="job-a")
    upgraded = json.loads(mail.mail_path().read_text(encoding="utf-8"))
    assert upgraded["version"] == 4 and upgraded["bindings"] == {} and upgraded["minted"] == {}


# ── 7. review round 2 (2026-09-17) ────────────────────────────────────────────

def test_memory_acceptance_is_fenced_by_mail_ownership(monkeypatch):
    """Reviewer's probe: ownership was checked, then the lease ran out before intake — and
    durable records were written from words another fold could now own. Driven through the
    chain's own fold step, with the lapse landing AFTER its ownership check."""
    _pin_instruction()
    episode = _episode()
    packet = _attach(episode)
    result = agent_pack.parse({"control": _control(), **DELTA}, packet)
    monkeypatch.setattr("omega_loop.episode.run.run_omega_for_episode", lambda _e: result)
    from omega_loop.episode import packet as packet_mod

    real_check = packet_mod.omega_mail_ownership_lost

    def check_then_lapse(ep):
        lost = real_check(ep)
        _lapse(monkeypatch)                      # the lease runs out right after the check
        return lost

    monkeypatch.setattr(packet_mod, "omega_mail_ownership_lost", check_then_lapse)
    episode_sram.open_bucket(episode.episode_id, pipeline_id=0)
    before = dict(episode.global_)

    with episode_sram.bind_episode(episode.episode_id, pipeline_id=0):
        assert chain._run_omega_episode_cognition(episode) is None

    assert intake_journal.records() == (), "nothing was accepted into memory"
    assert episode.global_ == before | {"omega_mail": episode.global_["omega_mail"]}
    assert "omega_result" not in episode.global_
    assert episode.global_["omega_mail"]["disposition"] == "ownership_lost"
    assert "no longer its own" in episode.meta["omega_fold_failure"]


def test_injections_the_store_would_refuse_never_open_a_poisoned_record():
    """Reviewer's probe: sixteen permitted 1,200-character constraints pass parsing and exceed
    the store's payload limit. The record must not be opened naming them."""
    episode = _episode()
    huge = {**DELTA, "lane_injections": [{**DELTA["lane_injections"][0],
                                          "constraints": ["c" * 1190 + str(i) for i in range(16)]}]}
    control = _fold(episode, huge)
    assert len(episode.global_["omega_result"]["lane_injections"][0]["constraints"]) == 16

    successor = tx.publish_cognitive_transaction(episode, control)

    record = ledger.transaction(tx.transaction_fold_id(episode))
    assert record["outgoing"] == [] and record["published"] is True and _lane_entry() == []
    # Reviewer's probe (2026-09-17): the REQUIRED injection vanished while its continuation
    # was staged. The continuation is declined instead, durably and loudly.
    assert successor is None and ledger.staged_signals() == ()
    assert record["withheld"][0]["requirement"] == "required"
    assert "store would refuse" in record["withheld"][0]["reason"]
    _restart()
    assert tx.reconcile_transactions_at_boot() == [], "nothing for boot to trip over"


def test_one_unpublishable_record_never_blocks_boot_or_the_other_records(monkeypatch):
    first, second = _episode("ep-1", cycle_id="c-1"), _episode("ep-2", cycle_id="c-2", arrival_seq=8, pipeline_id=1, job_id="job-b")
    real_pin, poisoned = mail.pin_mail, tx.transaction_fold_id(first)

    def pin(**kwargs):
        raise OSError("crash")

    monkeypatch.setattr(mail, "pin_mail", pin)
    for episode in (first, second):
        with pytest.raises(OSError):
            tx.publish_cognitive_transaction(episode, _fold(episode, DELTA))

    def pin_all_but_one(**kwargs):
        if kwargs["mail_id"] == ledger.transaction(poisoned)["outgoing"][0]["mail_id"]:
            raise ValueError("the store refuses this one forever")
        return real_pin(**kwargs)

    monkeypatch.setattr(mail, "pin_mail", pin_all_but_one)
    _restart()

    assert tx.reconcile_transactions_at_boot() == [tx.transaction_fold_id(second)]
    assert len(_lane_entry()) == 1 and len(ledger.staged_signals()) == 1


def test_an_empty_binding_is_restart_stable_too():
    """Reviewer's probe: the fold bound nothing; a newer instruction arrived; after a restart
    the replay consumed it. "Nothing" is a membership decision as well."""
    first = _episode("ep-1")
    assert _attach(first).cross_lane_mail == ()
    assert mail.fold_binding(fold_identity(first))["mail_ids"] == []
    newer = _pin_instruction("arrived after the fold's cutoff")
    _restart()

    replay = _episode("ep-2")

    assert _attach(replay).cross_lane_mail == ()
    assert replay.global_["omega_mail"]["replayed"] is True
    assert [(row["mail_id"], row["status"]) for row in mail.all_mail()] == [(newer["mail_id"], "ready")]


def test_a_replays_receipt_identity_ignores_other_optional_products():
    """Reviewer's probe: knowledge then a decision; the replay omitted the knowledge, and the
    identical decision moved from index 1 to 0 — two durable records."""
    first = _episode("ep-1")
    with_knowledge = {**DELTA, "knowledge_to_ingest": [
        {"domain": "coding", "statement": "SQLite ships with Python", "confidence": 0.9}]}
    packet = _attach(first)
    result = agent_pack.parse({"control": _control(), **with_knowledge}, packet)
    from dataclasses import replace
    from omega_loop.episode.types import KnowledgeProduct

    result = replace(result, knowledge_to_ingest=(
        KnowledgeProduct("coding", "SQLite ships with Python", 0.9),))
    global_ = {**first.global_, "omega_result": result.to_dict()}
    episode_sram.open_bucket("ep-1", pipeline_id=0)
    with episode_sram.bind_episode("ep-1", pipeline_id=0):
        chain._ingest_fold_knowledge(first, result, global_)
    [decision] = [r for r in global_["omega_result"]["delta_receipts"] if r["kind"] == "decision"]
    _restart()

    replay = _episode("ep-2")
    _fold(replay, DELTA)                                      # no knowledge this time

    [again] = [r for r in replay.global_["omega_result"]["delta_receipts"] if r["kind"] == "decision"]
    assert again["journal_record_id"] == decision["journal_record_id"]
    assert sum(1 for row in intake_journal.records()
               if row.payload["type"] == "decision") == 1


def test_an_evicted_binding_turns_into_a_refusal_not_a_fresh_claim(monkeypatch):
    """Reviewer's probe, with the cap reduced: a settled fold's binding was evicted, and its
    replay claimed newer mail as if it had never run."""
    monkeypatch.setattr(mail, "_MAX_BINDINGS", 1)
    _pin_instruction()
    old = _episode("ep-1", cycle_id="c-old", arrival_seq=5)
    _attach(old)
    from omega_loop.episode.packet import acknowledge_omega_mail

    acknowledge_omega_mail(old)
    _attach(_episode("ep-2", cycle_id="c-new", arrival_seq=9))          # evicts the old binding
    assert mail.fold_binding(fold_identity(old)) is None
    newer = _pin_instruction("newer words")
    _restart()

    replay = _episode("ep-3", cycle_id="c-old", arrival_seq=5)

    assert _attach(replay).cross_lane_mail == ()
    assert replay.global_["omega_mail"]["disposition"] == "beyond_replay_horizon"
    assert [row["status"] for row in mail.all_mail() if row["mail_id"] == newer["mail_id"]] == ["ready"]
    fresh = _episode("ep-4", cycle_id="c-fresh", arrival_seq=12)
    assert [row["mail_id"] for row in _attach(fresh).cross_lane_mail] == [newer["mail_id"]]


def test_a_pruned_transaction_turns_into_a_refusal_not_a_second_publication(monkeypatch):
    monkeypatch.setattr(ledger, "_MAX_CLOSED_TRANSACTIONS", 0)
    old = _episode("ep-1", cycle_id="c-old", arrival_seq=5)
    tx.publish_cognitive_transaction(old, _fold(old, DELTA))
    for record_id in ledger.transaction(tx.transaction_fold_id(old))["receipt_ids"]:
        intake_journal.mark_consumed(record_id, reason="drained in test")
    tx.settle_cognitive_transaction(old)
    [pinned] = _lane_entry()
    [claimed] = mail.claim_mail(delivery_point="lane_entry", lane="agent", thread_id=THREAD,
                                episode_id="ep-consumer", lease_s=60, job_id="job-a",
                                mail_ids=[pinned["mail_id"]])
    mail.acknowledge_mail([claimed], episode_id="ep-consumer")             # consumed
    newer = _episode("ep-2", cycle_id="c-new", arrival_seq=9, pipeline_id=1, job_id="job-b")
    tx.publish_cognitive_transaction(newer, _fold(newer, DELTA))   # prunes the old record
    assert ledger.transaction(tx.transaction_fold_id(old)) is None
    assert ledger.transaction_horizon() == 5
    _restart()

    replay = _episode("ep-3", cycle_id="c-old", arrival_seq=5)
    assert tx.publish_cognitive_transaction(replay, _fold(replay, DELTA)) is None

    assert [row["source_episode_id"] for row in _lane_entry()] == ["ep-2"], "never pinned again"
    assert ledger.transaction(tx.transaction_fold_id(replay)) is None


def test_minted_ids_are_never_evicted_only_forgotten_when_their_transaction_closes(monkeypatch):
    monkeypatch.setattr(mail, "_MAX_MINTED", 1)
    episode = _episode()
    tx.publish_cognitive_transaction(episode, _fold(episode, DELTA))
    [pinned] = _lane_entry()

    with pytest.raises(ValueError, match="capacity"):
        mail.pin_mail(thread_id=THREAD, delivery_point="lane_entry", purpose="omega_objective",
                      requirement="optional", source_actor="omega", source_episode_id="ep-x",
                      payload={"objective": "x"}, mail_id="m-other", effect_id="e-other")
    state = json.loads(mail.mail_path().read_text(encoding="utf-8"))
    assert list(state["minted"]) == [pinned["mail_id"]], "kept: its transaction is still open"

    for record_id in ledger.transaction(tx.transaction_fold_id(episode))["receipt_ids"]:
        intake_journal.mark_consumed(record_id, reason="drained in test")
    tx.settle_cognitive_transaction(episode)

    assert json.loads(mail.mail_path().read_text(encoding="utf-8"))["minted"] == {}


def test_a_fold_without_a_birth_order_has_no_restart_identity():
    episode = _episode(arrival_seq=0)

    assert fold_identity(episode) == ""
    _attach(episode)
    assert json.loads(mail.mail_path().read_text(encoding="utf-8"))["bindings"] == {} \
        if mail.mail_path().exists() else True


def test_a_closed_record_never_pins_again_once_its_minted_ids_are_forgotten():
    """After the record closes, its minted ids stop being evidence — the record itself is.
    A replay that honours a closed record must not re-pin mail that was already consumed."""
    episode = _episode()
    control = _fold(episode, DELTA)
    tx.publish_cognitive_transaction(episode, control)
    [pinned] = _lane_entry()
    [claimed] = mail.claim_mail(delivery_point="lane_entry", lane="agent", thread_id=THREAD,
                                episode_id="ep-consumer", lease_s=60, job_id="job-a",
                                mail_ids=[pinned["mail_id"]])
    mail.acknowledge_mail([claimed], episode_id="ep-consumer")
    for record_id in ledger.transaction(tx.transaction_fold_id(episode))["receipt_ids"]:
        intake_journal.mark_consumed(record_id, reason="drained in test")
    tx.settle_cognitive_transaction(episode)
    assert json.loads(mail.mail_path().read_text(encoding="utf-8"))["minted"] == {}

    tx.publish_cognitive_transaction(episode, control)

    assert _lane_entry() == []


# ── 8. review round 3 (2026-09-17) ────────────────────────────────────────────

def test_a_withheld_optional_injection_does_not_decline_the_continuation():
    episode = _episode()
    advisory = {**DELTA, "lane_injections": [{**DELTA["lane_injections"][0], "requirement": "optional",
                                              "constraints": ["c" * 1190 + str(i) for i in range(16)]}]}

    successor = tx.publish_cognitive_transaction(episode, _fold(episode, advisory))

    record = ledger.transaction(tx.transaction_fold_id(episode))
    assert successor is not None and record["staging"] == "staged"
    assert [row["requirement"] for row in record["withheld"]] == ["optional"]


def _evicted_replay(monkeypatch):
    """A fold that ran, was settled, and whose binding was then evicted; its replay."""
    from omega_loop.episode.packet import acknowledge_omega_mail

    monkeypatch.setattr(mail, "_MAX_BINDINGS", 1)
    _pin_instruction()
    old = _episode("ep-1", cycle_id="c-old", arrival_seq=5)
    _attach(old)
    acknowledge_omega_mail(old)
    _attach(_episode("ep-2", cycle_id="c-new", arrival_seq=9, pipeline_id=1))
    _restart()
    return _episode("ep-3", cycle_id="c-old", arrival_seq=5)


def test_a_replay_refused_at_the_horizon_accepts_no_memory_and_publishes_nothing(monkeypatch):
    """Reviewer's probe: the refused replay still produced two memory receipts, one outgoing
    mail and one successor — the refusal stopped at the binding."""
    replay = _evicted_replay(monkeypatch)

    control = _fold(replay, DELTA)
    assert replay.global_["omega_mail"]["disposition"] == "beyond_replay_horizon"

    assert tx.publish_cognitive_transaction(replay, control) is None
    assert intake_journal.records() == ()
    assert "delta_receipts" not in replay.global_["omega_result"]
    assert ledger.transactions() == {} and _lane_entry() == [] and ledger.staged_signals() == ()


def test_a_replay_of_a_fold_that_ran_its_course_publishes_nothing_new():
    from omega_loop.episode.packet import acknowledge_omega_mail

    _pin_instruction()
    first = _episode("ep-1")
    _attach(first)
    acknowledge_omega_mail(first)                 # DONE, nothing recorded, mail settled
    _restart()

    replay = _episode("ep-2")
    control = _fold(replay, DELTA)                # the replay now wants to continue

    assert replay.global_["omega_mail"]["disposition"] == "reconciled"
    assert tx.publish_cognitive_transaction(replay, control) is None
    assert intake_journal.records() == () and ledger.staged_signals() == ()


def test_a_refused_replay_still_honours_the_first_attempts_record():
    from omega_loop.episode.packet import acknowledge_omega_mail

    _pin_instruction()
    first = _episode("ep-1")
    tx.publish_cognitive_transaction(first, _fold(first, DELTA))
    acknowledge_omega_mail(first)
    [staged] = ledger.staged_signals()
    _restart()

    replay = _episode("ep-2")
    successor = tx.publish_cognitive_transaction(replay, _fold(replay, DELTA))

    assert replay.global_["omega_mail"]["disposition"] == "reconciled"
    assert successor == staged and ledger.staged_signals() == (staged,)


def _decisions(replay):
    return [(row["statement"], row["accepted"])
            for row in replay.global_["omega_result"].get("delta_receipts", ())]


def _journalled(kind="decision"):
    return sorted(row.payload["payload"]["summary"] for row in intake_journal.records()
                  if row.payload["type"] == kind)


A = {"statement": "Use SQLite", "rationale": "asked"}
B = {"statement": "Keep the API", "rationale": "stability"}


def test_a_replay_accepts_the_folds_original_statements_not_its_own():
    """Reviewer's probe: first [A]; the replay emits [B, A]. Slot zero returned the original A
    and slot one recorded A again."""
    first = _episode("ep-1")
    _fold(first, {"decisions": [A]})
    _restart()

    replay = _episode("ep-2")
    _fold(replay, {"decisions": [B, A]})

    assert _decisions(replay) == [("Use SQLite", True)]
    assert _journalled() == ["Use SQLite"]


def test_an_interrupted_first_attempt_is_recovered_from_its_original_payloads(monkeypatch):
    """Reviewer's probe (2026-09-17): the first attempt declares [A, B], journals A and
    crashes; the replay emits [B, A]. Counts produced two durable A records and lost B."""
    import memory.api as memory_api

    first = _episode("ep-1")
    real_intake, calls = memory_api.intake_memory, []

    def dies_on_the_second_statement(envelope, **kwargs):
        calls.append(envelope)
        if len(calls) == 2:
            raise OSError("the process died mid-intake")
        return real_intake(envelope, **kwargs)

    monkeypatch.setattr(memory_api, "intake_memory", dies_on_the_second_statement)
    with pytest.raises(OSError):
        _fold(first, {"decisions": [A, B]})
    assert _journalled() == ["Use SQLite"]
    monkeypatch.setattr(memory_api, "intake_memory", real_intake)
    _restart()

    replay = _episode("ep-2")
    _fold(replay, {"decisions": [B, A]})

    assert _journalled() == ["Keep the API", "Use SQLite"], "B recovered, A not duplicated"
    assert _decisions(replay) == [("Use SQLite", True), ("Keep the API", True)]


def test_a_fold_that_committed_nothing_gains_nothing_on_replay():
    """Reviewer's probe: a zero-statement fold published its transaction; the replay added
    decisions and constraints and two new records were accepted. [] is not None."""
    _pin_instruction()
    first = _episode("ep-1")
    tx.publish_cognitive_transaction(first, _fold(first, {}))
    frozen = mail.fold_binding(fold_identity(first))["manifest"]["result"]
    assert frozen["decisions"] == [] and frozen["user_constraints"] == []
    _restart()

    replay = _episode("ep-2")
    control = _fold(replay, DELTA)

    assert intake_journal.records() == ()
    assert "delta_receipts" not in replay.global_["omega_result"]
    tx.publish_cognitive_transaction(replay, control)
    assert _lane_entry() == [], "the first attempt's record stands; nothing new is published"


def test_an_abandoned_replay_draws_no_new_conclusions():
    """Reviewer's probe: part of the binding was lost before any transaction existed; the
    abandoned replay still produced two receipts, one outgoing mail and a successor."""
    a, _b = _pin_instruction("use sqlite"), _pin_instruction("keep the API")
    first = _episode("ep-1")
    _attach(first)
    assert ledger.transactions() == {}
    mail.reset_cache_for_tests()
    state = json.loads(mail.mail_path().read_text(encoding="utf-8"))
    state["mail"][a["mail_id"]].update(claimed_by="ep-other")      # another fold took one row
    mail.mail_path().write_text(json.dumps(state), encoding="utf-8")
    _restart()

    replay = _episode("ep-2")
    control = _fold(replay, DELTA)

    assert replay.global_["omega_mail"]["disposition"] == "abandoned"
    assert tx.publish_cognitive_transaction(replay, control) is None
    assert intake_journal.records() == ()
    assert ledger.transactions() == {} and _lane_entry() == [] and ledger.staged_signals() == ()


def _close(episode):
    fold_id = tx.transaction_fold_id(episode)
    for record_id in ledger.transaction(fold_id)["receipt_ids"]:
        intake_journal.mark_consumed(record_id, reason="drained in test")
    return fold_id


def test_an_interrupted_minted_cleanup_is_retried_until_it_completes(monkeypatch):
    """Reviewer's probe: a failure after closing and before forgetting left the minted id in
    the store for good, and with a reduced capacity it blocked every later pin."""
    monkeypatch.setattr(mail, "_MAX_MINTED", 1)
    episode = _episode()
    tx.publish_cognitive_transaction(episode, _fold(episode, DELTA))
    fold_id = _close(episode)
    real_forget, crashing = mail.forget_minted, [True]

    def forget(ids):
        if crashing[0]:
            raise OSError("died after closing, before forgetting")
        return real_forget(ids)

    monkeypatch.setattr(mail, "forget_minted", forget)
    with pytest.raises(OSError):
        tx.settle_cognitive_transaction(episode)
    record = ledger.transaction(fold_id)
    assert record["closed"] is True and record["minted_forgotten"] is False
    crashing[0] = False
    _restart()

    tx.reconcile_transactions_at_boot()

    assert ledger.transaction(fold_id)["minted_forgotten"] is True
    assert json.loads(mail.mail_path().read_text(encoding="utf-8"))["minted"] == {}
    mail.pin_mail(thread_id=THREAD, delivery_point="lane_entry", purpose="omega_objective",
                  requirement="optional", source_actor="omega", source_episode_id="ep-x",
                  payload={"objective": "x"}, mail_id="m-next", effect_id="e-next")


def test_settlement_retries_the_cleanup_too(monkeypatch):
    episode = _episode()
    tx.publish_cognitive_transaction(episode, _fold(episode, DELTA))
    fold_id = _close(episode)
    real_forget = mail.forget_minted
    monkeypatch.setattr(mail, "forget_minted", lambda ids: (_ for _ in ()).throw(OSError("died")))
    with pytest.raises(OSError):
        tx.settle_cognitive_transaction(episode)
    monkeypatch.setattr(mail, "forget_minted", real_forget)

    tx.settle_cognitive_transaction(episode)

    assert ledger.transaction(fold_id)["minted_forgotten"] is True


def test_a_closed_record_that_still_owes_its_cleanup_is_never_pruned(monkeypatch):
    monkeypatch.setattr(ledger, "_MAX_CLOSED_TRANSACTIONS", 0)
    old = _episode("ep-1", cycle_id="c-old", arrival_seq=5)
    tx.publish_cognitive_transaction(old, _fold(old, DELTA))
    fold_id = _close(old)
    monkeypatch.setattr(mail, "forget_minted", lambda ids: (_ for _ in ()).throw(OSError("died")))
    with pytest.raises(OSError):
        tx.settle_cognitive_transaction(old)

    newer = _episode("ep-2", cycle_id="c-new", arrival_seq=9, pipeline_id=1)
    tx.publish_cognitive_transaction(newer, _fold(newer, {}))

    assert ledger.transaction(fold_id) is not None, "kept until its minted ids are forgotten"


# ── 9. review round 5 (2026-09-17) ────────────────────────────────────────────

def _crash_before_the_record(episode, payload, monkeypatch, decision="PRESSURE"):
    """Boundary B1: the fold's statements are accepted, then the process dies before its
    transaction record is opened."""
    control = _fold(episode, payload, decision=decision)
    assert ledger.transactions() == {}
    return control


def test_a_replay_publishes_the_folds_original_conclusion_not_a_mixture(monkeypatch):
    """Reviewer's probe: after a B1 interruption the accepted decision and constraint said
    SQLite while the published objective said "Switch storage to PostgreSQL"."""
    first = _episode("ep-1")
    _crash_before_the_record(first, DELTA, monkeypatch)
    _restart()

    replay = _episode("ep-2")
    postgres = {
        "decisions": [{"statement": "Store todos in PostgreSQL", "rationale": "changed my mind"}],
        "user_constraints": [{"statement": "Use PostgreSQL", "source_episode_id": "note-cycle"}],
        "lane_injections": [{**DELTA["lane_injections"][0],
                             "objective": "Switch storage to PostgreSQL"}]}
    control = _fold(replay, postgres)
    tx.publish_cognitive_transaction(replay, control)

    [row] = _lane_entry()
    assert row["payload"]["objective"] == "Switch storage to SQLite"
    assert row["payload"]["decisions"] == ["Store todos in SQLite"]
    assert row["payload"]["user_constraints"] == ["Use SQLite"]
    assert "PostgreSQL" not in json.dumps(row["payload"])


def test_a_replay_settles_the_folds_original_control(monkeypatch):
    first = _episode("ep-1")
    original = _crash_before_the_record(first, DELTA, monkeypatch)            # PRESSURE
    _restart()

    replay = _episode("ep-2")
    control = _fold(replay, DELTA, decision="DONE")                            # re-derived DONE

    assert control.decision == "PRESSURE" and control.to_dict() == original.to_dict()
    assert tx.publish_cognitive_transaction(replay, control) is not None, "the continuation stands"


LONG = ("Keep every existing API route working exactly as it does today, including the bulk "
        "import endpoint, the CSV export, the webhook callbacks and the admin audit log, and "
        "keep the current response shapes so that the mobile client keeps working without a "
        "release; migrate the stored todos in place with a reversible script and verify the row "
        "counts before and after. Never delete the source data.")


def test_by_value_delivery_never_truncates_an_accepted_constraint():
    """Reviewer's probe: a 426-character accepted constraint was delivered as 280 characters,
    dropping "Never delete the source data."."""
    assert len(LONG) > 280
    episode = _episode()
    payload = {**DELTA, "user_constraints": [{"statement": LONG, "source_episode_id": "note-cycle"}]}

    tx.publish_cognitive_transaction(episode, _fold(episode, payload))

    [row] = _lane_entry()
    assert row["payload"]["user_constraints"] == [LONG]
    [receipt] = [r for r in episode.global_["omega_result"]["delta_receipts"]
                 if r["kind"] == "user_constraint"]
    assert receipt["text"] == LONG and len(receipt["statement"]) == 280


def test_a_recovered_statement_is_delivered_whole_too():
    """Replay recovery reads the journal, which must hold the whole statement as well."""
    first = _episode("ep-1")
    payload = {**DELTA, "user_constraints": [{"statement": LONG, "source_episode_id": "note-cycle"}]}
    _fold(first, payload)
    _restart()

    replay = _episode("ep-2")
    tx.publish_cognitive_transaction(replay, _fold(replay, payload))

    [row] = _lane_entry()
    assert row["payload"]["user_constraints"] == [LONG]


def test_an_over_large_by_value_payload_is_withheld_not_cut():
    episode = _episode()
    many = [{"statement": f"constraint {i}: " + "x" * 1150, "source_episode_id": "note-cycle"}
            for i in range(8)]
    payload = {"decisions": [{"statement": f"decision {i}: " + "y" * 1150, "rationale": "r"}
                             for i in range(8)],
               "user_constraints": many,
               "lane_injections": [{**DELTA["lane_injections"][0], "requirement": "optional"}]}

    tx.publish_cognitive_transaction(episode, _fold(episode, payload))

    record = ledger.transaction(tx.transaction_fold_id(episode))
    assert _lane_entry() == [] and record["outgoing"] == []
    assert "store would refuse" in record["withheld"][0]["reason"]


# ── 10. review round 6 (2026-09-17) ───────────────────────────────────────────

_HANDLERS = ("_advance_proactive_lifecycle", "_apply_onboarding_subtopics",
             "_persist_motor_attempt_and_pressure", "_apply_build_report_decision",
             "_record_trajectory_influence", "_record_signal_omega")


def _apply_with_spies(episode, result, monkeypatch):
    """The REAL fold-application path, with its downstream handlers replaced by spies."""
    seen: dict = {}
    for name in _HANDLERS:
        monkeypatch.setattr(chain, name, lambda ep, res, _name=name: seen.__setitem__(_name, res))
    episode_sram.open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    with episode_sram.bind_episode(episode.episode_id, pipeline_id=episode.pipeline_id):
        effective = chain._apply_omega_episode_result(episode, result)
    return effective, seen


def test_a_replays_fresh_result_is_no_authority_for_any_consumer(monkeypatch):
    """Reviewer's probe: frozen original control PRESSURE, replay control DONE — and the build
    lifecycle ported to completed while the outcome artifact read "Replay says the build is
    finished". One effective result must reach every consumer."""
    from dataclasses import replace

    first = _episode("ep-1")
    original = replace(agent_pack.parse({"control": _control("PRESSURE"), **DELTA}, _attach(first)),
                       build_accepted=False)
    _apply_with_spies(first, original, monkeypatch)
    _restart()

    replay = _episode("ep-2")
    done = _control("DONE")
    done["synthesis"] = "Replay says the build is finished"
    fresh = replace(agent_pack.parse({"control": done}, _attach(replay)), build_accepted=True)
    assert fresh.control_decision["decision"] == "DONE"

    effective, seen = _apply_with_spies(replay, fresh, monkeypatch)

    assert set(seen) == set(_HANDLERS)
    for name, received in seen.items():
        assert received is effective, name
    assert effective.control_decision["decision"] == "PRESSURE"
    assert effective.build_accepted is False and effective.synthesis == "summary"
    assert effective.intent_decision["intent"] == "USE_TOOL", "control projections are the original's"
    assert effective.episode_id == "ep-2"
    carrier = replay.global_["omega_result"]
    assert carrier["control_decision"]["decision"] == "PRESSURE"
    assert replay.global_["outcome"]["artifacts"]["tool_result_summary"] == "summary"
    assert "Replay says" not in json.dumps(replay.global_, default=str)


def test_the_folds_own_result_is_its_effective_result_the_first_time(monkeypatch):
    episode = _episode()
    result = agent_pack.parse({"control": _control(), **DELTA}, _attach(episode))

    effective, _seen = _apply_with_spies(episode, result, monkeypatch)

    assert effective == result
    assert mail.fold_binding(fold_identity(episode))["manifest"]["result"]["decisions"] == [
        dict(row) for row in result.decisions]


def test_a_refused_replay_with_no_frozen_result_is_discarded_not_applied(monkeypatch):
    from omega_loop.episode.packet import acknowledge_omega_mail

    _pin_instruction()
    first = _episode("ep-1")
    _attach(first)
    acknowledge_omega_mail(first)                 # ran its course; no manifest was recorded
    _restart()
    replay = _episode("ep-2")
    fresh = agent_pack.parse({"control": _control("DONE")}, _attach(replay))
    monkeypatch.setattr("omega_loop.episode.run.run_omega_for_episode", lambda _e: fresh)
    before = dict(replay.global_)

    assert chain._run_omega_episode_cognition(replay) is None

    assert "omega_result" not in replay.global_ and "outcome" not in replay.global_
    assert {k: v for k, v in replay.global_.items() if k != "omega_mail"} == {
        k: v for k, v in before.items() if k != "omega_mail"}
    assert "refused replay" in replay.meta["omega_fold_failure"]


def test_recording_the_frozen_result_is_fenced_by_mail_ownership(monkeypatch):
    _pin_instruction()
    episode = _episode()
    result = agent_pack.parse({"control": _control(), **DELTA}, _attach(episode))
    _lapse(monkeypatch)

    with pytest.raises(mail.MailOwnershipLost):
        chain._apply_omega_episode_result(episode, result)

    assert "omega_result" not in episode.global_
    assert mail.fold_binding(fold_identity(episode)) is None or \
        mail.fold_binding(fold_identity(episode))["manifest"] is None


def test_a_lease_lapsing_between_the_frozen_result_and_intake_is_caught_by_the_intake_fence(monkeypatch):
    """Two fences run in sequence — recording the frozen result, then memory acceptance — and
    the lease can run out between them. The second must hold on its own."""
    _pin_instruction()
    episode = _episode()
    result = agent_pack.parse({"control": _control(), **DELTA}, _attach(episode))
    real_record = mail.record_fold_manifest

    def record_then_lapse(fold_id, manifest):
        recorded = real_record(fold_id, manifest)
        _lapse(monkeypatch)                       # the lease runs out right after the first fence
        return recorded

    monkeypatch.setattr(mail, "record_fold_manifest", record_then_lapse)
    for name in _HANDLERS:
        monkeypatch.setattr(chain, name, lambda *_a: None)
    episode_sram.open_bucket(episode.episode_id, pipeline_id=0)
    before = {k: v for k, v in episode.global_.items() if k != "omega_mail"}

    with episode_sram.bind_episode(episode.episode_id, pipeline_id=0):
        with pytest.raises(mail.MailOwnershipLost):
            chain._apply_omega_episode_result(episode, result)

    assert intake_journal.records() == (), "nothing was accepted into memory"
    assert {k: v for k, v in episode.global_.items() if k != "omega_mail"} == before, \
        "the carrier is untouched"


def test_the_frozen_result_is_never_recorded_by_a_fold_that_lost_its_mail(monkeypatch):
    _pin_instruction()
    episode = _episode()
    result = agent_pack.parse({"control": _control(), **DELTA}, _attach(episode))
    recorded: list = []
    real_record = mail.record_fold_manifest
    monkeypatch.setattr(mail, "record_fold_manifest",
                        lambda fold_id, manifest: recorded.append(fold_id) or real_record(fold_id, manifest))
    _lapse(monkeypatch)

    with pytest.raises(mail.MailOwnershipLost):
        chain._apply_omega_episode_result(episode, result)

    assert recorded == [], "the manifest write is inside the ownership fence"


# ── 11. T10 review (2026-09-17): a replay restores the ORIGINAL derivation ─────

def _recalling(episode, memory_id):
    episode.auto["outputs"]["remembered_knowledge_tool_results"] = [
        {"tool": "remember_knowledge", "evidence": [{"memory_id": memory_id}]}]
    return episode


def test_an_interrupted_replay_journals_the_original_evidence_not_its_own(monkeypatch):
    """Reviewer's probe: the first attempt concluded over one memory and crashed before it
    journalled the decision. The replay recalled a DIFFERENT memory. T8 restores the original
    wording; the derivation must be the original's too — evidence and episode."""
    first = _recalling(_episode("ep-1"), "original-memory")
    result = agent_pack.parse({"control": _control(), **DELTA}, _attach(first))
    chain._effective_fold_result(first, result)          # frozen ... then the process dies
    frozen = mail.fold_binding(fold_identity(first))["manifest"]["derivation"]
    assert frozen == {"episode_id": "ep-1", "recall": ["original-memory"], "mail": []}
    _restart()

    replay = _recalling(_episode("ep-2"), "replay-only-memory")
    _fold(replay, {"decisions": [{"statement": "A different conclusion", "rationale": "replay"}]})

    receipts = replay.global_["omega_result"]["delta_receipts"]
    assert [row["text"] for row in receipts] == ["Store todos in SQLite", "Use SQLite"], "T8: the original"
    for row in receipts:
        provenance = intake_journal.record_by_id(row["journal_record_id"]).payload["provenance"]
        assert "derived_from:episode:ep-1" in provenance
        assert "derived_from:memory:original-memory" in provenance
        assert not any("replay-only-memory" in ref or "ep-2" in ref for ref in provenance)


def test_the_derivation_is_frozen_once_with_the_result():
    first = _recalling(_episode("ep-1"), "original-memory")
    _pin_instruction()
    result = agent_pack.parse({"control": _control(), **DELTA}, _attach(first))
    chain._effective_fold_result(first, result)
    binding = first.global_["omega_mail"]

    manifest = mail.fold_binding(fold_identity(first))["manifest"]
    assert manifest["derivation"] == {"episode_id": "ep-1", "recall": ["original-memory"],
                                      "mail": list(binding["bound_mail_ids"])}
    assert manifest["result"]["decisions"], "beside the result, in ONE record"


def test_a_manifest_written_before_derivations_existed_still_loads():
    """No stored row changes shape, so the store version does not move: a pre-T10 manifest
    loads, and its fold falls back to the derivation it can see."""
    first = _recalling(_episode("ep-1"), "original-memory")
    chain._effective_fold_result(first, agent_pack.parse({"control": _control(), **DELTA}, _attach(first)))
    state = json.loads(mail.mail_path().read_text(encoding="utf-8"))
    for binding in state["bindings"].values():
        del binding["manifest"]["derivation"]
    mail.mail_path().write_text(json.dumps(state), encoding="utf-8")
    mail.reset_cache_for_tests()

    assert set(mail.fold_binding(fold_identity(first))["manifest"]) == {"result"}
    _derivation, unsupported = chain._effective_derivation(first)
    assert "frozen before derivations" in unsupported, "loads — but its evidence is unknown"


def _strip_frozen_derivations():
    state = json.loads(mail.mail_path().read_text(encoding="utf-8"))
    for binding in state["bindings"].values():
        del binding["manifest"]["derivation"]
    mail.mail_path().write_text(json.dumps(state), encoding="utf-8")


def test_recovering_a_pre_derivation_fold_never_invents_its_provenance():
    """Reviewer's probe: a pre-T10 manifest kept the original conclusion; after a restart the
    recovery journalled it against episode:new-replay and memory:replay-only-memory."""
    first = _recalling(_episode("ep-1"), "original-memory")
    chain._effective_fold_result(first, agent_pack.parse({"control": _control(), **DELTA}, _attach(first)))
    _strip_frozen_derivations()                       # as a fold frozen before T10 left it
    _restart()
    before = len(intake_journal.records())

    replay = _recalling(_episode("ep-2"), "replay-only-memory")
    _fold(replay, {"decisions": [{"statement": "A different conclusion", "rationale": "replay"}]})

    receipts = replay.global_["omega_result"]["delta_receipts"]
    assert [row["text"] for row in receipts] == ["Store todos in SQLite", "Use SQLite"]
    assert [(row["accepted"], row["status"]) for row in receipts] == [(False, "refused")] * 2
    assert len(intake_journal.records()) == before, "nothing journalled under invented provenance"


def test_a_pre_derivation_fold_still_reports_what_it_already_journalled():
    """The statement the first attempt DID journal keeps its record and its own provenance;
    only the unfinished one is refused."""
    first = _recalling(_episode("ep-1"), "original-memory")
    packet = _attach(first)
    result = chain._effective_fold_result(first, agent_pack.parse({"control": _control(), **DELTA}, packet))
    episode_sram.open_bucket("ep-1", pipeline_id=0)
    with episode_sram.bind_episode("ep-1", pipeline_id=0):
        [journalled] = ingest_knowledge_products(
            "ep-1", [{**result.decisions[0], "kind": "decision", "confidence": 0.8}],
            replay_key=tx.transaction_fold_id(first), derived_from=["memory:original-memory"])
    _strip_frozen_derivations()
    _restart()

    replay = _recalling(_episode("ep-2"), "replay-only-memory")
    _fold(replay, {})

    decision, constraint = replay.global_["omega_result"]["delta_receipts"]
    assert decision["accepted"] and decision["journal_record_id"] == journalled["journal_record_id"]
    assert intake_journal.record_by_id(decision["journal_record_id"]).payload["provenance"] == [
        "derived_from:episode:ep-1", "derived_from:memory:original-memory"], "untouched"
    assert (constraint["accepted"], constraint["status"]) == (False, "refused")


@pytest.mark.parametrize("broken", [{"episode_id": "ep-1", "recall": "original-memory", "mail": []},
                                    {"episode_id": "ep-1", "recall": []},
                                    {"episode_id": 7, "recall": [], "mail": []},
                                    {"episode_id": "ep-1", "recall": [], "mail": [], "actor": "operator"}])
def test_a_malformed_frozen_derivation_is_an_unreadable_store_not_an_empty_one(broken):
    first = _recalling(_episode("ep-1"), "original-memory")
    chain._effective_fold_result(first, agent_pack.parse({"control": _control(), **DELTA}, _attach(first)))
    state = json.loads(mail.mail_path().read_text(encoding="utf-8"))
    for binding in state["bindings"].values():
        binding["manifest"]["derivation"] = broken
    mail.mail_path().write_text(json.dumps(state), encoding="utf-8")
    mail.reset_cache_for_tests()

    with pytest.raises(ValueError, match="binding"):
        mail.fold_binding(fold_identity(first))


# ── 12. T10 review round 5 (2026-09-17): refused knowledge gates publication ───

KNOWLEDGE = {"knowledge_to_ingest": [{"domain": "storage", "statement": "SQLite fits this app",
                                      "confidence": 0.7}],
             "lane_injections": DELTA["lane_injections"]}


def _recalling_many(episode, count):
    # A knowledge-acquiring fold: the agent lane parses knowledge_to_ingest for USE_TOOL only.
    episode.meta["execution_action_pack"]["action"] = "USE_TOOL"
    episode.auto["outputs"]["remembered_knowledge_tool_results"] = [
        {"tool": "remember_knowledge", "evidence": [{"memory_id": f"mem-{n}"} for n in range(count)]}]
    return episode


def test_refused_knowledge_withholds_the_required_injection_and_declines_the_continuation():
    """Reviewer's probe, through the real parser, intake and publication: a USE_TOOL fold's
    only statement was refused for excessive evidence — nothing journalled — and publication
    still produced a required outgoing message and a successor, with nothing withheld.
    Publication checked the delta's receipts and never looked at the knowledge statement's."""
    episode = _recalling_many(_episode(), chain._recall_ref_capacity() + 1)
    control = _fold(episode, KNOWLEDGE)
    [receipt] = episode.global_["omega_result"]["knowledge_receipts"]
    assert (receipt["accepted"], receipt["status"]) == (False, "refused")

    assert tx.publish_cognitive_transaction(episode, control) is None, "the continuation is declined"

    record = ledger.transaction(tx.transaction_fold_id(episode))
    assert _lane_entry() == [] and record["outgoing"] == [] and ledger.staged_signals() == ()
    assert (record["staging"], record["successor"]) == ("none", None)
    assert [(row["requirement"], row["disposition"]) for row in record["withheld"]] == [
        ("required", "undeliverable")]
    assert "not accepted" in record["withheld"][0]["reason"]


def test_omitted_required_mail_declines_the_dependent_continuation(monkeypatch):
    """§6: required omega_reason mail refused at selection is not write-only.

    The omission is recorded AND the continuation that would act without it is declined.
    """
    from omega_loop.episode import prompts as prompts_mod

    monkeypatch.setattr(prompts_mod, "CROSS_LANE_MAIL_BLOCK_CHARS", 50)
    omitted = _pin_instruction("z" * 500)
    episode = _episode()
    control = _fold(episode, DELTA)
    assert episode.global_["omega_mail"]["omitted_required_mail_ids"] == [omitted["mail_id"]]

    assert tx.publish_cognitive_transaction(episode, control) is None, "the continuation is declined"
    assert ledger.staged_signals() == ()
    record = ledger.transaction(tx.transaction_fold_id(episode))
    assert record is not None
    assert (record["staging"], record["successor"]) == ("none", None)


def test_accepted_knowledge_publishes_and_is_cited():
    episode = _recalling_many(_episode(), 2)
    control = _fold(episode, KNOWLEDGE)

    successor = tx.publish_cognitive_transaction(episode, control)

    [pinned] = _lane_entry()
    [receipt] = episode.global_["omega_result"]["knowledge_receipts"]
    record = ledger.transaction(tx.transaction_fold_id(episode))
    assert successor is not None and record["withheld"] == []
    assert record["receipt_ids"] == [receipt["journal_record_id"]], "the record owes its drain too"
    assert f"intake:{receipt['journal_record_id']}" in pinned["provenance"]
    assert pinned["payload"]["decisions"] == [] and pinned["payload"]["user_constraints"] == [], \
        "a knowledge statement is evidence of acceptance, not content delivered by value"


# ── 13. T11: typed causal edges from the fold (cross-lane spec section 9) ─────

from memory.causal_edges import causal_facts  # noqa: E402


def _facts(episode, kind=None):
    return [(f["source"], f["target"], f["detail"]) for f in causal_facts(episode)
            if kind is None or f["kind"] == kind]


def _pin_evidence(text="the docs say SQLite is fine"):
    return mail.pin_mail(thread_id=THREAD, delivery_point="omega_reason", purpose="lane_evidence",
                         requirement="preferred", source_actor="browser",
                         source_episode_id="ep-browser", payload={"text": text}, target_job_id="job-a")


def _successor_episode(successor, episode_id="ep-next"):
    return SimpleNamespace(
        episode_id=episode_id, opened_by="omega_successor", meta={}, pipeline_id=1,
        pressure={"contributors": [{"id": "omega_successor", "weight": 1.0,
                                    "detail": json.loads(json.dumps(successor.metadata))}]})


def _ignite(next_episode):
    """As ``_ignite_episode_cascade`` does: the successor's SRAM bucket opens first, then the
    continuation hook commits its fact into it."""
    episode_sram.open_bucket(next_episode.episode_id, pipeline_id=next_episode.pipeline_id)
    with episode_sram.bind_episode(next_episode.episode_id, pipeline_id=next_episode.pipeline_id):
        chain._record_continuation_fact(next_episode)


def test_publication_puts_the_continued_obligation_on_the_durable_successor_row():
    episode = _episode()
    control = _fold(episode, DELTA)

    successor = tx.publish_cognitive_transaction(episode, control)

    record = ledger.transaction(tx.transaction_fold_id(episode))
    assert successor.metadata["continued_from"] == {
        "fold_id": record["fold_id"], "continuation_key": record["continuation_key"]}
    assert record["successor"]["metadata"]["continued_from"] == successor.metadata["continued_from"], \
        "by value on the transaction record: it survives a crash and a B2 recovery"
    assert _facts(episode, "continued") == [], "the fold's own carrier carries no continued fact"


def test_the_successor_records_that_its_parent_continued_into_it_when_it_ignites():
    episode = _episode()
    successor = tx.publish_cognitive_transaction(episode, _fold(episode, DELTA))
    record = ledger.transaction(tx.transaction_fold_id(episode))
    next_episode = _successor_episode(successor)

    _ignite(next_episode)

    [(source, target, detail)] = _facts(next_episode, "continued")
    assert (source, target) == ("episode:ep-1", f"continuation:{successor.metadata['successor_id']}")
    assert detail == {"continuation_key": record["continuation_key"], "fold_id": record["fold_id"],
                      "continuation_episode_id": "ep-next"}
    [receipt] = causal_facts(next_episode)
    assert receipt["evidence_event_id"] == "episode-ep-next", "the continuation having happened is the proof"
    fact_record = intake_journal.record_by_id(receipt["journal_record_id"])
    assert fact_record.status == intake_journal.PENDING and fact_record.owner_episode_id == "ep-next", \
        "the fact is an intake of the successor's own bucket: durable the instant it ignites"


def test_a_replayed_ignition_presents_the_same_continued_fact_once():
    """The successor's ignition itself replayed (the process died after the fact was journalled
    and before the bucket drained): the same fold-stable event id, the record on file."""
    episode = _episode()
    successor = tx.publish_cognitive_transaction(episode, _fold(episode, DELTA))
    first = _successor_episode(successor, "ep-next")
    _ignite(first)
    [original] = causal_facts(first)
    _restart()

    again = _successor_episode(successor, "ep-next")
    _ignite(again)

    [receipt] = causal_facts(again)
    assert (receipt["journal_record_id"], receipt["replayed"]) == (original["journal_record_id"], True)
    assert len([r for r in intake_journal.records() if r.event_id == original["event_id"]]) == 1


def test_a_crash_after_staging_still_yields_the_continued_fact():
    """Reviewer's probe: the process died right after the successor was durably staged,
    before the fold's carrier recorded anything. The successor staged from the record
    ignites after the restart and records the continuation itself."""
    episode = _episode()
    successor = tx.publish_cognitive_transaction(episode, _fold(episode, DELTA))
    _restart()                                          # the parent's carrier is gone
    [staged] = ledger.staged_signals()
    assert staged.metadata["successor_id"] == successor.metadata["successor_id"]

    next_episode = _successor_episode(staged, "ep-after-restart")
    _ignite(next_episode)

    [(source, target, detail)] = _facts(next_episode, "continued")
    assert (source, target) == ("episode:ep-1", f"continuation:{staged.metadata['successor_id']}")
    assert detail["continuation_key"] == staged.metadata["continued_from"]["continuation_key"]


def test_a_b2_recovery_stages_the_obligation_with_the_successor(monkeypatch):
    """Boot completes a record left unpublished: the successor it stages carries the
    obligation, so its ignition records the fact — no carrier of the parent needed."""
    episode = _episode()
    control = _fold(episode, DELTA)
    real_complete, dying = ledger.complete_transaction, [True]

    def complete(fold_id):
        if dying[0]:
            raise OSError("died before staging")
        return real_complete(fold_id)

    # Switched off rather than undone: undoing the monkeypatch would also undo the fixtures
    # that keep this test off the live ledger.
    monkeypatch.setattr(ledger, "complete_transaction", complete)
    with pytest.raises(OSError):
        tx.publish_cognitive_transaction(episode, control)
    dying[0] = False
    _restart()

    tx.reconcile_transactions_at_boot()

    [staged] = ledger.staged_signals()
    assert staged.metadata["continued_from"]["fold_id"] == tx.transaction_fold_id(episode)
    next_episode = _successor_episode(staged, "ep-b2")
    _ignite(next_episode)
    assert [t for _s, t, _d in _facts(next_episode, "continued")] == [
        f"continuation:{staged.metadata['successor_id']}"]


@pytest.mark.parametrize("opened_by, row_id", [
    ("operator_query", "operator_query"),
    ("operator_query", "omega_successor"),      # a successor row on an episode it did not open
    ("omega_successor", "operator_query"),      # the opener's name on somebody else's row
])
def test_only_the_successor_that_opened_the_episode_records_a_continuation(opened_by, row_id):
    detail = {"successor_id": "omega-successor-x", "parent_episode_id": "ep-0",
              "continued_from": {"fold_id": "f", "continuation_key": "k"}}
    episode = SimpleNamespace(episode_id="ep-next", opened_by=opened_by, meta={}, pipeline_id=1,
                              pressure={"contributors": [{"id": row_id, "detail": detail}]})
    _ignite(episode)
    assert causal_facts(episode) == [] and intake_journal.records() == ()


def test_a_done_fold_continues_into_nothing():
    episode = _episode()
    assert tx.publish_cognitive_transaction(episode, _fold(episode, DELTA, decision="DONE")) is None
    assert _facts(episode, "continued") == []


def test_accepting_a_statement_that_rests_on_the_operators_words_records_modified():
    pinned = _pin_instruction()
    episode = _episode()

    _fold(episode, DELTA)

    receipts = episode.global_["omega_result"]["delta_receipts"]
    assert [row["accepted"] for row in receipts] == [True, True]
    assert sorted(_facts(episode, "modified")) == sorted(
        (f"mail:{pinned['mail_id']}", f"intake:{row['journal_record_id']}",
         {"purpose": "operator_instruction", "statement_kind": row["kind"]}) for row in receipts)
    assert _facts(episode, "informed") == []


def test_accepting_a_statement_that_rests_on_lane_evidence_records_informed():
    pinned = _pin_evidence()
    episode = _episode()

    _fold(episode, {"decisions": DELTA["decisions"]})

    [row] = episode.global_["omega_result"]["delta_receipts"]
    assert _facts(episode, "informed") == [
        (f"mail:{pinned['mail_id']}", f"intake:{row['journal_record_id']}",
         {"purpose": "lane_evidence", "statement_kind": "decision"})]
    assert _facts(episode, "modified") == []


def test_mail_that_changed_no_accepted_statement_writes_no_edge():
    _pin_instruction()
    _pin_evidence()
    episode = _episode()

    control = _fold(episode, {})                          # bound both, concluded nothing
    tx.publish_cognitive_transaction(episode, control)

    assert episode.global_["omega_mail"]["disposition"] == "bound"
    assert _facts(episode, "modified") == [] and _facts(episode, "informed") == []


def test_omegas_own_objective_mail_neither_modifies_nor_informs():
    """An omega_objective message bound at Global B is Omega's own intent, not evidence and
    not the operator's words: the accepted statement is edged to neither."""
    mail.pin_mail(thread_id=THREAD, delivery_point="omega_reason", purpose="omega_objective",
                  requirement="preferred", source_actor="omega", source_episode_id="ep-0",
                  payload={"objective": "finish the storage layer"}, target_job_id="job-a")
    episode = _episode()

    _fold(episode, {"decisions": DELTA["decisions"]})

    assert episode.global_["omega_mail"]["disposition"] == "bound"
    assert [row["accepted"] for row in episode.global_["omega_result"]["delta_receipts"]] == [True]
    assert causal_facts(episode) == []


def test_a_refused_statement_is_no_target():
    _pin_instruction()
    episode = _recalling_many(_episode(), chain._recall_ref_capacity() + 1)

    _fold(episode, KNOWLEDGE)

    [receipt] = episode.global_["omega_result"]["knowledge_receipts"]
    assert receipt["accepted"] is False
    assert causal_facts(episode) == []


def test_a_decision_that_supersedes_a_record_the_host_showed_it_records_superseded():
    episode = _recalling(_episode(), "mem-old")
    payload = {"decisions": [{"statement": "Store todos in SQLite", "rationale": "asked",
                              "supersedes_id": "mem-old"}]}

    _fold(episode, payload)

    [row] = episode.global_["omega_result"]["delta_receipts"]
    assert _facts(episode, "superseded") == [
        (f"intake:{row['journal_record_id']}", "memory:mem-old", {"statement_kind": "decision"})]


def test_a_supersession_the_host_cannot_resolve_is_no_edge():
    episode = _recalling(_episode(), "mem-other")
    payload = {"decisions": [{"statement": "Store todos in SQLite", "rationale": "asked",
                              "supersedes_id": "never-existed"}]}

    _fold(episode, payload)

    [row] = episode.global_["omega_result"]["delta_receipts"]
    assert row["accepted"] and row["supersedes_id"] == "never-existed"
    assert _facts(episode, "superseded") == []


def test_a_revised_decision_keeps_its_predecessor_readable_with_the_reason():
    """Acceptance item 5: a revision through the fold path does not erase the original."""
    first = _episode("ep-1")
    _fold(first, {"decisions": [{"statement": "Export through the CSV writer",
                                 "rationale": "it is already in the codebase"}]})
    [old] = first.global_["omega_result"]["delta_receipts"]
    predecessor = intake_journal.record_by_id(old["journal_record_id"])
    shown = str(predecessor.memory_id or predecessor.record_id)

    later = _recalling(_episode("ep-2", cycle_id="cycle-revision", pipeline_id=1), shown)
    _fold(later, {"decisions": [{"statement": "Export through the streaming writer",
                                 "rationale": "files larger than memory",
                                 "supersedes_id": shown}]})
    [new] = later.global_["omega_result"]["delta_receipts"]
    successor = intake_journal.record_by_id(new["journal_record_id"])
    still_there = intake_journal.record_by_id(old["journal_record_id"])

    assert still_there is not None and successor is not None
    assert still_there.record_id != successor.record_id
    assert "CSV writer" in str(still_there.payload)
    assert "streaming writer" in new["text"]
    assert new["supersedes_id"] == shown
    assert "files larger than memory" in str(successor.payload)


def test_the_folds_facts_are_intake_records_of_the_folds_own_bucket():
    """Each fact the fold established is an intake of the SAME bucket its statements went
    into — pending beside them, drained with them — and never a block on the carrier."""
    pinned = _pin_instruction()
    episode = _episode()
    control = _fold(episode, DELTA)
    tx.publish_cognitive_transaction(episode, control)

    facts = [r for r in intake_journal.records() if r.event_id.startswith("causal:")]

    assert sorted(r.payload["payload"]["enrichment"]["event_payload"]["kind"] for r in facts) == \
        ["modified", "modified"], "continued rides the SUCCESSOR's bucket (T11 review)"
    assert {r.owner_episode_id for r in facts} == {"ep-1"}
    assert all(r.status == intake_journal.PENDING for r in facts)
    assert {r.payload["payload"]["enrichment"]["event_payload"]["source"] for r in facts} == {
        f"mail:{pinned['mail_id']}"}
    assert all(r.payload["payload"]["enrichment"]["event_payload"]["evidence_event_id"] == "episode-ep-1"
               for r in facts)
    held = {i.journal_record_id for i in episode_sram.pending("ep-1")}
    assert {r.record_id for r in facts} <= held, "held in the fold's bucket, beside its statements"


def test_a_replay_of_a_pre_derivation_fold_manufactures_no_supersession(monkeypatch):
    """Reviewer's probe: the original decision proposed superseding a memory it had NOT
    recalled — no edge. With the frozen derivation gone (a pre-T10 manifest), the replay
    recalled that memory; recovery re-reported the original statement and edged it as
    superseding a record only the replay had seen."""
    first = _recalling(_episode("ep-1"), "mem-other")
    packet = _attach(first)
    payload = {"decisions": [{"statement": "Store todos in SQLite", "rationale": "asked",
                              "supersedes_id": "mem-old"}]}
    result = chain._effective_fold_result(first, agent_pack.parse({"control": _control(), **payload}, packet))
    episode_sram.open_bucket("ep-1", pipeline_id=0)
    with episode_sram.bind_episode("ep-1", pipeline_id=0):
        [journalled] = ingest_knowledge_products(
            "ep-1", [{**result.decisions[0], "kind": "decision", "confidence": 0.8}],
            replay_key=tx.transaction_fold_id(first), derived_from=["memory:mem-other"])
    assert _facts(first) == [], "never recalled: no edge the first time either"
    _strip_frozen_derivations()
    _restart()

    replay = _recalling(_episode("ep-2"), "mem-old")     # the replay recalls it
    _fold(replay, {})

    [decision] = replay.global_["omega_result"]["delta_receipts"]
    assert decision["accepted"] and decision["journal_record_id"] == journalled["journal_record_id"]
    assert _facts(replay, "superseded") == [], "the record's provenance names mem-other, not mem-old"
    assert causal_facts(replay) == [], "the replay's own recall is no proof of anything"


_REAL_RECORDER = chain._record_fold_causal_facts


def _accept_without_recording_facts(monkeypatch):
    """The first attempt: intake durably journals the statement, then the process dies before
    the facts are recorded (the recorder never runs)."""
    monkeypatch.setattr(chain, "_record_fold_causal_facts", lambda *a, **k: None)


def test_a_statement_journalled_before_the_crash_still_gets_its_facts_on_replay(monkeypatch):
    """Reviewer's probe: operator mail informs a decision; intake journals it; the process
    dies before fact recording; after the restart the decision returns accepted with
    replayed=True — and the recorder skipped it, so no modified fact ever existed."""
    pinned = _pin_instruction()
    first = _episode("ep-1")
    _accept_without_recording_facts(monkeypatch)
    _fold(first, {"decisions": DELTA["decisions"]})
    [journalled] = first.global_["omega_result"]["delta_receipts"]
    assert journalled["accepted"] and causal_facts(first) == []
    assert intake_journal.record_by_id(journalled["journal_record_id"]).status == intake_journal.PENDING
    monkeypatch.setattr(chain, "_record_fold_causal_facts", _REAL_RECORDER)
    _restart()

    replay = _episode("ep-2")
    _fold(replay, {"decisions": DELTA["decisions"]})

    [row] = replay.global_["omega_result"]["delta_receipts"]
    assert row["journal_record_id"] == journalled["journal_record_id"]
    assert _facts(replay, "modified") == [
        (f"mail:{pinned['mail_id']}", f"intake:{journalled['journal_record_id']}",
         {"purpose": "operator_instruction", "statement_kind": "decision"})]
    assert causal_facts(replay)[0]["evidence_event_id"] == "episode-ep-2", "captured with the replay"


def test_a_completed_fact_is_presented_again_but_never_duplicated(monkeypatch):
    """The first attempt's terminus ran: the fact's OWN record is committed. The replay
    re-reports the statement and presents the fact again under the same event id: the journal
    hands back the committed record, and nothing new is written."""
    _pin_instruction()
    first = _episode("ep-1")
    _fold(first, {"decisions": DELTA["decisions"]})
    [journalled] = first.global_["omega_result"]["delta_receipts"]
    [fact] = causal_facts(first)
    intake_journal.mark_committed(journalled["journal_record_id"], memory_id="mem-1")
    intake_journal.mark_committed(fact["journal_record_id"], memory_id="mem-fact-1")
    _restart()

    replay = _episode("ep-2")
    _fold(replay, {"decisions": DELTA["decisions"]})

    [row] = replay.global_["omega_result"]["delta_receipts"]
    assert row["journal_record_id"] == journalled["journal_record_id"]
    [again] = causal_facts(replay)
    assert (again["journal_record_id"], again["status"], again["replayed"]) == (
        fact["journal_record_id"], "committed", True)
    assert len([r for r in intake_journal.records() if r.event_id.startswith("causal:")]) == 1


def test_a_statement_committed_elsewhere_does_not_hide_its_pending_fact(monkeypatch):
    """Reviewer's probe: boot released the pending statement; another episode adopted and
    committed it; its fact was journalled by the first attempt but never completed. The
    replay presents the fact: its own record is still pending — recovered, not skipped, and
    not duplicated."""
    _pin_instruction()
    first = _episode("ep-1")
    _fold(first, {"decisions": DELTA["decisions"]})
    [journalled] = first.global_["omega_result"]["delta_receipts"]
    [fact] = causal_facts(first)
    _restart()
    intake_journal.reconcile_at_boot(episode_id="ep-1", pipeline_id=0, cascade_replay=False)
    adopted = {r.record_id for r in intake_journal.claim_released_records(episode_id="ep-adopter",
                                                                          pipeline_id=0)}
    assert adopted == {journalled["journal_record_id"], fact["journal_record_id"]}, \
        "the fact is released and adopted like any pending intake"
    intake_journal.mark_committed(journalled["journal_record_id"], memory_id="mem-1")
    _restart()

    replay = _episode("ep-2")
    _fold(replay, {"decisions": DELTA["decisions"]})

    [row] = replay.global_["omega_result"]["delta_receipts"]
    assert (row["journal_record_id"], row["accepted"]) == (journalled["journal_record_id"], True)
    [again] = causal_facts(replay)
    assert (again["journal_record_id"], again["status"], again["replayed"]) == (
        fact["journal_record_id"], "held", True)
    record = intake_journal.record_by_id(fact["journal_record_id"])
    assert (record.status, record.owner_episode_id) == (intake_journal.PENDING, "ep-2"), \
        "re-owned by the replay: its adopter is gone, and the replay's drain commits it"
    assert fact["journal_record_id"] in {i.journal_record_id for i in episode_sram.pending("ep-2")}
    assert len([r for r in intake_journal.records() if r.event_id.startswith("causal:")]) == 1


def test_a_statement_accepted_by_this_episode_is_still_held_and_always_edged():
    """Only a RE-REPORTED statement can have completed its obligation elsewhere: a
    statement this episode accepted is still held at fold time."""
    _pin_instruction()
    episode = _episode("ep-1")

    _fold(episode, {"decisions": DELTA["decisions"]})

    [row] = episode.global_["omega_result"]["delta_receipts"]
    assert intake_journal.record_by_id(row["journal_record_id"]).status == intake_journal.PENDING
    assert len(_facts(episode, "modified")) == 1


def test_a_re_reported_statement_the_lane_rejected_is_no_target():
    """Re-reported with its record on file but REJECTED: accepted is False even though the
    receipt carries a journal id, and only the statement that stands is edged."""
    pinned = _pin_instruction()
    first = _episode("ep-1")
    _fold(first, DELTA)
    decision, constraint = first.global_["omega_result"]["delta_receipts"]
    intake_journal.mark_rejected(decision["journal_record_id"], reason="verdict rejected it")
    _restart()

    replay = _episode("ep-2")
    _fold(replay, DELTA)

    again_decision, again_constraint = replay.global_["omega_result"]["delta_receipts"]
    assert (again_decision["accepted"], again_decision["journal_record_id"]) == (
        False, decision["journal_record_id"])
    assert again_constraint["accepted"]
    assert _facts(replay, "modified") == [
        (f"mail:{pinned['mail_id']}", f"intake:{constraint['journal_record_id']}",
         {"purpose": "operator_instruction", "statement_kind": "user_constraint"})]


def test_edges_come_from_the_records_provenance_not_the_folds_memory():
    """A fold's in-memory derivation is never the evidence: the accepted statement's journal
    record is. Here the record names the mail; the carrier's binding is what gives the
    mail its purpose."""
    pinned = _pin_instruction()
    episode = _episode()

    _fold(episode, {"decisions": DELTA["decisions"]})

    [row] = episode.global_["omega_result"]["delta_receipts"]
    provenance = intake_journal.record_by_id(row["journal_record_id"]).payload["provenance"]
    assert f"derived_from:mail:{pinned['mail_id']}" in provenance
    assert _facts(episode, "modified") == [
        (f"mail:{pinned['mail_id']}", f"intake:{row['journal_record_id']}",
         {"purpose": "operator_instruction", "statement_kind": "decision"})]
