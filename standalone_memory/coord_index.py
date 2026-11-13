"""
Coordinate index — fast lookup by memory ID and coordinate.

Maintains an in-memory index mapping memory IDs to their coordinates
and file paths. Supports:
- O(1) lookup by memory ID
- Coordinate-based filtering
- Atomic updates (crash-safe)
- Snapshot mode for consistent reads

This is the equivalent of Omega3.0's coord_index.json but simplified.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("standalone_memory.coord_index")


class CoordIndex:
    """In-memory coordinate index with atomic updates.

    Maps memory IDs to their (X, Y, Z) coordinates and file paths.
    Thread-safe: all operations are locked.
    """

    def __init__(self, path: Optional[str] = None):
        """Initialize the coordinate index.

        Args:
            path: Path to persist the index to disk. If None, in-memory only.
        """
        self._entries: Dict[str, Dict[str, Any]] = {}  # id -> {coord, path, title, type, ...}
        self._lock = threading.RLock()
        self._path = path
        self._generation = 0  # Monotonic generation counter

        if path:
            self._load()

    def _load(self) -> None:
        """Load index from disk."""
        if not self._path:
            return
        try:
            p = Path(self._path)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    with self._lock:
                        self._entries = data
                        self._generation += 1
                        log.debug("Loaded %d index entries from %s", len(self._entries), self._path)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Coord index load failed (%s) — starting fresh", exc)

    def _save(self) -> None:
        """Persist index to disk (atomic write)."""
        if not self._path:
            return
        try:
            p = Path(self._path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            json.dump(self._entries, tmp.open("w", encoding="utf-8"), indent=2)
            tmp.replace(p)  # atomic swap
            log.debug("Saved %d index entries to %s", len(self._entries), self._path)
        except OSError as exc:
            log.warning("Coord index save failed (%s)", exc)

    def add(
        self,
        memory_id: str,
        coord: List[int],
        file_path: str,
        title: str = "",
        memory_type: str = "episodic",
        **extra: Any,
    ) -> None:
        """Add or update an entry in the index.

        Args:
            memory_id: Unique memory identifier
            coord: [X, Y, Z] coordinate
            file_path: Filesystem path to the memory record
            title: Short title
            memory_type: Memory type string
            **extra: Additional metadata fields
        """
        with self._lock:
            self._entries[memory_id] = {
                "coord": coord[:3] if len(coord) >= 3 else coord + [0] * (3 - len(coord)),
                "path": file_path,
                "title": title,
                "type": memory_type,
                "generation": self._generation,
                **extra,
            }
            self._generation += 1
            self._save()

    def remove(self, memory_id: str) -> bool:
        """Remove an entry from the index.

        Args:
            memory_id: Unique memory identifier

        Returns:
            True if the entry was found and removed
        """
        with self._lock:
            if memory_id in self._entries:
                del self._entries[memory_id]
                self._generation += 1
                self._save()
                return True
            return False

    def get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Get an entry by memory ID.

        Args:
            memory_id: Unique memory identifier

        Returns:
            Entry dict with coord, path, title, type, or None
        """
        with self._lock:
            return self._entries.get(memory_id)

    def get_coord(self, memory_id: str) -> Optional[List[int]]:
        """Get the coordinate for a memory ID.

        Args:
            memory_id: Unique memory identifier

        Returns:
            [X, Y, Z] coordinate list, or None
        """
        entry = self.get(memory_id)
        if entry:
            return entry.get("coord")
        return None

    def filter_by_lobe(self, lobe: int) -> List[Dict[str, Any]]:
        """Get all entries in a specific lobe.

        Args:
            lobe: Lobe ID (X coordinate)

        Returns:
            List of entry dicts in the specified lobe
        """
        with self._lock:
            return [
                entry for entry in self._entries.values()
                if len(entry.get("coord", [])) > 0 and entry["coord"][0] == lobe
            ]

    def filter_by_subdomain(self, lobe: int, subdomain: int) -> List[Dict[str, Any]]:
        """Get all entries in a specific lobe and subdomain.

        Args:
            lobe: Lobe ID (X coordinate)
            subdomain: Subdomain ID (Y coordinate)

        Returns:
            List of entry dicts matching the lobe and subdomain
        """
        with self._lock:
            return [
                entry for entry in self._entries.values()
                if len(entry.get("coord", [])) > 1
                and entry["coord"][0] == lobe
                and entry["coord"][1] == subdomain
            ]

    def filter_by_type(self, memory_type: str) -> List[Dict[str, Any]]:
        """Get all entries of a specific type.

        Args:
            memory_type: Memory type string

        Returns:
            List of entry dicts of the specified type
        """
        with self._lock:
            return [
                entry for entry in self._entries.values()
                if entry.get("type") == memory_type
            ]

    def search_by_text(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search entries by text match in title or type.

        Simple substring matching — no full-text search.

        Args:
            query: Search query string
            limit: Maximum results

        Returns:
            List of matching entry dicts, sorted by relevance
        """
        query_lower = query.lower()
        with self._lock:
            matches = []
            for entry in self._entries.values():
                title = (entry.get("title") or "").lower()
                mem_type = (entry.get("type") or "").lower()

                # Score: title matches weigh more than type matches
                score = 0.0
                if query_lower in title:
                    score += 2.0
                if query_lower in mem_type:
                    score += 1.0

                if score > 0:
                    matches.append((score, entry))

            # Sort by score descending, then by ID for determinism
            matches.sort(key=lambda x: (-x[0], x[1].get("id", "")))
            return [entry for _, entry in matches[:limit]]

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """Get all entries (for iteration).

        Returns:
            Copy of all entry dicts
        """
        with self._lock:
            return dict(self._entries)

    def get_all_ids(self) -> List[str]:
        """Get all memory IDs in the index.

        Returns:
            List of memory ID strings
        """
        with self._lock:
            return list(self._entries.keys())

    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics.

        Returns:
            Dict with counts by lobe, type, and total
        """
        with self._lock:
            by_lobe: Dict[int, int] = {}
            by_type: Dict[str, int] = {}

            for entry in self._entries.values():
                coord = entry.get("coord", [])
                if coord:
                    lobe = coord[0]
                    by_lobe[lobe] = by_lobe.get(lobe, 0) + 1

                mem_type = entry.get("type", "unknown")
                by_type[mem_type] = by_type.get(mem_type, 0) + 1

            return {
                "total": len(self._entries),
                "by_lobe": by_lobe,
                "by_type": by_type,
                "generation": self._generation,
            }

    def clear(self) -> None:
        """Clear all entries."""
        with self._lock:
            self._entries.clear()
            self._generation += 1
            self._save()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, memory_id: str) -> bool:
        with self._lock:
            return memory_id in self._entries
