"""
recall_lanes.py
Multi-lane retrieval execution.

Each lane scopes the corpus along a specific dimension and emits
`LaneHit` records for memories that match. Lanes are ADDITIVE — the
evidence_set merges hits across lanes; the arbitrator scores the
merged set against the recall plan's posture.

Lane policy:
    * Every lane is gated on `plan.lane_weights[lane] > 0`. A lane
      weight of 0 means the posture explicitly suppresses that lane
      (e.g. IDENTITY_RESOLUTION sets `tools=0.0` and `workflows=0.0`).
    * Each lane reads `coord_index_snapshot` (a dict[id -> entry]) and
      resolves memories via `load_meta(path)` (caller-supplied callable
      so this module stays decoupled from filesystem layout).
    * Each lane caps its emissions to `plan.limit_per_lane` so
      pathological lobes can't drown the evidence set.
    * Lane scores live in [0, 1]. They're per-lane signals; the
      arbitrator combines them with multi-lane bonuses + suppressions.

Lane catalog (matches `lane_weights` keys in `recall_posture.POSTURE_CATALOG`):
    identity, persona, knowledge, reasoning, situational, tools, skills,
    coding, temporal, workflows, project, dev, journal, vision,
    embedding, strength

Lobe → lane mapping (Dewey 12-lobe layout, post-2026-05-06 restructure):
    0  identity      → identity
    1  personas      → persona
    2  knowledge     → knowledge
    3  reasoning     → reasoning
    4  situational   → situational
    5  skills        → skills + tools (procedures vs. tools subdomain)
    6  journal       → journal
    7  vision        → vision
    8  coding        → coding
    9  temporal      → temporal
    10 workflows     → workflows + project (workflow templates vs.
                       project records distinction lives in subdomain)
    11 dev           → dev

The 'embedding' and 'strength' lanes are non-lobe-scoped — they
pull from the cross-lobe walker baseline + StrengthIndex respectively.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


log = logging.getLogger("omega.memory.recall_lanes")


@dataclass(frozen=True)
class LaneHit:
    memory_id: str
    lane: str
    score: float
    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)


# Lane name constants (exact names from operator spec)
LANE_HOT = "hot"
LANE_IDENTITY = "identity"
LANE_PERSONA = "persona"
LANE_KNOWLEDGE = "knowledge"
LANE_REASONING = "reasoning"
LANE_SITUATIONAL = "situational"
LANE_TOOLS = "tools"
LANE_SKILLS = "skills"
LANE_CODING = "coding"
LANE_TEMPORAL = "temporal"
LANE_WORKFLOWS = "workflows"
LANE_PROJECT = "project"
LANE_DEV = "dev"
LANE_JOURNAL = "journal"
LANE_VISION = "vision"
LANE_EMBEDDING = "embedding"
#: The perception lane. Shares the ``embedding`` posture weight because it
#: occupies that lane's slot in POSTURE_CATALOG — the mechanism is new, the
#: role in arbitration is not.
LANE_RETINA = LANE_EMBEDDING
LANE_STRENGTH = "strength"

ALL_LANE_NAMES = [
    LANE_HOT,
    LANE_IDENTITY,
    LANE_PERSONA,
    LANE_KNOWLEDGE,
    LANE_REASONING,
    LANE_SITUATIONAL,
    LANE_TOOLS,
    LANE_SKILLS,
    LANE_CODING,
    LANE_TEMPORAL,
    LANE_WORKFLOWS,
    LANE_PROJECT,
    LANE_DEV,
    LANE_JOURNAL,
    LANE_VISION,
    LANE_EMBEDDING,
    LANE_STRENGTH,
]


# Lane → (allowed lobes, optional subdomain filter) for the lobe-scoped
# lanes. None for subdomain means "any subdomain in the lobe".
_LANE_TO_LOBES: Dict[str, Tuple[List[int], Optional[List[int]]]] = {
    LANE_IDENTITY: ([0], None),
    LANE_PERSONA: ([1], None),
    LANE_KNOWLEDGE: ([2], None),
    LANE_REASONING: ([3], None),
    LANE_SITUATIONAL: ([4], None),
    LANE_TOOLS: ([5], [1]),  # subdomain 1 = tools
    LANE_SKILLS: ([5], None),  # whole skills lobe
    LANE_CODING: ([8], None),
    LANE_TEMPORAL: ([9], None),
    LANE_WORKFLOWS: ([10], None),
    LANE_PROJECT: ([10, 11], None),  # workflows OR dev project records
    LANE_DEV: ([11], None),
    # Lobes 6/7 are STRUCTURALLY non-recallable through the general engine
    # (dewey_map.NON_RECALLABLE_LOBES, operator 2026-07-18): journal is a
    # metacognitive state (DeepSleep reads the FS directly); vision recalls
    # through the dedicated visual pipeline (pending). These lanes return
    # empty until then — kept so lane names stay stable for consumers.
    LANE_JOURNAL: ([6], None),
    LANE_VISION: ([7], None),
}


def _entry_lobe(entry: Dict[str, Any]) -> Optional[int]:
    coord = entry.get("coord") or []
    if not coord:
        return None
    try:
        return int(coord[0])
    except (TypeError, ValueError):
        return None


def _entry_subdomain(entry: Dict[str, Any]) -> Optional[int]:
    coord = entry.get("coord") or []
    if len(coord) < 2:
        return None
    try:
        return int(coord[1])
    except (TypeError, ValueError):
        return None


def _safe_load(
    load_meta: Callable[[str], Optional[Dict[str, Any]]], path: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Defensive wrapper — load_meta may raise on missing files (the
    project has known stale-coord-index entries)."""
    if not path:
        return None
    try:
        return load_meta(path)
    except Exception as exc:
        log.debug("recall_lanes: load_meta(%r) failed: %s", path, exc)
        return None


def _plan_terms(plan: Any) -> Set[str]:
    """Union of lowercased plan terms used for scoring against text."""
    out: Set[str] = set()
    for attr in ("exact_terms", "expanded_terms", "entities", "task_tags"):
        for t in getattr(plan, attr, None) or []:
            if isinstance(t, str) and t.strip():
                out.add(t.lower().strip())
    return out


def _term_overlap_score(haystack: str, terms: Set[str]) -> float:
    """Return [0, 1] score = (matched terms / total terms).

    Matching is substring-based for robustness against punctuation and
    word boundaries. Empty inputs return 0.
    """
    if not haystack or not terms:
        return 0.0
    h = haystack.lower()
    matched = sum(1 for t in terms if t in h)
    if not terms:
        return 0.0
    return min(1.0, matched / max(1, len(terms)))


def _score_meta_for_lane(meta: Dict[str, Any], plan_terms: Set[str]) -> float:
    """Combine title + summary + tag matches into a single per-lane score.

    title matches weigh more than summary matches because titles are
    deliberate and short; long summaries dilute. Capped at 1.0.
    """
    if not meta:
        return 0.0
    title = meta.get("title") or ""
    summary = meta.get("summary") or ""
    tags = " ".join(t for t in (meta.get("tags") or []) if isinstance(t, str))
    text_blob = (meta.get("text") or "")[:600]  # cap body contribution

    title_score = _term_overlap_score(title, plan_terms)
    summary_score = _term_overlap_score(summary, plan_terms)
    tag_score = _term_overlap_score(tags, plan_terms)
    text_score = _term_overlap_score(text_blob, plan_terms)

    score = 0.45 * title_score + 0.25 * summary_score + 0.20 * tag_score + 0.10 * text_score
    return min(1.0, score)


def _run_lobe_lane(
    lane_name: str,
    plan: Any,
    coord_index_snapshot: Dict[str, Dict[str, Any]],
    load_meta: Callable[[str], Optional[Dict[str, Any]]],
    plan_terms: Set[str],
    truncation_receipts: List[Dict[str, Any]] | None = None,
) -> List[LaneHit]:
    """Generic lobe-scoped lane: select index entries whose coord
    matches the lane's allowed lobes (and optional subdomain filter),
    then score by term overlap."""
    if lane_name not in _LANE_TO_LOBES:
        return []
    allowed_lobes, allowed_subs = _LANE_TO_LOBES[lane_name]
    allowed_lobes_set = set(allowed_lobes)
    allowed_subs_set: Optional[Set[int]] = set(allowed_subs) if allowed_subs is not None else None

    limit = max(1, getattr(plan, "limit_per_lane", 5))
    min_strength = float(getattr(plan, "min_strength", 0.0) or 0.0)

    scored: List[Tuple[float, str, Dict[str, Any]]] = []
    for mid, entry in coord_index_snapshot.items():
        lobe = _entry_lobe(entry)
        if lobe is None or lobe not in allowed_lobes_set:
            continue
        if allowed_subs_set is not None:
            sub = _entry_subdomain(entry)
            if sub is None or sub not in allowed_subs_set:
                continue

        meta = _safe_load(load_meta, entry.get("path"))
        if not meta:
            continue
        if meta.get("flags", {}).get("private"):
            continue

        score = _score_meta_for_lane(meta, plan_terms)
        if score < min_strength:
            continue

        scored.append((score, mid, meta))

    # Top-N by score, stable by id for tie-break determinism.
    scored.sort(key=lambda x: (-x[0], x[1]))

    hits: List[LaneHit] = []
    for score, mid, meta in scored[:limit]:
        if score <= 0.0:
            continue
        title = meta.get("title") or ""
        hits.append(
            LaneHit(
                memory_id=mid,
                lane=lane_name,
                score=score,
                reason=f"{lane_name}: term-overlap on title/summary/tags ({score:.2f}) — {title[:60]!r}",
                metadata={
                    "coord": meta.get("coord"),
                    "title": meta.get("title"),
                    "type": meta.get("type"),
                },
            )
        )
    if truncation_receipts is not None:
        from memory.recall_truncation import build_truncation_receipt

        truncation_receipts.append(
            build_truncation_receipt(
                f"lane:{lane_name}:top_k",
                [mid for _score, mid, _meta in scored],
                [hit.memory_id for hit in hits],
                details={"limit": limit},
            )
        )
    return hits


def _run_temporal_lane(
    plan: Any,
    coord_index_snapshot: Dict[str, Dict[str, Any]],
    load_meta: Callable[[str], Optional[Dict[str, Any]]],
    plan_terms: Set[str],
    truncation_receipts: List[Dict[str, Any]] | None = None,
) -> List[LaneHit]:
    """Temporal lane: lobe 9 (temporal) memories, with recency boost
    when posture asks for `recent` or `recent_first` temporal_bias.

    Record timestamps carry observed canonical time. Recency keys on
    `provenance.write_ordinal` — the monotonic counter stamped at commit —
    because it preserves exact order when timestamps coincide. Legacy records
    without an ordinal sort last, with `ingested_at` as a chronological
    tie-break; post-day-zero every record carries an ordinal. Falls back to
    plain term-overlap when the
    posture has no recency bias.
    """
    posture = getattr(plan, "posture", None)
    bias = getattr(posture, "temporal_bias", None)

    base_hits = _run_lobe_lane(
        LANE_TEMPORAL,
        plan,
        coord_index_snapshot,
        load_meta,
        plan_terms,
        truncation_receipts=truncation_receipts,
    )

    # If posture doesn't bias for recency, return base hits as-is.
    if bias not in {"recent", "recent_first"}:
        return base_hits

    from memory.temporal_authority import authoritative_event_timestamp

    # Otherwise re-rank recent-first by the monotonic write-ordinal (the real
    # write order), keeping only those that already cleared the term-overlap
    # threshold. Legacy records without an ordinal get -1 so they sort last,
    # with their (hash) ingested_at as a stable-but-meaningless tie-break.
    decorated: List[Tuple[int, str, LaneHit]] = []
    for hit in base_hits:
        meta = _safe_load(load_meta, coord_index_snapshot.get(hit.memory_id, {}).get("path"))
        ordinal = -1
        ts = ""
        if meta:
            prov = meta.get("provenance") or {}
            if isinstance(prov, dict):
                raw = prov.get("write_ordinal")
                ordinal = int(raw) if isinstance(raw, (int, float)) else -1
                ts = authoritative_event_timestamp(meta) or ""
        decorated.append((ordinal, ts, hit))

    decorated.sort(key=lambda x: (x[0], x[1]), reverse=True)
    # Apply a light recency bonus to top entries' scores so the
    # arbitrator sees the temporal-bias preference reflected in score.
    boosted: List[LaneHit] = []
    for rank, (_, _, hit) in enumerate(decorated):
        bonus = max(0.0, 0.10 - 0.02 * rank)
        boosted_score = min(1.0, hit.score + bonus)
        boosted.append(
            LaneHit(
                memory_id=hit.memory_id,
                lane=hit.lane,
                score=boosted_score,
                reason=f"{hit.reason} +recency_bonus={bonus:.2f}",
                metadata=hit.metadata,
            )
        )
    return boosted


def _run_strength_lane(
    plan: Any,
    coord_index_snapshot: Dict[str, Dict[str, Any]],
    load_meta: Callable[[str], Optional[Dict[str, Any]]],
    plan_terms: Set[str],
    truncation_receipts: List[Dict[str, Any]] | None = None,
) -> List[LaneHit]:
    """Strength lane: surface memories whose StrengthIndex value is
    elevated. Requires the strength_index module to be importable; if
    not available (boot-safe), returns []. Honors `posture.strength_bias`.
    """
    posture = getattr(plan, "posture", None)
    bias = getattr(posture, "strength_bias", None)
    if bias is None:
        return []

    try:
        from memory.strength_index import ensure_strength_index

        si = ensure_strength_index()
    except Exception as exc:
        log.debug("recall_lanes: strength_index unavailable (%s)", exc)
        return []

    limit = max(1, getattr(plan, "limit_per_lane", 5))

    # Iterate the index; for each memory, get its strength score
    # (defaults to 0). Score is the strength value, scaled to [0, 1].
    scored: List[Tuple[float, str, Dict[str, Any]]] = []
    for mid in coord_index_snapshot:
        # StrengthIndex exposes get_strength(mid) -> float (returns 0.0
        # for an unknown mid). The prior code called the non-existent
        # si.get(mid); the resulting AttributeError was swallowed by a
        # bare `except`, so EVERY memory scored 0 and the entire
        # strength lane was a silent no-op for the life of the feature
        # (audit 2026-05-18). Narrow the except to value-coercion only
        # so a future structural API break surfaces instead of hiding.
        try:
            strength = float(si.get_strength(mid))
        except (TypeError, ValueError):
            strength = 0.0
        if strength <= 0.0:
            continue
        scored.append((min(1.0, strength), mid, coord_index_snapshot[mid]))

    scored.sort(key=lambda x: (-x[0], x[1]))

    hits: List[LaneHit] = []
    for strength, mid, entry in scored[:limit]:
        meta = _safe_load(load_meta, entry.get("path"))
        if not meta:
            continue
        if meta.get("flags", {}).get("private"):
            continue
        # Optionally reweight by term-overlap so high-strength but
        # off-topic memories don't dominate.
        overlap = _score_meta_for_lane(meta, plan_terms)
        # Convex combo: half strength, half on-topic-ness.
        combined = 0.5 * strength + 0.5 * overlap
        hits.append(
            LaneHit(
                memory_id=mid,
                lane=LANE_STRENGTH,
                score=combined,
                reason=f"strength: si={strength:.2f} overlap={overlap:.2f} combined={combined:.2f}",
                metadata={
                    "coord": meta.get("coord"),
                    "title": meta.get("title"),
                    "type": meta.get("type"),
                    "strength": strength,
                },
            )
        )
    if truncation_receipts is not None:
        from memory.recall_truncation import build_truncation_receipt

        truncation_receipts.append(
            build_truncation_receipt(
                "lane:strength:top_k",
                [mid for _strength, mid, _entry in scored],
                [hit.memory_id for hit in hits],
                details={"limit": limit},
            )
        )
    return hits


def _run_retina_lane(
    plan: Any,
    coord_index_snapshot: Dict[str, Dict[str, Any]],
    load_meta: Callable[[str], Optional[Dict[str, Any]]],
    plan_terms: Set[str],
    truncation_receipts: List[Dict[str, Any]] | None = None,
) -> List[LaneHit]:
    """Perception lane: the semantic retina proposes, and only proposes.

    This replaces the dense ``embedding`` lane. Where that one delegated to the
    walker and, through it, to a 768-d embedding service, this scans the
    generation's D=16384 BINARY codes by popcount — the same operation and the
    same kind of bits the DME already uses, with no dense vector stored anywhere
    (17 §1 as amended by 21 §2).

    Its score is Hamming similarity, and it is a LANE SCORE like any other: the
    arbitrator weighs it against the lobe lanes and the RecallVerifier still
    decides what may ground an answer. The retina cannot admit a record on its
    own, which is exactly the constraint ADR-MO-020 places on it.

    A declared empty plane or a query without signal returns no proposals.
    Unavailable artifacts or query encoding failures propagate with their cause;
    a failed perception pass cannot be reported as a completed empty lane.
    """
    query = getattr(plan, "original_query", "") or ""
    if not query:
        return []

    from memory import retina

    if not retina.is_loaded():
        return []

    limit = max(1, getattr(plan, "limit_per_lane", 5))
    retina_ids = retina.record_ids()
    hits = retina.window(query, limit)

    window: List[tuple] = []       # (id, text, base_score, distance, entry)
    for hit in hits:
        if hit.record_id not in coord_index_snapshot:
            # The retina saw a record the coord index no longer lists. Canonical
            # truth wins; perception does not resurrect a retired record.
            continue
        entry = coord_index_snapshot.get(hit.record_id) or {}
        text = _candidate_text(entry, hit.record_id, load_meta)
        window.append((hit.record_id, text, hit.similarity, hit.distance, entry))

    if not window:
        return []

    # ── R0: stage 2 of ADR-MO-021 ──
    # The bi-encoder mean-pools tokens, so it is near order-invariant and barely
    # moves for a single "not". R0 reorders INSIDE the window using polarity,
    # before/after state and argument order. It can only permute what stage 1
    # retrieved, so it adds no candidate and manufactures no evidence.
    order = {cid: i for i, (cid, *_rest) in enumerate(window)}
    reranked = _rerank_window(
        getattr(plan, "original_query", "") or "",
        [(cid, text, base) for cid, text, base, _d, _e in window],
    )
    if reranked is not None:
        order = {cid: i for i, (cid, _score) in enumerate(reranked)}
        scores = dict(reranked)
    else:
        scores = {cid: base for cid, _t, base, _d, _e in window}

    out: List[LaneHit] = []
    for cid, _text, base, distance, entry in sorted(window, key=lambda r: order.get(r[0], 1 << 30)):
        reason = f"retina: hamming={distance} of {retina.DIMENSION} bits"
        if reranked is not None:
            reason += " (R0 reordered)"
        out.append(
            LaneHit(
                memory_id=cid,
                lane=LANE_RETINA,
                score=min(1.0, max(0.0, float(scores.get(cid, base)))),
                reason=reason,
                metadata={
                    "coord": entry.get("coord"),
                    "title": entry.get("title") or "",
                    "type": entry.get("type"),
                    "hamming": distance,
                    "retina_similarity": round(float(base), 6),
                    "reranked": reranked is not None,
                },
            )
        )
    if truncation_receipts is not None:
        from memory.recall_truncation import build_truncation_receipt

        truncation_receipts.append(
            build_truncation_receipt(
                "lane:retina:top_k",
                retina_ids,
                [hit.memory_id for hit in out],
                details={"limit": limit},
            )
        )
    return out


def _candidate_text(entry: Dict[str, Any], memory_id: str, load_meta) -> str:
    """Text R0 reads. Title+summary from the index, falling back to the record."""
    parts = [str(entry.get(key) or "") for key in ("title", "summary")]
    text = " ".join(p for p in parts if p).strip()
    if text:
        return text
    try:
        meta = load_meta(memory_id) or {}
    except Exception:  # noqa: BLE001
        return ""
    return " ".join(
        str(meta.get(key) or "") for key in ("title", "summary", "text")
    ).strip()


_RERANKER = None
_RERANKER_FAILED = False


def _rerank_window(query: str, candidates: List[tuple]):
    """Apply R0, or None when it cannot run.

    A missing spaCy model is not an error worth failing a recall over — the
    window is already correct, R0 only sharpens the order inside it — so the
    first failure disables the stage for the process and the retina's Hamming
    order stands.
    """
    global _RERANKER, _RERANKER_FAILED
    if _RERANKER_FAILED or not query or not candidates:
        return None
    if _RERANKER is None:
        try:
            from memory.rerank import LinguisticReranker

            _RERANKER = LinguisticReranker()
        except Exception as exc:  # noqa: BLE001
            log.info("recall_lanes: R0 unavailable, retina order stands (%s)", exc)
            _RERANKER_FAILED = True
            return None
    try:
        return _RERANKER.rerank(query, candidates)
    except Exception as exc:  # noqa: BLE001
        log.debug("recall_lanes: R0 raised (%s)", exc)
        return None


def _run_hot_lane(
    plan: Any,
    coord_index_snapshot: Dict[str, Dict[str, Any]],
    load_meta: Callable[[str], Optional[Dict[str, Any]]],
    plan_terms: Set[str],
    truncation_receipts: List[Dict[str, Any]] | None = None,
) -> List[LaneHit]:
    """HOT lane: the live session's turns (memory.hot_buffer), scored
    against the query — the DME short arc surfacing through recall.

    Unlike every other lane this does NOT read the coord index: hot
    turns are pre-memory (they consolidate through ingest at session
    close), so hits carry their text INLINE in metadata. The just-asked
    question never matches itself (an ingress echo answering the query
    verbatim is not evidence). Boot-safe: no session → [].
    """
    try:
        from memory.hot_buffer import current_hot_buffer
    except Exception:  # pragma: no cover — import cycle guard
        return []
    buf = current_hot_buffer()
    if buf is None:
        return []
    query_text = str(getattr(plan, "query_text", "") or "")
    if not query_text and plan_terms:
        query_text = " ".join(sorted(plan_terms))
    if not query_text:
        return []
    from memory.hot_buffer import _tokens as _hot_tokens

    q_tokens = _hot_tokens(query_text)
    limit = int(getattr(plan, "limit_per_lane", 10) or 10)
    scored_turns = buf.score(query_text)
    hits: List[LaneHit] = []
    for turn, score in scored_turns:
        turn_tokens = _hot_tokens(turn.text)
        if turn_tokens and turn_tokens <= q_tokens:
            # the question itself (or a contentless restatement of it)
            # is not its own answer — no NEW content words, no evidence
            continue
        hits.append(
            LaneHit(
                memory_id=turn.turn_id,
                lane=LANE_HOT,
                score=min(1.0, float(score)),
                reason=f"hot-session overlap={score:.3f} seq={turn.seq}",
                metadata={
                    "text": turn.text,
                    "role": turn.role,
                    "session_id": turn.session_id,
                    "seq": turn.seq,
                },
            )
        )
        if len(hits) >= limit:
            break
    if truncation_receipts is not None:
        from memory.recall_truncation import build_truncation_receipt

        truncation_receipts.append(
            build_truncation_receipt(
                "lane:hot:top_k",
                [turn.turn_id for turn, _score in scored_turns],
                [hit.memory_id for hit in hits],
                details={"limit": limit},
            )
        )
    return hits


# The live session is ALWAYS in scope: postures that predate the hot
# lane (or simply don't mention it) get this default weight; a posture
# silences the lane only by explicitly declaring ``hot: 0.0``. This is
# a deliberate exception to the absent-means-suppressed rule — hot is
# the present moment, not a corpus lane.
HOT_DEFAULT_WEIGHT = 1.5


# Lane name → executor. The dispatcher walks the plan's lane_weights
# and only runs lanes whose weight > 0.
def _run_perception_lane(
    plan: Any,
    coord_index_snapshot: Dict[str, Dict[str, Any]],
    load_meta: Callable[[str], Optional[Dict[str, Any]]],
    plan_terms: Set[str],
    truncation_receipts: List[Dict[str, Any]] | None = None,
) -> List[LaneHit]:
    """Run the one semantic-perception seat.

    A declared empty plane or no-signal query yields no proposals. An unavailable
    perception substrate raises; its caller must preserve that failure.
    """
    return _run_retina_lane(
        plan,
        coord_index_snapshot,
        load_meta,
        plan_terms,
        truncation_receipts=truncation_receipts,
    )


_LANE_RUNNERS: Dict[str, Callable[..., List[LaneHit]]] = {
    LANE_HOT: _run_hot_lane,
    LANE_TEMPORAL: _run_temporal_lane,
    LANE_STRENGTH: _run_strength_lane,
    LANE_EMBEDDING: _run_perception_lane,
    # All other lanes use the generic lobe-scoped runner via a closure
    # registered at run time below.
}


def run_recall_lanes(
    plan: Any,
    coord_index_snapshot: Dict[str, Dict[str, Any]],
    load_meta: Callable[[str], Optional[Dict[str, Any]]],
    *,
    truncation_receipts: List[Dict[str, Any]] | None = None,
) -> List[LaneHit]:
    """Execute every lane whose weight is > 0 in the plan.

    Returns a flat list of LaneHit. The caller (typically
    `read_memory`) is responsible for merging into an EvidenceSet
    and arbitrating.
    """
    hits: List[LaneHit] = []
    plan_terms = _plan_terms(plan)
    weights: Dict[str, float] = dict(getattr(plan, "lane_weights", {}) or {})

    for lane_name in ALL_LANE_NAMES:
        if lane_name == LANE_HOT and lane_name not in weights:
            weight = HOT_DEFAULT_WEIGHT  # present-moment default-on
        else:
            weight = float(weights.get(lane_name, 0.0) or 0.0)
        if weight <= 0.0:
            continue

        runner = _LANE_RUNNERS.get(lane_name)
        if runner is None:
            # Generic lobe-scoped lane
            lane_hits = _run_lobe_lane(
                lane_name,
                plan,
                coord_index_snapshot,
                load_meta,
                plan_terms,
                truncation_receipts=truncation_receipts,
            )
        else:
            lane_hits = runner(
                plan,
                coord_index_snapshot,
                load_meta,
                plan_terms,
                truncation_receipts=truncation_receipts,
            )

        # Apply the lane's posture weight to each hit's score before
        # emitting. The arbitrator will further combine across lanes,
        # but the per-lane weight makes "tools=2.0" actually outweigh
        # "knowledge=0.5" in the merged evidence.
        for hit in lane_hits:
            weighted = min(1.0, hit.score * weight)
            hits.append(
                LaneHit(
                    memory_id=hit.memory_id,
                    lane=hit.lane,
                    score=weighted,
                    reason=f"{hit.reason} | lane_weight={weight:.2f}",
                    metadata=hit.metadata,
                )
            )

    return hits
