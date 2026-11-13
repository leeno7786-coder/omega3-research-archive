"""Phase 7 — routing harness, edge-case catalog, and metrics."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from memory.dewey_map import UNSORTED_SUBDOMAIN_Y
from memory.routing.corpus import ADVERSARIAL_CASES, iter_all_cases, iter_training_data_cases
from memory.routing.edge_case_catalog import append_entry, load_entries, unsorted_rate
from memory.routing.harness import evaluate_case, run_harness


def test_corpus_training_data_non_empty():
    cases = list(iter_training_data_cases())
    assert len(cases) >= 400
    assert {c.expected_lobe for c in cases} >= {0, 1, 2, 3, 4, 5, 6, 7, 9, 11}


def test_adversarial_cases_route_to_unsorted():
    for case in (c for c in ADVERSARIAL_CASES if c.source == "adversarial"):
        result = evaluate_case(case)
        assert result.undecided
        assert result.write_coord == [case.expected_lobe, UNSORTED_SUBDOMAIN_Y, 0]
        assert result.symmetry_ok


def test_symmetry_smoke_cases_pass():
    smoke = [c for c in ADVERSARIAL_CASES if c.source == "symmetry"]
    assert smoke
    for case in smoke:
        result = evaluate_case(case)
        assert result.symmetry_ok, (
            f"symmetry failed text={case.text!r} lobe={case.expected_lobe} "
            f"write={result.write_coord} grammar_y={result.grammar_y}"
        )


def test_edge_case_catalog_append_and_load(tmp_path):
    path = tmp_path / "catalog.jsonl"
    append_entry(
        text="blizzard penguin teapot",
        lobe=3,
        coord=[3, UNSORTED_SUBDOMAIN_Y, 0],
        label_id="3.127",
        source="test",
        path=path,
    )
    rows = load_entries(path)
    assert len(rows) == 1
    assert rows[0]["lobe"] == 3
    assert rows[0]["coord"][1] == UNSORTED_SUBDOMAIN_Y


def test_ingest_appends_edge_catalog(tmp_path, monkeypatch):
    from memory.gates import base as gate_base
    from memory.gates.lobe_gates import ReasoningGate

    catalog = tmp_path / "edge.jsonl"
    monkeypatch.setattr(
        "memory.routing.edge_case_catalog._DEFAULT_PATH",
        catalog,
    )

    captured = {}

    def _capture_atomic_write(meta, update_index=True):
        captured["meta"] = dict(meta)
        return f"<test>/{meta['id']}.json"

    with (
        patch.object(gate_base, "atomic_write", side_effect=_capture_atomic_write),
        patch.object(gate_base, "_emit", lambda *a, **k: None),
        patch("memory.subdomain_flag.dynamic_y_lobes", return_value=frozenset()),
    ):
        assert ReasoningGate().ingest(
            "blizzard penguin teapot",
            metadata={"type": "concept", "title": "test/reasoning/x"},
        )

    rows = load_entries(catalog)
    assert len(rows) == 1
    assert rows[0]["text"] == "blizzard penguin teapot"
    assert captured["meta"]["provenance"]["undecided"] is True


def test_harness_metrics_thresholds():
    """Full corpus run — gates LFM go/no-go and catches regressions."""
    metrics = run_harness(collect_failures=False)
    assert metrics.total >= 400
    # Grammar resolves most labeled training examples; residual → _unsorted.
    assert metrics.decidability_rate >= 0.85
    assert metrics.symmetry_rate >= 0.99
    assert metrics.subdomain_accuracy >= 0.60
    assert metrics.unsorted_rate <= 0.40


def test_unsorted_rate_nonempty_catalog(tmp_path):
    path = tmp_path / "catalog.jsonl"
    append_entry(
        text="blizzard penguin teapot",
        lobe=3,
        coord=[3, UNSORTED_SUBDOMAIN_Y, 0],
        path=path,
    )
    assert unsorted_rate(path=path) == 1.0
    assert unsorted_rate(entries=[]) == 0.0


def test_routing_harness_report_script_runs():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "scripts/routing_harness_report.py"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0
    assert "decidability=" in result.stdout
    assert "unsorted_rate=" in result.stdout
