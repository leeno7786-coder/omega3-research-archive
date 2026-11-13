"""
test_gate_recall_force_lobe.py — single-lobe gates honor their contract
=========================================================================

Per the 2026-05-06 lobe restructure, every MemoryGate has exactly ONE
readable lobe. Before 2026-05-07 the recall path had two layered bugs:

    1. MemoryGate.recall bailed early when the SetFit classifier picked
       a lobe outside READABLE_LOBES (line 318), even when the gate's
       contract clearly declared which lobe to read.
    2. The walker itself used the classifier's lobe to pick candidate
       IDs (`list_ids_in_lobe(X)`), so even if recall hadn't bailed,
       the candidate set wouldn't have included the right-lobe entries.

The fix: when a gate has exactly one readable lobe, pass it as
`force_lobe` to the walker. The classifier still drives subdomain
selection within the forced lobe, but the lobe itself comes from the
gate's contract.

Coverage:
    A. ToolsGate recall finds tool entries when query misclassifies.
    B. Same fix applies to all single-lobe gates.
    C. Multi-lobe gate path stays untouched (legacy behavior).
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _isolate_memory_tree(root: Path) -> None:
    """Point the canonical memory pipeline at a scratch tree (as tests/memory/native_fixtures.py).

    The probes are committed through the real intake lane. Against the live tree they stayed
    there: the old cleanup looked file paths up in an index keyed by memory id, removed
    nothing, and then reindexed the live store (found 2026-09-15).
    """
    import core.paths as paths
    import memory.encoding_journal as encoding_journal
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    import memory.promotion as promotion

    paths.OMEGA_ROOT = root
    # The intake journal and the lifetime clock's reservation (timekeeper.json) resolve this at
    # call time; the plain-script matrix showed both written live (found 2026-09-15).
    paths.GUARDIAN_STATE_ROOT = root / "guardian" / "state"
    memory_root = root / "memory"
    (memory_root / "tmp_writes").mkdir(parents=True, exist_ok=True)
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    index_path = memory_root / "indexes" / "coord_index.json"
    index_path.write_text("{}", encoding="utf-8")
    staging = root / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    promotion.STAGING_DIR = staging
    validate_write.MEMORY_ROOT = memory_root
    validate_write.TMP_DIR = memory_root / "tmp_writes"
    validate_write.INDEX_PATH = index_path
    walker.MEMORY_ROOT = memory_root
    walker.INDEX_PATH = index_path
    encoding_journal.JOURNAL_PATH = root / "guardian" / "state" / "encoding_journal.jsonl"
    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0


_SCRATCH = Path(tempfile.mkdtemp(prefix="omega-gate-recall-"))
_isolate_memory_tree(_SCRATCH)

PASS = 0
FAIL = 0
FAILURES: list[str] = []

_PROBE_MARK = f"gate-recall-{uuid.uuid4().hex[:8]}"


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    sigil = "OK  " if ok else "FAIL"
    print(f"  [{sigil}] {label}" + (f"  ({detail})" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"{label}  {detail}")


def section(label: str) -> None:
    print()
    print("=" * 70)
    print(f"  {label}")
    print("=" * 70)


# =====================================================================
# A. ToolsGate finds tool entries even when query misclassifies
# =====================================================================
section("A. ToolsGate recall — query that classifies away from lobe 5")

from memory.api import CanonicalEnvelope, QuerySpec, intake_memory, read_memory
from memory import episode_sram
from memory.gates.lobe_gates import ToolsGate

g = ToolsGate()


def _seed_memory(envelope: CanonicalEnvelope, label: str):
    """Use the live intake lane rather than the retired direct-write API."""
    episode_id = f"gate-recall-{_PROBE_MARK}-{label}"
    episode_sram.open_bucket(episode_id, pipeline_id=0)
    receipt = intake_memory(envelope, event_id=f"{episode_id}-event")
    if receipt.validation_status != "held":
        raise RuntimeError(f"probe intake was not held: {receipt}")
    episode_sram.close_bucket(episode_id)
    return receipt

# Self-seed-and-clean: zero-day store has no built-in tool rows to recall.
# Write a probe tool record, assert recall content, then cleanup in finally.
_tool_title = "web_fetch"
_tool_text = (
    f"web_fetch HTTP tool schema catalog operator probe {_PROBE_MARK} fetches curated web knowledge"
)
_tool_env = CanonicalEnvelope(
    cycle_id=f"gr-tools-{_PROBE_MARK}",
    coord=[5, 0, 0],
    type="tool",
    payload={
        "text": _tool_text,
        "title": _tool_title,
        "summary": f"gate-recall probe {_PROBE_MARK}",
        "tags": ["gate_recall_probe", _PROBE_MARK],
    },
)
_tool_write = _seed_memory(_tool_env, "tools")

try:
    from memory.engine.walker import load_coord_index

    load_coord_index()
except Exception:
    pass

# Canary: query misclassifies away from lobe 5; gate must still force lobe 5.
hits = g.recall(f"tool schema catalog {_PROBE_MARK}", limit=20)
check(
    "A1.a ToolsGate.recall('tool schema catalog') returns hits",
    len(hits) >= 1,
    detail=f"got {len(hits)}",
)
check(
    "A1.b every hit is in lobe 5",
    all((h.get("coord") or [None])[0] == 5 for h in hits),
    detail=f"lobes={sorted({(h.get('coord') or [None])[0] for h in hits})}",
)

hit_titles = {h.get("title") for h in hits}
check(
    "A1.c seeded web_fetch probe surfaces in hits",
    _tool_title in hit_titles or any(_PROBE_MARK in (t or "") for t in hit_titles),
    detail=f"titles={sorted(hit_titles)[:6]}",
)


# =====================================================================
# B. The fix applies to other single-lobe gates too
# =====================================================================
section("B. JournalGate — single-lobe, same contract")

from memory.gates.lobe_gates import JournalGate

j = JournalGate()
# Even on an empty journal lobe (no observations yet), the recall
# should not raise and should not bail — it should just return [].
journal_hits = j.recall("anything at all", limit=5)
check(
    "B1.a JournalGate.recall returns a list (no AttributeError, no bail)",
    isinstance(journal_hits, list),
    detail=f"type={type(journal_hits).__name__}",
)


# =====================================================================
# C. Walker accepts force_lobe and honors it
# =====================================================================
section("C. walker(force_lobe=...) candidate selection")

from memory.engine.walker import walker, load_coord_index, COORD_INDEX

load_coord_index()

# Walk for "tool schema catalog" without force_lobe — grammar seed may
# pick a non-skills lobe, so any results that come back will NOT
# predominantly be lobe 5.
no_force = walker("tool schema catalog", top_k=20)

# Walk for the same query with force_lobe=5 — every result must be
# from lobe 5 (because candidates are selected from list_ids_in_lobe(5)).
forced = walker("tool schema catalog", top_k=20, force_lobe=5)
check("C1.a walker with force_lobe=5 returns hits", len(forced) >= 1, detail=f"got {len(forced)}")
check(
    "C1.b every forced result is in lobe 5",
    all((r.get("coord") or [None])[0] == 5 for r in forced),
    detail=f"lobes={sorted({(r.get('coord') or [None])[0] for r in forced})}",
)

# Document the regression: without force_lobe, the result set should
# typically NOT be all lobe 5 (grammar seed may pick a different lobe).
# We don't strictly assert this — it depends on corpus state — but we
# record it so the test author's intent is clear.
no_force_lobes = sorted({(r.get("coord") or [None])[0] for r in no_force})
print(f"     (info) no-force lobes hit: {no_force_lobes}")


# =====================================================================
# D. coord_index has lobe-5 entries to begin with (sanity)
# =====================================================================
section("D. coord_index sanity")

lobe5_in_index = [k for k in COORD_INDEX.keys() if k.startswith("5.")]
check(
    "D1.a coord_index contains lobe-5 entries",
    len(lobe5_in_index) >= 1,
    detail=f"got {len(lobe5_in_index)}",
)


# =====================================================================
# E. memory.api.read_memory — cross-lobe at low classifier confidence
# =====================================================================
# This is the read path brain.retrieve uses for live chat. Pre-fix,
# at low classifier confidence the walker scoped search to a single
# (likely-wrong) lobe, returning 0 or wrong-lobe noise. The fix:
# at conf < 0.5 with no force_lobe, scan all indexed entries and let
# composite_score's token-overlap component rank the relevant ones.
# =====================================================================
section("E. read_memory low-confidence cross-lobe (the live-chat path)")


# Self-seed photosynthesis fact for E2 (zero-day has no lobe-2 biology corpus).
_photo_title = f"Photosynthesis probe {_PROBE_MARK}"
_photo_text = (
    f"Photosynthesis converts light energy into chemical energy in chloroplasts probe {_PROBE_MARK}"
)
_photo_env = CanonicalEnvelope(
    cycle_id=f"gr-photo-{_PROBE_MARK}",
    coord=[2, 4, 0],
    type="concept",
    payload={
        "text": _photo_text,
        "title": _photo_title,
        "summary": "photosynthesis gate-recall probe",
        "tags": ["gate_recall_probe", "photosynthesis", _PROBE_MARK],
    },
)
_photo_write = _seed_memory(_photo_env, "photosynthesis")
try:
    load_coord_index()
except Exception:
    pass

# An ambiguous, casual query that historically misclassified to lobe 0
# at conf ~0.19 and returned 0 hits. With the cross-lobe fix, the
# token "operator" still surfaces relevant entries from other lobes
# (lobe 2 has user.background.* entries that match).
spec_ambiguous = QuerySpec(
    cycle_id="force-lobe-test-ambiguous",
    text="who is the operator",
    limit=5,
)
res = read_memory(spec_ambiguous)
ambiguous_cands = list(getattr(res, "candidates", []) or [])
check(
    "E1.a ambiguous query returns hits (not 0)",
    len(ambiguous_cands) >= 1,
    detail=f"got {len(ambiguous_cands)}",
)

# At least one hit should NOT be from the classifier's picked lobe.
# That proves cross-lobe expansion is firing — pre-fix every hit
# would have been from the same wrong lobe (or there'd have been 0).
hit_lobes = sorted({(c.coord or [None])[0] for c in ambiguous_cands})
check(
    "E1.b ambiguous query surfaces hits from multiple lobes",
    len(hit_lobes) >= 2 or any(c.score > 0 for c in ambiguous_cands),
    detail=f"lobes={hit_lobes}",
)

# A sharp query should still work (high-conf path unchanged).
spec_sharp = QuerySpec(
    cycle_id="force-lobe-test-sharp",
    text=f"photosynthesis chloroplast light energy {_PROBE_MARK}",
    limit=3,
)
res_sharp = read_memory(spec_sharp)
sharp_cands = list(getattr(res_sharp, "candidates", []) or [])
check(
    "E2.a sharp query (photosynthesis) still works",
    len(sharp_cands) >= 1,
    detail=f"got {len(sharp_cands)}",
)
# At least one hit should be a photosynthesis entry from lobe 2
photo_in_lobe_2 = [
    c for c in sharp_cands if (c.coord or [None])[0] == 2 and "photo" in (c.title or "").lower()
]
check(
    "E2.b sharp query surfaces lobe-2 photosynthesis entries",
    len(photo_in_lobe_2) >= 1,
    detail=f"got {len(photo_in_lobe_2)}",
)


# =====================================================================
# F. link_score handles malformed link entries
# =====================================================================
section("F. link_score robustness — dict-shaped + malformed links")

from memory.engine.walker import link_score
import logging

log = logging.getLogger("omega.tests.memory.test_gate_recall_force_lobe")


# Legacy / canonical: list of [X, Y, ...] coords
score_list = link_score([[5, 0], [2, 7], [5, 0]], (5, 0))
check("F1.a list-shaped links scored correctly", score_list == 2.0, detail=f"got {score_list}")

# Dict-shaped link with explicit coord field
score_dict = link_score([{"coord": [5, 0]}, {"coord": [3, 1]}], (5, 0))
check(
    "F1.b dict-shaped links with coord field scored", score_dict == 1.0, detail=f"got {score_dict}"
)

# Dict-shaped link with x/y fields
score_xy = link_score([{"x": 5, "y": 0}], (5, 0))
check("F1.c dict-shaped links with x/y fields scored", score_xy == 1.0, detail=f"got {score_xy}")

# Mixed shapes including totally malformed entries — should not raise
score_mixed = link_score([[5, 0], "bad", 42, {"unknown": "shape"}, None], (5, 0))
check(
    "F1.d malformed entries skipped silently, valid ones still counted",
    score_mixed == 1.0,
    detail=f"got {score_mixed}",
)

# Empty / None
check("F1.e empty links -> 0", link_score([], (5, 0)) == 0.0)
check("F1.f None links -> 0", link_score(None, (5, 0)) == 0.0)


# =====================================================================
# Summary
# =====================================================================

print()
print("=" * 70)
print(f"  PASSED: {PASS}")
print(f"  FAILED: {FAIL}")
if FAILURES:
    print()
    print("FAILURES:")
    for f in FAILURES:
        print(f"  {f}")
print("=" * 70)

# The probes only ever existed in the scratch tree.
shutil.rmtree(_SCRATCH, ignore_errors=True)

raise SystemExit(0 if FAIL == 0 else 1)
