"""Fixed-width scoring geometry for the standalone DME lane.

Harrier embeddings belong to projection compilation. DME recall consumes
only Dewey coordinate bitmaps, so its MatMul contract is intentionally
independent of the configured embedding model and vector dimension.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

import numpy as np

from memory.dme.lobe_layout import ENTRY_WORDS, HEADER_WORDS

DME_GEOMETRY_SCHEMA_VERSION = "omega.dme_geometry.v1"


class DmeGeometryError(ValueError):
    """A DME artifact or scoring operand violates the fixed-width contract."""


@dataclass(frozen=True)
class DmeScoringGeometry:
    schema_version: str = DME_GEOMETRY_SCHEMA_VERSION
    compatible: bool = True
    entry_words: int = ENTRY_WORDS
    header_words: int = HEADER_WORDS
    entry_bits: int = ENTRY_WORDS * 64
    payload_words: int = ENTRY_WORDS - HEADER_WORDS
    query_dimension: int = (ENTRY_WORDS - HEADER_WORDS) * 64
    weight_dimension: int = (ENTRY_WORDS - HEADER_WORDS) * 64
    storage_dtype: str = "uint64"
    matmul_dtype: str = "uint8"
    uses_embeddings: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DME_SCORING_GEOMETRY = DmeScoringGeometry()


def scoring_geometry_receipt() -> dict[str, Any]:
    """Return the exact operand contract published with every DME pack."""
    return DME_SCORING_GEOMETRY.to_dict()


def validate_geometry_receipt(receipt: Mapping[str, Any] | None) -> None:
    """Reject an absent, legacy, or internally inconsistent pack receipt."""
    if not isinstance(receipt, Mapping):
        raise DmeGeometryError("DME geometry receipt missing")
    expected = scoring_geometry_receipt()
    mismatches = {
        key: {"expected": value, "actual": receipt.get(key)}
        for key, value in expected.items()
        if receipt.get(key) != value
    }
    if mismatches:
        raise DmeGeometryError(f"DME geometry receipt mismatch: {mismatches}")


def validate_weight_entries(
    matrix: np.ndarray,
    *,
    label: str = "weight",
    expected_rows: int | None = None,
) -> None:
    """Validate full uint64 DME entry rows before publication or scoring."""
    if not isinstance(matrix, np.ndarray):
        raise DmeGeometryError(
            f"DME {label} entries must be a numpy array, "
            f"actual_type={type(matrix).__name__}"
        )
    if matrix.ndim != 2:
        raise DmeGeometryError(
            f"DME {label} entries must be rank 2, actual_shape={matrix.shape}"
        )
    actual_words = int(matrix.shape[1])
    if actual_words != ENTRY_WORDS:
        raise DmeGeometryError(
            f"DME {label} entry width mismatch: "
            f"expected_words={ENTRY_WORDS} actual_words={actual_words}"
        )
    if matrix.dtype != np.uint64:
        raise DmeGeometryError(
            f"DME {label} storage dtype mismatch: "
            f"expected=uint64 actual={matrix.dtype}"
        )
    if expected_rows is not None and int(matrix.shape[0]) != expected_rows:
        raise DmeGeometryError(
            f"DME {label} row count mismatch: "
            f"expected_rows={expected_rows} actual_rows={matrix.shape[0]}"
        )


def validate_query_entry(query: np.ndarray) -> None:
    """Validate one full uint64 query entry before stripping its header."""
    if not isinstance(query, np.ndarray):
        raise DmeGeometryError(
            "DME query entry must be a numpy array, "
            f"actual_type={type(query).__name__}"
        )
    if query.ndim != 1:
        raise DmeGeometryError(
            f"DME query entry must be rank 1, actual_shape={query.shape}"
        )
    actual_words = int(query.shape[0])
    if actual_words != ENTRY_WORDS:
        raise DmeGeometryError(
            "DME query entry width mismatch: "
            f"expected_words={ENTRY_WORDS} actual_words={actual_words}"
        )
    if query.dtype != np.uint64:
        raise DmeGeometryError(
            "DME query storage dtype mismatch: "
            f"expected=uint64 actual={query.dtype}"
        )


def validate_payload_operands(rows: np.ndarray, query: np.ndarray) -> None:
    """Validate unpacked uint8 MatMul operands at the scoring boundary."""
    expected = DME_SCORING_GEOMETRY.query_dimension
    if not isinstance(rows, np.ndarray) or rows.ndim != 2:
        shape = getattr(rows, "shape", None)
        raise DmeGeometryError(
            f"DME weight payload must be rank 2, actual_shape={shape}"
        )
    if rows.dtype != np.uint8:
        raise DmeGeometryError(
            "DME weight payload dtype mismatch: "
            f"expected=uint8 actual={rows.dtype}"
        )
    if int(rows.shape[1]) != expected:
        raise DmeGeometryError(
            "DME weight dimension mismatch: "
            f"expected={expected} actual={rows.shape[1]}"
        )
    if not isinstance(query, np.ndarray) or query.ndim != 1:
        shape = getattr(query, "shape", None)
        raise DmeGeometryError(
            f"DME query payload must be rank 1, actual_shape={shape}"
        )
    if query.dtype != np.uint8:
        raise DmeGeometryError(
            "DME query payload dtype mismatch: "
            f"expected=uint8 actual={query.dtype}"
        )
    if int(query.shape[0]) != expected:
        raise DmeGeometryError(
            "DME query dimension mismatch: "
            f"expected={expected} actual={query.shape[0]}"
        )
