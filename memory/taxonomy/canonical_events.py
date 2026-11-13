"""Stage 2: Read memory events directly from canonical memory coordinate index."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.paths import OMEGA_ROOT
from memory.taxonomy.schemas import MemoryEvent


def read_events_from_canonical(
    start: str | None = None,
    end: str | None = None,
    event_kind: str | None = None,
    episode_id: str | None = None,
    root: Any = None,
) -> list[MemoryEvent]:
    """Walks the coord_index snapshot for canonical=true records in lobes 4, 9,
    reconstructing MemoryEvent objects from each. Replaces ledger.read_events.
    """
    if root is not None:
        memory_root = Path(root) / "memory"
    else:
        memory_root = OMEGA_ROOT / "memory"

    index_path = memory_root / "indexes" / "coord_index.json"
    if not index_path.exists():
        return []

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            coord_index = json.load(f)
    except Exception:
        return []

    candidates = []
    for mid, entry in coord_index.items():
        coord = entry.get("coord")
        if not coord or not isinstance(coord, list) or len(coord) < 3:
            continue
        lobe = coord[0]
        if lobe not in (4, 9):
            continue

        path_str = entry.get("path")
        if not path_str:
            continue

        p = Path(path_str)
        if not p.is_absolute():
            # Join at read time so profile-relative coord_index paths work
            # in the worker path (root=None ⇒ OMEGA_ROOT is the profile).
            base = Path(root) if root is not None else OMEGA_ROOT
            p = base / p

        if not p.exists():
            continue

        try:
            with open(p, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue

        enrichment = meta.get("enrichment") or {}
        if not enrichment.get("canonical"):
            continue

        # Extract attributes
        event_kind_val = enrichment.get("event_kind")
        episode_id_val = enrichment.get("episode_id")
        created_at = enrichment.get("closed_at") or meta.get("timestamp_created") or ""

        # Filter
        if event_kind is not None and event_kind_val != event_kind:
            continue
        if episode_id is not None and episode_id_val != episode_id:
            continue
        if start is not None and created_at < start:
            continue
        if end is not None and created_at > end:
            continue

        # Reconstruct MemoryEvent
        # Depending on the coordinate, the payload is structured differently
        # For lobe 9 [9, 1, 0] full episode: text is full slot JSON
        # For lobe 9 [9, 3, 0]/[9, 4, 0] consolidation/capability: text is full event dict JSON
        # For lobe 4: payload is stored in enrichment["event_payload"]
        if meta.get("type") == "episode_event":
            # Post-seal terminus events are typed supplements, not a replacement
            # episode body. The complete event lives in enrichment; text is a preview.
            payload = enrichment
            event_id = enrichment.get("event_id") or mid
            source = enrichment.get("event_source") or "deepsleep.episode_event"
        elif coord == [9, 1, 0]:
            try:
                payload = json.loads(meta.get("text", "{}"))
            except Exception:
                payload = {}
            event_id = enrichment.get("event_id") or f"episode-{episode_id_val}"
            source = meta.get("provenance", {}).get("author") or "temporal_episode"
        elif coord[0] == 9 and coord[1] in (3, 4):
            try:
                event_dict = json.loads(meta.get("text", "{}"))
                payload = event_dict.get("payload") or {}
                event_id = event_dict.get("event_id") or enrichment.get("event_id") or mid
                source = event_dict.get("source") or "temporal_consolidation"
            except Exception:
                payload = {}
                event_id = enrichment.get("event_id") or mid
                source = "temporal_consolidation"
        else:
            # Lobe 4 Situational
            payload = enrichment.get("event_payload") or {}
            event_id = enrichment.get("event_id") or mid
            source = enrichment.get("event_source") or "situational"

        ev = MemoryEvent(
            event_id=event_id,
            event_kind=event_kind_val or meta.get("type") or "observation",
            episode_id=episode_id_val or "",
            source=source,
            created_at=created_at,
            content_hash=meta.get("content_hash") or f"hash_{event_id}",
            payload=payload,
        )
        candidates.append((created_at, coord, mid, ev))

    # Stable order by (created_at, coord, id)
    candidates.sort(key=lambda x: (x[0], x[1], x[2]))
    return [x[3] for x in candidates]
