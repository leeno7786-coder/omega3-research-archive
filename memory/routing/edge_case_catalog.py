"""Append-only edge-case catalog for tier-3 ``_unsorted`` holds (Phase 7)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.paths import OMEGA_ROOT

_CATALOG_LOCK = threading.Lock()
_DEFAULT_PATH = OMEGA_ROOT / "guardian" / "state" / "routing_edge_catalog.jsonl"


def catalog_path(path: Optional[Path] = None) -> Path:
    return path or _DEFAULT_PATH


def append_entry(
    *,
    text: str,
    lobe: int,
    coord: List[int],
    label_id: str = "",
    source: str = "",
    expected_coord: Optional[List[int]] = None,
    path: Optional[Path] = None,
) -> None:
    """Record one tier-3 undecided hold for LFM go/no-go metrics."""
    entry: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "text": text[:2000],
        "lobe": lobe,
        "coord": list(coord),
        "label_id": label_id,
        "source": source,
        "tier": "unsorted_hold",
    }
    if expected_coord is not None:
        entry["expected_coord"] = list(expected_coord)
    target = catalog_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with _CATALOG_LOCK, target.open("a", encoding="utf-8") as fh:
        fh.write(line)


def load_entries(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    target = catalog_path(path)
    if not target.is_file():
        return []
    out: List[Dict[str, Any]] = []
    with target.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def unsorted_rate(
    entries: Optional[List[Dict[str, Any]]] = None,
    *,
    path: Optional[Path] = None,
) -> float:
    """Fraction of catalog entries that are tier-3 holds (always 1.0 if non-empty)."""
    rows = entries if entries is not None else load_entries(path)
    if not rows:
        return 0.0
    return 1.0
