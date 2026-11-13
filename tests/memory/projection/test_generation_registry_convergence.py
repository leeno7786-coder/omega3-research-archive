"""D-13-39 — identity binds ONLY the finalized effective registry: build-from-R0 that
finalizes R1, then an unchanged rebuild-from-R1, yield ONE generation id; a changed
assignment yields a different id; pending keys never move existing assignments."""

from __future__ import annotations

import json

import pytest

from core import timekeeper as TK
from memory.projection import generation as G
from memory.projection import registry_authority as RA
from tests.memory.projection._fixtures import build_and_publish, redirect_timekeeper, seed_records_only, write_record


@pytest.fixture(autouse=True)
def _tk(tmp_path):
    redirect_timekeeper(tmp_path)
    yield
    TK.reset_for_tests()


def test_r0_to_r1_then_r1_converge_on_one_id(tmp_path):
    profile = tmp_path / "profile"; profile.mkdir(); seed_records_only(profile)
    proj = G.projection_root(profile)
    c1, r1 = build_and_publish(profile)                                # R0 (empty) → finalizes R1
    assert c1.source_input_origins["coordinate_registry"] == "empty" and r1.outcome == "published_new"
    r1_sha = c1.manifest.content.registry_sha256
    assert RA.select_authority(proj)[0] == r1_sha
    c2, r2 = build_and_publish(profile)                                # built FROM R1, stays R1
    assert c2.source_input_origins["coordinate_registry"] == f"authority:{r1_sha}"
    assert c2.manifest.generation_id == c1.manifest.generation_id and r2.outcome == "reused_existing"
    assert len(RA.read_authority_lineage(proj).rows) == 1
    # a changed assignment (new key minted) ⇒ different finalized registry ⇒ different id, exactly one authority transition
    write_record(profile, "k8", title="Myelin", summary="myelin insulates the axon")
    c3, r3 = build_and_publish(profile)
    assert c3.manifest.content.registry_sha256 != r1_sha and c3.manifest.generation_id != c1.manifest.generation_id
    assert len(RA.read_authority_lineage(proj).rows) == 2
    old = json.loads(RA.read_selected_registry_bytes(proj))["addresses"]
    gen_dir = proj / f"gen-{c1.manifest.generation_id}"
    if gen_dir.exists():
        prev = json.loads((gen_dir / "coordinate_registry.json").read_text())["addresses"]
        assert all(old[k] == v for k, v in prev.items())               # existing assignments never move
