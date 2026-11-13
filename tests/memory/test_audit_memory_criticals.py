"""
test_audit_memory_criticals.py - regression gates for the three memory
CRITICALs the 2026-05-18 fine-grained audit found + this pass fixed.

Each gate is designed to FAIL on the pre-fix code and PASS on the fix
(the unit-green != wired lesson: a silent bug means there was no gate).

  F1  recall_lanes strength lane was a SILENT NO-OP: it called the
      non-existent `si.get(mid)`; the AttributeError was swallowed by a
      bare `except`, so every memory scored 0 and the lane returned []
      in every strength_bias posture (identity/persona resolution).
      Fix: `si.get_strength(mid)` + a narrow (TypeError, ValueError)
      except so a future structural break can't hide again.

  F2  reindex (Draft7Validator on memory_schema.json,
      additionalProperties:false) skipped every record carrying the
      revision-chain fields the chain writer (supersedes.py) stamps
      (`supersedes`/`superseded_by`/`version`) -> coord_index data
      loss of the deterministic-recall guarantee.

  F3  atomic_write's validator (memory.engine.validate_write) rejected
      the same chained shape -> ValueError -> rollback on every
      re-encode of a chained record.
      Fix (F2+F3, one root): the 3 chain fields added to
      memory_schema.json properties (NOT required) -> schema made
      honest about what the chain writer legitimately produces;
      additionalProperties:false integrity preserved.

ASCII only (Windows cp1252 stdout). PASS/FAIL counter + sys.exit.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import logging

log = logging.getLogger("omega.tests.memory.test_audit_memory_criticals")

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


# ====================================================================
# F1 - strength lane is no longer a silent no-op
# ====================================================================
print("\n[F1] recall_lanes strength lane returns hits (not a silent no-op)")

import memory.strength_index as si_mod  # noqa: E402
from memory.recall_compiler import compile_recall_plan  # noqa: E402
from memory.recall_lanes import _run_strength_lane  # noqa: E402
from memory.strength_index import StrengthIndex  # noqa: E402

# API-contract lock: the bug was calling .get() on a class that only
# exposes get_strength(). Pin it so the wrong-method bug can't return.
check(
    "StrengthIndex exposes get_strength()", callable(getattr(StrengthIndex, "get_strength", None))
)
check(
    "StrengthIndex does NOT expose .get() (the bug called this)",
    getattr(StrengthIndex, "get", None) is None,
)


class _QS:
    def __init__(self, text: str) -> None:
        self.cycle_id = "test-audit-crit"
        self.text = text
        self.limit = 10
        self.modality = "text"
        self.task_scope = None
        self.recall_policy_id = None
        self.domain = None


class _StubSI:
    """Stub StrengthIndex: exposes get_strength (correct API) and
    deliberately NO .get — so pre-fix `si.get(mid)` would AttributeError
    (then be swallowed -> []), while the fix scores real hits."""

    def get_strength(self, mid: str) -> float:
        return 0.9


_IDX = {
    "0.0.aaa": {"id": "0.0.aaa", "coord": [0, 0, 0], "path": "FAKE/a.json"},
    "1.0.bbb": {"id": "1.0.bbb", "coord": [1, 0, 0], "path": "FAKE/b.json"},
}
_META = {
    "FAKE/a.json": {
        "id": "0.0.aaa",
        "coord": [0, 0, 0],
        "type": "identity",
        "title": "operator identity",
        "summary": "the operator is Noah",
        "text": "Operator is Noah.",
        "tags": ["operator"],
        "flags": {},
    },
    "FAKE/b.json": {
        "id": "1.0.bbb",
        "coord": [1, 0, 0],
        "type": "persona",
        "title": "operator persona",
        "summary": "operator persona",
        "text": "Persona of the operator.",
        "tags": ["operator"],
        "flags": {},
    },
}

plan = compile_recall_plan(_QS("who is the operator"))
posture = getattr(plan, "posture", None)
check(
    "fixture precondition: plan posture has strength_bias (the audit's dead path)",
    bool(getattr(posture, "strength_bias", None)),
    detail=f"strength_bias={getattr(posture, 'strength_bias', None)}",
)

_orig_ensure = si_mod.ensure_strength_index
si_mod.ensure_strength_index = lambda: _StubSI()
try:
    hits = _run_strength_lane(
        plan,
        _IDX,
        lambda p: _META.get(p),
        plan_terms=set(getattr(plan, "exact_terms", []) or []),
    )
finally:
    si_mod.ensure_strength_index = _orig_ensure

check(
    "strength lane returns NON-EMPTY hits with a populated index "
    "(pre-fix: silent [] via swallowed AttributeError)",
    isinstance(hits, list) and len(hits) > 0,
    detail=f"got {len(hits) if isinstance(hits, list) else hits} hits",
)
check(
    "each strength hit carries a real si= score in its reason",
    all("si=0.90" in h.reason for h in hits) if hits else False,
    detail=(hits[0].reason if hits else "no hits"),
)


# ====================================================================
# F2 + F3 - schema is honest about the revision chain (no data loss /
# no re-encode rollback). Same shape supersedes.py stamps.
# ====================================================================
print(
    "\n[F2/F3] memory_schema accepts the revision chain "
    "(reindex keeps it; atomic_write doesn't roll back)"
)

from jsonschema import Draft7Validator  # noqa: E402
from memory.engine.validate_write import _get_validator  # noqa: E402

_SCHEMA_PATH = ROOT / "schema" / "memory_schema.json"
_schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
Draft7Validator.check_schema(_schema)
_reindex_v = Draft7Validator(_schema)  # exactly what reindex.py builds
_aw_v = _get_validator()  # exactly what atomic_write uses

_base = {
    "id": "1.00.rev2",
    "coord": [1, 0, 0],
    "type": "concept",
    "title": "color preference",
    "summary": "operator prefers teal",
    "provenance": {
        "origin_id": "o",
        "origin_type": "user",
        "author": "noah",
        "ingested_at": "2026-05-18T00:00:00Z",
        "confidence": 0.9,
    },
    "flags": {"canonical": True},
    "audit": [{"when": "2026-05-18T00:00:00Z", "what": "create", "by": "sys"}],
}
# the EXACT chain shape memory/supersedes.py stamps
_chained = copy.deepcopy(_base)
_chained.update({"supersedes": "1.00.rev1", "superseded_by": None, "version": 2})
_head = copy.deepcopy(_base)
_head.update({"supersedes": None, "superseded_by": "1.00.rev3", "version": 1})

check(
    "F2: reindex validator ACCEPTS a superseding record "
    "(pre-fix: skipped -> coord_index data loss)",
    list(_reindex_v.iter_errors(_chained)) == [],
    detail=str([e.message for e in _reindex_v.iter_errors(_chained)]),
)
check(
    "F2: reindex validator ACCEPTS a superseded head record",
    list(_reindex_v.iter_errors(_head)) == [],
)
check(
    "F3: atomic_write validator ACCEPTS the chained record "
    "(pre-fix: ValueError -> rollback on re-encode)",
    list(_aw_v.iter_errors(_chained)) == [],
    detail=str([e.message for e in _aw_v.iter_errors(_chained)]),
)
check(
    "back-compat: a legacy un-chained record still validates", list(_aw_v.iter_errors(_base)) == []
)

_bad = copy.deepcopy(_base)
_bad["totally_unknown_key"] = 1
check(
    "integrity: additionalProperties:false still rejects unknown keys "
    "(schema made honest, NOT loosened)",
    len(list(_aw_v.iter_errors(_bad))) >= 1,
)

print(f"\nTOTAL  PASS={PASS}  FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
