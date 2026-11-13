from __future__ import annotations

from memory.recall_verifier import append_verdict_log
from memory.taxonomy.pressure_signals import analyze_recall_verdict_pressure
from memory.taxonomy.schemas import RecallVerdict
from pressure_loop.recall_verdict_pressure import recall_verdict_pressure_contributor
from pressure_loop.taxonomy_pressure import taxonomy_pressure_contributor


def test_analyze_recall_verdict_pressure_maps_conflict_to_reconciliation(tmp_path):
    append_verdict_log(
        RecallVerdict(
            status="conflict",
            query="who built omega",
            reason="source ledger span hash mismatch",
            rejected_hits=({"memory_id": "mem.creator", "status": "conflict"},),
        ),
        cycle_id="cycle-1",
        root=tmp_path,
    )

    result = analyze_recall_verdict_pressure(root=tmp_path)

    assert result["recommended_pressure"] == "memory_reconciliation"
    assert result["signals"][0]["signal"] == "memory_reconciliation"
    assert result["signals"][0]["detail"]["status"] == "conflict"
    assert result["evidence_event_ids"]


def test_taxonomy_pressure_contributor_ignores_recall_verdict_log(tmp_path):
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


def test_recall_verdict_pressure_contributor_reads_logged_recall_verdicts(tmp_path):
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
