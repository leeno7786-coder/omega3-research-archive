"""The cognitive transaction: receipt-before-publish, made recoverable.

Cross-lane cognitive integration spec, section 6 (T8). The fold already ran in the right
order — memory intake, then successor staging. This module makes that order RECOVERABLE:

1. the delta was validated and ingested, and every receipt was checked for acceptance
   (``cognition_chain._ingest_fold_knowledge``; a ``held`` receipt IS acceptance);
2. ONE durable record is opened on the successor ledger (``pressure.open_transaction``)
   naming the decision digest, the accepted receipt ids, the mail cutoff and bound set, the
   outgoing ``lane_entry`` mail with its ``effect_id``s, the continuation key, the staging
   status and the receipts still owed a canonical drain — and carrying the successor row
   and the mail BY VALUE;
3. only then is the mail pinned, under identities minted from the fold's identity, so
   pinning is idempotent for good;
4. the successor is staged FROM THE RECORD, in the same ledger write that marks it staged;
5. after the episode archives, DeepSleep's drain has made the receipts canonical and the
   record closes.

**One publication per fold.** The record is keyed by the fold's stable identity
(``packet.fold_identity``), and the first record stands. A replay of the fold — a new
episode after a restart — may re-derive its conclusion, but it completes or honours the
record; it never publishes a second successor or a second copy of the mail.

**Nothing is published over mail the fold no longer owns.** Publication runs inside
``intersections.while_owned``: ownership of every bound row is proven and the record opened,
mail pinned and successor staged in one critical section of the mail store. A fold whose
lease lapsed during inference publishes nothing at all.

**A rejected statement publishes no dependent mail.** A fold's injections are its
conclusion, resting on the decisions and constraints it committed beside them; the delta
carries no finer-grained dependency, and the host never guesses one. If any of them was not
accepted, none of the fold's injections is pinned.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping

log = logging.getLogger(__name__)

def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def transaction_fold_id(episode: Any) -> str:
    """The fold's stable identity; an episode with none gets one scoped to itself."""
    from omega_loop.episode.packet import fold_identity

    return fold_identity(episode) or _digest(
        {"omega-fold": "episode", "id": str(getattr(episode, "episode_id", "") or "")})[:40]


def _emit(summary: str, *, ok: bool, **fields: Any) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("omega", "cognitive_transaction", summary=summary[:200], ok=ok,
                  status="ok" if ok else "degraded", **fields)
    except Exception:  # noqa: BLE001 - telemetry never fails cognition
        pass


def continuation_key(episode: Any, control: Any) -> str:
    """Host-derived from the WORK being continued (spec section 8): the thread, the job or
    workflow, the decision's intent and its resolution-conditions digest. Recorded and
    stamped on lane-entry mail; ownership of it is T4's, which this does not assume."""
    pressure = getattr(control, "pressure", None)
    if pressure is None:
        return ""
    global_ = dict(getattr(episode, "global_", None) or {})
    outputs = dict(dict(getattr(episode, "auto", None) or {}).get("outputs") or {})
    work = (str(dict(outputs.get("agent_run") or {}).get("job_id") or "")
            or str(dict(global_.get("browser_workflow") or {}).get("workflow_id") or ""))
    return _digest({"thread": str(getattr(episode, "thread_id", "") or ""), "work": work,
                    "intent": pressure.intent,
                    "conditions": list(pressure.resolution_conditions)})[:32]


def _outgoing(episode: Any, fold_id: str, key: str, injections, accepted) -> list[dict]:
    """The lane-entry mail this fold will publish: identities and pin arguments, by value.

    ``injections`` are ``(index, row)`` pairs: identities are minted from a row's position in
    the fold's RESULT, so withholding one injection never renames another."""
    episode_id = str(getattr(episode, "episode_id", "") or "")
    outputs = dict(dict(getattr(episode, "auto", None) or {}).get("outputs") or {})
    job_id = str(dict(outputs.get("agent_run") or {}).get("job_id") or "")
    # The WHOLE accepted statements ("text"), never the receipts' 280-character summaries:
    # a truncated constraint dropped "Never delete the source data." (review 2026-09-17).
    # If the complete payload is too large the store refuses it and the injections are
    # withheld — limits are enforced by refusal, never by cutting.
    statements = {kind: [str(row.get("text") or row["statement"]) for row in accepted
                         if row.get("kind") == kind]
                  for kind in ("decision", "user_constraint")}
    provenance = [f"episode:{episode_id}"] + [
        f"intake:{row['journal_record_id']}" for row in accepted if row.get("journal_record_id")]
    rows = []
    for index, injection in injections:       # (its index in the fold's result, the row)
        lane = str(injection["target_lane"])
        rows.append({
            # Host-minted from the fold's identity: a replay names the same mail.
            "mail_id": _digest({"fold": fold_id, "lane_entry": index})[:16],
            "effect_id": _digest({"fold": fold_id, "effect": index})[:32],
            "pin": {
                "thread_id": str(getattr(episode, "thread_id", "") or ""),
                "delivery_point": "lane_entry", "purpose": "omega_objective",
                "requirement": str(injection["requirement"]), "source_actor": "omega",
                "source_episode_id": episode_id, "target_lane": lane,
                "target_job_id": job_id if lane == "agent" else "",
                "continuation_key": key, "provenance": provenance,
                # Objective, constraints and evidence BY VALUE: what was accepted this fold is
                # durable but not yet recallable, so the consumer must never need to recall it.
                "payload": {"objective": injection["objective"],
                            "constraints": list(injection["constraints"]),
                            "evidence_refs": list(injection["evidence_refs"]),
                            "decisions": statements["decision"],
                            "user_constraints": statements["user_constraint"]},
            },
        })
    return rows


def _complete(fold_id: str):
    """Pin the record's mail and stage its successor — from the record, idempotently."""
    from global_loop import intersections as mail_store
    from pressure_loop import pressure as ledger
    from pressure_loop.omega_successor import announce_successor
    from schema.omega_control_decision import OmegaControlDecision

    with mail_store._LOCK, ledger._RESEED_LOCK:
        record = ledger.prepare_transaction_publication(fold_id)
        if not record["published"]:
            for row in record["outgoing"]:
                mail_store.pin_mail(mail_id=row["mail_id"], effect_id=row["effect_id"], **row["pin"])
        successor = ledger.complete_transaction(fold_id)
    if successor is not None and not record["published"]:
        announce_successor(OmegaControlDecision.from_dict(successor.metadata["omega_control"]))
    return successor


def _browser_evidence(episode, fold_id):
    """Publish the browser's observed result, not an executable lane directive.

    The transaction owns its by-value payload and stable identities before the pin,
    so B2 recovery finishes the same publication without consulting a live browser.
    """
    from pressure_loop.continuation import incoming

    outputs = dict(dict(getattr(episode, "auto", None) or {}).get("outputs") or {})
    result = dict(outputs.get("browser_run") or {})
    thread = str(getattr(episode, "thread_id", "") or "")
    if result.get("status") != "done" or not result.get("summary") or not thread:
        return None
    page = dict(result.get("final_page") or {})
    source = str(episode.episode_id)
    return {
        "mail_id": _digest({"fold": fold_id, "browser_evidence": True})[:16],
        "effect_id": _digest({"fold": fold_id, "browser_evidence_effect": True})[:32],
        "pin": {
            "thread_id": thread, "delivery_point": "omega_reason",
            "purpose": "lane_evidence", "requirement": "preferred",
            "source_actor": "browser", "source_episode_id": source,
            "target_lane": "", "target_job_id": str(incoming(episode).get("agent_job_id") or ""),
            "provenance": [f"episode:{source}"] + ([str(page["url"])] if page.get("url") else []),
            "payload": {"text": str(result["summary"]), "url": str(page.get("url") or ""),
                        "observation": {"title": str(page.get("title") or ""),
                                        "text": str(page.get("text") or "")}},
        },
    }


def publish_cognitive_transaction(episode: Any, control: Any, *, stage_successor: bool = True):
    """Open the fold's record, publish its mail and stage its successor. Returns the staged
    successor, or None when the fold stages none (DONE, declined, or nothing published)."""
    from global_loop import intersections as mail_store
    from omega_loop.episode.packet import (_mail_record, fold_origin_seq, fold_replay_refused,
                                           while_omega_mail_owned)
    from pressure_loop import omega_successor
    from pressure_loop import pressure as ledger

    fold_id = transaction_fold_id(episode)
    episode_id = str(getattr(episode, "episode_id", "") or "")
    from pressure_loop.continuation import incoming

    ledger.complete_continuation(incoming(episode))
    if ledger.transaction(fold_id) is not None:
        # The first record stands. Complete it if it was interrupted; publish nothing new.
        _emit("fold already has its transaction record; honoured, not replaced", ok=True,
              episode_id=episode_id, fold_id=fold_id)
        return _complete(fold_id)

    refused = fold_replay_refused(episode)
    if refused:
        # No record to honour, and the stores cannot vouch that this fold never ran: it
        # publishes NOTHING — no record, no mail, no continuation (review 2026-09-17).
        _emit(f"fold replay refused ({refused}); nothing published", ok=False,
              episode_id=episode_id, fold_id=fold_id)
        return None

    successor = omega_successor.build_omega_successor(episode, control) if stage_successor else None
    omitted = [str(one) for one in (dict(_mail_record(episode) or {}).get("omitted_required_mail_ids") or ())
               if str(one)]
    if successor is not None and omitted:
        # PREREQUISITE ENFORCEMENT (spec section 6): required omega_reason mail refused at
        # selection is not write-only. The dependent continuation does not run.
        omega_successor._decline(
            episode_id,
            "required omega_reason mail was omitted over the payload budget")
        successor = None
    omega_result = dict(dict(getattr(episode, "global_", None) or {}).get("omega_result") or {})
    # EVERY statement the fold committed beside its injections — its knowledge statements as
    # well as its decisions and constraints. A fold's injections are its conclusion, resting
    # on all of them. Checking the delta alone let a fold whose only statement was REFUSED
    # (nothing journalled) still publish a required injection and stage its continuation
    # (review 2026-09-17): a normal return from intake is not acceptance; a receipt is.
    receipts = [dict(row) for name in ("knowledge_receipts", "delta_receipts")
                for row in omega_result.get(name) or () if isinstance(row, Mapping)]
    accepted = [row for row in receipts if row.get("accepted")]
    injections = [dict(row) for row in omega_result.get("lane_injections") or ()
                  if isinstance(row, Mapping)]
    key = continuation_key(episode, control)
    # NO CONSUMER (review decision 2026-09-17). Lane-entry mail is claimed by explicit id, by
    # the continuation that names it — so an injection for a lane the successor does not
    # enter, or from a fold that stages no successor at all (DONE), could only be pinned to
    # expire unread. It is WITHHELD instead, with an explicit ``no_consumer`` disposition that
    # keeps the conclusion and its provenance on the record. Nothing is invented to receive
    # it, and the statements the fold committed to memory are untouched.
    entering = str(successor.metadata.get("omega_lane") or "") if successor is not None else ""
    indexed = list(enumerate(injections))
    orphaned = [(index, row) for index, row in indexed if str(row["target_lane"]) != entering]
    indexed = [(index, row) for index, row in indexed if str(row["target_lane"]) == entering]
    injections = [row for _index, row in indexed]
    outgoing: list[dict] = []
    withheld_reason = ""
    if injections and len(accepted) != len(receipts):
        withheld_reason = "a statement they rest on was not accepted"
    elif injections:
        outgoing = _outgoing(episode, fold_id, key, indexed, accepted)
        try:
            # The COMPLETE mail, against the store's own rules, BEFORE the record exists: a
            # record naming mail the store will always refuse can never be published, and
            # boot recovery would fail on it forever (review 2026-09-17). All or nothing —
            # a fold's injections are one conclusion.
            for row in outgoing:
                mail_store.check_pin(mail_id=row["mail_id"], effect_id=row["effect_id"],
                                     **row["pin"])
        except ValueError as exc:
            withheld_reason = f"the mail store would refuse them ({exc})"
            outgoing = []
    withheld = ([{"target_lane": row["target_lane"], "requirement": row["requirement"],
                  "disposition": "undeliverable", "reason": withheld_reason[:300]}
                 for row in injections] if withheld_reason else [])
    if orphaned:
        why = (f"the continuation enters the {entering} lane" if entering
               else "the fold stages no continuation")
        _emit(f"lane injections have no consumer ({why}); withheld, not pinned", ok=False,
              episode_id=episode_id, fold_id=fold_id, withheld=len(orphaned))
    no_consumer = [{
        "target_lane": row["target_lane"], "requirement": row["requirement"],
        "disposition": "no_consumer", "reason": f"no continuation will name it: {why}"[:300],
        # The conclusion and its provenance, by value: withheld, never dropped.
        "injection": {"index": index, "objective": row["objective"],
                      "constraints": list(row["constraints"]),
                      "evidence_refs": list(row["evidence_refs"])},
        "provenance": [f"episode:{episode_id}"] + [
            f"intake:{one['journal_record_id']}" for one in accepted if one.get("journal_record_id")],
    } for index, row in orphaned]
    if withheld:
        _emit(f"lane injections withheld: {withheld_reason}", ok=False, episode_id=episode_id,
              fold_id=fold_id, withheld=len(withheld))
        if successor is not None and any(row["requirement"] == "required" for row in withheld):
            # PREREQUISITE ENFORCEMENT (spec section 6): a continuation may not run without
            # knowledge the fold itself marked REQUIRED for it. Telemetry cannot enforce
            # that; declining the continuation does — a loud, bounded failure, durable on
            # the record below (review 2026-09-17).
            omega_successor._decline(
                episode_id, f"a required lane injection could not be delivered: {withheld_reason}")
            successor = None

    if successor is not None:
        # BEFORE the record is built: it carries the successor row by value, so the mail the
        # continuation must be handed is durable with it (spec section 4, T9).
        omega_successor.name_lane_entry_mail(successor, fold_id=fold_id, continuation_key=key,
                                             outgoing=outgoing)
        # The CONTINUED fact's obligation rides the durable successor row (spec section 9,
        # T11): the continuation key it claimed and the fold that staged it. It is discharged
        # when the successor IGNITES — from that row, on the successor's own carrier — so a
        # crash after staging, or a B2 boot recovery that stages from the record with no
        # carrier at all, still yields the fact once the continuation actually happens.
        successor.metadata["continued_from"] = {"fold_id": fold_id, "continuation_key": key}

    withheld = withheld + no_consumer
    evidence = _browser_evidence(episode, fold_id)
    if evidence is not None:
        try:
            mail_store.check_pin(mail_id=evidence["mail_id"], effect_id=evidence["effect_id"],
                                 **evidence["pin"])
        except ValueError as exc:
            reason = f"browser evidence cannot be delivered: {exc}"
            withheld.append({"purpose": "lane_evidence", "requirement": "preferred",
                             "disposition": "undeliverable", "reason": reason,
                             "payload": evidence["pin"]["payload"],
                             "provenance": evidence["pin"]["provenance"]})
            _emit(reason, ok=False, episode_id=episode_id, fold_id=fold_id)
        else:
            outgoing.append(evidence)
    binding = dict(_mail_record(episode) or {})
    bound = binding.get("disposition") == "bound"
    if successor is None and not outgoing and not bound and not accepted and not withheld:
        return None  # nothing was decided over mail, committed or published: no transaction

    from pressure_loop.pressure import _successor_row

    record = {
        "fold_id": fold_id, "episode_id": episode_id,
        "thread_id": str(getattr(episode, "thread_id", "") or ""),
        "decision_digest": _digest(control.to_dict()),
        "receipt_ids": [str(row["journal_record_id"]) for row in accepted
                        if row.get("journal_record_id")],
        "mail_cutoff_seq": int(binding.get("cutoff_seq") or 0),
        "bound_mail_ids": list(binding.get("bound_mail_ids") or ()) if bound else [],
        "outgoing": outgoing, "continuation_key": key,
        "staging": "pending" if successor is not None else "none", "published": False,
        # Held receipts are durable but not yet canonical; the DeepSleep drain owes them.
        "pending_canonical": [str(row["journal_record_id"]) for row in accepted
                              if row.get("status") == "held" and row.get("journal_record_id")],
        "successor": _successor_row(successor) if successor is not None else None,
        "opened_at": datetime.now(timezone.utc).isoformat(), "closed": False,
        "origin_seq": fold_origin_seq(episode),
        "withheld": withheld,
        # No minted ids, nothing to forget.
        "minted_forgotten": not outgoing,
        # Lane-entry delivery of each outgoing effect, recorded by its consumer (T9).
        "deliveries": {},
    }

    def publish():
        opened, _created = ledger.open_transaction(record)
        if opened is None:
            # Born at or before the ledger's replay horizon with no record left: this may be
            # a replay of a fold that already published. Refused, loudly; nothing is staged.
            _emit("fold is beyond the replay horizon; nothing published", ok=False,
                  episode_id=episode_id, fold_id=fold_id)
            return None
        return _complete(fold_id)

    try:
        # Ownership of every bound row proven, and everything published, in ONE critical
        # section of the mail store: no other consumer can take the mail in between.
        return while_omega_mail_owned(episode, publish)
    except mail_store.MailOwnershipLost as exc:
        log.warning("fold %s published nothing: %s", episode_id, exc)
        _emit("fold lost its bound mail before publication; nothing published", ok=False,
              episode_id=episode_id, fold_id=fold_id)
        return None


def settle_cognitive_transaction(episode: Any) -> None:
    """After the episode archived: record which receipts still await canonical, and close."""
    from memory import intake_journal
    from pressure_loop import pressure as ledger

    fold_id = transaction_fold_id(episode)
    record = ledger.transaction(fold_id)
    if record is None:
        return
    if record["closed"]:
        _forget_minted(record)      # a cleanup an earlier crash interrupted is retried here
        return
    owed = []
    for record_id in record["receipt_ids"]:
        row = intake_journal.record_by_id(record_id)
        if row is None or row.status not in intake_journal.TERMINAL_STATUSES:
            owed.append(record_id)
    if ledger.settle_transaction(fold_id, pending_canonical=owed):
        _forget_minted(record)


def _forget_minted(record: Mapping[str, Any]) -> None:
    """A CLOSED record never pins again, so its minted ids are no longer replay evidence.

    RECOVERABLE: the record says whether this cleanup completed, and every settlement and
    every boot retries it until it has. A crash between closing and forgetting used to leave
    the ids in the store for good, eating its capacity (review 2026-09-17).
    """
    from global_loop import intersections as mail_store
    from pressure_loop import pressure as ledger

    if record["minted_forgotten"]:
        return
    mail_store.forget_minted([row["mail_id"] for row in record["outgoing"]])
    ledger.mark_minted_forgotten(record["fold_id"])


def reconcile_transactions_at_boot() -> list[str]:
    """Boundary B2: complete every record a crash left unpublished — from the record, once.

    Also re-checks the canonical debt of published records a crash left open.
    """
    from memory import intake_journal
    from pressure_loop import pressure as ledger

    completed: list[str] = []
    for fold_id, record in ledger.transactions().items():
        try:
            if record["closed"]:
                _forget_minted(record)
                continue
            if not record["published"]:
                _complete(fold_id)
                completed.append(fold_id)
                _emit("interrupted cognitive transaction completed from its record", ok=True,
                      fold_id=fold_id, staging=record["staging"])
            owed = [one for one in record["receipt_ids"]
                    if (row := intake_journal.record_by_id(one)) is None
                    or row.status not in intake_journal.TERMINAL_STATUSES]
            if ledger.settle_transaction(fold_id, pending_canonical=owed):
                _forget_minted(record)
        except Exception as exc:  # noqa: BLE001 - one record never blocks boot or the others
            log.warning("cognitive transaction %s could not be completed at boot", fold_id,
                        exc_info=True)
            _emit(f"cognitive transaction could not be completed at boot ({exc})", ok=False,
                  fold_id=fold_id)
    return completed


__all__ = ["continuation_key", "publish_cognitive_transaction", "reconcile_transactions_at_boot",
           "settle_cognitive_transaction", "transaction_fold_id"]
