"""
Binary semantic encoding — the retina.

Inspired by Omega3.0's Ettin retina, this module produces binary codes
from text without dense vectors or runtime embeddings. Uses hash-based
bit assignment for deterministic, reproducible encoding.

Key properties:
- No dense vectors stored anywhere
- Binary codes are reproducible (same text → same code)
- Hamming distance is the similarity metric
- D=64 bits by default (configurable)
- Candidate-only: the retina proposes, never admits

This is the "perception lane" in the recall system.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("standalone_memory.retina")

# Default binary code dimension
DEFAULT_DIMENSION = 64


class Retina:
    """Binary semantic encoder — the retina.

    Produces D-dimensional binary codes from text. Codes are
    reproducible: the same text always produces the same code.

    Thread-safe: all operations are locked.
    """

    def __init__(self, dimension: int = DEFAULT_DIMENSION):
        """Initialize the retina.

        Args:
            dimension: Number of bits in each binary code (default 64)
        """
        self._dimension = dimension
        self._codes: Dict[str, bytes] = {}  # id -> binary code
        self._texts: Dict[str, str] = {}  # id -> text (for similarity)
        self._lock = threading.RLock()

    @property
    def dimension(self) -> int:
        """The binary code dimension."""
        return self._dimension

    def encode(self, text: str) -> bytes:
        """Encode text into a binary code.

        Uses hash-based bit assignment: for each bit position i,
        hash(text + i) determines the bit value.

        Args:
            text: Text to encode

        Returns:
            Binary code bytes
        """
        if not text:
            return b"\x00" * ((self._dimension + 7) // 8)

        text_bytes = text.encode("utf-8")
        code = bytearray((self._dimension + 7) // 8)

        for i in range(self._dimension):
            seed_hash = hashlib.sha256(text_bytes + i.to_bytes(4, "big")).digest()
            bit = seed_hash[0] & 1
            byte_idx = i // 8
            bit_idx = i % 8
            if bit:
                code[byte_idx] |= (1 << bit_idx)

        return bytes(code)

    def register(self, record_id: str, text: str) -> bytes:
        """Register a record's binary code.

        Args:
            record_id: Unique identifier for the record
            text: Text content to encode

        Returns:
            The generated binary code
        """
        with self._lock:
            code = self.encode(text)
            self._codes[record_id] = code
            self._texts[record_id] = text
            return code

    def unregister(self, record_id: str) -> None:
        """Remove a record from the retina.

        Args:
            record_id: Unique identifier for the record
        """
        with self._lock:
            self._codes.pop(record_id, None)
            self._texts.pop(record_id, None)

    def get_code(self, record_id: str) -> Optional[bytes]:
        """Get the binary code for a registered record.

        Args:
            record_id: Unique identifier for the record

        Returns:
            Binary code bytes, or None if not registered
        """
        with self._lock:
            return self._codes.get(record_id)

    def hamming_distance(self, code_a: bytes, code_b: bytes) -> int:
        """Compute Hamming distance between two binary codes.

        Args:
            code_a: First binary code
            code_b: Second binary code

        Returns:
            Number of differing bits

        Raises:
            ValueError: If codes have different lengths
        """
        if len(code_a) != len(code_b):
            raise ValueError(
                f"Code length mismatch: {len(code_a)} vs {len(code_b)}"
            )

        distance = 0
        for b1, b2 in zip(code_a, code_b):
            xor = b1 ^ b2
            while xor:
                distance += xor & 1
                xor >>= 1
        return distance

    def find_similar(
        self,
        query_text: str,
        limit: int = 10,
        max_distance: Optional[int] = None,
    ) -> List[Tuple[str, int, float]]:
        """Find records similar to the query text.

        Encodes the query, then computes Hamming distance to all
        registered codes. Returns the top-K most similar records.

        Args:
            query_text: Query text to find similar records for
            limit: Maximum number of results
            max_distance: Optional maximum Hamming distance filter

        Returns:
            List of (record_id, hamming_distance, similarity_score) tuples,
            sorted by distance (ascending)
        """
        query_code = self.encode(query_text)
        results = []

        with self._lock:
            for record_id, code in self._codes.items():
                distance = self.hamming_distance(query_code, code)

                if max_distance is not None and distance > max_distance:
                    continue

                # Similarity score: 1.0 - (distance / max_possible_distance)
                max_dist = self._dimension
                similarity = 1.0 - (distance / max_dist) if max_dist > 0 else 0.0

                results.append((record_id, distance, similarity))

        # Sort by distance ascending (most similar first)
        results.sort(key=lambda x: (x[1], -x[2]))

        return results[:limit]

    def window(
        self,
        query_text: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Get a similarity window around the query.

        Returns the top-K most similar records as dicts suitable
        for the recall pipeline.

        Args:
            query_text: Query text
            limit: Maximum number of results

        Returns:
            List of dicts with record_id, similarity, distance
        """
        similar = self.find_similar(query_text, limit=limit)
        return [
            {
                "record_id": rid,
                "similarity": sim,
                "distance": dist,
            }
            for rid, dist, sim in similar
        ]

    def record_ids(self) -> List[str]:
        """Get all registered record IDs.

        Returns:
            List of record ID strings
        """
        with self._lock:
            return list(self._codes.keys())

    def __len__(self) -> int:
        with self._lock:
            return len(self._codes)

    def clear(self) -> None:
        """Clear all registered codes."""
        with self._lock:
            self._codes.clear()
            self._texts.clear()
