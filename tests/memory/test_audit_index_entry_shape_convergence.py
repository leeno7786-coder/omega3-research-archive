"""
test_audit_index_entry_shape_convergence.py — regression gate for the
memory HIGH: dual index-entry shapes (reindex vs patch_index, subtopic
drift).

`memory/engine/reindex.extract_minimal` (full rebuild) emitted 8 keys
incl. `subtopic` (meta.subtopic, topic-fallback) + `domain` default
"". `memory/engine/validate_write._patch_index_unsafe` (incremental
on-commit) emitted 7 keys — NO `subtopic` at all + `domain` default
None. So a freshly-committed memory's coord_index entry had no
subtopic; the first full reindex after commit ADDED it (and flipped
the domain default), so recall candidates (memory.api:392 /
walker:892) read a different subtopic/domain for the SAME memory
depending purely on index provenance — a deterministic-home /
no-spaghetti violation.

Fix: patch builds the entry via the canonical `extract_minimal` —
one entry-schema source of truth, cannot re-drift.

A1 (shape parity) + A2 (subtopic present, topic-fallback) FAIL
pre-fix. Writes only to a temp index (real coord_index untouched).
ASCII only. PASS/FAIL + sys.exit.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json
import tempfile
import logging

log = logging.getLogger("omega.tests.memory.test_audit_index_entry_shape_convergence")

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f" -- {detail}" if detail else ""))


import memory.engine.validate_write as vw  # noqa: E402
from memory.engine.reindex import extract_minimal  # noqa: E402

# A meta with subtopic ABSENT but `topic` present — exercises the
# topic-fallback that only reindex did (the precise drift).
META = {
    "id": "2.06.shapegate1",
    "coord": [2, 6, 0],
    "title": "shape gate",
    "type": "concept",
    "domain": "",  # default-domain edge
    "topic": "neuroscience_fallback",  # subtopic must fall back to this
    "flags": {"canonical": False},
}

with tempfile.TemporaryDirectory() as td:
    tmp_idx = Path(td) / "coord_index.json"
    _orig = vw.INDEX_PATH
    _orig_root = vw.MEMORY_ROOT
    vw.INDEX_PATH = tmp_idx
    # patch_index resolves the record's final path, which creates its folder; keep that
    # out of the live memory tree.
    vw.MEMORY_ROOT = Path(td) / "memory"
    try:
        vw.patch_index(dict(META))
        patched = json.load(open(tmp_idx, encoding="utf-8"))[META["id"]]
    finally:
        vw.INDEX_PATH = _orig
        vw.MEMORY_ROOT = _orig_root

canonical = extract_minimal(dict(META))

print("\n[A1] patch_index entry shape == canonical reindex shape")
check(
    "same key set as extract_minimal (pre-fix: missing 'subtopic')",
    set(patched.keys()) == set(canonical.keys()),
    detail=f"patched={sorted(patched)} canonical={sorted(canonical)}",
)

print("\n[A2] subtopic present + topic-fallback honored (pre-fix: key absent)")
check(
    "'subtopic' key present in the patched entry",
    "subtopic" in patched,
    detail=f"keys={sorted(patched)}",
)
check(
    "subtopic fell back to meta['topic'] ('neuroscience_fallback') exactly as reindex does",
    patched.get("subtopic") == "neuroscience_fallback",
    detail=f"got {patched.get('subtopic')!r}",
)

print("\n[A3] domain default convergent ('' not None) + path set")
check(
    "domain default '' (matches reindex; pre-fix patch gave None)",
    patched.get("domain") == "" and patched.get("domain") == canonical.get("domain"),
    detail=f"patched={patched.get('domain')!r} canonical={canonical.get('domain')!r}",
)
check(
    "patch entry carries a concrete path (reindex fills it later)",
    bool(patched.get("path")) and patched.get("path") != "None",
    detail=f"path={patched.get('path')!r}",
)
check(
    "id + coord preserved",
    patched.get("id") == META["id"] and patched.get("coord") == META["coord"],
)

print(f"\nTOTAL  PASS={PASS}  FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
