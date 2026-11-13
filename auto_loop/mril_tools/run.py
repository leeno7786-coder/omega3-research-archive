"""Run forced MRIL remember_* tools for an ExecutionActionPack."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from auto_loop._auto_state import MemoryEnvelope
from auto_loop.mril_tools.candidate_hygiene import finalize_recall_results_with_exposure
from auto_loop.mril_tools.remember import invoke_remember_tool


def is_browser_route(execution_pack: Mapping[str, Any]) -> bool:
    """Whether this execution pack is an explicit browser capability turn."""
    inputs = execution_pack.get("execution_inputs")
    return (
        isinstance(inputs, Mapping)
        and str(inputs.get("operator_route") or "").strip().lower() == "browser"
    )


def memory_recall_required(execution_pack: Mapping[str, Any]) -> bool:
    """True when execute must run forced remember_* tools before inference.

    Browser turns are capability actions, not memory-grounded answers.  The
    route is stamped at ingress and therefore takes precedence over any stale
    or overly broad memory flag carried by an older pack.
    """
    return bool(execution_pack.get("memory_required")) and not is_browser_route(execution_pack)


def run_forced_memory_tools(
    execution_pack: Mapping[str, Any],
    episode_id: str,
    *,
    memory: Optional[Callable[..., Any]] = None,
) -> Tuple[List[Dict[str, Any]], MemoryEnvelope]:
    """Execute all forced remember_* tools; fail-hard on read errors."""
    pack = dict(execution_pack)
    tools = list(pack.get("memory_tools") or ())
    if not tools:
        raise ValueError("memory_tools required when memory_required")
    # One OR MORE Meta-routed remember_* tools — the loop below runs them all and
    # aggregates the evidence (freeform recalls identity + persona together).
    for spec in tools:
        name = spec.get("tool") if isinstance(spec, dict) else None
        if not (isinstance(name, str) and name.startswith("remember_")):
            raise ValueError("each memory_tools entry must be a Meta-routed remember_* tool")
    query = str(pack.get("memory_query") or pack.get("memory_reason") or "")

    results: List[Dict[str, Any]] = []
    for spec in tools:
        tool_spec = dict(spec)
        if not tool_spec.get("query"):
            tool_spec["query"] = query
        out = invoke_remember_tool(tool_spec, episode_id, memory=memory)
        results.append(out)

    # Lane-local tools cannot compare one another.  Finalize once, after every
    # verified result is available, so the HUD hit list and inference prompt are
    # built from the exact same query-scoped, cross-tool-deduplicated evidence.
    results, exposure = finalize_recall_results_with_exposure(
        results,
        query,
        preserve_identity=str(pack.get("memory_reason") or "").startswith("Freeform chat"),
    )
    # WO-10: what was retrieved, what was admitted and why the rest was not, recorded in this
    # episode's own events — the only evidence that establishes what it was actually supplied.
    from memory.auto_evidence import capture_input_exposure

    capture_input_exposure(episode_id, exposure)

    items: List[dict] = []
    verdicts: List[str] = []
    for out in results:
        verdict = str(out.get("verdict") or "").strip().lower()
        if verdict:
            verdicts.append(verdict)
        for ev in out.get("evidence") or []:
            items.append(
                {
                    "memory_id": ev.get("memory_id"),
                    "source": f"lobe:{out.get('lobe')}",
                    "timestamp": None,
                    "relevance": ev.get("confidence"),
                    "content": ev.get("remembered_as"),
                    "tool": out.get("tool"),
                }
            )
        if verdict and verdict != "verified":
            items.append(
                {
                    "memory_id": None,
                    "source": f"recall_verdict:{out.get('tool')}",
                    "timestamp": None,
                    "relevance": 0.0,
                    "content": out.get("verdict_reason") or verdict,
                    "tool": out.get("tool"),
                    "verdict": verdict,
                    "pressure_signal": out.get("pressure_signal"),
                }
            )

    memory_status = "success"
    if verdicts and not any(str(r.get("verdict") or "verified") == "verified" for r in results):
        for status in ("conflict", "ambiguous", "unknown"):
            if status in verdicts:
                memory_status = status
                break

    # Diagnostic receipts (Cut 1: DME activation traces) — nonempty only, with
    # the tool/lobe that produced them. Never merged into ``items``.
    receipts: List[dict] = []
    for out in results:
        receipt = out.get("receipt")
        if isinstance(receipt, dict) and receipt:
            receipts.append({"tool": out.get("tool"), "lobe": out.get("lobe"), **receipt})

    envelope = MemoryEnvelope(
        memory_required=True,
        memory_status=memory_status,
        memory_reason=pack.get("memory_reason"),
        memory_scope=tuple(pack.get("memory_scope") or ()),
        items=tuple(items),
        error=None,
        receipts=tuple(receipts),
    )
    try:
        from guardian.terminal_feedback import emit_data, emit_guardian_event

        tool_count = len(results)
        emit_data(
            "auto",
            "mril_recall",
            episode_id=episode_id,
            summary=f"tool_count={tool_count}",
            tool_count=tool_count,
        )
        emit_guardian_event(
            "omega.auto",
            "mril_recall",
            episode_id=episode_id,
            tool_count=tool_count,
        )
    except Exception:
        pass
    return results, envelope
