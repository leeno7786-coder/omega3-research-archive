"""Verified Workspace source recall for alignment context."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.paths import OMEGA_ROOT
from memory.recall_verifier import verify_candidates
from memory.source_validity import (
    SourceValidityError,
    load_projection,
    projection_path,
    rebuild_from_canonical,
    verify_workspace_source,
)
from memory.workspace_retention import workspace_source_is_current


class AlignmentContextError(RuntimeError):
    """Workspace alignment recall infrastructure is unavailable or corrupt."""


_FACETS: tuple[str, ...] = (
    "responsibilities",
    "communication_patterns",
    "document_conventions",
    "schedules",
    "recurring_task_flows",
)

_FACET_TERMS: dict[str, tuple[str, ...]] = {
    "responsibilities": (
        "responsibilities",
        "responsibility",
        "owner",
        "owners",
        "owns",
        "assigned",
        "assignment",
        "role",
        "roles",
        "accountable",
        "decision",
        "decisions",
    ),
    "communication_patterns": (
        "communication",
        "communications",
        "email",
        "emails",
        "thread",
        "threads",
        "message",
        "messages",
        "update",
        "updates",
        "sync",
        "standup",
        "reply",
        "replies",
    ),
    "document_conventions": (
        "document",
        "documents",
        "doc",
        "docs",
        "brief",
        "briefs",
        "heading",
        "headings",
        "template",
        "templates",
        "convention",
        "conventions",
        "decision log",
        "decision logs",
        "format",
        "formats",
    ),
    "schedules": (
        "schedule",
        "schedules",
        "scheduled",
        "calendar",
        "calendars",
        "meeting",
        "meetings",
        "deadline",
        "deadlines",
        "weekly",
        "monthly",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
    ),
    "recurring_task_flows": (
        "recurring",
        "repeat",
        "repeats",
        "routine",
        "routines",
        "task flow",
        "task flows",
        "workflow",
        "workflows",
        "checklist",
        "checklists",
        "handoff",
        "handoffs",
        "triage",
        "review cycle",
    ),
}

_WORKSPACE_SOURCE_IDENTITY_FIELDS: tuple[str, ...] = (
    "connection_id",
    "capability",
    "object_id",
    "revision_id",
    "content_digest",
    "observed_at",
    "availability_state",
)

_SKIP_PARTS = frozenset(
    {
        "dme",
        "indexes",
        "projection_receipts",
        "quarantine",
        "recall_verifier",
        "source_ledger",
        "taxonomy_store",
        "tmp_writes",
        "tombstones",
    }
)

_SUMMARY_CHAR_LIMIT = 480


def build_alignment_context(
    connection_id: str,
    *,
    root: Path | str | None = None,
    token_budget: int = 3000,
) -> dict[str, Any]:
    """
    Return verified, bounded Workspace evidence for adaptive alignment.

    Empty evidence means the source-validity and recall verification machinery
    ran successfully and found no current cryptographically verified Workspace
    records for the requested connection.
    """

    connection = str(connection_id or "").strip()
    if not connection:
        raise AlignmentContextError("connection_id is required")
    budget = _validate_token_budget(token_budget)
    profile_root = _profile_root(root)
    _ensure_source_validity_projection(profile_root)

    evidence: list[dict[str, Any]] = []
    facets = {facet: [] for facet in _FACETS}
    tokens_used = 0
    seen: set[str] = set()

    for path in _canonical_json_paths(_memory_root(profile_root)):
        record = _read_record(path)
        workspace_source = record.get("workspace_source")
        if not isinstance(workspace_source, Mapping):
            continue
        if str(workspace_source.get("connection_id") or "").strip() != connection:
            continue
        if not workspace_source_is_current(record):
            continue

        facet = _facet_for_record(record)
        if facet is None:
            continue
        if not _workspace_source_is_source_valid(workspace_source, root=profile_root):
            continue

        memory_id = str(record.get("id") or "").strip()
        if not memory_id or memory_id in seen:
            continue
        verified = _verify_record(record, path, profile_root)
        if verified is None:
            continue

        summary = _bounded_summary(record)
        item_tokens = _estimate_tokens(summary)
        if tokens_used + item_tokens > budget:
            remaining = budget - tokens_used
            if remaining <= 0:
                break
            summary = _clip_to_token_budget(summary, remaining)
            item_tokens = _estimate_tokens(summary)

        item = {
            "memory_id": memory_id,
            "connection_id": connection,
            "facet": facet,
            "summary": summary,
            "source_proof": _public_source_proof(verified["source_proof"]),
            "workspace_source": _workspace_source_identity(workspace_source),
        }
        evidence.append(item)
        facets[facet].append(memory_id)
        tokens_used += item_tokens
        seen.add(memory_id)

    return {
        "connection_id": connection,
        "evidence": evidence,
        "facets": facets,
        "empty": not evidence,
        "token_budget": budget,
        "tokens_used": tokens_used,
    }


def _validate_token_budget(token_budget: int) -> int:
    try:
        budget = int(token_budget)
    except (TypeError, ValueError) as exc:
        raise AlignmentContextError("token_budget must be a positive integer") from exc
    if budget <= 0:
        raise AlignmentContextError("token_budget must be a positive integer")
    return budget


def _ensure_source_validity_projection(root: Path) -> None:
    path = projection_path(root)
    try:
        if not path.exists():
            rebuild_from_canonical(root)
        load_projection(root)
    except SourceValidityError as exc:
        raise AlignmentContextError(f"source validity unavailable: {exc}") from exc
    except OSError as exc:
        raise AlignmentContextError(f"source validity unavailable: {exc}") from exc


def _workspace_source_is_source_valid(
    workspace_source: Mapping[str, Any],
    *,
    root: Path,
) -> bool:
    try:
        verdict = verify_workspace_source(workspace_source, root=root)
    except SourceValidityError as exc:
        raise AlignmentContextError(f"source validity unavailable: {exc}") from exc
    return verdict.get("ok") is True


def _verify_record(record: Mapping[str, Any], path: Path, root: Path) -> dict[str, Any] | None:
    candidate = _candidate_from_record(record, path, root)
    try:
        verdict = verify_candidates(
            _verification_query(record),
            [candidate],
            root=root,
        )
    except Exception as exc:
        raise AlignmentContextError(f"recall verification failed: {type(exc).__name__}: {exc}") from exc

    if verdict.status != "verified" or not verdict.answer_payload:
        _raise_if_infrastructure_rejection(verdict)
        return None
    candidates = verdict.answer_payload.get("candidates")
    if not candidates:
        return None
    verified = dict(candidates[0])
    proof = verified.get("source_proof")
    if not isinstance(proof, Mapping):
        return None
    if str(proof.get("evidence_strength") or "") != "source_ledger_crypto":
        return None
    return verified


def _raise_if_infrastructure_rejection(verdict: Any) -> None:
    reasons = [
        str(hit.get("reason") or "")
        for hit in getattr(verdict, "rejected_hits", ()) or ()
        if isinstance(hit, Mapping)
    ]
    infrastructure_markers = (
        "source ledger unavailable",
        "workspace source validity unavailable",
        "projection receipt verification failed",
    )
    for reason in reasons:
        if any(marker in reason for marker in infrastructure_markers):
            raise AlignmentContextError(f"recall verification failed: {reason}")


def _candidate_from_record(record: Mapping[str, Any], path: Path, root: Path) -> dict[str, Any]:
    try:
        relative_path = path.relative_to(root).as_posix()
    except ValueError:
        relative_path = path.as_posix()
    return {
        "memory_id": str(record.get("id") or ""),
        "coord": list(record.get("coord") or ()),
        "score": 1.0,
        "title": str(record.get("title") or ""),
        "summary": str(record.get("summary") or ""),
        "domain": str(record.get("domain") or ""),
        "subtopic": str(record.get("subtopic") or ""),
        "tags": list(record.get("tags") or ()),
        "path": relative_path,
        "filesystem_fallback": True,
        "workspace_source": dict(record.get("workspace_source") or {}),
    }


def _verification_query(record: Mapping[str, Any]) -> str:
    parts = [
        str(record.get("title") or ""),
        str(record.get("summary") or ""),
        " ".join(str(tag) for tag in record.get("tags") or ()),
    ]
    query = " ".join(part for part in parts if part.strip()).strip()
    return query or str(record.get("id") or "workspace alignment")


def _facet_for_record(record: Mapping[str, Any]) -> str | None:
    text = " ".join(
        str(part or "").lower()
        for part in (
            record.get("title"),
            record.get("summary"),
            record.get("subtopic"),
            record.get("domain"),
            " ".join(str(tag) for tag in record.get("tags") or ()),
        )
    )
    for facet in _FACETS:
        if any(term in text for term in _FACET_TERMS[facet]):
            return facet
    return None


def _bounded_summary(record: Mapping[str, Any]) -> str:
    summary = str(record.get("summary") or record.get("title") or record.get("text") or "").strip()
    return _clip_text(summary, _SUMMARY_CHAR_LIMIT)


def _public_source_proof(proof: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        "evidence_strength": "cryptographic",
        "verification_method": "source_ledger_crypto",
    }
    for key in (
        "source_id",
        "source_type",
        "source_session_id",
        "source_session_timestamp",
        "span_start",
        "span_end",
        "span_hash",
        "source_content_hash",
    ):
        value = proof.get(key)
        if value is not None and str(value).strip():
            out[key] = value
    return out


def _workspace_source_identity(workspace_source: Mapping[str, Any]) -> dict[str, str]:
    return {
        field: str(workspace_source.get(field) or "").strip()
        for field in _WORKSPACE_SOURCE_IDENTITY_FIELDS
    }


def _canonical_json_paths(memory_root: Path) -> list[Path]:
    if not memory_root.exists():
        return []
    paths: list[Path] = []
    for path in memory_root.rglob("*.json"):
        rel = path.relative_to(memory_root)
        if any(part in _SKIP_PARTS for part in rel.parts):
            continue
        paths.append(path)
    return sorted(paths, key=lambda item: item.relative_to(memory_root).as_posix())


def _read_record(path: Path) -> Mapping[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AlignmentContextError(f"canonical recall corpus unreadable: {path}") from exc
    if not isinstance(data, Mapping):
        return {}
    return data


def _clip_to_token_budget(text: str, token_budget: int) -> str:
    return _clip_text(text, max(1, token_budget) * 4)


def _clip_text(text: str, max_chars: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 3)].rstrip() + "..."


def _estimate_tokens(text: str) -> int:
    value = str(text or "")
    if not value:
        return 0
    return max(1, (len(value) + 3) // 4)


def _profile_root(root: Path | str | None) -> Path:
    if root is None:
        return OMEGA_ROOT
    path = Path(root)
    return path.parent if path.name == "memory" else path


def _memory_root(root: Path) -> Path:
    return root if root.name == "memory" else root / "memory"


__all__ = ["AlignmentContextError", "build_alignment_context"]
