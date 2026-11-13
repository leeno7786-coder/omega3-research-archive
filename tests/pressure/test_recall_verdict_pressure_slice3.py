"""Slice 3 — recall verdict pressure → Global A intent + single contributor seam."""

from __future__ import annotations

import yaml

from core.paths import OMEGA_ROOT
from memory.recall_verifier import append_verdict_log
from memory.taxonomy.schemas import RecallVerdict
from pressure_loop.recall_verdict_pressure import recall_verdict_pressure_contributor
from pressure_loop.taxonomy_pressure import taxonomy_pressure_contributor


def test_global_intent_maps_memory_gap_to_learn():
    from global_loop.intent.run import _map_opened_by_to_intent

    assert _map_opened_by_to_intent("memory_gap", "") == "LEARN"


def test_global_intent_maps_sense_resolution_to_idle():
    from global_loop.intent.run import _map_opened_by_to_intent

    assert _map_opened_by_to_intent("sense_resolution", "") == "IDLE"


def test_global_intent_maps_memory_reconciliation_to_idle():
    from global_loop.intent.run import _map_opened_by_to_intent

    assert _map_opened_by_to_intent("memory_reconciliation", "") == "IDLE"


def test_global_intent_yaml_maps_recall_verdict_pressure_kinds():
    gi = yaml.safe_load((OMEGA_ROOT / "config" / "global_intent.yaml").read_text(encoding="utf-8"))[
        "global_intent"
    ]
    mapping = gi["opened_by_map"]
    assert mapping["memory_gap"] == "LEARN"
    assert mapping["sense_resolution"] == "IDLE"
    assert mapping["memory_reconciliation"] == "IDLE"


def test_recall_verdict_pressure_contributor_reads_logged_verdicts(tmp_path):
    append_verdict_log(
        RecallVerdict(
            status="ambiguous",
            query="neuron",
            reason="query resolves to multiple live canonical senses",
        ),
        cycle_id="cycle-ambiguous",
        root=tmp_path,
    )

    contributor = recall_verdict_pressure_contributor(root=tmp_path)

    assert contributor is not None
    assert contributor.source == "recall_verdict"
    assert contributor.type == "sense_resolution"
    assert contributor.weight > 0
    assert contributor.metadata["signals"] == ["sense_resolution"]
    assert contributor.metadata["source"] == "recall_verdict_log"


def test_taxonomy_pressure_does_not_read_recall_verdict_log(tmp_path):
    append_verdict_log(
        RecallVerdict(
            status="ambiguous",
            query="neuron",
            reason="query resolves to multiple live canonical senses",
        ),
        cycle_id="cycle-ambiguous",
        root=tmp_path,
    )

    assert taxonomy_pressure_contributor(root=tmp_path) is None
