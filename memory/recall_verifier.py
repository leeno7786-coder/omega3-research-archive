"""Gate-first recall verification over canonical memory records.

Cited canonical records verify through SourceLedger span_hash crypto proof.
Records without source citations still use re-derive verification over the
live canonical file. The public verdict contract stays stable.
"""

from __future__ import annotations

import json
import logging
import re
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from core.paths import OMEGA_ROOT
from memory.recall_features import derived_recall_features
from memory.taxonomy.schemas import RecallVerdict

log = logging.getLogger("omega.memory.recall_verifier")

_STATUS_PRIORITY = {"conflict": 3, "ambiguous": 2, "unknown": 1, "verified": 0}
_PRESSURE_BY_STATUS = {
    "unknown": "memory_gap",
    "ambiguous": "sense_resolution",
    "conflict": "memory_reconciliation",
}


def pressure_signal_for_status(status: str) -> str:
    return _PRESSURE_BY_STATUS.get(str(status or "").strip().lower(), "")


def append_verdict_log(
    verdict: RecallVerdict,
    *,
    cycle_id: str = "",
    root: Path | str | None = None,
) -> dict[str, Any]:
    created_at = datetime.now(timezone.utc).isoformat()
    admitted_count = len(
        (verdict.answer_payload or {}).get("candidates") or ()
    )
    retrieved_count = _retrieved_candidate_count(verdict, admitted_count=admitted_count)
    record = {
        "type": "recall_verdict_log",
        "verdict_id": _verdict_id(verdict, cycle_id=cycle_id, created_at=created_at),
        "created_at": created_at,
        "cycle_id": str(cycle_id or ""),
        "status": verdict.status,
        "pressure_signal": pressure_signal_for_status(verdict.status),
        "query": verdict.query,
        "reason": verdict.reason,
        "evidence_count": len(verdict.evidence),
        "admitted_count": admitted_count,
        "rejected_count": len(verdict.rejected_hits),
        "retrieved_count": retrieved_count,
        "verified_memory_ids": _verified_memory_ids(verdict),
        "retrieval_trace": dict(verdict.retrieval_trace or {}),
    }
    path = verdict_log_path(root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    return record


def verdict_log_path(*, root: Path | str | None = None) -> Path:
    return _memory_root(root) / "recall_verifier" / "verdict_log.jsonl"


def _verdict_id(verdict: RecallVerdict, *, cycle_id: str, created_at: str) -> str:
    raw = json.dumps(
        {
            "cycle_id": cycle_id,
            "created_at": created_at,
            "status": verdict.status,
            "query": verdict.query,
            "reason": verdict.reason,
        },
        sort_keys=True,
    )
    return "rv_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _verified_memory_ids(verdict: RecallVerdict) -> list[str]:
    """The ids this verdict actually GROUNDED on, for the durable audit row.

    Read only from the FINAL ``evidence`` of a ``verified`` verdict: that is the
    set the answer rests on. Every other status grounds nothing and records the
    empty list, so "verified nothing" and "did not verify" stay distinguishable
    from "verified these". The pre-verifier candidate trace is deliberately NOT
    a source — it names rows the gate may since have demoted or rejected, and a
    rejected id in an audit row of what was believed is the exact confusion this
    field exists to remove.
    """
    if str(getattr(verdict, "status", "") or "") != "verified":
        return []
    memory_ids: list[str] = []
    seen: set[str] = set()
    for row in getattr(verdict, "evidence", None) or ():
        if not isinstance(row, Mapping):
            continue
        memory_id = str(row.get("memory_id") or "").strip()
        if not memory_id or memory_id in seen:
            continue
        seen.add(memory_id)
        memory_ids.append(memory_id)
    return memory_ids


def _retrieved_candidate_count(
    verdict: RecallVerdict, *, admitted_count: int | None = None
) -> int:
    """Count rows presented to the verifier; upstream top-K drops are separate receipts."""
    accounting = (verdict.retrieval_trace or {}).get("candidate_accounting")
    if isinstance(accounting, Mapping) and accounting.get("retrieved_count") is not None:
        return int(accounting["retrieved_count"])
    pool = (verdict.retrieval_trace or {}).get("pre_verifier_candidate_pool")
    if isinstance(pool, Mapping) and pool.get("candidate_count") is not None:
        return int(pool["candidate_count"])
    if admitted_count is None:
        admitted_count = len((verdict.answer_payload or {}).get("candidates") or ())
    return int(admitted_count) + len(verdict.rejected_hits)


def _with_candidate_accounting(
    retrieval_trace: Mapping[str, Any],
    *,
    retrieved_count: int,
    admitted_count: int,
    rejected_count: int,
) -> dict[str, Any]:
    """Attach the terminal accounting for verifier inputs and enforce the count law."""
    counts = {
        "scope": "pre_verifier_candidate_pool",
        "retrieved_count": int(retrieved_count),
        "admitted_count": int(admitted_count),
        "rejected_count": int(rejected_count),
    }
    if counts["retrieved_count"] != counts["admitted_count"] + counts["rejected_count"]:
        raise RuntimeError(
            "recall verifier candidate accounting mismatch: "
            f"retrieved={counts['retrieved_count']} admitted={counts['admitted_count']} "
            f"rejected={counts['rejected_count']}"
        )
    return {**dict(retrieval_trace), "candidate_accounting": counts}


def verify_leaf(leaf_id: str, *, root: Path | str | None = None) -> RecallVerdict:
    """Revalidate one canonical memory record against live filesystem truth."""
    memory_id = str(leaf_id or "").strip()
    coord_index = _load_coord_index(root)
    entry = coord_index.get(memory_id)
    if not isinstance(entry, dict):
        return RecallVerdict(
            status="unknown",
            query=memory_id,
            reason="coord_index entry missing",
        )

    path = _resolve_path(entry.get("path"), root=root)
    record = _read_json(path) if path and path.exists() else None
    if not isinstance(record, dict):
        return RecallVerdict(
            status="unknown",
            query=memory_id,
            reason="canonical file missing",
        )

    cand = {
        "memory_id": memory_id,
        "coord": list(record.get("coord") or entry.get("coord") or []),
        "score": 1.0,
        "title": str(record.get("title") or ""),
        "summary": str(record.get("summary") or ""),
    }
    outcome = _verify_candidate(cand, coord_index, root=root)
    if outcome.get("verified"):
        verified = outcome["candidate"]
        return RecallVerdict(
            status="verified",
            query=memory_id,
            answer_payload={"candidates": (verified,)},
            evidence=(
                {
                    "memory_id": memory_id,
                    "path": verified.get("path"),
                    "coord": list(verified.get("coord") or []),
                    "title": verified.get("title") or "",
                    "score": float(verified.get("score") or 0.0),
                    **dict(verified.get("source_proof") or {"evidence_strength": "rederive"}),
                },
            ),
            reason="verified canonical leaf against live file",
        )

    rejected = outcome.get("rejected") or {}
    status = str(rejected.get("status") or "unknown")
    return RecallVerdict(
        status=status if status in _STATUS_PRIORITY else "unknown",
        query=memory_id,
        rejected_hits=(dict(rejected),),
        reason=str(rejected.get("reason") or status),
    )


def verify_retrieval_result(
    query: str,
    retrieval_result: Any,
    *,
    root: Path | str | None = None,
    allowed_lobes: Iterable[int] | None = None,
    include_historical_sources: bool = False,
    denied_lobes: Iterable[int] | None = None,
    query_spec: Any = None,
) -> RecallVerdict:
    candidates = []
    for candidate in getattr(retrieval_result, "candidates", None) or []:
        candidates.append(
            {
                "memory_id": str(getattr(candidate, "id", "") or ""),
                "coord": list(getattr(candidate, "coord", None) or []),
                "score": float(getattr(candidate, "score", 0.0) or 0.0),
                "title": str(getattr(candidate, "title", "") or ""),
                "summary": str(getattr(candidate, "summary", "") or ""),
                "domain": getattr(candidate, "domain", None),
                "subtopic": getattr(candidate, "subtopic", None),
                "tags": list(getattr(candidate, "tags", None) or []),
                "provenance": dict(getattr(candidate, "provenance", None) or {}),
                "path": getattr(candidate, "path", None),
                "filesystem_fallback": bool(getattr(candidate, "filesystem_fallback", False)),
                "semantic_recall": bool(getattr(candidate, "semantic_recall", False)),
                # Cut 3: routing-proof id (memory/recall_basis.py); looked up in
                # meta["recall_bases"] and independently revalidated at the gate
                "recall_basis_id": getattr(candidate, "recall_basis_id", None) or None,
            }
        )
    meta = dict(getattr(retrieval_result, "meta", None) or {})
    return verify_candidates(
        query,
        candidates,
        meta=meta,
        root=root,
        allowed_lobes=allowed_lobes,
        include_historical_sources=include_historical_sources,
        denied_lobes=denied_lobes,
        query_spec=query_spec,
    )


def verify_candidates(
    query: str,
    candidates: Iterable[Mapping[str, Any]],
    *,
    meta: Mapping[str, Any] | None = None,
    root: Path | str | None = None,
    allowed_lobes: Iterable[int] | None = None,
    include_historical_sources: bool = False,
    denied_lobes: Iterable[int] | None = None,
    query_spec: Any = None,
) -> RecallVerdict:
    candidates = [dict(cand) for cand in candidates]
    from memory.recall_truncation import candidate_pool_trace

    receipts = (meta or {}).get("candidate_truncation_receipts") or []
    retrieval_trace = {
        "pre_verifier_candidate_pool": candidate_pool_trace(
            str(candidate.get("memory_id") or "") for candidate in candidates
        ),
        "truncation_receipts": [
            dict(receipt)
            for receipt in receipts[:32]
            if isinstance(receipt, Mapping)
        ],
    }

    # ── VERDICT TOPICALITY GATE, rule 1 (operator adjudication 2026-07-18):
    # the ROUTED REGION is the topicality authority. Region = the caller's
    # explicit allowed_lobes (the deterministic router already picked them for
    # MRIL recall tools) or, unscoped, the lobe route_memory_tool names for
    # the query text. A query whose region is structurally NON-RECALLABLE
    # (journal = metacognitive state, vision = dedicated pipeline) grounds
    # NOTHING: strict unknown, provenance-real residue included. This is what
    # closed the verified-on-residue half of the 2026-07-17 verdict gap
    # ("DeepSleep … journal" grounding identity records at 0.956).
    region_refusal = _non_recallable_region_reason(query, allowed_lobes)
    if region_refusal:
        rejected_hits = tuple(
            {
                "memory_id": str(cand.get("memory_id") or ""),
                "status": "unknown",
                "reason": region_refusal,
            }
            for cand in candidates
        )
        return RecallVerdict(
            status="unknown",
            query=query,
            rejected_hits=rejected_hits,
            reason=region_refusal,
            retrieval_trace=_with_candidate_accounting(
                retrieval_trace,
                retrieved_count=len(candidates),
                admitted_count=0,
                rejected_count=len(rejected_hits),
            ),
        )

    coord_index = _load_coord_index(root)
    verified: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for cand in candidates:
        outcome = _verify_candidate(
            dict(cand),
            coord_index,
            root=root,
            include_historical_sources=include_historical_sources,
        )
        if outcome.get("verified"):
            verified.append(outcome["candidate"])
        else:
            rejected.append(outcome["rejected"])

    verified_before_sense_filter = verified
    verified = _filter_verified_by_query_sense(query, verified, root=root)
    retained_candidate_objects = {id(candidate) for candidate in verified}
    rejected.extend(
        _withheld_candidate_receipt(
            candidate,
            status="unknown",
            reason="verified candidate did not match the resolved query sense",
        )
        for candidate in verified_before_sense_filter
        if id(candidate) not in retained_candidate_objects
    )
    # ── Cut 3 (memory/recall_basis.py): independently revalidated RecallBasis
    # ids. Runs AFTER canonical/source verification, on verified rows only —
    # a basis is routing/topicality evidence and can never make an unverified
    # candidate clean. Kill switch OMEGA_RECALL_BASIS_TOPICALITY=0 restores
    # the exact pre-Cut-3 topicality behaviour.
    validated_basis_ids: set[str] = set()
    try:
        from memory.recall_basis import recall_basis_topicality_enabled

        if recall_basis_topicality_enabled():
            validated_basis_ids = _validated_basis_ids(
                verified,
                meta,
                root=root,
                query=query,
                allowed_lobes=allowed_lobes,
                denied_lobes=denied_lobes,
                query_spec=query_spec,
            )
    except Exception:  # noqa: BLE001 — basis machinery is optional; the gate is not
        validated_basis_ids = set()
    # ── VERDICT TOPICALITY GATE, rule 2: deterministic topicality floor.
    # Provenance-real is NOT topical: a verified candidate sharing zero query
    # content-tokens (stopword-stripped) with its canonical content cannot
    # ground an answer — it demotes to strict unknown. Set-membership, not a
    # score threshold (scores provably do not separate: legit 0.04–0.195 vs
    # residue up to 0.956). Same lexical rule recall() applies to STRUCTURAL
    # graph hops — with one honest divergence: recall() exempts
    # SEMANTIC_SIMILARITY hops (the edge cosine is its relevance signal), but
    # candidates reach the verdict without edge provenance, so the floor here
    # applies uniformly; a zero-lexical-overlap semantic hop demotes to strict
    # unknown (the operator-chosen fail-closed posture until a semantic layer
    # is explicitly added). A contentless query does not gate. This closed the
    # other half of the verdict gap ("replay badge color" grounding the ATP
    # record → the quarantined conflict now surfaces).
    verified, atopical = _partition_topicality(
        query, verified, validated_basis_ids=validated_basis_ids
    )
    rejected.extend(atopical)
    # Ambiguity — a single-token query resolving to multiple live SENSES ("neuron": bio vs
    # ML) — takes precedence over conflict partitioning: the records don't disagree, the
    # QUERY is underspecified. Same-title/different-summary reads as a conflict otherwise.
    if verified and _is_ambiguous_query(query, verified):
        ambiguity_reason = "verified candidate withheld because the query resolves to multiple senses"
        rejected.extend(
            _withheld_candidate_receipt(candidate, status="ambiguous", reason=ambiguity_reason)
            for candidate in verified
        )
        return RecallVerdict(
            status="ambiguous",
            query=query,
            answer_payload=None,
            evidence=(),
            rejected_hits=tuple(rejected),
            reason="query resolves to multiple live canonical senses",
            retrieval_trace=_with_candidate_accounting(
                retrieval_trace,
                retrieved_count=len(candidates),
                admitted_count=0,
                rejected_count=len(rejected),
            ),
        )
    # Quarantine contradicting singular claims BEFORE classification so a conflict on one
    # anchor ("operator name is Kim") never suppresses the co-recalled clean facts.
    clean, conflicted = _partition_conflicts(verified)
    rejected.extend(
        _withheld_candidate_receipt(
            candidate,
            status="conflict",
            reason="verified candidate withheld because its canonical claim conflicts with another value",
        )
        for candidate in conflicted
    )
    status, reason = _classify_verdict(query, clean, rejected, had_conflict=bool(conflicted))
    if status == "verified":
        answer_payload = {
            "candidates": tuple(clean),
            "meta": dict(meta or {}),
        }
        evidence = tuple(
            {
                "memory_id": item["memory_id"],
                "path": item["path"],
                "coord": list(item.get("coord") or []),
                "title": item.get("title") or "",
                "score": float(item.get("score") or 0.0),
                "sense_ids": list(item.get("sense_ids") or []),
                **(
                    {"workspace_source": dict(item["workspace_source"])}
                    if isinstance(item.get("workspace_source"), Mapping)
                    else {}
                ),
                # Cut 3: routing proof id (only when the row carried one) —
                # sits beside source proof, never inside it
                **(
                    {"recall_basis_id": str(item.get("recall_basis_id"))}
                    if item.get("recall_basis_id")
                    else {}
                ),
                **dict(item.get("source_proof") or {"evidence_strength": "rederive"}),
            }
            for item in clean
        )
    else:
        answer_payload = None
        evidence = ()

    admitted_count = len(clean) if status == "verified" else 0
    return RecallVerdict(
        status=status,
        query=query,
        answer_payload=answer_payload,
        evidence=evidence,
        rejected_hits=tuple(rejected),
        reason=reason,
        blocked_conflicts=tuple(conflicted),
        retrieval_trace=_with_candidate_accounting(
            retrieval_trace,
            retrieved_count=len(candidates),
            admitted_count=admitted_count,
            rejected_count=len(rejected),
        ),
    )


def _withheld_candidate_receipt(
    candidate: Mapping[str, Any], *, status: str, reason: str
) -> dict[str, str]:
    """Account for verified candidates deliberately withheld from an answer.

    ``blocked_conflicts`` remains the detailed conflict payload; these compact
    receipts make the candidate-level retrieved/admitted/rejected count complete.
    """
    return {
        "memory_id": str(candidate.get("memory_id") or ""),
        "status": status,
        "reason": reason,
    }


def _classify_verdict(
    query: str,
    clean: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    *,
    had_conflict: bool = False,
) -> tuple[str, str]:
    # ``clean`` is the verified set with contradicting anchors already partitioned out.
    if not clean:
        # nothing clean survives: if the only thing recalled WAS a contradiction, surface
        # the conflict (drives the memory_reconciliation pressure signal); else fall to the
        # rejected status / honest unknown. (Ambiguity is resolved by the caller before
        # partitioning — a sense split is not a conflict.)
        if had_conflict:
            return "conflict", "verified canonical memories disagree on the same anchor"
        top = _highest_rejected_status(rejected)
        if top:
            return top, _reason_for_status(top, rejected)
        return "unknown", "no canonical memory matched the query"

    return "verified", f"verified {len(clean)} canonical hit(s) against live files"


def _highest_rejected_status(rejected: list[dict[str, Any]]) -> str:
    if not rejected:
        return ""
    statuses = [str(item.get("status") or "unknown") for item in rejected]
    return sorted(statuses, key=lambda s: _STATUS_PRIORITY.get(s, -1), reverse=True)[0]


def _reason_for_status(status: str, rejected: list[dict[str, Any]]) -> str:
    for item in rejected:
        if str(item.get("status") or "") == status:
            return str(item.get("reason") or status)
    return status


def _verify_candidate(
    cand: dict[str, Any],
    coord_index: dict[str, Any],
    *,
    root: Path | str | None = None,
    include_historical_sources: bool = False,
) -> dict[str, Any]:
    memory_id = str(cand.get("memory_id") or "")
    if not memory_id:
        return {
            "verified": False,
            "rejected": {
                "memory_id": "",
                "status": "unknown",
                "reason": "candidate missing memory_id",
            },
        }

    entry = coord_index.get(memory_id)
    filesystem_fallback = cand.get("filesystem_fallback") is True
    if not isinstance(entry, dict) and not filesystem_fallback:
        return {
            "verified": False,
            "rejected": {
                "memory_id": memory_id,
                "status": "unknown",
                "reason": "coord_index entry missing",
            },
        }

    entry = entry if isinstance(entry, dict) else {}
    path = _resolve_path(entry.get("path"), root=root)
    if path is None and filesystem_fallback:
        path = _resolve_path(cand.get("path"), root=root)
        if path is not None:
            try:
                path.resolve().relative_to(_memory_root(root).resolve())
            except (OSError, ValueError):
                path = None
    if path is None or not path.exists():
        return {
            "verified": False,
            "rejected": {
                "memory_id": memory_id,
                "status": "unknown",
                "reason": "canonical file missing",
            },
        }

    record = _read_json(path)
    if not isinstance(record, dict):
        return {
            "verified": False,
            "rejected": {
                "memory_id": memory_id,
                "status": "unknown",
                "reason": "canonical file unreadable",
            },
        }

    if filesystem_fallback and str(record.get("id") or "") != memory_id:
        return {
            "verified": False,
            "rejected": {
                "memory_id": memory_id,
                "status": "unknown",
                "reason": "canonical file id mismatch",
            },
        }
    if filesystem_fallback and str(record.get("id") or "") != memory_id:
        return {
            "verified": False,
            "rejected": {
                "memory_id": memory_id,
                "status": "unknown",
                "reason": "filesystem candidate id does not match canonical file",
            },
        }

    title = str(record.get("title") or "")
    summary = str(record.get("summary") or "")
    text = str(record.get("text") or "")
    coord = list(record.get("coord") or entry.get("coord") or cand.get("coord") or [])
    domain = record.get("domain")
    if not title and not summary and not text:
        return {
            "verified": False,
            "rejected": {
                "memory_id": memory_id,
                "status": "unknown",
                "reason": "canonical file has no comparable content",
            },
        }

    label_ok = _matches_text(str(cand.get("title") or ""), (title, summary, text))
    summary_ok = _matches_text(str(cand.get("summary") or ""), (summary, text, title))

    provenance = record.get("provenance") if isinstance(record.get("provenance"), Mapping) else {}
    source_id = str(provenance.get("source_id") or "") if isinstance(provenance, Mapping) else ""

    if source_id:
        # Cited path: the SourceLedger crypto proof is AUTHORITATIVE. A drifted
        # graph label does not reject — the proof attests the file content, and the
        # verifier surfaces the on-file truth below. Projection receipts further
        # bind the current canonical claim fields to that source citation.
        source_proof = _verify_source_proof(record, (title, summary, text), root=root)
        if source_proof.get("status") != "verified":
            return {
                "verified": False,
                "rejected": {
                    "memory_id": memory_id,
                    "status": source_proof.get("status") or "unknown",
                    "reason": source_proof.get("reason") or "source ledger crypto proof failed",
                },
            }
        receipt_error = _projection_receipt_error(record, root=root)
        if receipt_error:
            return {
                "verified": False,
                "rejected": {
                    "memory_id": memory_id,
                    "status": "unknown",
                    "reason": receipt_error,
                },
            }
        source_validity_error = _workspace_source_validity_error(
            record,
            root=root,
            include_historical_sources=include_historical_sources,
        )
        if source_validity_error:
            return {
                "verified": False,
                "rejected": {
                    "memory_id": memory_id,
                    "status": "unknown",
                    "reason": source_validity_error,
                },
            }
        verified = dict(cand)
        verified["path"] = str(path)
        verified["coord"] = coord
        verified["title"] = title or str(cand.get("title") or "")
        verified["summary"] = summary or str(cand.get("summary") or "")
        verified["text"] = text
        verified["domain"] = domain or cand.get("domain")
        verified["subtopic"] = record.get("subtopic") or cand.get("subtopic")
        verified["tags"] = list(record.get("tags") or cand.get("tags") or [])
        verified["contradiction"] = record.get("contradiction") or record.get("contradiction_state")
        flags = record.get("flags") if isinstance(record.get("flags"), Mapping) else {}
        verified["flagged"] = bool(flags.get("flagged") or record.get("flagged"))
        verified["flag_reason"] = flags.get("flag_reason") or record.get("flag_reason")
        verified["flag_response_mode"] = flags.get("flag_response_mode") or record.get(
            "flag_response_mode"
        )
        verified["type"] = str(record.get("type") or "")
        verified["sense_ids"] = _record_sense_ids(record)
        verified["relation_facets"] = _record_relation_facets(record)
        verified["source_proof"] = dict(source_proof.get("proof") or {})
        workspace_source = record.get("workspace_source")
        if isinstance(workspace_source, Mapping):
            verified["workspace_source"] = dict(workspace_source)
        return {"verified": True, "candidate": verified}

    # Uncited path: graph-drift guard, then FAIL CLOSED. _verify_source_proof
    # returns "unknown" for a record with no source citation (2026-07-21) — a bare
    # file-match (rederive) is no longer accepted as verification.
    if not label_ok:
        return {
            "verified": False,
            "rejected": {
                "memory_id": memory_id,
                "status": "unknown",
                "reason": "graph label no longer matches canonical file",
            },
        }
    if cand.get("summary") and not summary_ok:
        return {
            "verified": False,
            "rejected": {
                "memory_id": memory_id,
                "status": "unknown",
                "reason": "graph summary no longer matches canonical file",
            },
        }
    source_proof = _verify_source_proof(record, (title, summary, text), root=root)
    if source_proof.get("status") != "verified":
        return {
            "verified": False,
            "rejected": {
                "memory_id": memory_id,
                "status": source_proof.get("status") or "unknown",
                "reason": source_proof.get("reason") or "source ledger proof failed",
            },
        }
    source_validity_error = _workspace_source_validity_error(
        record,
        root=root,
        include_historical_sources=include_historical_sources,
    )
    if source_validity_error:
        return {
            "verified": False,
            "rejected": {
                "memory_id": memory_id,
                "status": "unknown",
                "reason": source_validity_error,
            },
        }

    verified = dict(cand)
    verified["path"] = str(path)
    verified["coord"] = coord
    verified["title"] = title or str(cand.get("title") or "")
    verified["summary"] = summary or str(cand.get("summary") or "")
    verified["text"] = text
    verified["domain"] = domain or cand.get("domain")
    verified["subtopic"] = record.get("subtopic") or cand.get("subtopic")
    verified["tags"] = list(record.get("tags") or cand.get("tags") or [])
    verified["contradiction"] = record.get("contradiction") or record.get("contradiction_state")
    flags = record.get("flags") if isinstance(record.get("flags"), Mapping) else {}
    verified["flagged"] = bool(flags.get("flagged") or record.get("flagged"))
    verified["flag_reason"] = flags.get("flag_reason") or record.get("flag_reason")
    verified["flag_response_mode"] = flags.get("flag_response_mode") or record.get(
        "flag_response_mode"
    )
    verified["type"] = str(record.get("type") or "")
    verified["sense_ids"] = _record_sense_ids(record)
    verified["relation_facets"] = _record_relation_facets(record)
    verified["source_proof"] = dict(source_proof.get("proof") or {})
    workspace_source = record.get("workspace_source")
    if isinstance(workspace_source, Mapping):
        verified["workspace_source"] = dict(workspace_source)
    return {"verified": True, "candidate": verified}


def _record_sense_ids(record: Mapping[str, Any]) -> list[str]:
    enrichment = record.get("enrichment") if isinstance(record, Mapping) else {}
    if not isinstance(enrichment, Mapping):
        enrichment = {}
    raw = enrichment.get("sense_ids") or record.get("sense_ids") or ()
    if isinstance(raw, str):
        raw = (raw,)
    return [str(item) for item in raw or () if item]


def _record_relation_facets(record: Mapping[str, Any]) -> list[dict[str, str]]:
    """On-file relation facets for a record — the FS truth the planner narrates and the
    subject-strip filters on. Reads enrichment.entities, the SAME parsed relations
    extract_persona projects into graph edges, so a verified fact and its graph edge share
    one source. Each facet: {subject, facet, relation, object}. subject defaults to
    'operator' (a self-fact); a third-person fact ("Abel likes trains") carries subject=abel.
    Empty for non-relational records (the common case)."""
    enrichment = record.get("enrichment") if isinstance(record, Mapping) else {}
    if not isinstance(enrichment, Mapping):
        return []
    items = enrichment.get("entities")
    if not isinstance(items, list):
        return []
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        facet = str(item.get("facet") or "").strip().lower()
        if not facet:
            continue
        subject = str(item.get("subject") or "operator").strip().lower()
        # relationships name the person in linked_name; other facets in name (mirrors
        # extract_persona._FACET_EDGE object resolution so edge and facet agree).
        raw_object = item.get("linked_name") if facet == "relationship" else item.get("name")
        obj = str(raw_object or "").strip().lower()
        # the SPECIFIC relation the planner narrates: "son" for a relationship, else the facet.
        relation = (
            str(item.get("name") or facet).strip().lower() if facet == "relationship" else facet
        )
        out.append({"subject": subject, "facet": facet, "relation": relation, "object": obj})
    return out


def _filter_verified_by_query_sense(
    query: str,
    verified: list[dict[str, Any]],
    *,
    root: Path | str | None,
) -> list[dict[str, Any]]:
    if len(verified) < 2:
        return verified
    try:
        from memory.sense_registry import POLYSEMOUS_TERMS, resolve_query_senses

        query_senses = resolve_query_senses(query, root=root)
    except Exception:
        return verified
    if not query_senses:
        return verified
    tokens = set(_query_tokens(query))
    sense_terms = {str(row.get("term") or "") for row in query_senses}
    if not (tokens - sense_terms):
        return verified
    if not (sense_terms & POLYSEMOUS_TERMS):
        return verified
    resolved_ids = {str(row.get("sense_id") or "") for row in query_senses}
    matching = [
        item
        for item in verified
        if set(str(sid) for sid in item.get("sense_ids") or ()) & resolved_ids
    ]
    if matching:
        return matching
    if tokens - sense_terms:
        return []
    return verified


def _verify_source_proof(
    record: Mapping[str, Any],
    comparable_text: Iterable[str],
    *,
    root: Path | str | None = None,
) -> dict[str, Any]:
    provenance = record.get("provenance") if isinstance(record, Mapping) else {}
    if not isinstance(provenance, Mapping):
        provenance = {}
    source_id = str(provenance.get("source_id") or "")
    if not source_id:
        # Fail closed (operator 2026-07-21 — ledger-or-I-don't-know): a canonical
        # record with NO source citation is UNPROVENANCED. It may exist on disk,
        # but "verified" is reserved for ledger-crypto-proven provenance; a bare
        # file-match (rederive) is NOT verification. Every legitimate write mints a
        # citation — create_staging → ensure_source_citation, seeds → record_source
        # — so an uncited record is unprovenanced, surfaced as a memory gap, never
        # asserted to cognition as verified fact.
        return {
            "status": "unknown",
            "reason": "unprovenanced — no source citation (rederive is not accepted as verified)",
        }

    try:
        from memory.source_ledger.store import read_source
        from memory.taxonomy.hashing import span_hash_for
    except Exception:
        return {"status": "unknown", "reason": "source ledger unavailable"}

    artifact = read_source(source_id, root=root)
    if artifact is None:
        return {"status": "unknown", "reason": "source ledger artifact missing"}

    if hashlib.sha256(artifact.content.encode("utf-8")).hexdigest() != artifact.content_hash:
        return {"status": "conflict", "reason": "source ledger stored content hash mismatch"}

    cited_span_hash = str(provenance.get("span_hash") or "")
    if not cited_span_hash:
        return {"status": "unknown", "reason": "canonical record missing span_hash citation"}

    cited_content_hash = str(provenance.get("source_content_hash") or "")
    if cited_content_hash and cited_content_hash != artifact.content_hash:
        return {"status": "conflict", "reason": "source ledger content hash mismatch"}

    if cited_span_hash != artifact.span_hash:
        return {"status": "conflict", "reason": "source ledger span hash mismatch"}

    span_text = artifact.content[artifact.span_start : artifact.span_end]
    if span_hash_for(span_text) != artifact.span_hash:
        return {
            "status": "conflict",
            "reason": "source ledger span hash does not match span content",
        }

    return {
        "status": "verified",
        "proof": {
            "evidence_strength": "source_ledger_crypto",
            "source_id": artifact.source_id,
            "source_type": artifact.source_type,
            "source_session_id": artifact.source_session_id,
            "source_session_timestamp": artifact.source_session_timestamp,
            "span_start": artifact.span_start,
            "span_end": artifact.span_end,
            "span_hash": artifact.span_hash,
            "source_content_hash": artifact.content_hash,
        },
    }


def _verify_projection_receipt(
    record: Mapping[str, Any],
    *,
    root: Path | str | None = None,
) -> dict[str, Any]:
    try:
        from memory.projection_receipt import verify_projection_receipt
    except Exception:
        return {"status": "unknown", "reason": "projection receipt module unavailable"}

    # Prefer the explicit root; otherwise use this module's OMEGA_ROOT so tests
    # (and alternate profile bindings) stay consistent with coord_index lookup.
    receipt_root = Path(root) if root is not None else OMEGA_ROOT
    result = verify_projection_receipt(record, root=receipt_root)
    if result.ok:
        return {"status": "verified", "reason": "ok"}

    conflict_reasons = {"canonical_claim_hash_mismatch", "source_mismatch"}
    status = "conflict" if result.reason in conflict_reasons else "unknown"
    return {"status": status, "reason": result.reason}


def _matches_text(fragment: str, haystacks: Iterable[str]) -> bool:
    needle = _normalize(fragment)
    if not needle:
        return True
    return any(needle in _normalize(text) for text in haystacks if text)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _query_tokens(query: str) -> list[str]:
    return [tok for tok in re.findall(r"[a-z0-9_]+", _normalize(query)) if len(tok) >= 2]


def _content_query_tokens(query: str) -> set[str]:
    """Stopword-stripped query tokens — the topical core the gate matches on.
    Single stopword source: the recall engine's list (drifting a second copy
    here would let the verdict and the hop gate disagree on topicality)."""
    from memory.taxonomy.recall import _STOPWORDS

    return set(_query_tokens(query)) - _STOPWORDS


def _with_morphological_forms(tokens: Iterable[str]) -> set[str]:
    """Expand tokens to their base forms so the gate matches what the scorer did.

    Same single-source discipline as ``_STOPWORDS`` above, for the same reason:
    the scorer stems both sides, so comparing raw surface forms here made the two
    organs disagree about what a word IS — "specialists" could not match
    "specialist", and rows the scorer ranked 3rd were demoted to strict unknown.

    Applied only inside the topicality comparison, never to ``_query_tokens``
    itself: ``_is_ambiguous_query`` counts tokens to detect a single-term query,
    and expanding there would make every inflected word look like two terms.
    """
    from memory.taxonomy.recall import morphological_forms

    return {form for token in tokens for form in morphological_forms(token)}


def _non_recallable_region_reason(query: str, allowed_lobes: Iterable[int] | None) -> str:
    """Rule-1 refusal reason, or '' when the query's region is recallable.

    Caller routing wins: explicit allowed_lobes ARE the region (refuse only
    when every allowed lobe is non-recallable — deny-listing is the recall
    engine's job). Unscoped queries refuse ONLY on the router's high-precision
    journal/vision INTENT regexes (route_names_non_recallable_lobe) — never
    the greedy grammar-probe dispatch fallback, whose single-generic-word
    matches ("patterns", "screen", "photo" all probe to lobe 7) would veto
    unrelated recalls outright (review finding 2026-07-18). The named lobe is
    used ONLY for this refusal, never to demote in-region-adjacent candidates:
    via_link cross-lobe chains are a designed feature of the recall engine."""
    from memory.dewey_map import NON_RECALLABLE_LOBES

    if allowed_lobes is not None:
        lobes = {int(x) for x in allowed_lobes}
        if lobes and lobes <= NON_RECALLABLE_LOBES:
            return (
                f"query region {sorted(lobes)} is structurally non-recallable "
                "(journal is metacognitive state; vision has a dedicated pipeline)"
            )
        return ""

    from auto_loop.mril_tools.router import route_names_non_recallable_lobe

    lobe = route_names_non_recallable_lobe(query)
    if lobe is not None and lobe in NON_RECALLABLE_LOBES:
        return (
            f"query routes to structurally non-recallable lobe {lobe} "
            "(journal is metacognitive state; vision has a dedicated pipeline)"
        )
    return ""


def _validated_basis_ids(
    verified: list[dict[str, Any]],
    meta: Mapping[str, Any] | None,
    *,
    root: Path | str | None = None,
    query: str = "",
    allowed_lobes: Iterable[int] | None = None,
    denied_lobes: Iterable[int] | None = None,
    query_spec: Any = None,
) -> set[str]:
    """Cut 3: independently revalidate the RecallBasis each VERIFIED candidate
    names, from UNTRUSTED metadata rows. Runs after canonical/source
    verification; returns the basis ids that hold for their candidate. Never
    raises — malformed/forged/stale rows are simply not validated.

    Execution binding (CUT3_REVIEW_REWORK.md / _2.md): the executed
    query-coordinate set is taken from the run's DmeExecutionBinding — created
    by the actual DME execution, independent of the diagnostic trace — after
    validating it against a deterministic replay (raw tokens + recompiled plan
    cues under the exact two production lobe policies): everything replayable
    must be present, and any extra must be a generated address that was really
    applied through the receipt the binding names. Never the basis's own
    claims; never a bare receipt. Every basis must draw its addresses from it."""
    try:
        from memory.recall_basis import RecallBasis, bind_executed_addresses, verify_recall_basis
    except Exception:  # noqa: BLE001
        return set()
    rows = (meta or {}).get("recall_bases") if isinstance(meta, Mapping) else None
    if not isinstance(rows, (list, tuple)) or not rows:
        return set()
    by_id: dict[str, RecallBasis] = {}
    for raw in rows:
        basis = RecallBasis.from_dict(raw)
        if basis is not None and basis.basis_id:
            by_id.setdefault(basis.basis_id, basis)
    if not by_id:
        return set()
    cue_receipt = (meta or {}).get("routing_cues") if isinstance(meta, Mapping) else None
    cue_receipt = cue_receipt if isinstance(cue_receipt, Mapping) else None
    try:
        executed, reason = bind_executed_addresses(
            meta,
            query=query,
            root=root,
            allowed_lobes=allowed_lobes,
            denied_lobes=denied_lobes,
            query_spec=query_spec,
        )
    except Exception as exc:  # noqa: BLE001 — cannot bind → no basis validates (fail closed)
        log.warning("recall_verifier: execution binding failed (%s)", exc)
        return set()
    if not executed:
        log.debug("recall_verifier: no executed set (%s)", reason)
        return set()
    validated: set[str] = set()
    for item in verified:
        basis_id = str(item.get("recall_basis_id") or "")
        basis = by_id.get(basis_id)
        if basis is None:
            continue
        try:
            outcome = verify_recall_basis(
                basis,
                candidate_id=str(item.get("memory_id") or ""),
                root=root,
                cue_receipt=cue_receipt,
                executed_addresses=executed,
            )
        except Exception:  # noqa: BLE001
            continue
        if outcome.valid:
            validated.add(basis_id)
    return validated


def _partition_topicality(
    query: str,
    verified: list[dict[str, Any]],
    *,
    validated_basis_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split verified hits into (topical, demoted-rejection-rows).

    Topical = shares at least one content token with the query across the
    CANONICAL content (title/summary/text/tags — FS truth, not graph labels).
    Zero overlap demotes to strict unknown. Contentless queries (all
    stopwords) do not gate — mirrors the recall engine's hop-gate fallback.

    Cut 3 (memory/recall_basis.py): a row whose ``recall_basis_id`` is in
    ``validated_basis_ids`` — a routing proof the verifier itself just
    revalidated against the live projection/graph — is topical without a
    lexical token. Every other rule is unchanged; an empty/None set is the
    exact pre-Cut-3 behaviour."""
    basis_ids = validated_basis_ids or set()
    from memory.canonical_recall import expand_query_with_category_aliases

    expanded_query = expand_query_with_category_aliases(query)
    content = _with_morphological_forms(_content_query_tokens(expanded_query))
    specific_query_concepts = {
        token for token in content if token.startswith("concept_") and token != "concept_family"
    }
    if not content:
        return list(verified), []
    topical: list[dict[str, Any]] = []
    atopical: list[dict[str, Any]] = []
    for item in verified:
        # A row the semantic/relational recall lane surfaced was already vouched
        # for by an organ that reads MEANING, and it routinely shares no content
        # token with the query -- that is precisely why that lane exists. "How
        # many different doctors did I visit?" has to reach a turn saying
        # "diagnosed by an ENT specialist", which a lexical overlap test rejects.
        # Measured over a ten-case gate this demoted 43-108 rows per case and cut
        # one case to 20 rows of evidence. Authenticity is this verifier's job;
        # relevance belongs to the scorer that ranked them.
        if item.get("semantic_recall") is True:
            topical.append(item)
            continue
        if basis_ids and str(item.get("recall_basis_id") or "") in basis_ids:
            # the mark downstream consumers (MRIL relevance floor) may trust:
            # set ONLY here, by the verifier, after its own revalidation
            item["recall_basis_validated"] = True
            topical.append(item)
            continue
        blob_tokens: set[str] = set()
        canonical_text: list[str] = []
        for text in (item.get("title"), item.get("summary"), item.get("text")):
            value = str(text or "")
            canonical_text.append(value)
            expanded_value = expand_query_with_category_aliases(value)
            blob_tokens.update(_with_morphological_forms(_query_tokens(expanded_value)))
        for tag in item.get("tags") or ():
            expanded_tag = expand_query_with_category_aliases(str(tag))
            blob_tokens.update(_with_morphological_forms(_query_tokens(expanded_tag)))
        blob_tokens.update(derived_recall_features(" ".join(canonical_text)))
        blob_specific_concepts = {
            token
            for token in blob_tokens
            if token.startswith("concept_") and token != "concept_family"
        }
        precise_concept_match = not specific_query_concepts or bool(
            specific_query_concepts & blob_specific_concepts
        )
        if precise_concept_match and content & blob_tokens:
            topical.append(item)
        else:
            atopical.append(
                {
                    "memory_id": str(item.get("memory_id") or ""),
                    "status": "unknown",
                    "reason": "no topical overlap with the query (strict unknown)",
                }
            )
    return topical, atopical


def _is_ambiguous_query(query: str, verified: list[dict[str, Any]]) -> bool:
    tokens = _query_tokens(query)
    if len(tokens) != 1 or len(verified) < 2:
        return False
    sense_ids = {str(sid) for item in verified for sid in (item.get("sense_ids") or ()) if sid}
    if len(sense_ids) > 1:
        return True
    senses = {
        (
            tuple(item.get("coord")[:2]) if item.get("coord") else (),
            str(item.get("domain") or ""),
            _normalize(str(item.get("title") or "")),
        )
        for item in verified
    }
    return len(senses) > 1


# Fact-STREAM record types: many records per topic sharing one title label is
# their normal shape (the learn cascade titles every fact "Semantic — <domain>",
# every mastery tick "Mastery — <domain>"; temporal records stream per episode).
# Same-title/different-summary is COMPLEMENTARY there, not disagreement — without
# this exemption, learning a second fact about any topic poisoned every recall of
# that topic to CONFLICT (observed live 2026-07-10: 9 python semantic facts →
# chat recall gated empty). Singular-claim records (concept, identity_fact,
# user_profile, …) keep the conflict rule: one anchor, one truth.
_FACT_STREAM_TYPES = frozenset(
    {
        "semantic_fact",
        "mastery_progress",
        "episode",
        "consolidation",
        "capability",
        "situational",
        "resolved_inquiry",
    }
)


def _conflict_titles(verified: list[dict[str, Any]]) -> set[str]:
    """Normalized titles of SINGULAR-claim records carrying >1 distinct summary — the same
    anchor with disagreeing values. Fact-stream types (many facts per title label) are
    exempt: same-title/different-summary is complementary there, not disagreement."""
    by_title: dict[str, set[str]] = {}
    for item in verified:
        if str(item.get("type") or "") in _FACT_STREAM_TYPES:
            continue
        title = _normalize(str(item.get("title") or ""))
        summary = _normalize(str(item.get("summary") or item.get("text") or ""))
        if not title or not summary:
            continue
        by_title.setdefault(title, set()).add(summary)
    return {title for title, summaries in by_title.items() if len(summaries) > 1}


def _partition_conflicts(
    verified: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split verified into (clean, conflicted). Both sides of every conflicting anchor are
    quarantined into ``conflicted``; unrelated facts stay ``clean``, so a single
    contradiction ("operator name is Kim") never poisons the rest of a co-recall."""
    titles = _conflict_titles(verified)
    if not titles:
        return list(verified), []
    clean: list[dict[str, Any]] = []
    conflicted: list[dict[str, Any]] = []
    for item in verified:
        is_stream = str(item.get("type") or "") in _FACT_STREAM_TYPES
        title = _normalize(str(item.get("title") or ""))
        if title in titles and not is_stream:
            conflicted.append(item)
        else:
            clean.append(item)
    return clean, conflicted


def _load_coord_index(root: Path | str | None) -> dict[str, Any]:
    index_path = _memory_root(root) / "indexes" / "coord_index.json"
    if not index_path.exists():
        return {}
    data = _read_json(index_path)
    return data if isinstance(data, dict) else {}


def _memory_root(root: Path | str | None) -> Path:
    if root is not None:
        root_path = Path(root)
        return root_path / "memory" if root_path.name != "memory" else root_path
    try:
        from memory.engine import validate_write

        return Path(validate_write.MEMORY_ROOT)
    except Exception:
        return OMEGA_ROOT / "memory"


def _resolve_path(raw_path: Any, *, root: Path | str | None = None) -> Path | None:
    if not raw_path:
        return None
    path = Path(str(raw_path))
    if path.is_absolute():
        return path
    # Prefer profile root so profile-relative ``memory/...`` entries resolve.
    # Callers sometimes pass MEMORY_ROOT; strip a trailing ``memory`` segment.
    # root=None derives the profile from _memory_root() (which follows the
    # patchable validate_write.MEMORY_ROOT), never the import-time constant.
    base = Path(root) if root is not None else _memory_root(None)
    if base.name == "memory":
        base = base.parent
    candidate = base / path
    if candidate.exists():
        return candidate
    # Legacy memory-relative paths (no ``memory/`` prefix).
    mem_base = base / "memory" if base.name != "memory" else base
    alt = mem_base / path
    return alt if alt.exists() else candidate


def _projection_receipt_error(
    record: Mapping[str, Any],
    *,
    root: Path | str | None = None,
) -> str:
    provenance = record.get("provenance")
    if not isinstance(provenance, Mapping) or not provenance.get("projection_receipt_required"):
        return ""
    try:
        from memory.projection_receipt import verify_projection_receipt

        verdict = verify_projection_receipt(record, root=root)
    except Exception as exc:
        return f"projection receipt verification failed: {type(exc).__name__}"
    if verdict.ok:
        return ""
    return f"projection receipt verification failed: {verdict.reason}"


def _workspace_source_validity_error(
    record: Mapping[str, Any],
    *,
    root: Path | str | None = None,
    include_historical_sources: bool = False,
) -> str:
    workspace_source = record.get("workspace_source")
    if not isinstance(workspace_source, Mapping):
        return ""
    from memory.source_validity import SourceValidityError, verify_workspace_source

    try:
        verdict = verify_workspace_source(
            workspace_source,
            root=root,
            include_historical_sources=include_historical_sources,
        )
    except SourceValidityError as exc:
        return f"workspace source validity unavailable: {exc}"
    if verdict.get("ok") is True:
        return ""
    return str(verdict.get("reason") or "workspace source proof missing")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
