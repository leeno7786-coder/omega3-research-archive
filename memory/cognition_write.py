"""The ONE cognition write boundary: structural epistemic provenance.

Cross-lane cognitive integration spec, section 7 (T10). Every cross-lane commit — a fold's
statements, decisions and constraints; a window reflection's synthesis — enters memory through
``cognition_write``. It exists so that WHO concluded something is a structural fact of the
record rather than something its text happens to say.

**The host assigns the actor, from the write site.** ``actor`` is a literal at each call
site, one of a closed set. Nothing in the payload can name it: a model that emits
``source_actor``, ``source``, ``source_type``, ``actor`` or ``derived_from`` — or any CITATION
field (``source_id``, the span and session fields), which promotion would reuse in place of
the host's own — has those keys STRIPPED before the envelope is built, because the payload is
model-adjacent data and those fields are host authority.

**A derived product names what it was derived from.** ``omega_inference`` and ``reflection``
are Omega's own conclusions — about the world, and about its own history. Each requires a
non-empty ``derived_from``: host-written references to the memories and receipts it rests on.
A conclusion that cannot say what it rests on is refused, not recorded.

**The class is structural.** The actor becomes the envelope's ``source``. Promotion records
the source-ledger artifact under ``source_type = envelope.source`` and cites it in the
canonical record's ``provenance.source_type`` — so retrieving Omega's own inference or
reflection can never be mistaken for independent supporting evidence, whatever its text
claims. ``derived_from`` rides the envelope's existing ``provenance`` list, which the intake
journal makes durable with the event; the journal remains the proof. No new top-level memory
field, no second writer: this module only BUILDS the envelope and hands it to
``memory.api.intake_memory``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

SOURCE_ACTORS = frozenset({
    "external_source", "operator", "omega_inference", "agent_observation", "reflection",
    "verified_artifact",
})
# Omega's OWN derived conclusions: an inference about the world, a reflection on its history.
DERIVED_ACTORS = frozenset({"omega_inference", "reflection"})
# Host authority. A payload is model-adjacent data and may never carry these.
#  - the actor and the derivation;
#  - CITATION authority. Promotion REUSES a payload's ``source_id`` instead of recording a
#    source under the envelope's actor, so a payload naming an existing external artifact had
#    its inference cite that artifact, with no ``provenance.source_type`` at all (review
#    2026-09-17). A cognition write's citation is always recorded by the host, under the actor.
_HOST_KEYS = frozenset({
    "source_actor", "actor", "source", "source_type", "derived_from",
    "source_id", "span_start", "span_end", "span_hash", "source_content_hash",
    "source_session_id", "source_session_timestamp", "session_timestamp",
})
# Sized to hold everything SELECTION can legitimately admit for one conclusion, so a valid
# fold can never be refused here (review 2026-09-17: 32 bound mail + 32 recalled memories +
# the mandatory episode references exceeded a limit of 64, and intake refused a real
# decision). The fold's side of the alignment — its mandatory references, the binder's mail
# row limit and the recall capacity derived from THIS number — is
# ``cognition_chain._recall_ref_capacity``; a test pins the two together.
#
# The references are INLINE and COMPLETE: the journal record is the one thing tied to the
# memory's lifetime, so it — not an evictable binding, and never a count — holds what a
# conclusion rests on (review 2026-09-17). A conclusion whose evidence exceeds this is not
# recorded with a placeholder; the fold refuses it (``cognition_chain``), as a bounded outcome.
MAX_DERIVED_FROM = 1024
DERIVED_FROM_PREFIX = "derived_from:"


def is_independent_evidence(source_type: str) -> bool:
    """False for Omega's own inference or reflection — never supporting evidence."""
    return str(source_type or "") not in DERIVED_ACTORS


class CognitionWriteRefused(ValueError):
    """The write was refused at the boundary. Nothing reached intake."""


def _checked_derived_from(actor: str, derived_from: Any) -> list[str]:
    if isinstance(derived_from, (str, bytes)) or not isinstance(derived_from, Iterable):
        raise CognitionWriteRefused("derived_from must be a list of memory or receipt ids")
    refs: list[str] = []
    for ref in derived_from:
        if not isinstance(ref, str) or not ref.strip():
            raise CognitionWriteRefused("derived_from names memory or receipt ids, as strings")
        if ref.strip() not in refs:
            refs.append(ref.strip())
    if actor in DERIVED_ACTORS and not refs:
        raise CognitionWriteRefused(
            f"a {actor} write must name what it was derived from (derived_from is empty)")
    if len(refs) > MAX_DERIVED_FROM:
        raise CognitionWriteRefused(f"derived_from names more than {MAX_DERIVED_FROM} ids")
    return refs


def cognition_envelope(*, actor: str, cycle_id: str, coord: list, type: str,  # noqa: A002
                       payload: Mapping[str, Any], confidence: float,
                       derived_from: Iterable[str] = (),
                       timestamp_created: Optional[str] = None):
    """The envelope for one cognition write, or ``CognitionWriteRefused``."""
    from memory.api import CanonicalEnvelope

    if actor not in SOURCE_ACTORS:
        raise CognitionWriteRefused(
            f"unknown source_actor {actor!r}: the write site assigns one of {sorted(SOURCE_ACTORS)}")
    if not isinstance(payload, Mapping):
        raise CognitionWriteRefused("a cognition write carries a payload object")
    refs = _checked_derived_from(actor, derived_from)
    return CanonicalEnvelope(
        cycle_id=cycle_id, coord=list(coord), type=type,
        # Whatever the payload claimed about its own origin is dropped, never trusted.
        payload={key: value for key, value in payload.items() if key not in _HOST_KEYS},
        # STRUCTURAL: promotion records the source ledger artifact under this, and the
        # canonical record cites it as provenance.source_type.
        source=actor,
        confidence=confidence,
        provenance=[DERIVED_FROM_PREFIX + ref for ref in refs],
        timestamp_created=timestamp_created,
    )


def cognition_write(*, actor: str, cycle_id: str, coord: list, type: str,  # noqa: A002
                    payload: Mapping[str, Any], confidence: float,
                    derived_from: Iterable[str] = (), timestamp_created: Optional[str] = None,
                    event_id: Optional[str] = None):
    """Commit one cross-lane cognition product through ``intake_memory``. Returns its
    ``WriteReceipt``; raises ``CognitionWriteRefused`` before anything is written."""
    from memory.api import intake_memory

    envelope = cognition_envelope(
        actor=actor, cycle_id=cycle_id, coord=coord, type=type, payload=payload,
        confidence=confidence, derived_from=derived_from, timestamp_created=timestamp_created)
    return intake_memory(envelope, propose=False, bypass_guardian=True, event_id=event_id)


def derivation_of(memory_id: str) -> Optional[dict]:
    """What a CANONICAL memory was derived from, resolved through the intake journal.

    The journal is the authoritative record of a cognition write's actor and derivation, and
    it binds each record to the canonical memory it became (``memory_id``, set at commit). This
    is that binding read backwards — the lookup T11's causal edges need — so neither a second
    writer nor a new memory field is required. ``None`` when no journalled cognition write
    became this memory.
    """
    from memory import intake_journal

    record = intake_journal.record_for_memory(memory_id)
    if record is None:
        return None
    envelope = dict(record.payload or {})
    actor = str(envelope.get("source") or "")
    if actor not in SOURCE_ACTORS:
        return None
    return {"actor": actor, "journal_record_id": record.record_id, "event_id": record.event_id,
            "derived_from": [str(ref)[len(DERIVED_FROM_PREFIX):]
                             for ref in envelope.get("provenance") or ()
                             if str(ref).startswith(DERIVED_FROM_PREFIX)]}


__all__ = ["CognitionWriteRefused", "DERIVED_ACTORS", "DERIVED_FROM_PREFIX", "MAX_DERIVED_FROM",
           "SOURCE_ACTORS",
           "cognition_envelope", "cognition_write", "derivation_of", "is_independent_evidence"]
