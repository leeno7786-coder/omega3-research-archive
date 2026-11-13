"""Audit observability emit coverage across rebuilt substrate."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SEQUENCERS: dict[str, Path] = {
    "pressure": ROOT / "pressure_loop" / "loop.py",
    "cognition_chain": ROOT / "cognition_chain.py",
    "global": ROOT / "global_loop" / "loop.py",
    "meta": ROOT / "meta_loop" / "loop.py",
    "auto": ROOT / "auto_loop" / "loop.py",
    # Omega's episode and motor runners are invoked by cognition_chain; there is
    # no retired omega_loop/loop.py sequencer in the live layout.
    "omega": ROOT / "cognition_chain.py",
    "deepsleep": ROOT / "deepsleep_loop" / "loop.py",
}

EXPECTED_PHASES: dict[str, tuple[str, ...]] = {
    "pressure": (
        "repass",
        "tick",
        "open_episode",
        "cascade",
        "episode_terminus",
        "deepsleep_episode_handoff",
        "window_commit",
        "deepsleep_window_handoff",
        "omega_episode",
        "knowledge_ingest",
        "global_a_handoff",
        "meta_a_handoff",
        "execution_contract",
        "neuromod",
        "fetched_comprehension",
        "auto_handoff",
        "meta_b_global_b_handoff",
        "meta_b_handoff",
        "global_b_handoff",
    ),
    "global": (
        "ignition",
        "intent",
        "assemble",
        "episode_outcome",
        "ground",
        "summarize",
        "followups",
        "package",
    ),
    "meta": (
        "intake",
        "action",
        "tool",
        "memory",
        "pack",
        "consistency",
        "disposition",
        "reflect",
    ),
    "auto": (
        "intake",
        "validate",
        "recall",
        "promptpack",
        "route",
        "execute",
        "fold",
    ),
    "omega": (
        "omega_episode",
    ),
    "deepsleep": (
        "intake",
        "mastery",
        "identity",
        "semantic",
        "trajectory",
        "failure",
        "query_lifecycle",
        "consolidate",
        "projection",
        "consolidate_memory",
        "practice_grounding",
        "canonical_projection",
        "episode_sram_flush",
        "workspace_retention",
        "conflict_reconciliation",
        "association_plasticity",
        "reflection_window_append",
        "pathway_projection",
        "memory_integrity",
    ),
}

# Required emit markers from OBSERVABILITY_EMIT_MAP.md (data + inference + signal).
DATA_MARKERS: tuple[tuple[str, str], ...] = (
    ("pressure_loop/pressure.py", "episode_ignition"),
    ("pressure_loop/pressure.py", "gather_contributors"),
    ("global_loop/loop.py", "intent_decision"),
    ("global_loop/package/run.py", "global_outcome_pack"),
    ("meta_loop/loop.py", "execution_action_pack"),
    ("meta_loop/loop.py", "reflection"),
    ("meta_loop/pack/run.py", "execution_action_pack"),
    ("meta_loop/reflect/run.py", "meta_reflection_pack"),
    ("auto_loop/fold/run.py", "auto_outcome_pack"),
    ("auto_loop/fold/run.py", "auto_outcome_pack"),
    ("auto_loop/fold/run.py", "fold_episode_auto"),
    ("auto_loop/execute/run.py", "tool_invocation"),
    ("auto_loop/skill/run.py", "skill_dispatch"),
    ("omega_loop/episode/run.py", "episode_model_output"),
    ("omega_loop/motor/run.py", "motor_program"),
    ("deepsleep_loop/loop.py", "consolidate_to_memory"),
    ("pressure_loop/episode.py", "episode_close"),
    ("deepsleep_loop/loop.py", "pressure_seed"),
    ("deepsleep_loop/loop.py", "deepsleep_outcome_pack"),
    ("deepsleep_loop/consolidate_memory/run.py", "consolidate_to_memory"),
    ("memory/api.py", "read_memory"),
    ("memory/api.py", "write_memory"),
    ("memory/api.py", "promote_memory"),
    ("memory/encoding.py", "encode_committed_memory"),
    ("memory/consolidation.py", "run_consolidation_once"),
    ("memory/promotion.py", "commit_promotion"),
    ("guardian/decision.py", "decide_request"),
    ("knowledge_extractor/pipeline.py", "knowledge_artifact"),
)

INFERENCE_MARKERS: tuple[tuple[str, str], ...] = (
    ("runtime/client.py", '"generate"'),
    ("runtime/client.py", '"generate_completion"'),
    ("auto_loop/execute/run.py", '"auto_image"'),
    ("runtime/socket.py", "emit_inference"),
    ("omega_loop/episode/run.py", "invoke_runtime_generate"),
    ("auto_loop/execute/run.py", "invoke_runtime_generate"),
)

SIGNAL_MARKERS: tuple[tuple[str, str], ...] = (("runtime/gpu_lease.py", "gpu_lease_wait"),)

KNOWLEDGE_PHASE_MARKERS: tuple[tuple[str, str], ...] = (
    ("knowledge_extractor/pipeline.py", '"extract"'),
    ("knowledge_extractor/pipeline.py", '"ingest"'),
    ("knowledge_extractor/pipeline.py", '"derive"'),
    ("knowledge_extractor/pipeline.py", '"store"'),
)


def _phase_span_phases(path: Path) -> set[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    phases: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            call = item.context_expr
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if isinstance(func, ast.Name) and func.id == "phase_span":
                if len(call.args) >= 2:
                    phase_arg = call.args[1]
                    if isinstance(phase_arg, ast.Constant) and isinstance(phase_arg.value, str):
                        phases.add(phase_arg.value)
    return phases


def _audit_phase_coverage() -> tuple[int, int, list[str]]:
    total_expected = 0
    total_covered = 0
    gaps: list[str] = []

    cognition_phases = _phase_span_phases(SEQUENCERS["cognition_chain"])
    pressure_phases = _phase_span_phases(SEQUENCERS["pressure"])
    combined_pressure = pressure_phases | cognition_phases

    for system, expected in EXPECTED_PHASES.items():
        if system == "pressure":
            instrumented = combined_pressure
        else:
            instrumented = _phase_span_phases(SEQUENCERS[system])

        for phase in expected:
            total_expected += 1
            if phase in instrumented:
                total_covered += 1
            else:
                gaps.append(f"{system}/{phase}")

    return total_covered, total_expected, gaps


def _audit_markers(
    markers: tuple[tuple[str, str], ...],
    *,
    label: str,
) -> tuple[int, int, list[str]]:
    covered = 0
    gaps: list[str] = []
    for rel_path, needle in markers:
        path = ROOT / rel_path.replace("/", "\\") if "\\" in str(ROOT) else ROOT / rel_path
        if not path.exists():
            gaps.append(f"{label}:{rel_path} (missing file)")
            continue
        text = path.read_text(encoding="utf-8")
        if needle in text:
            covered += 1
        else:
            gaps.append(f"{label}:{rel_path} ({needle})")
    return covered, len(markers), gaps


def audit() -> int:
    phase_cov, phase_total, phase_gaps = _audit_phase_coverage()
    data_cov, data_total, data_gaps = _audit_markers(DATA_MARKERS, label="data")
    infer_cov, infer_total, infer_gaps = _audit_markers(INFERENCE_MARKERS, label="inference")
    signal_cov, signal_total, signal_gaps = _audit_markers(SIGNAL_MARKERS, label="signal")
    knowledge_cov, knowledge_total, knowledge_gaps = _audit_markers(
        KNOWLEDGE_PHASE_MARKERS, label="knowledge_phase"
    )

    phase_pct = (100.0 * phase_cov / phase_total) if phase_total else 0.0
    data_pct = (100.0 * data_cov / data_total) if data_total else 0.0
    infer_pct = (100.0 * infer_cov / infer_total) if infer_total else 0.0
    signal_pct = (100.0 * signal_cov / signal_total) if signal_total else 0.0
    knowledge_pct = 100.0 * knowledge_cov / knowledge_total if knowledge_total else 0.0

    overall_cov = phase_cov + data_cov + infer_cov + signal_cov + knowledge_cov
    overall_total = phase_total + data_total + infer_total + signal_total + knowledge_total
    overall_pct = (100.0 * overall_cov / overall_total) if overall_total else 0.0

    print(f"Phase coverage:     {phase_cov}/{phase_total} ({phase_pct:.1f}%)")
    print(f"Data coverage:      {data_cov}/{data_total} ({data_pct:.1f}%)")
    print(f"Inference coverage: {infer_cov}/{infer_total} ({infer_pct:.1f}%)")
    print(f"Signal coverage:    {signal_cov}/{signal_total} ({signal_pct:.1f}%)")
    print(f"Knowledge phases:   {knowledge_cov}/{knowledge_total} ({knowledge_pct:.1f}%)")
    print(f"Overall coverage:   {overall_cov}/{overall_total} ({overall_pct:.1f}%)")

    all_gaps = phase_gaps + data_gaps + infer_gaps + signal_gaps + knowledge_gaps
    if all_gaps:
        print("Missing instrumentation:")
        for gap in all_gaps:
            print(f"  - {gap}")
    else:
        print("All expected emit points instrumented.")

    return 0 if not all_gaps else 1


if __name__ == "__main__":
    sys.exit(audit())
