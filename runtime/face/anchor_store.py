"""
Face Anchor Store — persistent name -> 512-dim embedding map
============================================================

Tiny JSON-backed key/value store. Each entry:
    name -> {embedding: [512 floats], anchored_at: iso, sample_count: int}

Identification is cosine similarity vs every stored embedding; the
anchor with the highest sim above THRESHOLD wins. Below threshold ->
"unknown". This is enough for a single-operator Windows-Hello-style
demo; an N-operator deployment would want a vector index.

The store lives at guardian/state/face_anchors.json so it survives
boots. Writes go through a temp-file rename so a crash mid-write
cannot corrupt the file.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from core.paths import GUARDIAN_STATE_ROOT

log = logging.getLogger("omega.runtime.face.anchor_store")


# Cosine similarity threshold above which a candidate is considered
# the operator. ArcFace embeddings on the same identity typically sit
# around 0.55-0.85; cross-identity sits around 0.05-0.30.
#
# +-- CROSS-PROCESS CONTRACT - DRIFT IS SILENT ----------------------+
# | This value MUST equal the runtime service's `_face_db_match` similarity|
# | cutoff (currently 0.4). They are two independently-hardcoded     |
# | copies in two separate processes/bundles; nothing enforces       |
# | equality at runtime. If they drift, cold-boot recognition (via   |
# | core.face_recall_warmup -> this store) and the warm runtime path |
# | disagree on identity for the SAME face - a silent split.         |
# | ANY change here MUST be mirrored in the runtime service `_face_db_match`|
# | in the same change. Live drift-enforcement (brain reads the      |
# | runtime's effective threshold at warmup) is a deferred build -   |
# | tracked in SESSION_FOLLOWUPS_2026-05-18 (cannot be validated      |
# | offline under quarantine; needs a runtime threshold endpoint).   |
# +------------------------------------------------------------------+
# Harmonized 2026-05-14 per build_log 0117 "Threshold harmonization".
RECOGNITION_THRESHOLD = 0.4


class AnchorStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or (GUARDIAN_STATE_ROOT / "face_anchors.json")
        self._lock = threading.RLock()
        self._anchors: dict[str, dict] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        try:
            if self._path.exists():
                self._anchors = json.loads(self._path.read_text(encoding="utf-8"))
            else:
                self._anchors = {}
        except Exception as exc:
            log.warning("anchor store read failed (%s); starting empty", exc)
            from guardian.terminal_feedback import emit_data

            emit_data(
                "runtime",
                "anchor_store_load_failed",
                ok=False,
                error=str(exc),
            )
            self._anchors = {}
        self._loaded = True

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._anchors, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def names(self) -> list[str]:
        with self._lock:
            self._ensure_loaded()
            return sorted(self._anchors.keys())

    def count(self) -> int:
        with self._lock:
            self._ensure_loaded()
            return len(self._anchors)

    def project_canonical(self, samples: list[tuple]) -> dict:
        """Atomically rebuild each canonical identity in capture-time order.

        Replaying only unseen IDs is insufficient: an older capture arriving
        later changes the capped running mean's order. Recompute from truth
        each time; publish the complete projection and its IDs together.
        """
        projected = {}
        all_ids = set()
        for timestamp, memory_id, name, embedding in sorted(samples):
            if memory_id in all_ids:
                raise ValueError(f"duplicate canonical face record: {memory_id}")
            all_ids.add(memory_id)
            emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
            if not memory_id or not name or emb.shape[0] != 512 or not np.isfinite(emb).all():
                raise ValueError("invalid canonical face sample")
            emb = emb / (np.linalg.norm(emb) + 1e-9)
            previous = projected.get(name)
            if previous:
                count = min(previous["sample_count"], 5)
                emb = (np.asarray(previous["embedding"], dtype=np.float32) * count + emb) / (count + 1)
                emb = emb / (np.linalg.norm(emb) + 1e-9)
            projected[name] = {
                "embedding": emb.tolist(), "sample_count": count + 1 if previous else 1,
                "anchored_at": previous["anchored_at"] if previous else timestamp,
                "updated_at": timestamp,
                "canonical_ids": [*(previous or {}).get("canonical_ids", []), memory_id],
            }
        with self._lock:
            self._ensure_loaded()
            old_ids = {source for row in self._anchors.values() for source in row.get("canonical_ids", [])}
            next_anchors = {**self._anchors, **projected}
            if next_anchors != self._anchors:
                previous_anchors = self._anchors
                self._anchors = next_anchors
                try:
                    self._persist()
                except Exception:
                    self._anchors = previous_anchors
                    raise
            return {"restored": len(all_ids - old_ids), "skipped": len(all_ids & old_ids)}

    def enroll(self, name: str, embedding: np.ndarray) -> dict:
        """Add or update an anchor. Existing-name case averages the
        new embedding into the old one (capped at 5 samples) so the
        anchor improves with repeated enrollments."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name required")
        emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
        if emb.shape[0] != 512:
            raise ValueError(f"embedding must be 512-dim, got {emb.shape[0]}")
        emb = emb / (np.linalg.norm(emb) + 1e-9)

        with self._lock:
            self._ensure_loaded()
            existing = self._anchors.get(name)
            if existing and "embedding" in existing:
                old = np.asarray(existing["embedding"], dtype=np.float32)
                samples = int(existing.get("sample_count", 1))
                # Running mean of unit vectors (renormalised after).
                samples = min(samples, 5)
                merged = (old * samples + emb) / (samples + 1)
                merged = merged / (np.linalg.norm(merged) + 1e-9)
                self._anchors[name] = {
                    "embedding": merged.tolist(),
                    "anchored_at": existing.get("anchored_at"),
                    "updated_at": _iso_now(),
                    "sample_count": samples + 1,
                }
            else:
                self._anchors[name] = {
                    "embedding": emb.tolist(),
                    "anchored_at": _iso_now(),
                    "updated_at": _iso_now(),
                    "sample_count": 1,
                }
            try:
                self._persist()
            except Exception:
                # Do not let an in-process retry reuse an unpersisted mean.
                if existing is None:
                    self._anchors.pop(name, None)
                else:
                    self._anchors[name] = existing
                raise
            out = {
                "name": name,
                "sample_count": self._anchors[name]["sample_count"],
                "anchored_at": self._anchors[name]["anchored_at"],
            }
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "runtime",
                    "anchor_enroll",
                    name=name,
                    sample_count=out["sample_count"],
                    ok=True,
                )
            except Exception:
                pass
            return out

    def identify(self, embedding: np.ndarray) -> dict:
        """Return the best-match anchor + cosine sim. If no anchors
        are stored or no candidate beats RECOGNITION_THRESHOLD, returns
        {recognized_name: None, confidence: float, candidates: [...]}."""
        emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
        emb = emb / (np.linalg.norm(emb) + 1e-9)
        with self._lock:
            self._ensure_loaded()
            if not self._anchors:
                return {
                    "recognized_name": None,
                    "confidence": 0.0,
                    "candidates": [],
                    "reason": "no_anchors",
                }
            scored: list[tuple[str, float]] = []
            for name, entry in self._anchors.items():
                stored = np.asarray(entry["embedding"], dtype=np.float32)
                sim = float(np.dot(emb, stored))
                scored.append((name, sim))
            scored.sort(key=lambda x: x[1], reverse=True)
            top_name, top_sim = scored[0]
            recognized = top_name if top_sim >= RECOGNITION_THRESHOLD else None
            out = {
                "recognized_name": recognized,
                "confidence": top_sim,
                "candidates": [{"name": n, "confidence": s} for n, s in scored[:5]],
                "threshold": RECOGNITION_THRESHOLD,
            }
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "runtime",
                    "anchor_identify",
                    recognized_name=recognized,
                    confidence=top_sim,
                    candidate_count=len(scored),
                    ok=True,
                )
            except Exception:
                pass
            return out

    def forget(self, name: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            if name in self._anchors:
                del self._anchors[name]
                self._persist()
                return True
            return False


_singleton: Optional[AnchorStore] = None


def get_anchor_store() -> AnchorStore:
    global _singleton
    if _singleton is None:
        _singleton = AnchorStore()
    return _singleton


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
