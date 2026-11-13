"""D-13-40 — the deterministic DME reference is the SRAM byte oracle: builder bytes ==
reference; the reference is sensitive to any row perturbation; legacy is compared
structurally only (see test_generation_determinism)."""

from __future__ import annotations

import numpy as np
import pytest

from core import timekeeper as TK
from memory.dme.lobe_layout import ENTRY_WORDS
from memory.projection import build as B
from memory.projection import generation as G
from tests.memory.projection._fixtures import redirect_timekeeper, seed_records_only
from tests.memory.projection.dme_reference import reference_rows, reference_sram_bytes


@pytest.fixture(autouse=True)
def _tk(tmp_path):
    redirect_timekeeper(tmp_path)
    yield
    TK.reset_for_tests()


def test_reference_matches_builder_and_detects_perturbation(tmp_path):
    profile = tmp_path / "profile"; profile.mkdir(); seed_records_only(profile)
    with G.projection_publication_authority(profile) as auth:
        cand = B.build_generation(profile, reason="ref", authority=auth)
    d = cand.candidate_dir
    ref = reference_sram_bytes(d)
    assert (d / "sram.bin").read_bytes() == ref
    rows = reference_rows(d)
    assert len(rows) == 4 and all(r.shape == (ENTRY_WORDS,) for r in rows)
    # perturb one seat's metadata (the field that feeds the timestamp word under the DME v7
    # observed-or-unknown rule, minting no key) ⇒ the reference bytes change (not vacuous)
    import json
    meta = json.loads((d / "sram_index.json").read_text())
    assert meta[0].get("temporal_timestamp") != "2001-02-03T04:05:06+00:00"
    meta[0]["temporal_timestamp"] = "2001-02-03T04:05:06+00:00"
    (d / "sram_index.json").write_text(json.dumps(meta))
    assert reference_sram_bytes(d) != ref
    # a token that was never registered is a loud reference failure (lookup only, never allocate)
    meta[0]["summary"] += " zzunregisteredtoken"
    (d / "sram_index.json").write_text(json.dumps(meta))
    with pytest.raises(AssertionError):
        reference_sram_bytes(d)
