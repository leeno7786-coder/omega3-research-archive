"""Episode learning-state envelope — Phase 2 §P2-S13.

Compact, JSON-safe representation of what the substrate "knows" at the
moment a CognitiveEpisode opens — composed from existing memory /
mastery / StrengthIndex / recall surfaces, NOT a new memory subsystem.

The envelope rides on ``episode.learning_state`` so every organ
(Global, Meta, Auto, Omega, DeepSleep, response_hold outlet,
tick-state WAL archive) can reference one canonical "what I know,
how strong it is, and where the gaps are right now" snapshot — same
shape as the P2-S12 temporal envelope, same propagation discipline.

Design constraints (PACKET_P2_S13.md §C):
  - JSON-safe primitives only — no live StrengthIndex objects, no
    raw memory bodies, no embeddings, no prompts, no full
    knowledge_mastery.json dump, no model outputs.
  - Compact: bounded counts + top-N summaries (3-5 entries), not
    full lists. Receipts use even smaller derived summaries.
  - Failure-soft: composer never raises into the cognitive
    transition. A missing StrengthIndex / missing mastery file /
    failing recall read all yield ``source="unavailable"``
    sections so the schema slot stays stable.
  - No new memory subsystem: composer READS
    ``memory.symphony_strength.build_strength_index_snapshot``,
    ``guardian/state/knowledge_mastery.json``, and brain-turn
    recall receipts. It does not write.
  - No pressure contribution in this slice: the envelope is
    EVIDENCE for Global routing decisions; turning learning_debt
    into a pressure source belongs to a later operator-directed
    slice with bounded refractory + matrix-v2 Axis 9 probes.

Public surface:
  - ``build_episode_learning_state(...)`` -> dict
  - ``compact_learning_state_summary(envelope)`` -> small subset
  - ``default_learning_state_provider()`` -> zero-arg callable for
    CueDesk constructor injection
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from guardian.terminal_feedback import emit_data, emit_phase

log = logging.getLogger("omega.pressure_loop.symphony_learning_state")


# Canonical field set the envelope carries. Each maps to a compact
# subsection; schema enum + WAL serializer + audit §2 all agree on
# this exact list.
LEARNING_STATE_FIELDS: tuple[str, ...] = (
    "captured_at_iso",  # ISO 8601 UTC timestamp at compose time
    "source",  # "live" / "partial" / "unavailable"
    "strength_index",  # compact dict — aggregate + top hot count
    "mastery",  # compact dict — domain summaries + freshness
    "recall_confidence",  # compact dict — recent recall status + avg
    "learning_debt_flags",  # bool flags — empty / stale / low-confidence
)


_TOP_DOMAIN_LIMIT: int = 3  # cap top-strong / top-weak slice
_TOP_HOT_LIMIT: int = 5  # cap top-hot memory_ids in summary

# Stale-mastery threshold: a mastery section whose `oldest_updated_at`
# is older than this is "stale" per the packet's freshness anchor
# (Required Behavior #1 — "mastery freshness"). 24 hours is the
# operator-decided default (2026-05-24 P2-S13 cure cycle): the system
# operates on tick-boundary granularity with per-epoch DeepSleep
# mastery consolidation, so day-old mastery surveys reflect a
# substrate that has missed multiple cognitive cycles. The flag's
# semantics ("the data exists and is chronologically old") stay
# stable; the threshold is operator-tunable as the system's learning
# cadence evolves.
#
# Important: emptiness (`domains_count == 0`) is NOT "stale" — that is
# absence, a different signal. The flag name and the derivation must
# agree on meaning; conflating the two was the P2-S13 cure cycle's
# semantic bug.
_STALE_MASTERY_SECONDS: float = 24 * 60 * 60  # 86_400.0


def _empty_section(source: str = "unavailable") -> Dict[str, Any]:
    return {"source": source}


def _empty_envelope(source: str = "unavailable") -> Dict[str, Any]:
    return {
        "captured_at_iso": "",
        "source": str(source or "unavailable"),
        "strength_index": _empty_section(source),
        "mastery": _empty_section(source),
        "recall_confidence": _empty_section(source),
        "learning_debt_flags": {
            "empty_substrate": False,
            "stale_mastery": False,
            "low_confidence_recall": False,
        },
    }


def build_episode_learning_state(
    *,
    strength_index_snapshot: Optional[Dict[str, Any]] = None,
    mastery_path: Optional[Path] = None,
    recall_receipts: Optional[Sequence[Dict[str, Any]]] = None,
    now_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """Compose one compact learning-state envelope from existing
    substrates.

    Args:
      strength_index_snapshot: optional pre-built snapshot dict from
        ``memory.symphony_strength.build_strength_index_snapshot``. If
        None, the composer builds it itself; tests can inject a
        deterministic fixture.
      mastery_path: optional path to the ``knowledge_mastery.json``
        file. If None, defaults to ``guardian/state/knowledge_mastery.json``;
        tests inject temp paths.
      recall_receipts: optional sequence of brain-turn /
        memory-operation receipts. Each dict should carry at minimum
        ``status`` and (optionally) ``confidence`` + ``selected_refs``.
        None or empty list yields an unavailable recall section.
      now_iso: optional ISO timestamp override (test determinism).

    Returns a JSON-safe dict whose keys match ``LEARNING_STATE_FIELDS``.
    NEVER raises — any failure mode degrades the affected subsection
    to ``source="unavailable"`` and continues.
    """
    envelope = _empty_envelope(source="unavailable")
    envelope["captured_at_iso"] = str(now_iso) if now_iso is not None else _now_iso()

    section_sources: List[str] = []

    # ---- StrengthIndex subsection ----
    si_section = _compose_strength_index_section(strength_index_snapshot)
    envelope["strength_index"] = si_section
    section_sources.append(str(si_section.get("source") or "unavailable"))

    # ---- Mastery subsection ----
    mastery_section = _compose_mastery_section(mastery_path)
    envelope["mastery"] = mastery_section
    section_sources.append(str(mastery_section.get("source") or "unavailable"))

    # ---- Recall confidence subsection ----
    recall_section = _compose_recall_section(recall_receipts)
    envelope["recall_confidence"] = recall_section
    section_sources.append(str(recall_section.get("source") or "unavailable"))

    # ---- Learning debt flags (derived from the three above) ----
    envelope["learning_debt_flags"] = _derive_debt_flags(
        si_section,
        mastery_section,
        recall_section,
    )

    # Envelope-level source: live if all three are live, partial if
    # any-but-not-all are live, unavailable if none are live.
    live_count = sum(1 for s in section_sources if s == "live")
    if live_count == len(section_sources):
        envelope["source"] = "live"
    elif live_count > 0:
        envelope["source"] = "partial"
    else:
        envelope["source"] = "unavailable"

    try:
        flags = envelope.get("learning_debt_flags") or {}
        if not isinstance(flags, dict):
            flags = {}
        mastery = envelope.get("mastery") or {}
        top_weak = []
        if isinstance(mastery, dict):
            raw_weak = mastery.get("top_weak") or []
            if isinstance(raw_weak, list):
                top_weak = [str(x) for x in raw_weak[:3]]
        active_flags = [k for k, v in flags.items() if v]
        try:
            emit_phase(
                "symphony", "learning_state", status="enter", summary=f"source={envelope['source']}"
            )
        except Exception:
            pass
        emit_phase(
            "symphony",
            "learning_state",
            status="ok",
            summary=(
                f"source={envelope['source']} "
                f"debt={','.join(active_flags) or 'none'} "
                f"weak={','.join(top_weak) or 'none'}"
            )[:200],
            envelope_source=envelope["source"],
            learning_debt_flags=active_flags,
            top_weak=top_weak,
        )
        emit_data(
            "pressure",
            "learning_state",
            envelope_source=envelope["source"],
            learning_debt_flags=active_flags,
            top_weak=top_weak,
            ok=True,
        )
    except Exception:
        pass

    return envelope


def compact_learning_state_summary(envelope: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce a full envelope to the small subset receipts carry.

    Three fields — captured_at_iso, source, learning_debt_flags —
    so Global / Omega / DeepSleep receipts stay JSON-compact under
    P2-S5 archive rules while still naming "this is the learning
    moment this receipt belongs to" + any explicit debt the
    operator path needs to react to.
    """
    if not isinstance(envelope, dict) or not envelope:
        return {}
    flags = envelope.get("learning_debt_flags") or {}
    if not isinstance(flags, dict):
        flags = {}
    return {
        "captured_at_iso": str(envelope.get("captured_at_iso") or ""),
        "source": str(envelope.get("source") or "unavailable"),
        "learning_debt_flags": {
            "empty_substrate": bool(flags.get("empty_substrate", False)),
            "stale_mastery": bool(flags.get("stale_mastery", False)),
            "low_confidence_recall": bool(flags.get("low_confidence_recall", False)),
        },
    }


def default_learning_state_provider() -> Callable[[], Dict[str, Any]]:
    """Return a zero-arg callable that builds a fresh envelope per
    call. Convenience for CueDesk-style injection."""

    def _provider() -> Dict[str, Any]:
        return build_episode_learning_state()

    return _provider


def fold_recall_into_envelope(
    envelope: Optional[Dict[str, Any]],
    receipts: Any,
) -> Dict[str, Any]:
    """Fold one or more recall receipts into an existing learning-state
    envelope's ``recall_confidence`` section. Returns a NEW envelope.

    Packet RB#5: "Brain turn memory-operation receipts **and** any
    Global recall step must fold into the envelope as confidence/
    status summaries. Missing recall must become explicit
    ``recall_unavailable`` / ``low_confidence_recall`` evidence; do
    not silently pretend context was available."

    ``receipts`` accepts:
      - None — preserves the envelope's existing recall section
        unchanged (no overwrite without intent)
      - dict — single receipt (compatibility with prior single-context
        callers; common case is the Global memory_context)
      - list of dicts — multiple receipts aggregated into one
        recall_confidence summary (brain_turn receipts + Global)

    Each receipt is normalized to ``{status, selected_refs}`` shape.
    Aggregation across receipts:
      - source: "live" iff at least one receipt has live status
      - receipts_count: number of LIVE receipts (skipped/unavailable
        receipts don't claim recall happened)
      - most_recent_status: status of the LAST receipt in the input
        order (Global memory_context is typically passed last,
        making this the "freshest" recall step)
      - avg_confidence: mean of ALL positive ``selected_refs[].score``
        across all live receipts; 0.0 when no scores available
        (brain_turn receipts carry memory_ids without confidence)
      - selected_refs_total: sum of selected_refs lengths across all
        live receipts

    Behavior:
      - If ``envelope`` is empty/None, returns a fresh envelope with
        only the recall section populated.
      - If no receipts present (None / empty list), envelope's
        existing recall section is preserved.
      - If receipts present but NONE live (all skipped/unavailable),
        recall section becomes explicit ``source="unavailable"`` —
        packet contract: missing recall is explicit, not pretend.
      - ``learning_debt_flags`` are re-derived from the updated
        envelope so ``low_confidence_recall`` reflects the just-
        folded receipts.

    Never mutates the input.
    """
    base: Dict[str, Any]
    if isinstance(envelope, dict) and envelope:
        base = {
            "captured_at_iso": str(envelope.get("captured_at_iso") or ""),
            "source": str(envelope.get("source") or "unavailable"),
            "strength_index": dict(envelope.get("strength_index") or {}),
            "mastery": dict(envelope.get("mastery") or {}),
            "recall_confidence": dict(envelope.get("recall_confidence") or {}),
            "learning_debt_flags": dict(envelope.get("learning_debt_flags") or {}),
        }
    else:
        base = _empty_envelope(source="unavailable")
        base["captured_at_iso"] = _now_iso()

    # Normalize input to a list of receipts
    receipts_list: List[Dict[str, Any]]
    if receipts is None:
        return base  # preserve existing recall section
    if isinstance(receipts, dict):
        receipts_list = [receipts]
    elif isinstance(receipts, list):
        receipts_list = [r for r in receipts if isinstance(r, dict)]
    else:
        receipts_list = []

    if not receipts_list:
        return base

    # Aggregate across all receipts
    live_receipt_count = 0
    all_scores: List[float] = []
    total_refs = 0
    last_status = ""
    saw_any_status = False
    for r in receipts_list:
        status = str(r.get("status") or "").strip()
        if status:
            saw_any_status = True
            last_status = status  # preserved across all receipts in order
        # Live = anything other than skipped / unavailable / empty.
        # The contract permits "used", "ok", "completed", and other
        # future-tolerant status strings to count as live.
        if status in ("", "skipped", "unavailable"):
            continue
        live_receipt_count += 1
        refs = r.get("selected_refs") or []
        if not isinstance(refs, list):
            refs = []
        total_refs += len(refs)
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            score = float(ref.get("score") or 0.0)
            if score > 0:
                all_scores.append(score)

    if live_receipt_count == 0:
        # Packet RB#5: explicit unavailable, not silent-success
        if saw_any_status:
            base["recall_confidence"] = _empty_section("unavailable")
        # else: no informative receipts at all — preserve prior section
    else:
        avg_conf = (sum(all_scores) / len(all_scores)) if all_scores else 0.0
        base["recall_confidence"] = {
            "source": "live",
            "receipts_count": live_receipt_count,
            "most_recent_status": last_status,
            "avg_confidence": avg_conf,
            "selected_refs_total": total_refs,
        }

    # Re-derive debt flags so low_confidence_recall reflects the
    # just-folded receipts. Re-derive empty_substrate / stale_mastery
    # too so the flag set stays consistent with the (unchanged)
    # strength_index + mastery sections.
    base["learning_debt_flags"] = _derive_debt_flags(
        base.get("strength_index") or {},
        base.get("mastery") or {},
        base.get("recall_confidence") or {},
    )

    # Re-compute top-level source — partial vs live vs unavailable —
    # so consumers can branch on it without inspecting each subsection.
    sub_sources = [
        str((base.get("strength_index") or {}).get("source") or "unavailable"),
        str((base.get("mastery") or {}).get("source") or "unavailable"),
        str((base.get("recall_confidence") or {}).get("source") or "unavailable"),
    ]
    live_count = sum(1 for s in sub_sources if s == "live")
    if live_count == len(sub_sources):
        base["source"] = "live"
    elif live_count > 0:
        base["source"] = "partial"
    else:
        base["source"] = "unavailable"

    return base


def extract_recall_receipt_from_brain_turn(
    brain_turn: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Convert a brain_turn dict into a recall-receipt-shaped dict
    suitable for ``fold_recall_into_envelope``.

    Packet RB#5: brain_turn memory-operation receipts must fold. A
    brain_turn's ``memory_operation.should_recall`` plus its
    ``store_receipt.memory_ids`` together name "BrainLoop actually
    recalled these memories." That is a status-only receipt — the
    brain_turn schema doesn't carry per-memory confidence scores
    (those live downstream in Global's memory_context).

    Returns ``None`` when the brain_turn did NOT perform recall
    (should_recall False, or store_receipt empty). The fold call site
    should drop None receipts; an empty fold input is preserved as
    the envelope's prior recall section.
    """
    if not isinstance(brain_turn, dict):
        return None
    mo = brain_turn.get("memory_operation") or {}
    sr = brain_turn.get("store_receipt") or {}
    if not isinstance(mo, dict) or not isinstance(sr, dict):
        return None
    should_recall = bool(mo.get("should_recall"))
    memory_ids = sr.get("memory_ids") or []
    if not should_recall:
        return None
    if not isinstance(memory_ids, list) or not memory_ids:
        # should_recall fired but nothing came back — that is recall-
        # attempted-but-empty, surface as unavailable (explicit), not
        # silently dropped.
        return {"status": "unavailable", "selected_refs": []}
    return {
        "status": "used",
        "selected_refs": [{"memory_id": str(mid), "score": 0.0} for mid in memory_ids],
    }


# ----------------------------------------------------------------------
# Subsection composers (each failure-soft, each compact)
# ----------------------------------------------------------------------


def _compose_strength_index_section(
    snapshot: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compose the compact StrengthIndex subsection.

    Pulls from ``memory.symphony_strength.build_strength_index_snapshot``
    if no snapshot is injected. The full snapshot can carry up to 20
    top_hot entries; this section bounds to TOP_HOT_LIMIT memory_ids
    + the aggregate scalar + total count. No raw memory bodies.
    """
    if snapshot is None:
        try:
            from memory.symphony_strength import build_strength_index_snapshot

            snapshot = build_strength_index_snapshot()
        except Exception as exc:  # noqa: BLE001 - failure-soft
            log.debug(
                "symphony_learning_state: strength_index build failed (%s); section degraded",
                exc,
            )
            return _empty_section("unavailable")
    if not isinstance(snapshot, dict):
        return _empty_section("unavailable")

    source = str(snapshot.get("source") or "unavailable")
    aggregate_strength = float(snapshot.get("aggregate_strength") or 0.0)
    top_hot_raw = snapshot.get("top_hot") or []
    if not isinstance(top_hot_raw, list):
        top_hot_raw = []

    # Compact each entry to just the id + strength — drop touch counts
    # and any future fat fields that might appear on the snapshot
    # shape. Cap at TOP_HOT_LIMIT.
    top_hot_compact: List[Dict[str, Any]] = []
    for entry in top_hot_raw[:_TOP_HOT_LIMIT]:
        if not isinstance(entry, dict):
            continue
        top_hot_compact.append(
            {
                "memory_id": str(entry.get("memory_id") or ""),
                "strength": float(entry.get("strength") or 0.0),
            }
        )

    return {
        "source": source,
        "aggregate_strength": aggregate_strength,
        "top_hot_count": len(top_hot_raw),
        "top_hot": top_hot_compact,
    }


def _compose_mastery_section(
    mastery_path: Optional[Path],
) -> Dict[str, Any]:
    """Compose the compact mastery subsection.

    Reads ``guardian/state/knowledge_mastery.json`` (or an injected
    path). The full file may carry dozens of domains; this section
    bounds to TOP_DOMAIN_LIMIT strong + weak + total count +
    oldest-update timestamp (freshness anchor). No per-domain raw
    counts beyond the top slices.
    """
    path = mastery_path
    if path is None:
        try:
            from core.paths import GUARDIAN_STATE_ROOT

            path = GUARDIAN_STATE_ROOT / "knowledge_mastery.json"
        except Exception as exc:  # noqa: BLE001 - failure-soft
            log.debug(
                "symphony_learning_state: mastery path resolve failed (%s); section degraded",
                exc,
            )
            return _empty_section("unavailable")
    if not isinstance(path, Path):
        try:
            path = Path(str(path))
        except Exception:
            return _empty_section("unavailable")
    if not path.exists():
        return _empty_section("unavailable")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:  # noqa: BLE001 - failure-soft
        log.debug(
            "symphony_learning_state: mastery file read failed (%s); section degraded",
            exc,
        )
        return _empty_section("unavailable")
    if not isinstance(raw, dict):
        return _empty_section("unavailable")

    # Each entry: {mastery, count, avg_confidence, avg_write_strength, updated_at}
    entries: List[Dict[str, Any]] = []
    for domain, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        try:
            mastery_val = float(rec.get("mastery") or 0.0)
            count_val = int(rec.get("count") or 0)
        except (TypeError, ValueError):
            continue
        entries.append(
            {
                "domain": str(domain),
                "mastery": mastery_val,
                "count": count_val,
                "updated_at": str(rec.get("updated_at") or ""),
            }
        )
    if not entries:
        return {
            "source": "live",
            "domains_count": 0,
            "top_strong": [],
            "top_weak": [],
            "oldest_updated_at": "",
        }

    entries_sorted = sorted(entries, key=lambda e: e["mastery"], reverse=True)
    top_strong = entries_sorted[:_TOP_DOMAIN_LIMIT]
    top_weak = list(reversed(entries_sorted[-_TOP_DOMAIN_LIMIT:]))

    oldest_updated_at = ""
    try:
        with_ts = [e for e in entries if e["updated_at"]]
        if with_ts:
            oldest_updated_at = min(with_ts, key=lambda e: e["updated_at"])["updated_at"]
    except Exception:
        oldest_updated_at = ""

    return {
        "source": "live",
        "domains_count": len(entries),
        "top_strong": [_compact_mastery_entry(e) for e in top_strong],
        "top_weak": [_compact_mastery_entry(e) for e in top_weak],
        "oldest_updated_at": oldest_updated_at,
    }


def _compact_mastery_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "domain": str(entry.get("domain") or "")[:80],
        "mastery": float(entry.get("mastery") or 0.0),
        "count": int(entry.get("count") or 0),
    }


def _compose_recall_section(
    receipts: Optional[Sequence[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Compose the compact recall-confidence subsection from a list
    of brain-turn / memory-operation receipts.

    Each receipt may carry ``status``, ``confidence``, and
    ``selected_refs`` (list of ref dicts with optional ``score``).
    Returns aggregate counts + an average confidence + the most-
    recent status. No raw recall text bodies.
    """
    if not receipts:
        return _empty_section("unavailable")
    if not isinstance(receipts, (list, tuple)):
        return _empty_section("unavailable")

    statuses: List[str] = []
    confidences: List[float] = []
    refs_count_total = 0
    for r in receipts:
        if not isinstance(r, dict):
            continue
        status = str(r.get("status") or "").strip()
        if status:
            statuses.append(status)
        raw_conf = r.get("confidence")
        if isinstance(raw_conf, (int, float)):
            confidences.append(float(raw_conf))
        else:
            # Some shapes use selected_refs[].score as a proxy
            refs = r.get("selected_refs") or []
            if isinstance(refs, list):
                refs_count_total += len(refs)
                scores = [float(ref.get("score") or 0.0) for ref in refs if isinstance(ref, dict)]
                if scores:
                    confidences.append(sum(scores) / len(scores))
        # Also accumulate refs_count for shapes that don't expose
        # explicit confidence
        refs = r.get("selected_refs")
        if isinstance(refs, list) and not (raw_conf is None and refs_count_total > 0):
            refs_count_total += len(refs)

    if not statuses and not confidences:
        return _empty_section("unavailable")

    return {
        "source": "live",
        "receipts_count": len(receipts),
        "most_recent_status": statuses[-1] if statuses else "",
        "avg_confidence": (sum(confidences) / len(confidences) if confidences else 0.0),
        "selected_refs_total": refs_count_total,
    }


def _derive_debt_flags(
    si_section: Dict[str, Any],
    mastery_section: Dict[str, Any],
    recall_section: Dict[str, Any],
) -> Dict[str, bool]:
    """Derive the three explicit learning-debt flags from the
    composed subsections. Pure function over already-compacted
    inputs."""
    empty_substrate = False
    if si_section.get("source") == "live":
        agg = float(si_section.get("aggregate_strength") or 0.0)
        empty_substrate = agg <= 0.0
    elif si_section.get("source") == "unavailable":
        # Substrate isn't unwired — that's a different signal; treat
        # as "we don't know" not "empty". Keep False.
        empty_substrate = False

    stale_mastery = False
    if mastery_section.get("source") == "live":
        # Packet RB#1: "stale" means "data exists and is chronologically
        # old" — measured against ``oldest_updated_at``, not domain
        # count. An empty mastery section is absence, not staleness;
        # it surfaces via the section's own ``source`` and the
        # ``domains_count == 0`` summary, not via this flag.
        oldest_iso = str(mastery_section.get("oldest_updated_at") or "")
        if oldest_iso:
            age_seconds = _seconds_since_iso(oldest_iso)
            if age_seconds is not None and age_seconds > _STALE_MASTERY_SECONDS:
                stale_mastery = True

    # Packet RB#5: "Missing recall must become explicit
    # `recall_unavailable` / `low_confidence_recall` evidence; do not
    # silently pretend context was available." The flag must fire on
    # BOTH (a) unavailable recall AND (b) live-but-low-confidence
    # recall. A consumer that only checks this flag must never
    # silently pretend recall was high-quality when it was missing
    # or weak. P2-S13 cure cycle: the flag previously fired only on
    # live + low, which left an unavailable recall section invisible
    # to flag-only consumers.
    low_confidence_recall = False
    recall_source = str(recall_section.get("source") or "").strip()
    if recall_source == "unavailable":
        low_confidence_recall = True
    elif recall_source == "live":
        avg = float(recall_section.get("avg_confidence") or 0.0)
        low_confidence_recall = avg < 0.3

    return {
        "empty_substrate": bool(empty_substrate),
        "stale_mastery": bool(stale_mastery),
        "low_confidence_recall": bool(low_confidence_recall),
    }


def _now_iso() -> str:
    # Phase 2 §P2-S20: route through the single temporal authority so
    # learning-state freshness anchors share the reference frame used
    # by episode / pack / archive / WAL surfaces.
    from core.symphony_time import now_iso

    return now_iso()


def _seconds_since_iso(iso_str: str) -> Optional[float]:
    """Parse an ISO 8601 timestamp and return seconds elapsed since
    it, in UTC. Returns ``None`` on parse failure so callers fall
    closed — an unparseable mastery freshness anchor is "we don't
    know" not "definitely stale" or "definitely fresh".

    Accepts both ``...+00:00`` and ``...Z`` zulu suffixes; converts
    naive strings to UTC by assumption (mastery timestamps are
    written by the local mastery_tracker which always emits UTC).

    Phase 2 §P2-S20: "now" derives from the temporal authority's
    `now_iso()` so fake-time tests can pin learning-debt elapsed-
    seconds calculations to the same clock as the surfaces that
    wrote the anchor.
    """
    if not iso_str:
        return None
    candidate = iso_str.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(candidate)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    try:
        from core.symphony_time import now_iso as _now_iso_authority

        now_str = _now_iso_authority()
        now_ts = datetime.fromisoformat(
            now_str[:-1] + "+00:00" if now_str.endswith("Z") else now_str
        )
        if now_ts.tzinfo is None:
            now_ts = now_ts.replace(tzinfo=timezone.utc)
        elapsed = (now_ts - ts).total_seconds()
    except Exception:  # pragma: no cover - guard against odd datetimes
        return None
    return elapsed if elapsed >= 0 else 0.0


__all__ = [
    "LEARNING_STATE_FIELDS",
    "build_episode_learning_state",
    "compact_learning_state_summary",
    "default_learning_state_provider",
    "fold_recall_into_envelope",
    "extract_recall_receipt_from_brain_turn",
]
