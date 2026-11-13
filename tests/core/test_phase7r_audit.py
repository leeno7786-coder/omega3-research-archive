"""Phase 7R — hardening audit: the substrate's load-bearing invariants, enforced.

These are not behavioural tests — they are the guardrails that keep the deterministic
memory taxonomy honest as it evolves: Guardian-free, model-free, the graph is never the
source of truth (only the compiler writes it), and every claim cites evidence.
"""

from __future__ import annotations

import pytest
from dataclasses import replace
from pathlib import Path

_TAXONOMY = Path(__file__).resolve().parents[2] / "memory" / "taxonomy"


def _py_files():
    return sorted(p for p in _TAXONOMY.glob("*.py") if p.name != "__init__.py")


def _import_lines(path: Path):
    return [
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith(("import ", "from "))
    ]


def test_taxonomy_is_guardian_free():
    # the operator directive: the autonomous memory substrate never touches Guardian
    for p in _py_files():
        for ln in _import_lines(p):
            assert "guardian" not in ln.lower(), f"{p.name} imports guardian: {ln.strip()}"


def test_taxonomy_is_model_free():
    banned = (
        "qwen",
        "transformers",
        "openai",
        "torch",
        "onnxruntime",
        "metacog",
        "setfit",
        "llama",
    )
    for p in _py_files():
        blob = "\n".join(_import_lines(p)).lower()
        for term in banned:
            assert term not in blob, f"{p.name} imports a model/inference dep: {term}"


def test_graph_is_never_the_source_of_truth():
    # the disposable graph is WRITTEN only by the compiler (via graph_store); every other
    # taxonomy module reads it. The filesystem ledger is the sole source of truth.
    writers = (
        "reset_graph",
        "insert_node",
        "insert_edge",
        "insert_event",
        "insert_entity",
        "insert_claim",
    )
    # the compiler FAMILY is the only writer; the store/schema own
    # the DDL + upserts. Everything else (recall, pressure_signals, graph_diff, meta_recall,
    # replay) only reads.
    allowed = {"compiler.py", "graph_store.py", "graph_schema.py"}
    for p in _py_files():
        if p.name in allowed:
            continue
        body = p.read_text(encoding="utf-8")
        for w in writers:
            assert f".{w}(" not in body, f"{p.name} mutates the graph ({w})"


def test_every_claim_cites_evidence():
    from memory.taxonomy.claims import claim

    valid = claim("subject", "predicate", "object", "evt_source")
    assert valid.validate() == []  # a real claim carries its source as evidence
    blind = replace(valid, evidence_event_ids=())
    assert any("evidence" in e.lower() for e in blind.validate())  # no evidence -> invalid
