"""Fetched-knowledge ingestion — web-fetched material becomes canonical memory.

Operator pipeline (2026-07-11): fetched content is pulled deterministically
(knowledge_extractor in AUTO's execute), then ONE chat-model inference names the
sources and writes a prose account of what was learned; that comprehension folds
back through Meta B / Global B / Omega AND is ingested here through the memory
API. This is the FETCHED-information lane: per the lobe-routing law, world
knowledge lands in lobe 2 and coding knowledge in lobe 8 — never situational,
which holds the mind's own synthesis ([[lobe-routing-fetched-vs-comprehension]],
memory/knowledge_ingest.py).

World vs coding is decided DETERMINISTICALLY from the exact persisted domain,
subtopic, title, and summary fields. The model's declared kind is advisory and
cannot override that replayable contract.
Subdomain Y comes from the dynamic-Y allocator (the same one recall resolves
through) when the lobe is flagged, else the static map.

MEMORY IS FIRST AND TRUTH: a comprehension that cannot be committed fails HARD —
an unrecorded learning action is a system failure, never a silent success.
"""

from __future__ import annotations

import logging
from typing import Any, List, Mapping, Optional

log = logging.getLogger("omega.memory.fetched_ingest")


class UnfileableDomain(ValueError):
    """The material names no domain memory can honestly file it under.

    Distinct from a commit failure: nothing was written and nothing was lost, the
    comprehension simply gave the record no home. Callers degrade the learning
    action loudly instead of shutting cognition down.
    """


def _lobe_names() -> frozenset:
    """The lobe names themselves — never legal as a domain WITHIN a lobe.

    Derived from LOBE_MAP so it cannot drift out of step with the taxonomy.
    """
    from memory.dewey_map import LOBE_MAP

    names = {"world"}  # the tag fetched_ingest writes for non-coding material
    for dirname in LOBE_MAP.values():
        # "200_knowledge" -> "knowledge"; "1100_dev" -> "dev"
        names.add(str(dirname).split("_", 1)[-1].strip().lower())
    return frozenset(names)


def resolve_domain_and_coord(lobe: int, domain: str) -> tuple:
    """Resolve a domain ONCE, returning ``(stamped_domain, coord)``.

    OPERATOR RULING 2026-08-02: "it needs to match exactly where its recorded."

    The returned domain IS the directory the coord points at. One call decides
    both, so nothing downstream can label a record one thing and file it as
    another — the disagreement is unrepresentable rather than merely detectable.

    It used to be two independent resolutions: ``payload["domain"]`` took the
    caller's raw string while ``_coord_for`` separately seeded the allocator with
    it. A LEARN episode passed ``domain="coding"`` -- a LOBE name, a key in
    neither KNOWLEDGE_DOMAINS nor CODING_DOMAINS -- and nothing checked, so the
    allocator minted ``coding`` as a subdomain OF knowledge, a fourth sibling
    beside the live curriculum (machine_learning Y=0, statistics Y=1,
    deep_learning Y=2, data_pipelines Y=3). A Machine Learning article landed
    there stamped ``coding`` and tagged ``world`` -- three names, none agreeing --
    while ``machine_learning``, its correct home, had existed for two and a half
    minutes already.

    Fails closed on a domain that names nothing (§2 no-silent-empty, and this
    module's own "MEMORY IS FIRST AND TRUTH"). Minting a subdomain from an
    unvalidated string is how the taxonomy acquires garbage that recall then has
    to resolve through forever.
    """
    raw = str(domain or "").strip()
    if not raw:
        raise UnfileableDomain(
            "illegal domain: empty — a missing domain is missing information, not a "
            "topic. Seeding the allocator with a placeholder mints a subdomain "
            "literally named 'knowledge' inside the knowledge lobe."
        )

    from memory.knowledge_router import _derive_name

    derived = _derive_name(raw, None)
    if derived in _lobe_names() or raw.strip().lower() in _lobe_names():
        raise UnfileableDomain(
            f"illegal domain {raw!r}: that is a lobe name, not a domain within one. "
            "A lobe cannot be a subdomain of itself; the caller passed the wrong field."
        )
    if derived == "general":
        raise UnfileableDomain(
            f"illegal domain {raw!r}: no salient tokens — it names no topic."
        )

    from memory.subdomain_flag import dynamic_y_lobes

    flag = dynamic_y_lobes()
    if lobe in flag:
        from memory.subdomain_allocator import get_or_create_subdomain

        y, dirname = get_or_create_subdomain(
            lobe=lobe,
            seed_text=raw,
            title=raw,
            dynamic_y_lobes=flag,
        )
        # The dirname is the authority: it is what exists on disk.
        return dirname, [lobe, y, 0]

    from memory.dewey_map import SUBDOMAIN_MAP

    rev = {name: y for (x, y), name in SUBDOMAIN_MAP.items() if x == lobe}
    if derived not in rev:
        raise UnfileableDomain(
            f"illegal domain {raw!r} for lobe {lobe}: not a known subdomain, and "
            "dynamic-Y allocation is disabled for this lobe — there is no slot to "
            "file it under that the stamped domain would honestly name."
        )
    return derived, [lobe, rev[derived], 0]


def ingest_fetched_knowledge(
    episode_id: str,
    *,
    learned: str,
    sources: List[Mapping[str, Any]],
    domain: str = "",
    kind_hint: str = "",
    artifact_id: Optional[str] = None,
    confidence: float = 0.7,
) -> dict[str, Any]:
    """Commit ONE fetched-knowledge memory (learned prose + named sources); return the receipt.

    Raises RuntimeError when the commit does not land — memory is first and
    truth (operator doctrine 2026-07-11).
    """
    from datetime import datetime, timezone

    from memory.api import CanonicalEnvelope, intake_memory, intake_receipt_accepted

    prose = str(learned or "").strip()
    if not prose:
        raise ValueError("fetched-knowledge ingest requires learned prose")
    named = [
        {
            "name": str(s.get("name") or "").strip(),
            "url": str(s.get("url") or "").strip(),
        }
        for s in (sources or [])
        if isinstance(s, Mapping)
    ]
    source_names = ", ".join(s["name"] for s in named if s["name"]) or "web fetch"
    # Lobe first, from the same replayable fields as before — a VALID domain
    # should vote on its lobe ("python" is coding). An invalid one still votes
    # here, but resolve_domain_and_coord refuses it a line later, so the vote
    # never reaches disk.
    from memory.routing.coding_vs_knowledge import classify_memory_record_lobe

    lobe = classify_memory_record_lobe(
        {
            "domain": domain,
            "subtopic": "fetched_knowledge",
            "title": f"{domain or 'knowledge'} — {source_names}",
            "summary": prose[:280],
        }
    )
    # ONE resolution. `resolved_domain` IS the directory `coord` points at, so
    # the record cannot claim one home and occupy another (operator ruling
    # 2026-08-02: "it needs to match exactly where its recorded").
    try:
        resolved_domain, coord = resolve_domain_and_coord(lobe, domain)
    except ValueError as exc:
        # The domain is MODEL-AUTHORED — the comprehension pass sometimes
        # names a lobe ('coding', live crash 2026-08-10) or nothing at all.
        # The model's word choice must never crash cognition: re-derive the
        # topic from the learned content itself, through the same salience
        # derivation the allocator already trusts. Only content that names
        # NO topic still fails (memory is first and truth — an unnameable
        # record has no home to match).
        fallback_seed = f"{kind_hint} {prose[:160]}".strip()
        log.warning(
            "fetched-knowledge domain %r rejected (%s) — deriving from content",
            domain,
            exc,
        )
        resolved_domain, coord = resolve_domain_and_coord(lobe, fallback_seed)
    payload = {
        "text": prose,
        "title": f"{resolved_domain} — {source_names}"[:120],
        "summary": prose[:280],
        "domain": resolved_domain,
        "subtopic": "fetched_knowledge",
        "sources": named,
        "knowledge_artifact_id": artifact_id or None,
        "tags": ["fetched_knowledge", "coding" if lobe == 8 else "world"],
    }

    envelope = CanonicalEnvelope(
        cycle_id=f"{episode_id}-fetched-knowledge",
        coord=coord,
        type="fetched_knowledge",
        payload=payload,
        source="omega.auto.fetched_ingest",
        confidence=max(0.0, min(1.0, float(confidence))),
        provenance=[s["url"] for s in named if s["url"]],
        # Preserve the observed fetch time explicitly on the intake envelope.
        timestamp_created=datetime.now(timezone.utc).isoformat(),
    )
    receipt = intake_memory(envelope, propose=False, bypass_guardian=True)
    status = str(getattr(receipt, "validation_status", "") or "")
    if not intake_receipt_accepted(receipt):
        raise RuntimeError(
            "fetched-knowledge intake did not land "
            f"({status or 'unknown'}: {getattr(receipt, 'message', '')}) — memory is "
            "first and truth; an unrecorded learning action is a system failure"
        )
    return {
        "coord": coord,
        "lobe": lobe,
        "kind": "coding" if lobe == 8 else "world",
        "path": getattr(receipt, "path", None),
        "status": status,
        "sources": named,
    }
