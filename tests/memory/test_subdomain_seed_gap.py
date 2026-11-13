"""Slice 6 — SEED -> GAP: creating a new dynamic subdomain registers a mastery
gap (target>0, actual=0) so it drives learning_debt and a first LEARN. Idempotent
so it never resets a domain Omega already learned."""

import importlib

import pytest

from deepsleep_loop._deepsleep_state import MasteryRecord
from deepsleep_loop.mastery_store import (
    load_mastery,
    normalize_domain_id,
    status_for,
    upsert_mastery,
)


def _setup(tmp_path, monkeypatch):
    root = tmp_path / "omega_root"
    root.mkdir(exist_ok=True)
    import core.paths as paths

    monkeypatch.setattr(paths, "OMEGA_ROOT", root)
    mastery_path = root / "mastery.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_mastery_path",
        lambda policy=None: mastery_path,
    )
    importlib.import_module("memory.knowledge_router")
    allocator = importlib.import_module("memory.subdomain_allocator")
    return allocator, mastery_path


def test_new_subdomain_seeds_a_mastery_gap(tmp_path, monkeypatch):
    allocator, mastery_path = _setup(tmp_path, monkeypatch)

    allocator.get_or_create_subdomain(
        lobe=2,
        seed_text="superposition and entanglement basics",
        title="Quantum Physics",
        dynamic_y_lobes={2},
    )

    records = load_mastery(mastery_path)
    did = normalize_domain_id("Quantum Physics")
    assert did in records, "new subdomain must register a mastery record"
    rec = records[did]
    assert rec.knowledge_mastery == 0.0  # nothing learned yet
    assert rec.target_mastery > 0.0  # a real gap -> learning pressure


def test_seed_gap_is_idempotent_does_not_reset_progress(tmp_path, monkeypatch):
    allocator, mastery_path = _setup(tmp_path, monkeypatch)
    did = normalize_domain_id("Quantum Physics")

    # Omega already learned this domain to 0.6.
    upsert_mastery(
        MasteryRecord(
            domain_id=did,
            domain_name="Quantum Physics",
            knowledge_mastery=0.6,
            application_mastery=0.0,
            confidence=0.0,
            evidence_count=2,
            subdomains={},
            target_mastery=0.7,
            status=status_for(0.6),
        ),
        path=mastery_path,
    )

    # Re-touching the subdomain must NOT reset the learned value.
    allocator.get_or_create_subdomain(
        lobe=2,
        seed_text="more quantum content",
        title="Quantum Physics",
        dynamic_y_lobes={2},
    )

    rec = load_mastery(mastery_path)[did]
    assert rec.knowledge_mastery == pytest.approx(0.6)
