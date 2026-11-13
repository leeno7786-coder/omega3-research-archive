"""
Standalone Memory System — inspired by Omega3.0's Dewey 3D cortex.

A minimal, dependency-free memory substrate that any agent can plug in.
Extracts the key architectural principles:

  - Deterministic coordinate system (X, Y, Z) for structured storage
  - Lane-based recall with posture-aware arbitration
  - Strength indexing with reinforcement and decay
  - Verification gate for candidate admission
  - Binary semantic encoding (no dense vectors)
  - Atomic file persistence (crash-safe)
  - Source provenance tracking

Usage:

    from standalone_memory import MemoryStore

    store = MemoryStore(path="./my_memory")
    store.write("What is Python?", coord=[2, 10, 0], type="knowledge")
    results = store.read("What programming language is Python?")
    for hit in results.candidates:
        print(f"{hit.title} (score={hit.score:.3f})")
"""

from __future__ import annotations

__version__ = "0.1.0"

from .memory import MemoryStore
from .types import (
    MemoryRecord,
    RetrievalResult,
    RetrievalCandidate,
    WriteResult,
    Verdict,
)

__all__ = [
    "MemoryStore",
    "MemoryRecord",
    "RetrievalResult",
    "RetrievalCandidate",
    "WriteResult",
    "Verdict",
]
