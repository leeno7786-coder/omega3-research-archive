from __future__ import annotations

import json
import subprocess
import sys


def _perfect_report():
    return {
        "ok": True,
        "comparison": {"equal": True},
        "runs": [
            {
                "dme_parity": {"status": "ok"},
                "boundary_assertions": {"ok": True},
                "queries": [
                    {
                        "ok": True,
                        "expected_status": "verified",
                        "success_mode": "exact",
                    },
                    {
                        "ok": True,
                        "expected_status": "unknown",
                        "success_mode": "safe_abstain",
                    },
                ],
            }
        ],
        "mutation_probes": {"probes": [{"ok": True}]},
    }


def test_scorecard_reports_minimum_hard_gate_score(tmp_path):
    from memory.benchmark_scorecard import build_scorecard
    from memory.determinism_replay import run_determinism_replay

    report = run_determinism_replay(base_dir=tmp_path / "replay")
    scorecard = build_scorecard(report)

    assert scorecard["ok"] is True
    assert scorecard["headline_score"] == 100.0
    assert scorecard["score_policy"] == "minimum_component_score"
    assert scorecard["hard_failures"] == []
    assert scorecard["comparison"]["omega_margin_points"] == 4.6
    assert scorecard["comparison"]["apples_to_apples"] is False
    assert scorecard["response_model"]["model"] == "qwen35_9b"
    assert (
        scorecard["response_model"]["model_class"]
        == "Qwen3.5-9B local cognition seat (all four lanes)"
    )
    assert scorecard["response_model"]["role"] == "answer_voice"
    assert scorecard["response_model"]["memory_score_dependency"] == "none"
    assert scorecard["components"]["brain_wiring"] == 100.0
    assert scorecard["brain_wiring"]["status"] == "covered"
    proof_names = {item["name"] for item in scorecard["brain_wiring"]["proofs"]}
    assert "operator_ingress_source_citation" in proof_names
    assert "source_session_metadata_write_to_recall" in proof_names
    assert "auto_question_system_prompt_grounding" in proof_names
    assert scorecard["counts"]["queries_ok"] == scorecard["counts"]["queries_total"]
    assert scorecard["counts"]["mutation_ok"] == scorecard["counts"]["mutation_total"]


def test_scorecard_drops_to_failed_component_minimum():
    from memory.benchmark_scorecard import build_scorecard

    report = {
        "ok": True,
        "comparison": {"equal": True},
        "runs": [
            {
                "dme_parity": {"status": "ok"},
                "boundary_assertions": {"ok": True},
                "queries": [
                    {
                        "ok": True,
                        "expected_status": "verified",
                        "success_mode": "exact",
                    },
                    {
                        "ok": False,
                        "expected_status": "unknown",
                        "success_mode": "exact",
                    },
                ],
            }
        ],
        "mutation_probes": {"probes": [{"ok": True}]},
    }

    scorecard = build_scorecard(report)

    assert scorecard["ok"] is False
    assert scorecard["headline_score"] == 0.0
    assert "safe_abstention_accuracy" in scorecard["hard_failures"]
    assert scorecard["counts"]["safe_abstention_ok"] == 0
    assert scorecard["counts"]["safe_abstention_total"] == 1


def test_scorecard_drops_when_brain_wiring_proof_is_missing():
    from memory.benchmark_scorecard import build_scorecard

    scorecard = build_scorecard(
        _perfect_report(),
        brain_wiring_proofs=(
            {
                "name": "source_session_metadata_write_to_recall",
                "surface": "ledger -> verifier",
                "status": "missing",
                "tests": ("tests/memory/test_recall_verifier.py",),
            },
        ),
    )

    assert scorecard["ok"] is False
    assert scorecard["headline_score"] == 0.0
    assert scorecard["components"]["brain_wiring"] == 0.0
    assert "brain_wiring" in scorecard["hard_failures"]


def test_scorecard_records_explicit_response_model():
    from memory.benchmark_scorecard import build_scorecard

    scorecard = build_scorecard(
        {
            "ok": True,
            "comparison": {"equal": True},
            "runs": [
                {"dme_parity": {"status": "ok"}, "boundary_assertions": {"ok": True}, "queries": []}
            ],
            "mutation_probes": {"probes": []},
        },
        response_model="gemma4-e4b-it-local",
        response_model_class="4B local answer voice",
    )

    assert scorecard["response_model"]["model"] == "gemma4-e4b-it-local"
    assert scorecard["response_model"]["model_class"] == "4B local answer voice"


def test_memory_benchmark_scorecard_cli_returns_json(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "scripts/memory_benchmark_scorecard.py",
            "--base-dir",
            str(tmp_path / "scorecard-replay"),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["type"] == "omega_memory_benchmark_scorecard"
    assert payload["ok"] is True
    assert payload["headline_score"] == 100.0
    assert payload["response_model"]["model"] == "qwen35_9b"
    assert payload["components"]["mutation_fail_closed"] == 100.0
