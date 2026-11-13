"""Fold-time statement ingestion — every comprehended statement becomes canonical memory.

Operator doctrine (2026-07-11): MEMORY IS FIRST AND TRUTH. An action that records
nothing to memory is a system failure. A fold that claims knowledge must land every
statement in memory IN-EPISODE with committed write receipts; zero commits against
claimed knowledge FAILS HARD (the mind must never keep running on unrecorded
learning — that is the "running on poison" class, not a foldable cognitive failure).

WHERE it lands (operator routing directive, same day): fold statements are the
MIND'S OWN COMPREHENSION of an episode, so they live in the SITUATIONAL lobe at
(4, 1) "what" — beside motor evidence in "how" (4, 5) and outcomes in "outcome"
(4, 6). The knowledge (2) and coding (8) lobes are reserved for FETCHED
information — raw external content — never for the fold's synthesis of it.

Write door is the canonical one: memory.api.write_memory (direct commit,
bypass_guardian per the guardian-scope doctrine: cognitive self-writes are never
Guardian-arbitrated). DeepSleep consolidation/semantic promotion later reads these
exactly as any situational memory.
"""

from __future__ import annotations

import logging
from typing import Any, List, Mapping

log = logging.getLogger("omega.memory.knowledge_ingest")

# (4, 1) "what" — the situational record of what the mind comprehended this
# episode. See memory.dewey_map lobe-4 facets and memory.motor_experience for
# the sibling "how"/"outcome" coords.
FOLD_STATEMENT_COORD: List[int] = [4, 1, 0]


def _product_fields(product: Any) -> tuple[str, str, float]:
    """(domain, statement, confidence) from a KnowledgeProduct or plain dict."""
    if isinstance(product, Mapping):
        domain = str(product.get("domain") or "")
        statement = str(product.get("statement") or "")
        confidence = float(product.get("confidence") or 0.5)
    else:
        domain = str(getattr(product, "domain", "") or "")
        statement = str(getattr(product, "statement", "") or "")
        confidence = float(getattr(product, "confidence", 0.5) or 0.5)
    return domain.strip(), statement.strip(), max(0.0, min(1.0, confidence))


# The kinds of fold statement. "decision" and "user_constraint" are the cognitive delta
# (cross-lane spec section 5): same lobe, same receipt path, same verdict gate.
_PRODUCT_KINDS = {"fold_statement": "knowledge", "decision": "decision",
                  "user_constraint": "constraint"}


def _product_kind(product: Any) -> str:
    kind = str(product.get("kind") or "") if isinstance(product, Mapping) else ""
    return kind if kind in _PRODUCT_KINDS else "fold_statement"


def _first_accepted(intake_journal, event_id: str, cycle_id: str):
    """The receipt and statement of the record a replayed fold event already has."""
    from memory.api import WriteReceipt

    record = intake_journal.record_by_id(intake_journal._record_id(event_id))
    if record is None:
        raise intake_journal.IntakeJournalError(f"conflicting event {event_id} has no record")
    # What the memory lane did with the FIRST presentation is this statement's outcome. Still
    # owed a drain -> held; committed, or consumed by the lane (handled once, durably) ->
    # committed. Only a rejected or uncertain first presentation stays unaccepted — a replay
    # must not fail hard over statements that were accepted the first time.
    if record.status in {intake_journal.PENDING, intake_journal.APPLYING}:
        status = "held"
    elif record.status in {intake_journal.COMMITTED, intake_journal.CONSUMED}:
        status = "committed"
    else:
        status = str(record.status)
    receipt = WriteReceipt(
        cycle_id=cycle_id, staging_ref=None, path=getattr(record, "canonical_path", None),
        validation_status=status, message="replayed: the fold's first statement stands",
        durable=True, journal_record_id=record.record_id, event_id=record.event_id,
        replayed=True)
    stored = dict(dict(record.payload or {}).get("payload") or {})
    # The FULL statement, never the 280-character summary: this text is what gets delivered
    # by value, and a cut constraint is a different constraint (review 2026-09-17).
    return receipt, str(stored.get("statement") or stored.get("summary") or "")


def ingest_knowledge_products(episode_id: str, products: Any, *, replay_key: str = "",
                              derived_from: Any = (), origin_episode_id: str = "",
                              unsupported: str = "") -> List[dict[str, Any]]:
    """Write every fold statement to situational memory; return receipts.

    Each receipt: {domain, coord, path, status, statement, kind, accepted,
    journal_record_id, event_id}. The last two are the WriteReceipt's own durable identity
    (no new identifier is minted): a ``held`` receipt is fsynced in the intake journal
    before it returns, so ``accepted`` — held or committed — IS acceptance, and dependent
    work may proceed on it and cite it.

    ``replay_key`` is a stable fold identity. With it, each statement's intake event id is
    derived from the fold rather than the episode, so a replay of the fold after a restart
    — a new episode — re-presents the SAME events and the journal answers "already have
    it" instead of recording the conclusion twice.

    A replaying caller passes the fold's RECORDED statements (its durable manifest), not the
    replay's own output, so each slot here is the slot the fold originally declared.

    STRUCTURAL PROVENANCE (spec section 7, T10). Every statement here is Omega's OWN
    conclusion, so this write site assigns the actor ``omega_inference`` — never the model —
    and commits through ``memory.cognition_write``, which requires the conclusion to name what
    it was derived from. A fold reasons over its own episode's evidence, so that episode is
    always named; ``derived_from`` adds the host-known evidence it also rested on (the
    memories it recalled, the mail it bound), and a constraint adds the operator turn it
    restates — an identity the HOST validated (T8). A decision's ``supersedes_id`` is NOT
    promoted into the derivation: it is the model's proposal, carried in the payload for the
    supersede hook, and a model naming a record is not proof the conclusion rests on it
    (review 2026-09-17). If the superseded record really was recalled, the host already
    named it.

    ``unsupported`` is WHY this fold's statements cannot be given an honest derivation — its
    evidence exceeds what a record can name, or it is a replay of a fold frozen before
    derivations were (so its original evidence is unknown). Nothing new is written then:
    provenance is never invented and never replaced by a count. A statement the fold ALREADY
    journalled is reported from that record (its provenance is on file, untouched); every
    other statement gets an UNACCEPTED receipt — a bounded outcome the publication gate
    already understands (its dependent injections are withheld), never the zero-commit crash,
    which is for a memory substrate that failed.

    ``origin_episode_id`` is the episode the conclusion AROSE in. A replay recovering a
    statement its fold originally declared passes the ORIGINAL episode (and the original
    evidence in ``derived_from``): the conclusion is the first attempt's, and so is what it
    rests on.

    Raises RuntimeError
    when statements were claimed but ZERO commits landed — memory is first and
    truth, and an episode whose learning cannot be recorded must fail loudly
    rather than close as success. Partial commits are tolerated (each rejected
    statement is logged and carried in the receipts with its status) as long as
    at least one statement landed.
    """
    from datetime import datetime, timezone

    from memory import intake_journal
    from memory.api import intake_receipt_accepted
    from memory.cognition_write import cognition_write

    rows = [p for p in (products or ()) if p is not None]
    receipts: List[dict[str, Any]] = []
    accepted = 0
    # A statement's replay identity is its ordinal WITHIN ITS OWN KIND. An index into the
    # combined list shifted whenever a replay emitted different optional products before it
    # — the identical decision became decision:1 then decision:0, two durable records
    # (review 2026-09-17).
    ordinals: dict[str, int] = {}
    for i, product in enumerate(rows):
        domain, statement, confidence = _product_fields(product)
        if not statement:
            continue
        coord = list(FOLD_STATEMENT_COORD)
        kind = _product_kind(product)
        label = _PRODUCT_KINDS[kind]
        payload = {
            "text": statement,
            "title": f"{domain or label}: {statement[:60]}",
            "summary": statement[:280],
            # The statement itself, whole. "text" may carry a rationale; "summary" is cut.
            "statement": statement,
            "domain": domain or None,
            "subtopic": kind,
            "tags": [kind, "comprehension", "fold_ingest"],
        }
        if kind != "fold_statement":
            # The delta's own fields travel by value, as provenance.
            for key in ("rationale", "supersedes_id", "source_episode_id"):
                if str(product.get(key) or "").strip():
                    payload[key] = str(product[key]).strip()
            if payload.get("rationale"):
                payload["text"] = statement + "\nRationale: " + payload["rationale"]
        suffix = "knowledge" if kind == "fold_statement" else label
        cycle_id = f"{episode_id}-{suffix}-{i}"
        rests_on = [f"episode:{origin_episode_id or episode_id}",
                    *(str(ref) for ref in derived_from or ())]
        if kind != "fold_statement" and payload.get("source_episode_id"):
            rests_on.append(f"episode:{payload['source_episode_id']}")
        ordinal = ordinals.get(kind, 0)
        ordinals[kind] = ordinal + 1
        event_id = f"fold:{replay_key}:{suffix}:{ordinal}" if replay_key else None
        if unsupported:
            receipt = None
            if event_id:
                try:
                    receipt, statement = _first_accepted(intake_journal, event_id, cycle_id)
                except intake_journal.IntakeJournalError:
                    receipt = None                      # never journalled: nothing to report
            if receipt is None:
                log.warning("fold statement refused (%s): %r", unsupported, statement[:120])
                receipts.append({
                    "domain": domain, "coord": coord, "path": None, "status": "refused",
                    "statement": statement[:280], "text": statement, "kind": kind,
                    "accepted": False, "journal_record_id": None, "event_id": None,
                    "refusal": unsupported[:300]})
                continue
        try:
            receipt = receipt if unsupported else cognition_write(
                # Assigned HERE, by the write site: the fold's statements are Omega's inference.
                actor="omega_inference", cycle_id=cycle_id, coord=coord, type=kind,
                payload=payload, confidence=confidence, derived_from=rests_on,
                # Preserve the observed event time explicitly on the intake envelope.
                timestamp_created=datetime.now(timezone.utc).isoformat(), event_id=event_id)
        except intake_journal.IntakeJournalConflict:
            # A REPLAY of this fold re-derived the statement in other words. Replaying
            # cognition is legal, recording it twice is not: the first accepted statement
            # stands, and its durable identity is what this receipt reports.
            receipt, statement = _first_accepted(intake_journal, event_id, cycle_id)
        status = str(getattr(receipt, "validation_status", "") or "")
        ok = intake_receipt_accepted(receipt)
        if ok:
            accepted += 1
        else:
            log.warning(
                "fold statement ingest rejected (%s): %s — %r",
                status,
                getattr(receipt, "message", ""),
                statement[:120],
            )
        receipts.append(
            {
                "domain": domain,
                "coord": coord,
                "path": getattr(receipt, "path", None),
                "status": status or "unknown",
                "statement": statement[:280],
                # The WHOLE validated statement. "statement" above is a display summary;
                # anything delivered by value must use this (review 2026-09-17).
                "text": statement,
                "kind": kind,
                "accepted": ok,
                "journal_record_id": getattr(receipt, "journal_record_id", None),
                "event_id": getattr(receipt, "event_id", None),
                # The model's supersession PROPOSAL, carried for the supersede hook and for
                # the host to edge (T11) — only once it resolves to a record the host knows.
                "supersedes_id": str(payload.get("supersedes_id") or ""),
            }
        )

    if receipts and accepted == 0 and not unsupported:
        raise RuntimeError(
            f"knowledge fold claimed {len(receipts)} statement(s) but ZERO memory "
            "intakes landed — memory is first and truth; an unrecorded action is "
            "a system failure (operator doctrine 2026-07-11)"
        )
    return receipts
