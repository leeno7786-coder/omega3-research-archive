"""Tests for lobe 000_identity — constitution + traits port from v1 memory engine."""

from __future__ import annotations

from memory.identity.personal_decompose import PersonalDecomposer
from memory.identity.seeds import CONSTITUTION_SEEDS, list_constitution_seeds
from memory.identity.subdomains import (
    CONSTITUTION_Y,
    TRAITS_Y,
    identity_recall_subdomains,
    is_traits_subdomain,
)


def test_personal_decomposer_extracts_noah_anchor():
    dec = PersonalDecomposer().decompose("My name is Noah and I go by Leeno online.")
    assert dec is not None
    assert dec.anchor_name == "Noah"
    assert "Leeno" in dec.aliases
    enrich = dec.to_enrichment()
    assert enrich["chain_anchor"] == "Noah"
    assert "Noah" in enrich["facets"]["who"]


def test_personal_decomposer_third_person_subject():
    dec = PersonalDecomposer().decompose("Jenny is my sister and she lives in Austin.")
    assert dec is not None
    assert "Jenny" in dec.third_person_subjects
    assert dec.to_enrichment()["linked_persons"] == ["Jenny"]


def test_personal_decomposer_action_and_why():
    text = "I built Omega using Python because I wanted fully local cognition."
    dec = PersonalDecomposer().decompose(text)
    assert dec is not None
    facets = dec.to_enrichment()["facets"]
    assert any("built" in w.lower() for w in facets["what"])
    assert facets["why"]


def test_constitution_seeds_from_omega_loop():
    seeds = list_constitution_seeds()
    assert len(seeds) == 7
    ids = {s.record_id for s in seeds}
    assert "0.00.omega_identity" in ids
    assert "0.00.omega_creator" in ids
    assert "0.00.memory_sovereignty" in ids
    assert all(
        "noah" not in " ".join((seed.record_id, seed.text, seed.title, seed.summary)).lower()
        for seed in seeds
    )
    for seed in CONSTITUTION_SEEDS:
        assert seed.text
        assert seed.title
        assert "omega_loop" in seed.tags or "founding" in seed.tags


def test_identity_recall_subdomains_constitution_and_traits_only():
    subs = identity_recall_subdomains()
    assert CONSTITUTION_Y in subs
    assert TRAITS_Y in subs
    assert 3 not in subs  # evolution
    assert 4 not in subs  # guardian
    assert is_traits_subdomain(TRAITS_Y)
    assert is_traits_subdomain(2)  # legacy


def test_seed_envelope_coord_is_constitution():
    from memory.identity.seeds import seed_envelope

    env = seed_envelope(CONSTITUTION_SEEDS[0])
    assert env.coord == [0, 0, 0]
    assert env.payload["canonical"] is True
