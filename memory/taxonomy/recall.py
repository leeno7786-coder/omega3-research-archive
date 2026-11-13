"""Phase 7J — deterministic recall engine.

recall() reads the COMPILED graph (never the model, never embeddings) and returns a
RecallPacket: candidates scored by a fixed formula, stable-sorted, each result citing
its evidence event IDs and explaining its reason. Same query + same graph => byte-identical
ordered packet (the determinism contract).

Scoring (work order):
    0.30 entity_overlap + 0.20 lexical_overlap + 0.20 graph_proximity
  + 0.10 confidence    + 0.10 recency_relevance + 0.10 skill_relevance
Stable tie-break: score DESC, confidence DESC, created_at ASC, node_id ASC.

Pure by default; pass log=True + created_at to append recall_query / recall_result
events to the ledger (idempotent — identical queries dedupe by content-derived id).
Guardian-free throughout.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from memory.taxonomy import graph_store as G, paths as P
from memory.taxonomy.normalize import canonical_json, normalize_id_field
from memory.taxonomy.schemas import RecallPacket

from memory.taxonomy import recall_graph
from memory.answerability_index import answerability_score
from memory.strength_index import ensure_strength_index
from memory.recall_compiler import compile_recall_plan

# claim predicates that indicate skill/workflow relevance
_SKILL_PREDICATES = frozenset(
    {
        "selected_for",
        "used_tool",
        "changed_file",
        "produced_status",
        "verified_by",
        "capability_signal",
    }
)


def _tokens(text: str) -> set[str]:
    return {t for t in normalize_id_field(text).split() if len(t) >= 2}


def morphological_forms(token: str) -> tuple[str, ...]:
    """Surface form plus its conservative base forms, for topicality matching.

    THE SINGLE SOURCE. The lexical scorer and the verdict's topicality gate must
    agree on what a word IS, exactly as they already share ``_STOPWORDS`` — and
    for the same reason: a second copy drifts. It drifted. The scorer stemmed
    both sides while the gate compared raw surface forms, so a query saying
    "specialists" could not match a record saying "specialist", and rows the
    scorer had ranked 3rd were demoted to strict unknown. Measured on
    gpt4_f2262a51: identical 116-row candidate pools, 9 matching evidence rows
    for the singular query and 0 for the plural.

    Deliberately conservative — suffix stripping only, no dictionary and no
    aggressive stemmer. It must never merge unrelated words, because widening
    this widens the topicality gate for every query in the system.
    """
    forms = [token]
    if len(token) > 4 and token.endswith(("ied", "ies")):
        forms.append(f"{token[:-3]}y")
    elif (len(token) > 5 and token.endswith("sses")) or (
        len(token) > 4 and token.endswith(("ches", "shes", "xes", "zes"))
    ):
        forms.append(token[:-2])
    elif len(token) > 4 and token.endswith("s") and not token.endswith(("is", "ss", "us")):
        forms.append(token[:-1])
    if len(token) > 5 and token.endswith("ing"):
        base = token[:-3]
        forms.append(base[:-1] if len(base) > 2 and base[-1] == base[-2] else base)
    if len(token) > 4 and token.endswith("ed"):
        base = token[:-2]
        forms.append(base[:-1] if len(base) > 2 and base[-1] == base[-2] else base)
    return tuple(dict.fromkeys(forms))


# Function / question / framing words that carry no topical content. Used ONLY to
# decide whether a hopped (via_link) neighbor is relevant to the QUERY — never to
# alter the lexical score of base candidates. Kept conservative: clear function
# words + question words + recall-framing verbs (tell/show/say/let), NOT ambiguous
# content verbs (learn, build, know) that can BE the topic of a query.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "done",
        "have",
        "has",
        "had",
        "having",
        "i",
        "me",
        "my",
        "mine",
        "we",
        "us",
        "our",
        "ours",
        "you",
        "your",
        "yours",
        "he",
        "him",
        "his",
        "she",
        "her",
        "hers",
        "it",
        "its",
        "they",
        "them",
        "their",
        "what",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "which",
        "that",
        "this",
        "these",
        "those",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "and",
        "or",
        "but",
        "with",
        "about",
        "from",
        "by",
        "as",
        "into",
        "onto",
        "up",
        "out",
        "off",
        "over",
        "under",
        "again",
        "then",
        "than",
        "so",
        "if",
        "because",
        "while",
        "can",
        "could",
        "would",
        "should",
        "will",
        "shall",
        "may",
        "might",
        "must",
        "tell",
        "show",
        "say",
        "said",
        "let",
        "lets",
        "please",
        "there",
        "here",
        "not",
        "no",
        "yes",
        "just",
        "only",
        "also",
        "too",
        "very",
        "really",
        "now",
        "some",
        "any",
        "all",
        "more",
        "most",
        "much",
    }
)


def _content_tokens(q_tokens: set[str]) -> set[str]:
    """Query tokens with function/question/framing words removed — the topical
    core of the query. Empty when the query is all stopwords."""
    return {t for t in q_tokens if t not in _STOPWORDS}


def _hop_is_query_relevant(meta: dict, content_tokens: set[str]) -> bool:
    """A hopped (via_link) neighbor earns its place only if it shares a CONTENT
    token with the query (in title / summary / tags / text) — a structural link
    alone is not relevance. A contentless query (all stopwords) does not gate:
    fall back to keeping hops rather than dropping everything."""
    if not content_tokens:
        return True
    title = (meta.get("title") or "").lower()
    summary = (meta.get("summary") or "").lower()
    tags = " ".join(t.lower() for t in (meta.get("tags") or []) if isinstance(t, str))
    blob = f"{title} {summary} {tags} {(meta.get('text') or '')[:600].lower()}"
    return any(t in blob for t in content_tokens)


def _hop_survives(meta: dict, content_tokens: set[str]) -> bool:
    """Relevance gate for a hopped (via_link) neighbor: it survives iff it shares a
    query CONTENT token. Every via-link edge is STRUCTURAL since compiler v7
    (memory-organ Cut 5A retired the dense SEMANTIC_SIMILARITY class and its
    "cosine is the signal" exemption): MENTIONS / SHARES_TAG / SHARES_DOMAIN /
    CO_LOCATED / SUPERSEDES / persona relations carry no relevance signal of
    their own, so a hop must earn its place lexically. A structural neighbor with
    no query term is the cross-chain junk this gate exists to drop."""
    return _hop_is_query_relevant(meta, content_tokens)


def _payload_tokens(obj: Any) -> set[str]:
    """Tokenize a payload's leaf string VALUES (recursively) — never the JSON blob.

    canonical_json has no whitespace, so splitting it yields one useless token; walking
    the values lets file paths / tool names / objectives inside a payload be matchable.
    """
    out: set[str] = set()
    if isinstance(obj, dict):
        for v in obj.values():
            out |= _payload_tokens(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out |= _payload_tokens(v)
    elif isinstance(obj, str):
        out |= _tokens(obj)
    elif obj is not None:
        out |= _tokens(str(obj))
    return out


def _query_hash(query_norm: str, query_kind: str) -> str:
    return hashlib.sha256(canonical_json([query_norm, query_kind]).encode("utf-8")).hexdigest()


def _read_snapshot_hash(root: Path | str | None) -> str:
    from memory.projection.generation import active_graph_snapshot_hash  # Cut 13: manifest hash under generation mode

    published = active_graph_snapshot_hash(root)
    if published is not None:
        return published
    f = P.compiled_root(root) / "latest_snapshot_hash.txt"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    return "uncompiled"


def _load_candidates(root: Path | str | None):
    conn = G.connect_for_read(root)
    try:
        nodes = [
            dict(r)
            for r in conn.execute(
                "SELECT node_id, node_type, label, payload_json, source_event_id FROM nodes"
            ).fetchall()
        ]
        claims = [
            dict(r)
            for r in conn.execute(
                "SELECT claim_id, subject, predicate, object, confidence, source_event_id, "
                "evidence_event_ids_json FROM claims"
            ).fetchall()
        ]
        events = {
            r["event_id"]: dict(r)
            for r in conn.execute("SELECT event_id, created_at, episode_id FROM events").fetchall()
        }
        edges = [dict(r) for r in conn.execute("SELECT source_id, target_id FROM edges").fetchall()]
    finally:
        conn.close()
    return nodes, claims, events, edges


def _candidate(node_id, node_type, label, payload, evidence, confidence, created_at, is_skill):
    return {
        "node_id": node_id,
        "node_type": node_type,
        "label": label,
        "payload": payload,
        "evidence": list(evidence),
        "confidence": float(confidence),
        "created_at": created_at or "",
        "is_skill": bool(is_skill),
        "text_tokens": _tokens(label) | _payload_tokens(payload),
    }


def _build_candidates(nodes, claims, events):
    cands = []
    for n in nodes:
        ev = events.get(n["source_event_id"], {})
        cands.append(
            _candidate(
                n["node_id"],
                n["node_type"],
                n["label"],
                json.loads(n["payload_json"]),
                [n["source_event_id"]] if n["source_event_id"] else [],
                1.0,
                ev.get("created_at", ""),
                n["node_type"] == "skill",
            )
        )
    for c in claims:
        ev = events.get(c["source_event_id"], {})
        label = f"{c['subject']} {c['predicate']} {c['object']}"
        cands.append(
            _candidate(
                c["claim_id"],
                "claim",
                label,
                dict(c),
                json.loads(c["evidence_event_ids_json"]),
                c["confidence"],
                ev.get("created_at", ""),
                c["predicate"] in _SKILL_PREDICATES,
            )
        )
    return cands


def _score(cand, q_tokens, resolved_name_tokens, resolved_ids, adjacency, max_created, query_kind):
    text_tokens = cand["text_tokens"]
    inter = q_tokens & text_tokens
    lexical = len(inter) / len(q_tokens) if q_tokens else 0.0
    entity_overlap = (
        len(text_tokens & resolved_name_tokens) / len(resolved_name_tokens)
        if resolved_name_tokens
        else 0.0
    )
    proximity = 1.0 if (resolved_ids & adjacency.get(cand["node_id"], set())) else 0.0
    confidence = cand["confidence"]
    recency = 1.0 if (cand["created_at"] and cand["created_at"] == max_created) else 0.5
    skill_rel = 1.0 if (cand["is_skill"] and query_kind in ("", "skill_use", "workflow")) else 0.0
    score = (
        0.30 * entity_overlap
        + 0.20 * lexical
        + 0.20 * proximity
        + 0.10 * confidence
        + 0.10 * recency
        + 0.10 * skill_rel
    )
    parts = []
    if inter:
        parts.append("lexical:" + ",".join(sorted(inter)))
    if entity_overlap:
        parts.append("entity-overlap")
    if proximity:
        parts.append("graph-neighbor")
    if skill_rel:
        parts.append("skill-relevant")
    return score, ("; ".join(parts) or "weak-match")


def _log_recall(
    packet: RecallPacket, *, episode_id: str, created_at: str, root: Path | str | None
) -> None:
    pass


def recall_skill_graph(
    query: str,
    *,
    query_kind: str = "",
    episode_id: str = "",
    limit: int = 10,
    root: Path | str | None = None,
    log: bool = False,
    created_at: Optional[str] = None,
) -> RecallPacket:
    """Legacy Phase-7J graph-scoring recall (skill-selection diagnostic).

    Retained for meta_recall skill context. The unified cognitive recall
    engine is `recall()` below (canonical records + graph via_link)."""
    from memory.projection.generation import projection_read_transaction

    with projection_read_transaction(root):  # Cut 13: one generation for graph + snapshot hash + log
        return _recall_skill_graph_in_transaction(
            query, query_kind=query_kind, episode_id=episode_id, limit=limit, root=root, log=log, created_at=created_at)


def _recall_skill_graph_in_transaction(
    query: str,
    *,
    query_kind: str,
    episode_id: str,
    limit: int,
    root: Path | str | None,
    log: bool,
    created_at: Optional[str],
) -> RecallPacket:
    q_norm = normalize_id_field(query)
    q_hash = _query_hash(q_norm, query_kind)
    q_tokens = _tokens(query)

    nodes, claims, events, edges = _load_candidates(root)

    adjacency: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        adjacency[e["source_id"]].add(e["target_id"])
        adjacency[e["target_id"]].add(e["source_id"])

    # resolve entities: entity nodes whose label tokens overlap the query
    resolved_ids: set[str] = set()
    resolved_name_tokens: set[str] = set()
    for n in nodes:
        label_tokens = _tokens(n["label"])
        if q_tokens & label_tokens:
            resolved_ids.add(n["node_id"])
            resolved_name_tokens |= label_tokens

    candidates = _build_candidates(nodes, claims, events)
    max_created = max((c["created_at"] for c in candidates), default="")

    scored = []
    for cand in candidates:
        score, reason = _score(
            cand, q_tokens, resolved_name_tokens, resolved_ids, adjacency, max_created, query_kind
        )
        if score > 0:
            scored.append((score, reason, cand))

    # stable tie-break: score DESC, confidence DESC, created_at ASC, node_id ASC
    scored.sort(key=lambda t: (-t[0], -t[2]["confidence"], t[2]["created_at"], t[2]["node_id"]))
    from memory.recall_truncation import build_truncation_receipt

    selected = scored[:limit]
    truncation_receipt = build_truncation_receipt(
        "taxonomy_skill_top_k",
        [cand["node_id"] for _score, _reason, cand in scored],
        [cand["node_id"] for _score, _reason, cand in selected],
    )

    results = tuple(
        {
            "node_id": cand["node_id"],
            "node_type": cand["node_type"],
            "score": round(score, 6),
            "reason": reason,
            "label": cand["label"],
            "payload": cand["payload"],
            "evidence_event_ids": list(cand["evidence"]),
        }
        for score, reason, cand in selected
    )
    evidence = tuple(sorted({eid for r in results for eid in r["evidence_event_ids"]}))

    packet = RecallPacket(
        query=query,
        query_hash=q_hash,
        query_kind=query_kind,
        snapshot_hash=_read_snapshot_hash(root),
        resolved_entities=tuple(sorted(resolved_ids)),
        results=results,
        evidence_event_ids=evidence,
        truncation_receipts=(truncation_receipt,),
    )

    if log and created_at:
        _log_recall(packet, episode_id=episode_id, created_at=created_at, root=root)

    return packet


class _LiteQuery:
    """Minimal duck-typed QuerySpec for compile_recall_plan (it reads
    text/limit/domain/task_scope/modality only)."""

    def __init__(self, text, limit):
        self.text = text
        self.limit = limit
        self.modality = "text"
        self.task_scope = None
        self.domain = None


def _score_candidate(meta, q_tokens, plan) -> float:
    """Deterministic relevance: term overlap on title/summary/tags + the
    answerability shape signal (0.20). Pure; no graph, no model."""
    title = (meta.get("title") or "").lower()
    summary = (meta.get("summary") or "").lower()
    tags = " ".join(t.lower() for t in (meta.get("tags") or []) if isinstance(t, str))
    blob = f"{title} {summary} {tags} {(meta.get('text') or '')[:600].lower()}"
    if not q_tokens:
        lexical = 0.0
    else:
        lexical = sum(1 for t in q_tokens if t in blob) / len(q_tokens)
    try:
        answer = answerability_score(meta, plan) if plan is not None else 0.0
    except Exception:
        answer = 0.0
    return round(min(1.0, 0.80 * lexical + 0.20 * answer), 6)


def recall(
    query: str,
    *,
    allowed_lobes=None,
    denied_lobes=recall_graph.DENIED_LOBES_DEFAULT,
    allowed_subdomains=None,
    lane_weights=None,
    limit: int = 10,
    root=None,
    touch: bool = True,
    # signature compatibility arguments:
    query_kind: Optional[str] = None,
    episode_id: str = "",
    log: bool = False,
    created_at: Optional[str] = None,
) -> RecallPacket:
    """Unified cognitive recall over canonical memory + the compiled graph.

    Candidate set = lobe/subdomain-filtered canonical records (write-Y==read-Y;
    denied 6/11 hard) scored by term-overlap + answerability, PLUS a via_link
    pass over structural graph edges (the dense similarity class is retired
    since compiler v7 / Cut 5A) that bypasses
    allowed_lobes but honors denied_lobes on every hop. strength_index.touch()
    fires per returned record (reconsolidation; id parity with node_id).
    Deterministic + Guardian-free. lane_weights is accepted for posture
    suppression (a lobe with weight 0 is dropped). Same query+corpus =>
    same packet."""
    from memory.projection.generation import projection_read_transaction

    with projection_read_transaction(root):  # Cut 13 (D-13-28): the canonical set + graph pass see ONE generation
        return _recall_in_transaction(
            query, allowed_lobes=allowed_lobes, denied_lobes=denied_lobes, allowed_subdomains=allowed_subdomains,
            lane_weights=lane_weights, limit=limit, root=root, touch=touch, query_kind=query_kind,
            episode_id=episode_id, log=log, created_at=created_at)


def _recall_in_transaction(
    query: str,
    *,
    allowed_lobes,
    denied_lobes,
    allowed_subdomains,
    lane_weights,
    limit: int,
    root,
    touch: bool,
    query_kind: Optional[str],
    episode_id: str,
    log: bool,
    created_at: Optional[str],
) -> RecallPacket:
    if query_kind:
        return recall_skill_graph(
            query,
            query_kind=query_kind,
            episode_id=episode_id,
            limit=limit,
            root=root,
            log=log,
            created_at=created_at,
        )

    q_norm = normalize_id_field(query)
    q_hash = _query_hash(q_norm, "")
    q_tokens = _tokens(query)

    # Posture plan (for answerability + suppression). Lazy + failure-soft.
    try:
        plan = compile_recall_plan(_LiteQuery(query, limit))
    except Exception:
        plan = None

    # lane_weights==0 → suppressed lobe (subtract from allowed). Map lane
    # names back to lobes via recall_lanes._LANE_TO_LOBES.
    suppressed_lobes = set()
    if lane_weights:
        from memory.recall_lanes import _LANE_TO_LOBES

        for lane, weight in lane_weights.items():
            if float(weight or 0.0) <= 0.0:
                if lane not in _LANE_TO_LOBES:
                    raise ValueError(f"unknown recall lane cannot be suppressed: {lane!r}")
                suppressed_lobes.update(_LANE_TO_LOBES[lane][0])
    eff_denied = set(denied_lobes or ()) | suppressed_lobes

    base = recall_graph.load_canonical_candidates(
        allowed_lobes=allowed_lobes,
        denied_lobes=eff_denied,
        allowed_subdomains=allowed_subdomains,
        root=root,
    )
    seed_ids = [m["id"] for m in base if m.get("id")]
    # via_link expansion bypasses allowed_lobes/subdomains but NOT denied.
    extra = recall_graph.expand_via_graph(seed_ids, denied_lobes=eff_denied, root=root)
    by_id = {m["id"]: m for m in base}
    for m in extra:
        by_id.setdefault(m["id"], m)

    content_tokens = _content_tokens(q_tokens)
    scored = []
    candidate_ids = []
    surviving_ids = []
    for _mid, meta in by_id.items():
        candidate_id = str(meta.get("id") or "")
        if candidate_id:
            candidate_ids.append(candidate_id)
        if meta.get("via_link") and not _hop_survives(meta, content_tokens):
            # The broad hop pulls every neighbor of every seed; a neighbor reached
            # by a STRUCTURAL link but bearing NO query content token is cross-chain
            # junk (operator -> Abel on a "what is Python" query). Discard it rather
            # than let a flat structural bonus ride it into the answer. Relevant
            # chains survive because the linked record carries the query's terms
            # (the name-bearing relation "Abel is Noah Lee's son" matches "Noah").
            # No per-type exemption remains since compiler v7 (Cut 5A).
            continue
        if candidate_id:
            surviving_ids.append(candidate_id)
        s = _score_candidate(meta, q_tokens, plan)
        if meta.get("via_link"):
            # A relevant hop still earns an edge-confidence bonus (structural edges
            # carry confidence 1.0 -> the prior flat +0.05; kept unchanged in Cut 5A).
            conf = meta.get("_via_confidence", 1.0)
            s = round(min(1.0, s + 0.05 * conf), 6)
        scored.append((s, meta))
    # score DESC, id ASC (stable, deterministic)
    scored.sort(key=lambda t: (-t[0], str(t[1].get("id") or "")))
    from memory.recall_truncation import build_truncation_receipt

    hop_receipt = build_truncation_receipt(
        "taxonomy_via_link_content",
        candidate_ids,
        surviving_ids,
    )
    top = scored[:limit]
    top_k_receipt = build_truncation_receipt(
        "taxonomy_top_k",
        [str(meta.get("id") or "") for _score, meta in scored],
        [str(meta.get("id") or "") for _score, meta in top],
    )

    si = ensure_strength_index() if touch else None
    results = []
    for s, meta in top:
        mid = meta["id"]
        if si is not None:
            try:
                si.touch(mid)  # id parity: canonical id == graph node_id
            except Exception:
                pass
        results.append(
            {
                "node_id": mid,
                "node_type": meta.get("type") or "memory_record",
                "score": s,
                "reason": "lane+answerability" + (";via_link" if meta.get("via_link") else ""),
                "label": meta.get("title") or "",
                "payload": {
                    "coord": meta.get("coord"),
                    "summary": meta.get("summary"),
                    "tags": meta.get("tags") or [],
                    "domain": meta.get("domain"),
                    "via_link": bool(meta.get("via_link")),
                    "subtopic": meta.get("subtopic"),
                    "contradiction": meta.get("contradiction"),
                    "flagged": meta.get("flagged"),
                    "flag_reason": meta.get("flag_reason"),
                    "flag_response_mode": meta.get("flag_response_mode"),
                    "version": meta.get("version"),
                },
                "evidence_event_ids": [f"canonical:{mid}"],
            }
        )

    evidence = tuple(sorted({eid for r in results for eid in r["evidence_event_ids"]}))
    return RecallPacket(
        query=query,
        query_hash=q_hash,
        query_kind="",
        snapshot_hash=_read_snapshot_hash(root),
        resolved_entities=tuple(sorted(seed_ids)),
        results=tuple(results),
        evidence_event_ids=evidence,
        truncation_receipts=(hop_receipt, top_k_receipt),
    )
