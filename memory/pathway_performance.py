"""Canonical PathwayOutcome writes and a disposable bounded projection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from memory.api import CanonicalEnvelope, WriteReceipt, intake_memory, intake_receipt_accepted
from core.symphony_time import now_iso
from pressure_loop.durable_write import write_text_atomic
from schema.pathway_outcome import PathwayOutcome


PATHWAY_OUTCOME_COORD = [4, 6, 1]
PATHWAY_PROJECTION_PATH = (
    Path(__file__).resolve().parent.parent / "deepsleep_loop" / "state" / "pathway_performance.json"
)
PATHWAY_RECENT_CAP = 40


def _clamp01(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _decoded_text(value: Mapping[str, Any]) -> dict[str, Any]:
    text = value.get("text")
    if not isinstance(text, str):
        return {}
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _outcome_row(value: Any) -> dict[str, Any] | None:
    if isinstance(value, PathwayOutcome):
        return value.to_dict()
    if not isinstance(value, Mapping):
        return None
    wrapper = dict(value)
    candidate: Any = wrapper.get("pathway_outcome")
    enrichment = wrapper.get("enrichment")
    if candidate is None and isinstance(enrichment, Mapping):
        candidate = enrichment.get("pathway_outcome")
    assessment = wrapper.get("assessment")
    if candidate is None and isinstance(assessment, Mapping):
        candidate = assessment.get("pathway_outcome")
    if candidate is None:
        candidate = _decoded_text(wrapper) if wrapper.get("type") == "pathway_outcome" else wrapper
    if not isinstance(candidate, Mapping):
        return None
    try:
        row = PathwayOutcome.from_dict(candidate).to_dict()
    except (TypeError, ValueError):
        return None
    for key in ("episode_id", "outcome_id", "expected_success"):
        if key in wrapper:
            row[key] = wrapper[key]
    return row


def _assessment_row(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    wrapper = dict(value)
    if wrapper.get("type") != "motor_attempt_assessment":
        return None
    payload = _decoded_text(wrapper)
    if not payload:
        payload = wrapper
    nested = payload.get("motor_attempt_assessment")
    if isinstance(nested, Mapping):
        payload = {**payload, **dict(nested)}
    assessment = payload.get("assessment")
    if isinstance(assessment, Mapping):
        payload = {**payload, **dict(assessment)}
    enrichment = wrapper.get("enrichment")
    if isinstance(enrichment, Mapping):
        for key in ("program_id", "episode_id", "outcome_id"):
            payload.setdefault(key, enrichment.get(key))
    if not str(payload.get("program_id") or ""):
        return None
    return payload


def _window_update_row(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    wrapper = dict(value)
    candidate: Any = wrapper.get("window_pathway_update")
    enrichment = wrapper.get("enrichment")
    if candidate is None and isinstance(enrichment, Mapping):
        candidate = enrichment.get("window_pathway_update")
    if candidate is None and wrapper.get("type") == "pathway_window_update":
        candidate = _decoded_text(wrapper) or wrapper
    if candidate is None and {"window_id", "pathway_id", "decision"} <= wrapper.keys():
        candidate = wrapper
    if not isinstance(candidate, Mapping):
        return None
    window_id = str(candidate.get("window_id") or "").strip()
    pathway_id = str(candidate.get("pathway_id") or "").strip()
    decision = str(candidate.get("decision") or "").strip().lower()
    evidence = candidate.get("evidence_program_ids")
    raw_order = candidate.get("window_order")
    if not isinstance(raw_order, Mapping):
        return None
    order_kind = str(raw_order.get("kind") or "").strip().lower()
    order_value: Any = raw_order.get("value")
    if order_kind == "ordinal":
        try:
            order_value = int(order_value)
        except (TypeError, ValueError):
            return None
        if order_value < 0:
            return None
    elif order_kind == "timestamp":
        order_value = str(order_value or "").strip()
        try:
            datetime.fromisoformat(order_value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    source_tie_break = str(candidate.get("source_tie_break") or "").strip()
    if (
        not window_id
        or not pathway_id
        or decision not in {"retain", "revise", "combine", "retire"}
        or not isinstance(evidence, list)
        or any(not isinstance(value, str) or not value.strip() for value in evidence)
        or not source_tie_break
    ):
        return None
    timestamp = str(
        candidate.get("timestamp")
        or wrapper.get("timestamp_created")
        or wrapper.get("source_session_timestamp")
        or ""
    )
    return {
        "window_id": window_id,
        "pathway_id": pathway_id,
        "decision": decision,
        "reason": str(candidate.get("reason") or "").strip(),
        "candidate_method": str(candidate.get("candidate_method") or "").strip(),
        "evidence_program_ids": list(dict.fromkeys(item.strip() for item in evidence)),
        "timestamp": timestamp,
        "window_order": {"kind": order_kind, "value": order_value},
        "source_tie_break": source_tie_break,
    }


def _window_update_content(row: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            key: row.get(key)
            for key in (
                "window_id",
                "pathway_id",
                "decision",
                "reason",
                "candidate_method",
                "evidence_program_ids",
                "window_order",
                "source_tie_break",
            )
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _window_update_order(row: Mapping[str, Any]) -> tuple[Any, ...]:
    order = row["window_order"]
    kind = str(order["kind"])
    return (
        0 if kind == "ordinal" else 1,
        order["value"],
        str(row["window_id"]),
        str(row["pathway_id"]),
        str(row["source_tie_break"]),
    )


def resolve_canonical_pathway_window_updates(
    canonical_rows: Sequence[Any],
) -> list[dict[str, Any]]:
    """Resolve immutable canonical decisions without reading projection bytes."""
    resolved: dict[tuple[str, str], dict[str, Any]] = {}
    for value in canonical_rows:
        update = _window_update_row(value)
        if update is None:
            continue
        identity = (update["window_id"], update["pathway_id"])
        prior = resolved.get(identity)
        if prior is not None and _window_update_content(prior) != _window_update_content(update):
            raise ValueError(
                f"conflicting canonical pathway updates for window {identity[0]!r} "
                f"and pathway {identity[1]!r}"
            )
        resolved[identity] = update
    return sorted(resolved.values(), key=_window_update_order)


def load_canonical_pathway_window_updates() -> list[dict[str, Any]]:
    """Read-only canonical `[3,3,0]` pathway decision view."""
    return resolve_canonical_pathway_window_updates(_load_canonical_pathway_rows())


def _identity_key(row: Mapping[str, Any]) -> str:
    return str(row.get("program_id") or "")


def _load_canonical_pathway_rows() -> list[dict[str, Any]]:
    """Read canonical attempt/outcome records through the live coord index."""
    from memory.engine.walker import (
        coord_index_snapshot,
        load_coord_index,
        load_meta_by_path,
    )

    load_coord_index()
    rows: list[dict[str, Any]] = []
    for _memory_id, entry in sorted(coord_index_snapshot().items()):
        if not isinstance(entry, Mapping):
            continue
        record_type = str(entry.get("type") or "")
        coord = list(entry.get("coord") or ())
        if record_type == "pathway_window_update":
            if coord[:3] != [3, 3, 0]:
                continue
        elif record_type not in {"motor_attempt_assessment", "pathway_outcome"} and (
            coord[:2] != [4, 6]
        ):
            continue
        path = str(entry.get("path") or "")
        if not path:
            continue
        record = load_meta_by_path(path)
        if isinstance(record, Mapping) and str(record.get("type") or record_type) in {
            "motor_attempt_assessment",
            "pathway_outcome",
            "pathway_window_update",
        }:
            rows.append(dict(record))
    return rows


def _expected_success(row: Mapping[str, Any]) -> float:
    if "expected_success" in row:
        return _clamp01(row.get("expected_success"))
    response = row.get("neuromodulator_response")
    dopamine = float(response.get("dopamine") or 0.0) if isinstance(response, Mapping) else 0.0
    actual = 1.0 if row.get("verification_verdict") == "verified" else 0.0
    return _clamp01(actual - dopamine)


def rebuild_pathway_projection(
    canonical_rows: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Deterministically replace the projection from canonical assessment rows."""
    sources = list(canonical_rows) if canonical_rows is not None else _load_canonical_pathway_rows()
    assessments = {
        _identity_key(row): row for value in sources if (row := _assessment_row(value)) is not None
    }
    rows = []
    matched_assessments: set[str] = set()
    for value in sources:
        row = _outcome_row(value)
        if row is None:
            continue
        identity = _identity_key(row)
        assessment = assessments.get(identity)
        if assessment is not None:
            matched_assessments.add(identity)
            if assessment.get("expected_success") is not None:
                row["expected_success"] = assessment["expected_success"]
        rows.append(row)
    rows = rows[-PATHWAY_RECENT_CAP:]
    pathways: dict[str, dict[str, Any]] = {}
    for row in rows:
        pathway_id = str(row["pathway_id"])
        summary = pathways.setdefault(
            pathway_id,
            {
                "attempts": 0,
                "verified_successes": 0,
                "average_knowledge_delta": 0.0,
                "average_application_delta": 0.0,
                "failure_fingerprints": [],
                "expected_success": 0.0,
                "recent_rows": [],
            },
        )
        summary["attempts"] += 1
        if row.get("verification_verdict") == "verified":
            summary["verified_successes"] += 1
        fingerprint = str(row.get("failure_fingerprint") or "")
        if fingerprint and fingerprint not in summary["failure_fingerprints"]:
            summary["failure_fingerprints"].append(fingerprint)
        summary["recent_rows"].append(dict(row))

    for summary in pathways.values():
        recent = summary["recent_rows"]
        count = len(recent)
        summary["average_knowledge_delta"] = round(
            sum(float(row.get("knowledge_delta") or 0.0) for row in recent) / count,
            12,
        )
        summary["average_application_delta"] = round(
            sum(float(row.get("application_delta") or 0.0) for row in recent) / count,
            12,
        )
        summary["expected_success"] = round(
            sum(_expected_success(row) for row in recent) / count,
            12,
        )

    # One immutable judgment exists per window/pathway. Exact canonical replay
    # deduplicates; conflicting content fails closed before projection bytes move.
    window_updates = [
        update
        for update in resolve_canonical_pathway_window_updates(sources)
        if update["pathway_id"] in pathways
    ][-PATHWAY_RECENT_CAP:]
    for update in window_updates:
        pathways[update["pathway_id"]]["window_decision"] = dict(update)

    projection = {
        "version": 1,
        "cap": PATHWAY_RECENT_CAP,
        "pathways": pathways,
        "recent_rows": rows,
        "window_updates": window_updates,
        "unmatched_assessments": [
            {key: assessment.get(key) for key in ("program_id", "episode_id", "outcome_id")}
            for identity, assessment in sorted(assessments.items())
            if identity not in matched_assessments
        ],
    }
    write_text_atomic(
        PATHWAY_PROJECTION_PATH,
        json.dumps(projection, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return projection


def _load_projection_rows() -> list[dict[str, Any]]:
    if not PATHWAY_PROJECTION_PATH.is_file():
        return []
    try:
        payload = json.loads(PATHWAY_PROJECTION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("recent_rows") if isinstance(payload, Mapping) else None
    return [dict(row) for row in rows or () if isinstance(row, Mapping)]


def _load_projection_sources() -> list[dict[str, Any]]:
    if not PATHWAY_PROJECTION_PATH.is_file():
        return []
    try:
        payload = json.loads(PATHWAY_PROJECTION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, Mapping):
        return []
    return [
        dict(row)
        for key in ("recent_rows", "window_updates")
        for row in (payload.get(key) or ())
        if isinstance(row, Mapping)
    ]


def _require_intake_accepted(receipt: WriteReceipt) -> None:
    if not intake_receipt_accepted(receipt):
        message = str(getattr(receipt, "message", None) or "memory write rejected")
        raise RuntimeError(f"pathway_outcome continuity write rejected: {message}")


def pathway_outcome_record_id(outcome: PathwayOutcome) -> str:
    """The one canonical record id of an attempt's outcome: episode + program (WO-07)."""
    import hashlib

    material = f"{outcome.episode_id}\x00{outcome.outcome_id}\x00{outcome.program_id}"
    return "pathway-outcome." + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def record_pathway_outcome(outcome: PathwayOutcome) -> WriteReceipt:
    """Place a canonical pathway outcome in the episode SRAM intake lane, once per attempt.

    WO-07: the intake carries a stable event identity and the record a stable id. A retried
    terminus that finds the intake row its earlier attempt journalled reconciles from that row
    instead of journalling a second outcome — the attempt's timestamp would otherwise make the
    retry look like new content, and one attempt would land twice.
    """
    from memory import intake_journal

    if not isinstance(outcome, PathwayOutcome):
        raise TypeError("outcome must be a canonical PathwayOutcome")
    row = outcome.to_dict()
    timestamp = now_iso()
    if not outcome.episode_id or not outcome.outcome_id:
        raise ValueError("pathway outcome requires episode_id and outcome_id linkage")
    record_id = pathway_outcome_record_id(outcome)
    event_id = f"pathway_outcome:{record_id}"
    existing = intake_journal.record_by_id(intake_journal._record_id(event_id))
    if existing is not None and existing.status != intake_journal.REJECTED:
        return WriteReceipt(
            cycle_id=outcome.episode_id,
            staging_ref=None,
            path=existing.memory_id or None,
            validation_status="committed" if existing.status == intake_journal.COMMITTED
            else "held",
            message="reconciled from the intake row an earlier terminus attempt journalled",
            durable=True,
            journal_record_id=existing.record_id,
            event_id=existing.event_id,
            replayed=True,
        )
    envelope = CanonicalEnvelope(
        cycle_id=outcome.episode_id,
        coord=list(PATHWAY_OUTCOME_COORD),
        type="pathway_outcome",
        payload={
            "title": f"pathway_outcome {outcome.program_id}",
            "summary": (
                f"{outcome.verification_verdict.capitalize()} pathway result "
                f"for {outcome.pathway_id}"
            ),
            "text": json.dumps(row, sort_keys=True, ensure_ascii=False),
            "subtopic": "motor_attempt_assessment",
            "source_session_id": outcome.episode_id,
            "source_session_timestamp": timestamp,
            "enrichment": {
                "canonical": True,
                "program_id": outcome.program_id,
                "episode_id": outcome.episode_id,
                "outcome_id": outcome.outcome_id,
                "pathway_id": outcome.pathway_id,
                "pathway_outcome": row,
            },
        },
        source="omega.pathway_performance",
        confidence=1.0,
        timestamp_created=timestamp,
        id=record_id,
    )
    receipt = intake_memory(envelope, propose=False, bypass_guardian=True, event_id=event_id)
    _require_intake_accepted(receipt)
    return receipt


def recent_pathway_outcomes(
    episode_ids: Sequence[str] = (),
    cap: int = PATHWAY_RECENT_CAP,
) -> list[dict[str, Any]]:
    rows = _load_projection_rows()
    wanted = {str(value) for value in episode_ids if str(value)}
    if wanted:
        rows = [row for row in rows if str(row.get("episode_id") or "") in wanted]
    bounded = max(0, min(PATHWAY_RECENT_CAP, int(cap)))
    return rows[-bounded:] if bounded else []


__all__ = [
    "load_canonical_pathway_window_updates",
    "record_pathway_outcome",
    "recent_pathway_outcomes",
    "rebuild_pathway_projection",
    "resolve_canonical_pathway_window_updates",
]
