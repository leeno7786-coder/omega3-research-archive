"""Disposable DME planes derived from learned association sidecar state."""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from memory.association_strength import (
    STATUS_ACTIVE,
    STRUCTURAL_PROVENANCE_CLASS,
    association_id_for,
    effective_strength,
)
from memory.dme.geometry import validate_weight_entries
from memory.dme.hop_engine import WEAK_EDGE_TYPES
from memory.dme.lobe_layout import HEADER_WORDS
from memory.taxonomy.normalize import canonical_json

LEARNED_PLANE_SCHEMA_VERSION = "omega.learned_planes.v1"
LEARNED_PLANE_CONFIG_VERSION = "omega.learned_planes.config.v1"
LEARNED_PLANE_WEIGHTS = {"strong": 0.60, "medium": 0.45, "weak": 0.25}
LEARNED_PLANE_FLOOR = 0.34


class LearnedPlaneError(ValueError):
    """Learned plane input is malformed or cannot be projected safely."""


@dataclass(frozen=True)
class LearnedPlaneThresholds:
    """Versioned policy values; they are not association schema semantics."""

    strong: float = 0.75
    medium: float = 0.45
    weak: float = 0.20

    def __post_init__(self) -> None:
        values = (self.strong, self.medium, self.weak)
        if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in values):
            raise LearnedPlaneError("learned plane thresholds must be finite")
        if not all(0.0 <= float(value) <= 1.0 for value in values):
            raise LearnedPlaneError("learned plane thresholds must be within [0, 1]")
        if not self.strong >= self.medium >= self.weak:
            raise LearnedPlaneError("learned plane thresholds must descend strong to weak")

    def to_dict(self) -> dict[str, float]:
        return {
            "strong": float(self.strong),
            "medium": float(self.medium),
            "weak": float(self.weak),
        }


@dataclass(frozen=True)
class LearnedPlanes:
    """Three tiered learned projections and the identities that produced them."""

    strong: np.ndarray
    medium: np.ndarray
    weak: np.ndarray
    association_generation: str
    projection_config_hash: str

    def __post_init__(self) -> None:
        arrays = (self.strong, self.medium, self.weak)
        if not all(isinstance(array, np.ndarray) and array.ndim == 2 for array in arrays):
            raise LearnedPlaneError("learned planes must be two-dimensional arrays")
        if not (self.strong.shape == self.medium.shape == self.weak.shape):
            raise LearnedPlaneError("learned plane shapes must match")
        if not str(self.association_generation or ""):
            raise LearnedPlaneError("association_generation is required")
        if not str(self.projection_config_hash or ""):
            raise LearnedPlaneError("projection_config_hash is required")


@dataclass(frozen=True)
class LearnedPlaneHit:
    memory_id: str
    row_index: int
    score: float
    edge_class: str
    match_count: int
    query_count: int


def projection_config_hash_for(thresholds: LearnedPlaneThresholds) -> str:
    """Hash the tier policy so changing thresholds changes projection identity."""
    payload = {
        "schema_version": LEARNED_PLANE_CONFIG_VERSION,
        "thresholds": thresholds.to_dict(),
        "weights": dict(LEARNED_PLANE_WEIGHTS),
        "floor": LEARNED_PLANE_FLOOR,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _finite_strength(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise LearnedPlaneError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise LearnedPlaneError(f"{name} must be finite")
    return max(0.0, min(1.0, result))


def _row_map(row_index: list[Mapping[str, Any]]) -> dict[str, int]:
    rows: dict[str, int] = {}
    for index, row in enumerate(row_index):
        if not isinstance(row, Mapping):
            raise LearnedPlaneError("learned plane row index contains a non-object")
        memory_id = str(row.get("memory_id") or "").strip()
        if not memory_id:
            raise LearnedPlaneError("learned plane row index contains a missing memory_id")
        if memory_id in rows:
            raise LearnedPlaneError(f"learned plane row index duplicates {memory_id}")
        rows[memory_id] = index
    return rows


def build_learned_planes(
    sram: np.ndarray,
    row_index: list[Mapping[str, Any]],
    records: Mapping[str, Mapping[str, Any]],
    *,
    association_generation: str,
    thresholds: LearnedPlaneThresholds | None = None,
    projection_config_hash: str | None = None,
) -> LearnedPlanes:
    """Build learned tier planes from active non-structural sidecar records."""
    validate_weight_entries(sram)
    if len(row_index) != len(sram):
        raise LearnedPlaneError("learned plane row index does not match SRAM rows")
    if not isinstance(records, Mapping):
        raise LearnedPlaneError("learned plane records must be an object")
    policy = thresholds or LearnedPlaneThresholds()
    config_hash = projection_config_hash or projection_config_hash_for(policy)
    rows = _row_map(row_index)
    planes = {
        "strong": np.zeros_like(sram),
        "medium": np.zeros_like(sram),
        "weak": np.zeros_like(sram),
    }

    for key in sorted(records):
        record = records[key]
        if not isinstance(record, Mapping):
            raise LearnedPlaneError(f"learned association {key!r} is not an object")
        provenance = str(record.get("provenance_class") or "").strip()
        if provenance == STRUCTURAL_PROVENANCE_CLASS:
            continue
        status = str(record.get("status") or "")
        if status not in {"active", "dormant", "retired"}:
            raise LearnedPlaneError(f"learned association {key!r} has an invalid status")
        if status != STATUS_ACTIVE:
            continue
        association_id = str(record.get("association_id") or "")
        if association_id != str(key):
            raise LearnedPlaneError(f"learned association key does not match {key!r}")
        source_id = str(record.get("source_id") or "").strip()
        target_id = str(record.get("target_id") or "").strip()
        relation_type = str(record.get("relation_type") or "").strip()
        try:
            expected_id = association_id_for(source_id, relation_type, target_id, provenance)
        except (TypeError, ValueError) as exc:
            raise LearnedPlaneError(f"learned association {key!r} has invalid identity") from exc
        if expected_id != association_id:
            raise LearnedPlaneError(f"learned association {key!r} has an invalid identity")
        if source_id not in rows or target_id not in rows:
            raise LearnedPlaneError(f"learned association {key!r} has a missing endpoint")
        effective = effective_strength(
            structural=_finite_strength(record.get("structural_strength"), "structural_strength"),
            experiential=_finite_strength(record.get("experiential_strength"), "experiential_strength"),
        )
        if effective >= policy.strong:
            plane = planes["strong"]
        elif effective >= policy.medium:
            plane = planes["medium"]
        elif effective >= policy.weak:
            plane = planes["weak"]
        else:
            continue
        source_row = rows[source_id]
        target_row = rows[target_id]
        plane[target_row, HEADER_WORDS:] |= sram[source_row, HEADER_WORDS:]
        if relation_type in WEAK_EDGE_TYPES:
            plane[source_row, HEADER_WORDS:] |= sram[target_row, HEADER_WORDS:]

    for plane in planes.values():
        plane[:, :HEADER_WORDS] = 0
    return LearnedPlanes(
        strong=planes["strong"],
        medium=planes["medium"],
        weak=planes["weak"],
        association_generation=str(association_generation or ""),
        projection_config_hash=config_hash,
    )


def learned_planes_enabled() -> bool:
    return os.environ.get("OMEGA_DME_LEARNED_PLANES", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def search_learned_planes(
    sram: np.ndarray,
    row_index: list[Mapping[str, Any]],
    planes: LearnedPlanes,
    query_coordinates: list[tuple[int, int, int]],
    *,
    max_results: int = 20,
    truncation_receipts: list[dict[str, Any]] | None = None,
) -> list[LearnedPlaneHit]:
    """Score learned tiers separately from the frozen structural hop scorer."""
    from memory.dme.hop_engine import _payload_bits_u8
    from memory.dme.packer import pack_entry

    validate_weight_entries(sram)
    if len(row_index) != len(sram) or not query_coordinates:
        return []
    query = pack_entry(0, 0.0, "query", query_coordinates)
    query_bits = _payload_bits_u8(query)[0]
    query_count = max(1, int(query_bits.sum()))
    best: dict[int, LearnedPlaneHit] = {}
    for tier, plane in (
        ("strong", planes.strong),
        ("medium", planes.medium),
        ("weak", planes.weak),
    ):
        matches = _payload_bits_u8(plane) @ query_bits
        ratio = np.minimum(matches.astype(np.float64) / query_count, 1.0)
        for row in np.nonzero(ratio >= LEARNED_PLANE_FLOOR)[0]:
            score = round(float(LEARNED_PLANE_WEIGHTS[tier] * ratio[row]), 6)
            hit = LearnedPlaneHit(
                memory_id=str((row_index[row] or {}).get("memory_id") or ""),
                row_index=int(row),
                score=score,
                edge_class=f"learned_{tier}",
                match_count=int(matches[row]),
                query_count=query_count,
            )
            prior = best.get(int(row))
            if prior is None or (hit.score, hit.edge_class) > (prior.score, prior.edge_class):
                best[int(row)] = hit
    ranked = sorted(
        best.values(),
        key=lambda hit: (-hit.score, hit.row_index, hit.memory_id),
    )
    selected = ranked[: max(0, int(max_results))]
    if truncation_receipts is not None:
        from memory.recall_truncation import build_truncation_receipt

        truncation_receipts.append(
            build_truncation_receipt(
                "dme_learned_planes:max_results",
                (hit.memory_id for hit in ranked),
                (hit.memory_id for hit in selected),
                details={"max_results": max(0, int(max_results))},
            )
        )
    return selected


__all__ = [
    "LEARNED_PLANE_CONFIG_VERSION",
    "LEARNED_PLANE_SCHEMA_VERSION",
    "LearnedPlaneError",
    "LearnedPlaneHit",
    "LearnedPlaneThresholds",
    "LearnedPlanes",
    "build_learned_planes",
    "learned_planes_enabled",
    "projection_config_hash_for",
    "search_learned_planes",
]
