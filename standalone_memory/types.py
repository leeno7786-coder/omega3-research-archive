"""
Data types for the standalone memory system.

Defines the core data structures used throughout the system:
- MemoryRecord: a single memory at a (X, Y, Z) coordinate
- RetrievalResult: the output of a read operation
- WriteResult: the output of a write operation
- Verdict: the verification gate's decision
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# =====================================================================
# Verdict — the verification gate's decision
# =====================================================================


class Verdict(Enum):
    """The verification gate's decision on a candidate.

    VERIFIED — provenance checks out, candidate is admitted
    UNKNOWN  — cannot verify (missing source, no proof)
    AMBIGUOUS — multiple conflicting sources
    CONFLICT — contradicted evidence
    """

    VERIFIED = "verified"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"


# =====================================================================
# MemoryRecord — a single memory at a (X, Y, Z) coordinate
# =====================================================================


@dataclass
class MemoryRecord:
    """A single memory record stored at a deterministic (X, Y, Z) coordinate.

    This is the core data structure. Every fact, event, or observation
    lives as one of these, persisted to disk as a JSON file.

    Attributes:
        id: Unique identifier (auto-generated UUID)
        text: The raw text content of the memory
        coord: (X, Y, Z) coordinate — X=lobe, Y=subdomain, Z=cluster
        type: Memory type (knowledge, episodic, procedural, identity, etc.)
        title: Short title/description
        summary: Brief summary of the content
        tags: List of tags for filtering
        domain: Domain classification (optional)
        confidence: Initial confidence score [0.0, 1.0]
        provenance: Source information (who/what created this)
        evidence_refs: References to supporting evidence
        flags: Special flags (canonical, private, contradicted, etc.)
        version: Revision version (for supersedes chains)
        written_at: ISO timestamp of creation
        updated_at: ISO timestamp of last update
        meta: Arbitrary metadata dict
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    text: str = ""
    coord: List[int] = field(default_factory=lambda: [0, 0, 0])
    type: str = "episodic"
    title: str = ""
    summary: str = ""
    tags: List[str] = field(default_factory=list)
    domain: Optional[str] = None
    confidence: float = 0.5
    provenance: Dict[str, Any] = field(default_factory=dict)
    evidence_refs: List[str] = field(default_factory=list)
    flags: Dict[str, Any] = field(default_factory=dict)
    version: int = 1
    written_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    updated_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    meta: Dict[str, Any] = field(default_factory=dict)

    # Derived fields (computed, not persisted directly)
    binary_code: Optional[bytes] = None  # D=64 binary encoding
    strength: float = 0.5  # Engram strength [0.0, 1.0]
    touches: int = 0  # Number of times accessed
    last_accessed: Optional[str] = None
    contradicted: bool = False
    contradiction_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON persistence."""
        return {
            "id": self.id,
            "text": self.text,
            "coord": self.coord,
            "type": self.type,
            "title": self.title,
            "summary": self.summary,
            "tags": self.tags,
            "domain": self.domain,
            "confidence": self.confidence,
            "provenance": self.provenance,
            "evidence_refs": self.evidence_refs,
            "flags": self.flags,
            "version": self.version,
            "written_at": self.written_at,
            "updated_at": self.updated_at,
            "meta": self.meta,
            # Derived fields stored for diagnostics
            "strength": self.strength,
            "touches": self.touches,
            "last_accessed": self.last_accessed,
            "contradicted": self.contradicted,
            "contradiction_count": self.contradiction_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryRecord":
        """Deserialize from dict (JSON)."""
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            text=data.get("text", ""),
            coord=data.get("coord", [0, 0, 0]),
            type=data.get("type", "episodic"),
            title=data.get("title", ""),
            summary=data.get("summary", ""),
            tags=data.get("tags", []),
            domain=data.get("domain"),
            confidence=float(data.get("confidence", 0.5)),
            provenance=data.get("provenance", {}),
            evidence_refs=data.get("evidence_refs", []),
            flags=data.get("flags", {}),
            version=int(data.get("version", 1)),
            written_at=data.get("written_at", ""),
            updated_at=data.get("updated_at", ""),
            meta=data.get("meta", {}),
        )

    def compute_binary_code(self, dimension: int = 64) -> bytes:
        """Compute a D-dimensional binary code from the record's text.

        This is a simplified version of Omega3.0's Ettin retina encoding.
        Uses hash-based bit assignment — no dense vectors, no embeddings.

        Args:
            dimension: Number of bits in the binary code (default 64)

        Returns:
            bytes object containing the binary code
        """
        if not self.text:
            return b"\x00" * ((dimension + 7) // 8)

        # Hash the text with multiple seeds to get independent bit positions
        text_bytes = self.text.encode("utf-8")
        code = bytearray((dimension + 7) // 8)

        for i in range(dimension):
            # Hash text with seed i to get a single bit
            seed_hash = hashlib.sha256(text_bytes + i.to_bytes(4, "big")).digest()
            bit = seed_hash[0] & 1
            byte_idx = i // 8
            bit_idx = i % 8
            if bit:
                code[byte_idx] |= (1 << bit_idx)

        return bytes(code)

    def hamming_distance(self, other_code: bytes) -> int:
        """Compute Hamming distance between this record's binary code and another.

        Lower distance = more similar.

        Args:
            other_code: Binary code bytes to compare against

        Returns:
            Number of differing bits
        """
        if len(self.binary_code) != len(other_code):
            raise ValueError(f"Code length mismatch: {len(self.binary_code)} vs {len(other_code)}")

        distance = 0
        for b1, b2 in zip(self.binary_code, other_code):
            xor = b1 ^ b2
            # Count set bits
            while xor:
                distance += xor & 1
                xor >>= 1
        return distance

    def __repr__(self) -> str:
        coord_str = f"({self.coord[0]},{self.coord[1]},{self.coord[2]})"
        return f"MemoryRecord(id={self.id[:8]}..., coord={coord_str}, type={self.type})"


# =====================================================================
# RetrievalResult — output of a read operation
# =====================================================================


@dataclass
class RetrievalCandidate:
    """A single candidate returned by a read operation.

    Attributes:
        record: The MemoryRecord this candidate refers to
        score: Relevance score [0.0, 1.0]
        lanes: Which recall lanes contributed this candidate
        lane_scores: Per-lane scores
        reason: Human-readable reason for this score
        verified: Whether this candidate passed the verification gate
        verdict: The verification verdict (if applicable)
        via_link: Whether reached via entity graph edge
        history: Revision history (supersedes chain)
    """

    record: MemoryRecord
    score: float = 0.0
    lanes: List[str] = field(default_factory=list)
    lane_scores: Dict[str, float] = field(default_factory=dict)
    reason: str = ""
    verified: bool = False
    verdict: Optional[Verdict] = None
    via_link: bool = False
    history: List[MemoryRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.record.id,
            "coord": self.record.coord,
            "score": self.score,
            "title": self.record.title,
            "summary": self.record.summary,
            "type": self.record.type,
            "lanes": self.lanes,
            "lane_scores": self.lane_scores,
            "reason": self.reason,
            "verified": self.verified,
            "verdict": self.verdict.value if self.verdict else None,
            "via_link": self.via_link,
        }


@dataclass
class RetrievalResult:
    """The complete result of a read operation.

    Attributes:
        candidates: List of candidates, sorted by score (descending)
        query: The original query text
        meta: Metadata about the recall operation
    """

    candidates: List[RetrievalCandidate] = field(default_factory=list)
    query: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "query": self.query,
            "meta": self.meta,
        }


# =====================================================================
# WriteResult — output of a write operation
# =====================================================================


@dataclass
class WriteResult:
    """The result of a write operation.

    Attributes:
        success: Whether the write succeeded
        record: The written MemoryRecord (if successful)
        path: File path where the record was stored (if successful)
        error: Error message (if failed)
        staged: Whether the record was staged (pending promotion)
    """

    success: bool = False
    record: Optional[MemoryRecord] = None
    path: Optional[str] = None
    error: Optional[str] = None
    staged: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "record_id": self.record.id if self.record else None,
            "path": self.path,
            "error": self.error,
            "staged": self.staged,
        }


# =====================================================================
# VerdictResult — output of the verification gate
# =====================================================================


@dataclass
class VerdictResult:
    """The result of the verification gate.

    Attributes:
        status: The verdict status
        reason: Human-readable reason
        evidence: List of evidence records supporting the verdict
        rejected_hits: Candidates that were rejected
        pressure_signal: Signal for the cognitive system (e.g., "memory_gap")
    """

    status: Verdict = Verdict.UNKNOWN
    reason: str = ""
    evidence: List[MemoryRecord] = field(default_factory=list)
    rejected_hits: List[RetrievalCandidate] = field(default_factory=list)
    pressure_signal: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "evidence_count": len(self.evidence),
            "rejected_count": len(self.rejected_hits),
            "pressure_signal": self.pressure_signal,
        }
