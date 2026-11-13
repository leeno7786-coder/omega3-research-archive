"""Bounded audit receipts for candidate rows omitted during recall."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

MAX_RECEIPT_CANDIDATE_IDS = 32
MAX_RECEIPT_ID_CHARS = 128


def _candidate_id(value: Any) -> str:
    raw = str(value or "")
    if len(raw) <= MAX_RECEIPT_ID_CHARS:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _candidate_id_digest(ids: list[str]) -> str:
    """Hash the full ordered ID list with length framing to avoid ambiguity."""
    digest = hashlib.sha256()
    for item in ids:
        encoded = item.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def candidate_pool_trace(ids: Iterable[Any]) -> dict[str, Any]:
    """Return a bounded identity summary for a verifier input pool."""
    normalized = [_candidate_id(value) for value in ids if value]
    return {
        "candidate_count": len(normalized),
        "candidate_ids": normalized[:MAX_RECEIPT_CANDIDATE_IDS],
        "candidate_ids_omitted_count": max(
            0, len(normalized) - MAX_RECEIPT_CANDIDATE_IDS
        ),
        "candidate_ids_sha256": _candidate_id_digest(normalized),
    }


def build_truncation_receipt(
    stage: str,
    input_ids: Iterable[Any],
    output_ids: Iterable[Any],
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe a candidate-selection boundary without changing its ordering.

    Receipts include at most 32 candidate IDs (each at most 128 characters) per
    stage. Counts and a SHA-256 digest cover the complete ordered dropped-ID
    list, so large corpora remain bounded without losing a stable reconciliation
    value.
    """
    input_rows = [_candidate_id(value) for value in input_ids if value]
    output_rows = [_candidate_id(value) for value in output_ids if value]
    output_set = set(output_rows)
    dropped = [memory_id for memory_id in input_rows if memory_id not in output_set]
    receipt: dict[str, Any] = {
        "stage": str(stage),
        "input_count": len(input_rows),
        "output_count": len(output_rows),
        "dropped_count": len(dropped),
        "dropped_candidate_ids": dropped[:MAX_RECEIPT_CANDIDATE_IDS],
        "dropped_candidate_ids_omitted_count": max(
            0, len(dropped) - MAX_RECEIPT_CANDIDATE_IDS
        ),
        "dropped_candidate_ids_sha256": _candidate_id_digest(dropped),
    }
    if details:
        receipt["details"] = dict(details)
    return receipt
