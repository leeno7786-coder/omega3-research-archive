"""Phase 7C — canonical normalization for deterministic memory IDs/hashes.

Rules (from the work order):
  1. Trim leading/trailing whitespace.
  2. Collapse repeated internal whitespace to one space.
  3. Lowercase ONLY for ID-normalization fields (normalize_id_field).
  4. Preserve original casing in payloads (normalize_text does NOT lowercase).
  5/6. Canonical JSON: sorted keys, no extra whitespace.
  9. Never use Python repr for canonical hashing.
  10. Never depend on dict insertion order (sort_keys handles it).

Pure, total, stdlib-only. No upward imports.
"""

from __future__ import annotations

import json
import re
from typing import Any

_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: Any) -> str:
    """Trim + collapse internal whitespace; PRESERVE case (rules 1, 2, 4)."""
    return _WHITESPACE.sub(" ", str(value).strip())


def normalize_id_field(value: Any) -> str:
    """Trim + collapse + lowercase — for ID-normalization fields only (rule 3).

    Use for free-text identity fields (canonical_name, claim subject/predicate/object)
    where casing/whitespace variants must collapse to the SAME id. Do NOT use for
    structured ids/paths that must stay verbatim.
    """
    return normalize_text(value).lower()


def canonical_json(obj: Any) -> str:
    """Deterministic JSON string: sorted keys, no extra whitespace (rules 5, 6, 10).

    Never uses repr (rule 9). Insertion-order independent. ensure_ascii=False keeps
    unicode stable across platforms.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sorted_for_hash(items: Any) -> list[str]:
    """Sort a collection of canonical strings for order-independent hashing (rule 7).

    Use when list order is NOT semantically meaningful (e.g. the set of canonical
    event lines feeding a snapshot hash).
    """
    return sorted(str(i) for i in items)
