"""Coordinate intersection / union / Hamming search over SRAM tensor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from memory.dme.matmul_engine import compute_overlaps, query_popcount
from memory.dme.packer import pack_entry, unpack_source_authority, unpack_timestamp


@dataclass
class DMEHit:
    row_index: int
    memory_id: str
    match_count: int
    query_count: int
    score: float
    title: str = ""
    summary: str = ""
    authority: int = 0
    timestamp: float = 0.0

    def to_candidate_dict(self, *, snapshot_hash: str, backend: str) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "coord": [],
            "score": self.score,
            "title": self.title,
            "summary": self.summary,
            "domain": "",
            "subtopic": "",
            "tags": [],
            "provenance": {
                "dme_snapshot_hash": snapshot_hash,
                "dme_backend": backend,
                "dme_match_count": self.match_count,
            },
        }


@dataclass
class DMESearchResult:
    hits: list[DMEHit]
    query_time_ms: float
    entries_scanned: int
    mode: str = "intersection"
    query_coordinates: list[tuple[int, int, int]] = field(default_factory=list)


def search_intersection(
    sram: np.ndarray,
    index: list[dict[str, Any]],
    query_coordinates: list[tuple[int, int, int]],
    *,
    target_lobe: int | None = None,
    max_results: int = 20,
    generation_id: str | None = None,
) -> DMESearchResult:
    if len(sram) == 0 or not query_coordinates:
        return DMESearchResult(hits=[], query_time_ms=0.0, entries_scanned=0)

    query = pack_entry(0, 0.0, "query", query_coordinates)
    required = query_popcount(query)
    overlaps = compute_overlaps(sram, query, generation_id=generation_id)  # Cut 13: pinned to the caller's view

    hits: list[DMEHit] = []
    for row, overlap in enumerate(overlaps):
        if overlap < required:
            continue
        meta = index[row] if row < len(index) else {}
        coord = meta.get("coord") or ()
        if target_lobe is not None and coord and int(coord[0]) != target_lobe:
            continue
        hits.append(
            DMEHit(
                row_index=row,
                memory_id=str(meta.get("memory_id") or ""),
                match_count=int(overlap),
                query_count=required,
                score=round(min(1.0, overlap / max(1, required)), 6),
                title=str(meta.get("title") or ""),
                summary=str(meta.get("summary") or ""),
                authority=unpack_source_authority(sram[row]),
                timestamp=unpack_timestamp(sram[row]),
            )
        )
        if len(hits) >= max_results:
            break

    hits.sort(key=lambda h: (-h.score, -h.authority, -h.timestamp, h.memory_id))
    return DMESearchResult(
        hits=hits,
        query_time_ms=0.0,
        entries_scanned=len(sram),
        mode="intersection",
        query_coordinates=query_coordinates,
    )
