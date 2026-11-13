"""D-13-40 — an INDEPENDENT deterministic reference for the generation DME SRAM bytes.

Given a candidate/generation directory (``coordinate_registry.json``, ``sram_index.json``,
``sram_meta.json``) it recomputes every row from the FINALIZED registry by LOOKUP only
(no allocation), with the coordinate tuple list SORTED before ``pack_entry`` — the
D-13-27/40 law — and returns the bytes the builder must match byte-for-byte across
fresh-process hash seeds. The row timestamp is the DME v7 rule (observed-or-unknown:
``timestamp_epoch_or_zero(seat.temporal_timestamp)``), never a content-hash pseudo-date.
It shares only the leaf primitives (tokenizer, packer, temporal authority) with the
adapter; the seating/ordering logic is its own.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from memory.dme.lobe_layout import ENTRY_WORDS
from memory.dme.packer import pack_entry
from memory.temporal_authority import timestamp_epoch_or_zero


def _fingerprint(memory_id: str) -> int:
    return int(hashlib.sha256(memory_id.encode("utf-8")).hexdigest()[:16], 16)


def reference_rows(candidate_dir: Path) -> list[np.ndarray]:
    from memory.dme.adapter import _coord_key, _tokens
    from memory.dme.lobe_layout import coordinate_tuple

    candidate_dir = Path(candidate_dir)
    registry = json.loads((candidate_dir / "coordinate_registry.json").read_text(encoding="utf-8"))
    addresses = {k: tuple(int(v) for v in addr) for k, addr in dict(registry.get("addresses") or {}).items()}
    rows_meta = json.loads((candidate_dir / "sram_index.json").read_text(encoding="utf-8"))
    out: list[np.ndarray] = []
    for meta in rows_meta:
        coord = tuple(int(v) for v in (meta.get("coord") or ()))
        lobe = coord[0] if coord else 0
        keys: list[str] = []
        triple = coordinate_tuple(coord) if coord else None
        if triple is not None:
            keys.append(_coord_key(triple))
        text = " ".join((meta.get("memory_id", ""), meta.get("title", ""), meta.get("summary", ""), meta.get("domain", ""),
                         meta.get("subtopic", ""), " ".join(meta.get("tags") or ()), " ".join(meta.get("sense_ids") or ())))
        keys.extend(sorted(_tokens(text)))
        coords = sorted({addresses[k.lower().strip()] for k in keys if k.lower().strip() in addresses})
        # every key must already be registered in the finalized registry (lookup only, never allocate)
        missing = [k for k in keys if k.lower().strip() not in addresses]
        if missing:
            raise AssertionError(f"reference: keys not in the finalized registry: {missing[:5]}")
        out.append(pack_entry(_fingerprint(str(meta.get("memory_id") or "")),
                              timestamp_epoch_or_zero(meta.get("temporal_timestamp") or ""),   # DME v7: observed-or-unknown
                              str(meta.get("source_id") or "gate:memory"), [tuple(c) for c in coords]))
    return out


def reference_sram_bytes(candidate_dir: Path) -> bytes:
    rows = reference_rows(candidate_dir)
    sram = np.vstack(rows) if rows else np.zeros((0, ENTRY_WORDS), dtype=np.uint64)
    return sram.tobytes()
