"""Rebuild the derived face anchor cache from canonical DeepSleep records."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Optional


def warm_anchor_store(store: Any = None, *, root: Optional[Path] = None) -> Dict[str, int]:
    """Project canonical face samples once; corrupt truth or failed writes raise.

    The canonical record ID commits atomically with the derived anchor mean,
    making retries and cold boot repeatable without inflating sample counts.
    ``root`` is a product root used by relocated-profile tests.
    """
    if store is None:
        from runtime.face.anchor_store import get_anchor_store

        store = get_anchor_store()
    if root is None:
        from core.paths import MEMORY_ROOT

        memory_root = MEMORY_ROOT
    else:
        memory_root = Path(root) / "memory"
    directory = memory_root / "700_vision" / "face_embeddings"
    report = {"scanned": 0, "restored": 0, "skipped": 0, "invalid": 0}
    if not directory.exists():
        return report
    samples = []
    for path in sorted(directory.rglob("*.json")):
        # Retired transport-written sidecars are not canonical memory.
        if path.name.endswith(".embedding.json"):
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError(f"face canonical record is not an object: {path}")
        if record.get("type") != "face_memory":
            continue
        report["scanned"] += 1
        face = (record.get("enrichment") or {}).get("face_memory")
        if not isinstance(face, dict):
            raise ValueError(f"canonical face payload missing: {path}")
        name = str(face.get("name") or "").strip()
        embedding = face.get("embedding")
        memory_id = str(record.get("id") or "")
        if (not memory_id or not name or not isinstance(embedding, list) or len(embedding) != 512
                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in embedding)):
            raise ValueError(f"invalid canonical face sample: {path}")
        samples.append((str(record.get("timestamp_created") or ""), memory_id, name, embedding))
    # Validate the whole corpus before mutating the derived cache; stable order
    # keeps capped running means identical during incremental and cold rebuilds.
    report.update(store.project_canonical(samples))
    return report
