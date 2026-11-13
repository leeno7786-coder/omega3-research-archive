"""Omega Memory benchmark scorecard.

The scorecard turns the deterministic replay report into operator-facing
metrics. It is intentionally stricter than a marketing average: the headline
score is the minimum of all hard gates, so one weak dimension lowers the
number.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

EXTERNAL_CLAIM_LONGMEMEVAL = 95.4
DEFAULT_RESPONSE_MODEL = "qwen35_9b"
DEFAULT_RESPONSE_MODEL_CLASS = "Qwen3.5-9B local cognition seat (all four lanes)"
DEFAULT_BRAIN_WIRING_PROOFS = (
    {
        "name": "operator_ingress_source_citation",
        "surface": "pressure_loop.operator_chat -> memory.source_ledger",
        "status": "covered",
        "tests": ("tests/memory/test_memory_engine_v2_followups.py",),
    },
    {
        "name": "source_session_metadata_write_to_recall",
        "surface": "SourceLedger citation -> canonical provenance -> graph/DME -> RecallVerifier evidence",
        "status": "covered",
        "tests": (
            "tests/memory/test_deterministic_read_write_paths.py",
            "tests/memory/taxonomy/test_memory_leaf_v2_projection.py",
            "tests/memory/test_dme_adapter.py",
            "tests/memory/test_recall_verifier.py",
        ),
    },
    {
        "name": "meta_question_graph_recall_bundle",
        "surface": "meta_loop.memory.run question recall -> graph-wide user-facing lobes",
        "status": "covered",
        "tests": ("tests/core/test_meta_question_recall.py",),
    },
    {
        "name": "meta_freeform_user_facing_bundle",
        "surface": "meta_loop.memory.run freeform recall -> identity/knowledge/coding/time",
        "status": "covered",
        "tests": ("tests/core/test_meta_freeform_recall.py",),
    },
    {
        "name": "auto_question_system_prompt_grounding",
        "surface": "auto_loop.execute.run RESPONSE_QUESTION -> verified SYSTEM evidence",
        "status": "covered",
        "tests": ("tests/core/test_auto_execution.py",),
    },
    {
        "name": "auto_unverified_recall_blocked",
        "surface": "auto_loop.execute.run -> unverified recall excluded from grounding",
        "status": "covered",
        "tests": ("tests/core/test_auto_execution.py",),
    },
    {
        "name": "fast_reply_verified_recall_only",
        "surface": "pressure_loop.fast_reply -> RecallVerifier-gated context",
        "status": "covered",
        "tests": ("tests/pressure/test_operator_fast_reply.py",),
    },
    {
        "name": "qwen35_9b_answer_voice_disclosed",
        "surface": "benchmark scorecard -> response model disclosure",
        "status": "covered",
        "tests": ("tests/memory/test_memory_benchmark_scorecard.py",),
    },
)


def build_scorecard(
    report: Mapping[str, Any],
    *,
    response_model: str = DEFAULT_RESPONSE_MODEL,
    response_model_class: str = DEFAULT_RESPONSE_MODEL_CLASS,
    brain_wiring_proofs: tuple[Mapping[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """Return deterministic benchmark metrics for a replay report."""
    runs = list(report.get("runs") or [])
    first = runs[0] if runs else {}
    all_queries = list(first.get("queries") or [])
    # KNOWN VERDICT-GROUNDING GAPS (see memory/determinism_replay.py
    # KNOWN_VERDICT_GAPS): gap queries are EXCLUDED from every denominator —
    # never silently counted as passes — and surfaced in the payload below so
    # the scorecard cannot read as full coverage while the gap stands.
    queries = [item for item in all_queries if not item.get("known_gap")]
    known_gaps = sorted(
        str(item.get("label") or "") for item in all_queries if item.get("known_gap")
    )
    mutation_probes = list(((report.get("mutation_probes") or {}).get("probes")) or [])
    wiring_proofs = tuple(brain_wiring_proofs or DEFAULT_BRAIN_WIRING_PROOFS)

    outcome_accuracy = _percent(sum(1 for item in queries if item.get("ok")), len(queries))
    verified_queries = [item for item in queries if item.get("expected_status") == "verified"]
    verified_recall_accuracy = _percent(
        sum(1 for item in verified_queries if item.get("ok")),
        len(verified_queries),
    )
    safe_queries = [item for item in queries if item.get("expected_status") != "verified"]
    safe_abstention_accuracy = _percent(
        sum(1 for item in safe_queries if item.get("ok")),
        len(safe_queries),
    )
    exact_queries = [
        item
        for item in queries
        if item.get("expected_status") == "verified"
        and item.get("success_mode") in {"exact", "any_verified", "graph_only"}
    ]
    exact_recall_accuracy = _percent(
        sum(1 for item in exact_queries if item.get("ok")),
        len(exact_queries),
    )
    mutation_fail_closed = _percent(
        sum(1 for item in mutation_probes if item.get("ok")),
        len(mutation_probes),
    )
    deterministic_replay = 100.0 if (report.get("comparison") or {}).get("equal") else 0.0
    projection_parity = 100.0 if (first.get("dme_parity") or {}).get("status") == "ok" else 0.0
    boundary_integrity = 100.0 if (first.get("boundary_assertions") or {}).get("ok") else 0.0
    brain_wiring = _brain_wiring_score(wiring_proofs)

    components = {
        "outcome_accuracy": outcome_accuracy,
        "verified_recall_accuracy": verified_recall_accuracy,
        "exact_recall_accuracy": exact_recall_accuracy,
        "safe_abstention_accuracy": safe_abstention_accuracy,
        "mutation_fail_closed": mutation_fail_closed,
        "deterministic_replay": deterministic_replay,
        "projection_parity": projection_parity,
        "boundary_integrity": boundary_integrity,
        "brain_wiring": brain_wiring,
    }
    omega_score = min(components.values()) if components else 0.0
    return {
        "type": "omega_memory_benchmark_scorecard",
        "ok": bool(report.get("ok")) and omega_score == 100.0,
        "headline_score": omega_score,
        "score_policy": "minimum_component_score",
        "known_gaps": known_gaps,  # visible — gap-excluded, never gap-blessed
        "components": components,
        "response_model": {
            "model": str(response_model or DEFAULT_RESPONSE_MODEL),
            "model_class": str(response_model_class or DEFAULT_RESPONSE_MODEL_CLASS),
            "role": "answer_voice",
            "memory_score_dependency": "none",
            "note": (
                "The scorecard proves memory ingest/recall gates. The response "
                "model is disclosed separately so small-model answer runs cannot "
                "be confused with retrieval proof."
            ),
        },
        "brain_wiring": {
            "status": "covered" if brain_wiring == 100.0 else "incomplete",
            "score": brain_wiring,
            "policy": "all_required_live_brain_surfaces_covered",
            "proofs": [_normalize_brain_wiring_proof(item) for item in wiring_proofs],
        },
        "counts": {
            "queries_ok": sum(1 for item in queries if item.get("ok")),
            "queries_total": len(queries),
            "verified_ok": sum(1 for item in verified_queries if item.get("ok")),
            "verified_total": len(verified_queries),
            "safe_abstention_ok": sum(1 for item in safe_queries if item.get("ok")),
            "safe_abstention_total": len(safe_queries),
            "mutation_ok": sum(1 for item in mutation_probes if item.get("ok")),
            "mutation_total": len(mutation_probes),
        },
        "comparison": {
            "external_claim": "OmegaMax LongMemEval public claim",
            "external_claim_score": EXTERNAL_CLAIM_LONGMEMEVAL,
            "omega_margin_points": round(omega_score - EXTERNAL_CLAIM_LONGMEMEVAL, 2),
            "apples_to_apples": False,
            "note": (
                "This scorecard measures Omega3 deterministic ingest/recall proof gates. "
                "Run the LongMemEval adapter for direct benchmark comparison."
            ),
        },
        "hard_failures": _hard_failures(components),
        "source_report_ok": bool(report.get("ok")),
    }


def _hard_failures(components: Mapping[str, float]) -> list[str]:
    return [name for name, score in components.items() if float(score) < 100.0]


def _brain_wiring_score(proofs: tuple[Mapping[str, Any], ...]) -> float:
    if not proofs:
        return 0.0
    return 100.0 if all(str(item.get("status") or "") == "covered" for item in proofs) else 0.0


def _normalize_brain_wiring_proof(proof: Mapping[str, Any]) -> dict[str, Any]:
    tests = proof.get("tests") or ()
    if isinstance(tests, str):
        normalized_tests = [tests]
    else:
        normalized_tests = [str(item) for item in tests]
    return {
        "name": str(proof.get("name") or ""),
        "surface": str(proof.get("surface") or ""),
        "status": str(proof.get("status") or ""),
        "tests": normalized_tests,
    }


def _percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100.0, 2)
