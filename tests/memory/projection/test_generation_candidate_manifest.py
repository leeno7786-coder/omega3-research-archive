"""D-13-38 — CandidateManifest envelope: no defaults for closure/page-0; the candidate
validator sees closure + page-0 + every artifact; the outer manifest COPIES the validated
telemetry (never supplied separately)."""

from __future__ import annotations

import dataclasses

import pytest

from memory.projection import generation as G
from tests.memory.projection._fixtures import clock, content, candidate_manifest, make_candidate, publish


def test_envelope_has_no_defaults_and_publisher_copies_telemetry(tmp_path):
    c = content()
    with pytest.raises(TypeError):
        G.CandidateManifest(content=c, generation_id=G.generation_id_for(c), snapshot_closure="proved")  # type: ignore[call-arg]
    root = tmp_path / "mem"
    cand = make_candidate(root, clock(100))
    cand = dataclasses.replace(cand, manifest=dataclasses.replace(cand.manifest, page0_used_by_lobe={"0": 7}, page0_overflow_attempts=2))
    with G.projection_publication_authority(root) as auth:
        res = G.publish_generation(root, cand, authority=auth)
    m = res.active_manifest
    assert m.page0_used_by_lobe == {"0": 7} and m.page0_overflow_attempts == 2 and m.unscorable_coordinate_count == 0
    # an unsafe candidate cannot be laundered by safe outer telemetry: the outer values come from the validated envelope
    unsafe = make_candidate(root, clock(101), compiler_version="v8")
    unsafe = dataclasses.replace(unsafe, manifest=dataclasses.replace(unsafe.manifest, unscorable_coordinate_count=3))
    with G.projection_publication_authority(root) as auth:
        with pytest.raises(G.GenerationIntegrityError):
            G.publish_generation(root, unsafe, authority=auth)
    assert G.read_active_manifest(G.projection_root(root)).generation_id == res.generation_id
