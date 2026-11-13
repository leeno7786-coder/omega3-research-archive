"""Process queued contradiction candidates via governed adjudication rules."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.memory_config import contradiction_rules

log = logging.getLogger("omega.memory.contradiction_resolution")

_REASON_RE = re.compile(r"^contradiction:canonical=(?P<canonical>[^;]+);evidence=(?P<evidence>.+)$")


def parse_contradiction_reason(reason: str) -> dict[str, Any] | None:
    text = str(reason or "").strip()
    match = _REASON_RE.match(text)
    if not match:
        return None
    canonical = str(match.group("canonical") or "").strip()
    evidence_raw = str(match.group("evidence") or "").strip()
    evidence = [part.strip() for part in evidence_raw.split(",") if part.strip()]
    if not canonical or not evidence:
        return None
    return {"canonical_id": canonical, "evidence_ids": evidence, "reason": text}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _confidence(doc: dict[str, Any]) -> float:
    provenance = doc.get("provenance") if isinstance(doc.get("provenance"), dict) else {}
    raw = provenance.get("confidence", doc.get("confidence"))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _is_canonical(doc: dict[str, Any]) -> bool:
    flags = doc.get("flags") if isinstance(doc.get("flags"), dict) else {}
    return bool(flags.get("canonical") or doc.get("canonical"))


def apply_loser_penalty(
    doc: dict[str, Any],
    adjudication: dict[str, Any],
) -> dict[str, Any]:
    """Mark loser and subtract governed confidence penalty in-place."""
    loser_id = str(adjudication.get("loser_id") or "")
    if str(doc.get("id") or "") != loser_id:
        return {"applied": False, "reason": "not_loser", "memory_id": doc.get("id")}

    if _is_canonical(doc):
        return {
            "applied": False,
            "reason": "canonical_immune",
            "memory_id": loser_id,
        }

    rules = contradiction_rules()
    penalty = float(
        adjudication.get("loser_penalty") or rules.get("loser_confidence_penalty") or 0.30
    )
    demotion_below = float(rules.get("demotion_eligible_below") or 0.25)

    provenance = doc.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
        doc["provenance"] = provenance

    before = _confidence(doc)
    after = max(0.0, before - penalty)
    provenance["confidence"] = after

    audit = list(doc.get("audit") or [])
    audit.append(
        {
            "when": _now_iso(),
            "what": "contradiction_adjudicated",
            "by": "memory.contradiction_resolution",
            "notes": (
                f"winner={adjudication.get('winner_id')}; "
                f"confidence {before:.3f}->{after:.3f}; "
                f"penalty={penalty:.3f}"
            ),
        }
    )
    doc["audit"] = audit

    return {
        "applied": True,
        "memory_id": loser_id,
        "confidence_before": before,
        "confidence_after": after,
        "penalty": penalty,
        "demotion_eligible": after < demotion_below,
    }


def _atomic_write_at_path(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def persist_adjudication_outcome(adjudication: dict[str, Any]) -> dict[str, Any]:
    """Load loser memory, apply penalty, and persist to disk."""

    def _emit_adjudication(outcome: dict[str, Any]) -> None:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "adjudication_persist",
                status="ok" if outcome.get("persisted") else "fail",
                ok=bool(outcome.get("persisted")),
                summary=str(outcome.get("reason") or ""),
                loser_id=outcome.get("memory_id") or adjudication.get("loser_id"),
                persisted=bool(outcome.get("persisted")),
                confidence_before=outcome.get("confidence_before"),
                confidence_after=outcome.get("confidence_after"),
                reason=outcome.get("reason"),
            )
        except Exception:
            pass

    loser_id = str(adjudication.get("loser_id") or "").strip()
    if not loser_id:
        outcome = {"persisted": False, "reason": "missing_loser_id"}
        _emit_adjudication(outcome)
        return outcome

    doc = _load_memory_document(loser_id)
    if not doc:
        outcome = {"persisted": False, "reason": "loser_not_found", "memory_id": loser_id}
        _emit_adjudication(outcome)
        return outcome

    # GRADUATED RESOLUTION (operator 2026-07-15): a DECISIVELY out-corroborated non-canonical loser
    # (a lone typo losing to a multiply-corroborated / canonical value) is TOMBSTONED — reversibly
    # archived + removed. A near-tie takes the softer penalty/demote path below (never a hard
    # removal on a coin-flip). Canonical records are IMMUNE (defense in depth; tombstone re-checks).
    decisive = bool(adjudication.get("conflict", True)) and bool(adjudication.get("decisive"))
    if decisive and not _is_canonical(doc):
        from memory.tombstone import tombstone_memory_record

        tomb = tombstone_memory_record(
            loser_id,
            reason=f"contradiction_loser;winner={adjudication.get('winner_id')}",
        )
        if tomb.get("tombstoned"):
            outcome = {
                "persisted": True,
                "resolution": "tombstoned",
                "memory_id": loser_id,
                "tombstone": tomb,
            }
            _emit_adjudication(outcome)
            return outcome
        # Tombstone failed (e.g. archive I/O) — fall through to the soft demote path so the loser
        # is still weakened rather than left untouched.
        log.warning(
            "tombstone of decisive loser %s failed (%s); falling back to demotion",
            loser_id,
            tomb.get("reason"),
        )

    mutation = apply_loser_penalty(doc, adjudication)
    if not mutation.get("applied"):
        outcome = {"persisted": False, **mutation}
        _emit_adjudication(outcome)
        return outcome

    from memory.demotion import (
        count_contradiction_strikes,
        demote_one_lifecycle_stage,
        demotion_trigger_after_contradiction,
    )

    should_demote, demote_reason = demotion_trigger_after_contradiction(
        doc,
        confidence_after=float(mutation.get("confidence_after") or 0.0),
    )
    mutation["contradiction_strikes"] = count_contradiction_strikes(doc)
    demotion: dict[str, Any] = {}
    if should_demote:
        demotion = demote_one_lifecycle_stage(doc, reason=demote_reason)
        mutation["demotion"] = demotion
    mutation["resolution"] = "demoted" if demotion.get("demoted") else "penalized"

    try:
        from memory.engine.validate_write import atomic_write

        atomic_write(doc, update_index=False)
        outcome = {"persisted": True, **mutation}
        _emit_adjudication(outcome)
        return outcome
    except Exception as schema_exc:
        log.debug(
            "atomic_write failed for %s (%s); falling back to path write", loser_id, schema_exc
        )

    try:
        from memory.engine.walker import path_for_id

        path_str = path_for_id(loser_id)
        if not path_str:
            outcome = {"persisted": False, "reason": "path_unresolved", **mutation}
            _emit_adjudication(outcome)
            return outcome
        _atomic_write_at_path(Path(path_str), doc)
        outcome = {"persisted": True, "fallback_write": True, **mutation}
        _emit_adjudication(outcome)
        return outcome
    except Exception as exc:
        log.warning("persist adjudication for %s failed: %s", loser_id, exc)
        outcome = {"persisted": False, "reason": str(exc), **mutation}
        _emit_adjudication(outcome)
        return outcome


def _load_memory_document(memory_id: str) -> dict[str, Any] | None:
    try:
        from memory.engine.walker import path_for_id

        path = path_for_id(memory_id)
        if not path:
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        log.debug("load memory %s failed: %s", memory_id, exc)
        return None


def _entity_fingerprints(doc: dict[str, Any]) -> set[tuple[str, str, str]]:
    """The (subject, facet, value) assertions a record makes, read from ``enrichment.entities``
    (the same parsed relations the graph hangs edges off). ``value`` is ``linked_name`` for a
    relationship facet else ``name`` (mirrors recall_verifier._record_relation_facets). Facet is
    kept in the key so 'operator name Sam' and 'operator city Sam' never corroborate each other.
    Empty for non-relational records. Normalized (lowercased/stripped) so counting is stable."""
    enrichment = doc.get("enrichment") if isinstance(doc.get("enrichment"), dict) else {}
    entities = enrichment.get("entities") if isinstance(enrichment, dict) else None
    if not isinstance(entities, list):
        return set()
    out: set[tuple[str, str, str]] = set()
    for e in entities:
        if not isinstance(e, dict) or not e.get("facet"):
            continue
        subject = str(e.get("subject") or "operator").strip().lower()
        facet = str(e.get("facet")).strip().lower()
        if facet == "relationship":
            value = e.get("linked_name") or e.get("object") or e.get("name")
        else:
            value = e.get("object") or e.get("name")
        value = str(value or "").strip().lower()
        if subject and facet and value:
            out.add((subject, facet, value))
    return out


def scan_fingerprint_counts(
    candidate_docs: list[dict[str, Any]],
    *,
    index_snapshot: dict[str, Any] | None = None,
    load_doc: Any | None = None,
) -> dict[tuple[str, str, str], int]:
    """Count, per (subject, facet, value) triple asserted by any candidate, how many records IN
    SCOPE assert that exact triple (self-inclusive). Scope = the candidates' lobes (coord[0]) so
    the file scan stays bounded — DeepSleep housekeeping is not latency-bound. Pure given
    ``index_snapshot`` + ``load_doc``; defaults read the live coord index + memory files.

    Exact-match only counts records that AGREE on a value, so a value's count can never be inflated
    by a disagreeing record; under-counting (unresolved aliases: "Noah" vs "Noah Lee") only lowers
    a count -> weaker decisiveness -> the softer demotion path, never a wrong tombstone."""
    wanted: set[tuple[str, str, str]] = set()
    lobes: set[int] = set()
    for d in candidate_docs or []:
        if not isinstance(d, dict):
            continue
        wanted |= _entity_fingerprints(d)
        coord = d.get("coord")
        if isinstance(coord, list) and coord:
            try:
                lobes.add(int(coord[0]))
            except (TypeError, ValueError):
                pass
    if not wanted:
        return {}

    snap = index_snapshot
    if snap is None:
        try:
            from memory.engine.walker import coord_index_snapshot

            snap = coord_index_snapshot()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("corroboration: coord_index_snapshot failed: %s", exc)
            snap = {}
    load = load_doc or _load_memory_document

    fp_counts: dict[tuple[str, str, str], int] = {fp: 0 for fp in wanted}
    for mid, entry in (snap or {}).items():
        coord = (entry or {}).get("coord") or []
        if lobes:
            if not (isinstance(coord, list) and coord):
                continue
            try:
                if int(coord[0]) not in lobes:
                    continue
            except (TypeError, ValueError):
                continue
        doc = load(mid)
        if not isinstance(doc, dict):
            continue
        for fp in _entity_fingerprints(doc):
            if fp in fp_counts:
                fp_counts[fp] += 1
    return fp_counts


def pairwise_corroboration(
    left: dict[str, Any],
    right: dict[str, Any],
    fp_counts: dict[tuple[str, str, str], int],
) -> dict[str, int]:
    """Axis-scoped corroboration for ONE conflicting pair: the two records disagree on some
    (subject, facet) axis, so each record's corroboration is measured ONLY on the axes the pair
    SHARES — how many records assert that record's value on a shared axis. This is the fix for
    cross-facet leakage: a record must not "win" a city-conflict on the strength of an unrelated,
    well-corroborated employer fact. If the pair shares no axis, both corroborations are 0 and the
    adjudicator falls back to confidence. Returns {left_id: n, right_id: n}. Pure."""
    fps_left = _entity_fingerprints(left)
    fps_right = _entity_fingerprints(right)
    axes_left = {(s, f) for (s, f, _v) in fps_left}
    axes_right = {(s, f) for (s, f, _v) in fps_right}
    shared = axes_left & axes_right
    left_id = str(left.get("id") or "left")
    right_id = str(right.get("id") or "right")

    def _corrob(fps: set[tuple[str, str, str]]) -> int:
        on_axis = [fp_counts.get(fp, 0) for fp in fps if (fp[0], fp[1]) in shared]
        return max(on_axis, default=0)

    return {left_id: _corrob(fps_left), right_id: _corrob(fps_right)}


def _normalize_title(text: Any) -> str:
    return " ".join(str(text or "").strip().lower().split())


def plan_conflict_enqueues(blocked_conflicts: Any) -> list[dict[str, Any]]:
    """Group recall-time quarantined conflict hits by normalized title into (canonical_ref,
    evidence_refs) pairs for enqueueing. The pivot is deterministic (sorted-first id) — it is only
    a GROUPING anchor; the actual winner/loser is decided later by the adjudicator on full docs, so
    the pivot choice never biases the outcome. Singleton titles are dropped (no pair, no conflict).
    Pure."""
    by_title: dict[str, list[str]] = {}
    for item in blocked_conflicts or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("memory_id") or item.get("id") or "").strip()
        if not mid:
            continue
        title = _normalize_title(item.get("title"))
        ids = by_title.setdefault(title, [])
        if mid not in ids:
            ids.append(mid)
    plans: list[dict[str, Any]] = []
    for title, ids in by_title.items():
        if len(ids) < 2:
            continue
        ordered = sorted(ids)
        plans.append({"canonical_ref": ordered[0], "evidence_refs": ordered[1:], "title": title})
    return plans


def enqueue_blocked_conflicts(blocked_conflicts: Any) -> dict[str, Any]:
    """Route recall-time quarantined conflicts (``RecallVerdict.blocked_conflicts``, produced by the
    FROZEN verifier) into the DeepSleep candidates queue for eventual reconciliation. The verifier
    only QUARANTINES; RESOLUTION is DeepSleep housekeeping (operator 2026-07-15). Best-effort — a
    queue failure never breaks the recall that surfaced the conflict."""
    plans = plan_conflict_enqueues(blocked_conflicts)
    queued = 0
    groups = 0
    for plan in plans:
        try:
            from memory.api import resolve_contradiction

            res = resolve_contradiction(plan["canonical_ref"], plan["evidence_refs"])
            queued += len(getattr(res, "queued_to", None) or [])
            groups += 1
        except Exception as exc:  # pragma: no cover - best-effort
            log.debug("enqueue_blocked_conflicts: resolve_contradiction failed: %s", exc)
    return {"groups": groups, "queued": queued, "plans": len(plans)}


def process_contradiction_candidates(
    *,
    max_groups: int = 10,
) -> dict[str, Any]:
    """Drain contradiction queue entries and adjudicate each group."""
    from memory.queues import take_deepsleep_candidates_where

    from core.memory_contradiction import adjudicate_contradiction_pair

    entries = take_deepsleep_candidates_where(
        lambda entry: parse_contradiction_reason(str(entry.get("reason") or "")) is not None
    )
    groups: dict[str, dict[str, Any]] = {}
    for entry in entries:
        parsed = parse_contradiction_reason(str(entry.get("reason") or ""))
        if parsed is None:
            continue
        key = parsed["reason"]
        bucket = groups.setdefault(
            key,
            {
                "canonical_id": parsed["canonical_id"],
                "evidence_ids": list(parsed["evidence_ids"]),
                "memory_ids": set(),
            },
        )
        mid = str(entry.get("memory_id") or "").strip()
        if mid:
            bucket["memory_ids"].add(mid)

    # Process at most max_groups groups. take_deepsleep_candidates_where already removed EVERY
    # contradiction entry from the queue, so any group we don't process this cycle must be
    # RE-ENQUEUED (drain-what-you-can, keep the rest) — otherwise overflow conflicts are silently
    # lost until a future recall happens to re-surface them.
    group_items = list(groups.items())
    cap = max(1, int(max_groups))
    ordered = [g for _reason, g in group_items[:cap]]
    overflow_reasons = {reason for reason, _g in group_items[cap:]}
    if overflow_reasons:
        from memory.queues import enqueue_deepsleep_candidate

        for entry in entries:
            if str(entry.get("reason") or "") in overflow_reasons:
                try:
                    enqueue_deepsleep_candidate(
                        str(entry.get("memory_id") or ""),
                        list(entry.get("coord") or []),
                        reason=str(entry.get("reason") or ""),
                    )
                except Exception as exc:  # pragma: no cover - best-effort
                    log.debug("re-enqueue overflow contradiction entry failed: %s", exc)

    # Pre-load every referenced doc ONCE so corroboration is counted over the whole candidate set in
    # a single scan (a challenger of one group can also corroborate the winner of another), and no
    # file is read twice.
    doc_cache: dict[str, dict[str, Any] | None] = {}

    def _doc(mid: str) -> dict[str, Any] | None:
        key = str(mid)
        if key not in doc_cache:
            doc_cache[key] = _load_memory_document(key)
        return doc_cache[key]

    candidate_docs: list[dict[str, Any]] = []
    for group in ordered:
        for mid in [group["canonical_id"], *group["evidence_ids"]]:
            d = _doc(str(mid))
            if d:
                candidate_docs.append(d)
    # ONE bounded scan for the whole candidate set; corroboration is then reduced PER PAIR on the
    # axis the pair actually disagrees on (cross-facet leakage guard).
    fp_counts = scan_fingerprint_counts(candidate_docs)

    adjudications: list[dict[str, Any]] = []
    processed = 0
    for group in ordered:
        canonical_id = str(group["canonical_id"])
        canonical_doc = _doc(canonical_id)
        if not canonical_doc:
            continue
        for evidence_id in group["evidence_ids"]:
            challenger = _doc(str(evidence_id))
            if not challenger:
                continue
            corroboration = pairwise_corroboration(canonical_doc, challenger, fp_counts)
            outcome = adjudicate_contradiction_pair(
                canonical_doc, challenger, corroboration=corroboration
            )
            row = {
                "canonical_id": canonical_id,
                "evidence_id": evidence_id,
                **outcome,
            }
            if not outcome.get("conflict"):
                # Subject-mismatch false positive from the title-only partitioner (the FROZEN
                # verifier is over-inclusive by design); adjudication clears it — nothing removed.
                row["persist"] = {"persisted": False, "reason": "not_a_conflict"}
            else:
                row["persist"] = persist_adjudication_outcome(row)
                if (row["persist"] or {}).get("resolution") == "tombstoned":
                    # Invalidate the cache for the just-removed record so it can never serve as a
                    # stale "ghost winner" anchor for a LATER group in this same run (its file is
                    # now gone; a later group must see it as absent and skip via the guards above).
                    doc_cache[str(outcome.get("loser_id") or "")] = None
            adjudications.append(row)
        processed += 1

    persisted = sum(1 for row in adjudications if (row.get("persist") or {}).get("persisted"))
    demoted = sum(
        1
        for row in adjudications
        if ((row.get("persist") or {}).get("demotion") or {}).get("demoted")
    )
    tombstoned = sum(
        1 for row in adjudications if (row.get("persist") or {}).get("resolution") == "tombstoned"
    )
    report = {
        "groups_processed": processed,
        "adjudications": adjudications,
        "persisted_count": persisted,
        "demoted_count": demoted,
        "tombstoned_count": tombstoned,
        "queued_entries": len(entries),
    }
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "process_contradiction_candidates",
            summary=(
                f"groups={processed} adjudicated={len(adjudications)} "
                f"persisted={persisted} tombstoned={tombstoned} demoted={demoted}"
            ),
            groups_processed=processed,
            adjudication_count=len(adjudications),
            persisted_count=persisted,
            tombstoned_count=tombstoned,
            demoted_count=demoted,
        )
    except Exception:
        pass
    return report
