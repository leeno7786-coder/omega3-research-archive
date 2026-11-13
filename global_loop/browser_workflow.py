"""Global A binds a BROWSE turn to the durable browser workflow (no plan is authored)."""
from __future__ import annotations


def bind_browser_workflow(ignition, *, operator_turn: bool) -> dict:
    """The workflow receipt this episode's browser run belongs to.

    An operator turn opens a workflow or joins the open one (its message becomes the newest
    instruction and a mid-run note). An Omega successor names the workflow it continues; a
    workflow that can no longer run is an error here, never a fresh one.
    """
    from meta_loop.browser.workflow import browser_workflow_receipt, close_browser_workflow, prepare_browser_turn

    metadata = dict(ignition.dominant_contributor.metadata)
    if operator_turn:
        prepared = prepare_browser_turn(str(metadata.get("message") or metadata.get("text") or ""),
                                        cycle_id=str(metadata.get("cycle_id") or ""))
        receipt = prepared.get("workflow")
        if not receipt:
            # A completion phrase with nothing open still routed here: the run has nothing to
            # do and must not take the cascade down. Bind a workflow that is already closed;
            # AUTO's run reports it and Omega tells the operator (live 2026-09-15: a Global A
            # ValueError on the browser binding halted the whole boot).
            receipt = close_browser_workflow(command=str(metadata.get("message") or "finished")) or                 _closed_receipt(str(metadata.get("message") or metadata.get("text") or ""), str(metadata.get("cycle_id") or ""))
        return dict(receipt)
    if ignition.dominant_contributor.source != "omega":
        raise ValueError("browser work is opened by the operator or continued by Omega")
    workflow_id = str(metadata.get("browser_workflow_id") or "")
    receipt = browser_workflow_receipt(workflow_id) if workflow_id else None
    if receipt is None or receipt["status"] not in {"active", "waiting_for_operator"}:
        raise ValueError("browser successor names a workflow that can no longer run")
    return dict(receipt)


def _closed_receipt(message: str, cycle_id: str) -> dict:
    """A workflow opened and closed in one motion: the operator asked to end what was not running."""
    from meta_loop.browser.workflow import close_browser_workflow, open_browser_workflow

    opened = open_browser_workflow(message or "close the browser", cycle_id=cycle_id)
    return close_browser_workflow(opened["workflow_id"], command=message or "finished") or dict(opened)
