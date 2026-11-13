"""End-to-end regression: a vision memory INGESTED into lobe 7 is RECALLED via
modality=vision.

This pins the full robotics-relevant loop the deployment depends on:
  write_memory(CanonicalEnvelope(coord=[7,..], type="vision")) -> commits to
  lobe 7 + coord index -> read_memory(QuerySpec(modality="vision", text=labels))
  -> the vision lane (lobe 7) surfaces it.

Hermetic: redirects the whole memory store to a tmp dir (never touches Omega's
real corpus). Uses the direct-commit path (propose=False, bypass_guardian=True,
skip_embed=True) so the test exercises the real commit + index + recall without
depending on the Guardian model or the :11434 embed runtime — neither of which
the lobe-7 lane's term-overlap recall needs. commit_promotion writes the
canonical file + index before the (skipped) embed steps, so recall finds it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def hermetic_memory(tmp_path, monkeypatch):
    """Isolated memory store — real commit + index, no real-corpus writes."""
    import memory.engine.validate_write as vw
    import memory.engine.walker as walker
    import memory.promotion as prom

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    mem_root = tmp_path / "memory"
    (mem_root / "tmp_writes").mkdir(parents=True, exist_ok=True)
    (mem_root / "indexes").mkdir(parents=True, exist_ok=True)
    index_path = mem_root / "indexes" / "coord_index.json"

    monkeypatch.setattr(prom, "STAGING_DIR", staging_dir)
    monkeypatch.setattr(vw, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(vw, "TMP_DIR", mem_root / "tmp_writes")
    monkeypatch.setattr(vw, "INDEX_PATH", index_path)
    monkeypatch.setattr(walker, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(walker, "INDEX_PATH", index_path)

    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0
    yield mem_root
    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0


def _vision_envelope():
    from memory.api import CanonicalEnvelope

    return CanonicalEnvelope(
        cycle_id="vis-ingest-recall",
        coord=[7, 0, 0],
        type="vision",
        payload={
            "text": "vision observation: an orange forklift and a cardboard box on the warehouse floor",
            "title": "warehouse forklift box",
            "summary": "vision scene observation",
            "tags": ["vision", "forklift", "box", "warehouse"],
        },
    )


def test_vision_memory_ingests_into_lobe_7(hermetic_memory):
    from memory.api import write_memory
    from memory.engine.walker import load_coord_index, COORD_INDEX

    rec = write_memory(_vision_envelope(), propose=False, bypass_guardian=True, skip_embed=True)

    assert rec.validation_status == "committed", rec.message
    assert str(rec.path).startswith("7."), f"vision write must land in lobe 7, got {rec.path!r}"

    load_coord_index()
    lobe7 = [k for k in COORD_INDEX if k.startswith("7.")]
    assert lobe7, "ingested vision memory is not indexed under lobe 7"


def test_vision_memory_never_recalls_through_the_general_engine(hermetic_memory):
    # THE RECALL-LANE RULE (operator 2026-07-18): lobe 7 (vision) recalls only
    # through the DEDICATED visual pipeline (visual recall + ingest + face
    # models — pending). The general engine must return NOTHING for lobe-7
    # content even on an explicit vision-modality query. INGEST is unchanged
    # (the test above pins the lobe-7 write); this test INVERTED with the
    # ruling — it used to pin general-lane vision recall as the feature.
    from memory.api import write_memory, read_memory, QuerySpec
    from memory.engine.walker import load_coord_index

    rec = write_memory(_vision_envelope(), propose=False, bypass_guardian=True, skip_embed=True)
    assert rec.validation_status == "committed", rec.message
    load_coord_index()

    res = read_memory(
        QuerySpec(
            cycle_id="vis-recall",
            text="forklift box warehouse",
            limit=5,
            modality="vision",
        )
    )
    cands = list(getattr(res, "candidates", []) or [])
    leaked = [c for c in cands if (c.coord or [None])[0] == 7]
    assert not leaked, (
        "lobe-7 content breached the vision recall boundary: "
        f"{[(c.coord, c.title) for c in leaked]}"
    )
