"""Phase 6O — first end-to-end skill-mediated agentic demo.

Drives the REAL Omega cascade (cognition_chain.ignite_episode) for ONE operator goal across
multiple cycles, driven by Global B's continuation pressure — inspect -> patch -> test:

  Cycle 1: Global USE_SKILL -> inspection skill -> qwen inspects -> Global B "more work needed"
           (continuation: implementation_needed)
  Cycle 2: continuation -> USE_SKILL -> coding skill -> qwen patches -> Global B "verification
           needed"
  Cycle 3: continuation -> USE_SKILL -> testing skill -> qwen tests -> Global B satisfied -> stop

qwen is reached ONLY by Auto (via dispatch_skill_pack's invoker). The default invoker hits the
live bridge; structural runs inject `demo_invoker` (a canned qwen that records the directive it
received and returns bounded results). The work loop reads each cycle's Global B continuation
pressure to choose the next cycle's objective — Global B drives the loop, the loop never decides
satisfaction itself.
"""

from __future__ import annotations

import json
import os as _os
import sys as _sys

# make the repo root importable when this script is run directly from the portable venv
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

# The full operator goal (the spec's demo input).
OPERATOR_GOAL = (
    "Inspect this repo, find where the dispatch schema should live, make the smallest safe "
    "change, run relevant tests, and report what changed."
)

# Cycle 1 opens on the investigation step; subsequent cycles are chosen from Global B's recorded
# continuation pressure reason (Global B drives the loop).
_FIRST_STEP = "Inspect this repo and find where the dispatch schema should live."
_STEP_FOR_REASON = {
    "implementation_needed": "Patch the smallest safe code change to add the dispatch schema.",
    "verification_needed": "Run the relevant tests and report what changed.",
}


# Per-kind tool preference — the canned qwen picks the action-relevant allowed tools (so the
# trace is faithful: patch -> patch_file, test -> run_tests), always WITHIN allowed_tools.
_PREFERRED_TOOLS = {
    "INSPECT": ("read_files", "search_repo", "inspect_git_diff"),
    "SEARCH": ("search_repo", "read_files"),
    "REVIEW": ("read_files", "inspect_git_diff", "search_repo"),
    "PLAN": ("read_files", "search_repo"),
    "PATCH": ("patch_file", "edit_file", "write_file", "apply_patch"),
    "REPAIR": ("patch_file", "edit_file", "write_file"),
    "WRITE": ("write_file", "patch_file"),
    "REFACTOR": ("patch_file", "edit_file"),
    "TEST": ("run_tests", "run_command", "pytest"),
    "VERIFY": ("run_tests", "run_command"),
}


def _pick_tools(kind: str, allowed: list[str]) -> list[str]:
    picked = [t for t in _PREFERRED_TOOLS.get(kind, ()) if t in allowed]
    return (picked or list(allowed))[:2]


def demo_invoker_factory():
    """A canned qwen invoker for structural runs. Returns (invoker, directives_seen). It NEVER
    exceeds the skill's allowed_tools (proving boundedness) and shapes its result to the skill's
    execution_kind (inspect -> findings, patch -> diff, test -> tests)."""
    directives: list[str] = []

    def _invoker(directive: str, dispatch) -> dict:
        directives.append(directive)
        allowed = list(dispatch.allowed_tools)
        kind = str(dispatch.execution_kind)
        tool_calls = [{"tool_name": t, "status": "success"} for t in _pick_tools(kind, allowed)]
        result = {"ok": True, "tool_calls": tool_calls}
        # HONEST SIGNALS (#19): supply every evidence key the dispatch declares as
        # required, or Meta-B records an evidence_unsatisfied blocker and the work loop
        # stalls after 1 cycle. Any non-empty value satisfies the foldback guard
        # (meta_loop/skill/foldback.py: `not provided.get(k)`).
        result["evidence"] = {
            k: f"demo_{k}" for k in (getattr(dispatch, "evidence_required", ()) or ())
        }
        if kind in ("INSPECT", "SEARCH", "REVIEW", "REPORT", "PLAN"):
            result["summary"] = f"inspected for objective: {dispatch.objective}"
            result["evidence"]["findings"] = [
                "the dispatch schema belongs in schema/qwen_skill_dispatch.py"
            ]
        elif kind in ("PATCH", "REPAIR", "WRITE", "REFACTOR", "MAINTAIN"):
            result["summary"] = "made the smallest safe change"
            result["files_changed"] = ["schema/qwen_skill_dispatch.py"]
            result["git_diff"] = (
                "diff --git a/schema/qwen_skill_dispatch.py b/schema/qwen_skill_dispatch.py\n+++ b/schema/qwen_skill_dispatch.py\n+# bounded change"
            )
        elif kind in ("TEST", "VERIFY"):
            result["summary"] = "ran the relevant tests; all green"
            result["tests_run"] = [
                "tests/core/test_phase6h_qwen_dispatch.py::test_qwen_receives_bounded_skill_context"
            ]
        else:
            result["summary"] = f"handled objective: {dispatch.objective}"
        return result

    return _invoker, directives


def build_ignition(index: int, message: str):
    from pressure_loop.contributors import EpisodeIgnition, make_contributor

    c = make_contributor("operator", "operator_command", 1.0, {"message": message})
    return EpisodeIgnition(
        episode_id=f"ep-6o-{index}",
        aggregate_pressure=1.0,
        opened_by="operator_command",
        dominant_contributor=c,
        contributors=(c,),
        trajectory_snapshot=None,
    )


def collect_trace(episode) -> dict:
    g = dict(getattr(episode, "global_", None) or {})
    m = dict(getattr(episode, "meta", None) or {})
    a = dict(getattr(episode, "auto", None) or {})
    outputs = dict(a.get("outputs") or {})
    qr = dict(outputs.get("skill_result") or {})
    sp = dict(m.get("skill_execution_pack") or {})
    cons = dict(m.get("skill_consolidation") or {})
    return {
        "episode_id": str(getattr(episode, "episode_id", "")),
        "intent": (g.get("intent_decision") or {}).get("intent"),
        "action": (m.get("execution_action_pack") or {}).get("action"),
        "selected_skill": (sp.get("selected_skill") or {}).get("skill_name"),
        "execution_kind": (sp.get("selected_skill") or {}).get("execution_kind"),
        "allowed_tools": list(sp.get("allowed_tools") or []),
        "dispatch_id": (outputs.get("skill_dispatch") or {}).get("dispatch_id"),
        "qwen_tool_calls": list(qr.get("tool_calls") or []),
        "files_changed": list(qr.get("files_changed") or []),
        "tests_run": list(qr.get("tests_run") or []),
        "meta_b_outcome": (m.get("skill_reflection") or {}).get("outcome_kind"),
        "global_b": dict(g.get("skill_outcome") or {}),
        "consolidation": cons,
    }


def run_work_loop(store, *, max_cycles: int = 6) -> tuple[list[dict], str]:
    """Drive ONE operator goal across cycles. Each cycle's next objective comes from the PRIOR
    cycle's Global B continuation pressure; the loop stops when Global B is satisfied. Assumes
    OMEGA_USE_SKILL_ROUTING is ON and the invoker is set by the caller's environment."""
    import cognition_chain as cl

    traces: list[dict] = []
    next_message = _FIRST_STEP
    stop_reason = "max_cycles_reached"
    for cycle in range(1, max_cycles + 1):
        episode = cl.ignite_episode(store, build_ignition(cycle, next_message))
        trace = collect_trace(episode)
        traces.append(trace)
        gb = trace["global_b"]
        if gb.get("intent_satisfied"):
            stop_reason = "intent_satisfied"
            break
        reason = (gb.get("continuation_pressure") or {}).get("reason")
        nxt = _STEP_FOR_REASON.get(reason)
        if nxt is None:
            stop_reason = f"no_continuation({reason})"
            break
        next_message = nxt
    return traces, stop_reason


def build_demo_output(traces: list[dict], stop_reason: str) -> dict:
    last = traces[-1] if traces else {}
    return {
        "episode_id": traces[0]["episode_id"] if traces else "",
        "episode_ids": [t["episode_id"] for t in traces],
        "cycle_count": len(traces),
        "global_intents": [t["intent"] for t in traces],
        "selected_skills": [t["selected_skill"] for t in traces],
        "dispatches": [t["dispatch_id"] for t in traces],
        "qwen_tool_calls": [tc.get("tool_name") for t in traces for tc in t["qwen_tool_calls"]],
        "files_changed": [f for t in traces for f in t["files_changed"]],
        "tests_run": [r for t in traces for r in t["tests_run"]],
        "meta_b_outcomes": [t["meta_b_outcome"] for t in traces],
        "per_cycle_satisfaction": [bool(t["global_b"].get("intent_satisfied")) for t in traces],
        "global_b_satisfaction": bool((last.get("global_b") or {}).get("intent_satisfied")),
        "omega_consolidation": (last.get("consolidation") or {}).get("record") or {},
        "deepsleep_summary": (last.get("consolidation") or {}).get("summary") or {},
        "stop_reason": stop_reason,
    }


def main() -> None:  # pragma: no cover — live run (real bridge + jackrong_qwen)
    """Live demo: flip the flag, drive the work loop against the REAL qwen bridge, print JSON."""
    import importlib

    intent_run = importlib.import_module("global_loop.intent.run")
    prev = intent_run.OMEGA_USE_SKILL_ROUTING
    intent_run.OMEGA_USE_SKILL_ROUTING = True
    try:
        from pressure_loop.episode import EpisodeStore

        traces, stop_reason = run_work_loop(EpisodeStore())
        print(json.dumps(build_demo_output(traces, stop_reason), indent=2))
    finally:
        intent_run.OMEGA_USE_SKILL_ROUTING = prev


if __name__ == "__main__":  # pragma: no cover
    main()
