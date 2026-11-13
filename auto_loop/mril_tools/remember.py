"""Thin remember_* wrappers over memory.api.recall_verified(QuerySpec)."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional

from auto_loop.mril_tools.lobes import denied_lobes_for_scope
from auto_loop.mril_tools.registry import REMEMBER_TOOL_REGISTRY


def _candidate_value(candidate: Any, key: str, default: Any = None) -> Any:
    if isinstance(candidate, Mapping):
        return candidate.get(key, default)
    if key == "memory_id":
        return getattr(candidate, "memory_id", getattr(candidate, "id", default))
    return getattr(candidate, key, default)


_SOURCE_CITATION_FIELDS = (
    "source_id",
    "source_type",
    "source_session_id",
    "source_session_timestamp",
    "span_start",
    "span_end",
    "span_hash",
    "source_content_hash",
    "evidence_strength",
)


def _source_citation(candidate: Any) -> Dict[str, Any]:
    """Keep the verifier's source-ledger proof beside its admitted evidence.

    RecallVerdict candidates carry ``source_proof`` only after the host has
    checked the canonical record against SourceLedger. Do not reconstruct or
    infer proof fields from the search candidate's generic provenance.
    """
    proof = _candidate_value(candidate, "source_proof")
    if not isinstance(proof, Mapping):
        return {}
    if str(proof.get("evidence_strength") or "") != "source_ledger_crypto":
        return {}
    return {key: proof[key] for key in _SOURCE_CITATION_FIELDS if key in proof}


def _legacy_retrieval_verdict(result: Any) -> tuple[str, str]:
    meta = getattr(result, "meta", None)
    if isinstance(meta, Mapping):
        verdict = meta.get("recall_verdict")
        if isinstance(verdict, Mapping):
            return (
                str(verdict.get("status") or "unknown"),
                str(verdict.get("reason") or ""),
            )
    # Older callers inject a post-read RetrievalResult rather than a
    # RecallVerdict. Treat it as already gated unless it carries explicit
    # negative verdict metadata.
    return "verified", "legacy RetrievalResult accepted as gated recall"


# ── Relevance floor (Gate-1 finding, operator spec 2026-07-04) ──
# Truth verification and answer relevance are SEPARATE checks. The verifier
# proves a memory is real/source-backed; it does not prove the memory answers
# the question. Grounding labels:
#   VERIFIED_RELEVANT      → can enter answer context
#   VERIFIED_NONRESPONSIVE → real memory, blocked from the answer model
#   UNKNOWN                → no usable memory
#
# The floor is FULLY DETERMINISTIC: lexical anchor OR strong recall
# provenance. Embed-cosine rescue was tried twice (floors 0.35 and 0.55) and
# leaked both times — same-theme wrong memories genuinely overlap true
# recoveries in cosine space, so no threshold separates them (Gate-1 live:
# 3–4 fabricated recoveries per run, and the service dependence broke
# double-run byte-stability). Paraphrase belongs to canonicalization, not
# fuzz (ratified). True-but-unanchored candidates stay VISIBLE in
# nonresponsive_evidence — blocked from the answer model, never lost.

# graph/coordinate-anchored relevance: direct/union matched the query's own
# coordinates; strong_hop1 is one compiler-proven link (entity/session/record)
# from a match — the alias lane. Deterministic, no embed service involved.
# NOT strong_hop2: two hops of provenance is connectivity, not responsiveness.
_RELEVANCE_ANCHORED_CLASSES = frozenset({"direct", "union", "strong_hop1"})


def _content_tokens(text: str) -> set:
    # ONE shared token grammar (canonicalize.py) with the pre-verifier anchor
    # filter — write-side and read-side vocabularies can never drift.
    from memory.taxonomy.canonicalize import content_tokens

    return content_tokens(text)


def _candidate_text(candidate) -> str:
    return " ".join(
        str(_candidate_value(candidate, k, "") or "") for k in ("summary", "title", "text")
    )


def _relevance_anchored(candidate) -> bool:
    """Graph/coordinate-anchored relevance from recall provenance — the
    deterministic lane (no embed service): the candidate either matched the
    query's own coordinates (direct/union) or is ONE compiler-proven link
    from a match (strong_hop1 = the alias lane)."""
    prov = _candidate_value(candidate, "provenance") or {}
    if bool(_candidate_value(candidate, "semantic_recall", False)):
        return True
    # Cut 3 (memory/recall_basis.py): the RecallVerifier admitted this row
    # through a RecallBasis it independently revalidated against the live
    # projection/graph and set this mark itself. That routing proof is the
    # anchor. A bare recall_basis_id anchors nothing — only the verifier's mark.
    if _candidate_value(candidate, "recall_basis_validated", False) is True:
        return True
    if not isinstance(prov, Mapping):
        return False
    return str(prov.get("dme_edge_class") or "") in _RELEVANCE_ANCHORED_CLASSES


_OPERATOR_TOKEN = "operator"


def _resolve_operator_display_name():
    """The operator's display name from the canonical operator-name anchor.

    A thin seam over ``memory.persona_ingest`` so the READ side resolves the
    operator through the exact same authority the WRITE side already uses, and
    so admission stays testable without a live coord index."""
    from memory.persona_ingest import _resolve_operator_name

    return _resolve_operator_name()


def _operator_alias_tokens(q_tokens):
    """Bridge the operator's NAME and the literal token "operator".

    Persona facts are written speaker-relative ("operator lives in DeRidder")
    but the operator is asked about by name ("what do you remember about
    Noah"). Those share no content token, so lexical admission drops every
    speaker-relative record. Measured live 2026-07-31 (boot #1): 13 records
    retrieved, 4 admitted, and the 4 survivors were exactly the ones spelling
    "Noah" -- birthday, location, occupation, pronouns, relationship and
    timezone were all thrown away by ``overlap == set()``, before the
    question-form gate was ever consulted.

    Omega already HELD this binding: ``1.00.operator_name`` states "when a
    memory refers to 'the operator' ... it means Noah Lee", and it was among
    the recalled records. It was being consumed as evidence and never as an
    admission rule. ``memory.persona_ingest`` resolves the same aliases on the
    write side; this is the read-side half of that symmetry.

    Failure-soft by contract: an unnamed operator, or an unreadable index,
    yields no aliases and admission behaves exactly as it did before."""
    try:
        name = _resolve_operator_display_name()
    except Exception:  # noqa: BLE001 — alias enrichment must never break recall
        return frozenset()
    name_tokens = _content_tokens(str(name or ""))
    if not name_tokens:
        return frozenset()
    if q_tokens & name_tokens:
        return frozenset({_OPERATOR_TOKEN})
    if _OPERATOR_TOKEN in q_tokens:
        return frozenset(name_tokens)
    return frozenset()


def _partition_responsive(query: str, candidates):
    """Split candidates into (responsive, nonresponsive) — deterministically.

    Responsive = (lexical content-token overlap OR strong recall provenance)
    AND the question-form payload check: a WHEN-question is only answerable
    by temporal evidence, WHO by an agent, WHERE by location/placement
    (ratified question-form canonicalization). Everything else is
    real-but-blocked. No model calls, byte-identical across runs."""
    from memory.taxonomy.canonicalize import carries_form_payload, question_form

    responsive, blocked = [], []
    q_tokens = _content_tokens(query)
    # Speaker-relative persona records ("operator lives in ...") share no
    # content token with a by-name question ("what do you remember about
    # Noah"), so without this bridge they are dropped at admission even though
    # recall retrieved them. See _operator_alias_tokens.
    q_tokens = q_tokens | _operator_alias_tokens(q_tokens)
    form = question_form(query)
    for c in candidates or []:
        text = _candidate_text(c)
        overlap = q_tokens & _content_tokens(text)
        anchored = bool(overlap) or _relevance_anchored(c)
        # the form gate scopes to MARGINAL admissions (≤1 shared token — the
        # pun class). A multi-token lexical anchor means the memory is about
        # the asked thing; whether it answers the form is the answer model's
        # judgment. Payload lexicons cannot enumerate English ("redeemed …
        # in my email inbox" carries a WHERE answer no lexicon predicts).
        if anchored and (len(overlap) >= 2 or carries_form_payload(form, text)):
            responsive.append(c)
        else:
            blocked.append(c)
    return responsive, blocked


def _recall_and_extract(read_fn, spec):
    """Run one recall and normalize
    (status, reason, pressure_signal, candidates, retrieval_meta).

    ``retrieval_meta`` is the recall path's metadata dict (RecallVerdict
    ``answer_payload["meta"]`` / legacy ``RetrievalResult.meta``) — carried so
    diagnostic receipts such as the Cut-1 ``dme_activation_trace`` can ride the
    remember output without touching evidence."""
    result = read_fn(spec)
    verdict_status = ""
    verdict_reason = ""
    pressure_signal = ""
    payload_candidates = None
    retrieval_meta: Dict[str, Any] = {}
    if hasattr(result, "status") and hasattr(result, "reason"):
        verdict_status = str(getattr(result, "status", "") or "")
        verdict_reason = str(getattr(result, "reason", "") or "")
        try:
            from memory.recall_verifier import pressure_signal_for_status

            pressure_signal = pressure_signal_for_status(verdict_status)
        except Exception:
            pressure_signal = ""
        # A quarantined conflict (the clean facts still answered, so status is 'verified')
        # must STILL raise the reconciliation drive — Omega should notice the contradiction
        # even while answering from the unrelated facts around it.
        blocked = getattr(result, "blocked_conflicts", None)
        if not pressure_signal and blocked:
            pressure_signal = "memory_reconciliation"
        # ROUTE the quarantined conflict to DeepSleep for eventual reconciliation (the verifier
        # only QUARANTINES; resolution is DeepSleep housekeeping). Best-effort — never break recall.
        if blocked:
            try:
                from memory.contradiction_resolution import enqueue_blocked_conflicts

                enqueue_blocked_conflicts(blocked)
            except Exception:
                pass
        payload = getattr(result, "answer_payload", None)
        if isinstance(payload, dict):
            payload_candidates = list(payload.get("candidates") or [])
            meta = payload.get("meta")
            retrieval_meta = dict(meta) if isinstance(meta, dict) else {}
    elif hasattr(result, "candidates"):
        verdict_status, verdict_reason = _legacy_retrieval_verdict(result)
        payload_candidates = list(getattr(result, "candidates", None) or [])
        meta = getattr(result, "meta", None)
        retrieval_meta = dict(meta) if isinstance(meta, dict) else {}
    else:
        verdict_status = "unknown"
        verdict_reason = "recall path did not return a RecallVerdict"
        payload_candidates = []
    return verdict_status, verdict_reason, pressure_signal, payload_candidates, retrieval_meta


def _collect_dme_trace(dme_traces: List[Dict[str, Any]], retrieval_meta: Mapping[str, Any]) -> None:
    """Append this attempt's DME activation trace, if the recall path attached
    one. No dedupe: each widening rung is a distinct DME execution and keeping
    every trace is correct observability."""
    trace = retrieval_meta.get("dme_activation_trace") if retrieval_meta else None
    if isinstance(trace, dict):
        dme_traces.append(dict(trace))


def _observed_at_map() -> Dict[str, str]:
    """{memory_id: observed_at iso} from the live sidecar; empty when unavailable."""
    try:
        from memory.engine.validate_write import MEMORY_ROOT
        from memory.observed_at import all_observed_at

        return all_observed_at(MEMORY_ROOT / "indexes" / "observed_at.json")
    except Exception:  # noqa: BLE001 — dating evidence is a convenience, never a failure
        return {}


def invoke_remember_tool(
    tool_spec: Mapping[str, Any],
    episode_id: str,
    *,
    memory: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Run one remember_* tool and return the MRIL output envelope."""
    tool = str(tool_spec.get("tool") or "")
    if tool not in REMEMBER_TOOL_REGISTRY:
        raise ValueError(f"unknown remember tool: {tool!r}")

    lane, default_lobe, default_memory_type = REMEMBER_TOOL_REGISTRY[tool]
    allowed = list(tool_spec.get("allowed_lobes") or [default_lobe])
    if allowed != [default_lobe]:
        raise ValueError(f"{tool} requires exact allowed_lobes [{default_lobe}], got {allowed}")

    read_fn = memory
    if read_fn is None:
        from memory.api import recall_verified as read_fn  # type: ignore[assignment]

    from memory.api import QuerySpec
    from pressure_loop.policy import auto_memory_recall_limit

    query_text = str(tool_spec.get("query") or "")
    memory_type = str(tool_spec.get("memory_type") or default_memory_type)
    domain = str(tool_spec.get("domain") or "").strip() or None
    modality = "vision" if lane == "vision" else str(tool_spec.get("modality") or "text")

    allowed_subdomains = None
    if tool_spec.get("allowed_subdomains") is not None:
        allowed_subdomains = [int(y) for y in tool_spec["allowed_subdomains"]]
    elif tool_spec.get("subdomain") is not None:
        allowed_subdomains = [int(tool_spec["subdomain"])]

    # A per-tool limit lets a whole-lobe grounding recall (identity/persona) pull its small lobe
    # exhaustively; everything else uses the global default.
    spec_limit = tool_spec.get("limit")
    recall_limit = int(spec_limit) if spec_limit else auto_memory_recall_limit()

    spec = QuerySpec(
        cycle_id=f"{episode_id}-mril-{tool}",
        episode_id=episode_id,
        text=query_text,
        modality=modality,
        task_scope="RESPOND",
        domain=domain,
        limit=recall_limit,
        allowed_lobes=allowed,
        denied_lobes=denied_lobes_for_scope((lane,)),
        allowed_subdomains=allowed_subdomains,
    )

    # Cut 1: DME activation traces from every recall attempt (diagnostic receipt only)
    dme_traces: List[Dict[str, Any]] = []

    verdict_status, verdict_reason, pressure_signal, payload_candidates, retrieval_meta = (
        _recall_and_extract(read_fn, spec)
    )
    _collect_dme_trace(dme_traces, retrieval_meta)

    # ── _unsorted recovery (stress battery 2026-07-04) ──
    # The write allocator conservatively parks low-confidence memories at
    # _unsorted (y=127) rather than guessing a subdomain. A GRAMMAR-scoped
    # recall (router resolved one Y from the query wording) must look there
    # before reporting empty, or every safe deferral is unreachable. Callers
    # that pass an explicit allowed_subdomains list keep their exact contract —
    # no widen. One retry, recall-side only; the writer stays untouched.
    widened_unsorted = False
    grammar_scoped_y = (
        tool_spec.get("allowed_subdomains") is None and tool_spec.get("subdomain") is not None
    )
    if grammar_scoped_y and not payload_candidates:
        from memory.dewey_map import UNSORTED_SUBDOMAIN_Y

        if UNSORTED_SUBDOMAIN_Y not in (allowed_subdomains or []):
            widened_unsorted = True
            spec = QuerySpec(
                cycle_id=f"{episode_id}-mril-{tool}-unsorted",
                episode_id=episode_id,
                text=query_text,
                modality=modality,
                task_scope="RESPOND",
                domain=domain,
                limit=recall_limit,
                allowed_lobes=allowed,
                denied_lobes=denied_lobes_for_scope((lane,)),
                allowed_subdomains=[*(allowed_subdomains or []), UNSORTED_SUBDOMAIN_Y],
            )
            verdict_status, verdict_reason, pressure_signal, payload_candidates, retrieval_meta = (
                _recall_and_extract(read_fn, spec)
            )
            _collect_dme_trace(dme_traces, retrieval_meta)

    # rung 3 of the RATIFIED widen ladder: the whole lobe before UNKNOWN. A
    # grammar-resolved Y is a routing hypothesis, not a contract — the memory
    # may live at a sibling subdomain. Explicit allowed_subdomains callers
    # never reach here (grammar_scoped_y is False for them).
    widened_lobe = False

    # relevance floor: partition truth-verified candidates into responsive
    # (answer context) vs non-responsive (real, blocked, kept for observability).
    # EXEMPT: whole-lobe grounding recalls (identity/persona with an explicit
    # per-tool limit) — "the recall is EXACT (the full self), never a
    # wording-shaped slice that can miss the creator." Grounding is not
    # question-answering; relevance-to-the-greeting is meaningless there.
    grounding_recall = bool(spec_limit) and lane in ("identity", "persona")
    if grounding_recall:
        responsive, nonresponsive = list(payload_candidates or []), []
    else:
        responsive, nonresponsive = _partition_responsive(query_text, payload_candidates)
        if grammar_scoped_y and not responsive:
            widened_lobe = True
            spec = QuerySpec(
                cycle_id=f"{episode_id}-mril-{tool}-lobe",
                episode_id=episode_id,
                text=query_text,
                modality=modality,
                task_scope="RESPOND",
                domain=domain,
                limit=recall_limit,
                allowed_lobes=allowed,
                denied_lobes=denied_lobes_for_scope((lane,)),
                allowed_subdomains=None,
            )
            verdict_status, verdict_reason, pressure_signal, payload_candidates, retrieval_meta = (
                _recall_and_extract(read_fn, spec)
            )
            _collect_dme_trace(dme_traces, retrieval_meta)
            responsive, nonresponsive = _partition_responsive(query_text, payload_candidates)

    observed_at_by_id = _observed_at_map()

    def _remembered_as(candidate: Any) -> str:
        summary = str(
            _candidate_value(candidate, "summary", "")
            or _candidate_value(candidate, "title", "")
            or ""
        )
        # Plugin result records deliberately keep the bounded canonical event
        # text out of generic summaries. Include it only for this event kind so
        # ordinary persona/knowledge recall remains concise and unchanged.
        subtopic = str(_candidate_value(candidate, "subtopic", "") or "")
        candidate_type = str(_candidate_value(candidate, "type", "") or "")
        if subtopic == "tool_result" or candidate_type == "dev_tool_observation" or subtopic.startswith("dev_"):
            excerpt = str(_candidate_value(candidate, "text", "") or "")[:1600].strip()
            if excerpt and excerpt not in summary:
                return f"{summary}\n{excerpt}" if summary else excerpt
        return summary

    def _shape(c) -> Dict[str, Any]:
        coord = list(_candidate_value(c, "coord", []) or [])
        if len(coord) == 2:
            coord = coord + [0]
        score = float(_candidate_value(c, "score", 0.0) or 0.0)
        memory_id = str(_candidate_value(c, "memory_id", "") or "")
        evidence = {
            "memory_id": memory_id,
            "coord": coord or [default_lobe, 0, 0],
            # when the record was observed (sidecar); episodes are read in time
            # order and rendered with their date (live 2026-09-06: "what did I
            # ask you about earlier?" got eight undated transcripts in score
            # order and the model could not tell which was earlier)
            "observed_at": str(observed_at_by_id.get(memory_id) or ""),
            "remembered_as": _remembered_as(c),
            "title": str(_candidate_value(c, "title", "") or ""),
            "confidence": score,
            # on-file relation facets (verifier-stashed) — the subject-centered planner
            # groups on these; absent -> the item renders as a plain topical bullet.
            "relation_facets": list(_candidate_value(c, "relation_facets", []) or []),
        }
        citation = _source_citation(c)
        if citation:
            evidence["source_citation"] = citation
        return evidence

    # canonical envelope order: score-desc, then memory_id — upstream lane
    # arbitration can emit equal-scored candidates in unstable order (Gate-1
    # byte-stability tracer: identical SETS, drifting order in the blocked
    # list). Ties must break on the id, never on iteration accidents.
    def _canonical_order(items):
        return sorted(
            items,
            key=lambda c: (
                -float(_candidate_value(c, "score", 0.0) or 0.0),
                str(_candidate_value(c, "memory_id", "") or ""),
            ),
        )

    responsive = _canonical_order(responsive)
    if int(default_lobe) == 9:
        # the conversation itself: newest first, so "earlier" / "last" are answerable
        responsive = sorted(
            responsive,
            key=lambda c: str(observed_at_by_id.get(str(_candidate_value(c, "memory_id", "") or "")) or ""),
            reverse=True,
        )
    nonresponsive_evidence = [_shape(c) for c in _canonical_order(nonresponsive)]

    evidence: List[Dict[str, Any]] = []
    confidences: List[float] = []
    for c in responsive:
        coord = list(_candidate_value(c, "coord", []) or [])
        if len(coord) == 2:
            coord = coord + [0]
        score = float(_candidate_value(c, "score", 0.0) or 0.0)
        confidences.append(score)
        memory_id = str(_candidate_value(c, "memory_id", "") or "")
        item = {
            "memory_id": memory_id,
            "coord": coord or [default_lobe, 0, 0],
            "observed_at": str(observed_at_by_id.get(memory_id) or ""),
            "remembered_as": _remembered_as(c),
            "title": str(_candidate_value(c, "title", "") or ""),
            "confidence": score,
            # on-file relation facets (verifier-stashed) — the subject-centered planner
            # groups on these; absent -> the item renders as a plain topical bullet.
            "relation_facets": list(_candidate_value(c, "relation_facets", []) or []),
        }
        citation = _source_citation(c)
        if citation:
            item["source_citation"] = citation
        evidence.append(item)

    if not evidence:
        try:
            from guardian.terminal_feedback import emit_guardian_event, emit_memory

            emit_memory(
                "recall",
                coord=[default_lobe, 0, 0],
                status="empty",
                candidate_count=0,
                tool=tool,
                episode_id=episode_id,
            )
            emit_guardian_event(
                "omega.auto",
                "mril_remember",
                episode_id=episode_id,
                tool=tool,
                lobe=default_lobe,
                candidate_count=0,
                status="empty",
            )
        except Exception:
            pass
        return {
            "kind": "remembered_knowledge",
            "tool": tool,
            "memory_type": "none",
            "lane": lane,
            "lobe": default_lobe,
            "recall_reason": "operator_query",
            "evidence": [],
            "confidence": 0.0,
            "receipt": ({"dme_activation_traces": dme_traces} if dme_traces else {}),
            "verdict": verdict_status,
            "verdict_reason": verdict_reason
            or ("no verified memory recalled" if verdict_status else ""),
            "pressure_signal": pressure_signal,
            "widened_unsorted": widened_unsorted,
            "widened_lobe": widened_lobe,
            "grounding": (
                "VERIFIED_NONRESPONSIVE"
                if nonresponsive_evidence and verdict_status == "verified"
                else "UNKNOWN"
            ),
            "nonresponsive_evidence": nonresponsive_evidence,
        }

    overall = max(confidences) if confidences else 0.0
    try:
        from guardian.terminal_feedback import emit_guardian_event, emit_memory

        top_coord = evidence[0].get("coord") if evidence else [default_lobe, 0, 0]
        emit_memory(
            "recall",
            coord=top_coord,
            status="ok",
            candidate_count=len(evidence),
            tool=tool,
            episode_id=episode_id,
        )
        emit_guardian_event(
            "omega.auto",
            "mril_remember",
            episode_id=episode_id,
            tool=tool,
            lobe=default_lobe,
            candidate_count=len(evidence),
            status="ok",
        )
    except Exception:
        pass
    return {
        "kind": "remembered_knowledge",
        "tool": tool,
        "memory_type": memory_type,
        "lane": lane,
        "lobe": default_lobe,
        "recall_reason": "operator_query",
        "evidence": evidence,
        "confidence": overall,
        "receipt": ({"dme_activation_traces": dme_traces} if dme_traces else {}),
        "verdict": verdict_status or "unknown",
        "verdict_reason": verdict_reason,
        "pressure_signal": pressure_signal,
        "widened_unsorted": widened_unsorted,
        "widened_lobe": widened_lobe,
        "grounding": "VERIFIED_RELEVANT",
        "nonresponsive_evidence": nonresponsive_evidence,
    }
