"""Sortable temporal hashes distinguish newly ingested canonical memories."""

from __future__ import annotations

import re

from memory.temporal_hash import build_temporal_hash


def test_temporal_hash_is_stable_and_lexically_sortable() -> None:
    older = build_temporal_hash("2024-01-02T03:04:05+00:00", "4.00.older")
    newer = build_temporal_hash("2025-01-02T03:04:05+00:00", "4.00.newer")

    assert older == build_temporal_hash(
        "2024-01-02T03:04:05+00:00", "4.00.older"
    )
    assert re.fullmatch(r"[0-9a-f]{16}-[0-9a-f]{16}", older)
    assert older < newer
