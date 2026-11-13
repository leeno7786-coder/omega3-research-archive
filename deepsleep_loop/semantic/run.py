"""DeepSleep Phase 5 — semantic promotion from mastery evidence."""

from __future__ import annotations

from typing import Any

from deepsleep_loop._deepsleep_state import DeepSleepInput, MasteryRecord, SemanticEntry
from deepsleep_loop.mastery_store import normalize_domain_id
from deepsleep_loop.semantic_store import load_semantic, semantic_key, upsert_semantic


def semantic_effects_from_entries(
    entries: list[SemanticEntry],
) -> dict[str, dict[str, str | int]]:
    """Build journal semantic_effects from promoted entries.

    Keyed by semantic_key(domain, statement) — NOT bare domain — so that a LEARN
    episode emitting several grounded statements in one domain promotes ALL of them
    to the cortex, not just the last (operator 2026-07-09 cascade audit: bare-domain
    keying collapsed N same-domain facts to 1 before _semantic_envelopes wrote them,
    silently dropping recall-growth; the store itself already keys per-statement).
    _semantic_envelopes reads domain/statement from the value, so the compound key
    only controls dedup — the written envelope is unchanged."""
    effects: dict[str, dict[str, str | int]] = {}
    for entry in entries:
        effects[semantic_key(entry.domain, entry.statement)] = {
            "domain": entry.domain,
            "statement": entry.statement,
            "reinforcement_count": int(entry.reinforcement_count),
            "promoted_episode": entry.promoted_episode,
        }
    return effects


def _statement_for_domain(domain_name: str) -> str:
    return f"foundational understanding of {domain_name}"


def _omega_knowledge_products(entry: Any) -> list[dict[str, Any]]:
    """Omega's comprehended knowledge for an episode (the real statements)."""
    payload = dict(entry) if isinstance(entry, dict) else {}
    omega = payload.get("omega_result")
    if not isinstance(omega, dict):
        return []
    products = omega.get("knowledge_to_ingest")
    # tuple OR list: OmegaEpisodeResult.to_dict() (asdict) preserves the tuple
    # type; a JSON round-trip yields a list. Accept both.
    if not isinstance(products, (list, tuple)):
        return []
    return [p for p in products if isinstance(p, dict)]


def _reconcile_own_promotion(entry: SemanticEntry, episode_id: str,
                             promoted: list[SemanticEntry]) -> None:
    """Report an entry THIS episode already promoted on an earlier attempt of its terminus.

    ``promoted_episode`` is the entry's receipt. A retried terminus (WO-07) finds its own first
    pass's promotion already in the store; skipping it as someone else's reported an empty effect
    and — because ``semantic_effects`` drive ``consolidate_to_memory`` — dropped the canonical
    knowledge write the first pass never reached. It is reported again, never written again.
    """
    if str(entry.promoted_episode or "") == str(episode_id or "") and entry not in promoted:
        promoted.append(entry)


def promote_semantic(
    ds_input: DeepSleepInput,
    mastery_records: list[MasteryRecord],
    *,
    semantic_path: Any = None,
) -> list[SemanticEntry]:
    """Promote domains to semantic memory when mastery criteria are met.

    Fail-hard: semantic promotion writes the knowledge substrate. A store-write /
    malformed-record failure propagates and CRASHES — it is not swallowed to []."""
    from pressure_loop.policy import (
        deepsleep_semantic_developing_threshold,
        deepsleep_semantic_path,
        deepsleep_semantic_promote_evidence,
    )

    if not mastery_records:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "deepsleep",
                "semantic_skip",
                status="skip",
                summary="no_mastery_records",
                reason="no_mastery_records",
            )
        except Exception:
            pass
        return []

    resolved_path = semantic_path or deepsleep_semantic_path()
    existing = load_semantic(resolved_path)
    evidence_threshold = deepsleep_semantic_promote_evidence()
    promoted: list[SemanticEntry] = []

    for record in mastery_records:
        if int(record.evidence_count) < evidence_threshold:
            continue
        if float(record.knowledge_mastery) < deepsleep_semantic_developing_threshold():
            continue

        domain = str(record.domain_id or normalize_domain_id(record.domain_name))
        statement = _statement_for_domain(record.domain_name or domain)
        key = semantic_key(domain, statement)
        if key in existing:
            _reconcile_own_promotion(existing[key], ds_input.episode_id, promoted)
            continue

        entry = SemanticEntry(
            domain=domain,
            statement=statement,
            reinforcement_count=int(record.evidence_count),
            promoted_episode=ds_input.episode_id,
        )
        upsert_semantic(entry, path=resolved_path)
        existing[key] = entry
        promoted.append(entry)

    # Omega's comprehended knowledge — the REAL statements (recall ∪ result),
    # ingested into the domain's subdomain so recall finds them next episode.
    # This is the "remember what I know" half of the loop.
    for entry_body in ds_input.episode_window or []:
        for product in _omega_knowledge_products(entry_body):
            domain = normalize_domain_id(str(product.get("domain") or ""))
            statement = str(product.get("statement") or "").strip()
            if not domain or not statement:
                continue
            key = semantic_key(domain, statement)
            if key in existing:
                _reconcile_own_promotion(existing[key], ds_input.episode_id, promoted)
                continue
            entry = SemanticEntry(
                domain=domain,
                statement=statement,
                reinforcement_count=1,
                promoted_episode=ds_input.episode_id,
            )
            upsert_semantic(entry, path=resolved_path)
            existing[key] = entry
            promoted.append(entry)

    return promoted
