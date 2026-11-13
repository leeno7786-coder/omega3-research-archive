"""Day-zero coverage-audit fixes (2026-07-18): the seven surfaces that
survived a factory reset — non-lobe memory runtime stores, stray compiled
graph fossils, root-level boot logs — are now wiped."""

from __future__ import annotations

import scripts.day_zero_reset as dz


def _touch(p, name="x.json"):
    p.mkdir(parents=True, exist_ok=True)
    (p / name).write_text("{}", encoding="utf-8")


def test_memory_runtime_stores_wipe_data_and_NEVER_code(tmp_path, monkeypatch):
    # Live-wipe incident 2026-07-18: dme/source_ledger/integrity are Python
    # PACKAGES with data mixed in — the first wipe rmtree'd the code. The
    # contract is now precise: data dies, .py files survive.
    monkeypatch.setattr(dz, "OMEGA_ROOT", tmp_path)
    mem = tmp_path / "memory"
    _touch(mem / "dme", "adapter.py")
    _touch(mem / "dme", "sram.bin")
    _touch(mem / "dme", "hop_plane_strong.bin")
    _touch(mem / "source_ledger", "store.py")
    _touch(mem / "source_ledger" / "artifacts", "src_abc.json")
    _touch(mem / "integrity", "audit.py")
    _touch(mem / "integrity", "last_audit.json")
    _touch(mem / "recall_verifier", "verdict_log.jsonl")
    _touch(mem / "sense_registry", "senses.jsonl")

    removed = dz.wipe_memory_runtime_stores()
    assert "source_ledger/artifacts" in removed
    assert "integrity/last_audit.json" in removed
    assert "recall_verifier" in removed and "sense_registry" in removed
    # data gone
    assert not (mem / "dme" / "sram.bin").exists()
    assert not (mem / "dme" / "hop_plane_strong.bin").exists()
    assert not (mem / "source_ledger" / "artifacts").exists()
    assert not (mem / "integrity" / "last_audit.json").exists()
    assert not (mem / "recall_verifier").exists()
    assert not (mem / "sense_registry").exists()
    # CODE SURVIVES
    assert (mem / "dme" / "adapter.py").exists()
    assert (mem / "source_ledger" / "store.py").exists()
    assert (mem / "integrity" / "audit.py").exists()
    # idempotent on an already-clean tree
    assert dz.wipe_memory_runtime_stores() == []


def test_source_ledger_and_dme_named_in_the_store_list():
    # The audit's two heaviest survivors must stay pinned by NAME.
    assert "source_ledger" in dz.MEMORY_RUNTIME_STORES
    assert "dme" in dz.MEMORY_RUNTIME_STORES


def test_stray_compiled_graphs_are_wiped(tmp_path, monkeypatch):
    monkeypatch.setattr(dz, "OMEGA_ROOT", tmp_path)
    _touch(tmp_path / "memory" / "compiled", "graph.sqlite")
    _touch(tmp_path / "compiled", "graph.sqlite")
    assert len(dz.wipe_stray_compiled_graphs()) == 2
    assert not (tmp_path / "memory" / "compiled").exists()
    assert not (tmp_path / "compiled").exists()


def test_constitution_seeding_passes_the_canonical_source_contract(tmp_path, monkeypatch):
    # Live-wipe failure 2026-07-18: the seeder's direct atomic_write bypassed
    # the SourceLedger builder and died on the ledger-first contract — while
    # every sweep stayed green (no test ran the REAL seeding path). This pin
    # runs it hermetically: every seed must COMMIT through the api path.
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    redirect_memory_to_tmp(tmp_path, monkeypatch)
    from memory.identity.seeds import CONSTITUTION_SEEDS

    paths = dz.seed_constitution_lobe()
    assert len(paths) == len(CONSTITUTION_SEEDS)
    assert all(paths)


def test_boot_logs_are_wiped(tmp_path, monkeypatch):
    monkeypatch.setattr(dz, "OMEGA_ROOT", tmp_path)
    (tmp_path / "_brain_boot.log").write_text("x", encoding="utf-8")
    (tmp_path / "_brain_boot.err.log").write_text("x", encoding="utf-8")
    (tmp_path / "boot_live3.out.log").write_text("x", encoding="utf-8")
    (tmp_path / "boot_v2.err.log").write_text("x", encoding="utf-8")
    _touch(tmp_path / "_boot_logs", "run.log")
    _touch(tmp_path / "checkpoints", "boot_1.log")
    (tmp_path / "checkpoints" / "keep.json").write_text("{}", encoding="utf-8")
    assert dz.wipe_boot_logs() >= 5
    assert not (tmp_path / "_brain_boot.log").exists()
    assert not (tmp_path / "_boot_logs").exists()
    assert not (tmp_path / "checkpoints" / "boot_1.log").exists()
    assert (tmp_path / "checkpoints" / "keep.json").exists(), "only boot_*.log leave"
