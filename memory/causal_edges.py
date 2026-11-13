"""Typed causal edges: host-authored facts about what led to what.

Cross-lane cognitive integration spec, section 9 (T11). A causal edge is a canonical
RELATIONSHIP FACT — ``(source, kind, target)`` with the accepting episode's own canonical event
as its evidence — and the graph is a full-rebuild projection of canonical memory, so nothing
here writes ``graph.sqlite``: the compiler projects each fact into a ``MemoryEdge`` row at the
next rebuild, and the record remains the proof.

**The host writes every fact.** A fact is committed by the host at the site where the
relationship becomes a fact: accepting a statement that rests on bound mail (``modified``
for an operator's words, ``informed`` for lane evidence), accepting a decision that supersedes
a record the host showed the fold (``superseded``), archiving a window reflection
(``observed``), and a staged successor igniting (``continued``). Nothing a model says becomes
an edge.

**Every fact has its own durable record.** A fact is written through ``memory.api.
intake_memory`` into the episode's SRAM bucket — the same ingress every memory takes: the
intake journal makes it durable the instant it is accepted, DeepSleep's drain writes it to the
Dewey filesystem as a canonical situational record (``event_kind: causal_edge``), and the
compiler reads it back as an event. The fact's obligation and its completion are therefore
tracked by the fact's OWN journal record, never inferred from the statement it is about
(review 2026-09-17: a statement's commitment proved nothing about its facts — boot released
the statement, another episode adopted and committed it, and no causal-fact record existed to
recover; and a real drain committed a statement beside a still-pending fact). A pending fact
is recovered exactly as any pending intake is — boot releases it, an episode adopts it, the
drain commits it — and a replay that presents the same fact again gets the existing record
back: the fact's event id is fold-stable (its identity is the triple, not the episode), so
nothing is ever recorded twice — and a pending record whose presenter died is re-admitted into
the replay's own bucket, under the original evidence, so the replay's drain commits it.

References are the host's prefixed identifiers, the same vocabulary T10's derivations use:
``episode:``, ``continuation:``, ``mail:``, ``intake:`` (an accepted statement's journal
record), ``memory:`` (a canonical record) and ``window:``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Iterable, Mapping, Optional

from memory.taxonomy.causal import EDGE_KINDS, REFERENCE_KINDS, reference_kind

log = logging.getLogger(__name__)

SOURCE_TAG = "omega.causal_edges"
META_KEY = "causal_facts"


class CausalFactRefused(ValueError):
    """A fact the host would not record: unknown kind, a reference it does not own,
    or intake that cannot run (no open SRAM bucket)."""


def _checked_reference(value: Any, role: str) -> str:
    ref = str(value or "").strip()
    kind, _, identity = ref.partition(":")
    if kind not in REFERENCE_KINDS or not identity.strip():
        raise CausalFactRefused(f"a causal fact's {role} must be a prefixed host reference, got {ref!r}")
    return ref


def evidence_event_id_for(episode_id: str) -> str:
    """The episode's own canonical event — the id its temporal record carries."""
    return f"episode-{episode_id}"


def fact_id_for(kind: str, source: str, target: str) -> str:
    """The fact's identity is the TRIPLE: a replay presents the same fact, not a new one."""
    return hashlib.sha256(json.dumps([kind, source, target], ensure_ascii=False)
                          .encode("utf-8")).hexdigest()[:24]


def _checked_fact(episode_id: str, kind: str, source: str, target: str, detail: Any) -> dict:
    if kind not in EDGE_KINDS:
        raise CausalFactRefused(f"unknown causal edge kind {kind!r}")
    source, target = _checked_reference(source, "source"), _checked_reference(target, "target")
    return {
        "fact_id": fact_id_for(kind, source, target), "kind": kind, "source": source,
        "target": target, "evidence_event_id": evidence_event_id_for(episode_id),
        "detail": json.loads(json.dumps(dict(detail or {}), ensure_ascii=False, default=str)),
    }


def _envelope(episode_id: str, fact: Mapping[str, Any]):
    """The canonical situational record for one fact — the shape DeepSleep's own capture
    writes for an episode event, so the compiler reads it back as one."""
    from memory.api import CanonicalEnvelope
    from memory.taxonomy.canonical_capture import EVENT_KIND_TO_FACET, SITUATIONAL_FACETS

    facet_y = SITUATIONAL_FACETS.index(EVENT_KIND_TO_FACET["causal_edge"])
    summary = f"{fact['source']} {fact['kind']} {fact['target']}"
    return CanonicalEnvelope(
        cycle_id=f"{episode_id}-causal-{fact['fact_id']}",
        coord=[4, facet_y, 0],
        type="situational",
        payload={
            "title": f"Causal edge: {summary}"[:200],
            "summary": summary[:280],
            "text": f"Event kind: causal_edge\nSource: {SOURCE_TAG}\nEpisode: {episode_id}\nPayload: "
                    + json.dumps(dict(fact), ensure_ascii=False, sort_keys=True),
            "subtopic": "causal_edge",
            "tags": ["causal_edge", fact["kind"], reference_kind(fact["source"]),
                     reference_kind(fact["target"])],
            "enrichment": {
                "episode_id": episode_id,
                "event_kind": "causal_edge",
                "event_id": f"causal-{fact['fact_id']}",
                "event_source": SOURCE_TAG,
                "event_payload": dict(fact),
                "canonical": True,
            },
        },
        source=SOURCE_TAG,
        confidence=1.0,
    )


def _recorded_fact(receipt: Any) -> Optional[Mapping[str, Any]]:
    """The fact AS RECORDED, when the receipt is for a record already on file: its evidence
    and detail are the first presenter's, and the receipt must say so (T11 review, P3: a
    receipt built from the new proposal disagreed with the preserved record)."""
    from memory import intake_journal

    if not getattr(receipt, "replayed", False):
        return None
    record = intake_journal.record_by_id(str(getattr(receipt, "journal_record_id", "") or ""))
    if record is None:
        return None
    payload = dict(dict(dict(record.payload or {}).get("payload") or {}).get("enrichment") or {})
    recorded = payload.get("event_payload")
    return dict(recorded) if isinstance(recorded, Mapping) else None


def _receipt(fact: Mapping[str, Any], receipt: Any) -> dict:
    from memory.api import intake_receipt_accepted

    fact = _recorded_fact(receipt) or fact
    return {
        "fact_id": fact["fact_id"], "kind": fact["kind"], "source": fact["source"],
        "target": fact["target"], "evidence_event_id": fact["evidence_event_id"],
        "detail": dict(fact.get("detail") or {}),
        "journal_record_id": getattr(receipt, "journal_record_id", None),
        "event_id": getattr(receipt, "event_id", None),
        "status": str(getattr(receipt, "validation_status", "") or ""),
        "accepted": bool(intake_receipt_accepted(receipt)),
        # The intake receipt says whether the record was already on file: a same-content
        # replay is idempotent in the journal itself, and the conflict path reports the
        # record it found (or re-admitted).
        "replayed": bool(getattr(receipt, "replayed", False)),
    }


def _on_file(record, cycle_id: str):
    """Report the record a fact already has, without re-admitting it: it is terminal, or a
    LIVE owner still holds it in an open bucket and will drain it."""
    from memory import intake_journal
    from memory.api import WriteReceipt

    if record.status in {intake_journal.PENDING, intake_journal.APPLYING}:
        status = "held"
    elif record.status in {intake_journal.COMMITTED, intake_journal.CONSUMED}:
        status = "committed"
    else:
        status = str(record.status)
    return WriteReceipt(
        cycle_id=cycle_id, staging_ref=None, path=getattr(record, "canonical_path", None),
        validation_status=status, message="causal fact already on record", durable=True,
        journal_record_id=record.record_id, event_id=record.event_id, replayed=True)


def _represent(event_id: str, cycle_id: str):
    """The same fact, presented by ANOTHER episode (a replay after the first presenter died):
    its record is on file under the first presenter's evidence.

    A pending record whose owner is gone is RECOVERED, not merely reported: the ORIGINAL
    envelope (rebuilt from the record — its payload, evidence and event id preserved) is
    presented again through ``intake_memory``, whose replay path moves ownership to the
    presenting episode with the ownership check intake already enforces and admits the
    record into this bucket, so this episode's drain commits it (review 2026-09-17: the
    conflict handler returned a receipt and the fact stayed pending under the dead episode;
    a boot that left it for the replay — ``cascade_replay=True`` — never released it either).
    A pending record whose owner is LIVE is left with that owner — nothing is stolen — and a
    terminal record needs no recovery; both are reported as they stand.
    """
    from memory import episode_sram, intake_journal
    from memory.api import intake_memory

    record = intake_journal.record_by_id(intake_journal._record_id(event_id))
    if record is None:
        raise intake_journal.IntakeJournalError(f"conflicting causal fact {event_id} has no record")
    if record.status != intake_journal.PENDING or episode_sram.is_open(record.owner_episode_id):
        return _on_file(record, cycle_id)
    options = dict(record.write_options or {})
    return intake_memory(
        intake_journal.envelope_from_record(record),
        propose=bool(options.get("propose", False)),
        bypass_guardian=bool(options.get("bypass_guardian", True)),
        skip_embed=bool(options.get("skip_embed", False)),
        event_id=record.event_id,
    )


def commit_causal_facts(episode: Any, facts: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Commit facts through intake, as ONE batch: every fact is checked before any is written,
    so a refusal leaves nothing partial. Returns one receipt per fact.

    There is no count cap: the facts a fold can produce are bounded by what memory ACCEPTED
    (each target is an accepted intake record) times the binder's row limit — limits that
    already exist on both sides.
    """
    from memory import intake_journal
    from memory.api import intake_memory

    episode_id = str(getattr(episode, "episode_id", "") or "")
    if not episode_id:
        raise CausalFactRefused("a causal fact needs the episode it is evidenced by")
    checked = [_checked_fact(episode_id, str(row.get("kind")), row.get("source"), row.get("target"),
                             row.get("detail")) for row in facts]
    receipts: list[dict] = []
    for fact in checked:
        envelope = _envelope(episode_id, fact)
        event_id = f"causal:{fact['fact_id']}"
        try:
            receipt = intake_memory(envelope, propose=False, bypass_guardian=True, event_id=event_id)
        except intake_journal.IntakeJournalConflict:
            # The same fact, presented by another episode (a replay, or a second fold that
            # established the same relationship): the record on file stands, under the
            # evidence of the episode that first presented it — recovered into THIS bucket
            # if its owner is gone.
            receipt = _represent(event_id, envelope.cycle_id)
        except RuntimeError as exc:
            # Could-not-run must not leak a substrate RuntimeError: the seam's named
            # refusal is CausalFactRefused, with the SRAM cause preserved.
            if "no episode SRAM bucket is open" not in str(exc):
                raise
            raise CausalFactRefused(str(exc)) from exc
        receipts.append(_receipt(fact, receipt))
    _note(episode, receipts)
    return receipts


def commit_causal_fact(episode: Any, *, kind: str, source: str, target: str,
                       detail: Optional[Mapping[str, Any]] = None) -> dict:
    """Commit one fact (a batch of one). Returns its receipt."""
    [receipt] = commit_causal_facts(episode, [{"kind": kind, "source": source, "target": target,
                                               "detail": detail}])
    return receipt


def _note(episode: Any, receipts: list[dict]) -> None:
    """Receipts on the carrier, for the fold's own record of what it edged. NOT the
    durability: the journal is."""
    meta = getattr(episode, "meta", None)
    if not isinstance(meta, dict):
        meta = dict(meta or {})
        try:
            episode.meta = meta
        except Exception:  # noqa: BLE001 - a carrier that cannot take a note still committed
            return
    known = {row.get("fact_id") for row in meta.get(META_KEY) or () if isinstance(row, Mapping)}
    meta[META_KEY] = [dict(row) for row in meta.get(META_KEY) or () if isinstance(row, Mapping)] + [
        row for row in receipts if row["fact_id"] not in known]


def causal_facts(episode_or_view: Any) -> list[dict]:
    """The fact receipts noted on an episode carrier (or its serialized view)."""
    if isinstance(episode_or_view, Mapping):
        meta = episode_or_view.get("meta") or {}
    else:
        meta = getattr(episode_or_view, "meta", None) or {}
    return [dict(row) for row in (dict(meta).get(META_KEY) or ()) if isinstance(row, Mapping)
            and row.get("kind") in EDGE_KINDS]


__all__ = ["CausalFactRefused", "EDGE_KINDS", "META_KEY", "REFERENCE_KINDS", "SOURCE_TAG",
           "causal_facts", "commit_causal_fact", "commit_causal_facts", "evidence_event_id_for",
           "fact_id_for", "reference_kind"]
