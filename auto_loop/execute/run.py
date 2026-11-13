"""AUTO execute phase — model/tool/image execution."""

from __future__ import annotations

import importlib
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

from runtime.tool_dispatch import deterministic_args_from_order
from pressure_loop.operator_chat_responses import reply_token_sink as _reply_token_sink
from auto_loop._auto_state import (
    AutoInput,
    AutoOutcomePack,
    MemoryEnvelope,
    PromptPack,
)

log = logging.getLogger("omega.core.auto")

_EXTERNAL_PROGRESS_TOOLS = frozenset({
    "web_search", "web_fetch", "discover_plugin_tools", "call_plugin_tool",
})
_OPERATOR_PROGRESS_INTERVAL_S = 5.0


def _run_operator_tool_with_progress(
    name: str,
    invoke: Callable[[], Any],
    *,
    on_progress: Callable[[str, int], None],
) -> Any:
    """Keep long external web/plugin waits visible while their synchronous call runs."""
    if name not in _EXTERNAL_PROGRESS_TOOLS:
        return invoke()

    interval = max(0.001, float(_OPERATOR_PROGRESS_INTERVAL_S))
    started = time.monotonic()
    stopped = threading.Event()

    def heartbeat() -> None:
        while not stopped.wait(interval):
            elapsed_s = max(1, int(time.monotonic() - started))
            message = f"Waiting for {name.replace('_', ' ')} ({elapsed_s}s elapsed)"
            try:
                on_progress(message, elapsed_s)
            except Exception:  # noqa: BLE001 — visibility must not change tool execution
                log.debug("operator tool progress frame failed", exc_info=True)

    worker = threading.Thread(target=heartbeat, name=f"omega-{name}-progress", daemon=True)
    worker.start()
    try:
        return invoke()
    finally:
        stopped.set()
        worker.join()


def _browser_response_or_fallback(
    response: Mapping[str, Any] | None,
    records: list[dict[str, Any]],
    *,
    forced_tool: str,
) -> dict[str, Any] | None:
    """Keep a successful deterministic action useful when answer generation is empty."""
    if forced_tool == "list_directory":
        if isinstance(response, Mapping) and str(response.get("text") or "").strip():
            return dict(response)
        fallback = _workspace_action_fallback(records)
        if not fallback:
            return dict(response) if isinstance(response, Mapping) else None
        body = dict(response) if isinstance(response, Mapping) else {"ok": True}
        body.update({"ok": True, "text": fallback, "tool_calls": []})
        return body
    return dict(response) if isinstance(response, Mapping) else None


def _workspace_action_fallback(records: list[dict[str, Any]]) -> str:
    """Return a grounded reply after a deterministic workspace observation."""
    for record in reversed(records):
        if record.get("tool_name") != "list_directory" or record.get("status") != "success":
            continue
        output = str(record.get("output") or "").strip()
        if output:
            return f"I inspected the active trusted folder.\n\n{output}"
    return ""


def _raising_site(error: BaseException) -> str:
    """Where in Omega's own source this was raised, as ``file.py:line in func``.

    The innermost frame is the `raise` statement itself, which is exactly what
    a reason shared by nineteen call sites cannot say. The exception MESSAGE is
    logged by the caller and is already a closed Omega reason; nothing from a
    provider is read here.
    """
    frame = None
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback
        traceback = traceback.tb_next
    if frame is None:
        return "no traceback"
    code = frame.tb_frame.f_code
    return f"{os.path.basename(code.co_filename)}:{frame.tb_lineno} in {code.co_name}"


def response_streaming_enabled() -> bool:
    """RISK 5 kill switch. ``OMEGA_RESPONSE_STREAM=0`` (also false/no/off) puts
    the operator reply back on the blocking call.

    It exists because streaming changes the operator's SURFACE, not the mind:
    the reply, the durable record and every stage after AUTO are byte-identical
    either way. So the switch reverts exactly one thing — when the tokens are
    handed over — and nothing downstream needs to know which way it is set.
    """
    return os.environ.get("OMEGA_RESPONSE_STREAM", "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _token_count_from_generate(result: Any) -> int:
    if not isinstance(result, dict):
        return 0
    tok = result.get("tokens") or result.get("usage")
    if isinstance(tok, dict):
        total = tok.get("total")
        if total is not None:
            return int(total)
        prompt = tok.get("prompt") or tok.get("input") or 0
        completion = tok.get("completion") or tok.get("output") or 0
        return int(prompt) + int(completion)
    if isinstance(tok, (int, float)):
        return int(tok)
    ti = result.get("tokens_in")
    to = result.get("tokens_out")
    if ti is not None or to is not None:
        return int(ti or 0) + int(to or 0)
    return 0


def _plugin_response_request(objective: str, result: Mapping[str, Any]):
    """Compose a grounded answer from an already-executed structured plan."""
    from guardian.external_ingress import harden
    from organ.promptorgan import PromptContext, phrase

    evidence = json.dumps(dict(result), ensure_ascii=False, sort_keys=True)
    return phrase(PromptContext(
        kind="chat",
        question=objective,
        data={
            "hits": (), "memory_required": False, "chat_intent": "freeform",
            "prompt_type": "RESPONSE_FREEFORM",
            "context_blocks": (
                "Completed plugin operation. Answer from this result; no further action has run.\n"
                f"Source: signed plugin runtime\n{harden(evidence)}",
            ),
        },
    ))


def _native_plugin_outputs(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Project native operations into the existing episode/permission contract.

    The complete sequence remains in operator_tool_calls. The singular fields
    identify the latest operation (especially the exact pending grant), while
    every executed step remains visible to the ordinary plugin foldback.
    """
    outputs: Dict[str, Any] = {}
    steps: list[dict[str, Any]] = []
    keys = ("plugin_plan", "plugin_result", "plugin_permission_request", "plugin_failure")
    for record in records:
        metadata = record.get("result")
        if not isinstance(metadata, Mapping) or "plugin_status" not in metadata:
            continue
        for key in keys:
            outputs.pop(key, None)
            if metadata.get(key) is not None:
                outputs[key] = metadata[key]
        plan_id = (metadata.get("plugin_plan") or {}).get("plan_id")
        steps.extend({**dict(step), "plan_id": plan_id} for step in metadata.get("plugin_step_results") or ())
    if outputs or steps:
        outputs["plugin_step_results"] = steps
    return outputs


def _default_tool_invoker(
    tool_name: str,
    args: Dict[str, Any],
    *,
    episode_id: str = "auto-tool",
    runtime_client: Any = None,
    pipeline_id: Optional[int] = None,
    work_kind: Optional[str] = None,
    cycle_id: str = "",
    job_id: str = "",
) -> Dict[str, Any]:
    from runtime.runtime_tool_dispatch import invoke_runtime_backed_tool

    t0 = time.perf_counter()
    # Namespaced plugin tools are AUTO PLUGIN_OPERATION work only.  They must
    # not fall through to the legacy runtime/static loader, and they must not
    # be invoked from this generic tool rail.
    from mcp_platform.plugin_dispatch import is_plugin_tool_name

    if is_plugin_tool_name(tool_name):
        raise RuntimeError("plugin_tool_requires_plugin_operation")

    rt_result = invoke_runtime_backed_tool(
        tool_name,
        args,
        episode_id=episode_id,
        runtime_client=runtime_client,
        pipeline_id=pipeline_id,
        work_kind=work_kind,
        cycle_id=cycle_id,
        job_id=job_id,
    )
    if rt_result is not None:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        if isinstance(rt_result, (bytes, bytearray)):
            import hashlib as _hl

            payload = {
                "bytes_len": len(rt_result),
                "sha256": _hl.sha256(rt_result).hexdigest(),
            }
        else:
            payload = rt_result
        result = {"ok": True, "result": payload, "elapsed_ms": elapsed_ms}
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "auto",
                "tool_invocation",
                episode_id=episode_id,
                summary=f"tool={tool_name} ok=True",
                tool_name=tool_name,
                elapsed_ms=elapsed_ms,
                ok=True,
            )
        except Exception:
            pass
        return result

    from core.paths import TOOL_SCHEMA_ROOT
    from schema.tool_spec import load_spec_from_schema_file

    schema_path = TOOL_SCHEMA_ROOT / f"{tool_name}.json"
    spec = load_spec_from_schema_file(schema_path)
    if spec is None or not (spec.implementation_module and spec.implementation_function):
        raise RuntimeError(f"no implementation for tool {tool_name!r}")

    mod = importlib.import_module(spec.implementation_module)
    fn = getattr(mod, spec.implementation_function)
    t0 = time.perf_counter()
    result = fn(**args)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    out = {"ok": True, "result": result, "elapsed_ms": elapsed_ms}
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "auto",
            "tool_invocation",
            episode_id=episode_id,
            summary=f"tool={tool_name} ok=True",
            tool_name=tool_name,
            elapsed_ms=elapsed_ms,
            ok=True,
        )
    except Exception:
        pass
    return out


def _maybe_extract_knowledge(
    pack: Mapping[str, Any],
    outputs: Dict[str, Any],
    inputs: Dict[str, Any],
    order_domain: str,
    tool: str,
    warnings: List[str],
    *,
    episode_id: str = "",
) -> None:
    """Distill a successful web/LEARN tool result into a KnowledgeArtifact.

    Every fetched page is memory — LEARN and chat-lane web_search/web_fetch.
    Failure-soft — never breaks execution."""
    if not _should_extract_knowledge(pack, inputs, tool, outputs):
        return
    from pressure_loop.policy import knowledge_learn_integration_wired

    if not knowledge_learn_integration_wired():
        return
    try:
        from knowledge_extractor import Source, extract

        art = extract(
            Source(
                kind="tool_output",
                content=str(outputs.get("tool_result") or ""),
                source_ref=str(inputs.get("query") or order_domain or tool),
                meta={"tool": tool, "domain": order_domain},
            )
        )
        outputs["knowledge_artifact_id"] = art.artifact_id
        outputs["knowledge_domain"] = art.domain
    except Exception as exc:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "auto",
            "knowledge_extract_failed",
            status="degraded",
            ok=False,
            error=str(exc),
            episode_id=episode_id,
        )
        warnings.append(f"knowledge_extract_failed: {exc}")


def _should_extract_knowledge(
    pack: Mapping[str, Any],
    inputs: Mapping[str, Any],
    tool: str,
    outputs: Mapping[str, Any],
) -> bool:
    intent = str((pack.get("source_action_goal") or {}).get("intent") or "")
    if intent == "LEARN":
        return True
    if str(tool or "") in {"web_fetch", "web_search"}:
        return True
    if inputs.get("web_research"):
        return True
    result = outputs.get("tool_result")
    return isinstance(result, dict) and bool(result.get("results"))


def _record_web_tool_result(outputs: Dict[str, Any], records: Any) -> None:
    """Promote chat/LEARN web tool pages into the structured tool_result ingest shape."""
    from cognition.web_research import merge_research_pages, pages_from_operator_records

    pages = pages_from_operator_records(records)
    if not pages:
        return
    merged = merge_research_pages(pages)
    if not merged.get("results"):
        return
    outputs["tool_result"] = merged


def _tool_provenance(tool: str, args: Mapping[str, Any], result: Any) -> Dict[str, Any]:
    """Return compact, operator-safe provenance for the HUD audit stream."""
    out: Dict[str, Any] = {
        "tool": str(tool or ""),
        "query": str(args.get("query") or "")[:160],
        "domain": str(args.get("domain") or "")[:80],
        "learned_link": "",
        "source_name": "",
        "result_count": 0,
    }
    if not isinstance(result, dict):
        return out

    out["domain"] = str(result.get("domain") or out["domain"] or "")[:80]
    rows = result.get("results")
    if isinstance(rows, list):
        out["result_count"] = len(rows)
        for row in rows:
            if not isinstance(row, dict):
                continue
            link = str(row.get("url") or "").strip()
            if link.startswith(("http://", "https://")):
                out["learned_link"] = link[:500]
                out["source_name"] = str(row.get("source") or row.get("title") or "")[:120]
                break
    return out


def _job_has_plan(job_id: Any) -> bool:
    """Whether this task has written its steps down yet. Never raises: no job, no plan."""
    from auto_loop.execute import job_control as jobs

    try:
        return bool(jobs.get_job(str(job_id)).plan) if job_id else False
    except (KeyError, ValueError, OSError):
        return False


def _job_plan_block(job_id: Any) -> list[str]:
    """The task's plan, as the next episode should read it: done, and left."""
    from auto_loop.execute import job_control as jobs

    try:
        job = jobs.get_job(str(job_id)) if job_id else None
    except (KeyError, ValueError, OSError):
        return []
    if job is None or not job.plan:
        return []
    block = ["TASK PLAN (carried from the step that wrote it)", jobs.render_job_plan(job)]
    block.append(
        "Every step is done: verify what the task asked for and call finish_task."
        if jobs.plan_is_complete(job)
        else "Do the next open step, then mark it with task_plan(completed=[...])."
    )
    return [*block, ""]


def _agent_continuation_prompt(inputs: Mapping[str, Any]) -> str:
    """What earlier steps of an operator's terminal-agent task found, for its next tool batch."""
    lines = [
        "CONTINUING THE OPERATOR'S TASK",
        f"Next step: {str(inputs.get('agent_next_step') or '').strip()[:1000]}",
        "",
        # The plan leads. A brief that opened with the raw tool log invited the model to
        # re-read what it had already written (live 2026-09-20).
        *_job_plan_block(inputs.get("job_id")),
        "Tool results from earlier steps (most recent last):",
    ]
    for row in inputs.get("agent_observations") or ():
        if isinstance(row, Mapping):
            body = str(row.get("output") or row.get("error") or "")
            lines.append(f"- {row.get('tool')} {row.get('args')} -> {row.get('status')}: {body}")
    return "\n".join(lines)


# A batch that reproduces an earlier result made no progress. Two such rounds and the
# table narrows to the moves that RESOLVE a batch, so a step cannot mill (live 2026-09-20:
# the todo-app build re-read its own two files until the operator stopped it).
_RESOLVING_TOOLS = ("finish_task", "ask_operator")
# Mutable work on the agent lane waits for a plan, exactly as it waits for a claimed
# project: the first step of a task is deciding what its steps are. The chat lane is
# untouched — an operator asking for one edit is not running a build.
_PLAN_REQUIRED_TOOLS = frozenset({"write_file", "edit_file", "run_command", "run_tests"})
_MAX_STALLED_ROUNDS = 2

_AGENT_PROJECT_FILE_LIMIT = 64
# A project claimed at a drive root would otherwise report the recycle bin and system volume as
# its files, which is what Omega reads as build evidence (live 2026-09-15: 64 recycled paths).
_AGENT_PROJECT_SKIP = frozenset({
    "system volume information", "node_modules", "__pycache__", "site-packages", "venv",
})


def _agent_run_receipt(records, *, operator_route: str, text: str, stop_reason: str,
                       operator_notes: list[str] | None = None) -> dict | None:
    """The host's receipt for one terminal-agent batch, the agent lane's browser_run.

    Status is what the host can see; whether the operator's request is satisfied is Omega's
    judgement in the foldback, never this function's.
    """
    if str(operator_route or "").strip().lower() != "agent":
        return None
    from pathlib import Path as _Path

    from auto_loop.execute import job_control as jobs

    rows = [row for row in (records or ()) if isinstance(row, Mapping)]
    actions: list[dict[str, Any]] = []
    touched: list[str] = []
    job_id = ""
    for index, row in enumerate(rows):
        job_id = str(row.get("job_id") or "") or job_id
        actions.append({
            "step": index + 1,
            "call_id": str(row.get("tool_call_id") or ""),
            "tool": str(row.get("tool_name") or ""),
            "args": json_compact(row.get("args") or {}),
            "ok": str(row.get("status") or "") == "success",
            "summary": str(row.get("output") or ""),
            "error": str(row.get("error") or ""),
        })
        for entry in row.get("touched_files") or ():
            path = entry.get("path") if isinstance(entry, Mapping) else entry
            if path:
                touched.append(str(path))
    project_root = ""
    task_touched: list[str] = []
    plan: list[dict[str, Any]] = []
    plan_complete = False
    if job_id:
        # A failed ledger read/write is missing evidence, never an empty task.
        # This batch's paths join the task's ledger so Omega reads what the
        # whole task changed, including earlier episodes.
        if touched:
            jobs.record_touched_paths(job_id, touched)
        job = jobs.get_job(job_id)
        project_root = str(job.project_root or "")
        task_touched = list(job.touched_paths)
        plan = [{"step": step.step, "text": step.text, "done": step.done}
                for step in job.plan]
        plan_complete = jobs.plan_is_complete(job)
    project_files: list[str] = []
    if project_root:
        import os as _os

        root = _Path(project_root)

        def scan_failed(error):
            raise error

        for current, directories, names in _os.walk(root, onerror=scan_failed):
            directories[:] = sorted(
                name for name in directories
                if not name.startswith((".", "$")) and name.lower() not in _AGENT_PROJECT_SKIP
            )
            for name in sorted(names):
                project_files.append(_Path(current, name).relative_to(root).as_posix())
                if len(project_files) >= _AGENT_PROJECT_FILE_LIMIT:
                    break
            if len(project_files) >= _AGENT_PROJECT_FILE_LIMIT:
                break
    answered = str(text or "").strip()
    succeeded = [row for row in actions if row["ok"]]
    # What the agent itself declared this batch: its own question, or that the task is done.
    question = ""
    options: list[str] = []
    finished = ""
    for row in rows:
        result = row.get("result") if isinstance(row.get("result"), Mapping) else {}
        if str(row.get("status") or "") != "success":
            continue
        if result.get("agent_question"):
            question = str(result["agent_question"])
            raw = result.get("options")
            options = [str(option) for option in raw] if isinstance(raw, (list, tuple)) else []
        if result.get("agent_finished"):
            finished = str(result.get("summary") or answered)
    if question:
        status = "ask_operator"
    elif finished:
        status = "done_claimed"
    elif not actions:
        status = "ask_operator" if answered.endswith("?") else ("done_claimed" if answered else "no_action")
    elif not succeeded:
        status = "blocked"
    elif len(actions) == 1 and actions[0]["tool"] == "start_job":
        status = "job_claimed"
    else:
        status = "batch_ran"
    return {
        "status": status,
        "steps": len(actions),
        "job_id": job_id,
        "project_root": project_root,
        "actions": actions,
        "touched_files": touched,
        # What the TASK has changed, across every episode of it. Omega judges the
        # operator's request, and one batch's empty list is not an answer to that.
        "task_touched_files": task_touched,
        "plan": plan,
        "plan_complete": plan_complete,
        "project_files": project_files,
        "summary": finished or ("" if status == "ask_operator" else answered),
        "question": question or (answered if status == "ask_operator" else ""),
        "options": options,
        # The operator's own words, sent while this batch ran: the host handed them to the batch
        # and hands them to Omega, so one decision sees the work and the new instruction.
        "operator_notes": [str(note) for note in (operator_notes or ())],
        "stopped_reason": str(stop_reason or ""),
    }


def _operator_turn_should_stop(cycle_id: str):
    """Esc, the stop button and a plain "stop" in chat all land here.

    The tool loop checks it between passes and between calls, so a batch stops at the next
    safe boundary instead of running to its budget (live 2026-09-15: nothing polled it).
    """
    cycle = str(cycle_id or "").strip()
    if not cycle:
        return None

    def stopped() -> bool:
        from pressure_loop.operator_chat_responses import work_halt_requested

        # The work's own halt, never the playback interrupt: cancelling speech must not stop
        # cognition (pinned since 2026-07).
        return work_halt_requested(cycle)

    return stopped


def _operator_tools_reply(generate, *, notes_out=None, **kwargs):
    """Own one writer invocation without holding the mailbox mutex across work.

    ``notes_out`` collects the operator's held words as the batch reads them, so the caller can
    put the same instruction into the batch receipt Omega folds on.
    """
    from auto_loop.execute import job_control as jobs

    session = {"job_id": "", "cursor": 0, "owned": False, "records": [],
               "operator_notes": notes_out if notes_out is not None else []}
    outcome = None
    try:
        outcome = _operator_tools_exchange(generate, session=session, **kwargs)
        return outcome
    finally:
        if session["owned"]:
            body, _, stop = outcome if outcome is not None else (None, [], "execution failed")
            route = str(kwargs.get("operator_route") or "").strip().lower()
            if route == "agent" and stop != "interrupted":
                # An operator's agent task spans batches, so the job is its workflow: it stays
                # open between them and Omega closes it when it settles the work. Finished here,
                # the next batch owned nothing and its writes were refused for want of start_job
                # (live 2026-09-15).
                jobs.leave_writer(session["job_id"], session["owner_cycle_id"])
            else:
                status = "stopped" if stop == "interrupted" else "failed" if not (body or {}).get("text") else "completed"
                try:
                    jobs.finish_job(session["job_id"], status=status,
                                    summary=str((body or {}).get("text") or stop or "execution failed"))
                finally:
                    jobs.leave_writer(session["job_id"], session["owner_cycle_id"])


def _operator_tools_exchange(
    generate: Callable[..., Optional[Dict[str, Any]]],
    *,
    system: str,
    prompt: str,
    objective: str,
    episode_id: str,
    max_tokens: int,
    enable_thinking: bool,
    timeout: Optional[float],
    chat_cycle_id: str,
    warnings: List[str],
    token_sink: Optional[Callable[[str], None]] = None,
    request_cycle_id: str = "",
    job_cycle_id: str = "",
    arrival_seq: int = 0,
    pipeline_id: Optional[int] = None,
    job_id: str = "",
    # T3: the thread this execution belongs to, so a question asked mid-batch records
    # the thread that asked it. Empty degrades to a new root, never to a wrong thread.
    thread_id: str = "",
    permission_mode: str | None = None,
    forced_tool: str = "",
    runtime_client: Any = None,
    work_kind: str | None = None,
    operator_route: str = "chat",
    browser_workflow_id: str | None = None,
    plugin_active_contributor: Mapping[str, object] | None = None,
    plugin_operation: Mapping[str, object] | None = None,
    # T9: the continuation's lane entry. EVERY tool below checks it first.
    lane_entry: Any = None,
    session: dict,
    acceptance_criteria: tuple = (),
) -> tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], str]:
    """The operator-facing reply, with the chat brain's tools on demand.

    One OpenAI-shaped transcript on the chat seat: the answer prompt goes up
    with the bounded tool table; a reply that names tools has them RUN here
    (``brain_skill_executor``'s jailed handlers) and their results appended as
    ``role=tool`` turns; the exchange repeats until the model answers in text.
    Past the round or wall-clock budget the model is asked once more WITHOUT
    tools, so the turn always ends in an answer.

    Everything the operator would want to watch goes to the waiting bubble as
    it happens (operator 2026-09-05): each pass's prose streams through
    ``token_sink`` (seq 0 restarts it per pass), the think trace rides
    ``token_sink.reasoning``, and every tool call and its result — output,
    error, the diff a write made — is a ``token_sink.event`` frame. The same
    ledger rides the finished reply's ``thinking`` so nothing depends on
    having watched.

    Returns ``(res, records, stop_reason)``: the final generate body (or None
    when the runtime declined), the tool-call ledger for the journal, and
    ``"interrupted"`` when a durable stop is consumed at a safe boundary,
    else ``""``. Playback interruption never cancels cognition.
    """
    from auto_loop.execute.brain_skill_executor import (
        OPERATOR_TOOL_BUDGET_S,
        OPERATOR_TOOL_MAX_ROUNDS,
        OPERATOR_PLUGIN_MAX_CALLS,
        ToolResult,
        _workspace_snapshot,
        capture_declared_verification as executor_capture_verification,
        operator_tool_context,
        operator_tool_preamble,
        operator_tool_specs,
        run_operator_tool,
    )

    if lane_entry is not None:
        # THE EXECUTION BOUNDARY (T9, review 2026-09-17): the deterministic routes below run a
        # tool AHEAD of the first inference, so guarding inference alone let a continuation
        # whose mail it no longer owned list a directory before it was refused. No tool runs
        # for a continuation whose lane-entry mail has not been delivered.
        _unguarded_tool = run_operator_tool

        def run_operator_tool(*tool_args, **tool_kwargs):
            lane_entry.require_delivered()
            return _unguarded_tool(*tool_args, **tool_kwargs)

    from dataclasses import replace
    from pathlib import Path
    from auto_loop.execute import job_control as jobs
    from pressure_loop import cognition_queue as queue
    from runtime.client import RuntimeStreamError
    from meta_loop.browser.workflow import (
        browser_workflow_prompt,
        browser_workflow_receipt,
    )

    if operator_route == "browser":
        return None, [], "browser execution runs the browser agent from a BROWSER_ACTION perception pack"

    owning_cycle = job_cycle_id or chat_cycle_id or episode_id
    plugin_selector = ""
    if plugin_operation is not None:
        from mcp_platform.plugin_directory import _identity

        if not isinstance(plugin_operation, Mapping) or set(plugin_operation) != {"plugin_id"}:
            raise ValueError("plugin_selector_invalid")
        plugin_selector = _identity(plugin_operation["plugin_id"])
    ctx = replace(
        operator_tool_context(
            objective,
            permission_mode=permission_mode,
            should_stop=_operator_turn_should_stop(chat_cycle_id or episode_id),
            # Answering ordinary chat must not require a filesystem grant.
            # The context marks workspace tools unavailable until /trust + /cd
            # establish authority; unexpected jail failures still propagate.
            allow_missing_workspace=True,
        ),
        cycle_id=owning_cycle,
        arrival_seq=arrival_seq,
        runtime_client=runtime_client,
        episode_id=episode_id,
        thread_id=thread_id,
        pipeline_id=pipeline_id,
        work_kind=work_kind,
        operator_route=str(operator_route or "chat").strip().lower(),
        plugin_active_contributor=plugin_active_contributor,
        plugin_selector=plugin_selector,
        acceptance_criteria=tuple(str(item) for item in acceptance_criteria or ()),
    )
    decision_settled = False
    control_turn = False
    if ctx.operator_route == "agent" and ctx.workspace_available:
        # The trusted workspaces enter dev memory once per change, from the agent episode
        # that first works after it; remember_dev recalls them on later agent turns.
        from memory.dev_observation_intake import hold_trusted_workspaces

        hold_trusted_workspaces(episode_id=episode_id)

    def settle(outcome: str) -> None:
        nonlocal decision_settled
        if not decision_settled:
            if arrival_seq:
                queue.mark_operator_turn_interpreted(arrival_seq, owning_cycle, outcome=outcome)
            decision_settled = True

    def own(job) -> bool:
        nonlocal ctx
        if not jobs.enter_writer(job.job_id, owning_cycle):
            return False
        session.update(job_id=job.job_id, cursor=job.handled_revision, owned=True, owner_cycle_id=owning_cycle)
        ctx = replace(ctx, job_id=job.job_id, workspace_root=Path(job.workspace_root),
                      objective=job.objective, revision_cursor=job.handled_revision)
        bind_sink = getattr(token_sink, "bind_job", None)
        if callable(bind_sink):
            bind_sink(job.job_id)
        if pipeline_id is not None:
            from core.pipeline_state import bind_job

            # The pipeline is owned by this episode's own admission (a successor continuing
            # the operator's task has its own cycle); the job stays owned by the chat turn.
            bind_job(pipeline_id, request_cycle_id or owning_cycle, job.job_id)
        return True

    if job_id:
        # A turn restored after a crash re-owns the job its writer held — including one the
        # host stopped at boot, whose owner and status boot cleared (``reclaim_writer``).
        job = jobs.get_job(job_id)
        if not own(job):
            try:
                reclaimed = jobs.reclaim_writer(job_id, owning_cycle)
            except jobs.ProjectClaimConflict as conflict:
                # Ordinary contention, and the one expected refusal on this path: the project
                # was unowned while this job's writer was gone, so another build could take
                # it. Refused with the owner NAMED — no record is touched, and the work stands
                # until that job finishes or the operator stops it. Deliberately narrow: an
                # unreadable record and a failed write are not contention and still raise.
                settle("project_owned_elsewhere")
                if lane_entry is not None:
                    # DECLARED through T9's own channel for a path that builds no prompt, so
                    # the reason reaches AUTO's bounded failure instead of the generic "the
                    # lane returned without showing its required lane-entry mail" backstop.
                    # With required mail this raises and hands the mail back undelivered.
                    lane_entry.cannot_consume(
                        f"job {conflict.owner_job_id} owns the project this task needs")
                return {"text": f"Job {conflict.owner_job_id} is working in that project, so "
                                "this task cannot continue until it finishes or is stopped."}, [], ""
            if reclaimed is None or not own(reclaimed):
                settle("job_already_owned")
                return {"text": "This job already has an active writer."}, [], ""
    specs = operator_tool_specs(
        ctx.permission_mode,
        workspace_available=ctx.workspace_available,
        operator_route=ctx.operator_route,
    )
    workflow_prompt = browser_workflow_prompt(browser_workflow_id)
    initial_user_prompt = (
        f"{workflow_prompt}\n\n{prompt}" if workflow_prompt else prompt
    )
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": f"{system}\n\n{operator_tool_preamble(ctx)}"},
        {"role": "user", "content": initial_user_prompt},
    ]
    records: List[Dict[str, Any]] = session["records"]
    started = time.monotonic()
    res: Optional[Dict[str, Any]] = None
    plugin_permission_pending = False
    plugin_calls_used = 0
    # An agent-routed action may use one corrective model pass before its single
    # tool batch. This catches text-only promises ("I'll create it now") without
    # allowing multiple execution batches inside one episode.
    agent_noop_replans = 0
    publish = getattr(token_sink, "event", None)
    reasoning_sink = getattr(token_sink, "reasoning", None)
    restart = getattr(token_sink, "restart", None)

    def _final_browser_response(response):
        return dict(response) if isinstance(response, Mapping) else None

    # A blocked receipt is a deliberate terminal safety boundary.  Do not let
    # the model or the deterministic route spend another browser pass until
    # the operator supplies a new, explicit workflow (or closes this one).
    blocked_receipt = browser_workflow_receipt(browser_workflow_id)
    if blocked_receipt and blocked_receipt.get("status") == "blocked":
        detail = str(blocked_receipt.get("last_error") or "browser action failed")[:500]
        return {
            "ok": True,
            "text": (
                f"Browser workflow blocked after repeated failures: {detail}\n\n"
                "What would you like to do next, or are we finished?"
            ),
            "tool_calls": [],
            "browser_workflow": blocked_receipt,
        }, [], ""

    def _frame(kind: str, **fields: Any) -> None:
        if callable(publish):
            if session["job_id"]:
                fields["job_id"] = session["job_id"]
            publish(kind, **fields)

    def _execute_deterministic_workspace_route() -> ToolResult | None:
        """Inspect the active trusted folder before answer generation."""
        if forced_tool != "list_directory" or any(
            isinstance(record, Mapping) and record.get("tool_name") == "list_directory"
            for record in records
        ):
            return None
        call_id = "workspace-route-0"
        args = {"path": "."}
        _frame(
            "chat.tool_call",
            call_id=call_id,
            tool="list_directory",
            args=args,
            narration="",
            round=0,
        )
        tool_started = time.monotonic()
        try:
            result, parsed_args = run_operator_tool(
                "list_directory", json.dumps(args, separators=(",", ":")), ctx
            )
        except Exception as exc:  # defensive: run_operator_tool normally contains failures
            result, parsed_args = ToolResult(
                False,
                error=f"list_directory crashed: {type(exc).__name__}: {exc}",
            ), args
        duration_ms = int((time.monotonic() - tool_started) * 1000)
        record = {
            "tool_call_id": call_id,
            "tool_name": "list_directory",
            "args": dict(parsed_args or args),
            "status": "success" if result.ok else "failure",
            "output": str(result.output or "")[:2000],
            "error": str(result.error or "")[:500],
            "diff": "",
            "duration_ms": duration_ms,
            "touched_files": [],
            "held_in_memory": [],
            "job_id": ctx.job_id,
            "cycle_id": ctx.cycle_id,
            "result": dict(result.meta or {}),
        }
        record["dev_memory"] = _hold_dev_tool_observation(
            episode_id=episode_id,
            objective=objective,
            record=record,
            ctx=ctx,
        )
        records.append(record)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "auto",
                "operator_tool",
                episode_id=episode_id,
                status="ok" if result.ok else "fail",
                ok=result.ok,
                tool="list_directory",
                summary="deterministic trusted-folder inspection",
                error=(result.error or None) if not result.ok else None,
            )
        except Exception:
            pass
        _frame(
            "chat.tool_result",
            call_id=call_id,
            tool="list_directory",
            ok=result.ok,
            output=str(result.output or "")[:4000],
            error=str(result.error or "")[:500],
            diff="",
            duration_ms=duration_ms,
        )
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "list_directory",
                            "arguments": json.dumps(args, separators=(",", ":")),
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "list_directory",
                "content": result.message_content(),
            }
        )
        return result

    def poll(target=None) -> bool:
        nonlocal ctx
        if not session["owned"]:
            return False
        sink = messages if target is None else target
        before = len(sink)
        controls = jobs.poll_job_controls(sink, session["job_id"], session["cursor"])
        for row in sink[before:]:
            # The operator's held words, handed to the batch in flight. Keep them for the
            # receipt so Omega's foldback sees the same instruction the batch just read.
            content = str(row.get("content") or "")
            if content:
                session["operator_notes"].append(content)
        session["cursor"] = controls.revision_cursor
        ctx = replace(ctx, revision_cursor=controls.revision_cursor)
        if controls.stop_requested:
            jobs.acknowledge_job_stop(session["job_id"], through_revision=controls.revision_cursor)
        return controls.stop_requested

    def cancel(calls, reason):
        for call in calls:
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "name": str((call.get("function") or {}).get("name") or ""),
                             "content": f"Not executed: {reason}".lower()})

    if lane_entry is not None:
        from auto_loop.lane_entry import prompt_text

        # DELIVERY (T9). Here and nowhere earlier or later (review 2026-09-17):
        #  - AFTER every non-executing admission check above — the job's writer, a blocked
        #    workflow. Delivered sooner, a batch that was never admitted ("This job already
        #    has an active writer.") had already consumed its required instruction.
        #  - BEFORE the deterministic routes just below, which run a tool ahead of the first
        #    inference. The transcript is built, so this is the lane's real prompt.
        lane_entry.deliver(prompt_text(messages=messages))

    route_for_fallback = str(forced_tool or "")
    if forced_tool == "web_search":
        # Model-owned: the research prompt and tool table choose topic + site.
        # Do not run web_search with the raw operator sentence before generate.
        forced_tool = ""
    elif forced_tool == "list_directory":
        deterministic_result = _execute_deterministic_workspace_route()
        if deterministic_result is not None:
            forced_tool = ""

    # Signature -> digest of what that exact call last produced, for this batch only.
    seen_results: Dict[str, str] = {}
    stalled_rounds = 0
    for round_no in range(OPERATOR_TOOL_MAX_ROUNDS + 1):
        round_calls = 0
        round_repeats = 0
        if poll() or (ctx.should_stop is not None and ctx.should_stop()):
            return res, records, "interrupted"
        budget_exhausted = (
            round_no >= OPERATOR_TOOL_MAX_ROUNDS
            or (time.monotonic() - started) > OPERATOR_TOOL_BUDGET_S
        )
        exhausted = budget_exhausted or plugin_permission_pending
        if budget_exhausted:
            warnings.append("operator tool budget exhausted; the model was asked for a final answer")
        if round_no and callable(restart):
            # Each pass is its own utterance: seq 0 REPLACES the bubble's
            # pending text, and the HUD has already moved the previous pass's
            # prose into the trace when it saw the tool-call frames.
            restart()
        observed_cursor = session["cursor"]
        current_specs = specs
        if session["owned"]:
            current_job = jobs.get_job(session["job_id"])
            ceiling = jobs.job_tool_ceiling(current_job, continuation=True)
            if ceiling is not None:
                current_specs = [spec for spec in specs if spec["function"]["name"] in ceiling
                                 or (not current_job.project_root and spec["function"]["name"] == "start_job")]
            if ctx.operator_route == "agent" and jobs.job_is_stalled(current_job):
                # The task has reproduced its own results across steps. Its only honest
                # moves left are to finish or to say what it is blocked on; anything else
                # is another whole episode spent standing still (live 2026-09-20: seven).
                resolving = [spec for spec in current_specs
                             if spec["function"]["name"] in _RESOLVING_TOOLS]
                if resolving:
                    current_specs = resolving
                    warnings.append(
                        "this task reproduced its own results across steps; "
                        "only finishing moves offered")
            if current_job.project_root:
                # The project is claimed and this batch owns the writer: re-claiming is a
                # no-op that costs a round out of OPERATOR_TOOL_MAX_ROUNDS. Offering it is
                # how one live build spent 7 of its 16 calls (2026-09-20).
                current_specs = [spec for spec in current_specs
                                 if spec["function"]["name"] != "start_job"]
        if stalled_rounds >= _MAX_STALLED_ROUNDS:
            # Nothing new came back for two rounds. Leave only the moves that end a step, so
            # the batch resolves instead of milling to its budget.
            resolving = [spec for spec in current_specs
                         if spec["function"]["name"] in _RESOLVING_TOOLS]
            if resolving:
                current_specs = resolving
                warnings.append("no new tool results for two rounds; only finishing moves offered")
        try:
            res = generate(
            operation="auto_response",
            prompt=None,
            system=system,
            # a snapshot: the transcript grows below, and what the seat saw on
            # THIS pass is what the audit (and a fake in a test) must keep
            messages=[dict(turn) for turn in messages],
            tools=None if exhausted else current_specs,
            # a forced first tool is the deterministic selection for this turn
            tool_choice=(
                None if exhausted
                else {"type": "function", "function": {"name": forced_tool}}
                if (forced_tool and round_no == 0 and any(spec["function"]["name"] == forced_tool for spec in current_specs))
                else "auto"
            ),
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
            timeout=timeout,
            token_sink=token_sink,
            reasoning_sink=reasoning_sink if callable(reasoning_sink) else None,
            request_job_id=session["job_id"],
            )
        except RuntimeStreamError as exc:
            settle(f"model_failed:{exc.reason}")
            records.append({"status": "failure", "kind": "inference", "error": str(exc),
                            "request_context": dict(exc.context)})
            fallback = _final_browser_response(
                _browser_response_or_fallback(None, records, forced_tool=route_for_fallback)
            )
            if fallback is not None:
                return fallback, records, ""
            return None, records, str(exc)
        if res is None:
            settle("model_failed:missing_result")
            return _final_browser_response(
                _browser_response_or_fallback(None, records, forced_tool=route_for_fallback)
            ), records, ""
        if not res.get("ok", True):
            settle("model_failed:" + str(res.get("reason") or "generation_failed"))
            fallback = _final_browser_response(
                _browser_response_or_fallback(None, records, forced_tool=route_for_fallback)
            )
            if fallback is not None:
                return fallback, records, ""
            return None, records, str(res.get("detail") or res.get("reason") or "generation failed")
        if session["owned"] and observed_cursor:
            jobs.acknowledge_job_controls(session["job_id"], through_revision=observed_cursor)
        calls = [call for call in (res.get("tool_calls") or ()) if isinstance(call, dict)]
        if calls and exhausted:
            settle("model_failed:decision_budget")
            return None, records, "Model proposed tools after its execution budget was exhausted"
        for index, call in enumerate(calls):
            call.setdefault("id", f"call-{round_no}-{index}")
        control_names = {"stop_job"}
        controls = [call for call in calls if (call.get("function") or {}).get("name") in control_names]
        starts = [call for call in calls if (call.get("function") or {}).get("name") == "start_job"]
        if starts and (len(starts) != 1 or calls[0] is not starts[0] or control_turn and not session["owned"]):
            messages.append({"role": "assistant", "content": "", "tool_calls": calls})
            cancel(calls, "start_job must be the first call of the batch, once, by this job's owner")
            continue
        if controls and len(controls) != len(calls):
            messages.append({"role": "assistant", "content": str(res.get("text") or ""), "tool_calls": calls})
            cancel(calls, "job controls cannot mix with workspace tools; replan")
            continue
        # Reject conflicting controls before the first write of this decision.
        seen_controls = {}
        conflict = False
        for call in controls:
            function = call["function"]
            args, _ = _preview_tool_args(function.get("arguments"))
            target = args.get("job_id")
            value = (function["name"], args.get("instruction"))
            if target in seen_controls and seen_controls[target] != value:
                conflict = True
            seen_controls[target] = value
        if conflict:
            messages.append({"role": "assistant", "content": "", "tool_calls": calls})
            cancel(calls, "conflicting controls for one arrival; replan")
            continue
        if calls and not controls and not session["owned"]:
            if control_turn:
                messages.append({"role": "assistant", "content": "", "tool_calls": calls})
                cancel(calls, "revision turn does not own this workspace job")
                continue
        if (
            not calls
            and not records
            and ctx.operator_route == "agent"
            and ctx.workspace_available
            and ctx.permission_mode != "read_only"
        ):
            if not exhausted and agent_noop_replans < 1:
                agent_noop_replans += 1
                messages.append({"role": "assistant", "content": str(res.get("text") or "")})
                messages.append({
                    "role": "user",
                    "content": (
                        "Agent-route correction: this is workspace action, so a text-only promise is not "
                        "completion. Use the available workspace tools now. For mutable work, start with "
                        "start_job and then perform the requested action. If execution is blocked by an "
                        "actual permission or workspace constraint, state that concrete constraint instead "
                        "of promising future work."
                    ),
                })
                continue
            settle("model_failed:agent_action_missing")
            return None, records, "agent_action_missing: agent route produced no tool calls after corrective replan"
        if not controls and not starts:
            settle("workspace_tools" if calls else "direct_answer")
        if not calls or exhausted:
            return _final_browser_response(
                _browser_response_or_fallback(res, records, forced_tool=route_for_fallback)
            ), records, ""
        narration = str(res.get("text") or "")
        messages.append({"role": "assistant", "content": narration, "tool_calls": calls})
        for index, call in enumerate(calls):
            prior_cursor = session["cursor"]
            pending_messages = []
            if poll(pending_messages) or (ctx.should_stop is not None and ctx.should_stop()):
                cancel(calls[index:], "durable stop requested")
                messages.extend(pending_messages)
                return res, records, "interrupted"
            if session["cursor"] != prior_cursor:
                cancel(calls[index:], "new job revision requires model replan")
                messages.extend(pending_messages)
                break
            function = dict(call.get("function") or {})
            name = str(function.get("name") or "")
            call_id = str(call.get("id") or f"call-{round_no}-{index}")
            args_preview, _parse_error = _preview_tool_args(function.get("arguments"))
            _frame(
                "chat.tool_call",
                call_id=call_id,
                tool=name,
                args=args_preview,
                narration=narration if index == 0 else "",
                round=round_no,
            )
            tool_started = time.monotonic()
            before = _workspace_snapshot(ctx.workspace_root) if name in _SNAPSHOT_TOOLS else None
            if name == "call_plugin_tool" and plugin_calls_used >= OPERATOR_PLUGIN_MAX_CALLS:
                result = ToolResult(False, error="plugin_call_budget_exhausted", meta={
                    "plugin_status": "failed", "plugin_failure": "plugin_call_budget_exhausted",
                })
                _capture_exchange_refusal(ctx, name, "budget_exhausted", result.error)
                args = args_preview
            elif (ctx.operator_route == "agent" and name in _PLAN_REQUIRED_TOOLS
                  and not _job_has_plan(session.get("job_id"))):
                result = ToolResult(False, error=(
                    f"plan_required: call task_plan with the steps this task needs before "
                    f"{name}. The plan is carried into every later step, so it is how you "
                    "remember what is left."))
                _capture_exchange_refusal(ctx, name, "plan_required", result.error)
                args = args_preview
            else:
                if name == "call_plugin_tool":
                    plugin_calls_used += 1
                tool_ctx = ctx
                if name == "run_command":
                    tool_ctx = replace(
                        ctx,
                        on_progress=lambda message: _frame(
                            "chat.tool_progress",
                            call_id=call_id,
                            tool=name,
                            message=message,
                        ),
                    )
                def _external_progress(message: str, elapsed_s: int) -> None:
                    _frame(
                        "chat.tool_progress",
                        call_id=call_id,
                        tool=name,
                        message=message,
                        elapsed_s=elapsed_s,
                    )

                result, args = _run_operator_tool_with_progress(
                    name,
                    lambda: run_operator_tool(name, function.get("arguments"), tool_ctx),
                    on_progress=_external_progress,
                )
            if name == "start_job" and result.ok:
                if not session["owned"] and not own(jobs.get_job(result.meta["job_id"])):
                    raise ValueError("new project claim lost its writer before execution")
                settle("project_claimed")
            # Did this exact call already produce exactly this? Then the round added nothing,
            # and repeating the payload would only re-teach the model what it already read.
            # The tool still RAN: the state it reports is real, only the answer is not new.
            signature = f"{name}:{json_compact(args)}"
            digest = _result_digest(result)
            round_calls += 1
            batch_repeat = seen_results.get(signature) == digest
            # The batch ledger cannot see across episodes, and on the agent lane a batch
            # IS an episode - so the job carries its own ledger and catches the only kind
            # of repeat this lane can make.
            task_repeat = (
                jobs.record_tool_echo(str(session["job_id"]), signature, digest)
                if not batch_repeat and ctx.operator_route == "agent" and session["job_id"]
                else False
            )
            if batch_repeat or task_repeat:
                round_repeats += 1
                where = ("an earlier step of this task" if task_repeat
                         else "an earlier call in this step")
                result = ToolResult(
                    result.ok,
                    output=(
                        f"no_progress: this {name} call returned exactly what {where} "
                        "returned; nothing changed. Do the next piece of the task, call "
                        "finish_task if the work is done, or ask_operator if you are blocked."
                    ),
                    error=result.error,
                    meta={**dict(result.meta or {}), "no_progress": True},
                )
            seen_results[signature] = digest
            after = _workspace_snapshot(ctx.workspace_root) if before is not None else None
            duration_ms = int((time.monotonic() - tool_started) * 1000)
            diff = str(result.meta.get("diff") or "") if result.ok else ""
            touched = _touched_paths(name, result, before, after, ctx.workspace_root)
            _capture_artifact_revisions(ctx, name, result, before, after, touched)
            executor_capture_verification(ctx, name, result)
            artifact_hold = _hold_dev_artifacts(episode_id, objective, touched, job_id=ctx.job_id)
            held = int(artifact_hold["held_count"])
            record = {
                "tool_call_id": call_id,
                "tool_name": name,
                "args": dict(args or {}),
                "status": "success" if result.ok else "failure",
                "output": str(result.output or "")[:2000],
                "error": str(result.error or "")[:500],
                "diff": diff[:6000],
                "duration_ms": duration_ms,
                "touched_files": touched,
                "held_in_memory": held,
                "dev_artifact_hold": artifact_hold,
                "job_id": ctx.job_id,
                "cycle_id": ctx.cycle_id,
                "result": dict(result.meta or {}),
            }
            record["dev_memory"] = _hold_dev_tool_observation(
                episode_id=episode_id,
                objective=objective,
                record=record,
                ctx=ctx,
            )
            records.append(record)
            _frame(
                "chat.tool_result",
                call_id=call_id,
                tool=name,
                ok=result.ok,
                output=str(result.output or "")[:4000],
                error=str(result.error or "")[:500],
                diff=diff[:6000],
                duration_ms=duration_ms,
                browser_setup=(
                    dict(result.meta.get("browser_setup") or {})
                    if isinstance(result.meta.get("browser_setup"), Mapping)
                    else None
                ),
                dev_artifact_hold=(
                    dict(artifact_hold)
                    if artifact_hold["status"] != "not_needed"
                    else None
                ),
                # The task's plan rows, so the trace draws a checklist live.
                plan=(
                    [dict(row) for row in result.meta.get("plan") if isinstance(row, Mapping)]
                    if isinstance(result.meta.get("plan"), list)
                    else None
                ),
            )
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "auto",
                    "operator_tool",
                    episode_id=episode_id,
                    status="ok" if result.ok else "fail",
                    ok=result.ok,
                    tool=name,
                    summary=(f"{name} {json_compact(args)}")[:120],
                    error=(result.error or None) if not result.ok else None,
                )
            except Exception:
                pass
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": result.message_content(),
                }
            )
            if result.meta.get("plugin_status") == "permission_required":
                # A pending permission owns one immutable plan. Stop dispatch
                # now; another call must not overwrite it or run ahead of it.
                plugin_permission_pending = True
                cancel(calls[index + 1:], "plugin permission is pending")
                break
        if controls:
            control_turn = True
            settle("job_controls_committed")
        if ctx.operator_route == "agent":
            # One tool batch per agent episode (operator 2026-09-14): the batch ran, and Omega
            # reviews its results and continues with a successor episode if work remains.
            return res, records, ""
    settle("model_failed:decision_budget")
    fallback = _final_browser_response(
        _browser_response_or_fallback(None, records, forced_tool=route_for_fallback)
    )
    if fallback is not None:
        return fallback, records, ""
    return None, records, "Model did not produce a valid decision within its execution budget"


def _result_digest(result: Any) -> str:
    """A stable fingerprint of what one tool call reported."""
    import hashlib

    body = f"{bool(getattr(result, 'ok', False))}|{getattr(result, 'output', '')}|{getattr(result, 'error', '')}"
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()


def json_compact(value: Any) -> str:
    """One-line JSON for terminal summaries; never raises."""
    import json

    try:
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:
        return str(value)


_SNAPSHOT_TOOLS = frozenset({"run_command", "run_tests"})


def _touched_paths(
    name: str,
    result: Any,
    before: Optional[Dict[str, tuple]],
    after: Optional[Dict[str, tuple]],
    root: Any,
) -> List[str]:
    """The files one tool call changed: the path a write/edit reports, or the
    workspace snapshot difference around a command (created, changed, deleted)."""
    touched: List[str] = []
    meta = dict(getattr(result, "meta", None) or {})
    if meta.get("changed") and meta.get("path"):
        touched.append(str(meta["path"]))
    if before is not None and after is not None:
        for relative in sorted(set(before) | set(after)):
            if before.get(relative) != after.get(relative):
                touched.append(str(root / relative))
    return touched


def _capture_exchange_refusal(ctx: Any, name: str, error_kind: str, error: str) -> None:
    """Record a model tool call the exchange refused before dispatch (WO-05 step 3).

    The attempt happened even though nothing ran; dropping it would erase the refusal's cause.
    Owner-gated like every other capture.
    """
    from memory.auto_evidence import attempt_recorder

    recorder = attempt_recorder(str(getattr(ctx, "episode_id", "") or ""),
                                lane=str(getattr(ctx, "operator_route", "") or ""))
    if recorder is not None:
        token = recorder.attempt(name)
        recorder.result(token, name, ok=False, error_kind=error_kind, error_detail=error)


def _acceptance_criteria_of(motor_program: Any) -> tuple[str, ...]:
    """The objective's acceptance criteria from a motor program (object or mapping), or ()."""
    from meta_loop.checks.run import criteria_of

    if motor_program is None:
        return ()
    to_dict = getattr(motor_program, "to_dict", None)
    return criteria_of(to_dict() if callable(to_dict) else motor_program)[0]


def _capture_artifact_revisions(
    ctx: Any,
    name: str,
    result: Any,
    before: Optional[Dict[str, tuple]],
    after: Optional[Dict[str, tuple]],
    touched: List[str],
) -> None:
    """Record the revisions one tool call left on disk (WO-05 step 4, the real writer seam).

    Owner-gated exactly like ``run_operator_tool``'s attempt capture: a thread owning nothing
    records nothing, and a thread bound to another episode fails loudly inside ``record_event``.
    A restore from memory is a rollback, never an ordinary edit.
    """
    from auto_loop.execute.brain_skill_executor import _owns_episode_for_capture

    meta = dict(getattr(result, "meta", None) or {})
    restored = [str(p) for p in meta.get("paths") or ()] if name == "restore_project_from_memory" else []
    if (not touched and not restored) or _owns_episode_for_capture(ctx) is None:
        return
    from memory.auto_evidence import capture_workspace_revisions

    root = ctx.workspace_root
    created = [str(meta["path"])] if meta.get("created") and meta.get("path") else []
    if before is not None and after is not None:
        created += [str(root / relative) for relative in set(after) - set(before)]
    if touched:
        capture_workspace_revisions(ctx, touched, root=root, created=created)
    if restored:
        capture_workspace_revisions(ctx, restored, root=root, rolled_back=True)


def _hold_dev_artifacts(
    episode_id: str, objective: str, paths: List[str], *, job_id: str = ""
) -> Dict[str, Any]:
    """Snapshot the files a chat tool touched into the episode's SRAM.

    ``memory.dev_artifact_intake`` is the built substrate for "I remember what
    I built": DeepSleep commits the held snapshots to the dev lobe (11), and a
    later "what did we build" recalls them through ``remember_dev``. The same
    intake is shared by the builder ledger and native file-tool records;
    verified non-byte observations (trees, strings, and operations) are held
    by ``memory.dev_observation_intake`` beside these snapshots. The status
    distinguishes unavailable intake from an empty successful intake; a
    recording failure never looks like a valid empty result.
    """
    if not paths:
        return {"status": "not_needed", "held_count": 0}
    ep_id = str(episode_id or "").strip()
    try:
        from memory import episode_sram
        from memory.dev_artifact_intake import hold_dev_artifact_snapshots

        if not ep_id or not episode_sram.is_open(ep_id):
            reason = f"no open SRAM bucket for episode {ep_id or '<none>'}"
            log.debug("dev artifacts not held: %s", reason)
            return {"status": "unavailable", "held_count": 0, "reason": reason}
        held = hold_dev_artifact_snapshots(
            episode_id=ep_id, objective=objective, paths=paths, job_id=str(job_id or "")
        )
        count = len(held)
        if count:
            return {"status": "held", "held_count": count}
        return {
            "status": "empty",
            "held_count": 0,
            "reason": "no eligible file snapshots",
        }
    except Exception as exc:  # noqa: BLE001 — preserve cause without failing the work
        log.warning("dev artifact hold failed (failure-soft)", exc_info=True)
        return {
            "status": "failed",
            "held_count": 0,
            "reason": f"{type(exc).__name__}: {exc}",
        }


def _hold_dev_tool_observation(
    *, episode_id: str, objective: str, record: Mapping[str, Any], ctx: Any
) -> Dict[str, Any]:
    """Hold a verified filesystem-tool observation in the dev lobe's SRAM."""
    try:
        from memory.dev_observation_intake import hold_dev_tool_observation

        return hold_dev_tool_observation(
            episode_id=episode_id,
            objective=objective,
            record=record,
            ctx=ctx,
        )
    except Exception as exc:  # preserve the cause in the ledger, never hide it
        log.warning("dev tool observation intake failed", exc_info=True)
        return {
            "status": "failed",
            "tool": str(record.get("tool_name") or ""),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _persist_dev_project_memory(
    *,
    paths: List[str],
    objective: str,
    episode_id: str,
) -> int:
    """Read SRAM/canonical snapshot history to produce builder change evidence."""
    if not paths:
        return 0
    from memory.dev_project_store import persist_project_files

    changes = persist_project_files(
        paths,
        objective=objective,
        episode_id=episode_id,
    )
    return len(changes)


def _preview_tool_args(raw: Any) -> tuple[Dict[str, Any], str]:
    """The arguments a tool call named, as a dict for the chat pane (the tool
    itself parses them again and reports its own error on bad JSON)."""
    if isinstance(raw, dict):
        return dict(raw), ""
    if raw in (None, ""):
        return {}, ""
    import json

    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        return {"_raw": str(raw)[:500]}, str(exc)
    return (dict(parsed) if isinstance(parsed, dict) else {"_raw": str(raw)[:500]}), ""


def _browser_primitive_result(value):
    """A bounded JSON fact in the existing scalar result receipt field."""
    from schema.browser_perception import public_url

    if not isinstance(value, Mapping):
        return "{}"
    receipt = {}
    # Only structural primitive results; never DOM, input text or credentials.
    for key in ("current", "locked", "handle", "session_id", "element_id",
                "url", "title", "x", "y", "start_x", "start_y", "end_x", "end_y",
                "delta_x", "delta_y"):
        item = value.get(key)
        if type(item) not in (str, int, bool):
            continue
        if isinstance(item, str):
            item = public_url(item) if key == "url" else item[:200]
        candidate = {**receipt, key: item}
        if len(json.dumps(candidate, ensure_ascii=True)) <= 900:
            receipt = candidate
        else:
            receipt["truncated"] = True
    handles = value.get("handles")
    if isinstance(handles, (list, tuple)):
        receipt["handles"] = []
        for handle in handles[:32]:
            if type(handle) is not str or len(handle) > 200:
                receipt["truncated"] = True
                continue
            receipt["handles"].append(handle)
            if len(json.dumps(receipt, ensure_ascii=True)) > 950:
                receipt["handles"].pop()
                receipt["truncated"] = True
                break
        if len(handles) > 32:
            receipt["truncated"] = True
    return json.dumps(receipt, ensure_ascii=True)


# Vision models downscale anything larger than this long edge anyway. Sending the raw
# full-resolution PNG only made the request body big enough that its upload outlived the
# cloud client's 5 s send budget (live 2026-09-14). Meta A's evidence hash stays bound to
# the original pixels; this is only the encoding the model receives.
_MODEL_SCREENSHOT_EDGE = 1568
_MODEL_SCREENSHOT_QUALITY = 80


def _screenshot_for_model(screenshot: bytes) -> str:
    """The perceived screenshot as a bounded JPEG data URL for the multimodal call."""
    import base64
    from io import BytesIO

    from PIL import Image

    try:
        with Image.open(BytesIO(screenshot)) as source:
            image = source.convert("RGB")
        image.thumbnail((_MODEL_SCREENSHOT_EDGE, _MODEL_SCREENSHOT_EDGE))
        encoded = BytesIO()
        image.save(encoded, format="JPEG", quality=_MODEL_SCREENSHOT_QUALITY, optimize=True)
    except (OSError, ValueError) as exc:
        raise ValueError("browser screenshot could not be encoded for the model") from exc
    return "data:image/jpeg;base64," + base64.b64encode(encoded.getvalue()).decode("ascii")


def _execute_browser_run(generate, pack, attachment, *, episode_id, token_sink=None,
                         direction="", before_action=None, should_stop=None):
    """Run Omega's browser agent from Meta A's perception; record the receipt; never judge it."""
    from auto_loop.execute import brain_skill_executor as executor
    from browser_agent import agent_loop
    from memory.auto_evidence import attempt_recorder
    from meta_loop.browser.workflow import (
        browser_progress_digest, operator_notes_for_step, record_browser_run,
    )
    from schema.browser_perception import BrowserPerceptionPack

    result = {"outputs": {}, "failure_kind": None, "failure_reason": None,
              "model_id": None, "warnings": [], "tokens": None, "inference_latency_s": 0.0}
    outputs = result["outputs"]
    stage = "browser_perception_invalid"
    try:
        inputs = pack["execution_inputs"]
        perception = BrowserPerceptionPack.from_dict(inputs["browser_perception"])
        workflow_id = str(inputs.get("browser_workflow_id") or "")
        objective = str(inputs.get("objective") or "")
        instruction = str(inputs.get("current_instruction") or objective)
        publish = getattr(token_sink, "event", None)
        if perception.status == "busy" or agent_loop.is_running():
            # Another episode's run owns the window: this turn's instruction was queued as a
            # note the running loop reads on its next step. Nothing else to do here.
            notes = 0
            if workflow_id:
                from meta_loop.browser.workflow import browser_workflow_receipt

                notes = int((browser_workflow_receipt(workflow_id) or {}).get("pending_notes") or 0)
            outputs["browser_run"] = {"status": "note_queued" if notes else "busy", "steps": 0, "actions": [],
                                      "final_page": {"url": "", "title": "", "text": ""}, "summary": "",
                                      "question": "", "stopped_reason": f"{notes} note(s) queued for the running browser task",
                                      "duration_s": 0.0, "operator_notes_seen": 0, "inference_failures": 0}
            return result
        workflow_status = str(dict(inputs.get("browser_workflow") or {}).get("status") or "")
        if workflow_status in {"completed", "stopped"}:
            # The operator ended a workflow that was not running (a close phrase with nothing
            # open): there is nothing to drive, and Omega tells them so.
            outputs["browser_run"] = {"status": "blocked", "steps": 0, "actions": [],
                                      "final_page": {"url": "", "title": "", "text": ""}, "summary": "",
                                      "question": "", "stopped_reason": "the browser workflow is closed; nothing to run",
                                      "duration_s": 0.0, "operator_notes_seen": 0, "inference_failures": 0}
            return result
        if perception.status not in {"success", "no_task_tab"}:
            raise ValueError(f"browser perception {perception.status}: {perception.error or 'page not perceived'}")
        controller = executor._browser_controller()
        # The window AUTO drives is the session Meta A perceived this episode, in this process.
        if not perception.session_id or controller.session_id != perception.session_id:
            raise ValueError("stale browser session")
        if perception.status == "success" and (perception.locked_window or "") != (controller.locked_window or ""):
            raise ValueError("stale browser locked_window")
        stage = "browser_run_failed"
        started = time.perf_counter()
        run = agent_loop.run_browser_agent(
            generate, controller, objective=objective, instruction=instruction,
            progress=browser_progress_digest(workflow_id) if workflow_id else "",
            notes_reader=(lambda: operator_notes_for_step(workflow_id)) if workflow_id else None,
            publish=publish if callable(publish) else None,
            stream=token_sink,
            # Omega's lane-entry direction for this run (T9), verbatim and never cut; and
            # the delivery gate no browser action may bypass.
            direction=direction, before_action=before_action, should_stop=should_stop,
            # WO-05 carry-over: every browser action attempt and its provable effect is
            # captured into this episode (None when this thread owns no episode).
            capture=attempt_recorder(episode_id, lane="browser"))
        result["inference_latency_s"] = time.perf_counter() - started
        run["cycle_id"] = episode_id
        outputs["browser_run"] = run
        outputs["operator_tool_calls"] = [
            {"tool_call_id": str(row.get("call_id") or f"browser-{row['step']}"), "tool_name": row["action"],
             "args": {"summary": row["summary"]},
             "status": "success" if row["ok"] else "failure", "output": row["summary"], "error": row["error"]}
            for row in run.get("actions") or ()]
        if workflow_id:
            receipt = record_browser_run(workflow_id, run)
            if receipt is None:
                raise ValueError(f"browser workflow {workflow_id!r} could not record the completed run")
            outputs["browser_workflow"] = receipt
        if run.get("status") in {"blocked", "inference_failed"}:
            result.update(failure_kind=stage, failure_reason=str(run.get("stopped_reason") or run["status"])[:500])
    except Exception as exc:
        result.update(failure_kind=stage, failure_reason=f"{type(exc).__name__}: {exc}"[:500])
    return result


def execute(
    prompt_pack: PromptPack,
    model_profile: Optional[str],
    execution_pack: Mapping[str, Any],
    memory_envelope: MemoryEnvelope,
    episode_id: str,
    *,
    runtime_client: Any = None,
    tool_invoker: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    memory: Optional[Callable[..., Any]] = None,
    temporal: Optional[Mapping[str, Any]] = None,
    opened_by: str = "",
    chat_cycle_id: str = "",
    pipeline_id: Optional[int] = None,
    work_kind: str = "autonomous",
    job_id: str = "",
    # T3: the cognitive thread of the episode running this phase, carried from the
    # carrier so a question asked mid-batch records the thread that asked it.
    thread_id: str = "",
    request_cycle_id: str = "",
    arrival_seq: int = 0,
    browser_perception_attachment: Any = None,
    # T9: the continuation's claimed lane-entry mail (auto_loop.lane_entry.LaneEntry). Its
    # block is rendered in the lane's prompt and delivered at the FIRST inference.
    lane_entry: Any = None,
) -> Dict[str, Any]:
    """PHASE 4 — model/tool/image execution.

    ``temporal`` is the episode's temporal envelope (core.episode_temporal) — the fixed
    per-episode clock relative datetimes resolve against.
    ``opened_by`` is the IGNITION origin (the episode's dominant contributor type),
    carried for audit metadata; plugin authority is verified by the signed MCP
    runtime, never inferred from this string.
    ``chat_cycle_id`` is the operator turn whose chat bubble is waiting on this
    episode's reply, or "" — the ONE address response tokens stream to (RISK 5).
    It is derived by ``operator_chat_responses.waiting_chat_turn``, the same
    function that decides where the FINISHED reply lands, so the two halves of an
    answer can never be painted into different bubbles.
    """
    from auto_loop.mril_tools import (
        is_browser_route,
        memory_recall_required,
        run_forced_memory_tools,
    )
    from pressure_loop.policy import (
        auto_generate_temperature,
        auto_generate_top_p,
        auto_profile_max_tokens,
    )

    from auto_loop.motor_adapter import effective_motor_action, motor_program_from_pack

    pack = dict(execution_pack)
    # Keep direct execute() callers on the same capability boundary as run_auto:
    # a browser action must never inherit a stale classifier memory contract.
    if is_browser_route(pack):
        pack.update(
            {
                "memory_required": False,
                "memory_answer_required": False,
                "memory_reason": None,
                "memory_query": None,
                "memory_scope": [],
                "memory_tools": [],
            }
        )
    action = effective_motor_action(pack)
    motor_program = motor_program_from_pack(pack)
    outputs: Dict[str, Any] = {}
    if str(pack.get("action") or "") == "EXECUTE_MOTOR_PROGRAM":
        outputs["effective_action"] = action
    failure_kind: Optional[str] = None
    failure_reason: Optional[str] = None
    model_id: Optional[str] = None
    warnings: List[str] = []
    inference_tokens: Optional[int] = None
    inference_latency_s: float = 0.0

    visual = (getattr(prompt_pack, "context_blocks", None) or {}).get("visual_intake")
    visual_response = None
    if visual:
        try:
            from pressure_loop.operator_vision import execute_visual_intake

            visual_report = execute_visual_intake(
                visual, text=str(prompt_pack.user_prompt or ""),
                cycle_id=chat_cycle_id or request_cycle_id or episode_id, episode_id=episode_id,
                source_citation=prompt_pack.context_blocks.get("visual_source_citation"),
            )
            outputs["visual_intake"] = visual_report
            if not visual_report["ok"]:
                errors = [str(row.get("error") or row.get("outcome"))
                          for row in visual_report["outcomes"] if not row.get("ok")]
                raise RuntimeError("; ".join(errors))
            visual_response = visual_report.get("response")
        except Exception as exc:
            return {
                "outputs": outputs, "failure_kind": "visual_intake_failed",
                "failure_reason": f"{type(exc).__name__}: {exc}", "model_id": None,
                "warnings": warnings, "tokens": None, "inference_latency_s": 0.0,
                "memory_envelope": memory_envelope,
            }

    execution_inputs = pack.get("execution_inputs")
    handoff = (
        execution_inputs.get("specialist_handoff")
        if isinstance(execution_inputs, Mapping)
        else None
    )
    if handoff is not None:
        # A specialist classification is made in Chat, but its work must be durably
        # admitted to the selected lane before AUTO resolves the operator's bubble. It
        # does not need a second model pass: the queue receipt is the deterministic
        # action result, and the target episode will do the work under its own lease.
        try:
            if not isinstance(handoff, Mapping):
                raise ValueError("specialist_handoff_must_be_an_object")
            if pipeline_id != 0:
                raise ValueError("specialist_handoff_must_run_on_chat_pipeline_0")
            if str(opened_by or "") not in {
                "operator_query", "operator_command", "operator_response",
            }:
                raise ValueError("specialist_handoff_requires_an_operator_opened_episode")
            lane = handoff.get("lane")
            cycle_id = str(handoff.get("original_cycle_id") or "")
            destination_thread = str(handoff.get("thread_id") or "")
            if not chat_cycle_id or cycle_id != str(chat_cycle_id):
                raise ValueError("specialist_handoff_cycle_does_not_match_chat_turn")
            if not thread_id or destination_thread != str(thread_id):
                raise ValueError("specialist_handoff_thread_does_not_match_chat_turn")
            if lane not in {"browser", "agent"}:
                raise ValueError(f"specialist_handoff_lane_invalid: {lane!r}")
            route_context = handoff.get("route_context")
            if type(route_context) is not str:
                raise ValueError("specialist_handoff_route_context_invalid")

            from pressure_loop.specialist_lane_handoff import stage_operator_lane_work

            kwargs = {
                "lane": lane,
                "request": handoff.get("request"),
                "cycle_id": cycle_id,
                "thread_id": destination_thread,
                "route_context": route_context,
                "arrival_seq": handoff.get("arrival_seq", 0),
            }
            if "selected_skill" in handoff:
                kwargs["selected_skill"] = handoff["selected_skill"]
            if lane == "browser":
                kwargs["browser_workflow"] = handoff.get("browser_workflow")
            receipt = stage_operator_lane_work(**kwargs)
            if (
                not isinstance(receipt, Mapping)
                or receipt.get("status") not in {"queued", "already_accepted"}
                or receipt.get("lane") != lane
                or receipt.get("original_cycle_id") != cycle_id
                or receipt.get("thread_id") != destination_thread
            ):
                raise ValueError("specialist_handoff_queue_receipt_invalid")
        except Exception as exc:  # noqa: BLE001 — preserve failure as AUTO episode content
            return {
                "outputs": {},
                "failure_kind": "specialist_lane_handoff_failed",
                "failure_reason": f"{type(exc).__name__}: {exc}"[:500],
                "model_id": None,
                "warnings": warnings,
                "tokens": None,
                "inference_latency_s": 0.0,
                "memory_envelope": memory_envelope,
            }

        destination = "Browser" if lane == "browser" else "Coding Agent"
        outputs["specialist_handoff"] = dict(receipt)
        outputs["response_text"] = (
            f"I’ve queued this for the {destination} lane. "
            "I’ll bring the result back to this conversation."
        )
        outputs["deterministic_response_kind"] = "specialist_lane_handoff"
        return {
            "outputs": outputs,
            "failure_kind": None,
            "failure_reason": None,
            "model_id": None,
            "warnings": warnings,
            "tokens": None,
            "inference_latency_s": 0.0,
            "memory_envelope": memory_envelope,
        }

    # Routing retirement (6M) on the HOT PATH — a raw world-mutating tool (write_file/
    # execute_command/…) is NEVER first-class execution; agentic work routes via USE_SKILL ->
    # qwen's internal tools. Fail CLOSED if one ever reaches AUTO as a USE_TOOL (a future
    # regression, not a normal path — the cascade exposes no such first-class tool today).
    if action == "USE_TOOL":
        from schema.routing_policy import is_world_mutating_raw

        if is_world_mutating_raw(str(pack.get("tool") or "")):
            return {
                "outputs": outputs,
                "failure_kind": "raw_tool_bypass_blocked",
                "failure_reason": f"raw world-mutating tool {pack.get('tool')!r} cannot be first-class execution; route agentic work via USE_SKILL",
                "model_id": None,
                "warnings": warnings,
                "tokens": None,
                "inference_latency_s": None,
                "memory_envelope": memory_envelope,
            }

    # Fail-hard: remember_* read failures return memory_continuity_substrate_invalid.

    if runtime_client is None:
        from runtime.client import get_runtime_client

        runtime_client = get_runtime_client()

    cycle_id = f"{episode_id}-auto"
    if tool_invoker is None:

        def invoke(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
            return _default_tool_invoker(
                tool_name,
                args,
                episode_id=cycle_id,
                runtime_client=runtime_client,
                pipeline_id=pipeline_id,
                work_kind=work_kind,
                cycle_id=request_cycle_id or chat_cycle_id or episode_id,
                job_id=job_id,
            )
    else:
        invoke = tool_invoker

    def _record_inference(operation: str, res: Any) -> None:
        """WO-05 step 1: record each model pass with the transmitted request's identity.

        Owner-gated: a thread owning no episode records nothing. The body's safe fields only.
        """
        from memory.auto_evidence import attempt_recorder, capture_inference

        recorder = attempt_recorder(episode_id, lane="auto")
        if recorder is not None:
            capture_inference(recorder, operation=operation, body=res)

    def _generate(
        *,
        operation: str,
        prompt: Optional[str],
        system: str,
        max_tokens: int,
        tools: Optional[List[Any]] = None,
        temperature: Optional[float] = None,
        enable_thinking: bool = False,
        timeout: Optional[float] = None,
        token_sink: Optional[Callable[[str], None]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        reasoning_sink: Optional[Callable[[str], None]] = None,
        request_job_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """One cognition pass on the shared /generate socket.

        ``messages`` carries a whole transcript (assistant tool_calls and their
        ``role=tool`` results) for the operator tool loop; the runtime forwards
        it verbatim. A tool-carrying pass streams too (2026-09-05): the gateway
        forwards tool-call fragments and the client reassembles them, so the
        prose reaches ``token_sink`` live and ``reasoning_sink`` hears the think
        trace, while the returned dict still carries whole ``tool_calls``.

        A continuation's lane-entry mail is DELIVERED here, at its first inference (T9):
        the host checks this very request carries the block, records it, and acknowledges
        the mail. A refusal raises — on every pass — so nothing is ever inferred after one.

        ``token_sink`` opts this pass into STREAMING (RISK 5): the same endpoint,
        the same lane slot, the same result dict — the tokens are merely handed
        over as the engine produces them instead of after it has finished. Only
        the operator-facing reply passes one. Every structured stage
        (JSON synthesis, tool_call dispatch, evaluation) parses a whole object
        and must keep waiting for one.

        Failed inference stays a failure of this pass. Delivery callbacks are
        isolated by the client; a provider failure never triggers regeneration.
        """
        nonlocal inference_tokens, inference_latency_s
        from runtime.socket import invoke_runtime_generate

        if lane_entry is not None:
            from auto_loop.lane_entry import prompt_text

            lane_entry.deliver(prompt_text(system, prompt, messages))
        t_gen = time.perf_counter()
        # timeout passed through only when set — otherwise the socket default applies
        # (a reasoning reply overrides it with headroom; fast replies keep the default).
        _gen_kwargs: Dict[str, Any] = {}
        if timeout is not None:
            _gen_kwargs["timeout"] = timeout
        if messages is not None:
            _gen_kwargs["messages"] = messages
        if tool_choice is not None:
            _gen_kwargs["tool_choice"] = tool_choice
        _gen_kwargs.update(
            pipeline_id=pipeline_id,
            work_kind=work_kind,
            cycle_id=request_cycle_id or chat_cycle_id or episode_id,
        )
        if request_job_id or job_id:
            _gen_kwargs["job_id"] = request_job_id or job_id
        streamed = token_sink is not None and response_streaming_enabled()
        res = None
        if streamed:
            _stream_kwargs: Dict[str, Any] = dict(_gen_kwargs)
            if tools:
                _stream_kwargs["tools"] = tools
            if reasoning_sink is not None:
                _stream_kwargs["on_reasoning"] = reasoning_sink
            try:
                res = invoke_runtime_generate(
                    operation=operation,
                    episode_id=cycle_id,
                    client=runtime_client,
                    method="generate_completion_stream",
                    on_token=token_sink,
                    prompt=prompt or "",
                    system=system,
                    max_tokens=max_tokens,
                    temperature=(
                        auto_generate_temperature() if temperature is None else temperature
                    ),
                    top_p=auto_generate_top_p(),
                    enable_thinking=enable_thinking,
                    **_stream_kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                from runtime.client import RuntimeStreamError
                if isinstance(exc, RuntimeStreamError):
                    raise
                raise RuntimeStreamError("stream_inference_failed", str(exc), context={
                    key: value for key, value in _gen_kwargs.items()
                    if key in {"pipeline_id", "work_kind", "cycle_id", "job_id"}
                }) from exc
            if res is None:
                from runtime.client import RuntimeStreamError
                raise RuntimeStreamError("stream_missing_result", "Streaming inference returned no completion", context={
                    key: value for key, value in _gen_kwargs.items()
                    if key in {"pipeline_id", "work_kind", "cycle_id", "job_id"}
                })
        if res is None:
            res = invoke_runtime_generate(
                operation=operation,
                episode_id=cycle_id,
                client=runtime_client,
                prompt=prompt or "",
                system=system,
                max_tokens=max_tokens,
                temperature=(auto_generate_temperature() if temperature is None else temperature),
                top_p=auto_generate_top_p(),
                enable_thinking=enable_thinking,
                tools=tools,
                **_gen_kwargs,
            )
        _record_inference(operation, res)
        elapsed = time.perf_counter() - t_gen
        elapsed_ms = int(elapsed * 1000)
        response_text = (
            str(res.get("text") or "").strip() if isinstance(res, Mapping) else ""
        )
        tool_call_count = (
            len([call for call in (res.get("tool_calls") or ()) if isinstance(call, Mapping)])
            if isinstance(res, Mapping)
            else 0
        )
        response_ok = bool(
            isinstance(res, Mapping)
            and res.get("ok", True)
            and (response_text or tool_call_count)
        )
        response_error = (
            str(res.get("reason") or "empty_completion")[:120]
            if isinstance(res, Mapping) and not response_ok
            else None
        )
        if res is not None:
            n = _token_count_from_generate(res)
            if n > 0:
                inference_tokens = (inference_tokens or 0) + n
                inference_latency_s += elapsed
        try:
            from guardian.terminal_feedback import emit_guardian_event, emit_inference

            emit_inference(
                "auto",
                operation,
                episode_id=cycle_id,
                phase=operation,
                latency_ms=elapsed_ms,
                status="ok" if response_ok else "fail",
                ok=response_ok,
                error=response_error,
                response_text_present=bool(response_text),
                tool_call_count=tool_call_count,
            )
            emit_guardian_event(
                "omega.auto",
                operation,
                episode_id=cycle_id,
                latency_ms=elapsed_ms,
                status="ok" if response_ok else "fail",
                ok=response_ok,
                error=response_error,
                response_text_present=bool(response_text),
                tool_call_count=tool_call_count,
                operation=operation,
            )
        except Exception:
            pass
        return res

    if action == "BROWSER_ACTION":
        token_sink = (_reply_token_sink(chat_cycle_id, episode_id,
                      pipeline_id=pipeline_id, work_kind=work_kind, job_id=job_id or None)
                      if chat_cycle_id else None)
        browser_result = _execute_browser_run(
            _generate, pack, browser_perception_attachment, episode_id=episode_id,
            token_sink=token_sink,
            direction=str(getattr(lane_entry, "block", "") or ""),
            before_action=getattr(lane_entry, "require_delivered", None),
            should_stop=_operator_turn_should_stop(chat_cycle_id or episode_id))
        browser_result["memory_envelope"] = memory_envelope
        browser_result["tokens"] = inference_tokens
        return browser_result

    def _max_tokens(profile: str) -> int:
        return auto_profile_max_tokens(profile)

    mril_results: Optional[List[Dict[str, Any]]] = None
    mril_memory_envelope: Optional[MemoryEnvelope] = None
    mril_recall_error: Optional[str] = None

    def _run_mril_recall() -> bool:
        nonlocal mril_results, mril_memory_envelope, mril_recall_error
        try:
            mril_results, mril_memory_envelope = run_forced_memory_tools(
                pack,
                episode_id,
                memory=memory,
            )
        except Exception as exc:
            mril_recall_error = str(exc)
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "auto",
                    "mril_recall_failed",
                    status="fail",
                    ok=False,
                    error=str(exc),
                    episode_id=episode_id,
                )
            except Exception:
                pass
            return False
        outputs["remembered_knowledge_tool_results"] = list(mril_results)
        return True

    # Recall is ALWAYS a system-side substrate read (no model hop) for any
    # memory_required episode — the fold-back Omega layer comprehends it. Empty
    # recall is VALID; a read error is a continuity-substrate failure (fail-hard).
    if memory_recall_required(pack):
        if not _run_mril_recall():
            return {
                "outputs": {},
                "failure_kind": "memory_continuity_substrate_invalid",
                "failure_reason": mril_recall_error,
                "model_id": None,
                "warnings": warnings,
                "tokens": None,
                "inference_latency_s": None,
                "memory_envelope": memory_envelope,
            }

    if action == "USE_TOOL":
        tool = pack.get("tool")
        if not tool:
            failure_kind = "missing_tool"
            failure_reason = "execution_pack.tool is null"
        else:
            inputs = {
                key: value
                for key, value in dict(pack.get("execution_inputs") or {}).items()
                if key not in {"motor_program", "motor_transport", "skill_execution_pack"}
            }
            if motor_program is not None and not str(inputs.get("query") or "").strip():
                inputs["query"] = motor_program.execution_plan.operations[0]
            order_domain = str(inputs.get("domain") or pack.get("domain") or "")
            if not failure_kind and inputs.get("web_research"):
                from auto_loop.execute.web_research import run_learn_web_research

                if lane_entry is not None:
                    lane_entry.cannot_consume("web research does not support lane-entry delivery")
                t0 = time.perf_counter()
                research = run_learn_web_research(
                    _generate,
                    seed=str(inputs.get("query") or ""),
                    domain=order_domain,
                    catalog=list(inputs.get("web_catalog") or ()),
                    invoke=invoke,
                    max_tokens=_max_tokens("TEXT_REASONING"),
                )
                tool_ms = int((time.perf_counter() - t0) * 1000)
                outputs["tool_args"] = {
                    "query": inputs.get("query"),
                    "domain": order_domain,
                    "web_research": True,
                }
                outputs["tool_runtime_ms"] = tool_ms
                if research.get("error"):
                    outputs["tool_status"] = "failed"
                    outputs["tool_result"] = research.get("tool_result") or {}
                    outputs["tool_error"] = str(research["error"])
                    failure_kind = "web_research_failed"
                    failure_reason = str(research["error"])
                elif not research.get("called"):
                    outputs["tool_status"] = "failed"
                    outputs["tool_result"] = research.get("tool_result") or {}
                    failure_kind = "web_research_no_tool_call"
                    failure_reason = (
                        "web research warranted but the model made no web tool call"
                    )
                else:
                    outputs["tool_result"] = research["tool_result"]
                    outputs["tool_status"] = "success"
                    _maybe_extract_knowledge(
                        pack,
                        outputs,
                        inputs,
                        order_domain,
                        str(tool),
                        warnings,
                        episode_id=episode_id,
                    )
                    provenance = _tool_provenance(
                        str(tool), outputs["tool_args"], outputs.get("tool_result")
                    )
                    try:
                        from guardian.terminal_feedback import emit_data

                        emit_data(
                            "auto",
                            "tool_invocation",
                            episode_id=episode_id,
                            summary=f"tool={tool} ok=True",
                            tool_name=str(tool),
                            action=action,
                            ok=True,
                            elapsed_ms=tool_ms,
                            query=provenance["query"],
                            domain=provenance["domain"],
                            learned_link=provenance["learned_link"],
                            source_name=provenance["source_name"],
                            result_count=provenance["result_count"],
                            knowledge_domain=str(outputs.get("knowledge_domain") or "")[:80],
                        )
                    except Exception:
                        pass
            elif not failure_kind:
                final_args = deterministic_args_from_order(
                    str(tool),
                    order_domain,
                    inputs,
                )
                t0 = time.perf_counter()
                try:
                    inv = invoke(str(tool), final_args)
                    tool_ms = int(inv.get("elapsed_ms", (time.perf_counter() - t0) * 1000))
                    outputs["tool_args"] = final_args
                    if not inv.get("ok", True):
                        outputs["tool_status"] = "failed"
                        outputs["tool_error"] = inv.get("error")
                        failure_kind = "tool_failed"
                        failure_reason = str(inv.get("error") or "tool reported ok=False")
                        try:
                            from guardian.terminal_feedback import emit_data

                            emit_data(
                                "auto",
                                "tool_invocation",
                                status="fail",
                                ok=False,
                                error=inv.get("error"),
                                tool=str(tool),
                                episode_id=episode_id,
                            )
                        except Exception:
                            pass
                    else:
                        outputs["tool_result"] = inv.get("result", inv)
                        outputs["tool_status"] = "success"
                        outputs["tool_runtime_ms"] = tool_ms
                        _maybe_extract_knowledge(
                            pack,
                            outputs,
                            inputs,
                            order_domain,
                            str(tool),
                            warnings,
                            episode_id=episode_id,
                        )
                        provenance = _tool_provenance(
                            str(tool), final_args, outputs.get("tool_result")
                        )
                        try:
                            from guardian.terminal_feedback import emit_data

                            emit_data(
                                "auto",
                                "tool_invocation",
                                episode_id=episode_id,
                                summary=f"tool={tool} ok=True",
                                tool_name=str(tool),
                                action=action,
                                ok=True,
                                elapsed_ms=tool_ms,
                                query=provenance["query"],
                                domain=provenance["domain"],
                                learned_link=provenance["learned_link"],
                                source_name=provenance["source_name"],
                                result_count=provenance["result_count"],
                                knowledge_domain=str(outputs.get("knowledge_domain") or "")[:80],
                            )
                        except Exception:
                            pass
                except Exception as exc:
                    outputs["tool_status"] = "failed"
                    outputs["tool_error"] = str(exc)
                    outputs["tool_args"] = final_args
                    failure_kind = "tool_failed"
                    failure_reason = str(exc)
                    try:
                        from guardian.terminal_feedback import emit_data

                        emit_data(
                            "auto",
                            "tool_invocation",
                            status="fail",
                            ok=False,
                            error=str(exc),
                            tool=str(tool),
                            episode_id=episode_id,
                        )
                    except Exception:
                        pass

    elif action == "IMAGE_CREATION":
        if pack.get("tool") != "image_tool":
            failure_kind = "invalid_execution_pack"
            failure_reason = "IMAGE_CREATION requires image_tool"
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "auto",
                    "image_creation_failed",
                    status="fail",
                    ok=False,
                    error=failure_reason,
                    episode_id=cycle_id,
                )
            except Exception:
                pass
        else:
            from runtime.socket import invoke_runtime_tool

            spec = prompt_pack.image_spec or {}
            prompt = str(spec.get("prompt") or prompt_pack.user_prompt or "")
            # Flux.2 Klein native resolution. The 860M pipeline (te=cpu encoder +
            # --max-vram graph-cut segmentation) renders 1024 within the iGPU's ~2GB
            # per-buffer Vulkan limit, so default to 1024 not 512 (operator 2026-07-01).
            img_w = int(spec.get("width") or 1024)
            img_h = int(spec.get("height") or 1024)
            img_steps = int(spec.get("steps") or 4)
            img_t0 = time.perf_counter()
            img = invoke_runtime_tool(
                "image_tool",
                operation="auto_image",
                episode_id=cycle_id,
                client=runtime_client,
                prompt=prompt,
                width=img_w,
                height=img_h,
                steps=img_steps,
                seed=spec.get("seed"),
            )
            img_ms = int((time.perf_counter() - img_t0) * 1000)
            try:
                from guardian.terminal_feedback import emit_inference

                emit_inference(
                    "auto",
                    "auto_image",
                    episode_id=cycle_id,
                    phase="auto_image",
                    latency_ms=img_ms,
                    ok=img is not None,
                    tool="image_tool",
                )
            except Exception:
                pass
            if img is None:
                failure_kind = "runtime_unavailable"
                failure_reason = "image_tool socket declined or returned None"
                try:
                    from guardian.terminal_feedback import emit_data

                    emit_data(
                        "auto",
                        "image_creation_failed",
                        status="fail",
                        ok=False,
                        error=failure_reason,
                        episode_id=cycle_id,
                    )
                except Exception:
                    pass
            else:
                import hashlib as _hl

                artifact: Dict[str, Any] = {
                    "bytes_len": len(img),
                    "sha256": _hl.sha256(img).hexdigest(),
                    "width": img_w,
                    "height": img_h,
                    "prompt": prompt,
                }
                try:
                    from guardian.terminal_feedback import emit_data, emit_guardian_event

                    emit_data(
                        "auto",
                        "image_artifact",
                        episode_id=cycle_id,
                        summary=f"bytes={artifact['bytes_len']}",
                        sha256=artifact["sha256"],
                        bytes_len=artifact["bytes_len"],
                    )
                    emit_guardian_event(
                        "omega.auto",
                        "image_artifact",
                        episode_id=cycle_id,
                        sha256=artifact["sha256"],
                        bytes_len=artifact["bytes_len"],
                    )
                except Exception:
                    pass
                # Persist the PNG to the canonical store so it survives the
                # episode and is browsable in the HUD gallery. The PATH (never
                # the bytes) rides in the artifact — raw bytes must not bloat
                # the episode outcome / shared-state projection. Persist failure
                # is a peripheral-artifact warning, not an episode failure.
                try:
                    from runtime.image_store import persist_generated_image

                    saved = persist_generated_image(img, prompt)
                    artifact["path"] = str(saved)
                    artifact["filename"] = saved.name
                    # Vision pipeline A (spec §3): Omega remembers what it
                    # makes — a screened VLM description of the generated
                    # image lands in [7,2,0]. Failure-soft peripheral step.
                    try:
                        from sensory.vision_ingest import describe_generated_image

                        artifact["description_outcome"] = str(
                            describe_generated_image(str(saved), episode_id=cycle_id).get("outcome")
                            or ""
                        )
                    except Exception:
                        warnings.append("image_describe_failed")
                except Exception as exc:
                    from guardian.terminal_feedback import emit_data

                    emit_data(
                        "auto",
                        "image_persist_failed",
                        status="degraded",
                        ok=False,
                        error=str(exc),
                        episode_id=cycle_id,
                    )
                    warnings.append(f"image_persist_failed: {exc}")
                outputs["image_artifact"] = artifact
                model_id = "image_tool"

    elif action == "DOCUMENT_CREATION":
        if pack.get("tool") != "document_tool":
            failure_kind = "invalid_execution_pack"
            failure_reason = "DOCUMENT_CREATION requires document_tool"
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "auto",
                    "document_creation_failed",
                    status="fail",
                    ok=False,
                    error=failure_reason,
                    episode_id=cycle_id,
                )
            except Exception:
                pass
        else:
            inputs = dict(pack.get("execution_inputs") or {})
            raw_spec = inputs.get("document_spec") or {}
            if isinstance(raw_spec, dict):
                fmt = str(raw_spec.get("format") or "md").lower().lstrip(".")
                template = str(raw_spec.get("template") or "generic")
                upload_ref = raw_spec.get("upload_ref")
                prompt = str(raw_spec.get("text") or prompt_pack.user_prompt or "").strip()
            else:
                fmt = "md"
                template = "generic"
                upload_ref = None
                prompt = str(raw_spec or prompt_pack.user_prompt or "").strip()
            title = prompt.splitlines()[0][:80] if prompt else "document"
            document_stage = "document_extract_failed"
            try:
                import hashlib as _hl

                from memory.document_evidence import (
                    capture_document_source,
                    capture_generated_document,
                    capture_verified_document,
                )
                from runtime.document_templates import build_fill_prompt

                source_text = prompt
                source_receipt = None
                source_event_id = None
                if upload_ref:
                    from guardian.external_ingress import harden, screen_text
                    from memory.auto_evidence import redaction_receipt
                    from runtime.document_upload import extract_upload_text

                    extracted = extract_upload_text(str(upload_ref))
                    if not extracted.strip():
                        raise ValueError(f"required document upload has empty extracted text: {upload_ref}")
                    extracted_bytes = extracted.encode("utf-8")
                    source_receipt = redaction_receipt(
                        source=str(upload_ref),
                        content_sha256=_hl.sha256(extracted_bytes).hexdigest(),
                        reason="Upload body withheld; digest identifies extracted text.",
                    )
                    source_receipt["extracted_text_bytes_len"] = len(extracted_bytes)
                    document_stage = "document_source_rejected"
                    screened, decision = screen_text(
                        extracted, source="document_upload", field="text",
                    )
                    source_receipt["guardian_decision"] = decision
                    document_stage = "document_capture_failed"
                    source_event_id = capture_document_source(episode_id, source=source_receipt)
                    document_stage = "document_source_rejected"
                    if decision not in {"allow", "flag"}:
                        raise ValueError(f"required document source received Guardian decision: {decision}")
                    source_text = (
                        f"{prompt}\n\nSource: operator-provided upload (untrusted)\n{harden(screened)}"
                    )

                document_stage = "document_template_failed"
                fill_prompt = build_fill_prompt(template=template, user_prompt=source_text, fmt=fmt)
                document_stage = "document_fill_failed"
                fill_res = _generate(
                    operation="auto_document",
                    prompt=fill_prompt,
                    system=prompt_pack.system_prompt,
                    max_tokens=_max_tokens("TEXT_REASONING"),
                )
                if fill_res is None:
                    document_stage = "runtime_unavailable"
                    raise RuntimeError("document fill preflight declined")
                generation_error = None
                if fill_res.get("ok") is False:
                    generation_error = str(
                        fill_res.get("error") or fill_res.get("reason")
                        or "document provider reported ok=False"
                    )
                markdown_body = str(fill_res.get("text") or "")
                if not markdown_body.strip():
                    raise ValueError(generation_error or "model returned empty markdown")

                # Capture the actual generated output before either later operation can fail.
                # This is episode intake; the artifact file is the action's deliverable, not
                # canonical memory. DeepSleep persists this event through the same seal lane.
                document_stage = "document_capture_failed"
                generated_event_id = capture_generated_document(
                    episode_id, markdown=markdown_body, title=title, template=template, fmt=fmt,
                    source=source_receipt, prompt=prompt,
                    request_digest=str(fill_res.get("request_digest") or ""),
                    source_event_id=source_event_id,
                    generation_error=generation_error,
                )
                if generation_error is not None:
                    document_stage = "document_fill_failed"
                    raise RuntimeError(generation_error)
                document_stage = "document_render_failed"
                from runtime.document_renderer import render_document

                rendered = render_document(markdown_body, fmt, title=title)
                payload = rendered.encode("utf-8") if isinstance(rendered, str) else bytes(rendered)
                document_stage = "document_persist_failed"
                from runtime.document_store import persist_document

                saved = persist_document(rendered, title, fmt)
                if saved.read_bytes() != payload:
                    raise ValueError(f"persisted document bytes differ from rendered output: {saved}")
                artifact: Dict[str, Any] = {
                    "bytes_len": len(payload),
                    "sha256": _hl.sha256(payload).hexdigest(),
                    "format": fmt,
                    "title": title,
                    "prompt": prompt,
                    "template": template,
                    "source": source_receipt,
                    "markdown_sha256": _hl.sha256(markdown_body.encode("utf-8")).hexdigest(),
                    "path": str(saved),
                    "filename": saved.name,
                }
                document_stage = "document_capture_failed"
                capture_verified_document(
                    episode_id, artifact=artifact, generated_event_id=generated_event_id,
                )
            except Exception as exc:
                failure_kind = document_stage
                failure_reason = str(exc)
                try:
                    from guardian.terminal_feedback import emit_data

                    emit_data(
                        "auto", document_stage, status="fail", ok=False,
                        error=failure_reason, episode_id=cycle_id, format=fmt,
                    )
                except Exception:
                    pass
            else:
                outputs["document_artifact"] = artifact
                model_id = "document_tool"
                try:
                    from guardian.terminal_feedback import emit_data, emit_guardian_event

                    emit_data(
                        "auto", "document_artifact", episode_id=cycle_id,
                        summary=f"bytes={artifact['bytes_len']} format={fmt}",
                        sha256=artifact["sha256"], bytes_len=artifact["bytes_len"], format=fmt,
                    )
                    emit_guardian_event(
                        "omega.auto", "document_artifact", episode_id=cycle_id,
                        sha256=artifact["sha256"], bytes_len=artifact["bytes_len"], format=fmt,
                    )
                except Exception:
                    pass

    elif action == "PLUGIN_OPERATION":
        execution_inputs = dict(pack.get("execution_inputs") or {})
        operation = execution_inputs.get("plugin_operation")
        objective = str(
            execution_inputs.get("plugin_objective")
            or (operation.get("objective") if isinstance(operation, Mapping) else "")
            or (operation.get("summary") if isinstance(operation, Mapping) else "")
            or (operation.get("capability") if isinstance(operation, Mapping) else "")
            or ""
        ).strip()
        response_objective = objective or str(getattr(prompt_pack, "user_prompt", "") or "Report the approved plugin operation.")
        from auto_loop.execute.plugin_plan import (
            plan_and_execute_plugin_request,
            plugin_plan_grant_from_contributor,
        )

        outcome = None
        approved_grant_id = ""
        permission_store = getattr(runtime_client, "permission_store", None)
        try:
            if failure_kind is None:
                grant = getattr(runtime_client, "plugin_plan_grant", None)
                permission_contributor = execution_inputs.get(
                    "plugin_permission_contributor"
                )
                if permission_contributor is not None:
                    grant = plugin_plan_grant_from_contributor(
                        runtime_client,
                        permission_contributor,
                    )
                    approved_grant_id = grant.grant_id
                    plan_loader = getattr(
                        permission_store,
                        "plan_for_grant",
                        None,
                    )
                    if not callable(plan_loader):
                        raise ValueError("plugin_permission_plan_unavailable")
                    execution_inputs["plugin_plan_override"] = plan_loader(
                        grant.grant_id
                    )
                    objective = "__approved_plugin_plan__"
                if not objective and execution_inputs.get("plugin_plan_override") is None:
                    failure_kind = "invalid_plugin_operation"
                    failure_reason = "PLUGIN_OPERATION requires a plugin objective"
                else:
                    outcome = plan_and_execute_plugin_request(
                        objective,
                        episode_id=episode_id,
                        runtime_client=runtime_client,
                        grant=grant,
                        explicit_operation=operation if isinstance(operation, Mapping) else None,
                        plan_override=execution_inputs.get("plugin_plan_override"),
                        pipeline_id=pipeline_id,
                        work_kind=work_kind,
                        cycle_id=request_cycle_id or chat_cycle_id or episode_id,
                        job_id=job_id,
                        # Plugin authorization is owned by the settings panel's
                        # selected scopes/capabilities and execution policy, not
                        # by the ordinary chat tool-mode gate.
                        active_contributor=(
                            {"source": "operator", "type": "command"}
                            if str(opened_by or "").startswith("operator")
                            else None
                        ),
                    )
        except Exception as exc:
            if approved_grant_id:
                revoke = getattr(permission_store, "revoke_grant", None)
                if callable(revoke):
                    try:
                        revoke(approved_grant_id)
                    except Exception:
                        log.warning(
                            "plugin grant cleanup failed for %s",
                            approved_grant_id,
                            exc_info=True,
                        )
            failure_kind = "plugin_execution_failed"
            failure_reason = str(exc) or "plugin execution failed"
            # The site, because the reason alone cannot name it:
            # `plugin_operation_failed` is raised from nineteen places in
            # plugin_backend and says the same thing at every one. Only Omega's
            # own source appears here -- never provider text, arguments, or a
            # result.
            log.warning(
                "plugin execution failed: %s (%s raised at %s)",
                failure_reason,
                type(exc).__name__,
                _raising_site(exc),
            )
        else:
            if outcome is not None:
                if approved_grant_id and outcome.status != "completed":
                    revoke = getattr(permission_store, "revoke_grant", None)
                    if callable(revoke):
                        try:
                            revoke(approved_grant_id)
                        except Exception:
                            log.warning(
                                "plugin grant cleanup failed for %s",
                                approved_grant_id,
                                exc_info=True,
                            )
                if outcome.plan is not None:
                    from mcp_platform.plugin_planning import plain_arguments

                    plan = outcome.plan
                    outputs["plugin_plan"] = {
                        "plan_id": plan.plan_id,
                        "episode_id": plan.episode_id,
                        "objective_sha256": plan.objective_sha256,
                        "authority_digest": plan.authority_digest,
                        "steps": [
                            {
                                "step_id": step.step_id,
                                "candidate_id": step.candidate_id,
                                "arguments": plain_arguments(step.arguments),
                                "consumes_step_ids": list(step.consumes_step_ids),
                            }
                            for step in plan.steps
                        ],
                    }
                outputs["plugin_step_results"] = [
                    {
                        "step_id": result.step_id,
                        "candidate_id": result.candidate_id,
                        "status": result.status,
                        "summary": result.summary.value,
                    }
                    for result in outcome.step_results
                ]
                if outcome.permission_request is not None:
                    outputs["plugin_permission_request"] = (
                        outcome.permission_request.to_hud_dict()
                    )
                if outcome.plugin_result is not None:
                    outputs["plugin_result"] = dict(outcome.plugin_result)
                if outcome.status == "completed":
                    # Approved continuations retain their authority-bound plan,
                    # but its admitted result still returns to the same brain
                    # for the answer. This pass cannot propose further actions.
                    try:
                        if not isinstance(outcome.plugin_result, Mapping):
                            raise ValueError("plugin_result_missing")
                        answer_request = _plugin_response_request(response_objective, outcome.plugin_result)
                        answer = _generate(
                            operation="auto_response", prompt=answer_request.user,
                            system=answer_request.system, max_tokens=_max_tokens("TEXT_FAST"),
                            tools=None,
                        )
                        if not isinstance(answer, Mapping) or not answer.get("ok", True):
                            raise ValueError(str((answer or {}).get("reason") or "plugin_response_inference_unavailable"))
                        if answer.get("tool_calls"):
                            raise ValueError("plugin_response_proposed_unrequested_tools")
                        text = str(answer.get("text") or "").strip()
                        if not text:
                            raise ValueError("plugin_response_empty")
                        outputs["response_text"] = text
                    except Exception as exc:
                        failure_kind = "response_generation_failed"
                        failure_reason = str(exc) or "plugin response generation failed"
                if outcome.failure_reason:
                    outputs["plugin_failure"] = outcome.failure_reason
                if outcome.status == "permission_required":
                    failure_kind = "pending_input"
                    failure_reason = "plugin permission required"
                elif outcome.status == "ask_user":
                    failure_kind = "pending_input"
                    failure_reason = outcome.failure_reason
                elif outcome.status == "failed":
                    failure_kind = "plugin_failed"
                    failure_reason = outcome.failure_reason or "plugin execution failed"
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "auto",
                "plugin_operation",
                status="fail" if failure_kind else "ok",
                ok=not failure_kind,
                summary=str(
                    outputs.get("plugin_receipt_summary") or failure_reason or "plugin operation"
                )[:160],
                episode_id=cycle_id,
                error=failure_reason or None,
            )
        except Exception:
            pass

    elif action == "USE_SKILL":
        # Skill-mediated brain dispatch — AUTO is the only place tool execution starts.
        # The SkillExecutionPack rides in execution_inputs (Meta A, 6O). dispatch_skill_pack
        # loads the SKILL.md, guards, and runs a native tool transcript on the same
        # Mistral seat used by chat. The normalized compatibility result
        # (6J) lands in outputs for the Meta B / Global B skill foldback (6K).
        _inputs = dict(pack.get("execution_inputs") or {})
        _sp = _inputs.get("skill_execution_pack") or {}
        if not _sp:
            failure_kind = "missing_skill_pack"
            failure_reason = (
                "USE_SKILL requires a skill_execution_pack (Meta A skill selection produced none)"
            )
        else:
            from auto_loop.skill.run import dispatch_skill_pack
            from schema.skill_execution_pack import SkillExecutionPack

            sp = SkillExecutionPack(
                episode_id=str(_sp.get("episode_id") or ""),
                dispatch_id=str(_sp.get("dispatch_id") or ""),
                objective=str(_sp.get("objective") or ""),
                selected_skill=dict(_sp.get("selected_skill") or {}),
                allowed_tools=tuple(_sp.get("allowed_tools") or ()),
                forbidden_tools=tuple(_sp.get("forbidden_tools") or ()),
                safety=dict(_sp.get("safety") or {}),
                limits=dict(_sp.get("limits") or {}),
                evidence_required=tuple(_sp.get("evidence_required") or ()),
                completion_criteria=tuple(_sp.get("completion_criteria") or ()),
                expected_output_schema=dict(_sp.get("expected_output_schema") or {}),
                source_action_goal=dict(_sp.get("source_action_goal") or {}),
            )
            # The real skill_execution_guard is the dispatch_skill_pack DEFAULT (6I) — fed the
            # live policy deps. skill_dispatch_confirmed authorizes bounded writes: operator work
            # always, and self-driven work only when OMEGA_AUTONOMOUS_SKILL_EXEC is explicitly on
            # — the mind executes IN THE JAIL. confirmed only clears the confirmation
            # gate; the workspace jail, self-mod denylist, and command policy still bound it, and
            # a write skill with no operator-workspace root still fails closed at the guard.
            from auto_loop.skill.run import (
                default_brain_skill_invoker,
                default_command_policy,
                operator_workspace_root,
                skill_dispatch_confirmed,
            )

            _operator_authorized = skill_dispatch_confirmed(_sp.get("source_action_goal"))
            _skill_workspace_root = operator_workspace_root()
            _skill_permission_mode = None
            if chat_cycle_id or str(opened_by or "").startswith("operator"):
                from auto_loop.execute.chat_permissions import load_chat_permission_mode

                _skill_permission_mode = str(
                    _inputs.get("chat_permission_mode") or load_chat_permission_mode()
                )
            _skill_should_stop = None
            if chat_cycle_id:
                from pressure_loop.operator_chat_responses import work_halt_requested

                def _chat_skill_should_stop() -> bool:
                    # A playback barge-in stops speech only. Long-running skill work stops on
                    # the explicit work-halt signal (Esc, stop, or the operator's stop command).
                    return work_halt_requested(chat_cycle_id)

                _skill_should_stop = _chat_skill_should_stop
            result = dispatch_skill_pack(
                sp,
                confirmed=_operator_authorized,
                command_policy=default_command_policy,
                workspace_root=_skill_workspace_root,
                motor_program=motor_program.to_dict() if motor_program is not None else None,
                pipeline_id=pipeline_id,
                work_kind=work_kind,
                cycle_id=request_cycle_id or chat_cycle_id or episode_id,
                job_id=job_id,
                permission_mode=_skill_permission_mode,
                thread_id=thread_id,
                invoker=lambda directive, dispatch: default_brain_skill_invoker(
                    directive,
                    dispatch,
                    client=runtime_client,
                    should_stop=_skill_should_stop,
                    jail_root=_skill_workspace_root,
                    pipeline_id=pipeline_id,
                    work_kind=work_kind,
                    cycle_id=request_cycle_id or chat_cycle_id or episode_id,
                    job_id=job_id,
                    permission_mode=_skill_permission_mode,
                    # T7: the carrier's thread, so a job this dispatch opens records it.
                    thread_id=thread_id,
                ),
            )
            try:
                from guardian.terminal_feedback import emit_data

                _dispatched = result.get("status") == "executed"
                emit_data(
                    "auto",
                    "skill_dispatch",
                    status="ok" if _dispatched else "fail",
                    ok=_dispatched,
                    skill=sp.objective,
                    dispatch_id=sp.dispatch_id,
                    guard_status=result.get("guard_status"),
                    failure_kind=(
                        None if _dispatched else f"skill_{result.get('status') or 'failed'}"
                    ),
                    episode_id=episode_id,
                )
            except Exception:
                pass
            qr = dict(result.get("qwen_result") or {})
            outputs["skill_objective"] = sp.objective
            outputs["skill_dispatch"] = {
                "status": result.get("status"),
                "dispatch_id": result.get("dispatch_id"),
                "reason": result.get("reason"),
            }
            outputs["skill_guard_status"] = result.get("guard_status")
            outputs["skill_result"] = qr
            _files = list(qr.get("files_changed") or ())
            _violations: list[str] = []
            _maxf = int((sp.limits or {}).get("max_files_changed") or 0)
            if result.get("status") != "executed":
                # rejected / blocked / requires_confirmation / invoker_error
                failure_kind = "skill_" + str(result.get("status") or "failed")
                failure_reason = str(result.get("reason") or "skill dispatch did not execute")
                outputs["skill_status"] = result.get("status")
            else:
                _st = str(qr.get("status") or "failure")
                outputs["skill_status"] = _st
                if _st == "success":
                    # SELF-MODIFICATION DENYLIST (§8.8, invariant 2) — a "success" whose ledger
                    # touched the safety machinery (guard / dispatcher / denylist / mind-checkpoint),
                    # the staged-approval gate, or any mind-state dir is qwen disarming its OWN jail,
                    # never legitimate operator work. It fails CLOSED *regardless* of the file count
                    # (the attack hides under max_files_changed). The classifier fails safe — an
                    # out-of-jail or malformed path counts as a violation, so ambiguity blocks too.
                    from auto_loop.execute.self_mod_denylist import self_mod_violations

                    _violations = self_mod_violations(
                        {"ledger": {"touched_files": _files}},
                        workspace_root=_skill_workspace_root,
                    )
                    # runtime max_files_changed enforcement: a success that touched more files
                    # than the skill allows (e.g. a read_only skill that wrote) fails CLOSED.
                    _nfiles = len(_files)
                    # DETERMINISTIC EVIDENCE GATE REMOVED (operator 2026-07-31).
                    # It judged a BUILD by a SKILL property (execution_kind) against a fixed
                    # ledger rule the agent was never told, so an agent could finish in good
                    # faith and be recorded as skill_no_evidence — live episode b06b85ac, 138
                    # seconds spent and nothing banked. The bar now comes from the MIND'S
                    # OUTPUT: outcome_contract.acceptance_criteria, rendered into the agent's
                    # brief BEFORE it starts (auto_loop.execute.success_criteria.render_brief),
                    # with a miss reported back as `UNMET:` -> unresolved_blockers, which the
                    # foldback turns into a decision (retry / change approach / stop) instead
                    # of a hard failure here or an unbounded retry loop.
                    #
                    # What SURVIVES at this seam is safety, not verdicts: the self-mod denylist
                    # and the max_files_changed limit below. Those bound what a dispatch may
                    # DO; they do not grade whether it succeeded.
                    #
                    # ⚠️ The retired gate caught FABRICATED completions (operator 2026-06-27:
                    # qwen narrating invented file lists and passing test counts without ever
                    # calling write/run). That protection now rests on the mind's authored
                    # criteria being specific enough to expose an empty run, plus the
                    # directive's anti-fabrication boundary statements and the deterministic
                    # battery (core.build_verification). Deliberate trade.
                    if _violations:
                        outputs["skill_status"] = "failure"
                        outputs["skill_self_mod_violations"] = _violations
                        failure_kind = "skill_self_modification_blocked"
                        failure_reason = (
                            "skill dispatch touched protected Omega path(s): "
                            + ", ".join(_violations)
                        )
                    elif _nfiles > _maxf:
                        outputs["skill_status"] = "failure"
                        failure_kind = "skill_files_changed_exceeded"
                        failure_reason = f"skill changed {_nfiles} files; limit is {_maxf}"
                    else:
                        model_id = "brain_runtime"
                    # The bridge can report success while the local executor rejects the result
                    # for a safety, file-limit, or evidence violation. Foldback and memory must
                    # consume that authoritative executor verdict, not the bridge's claim.
                    if failure_kind and failure_kind not in ("skill_yielded", "skill_partial"):
                        qr["status"] = "failure"
                        qr["failure_class"] = str(failure_kind).removeprefix("skill_")
                        qr["failure_reason"] = str(failure_reason or "")
                        outputs["skill_result"] = qr
                else:
                    failure_kind = "skill_" + _st
                    failure_reason = str(qr.get("summary") or qr.get("failure_class") or _st)
            if _files and not _violations and len(_files) <= _maxf:
                _persist_dev_project_memory(
                    paths=_files,
                    objective=sp.objective,
                    episode_id=episode_id,
                )
            # An operator command owns a waiting HUD bubble on this same lane.
            # Resolve it with the executor's grounded final report (or its real
            # failure), not a queued acknowledgement and not a side-panel notice.
            if opened_by.startswith("operator"):
                _report = (
                    str(failure_reason or result.get("reason") or "").strip()
                    if failure_kind
                    else str(qr.get("summary") or "").strip()
                )
                if not _report:
                    _report = "The requested tool dispatch ended without a report."
                outputs["response_text"] = _report
                outputs["deterministic_response_kind"] = "skill_execution_result"

    elif action in {"RESPONSE", "PROACTIVE_RESPONSE"}:
        # Terminal operator reply generated HERE at AUTO: reasoning / question /
        # freeform answer the operator and the episode ENDS QUIET — the cognition_chain
        # gate skips the Omega fold-back for these prompt types. ACK is the exception:
        # it gets a CANNED acknowledgment (no inference) and folds back through Omega for
        # pressure only (ack normally routes to the operator_command path at ingress;
        # this is the in-cascade ack episode whose reply must stay a cheap ack).
        # PROACTIVE_RESPONSE uses the same execution-limb generator, but unlike a
        # terminal chat reply it continues into Omega so the produced application
        # transcript can be evaluated, presented, and folded through the lifecycle.
        prompt_type = str(getattr(prompt_pack, "prompt_type", "") or "")
        deterministic_response = str(
            (visual_response or {}).get("message")
            or (getattr(prompt_pack, "context_blocks", None) or {}).get("deterministic_response_text")
            or ""
        ).strip()
        user = str(getattr(prompt_pack, "user_prompt", "") or "")
        from pressure_loop.contributors import infer_operator_command_kind

        remember_action = (action == "RESPONSE" and not visual_response
                           and infer_operator_command_kind(user) == "remember")
        if lane_entry is not None and (
            remember_action
            or (action == "RESPONSE" and deterministic_response)
            or prompt_type == "RESPONSE_ACK"
        ):
            # These replies build NO prompt, so they cannot show lane-entry mail: required
            # mail refuses them here, before anything is answered (review 2026-09-17: a
            # canned "Acknowledged — on it." succeeded past a required injection).
            lane_entry.cannot_consume("this reply is deterministic and builds no prompt")
        if remember_action:
            from memory.episode_sram import prepare_persona_intake_for_action

            admitted = prepare_persona_intake_for_action(user)
            outputs["memory_intake_records"] = admitted
            if admitted:
                outputs["response_text"] = "Captured for memory: " + "; ".join(admitted) + "."
                outputs["deterministic_response_kind"] = "memory_intake_admitted"
            else:
                outputs["response_text"] = "Nothing was stored; no memory facts were found."
                outputs["deterministic_response_kind"] = "memory_intake_empty"
        elif action == "RESPONSE" and deterministic_response:
            outputs["response_text"] = deterministic_response
            outputs["deterministic_response_kind"] = str(
                (visual_response or {}).get("kind")
                or (getattr(prompt_pack, "context_blocks", None) or {}).get(
                    "deterministic_response_kind"
                )
                or ""
            )
        elif prompt_type == "RESPONSE_ACK":
            # Command-receipt only: tell the operator the routed command landed
            # without paying for either answer-engineering or response inference.
            outputs["response_text"] = "Acknowledged — on it."
            outputs["deterministic_response_kind"] = "command_acknowledgement"
        else:
            rk = outputs.get("remembered_knowledge_tool_results")
            # The Global/Meta route owns the action. Once that action is a RESPONSE,
            # PromptOrgan owns query type, answer form, deixis, and grounding
            # instructions. The transport PromptPack carries the original turn and
            # recall context into the organ; it does not author the final answer.
            reasoning_response = prompt_type in {
                "RESPONSE_REASONING",
                "PROACTIVE_REASONING",
            } or (prompt_type == "RESPONSE_QUESTION" and model_profile == "TEXT_REASONING")
            from auto_loop.scaffold.runtime import (
                temporal_answer_hits,
                verified_hits_from_results,
            )

            answer_hits = (
                temporal_answer_hits(user, rk)
                if prompt_type == "RESPONSE_TEMPORAL"
                else verified_hits_from_results(rk)
            )
            from memory.association_strength import build_verified_use_receipt

            association_use_receipt = build_verified_use_receipt(rk, answer_hits)
            if association_use_receipt is not None:
                outputs["association_use_receipt"] = association_use_receipt
            # Recall admission and answer authority are separate contracts.  A
            # general question may search memory for useful grounding and still
            # answer from model knowledge when that search is empty.  Only an
            # explicitly memory-bound turn must refuse rather than answer around
            # missing verified evidence.
            answer_memory_required = bool(
                pack.get("memory_answer_required", pack.get("memory_required"))
            ) or (prompt_type == "RESPONSE_TEMPORAL")
            # Preserve the live episode context that cannot come from canonical
            # recall yet.  These are host-owned/fenced blocks, not a return to
            # the retired static prompt pack: recent turns, the active question
            # thread, and a proactive execution directive are all specific data
            # for this answer model invocation.
            #
            # Gathered BEFORE the prompt is assembled: whether this turn has a
            # conversation behind it is itself an input to the NLG. A turn like
            # "and what about that?" needs its pronoun bound to the turn above,
            # and only this block knows whether there IS a turn above.
            answer_context: list[str] = []
            if outputs.get("visual_intake"):
                from guardian.external_ingress import harden as harden_visual

                descriptions = [str(row["description"]) for row in outputs["visual_intake"]["outcomes"]
                                if row.get("description")]
                if descriptions:
                    answer_context.append("Current-turn visual observations (external image content):\n"
                                          + harden_visual("\n".join(descriptions)))
            try:
                from memory.hot_buffer import render_recent_block

                recent = render_recent_block(exclude_text=user)
                if recent:
                    answer_context.append(recent)
            except Exception as exc:
                warnings.append(f"recent conversation unavailable: {exc}")
            context_blocks = dict(getattr(prompt_pack, "context_blocks", None) or {})
            for context_key in ("conversation_thread", "response_execution_directive"):
                context_value = str(context_blocks.get(context_key) or "").strip()
                if context_value:
                    answer_context.append(context_value)
            if getattr(lane_entry, "block", ""):
                # Omega's lane-entry direction (T9), verbatim: the terminal-agent and chat
                # packs both render their context blocks uncut.
                answer_context.append(lane_entry.block)
            route_context = str(
                (pack.get("execution_inputs") or {}).get("operator_route_context") or ""
            ).strip()
            if route_context:
                from guardian.external_ingress import harden as _harden_route_context

                answer_context.append(
                    "Intent-router follow-up binding (host context used only to resolve what the "
                    "current operator message refers to):\n" + _harden_route_context(route_context)
                )

            # THE ANSWER NLG. PromptOrgan reads the deterministic ingress label,
            # Meta A's recall contract, and the host-owned context, then builds
            # the one answer request. No second composer inference is involved.
            #
            # response_mode carries the chat-intent label verbatim
            # (question/reasoning/freeform/temporal); prompt_type is the same
            # decision one step downstream; sentence shape is the floor that
            # still works with the classifier off. Register selects the system
            # prompt AND the base directive, so the two can never disagree.
            from organ.promptorgan import PromptContext, phrase

            operator_route = str(
                (pack.get("execution_inputs") or {}).get("operator_route") or "chat"
            ).strip().lower()

            temporal_line = ""
            if temporal:
                from core.episode_temporal import temporal_prompt_line

                temporal_line = temporal_prompt_line(dict(temporal))
            answer_request = phrase(
                PromptContext(
                    kind="proactive" if action == "PROACTIVE_RESPONSE" else "chat",
                    question=user,
                    data={
                        "hits": answer_hits,
                        "memory_required": answer_memory_required,
                        "chat_intent": str(getattr(prompt_pack, "response_mode", "") or ""),
                        "prompt_type": prompt_type,
                        "multiturn": bool(answer_context),
                        "context_blocks": tuple(answer_context),
                        "temporal_line": temporal_line,
                        "operator_route": operator_route,
                        "web_research": bool(
                            (pack.get("execution_inputs") or {}).get("web_research")
                        ),
                        "web_catalog": list(
                            (pack.get("execution_inputs") or {}).get("web_catalog") or ()
                        ),
                        "query": str(
                            (pack.get("execution_inputs") or {}).get("query") or user
                        ),
                        "domain": str(
                            (pack.get("execution_inputs") or {}).get("domain") or ""
                        ),
                    },
                )
            )
            system = answer_request.system
            response_prompt = answer_request.user
            if os.environ.get("OMEGA_DEBUG_ANSWER_PROMPT"):
                # diagnostic only: the exact answer prompt of the last turn, for reading
                # back what the model saw (the response rail keeps hashes, not text)
                try:
                    from pathlib import Path as _DebugPath

                    _DebugPath("guardian/state/last_answer_prompt.txt").write_text(
                        str(system) + chr(10) * 2 + "===== USER =====" + chr(10) + str(response_prompt),
                        encoding="utf-8",
                    )
                except Exception:  # noqa: BLE001
                    pass
            thinking_was_on = reasoning_response
            # RISK 5: the ONE pass in the cascade whose latency the operator
            # actually feels. Its tokens go to the waiting chat bubble as the
            # engine produces them — over the chat socket the finished reply
            # already uses, on the same cycle_id, so nothing new is invented to
            # carry them. No bubble waiting (an autonomous episode, or a
            # fast-reply that already answered) means no sink and the pass runs
            # exactly as before.
            token_sink = (
                _reply_token_sink(
                    chat_cycle_id,
                    episode_id,
                    pipeline_id=pipeline_id,
                    work_kind=work_kind,
                    job_id=job_id or None,
                ) if chat_cycle_id else None
            )
            # TOOLS ON DEMAND (operator 2026-09-05: "one pipeline for chat ...
            # it will chat, use tools, search the web etc on command"). An
            # operator turn's reply pass carries the bounded tool table, and the
            # model decides from the words whether to read, write, run or look
            # something up before it answers. Same rail, same bubble, same
            # chat.reply: nothing routes the turn elsewhere. A pass that
            # carries tools cannot stream (tool_call deltas only mean anything
            # reassembled), so the reply lands whole; an autonomous
            # PROACTIVE_RESPONSE keeps the streamed, tool-less pass below.
            # An Omega successor continuing the operator's terminal-agent task runs its next
            # tool batch on this same rail and bubble. A Coding Agent specialist handoff starts
            # its first batch here as well; its opened_by is lane_work_dispatch rather than an
            # operator turn, but Global A already classified and admitted that work in Chat.
            agent_continuation = bool((pack.get("execution_inputs") or {}).get("agent_continuation"))
            specialist_agent_work = (
                str(opened_by or "") == "lane_work_dispatch" and operator_route == "agent"
            )
            operator_turn = action == "RESPONSE" and (
                str(opened_by or "").startswith("operator")
                or agent_continuation
                or specialist_agent_work
            )
            if operator_turn:
                forced_tool = str(answer_request.metadata.get("force_tool") or "")
                if agent_continuation:
                    response_prompt = (
                        _agent_continuation_prompt(pack.get("execution_inputs") or {}) + "\n\n" + response_prompt
                    )
                agent_notes: list[str] = []
                res, tool_records, stop_reason = _operator_tools_reply(
                    _generate,
                    notes_out=agent_notes,
                    lane_entry=lane_entry,
                    system=system,
                    prompt=response_prompt,
                    forced_tool=forced_tool,
                    objective=user,
                    episode_id=episode_id,
                    # T3: the asking episode's OWN thread, carried from the carrier
                    # through AUTO. A PromptOrgan request's metadata is not the ingress
                    # contributor, and a freshly rooted episode has no thread on its
                    # contributor at all, so the carrier is the only authority
                    # (review 2026-09-16).
                    thread_id=thread_id,
                    max_tokens=_max_tokens(
                        "TEXT_REASONING" if reasoning_response else "TEXT_FAST"
                    ),
                    enable_thinking=thinking_was_on,
                    timeout=(600.0 if thinking_was_on else None),
                    chat_cycle_id=chat_cycle_id or request_cycle_id,
                    job_cycle_id=str((pack.get("execution_inputs") or {}).get("job_cycle_id") or ""),
                    request_cycle_id=request_cycle_id,
                    warnings=warnings,
                    token_sink=token_sink,
                    arrival_seq=arrival_seq,
                    pipeline_id=pipeline_id,
                    # A continuation re-owns the job its earlier batch claimed rather than
                    # claiming a second one on the same project (Meta A carries the id).
                    job_id=str((pack.get("execution_inputs") or {}).get("job_id") or job_id or ""),
                    permission_mode=(
                        str((pack.get("execution_inputs") or {}).get("chat_permission_mode") or "")
                        or None
                    ),
                    runtime_client=runtime_client,
                    # WO-05 carry-over: the objective's own acceptance criteria, so a test run
                    # can declare which one it verifies (and nothing else can be claimed).
                    acceptance_criteria=_acceptance_criteria_of(motor_program),
                    work_kind=work_kind,
                    operator_route=operator_route,
                    browser_workflow_id=str(
                        (pack.get("execution_inputs") or {}).get("browser_workflow_id") or ""
                    ) or None,
                    plugin_active_contributor=(
                        {"source": "operator", "type": "command"}
                        if str(opened_by or "").startswith("operator") else None
                    ),
                    plugin_operation=(pack.get("execution_inputs") or {}).get("plugin_operation"),
                )
                if tool_records:
                    outputs["operator_tool_calls"] = tool_records
                    outputs.update(_native_plugin_outputs(tool_records))
                    _record_web_tool_result(outputs, tool_records)
                    if outputs.get("tool_result"):
                        _maybe_extract_knowledge(
                            pack,
                            outputs,
                            dict(pack.get("execution_inputs") or {}),
                            str((pack.get("execution_inputs") or {}).get("domain") or ""),
                            "web_fetch",
                            warnings,
                            episode_id=episode_id,
                        )
                    if outputs.get("plugin_permission_request") is not None:
                        failure_kind = "pending_input"
                        failure_reason = "plugin permission required"
                agent_receipt = _agent_run_receipt(
                    tool_records,
                    operator_route=operator_route,
                    text=str(res.get("text") or "") if isinstance(res, Mapping) else "",
                    stop_reason=stop_reason,
                    operator_notes=agent_notes,
                )
                if agent_receipt is not None:
                    outputs["agent_run"] = agent_receipt
                if operator_route == "browser":
                    browser_id = str(
                        (pack.get("execution_inputs") or {}).get("browser_workflow_id") or ""
                    ).strip()
                    if browser_id:
                        try:
                            from meta_loop.browser.workflow import browser_workflow_receipt

                            receipt = browser_workflow_receipt(browser_id)
                            if receipt:
                                outputs["browser_workflow"] = receipt
                        except Exception:
                            warnings.append("browser workflow receipt unavailable")
                if isinstance(res, dict) and str(res.get("reasoning") or "").strip():
                    # The think trace behind the answer, kept for the resolved
                    # thinking block and the journal (capped; display only).
                    outputs["reasoning"] = str(res["reasoning"]).strip()[:4000]
                if stop_reason == "interrupted":
                    outputs["response_text"] = "(stopped)"
                    outputs["deterministic_response_kind"] = "operator_interrupted"
                elif res is None:
                    if str(stop_reason or "").startswith("agent_action_missing:"):
                        failure_kind = "agent_action_missing"
                        failure_reason = stop_reason
                    else:
                        failure_kind = "runtime_unavailable"
                        failure_reason = stop_reason or "response generation preflight declined"
                else:
                    text = str(res.get("text") or "").strip()
                    # An agent batch's narration beside its tool calls is progress, not the answer;
                    # a reply that calls nothing (for example after Meta A's deterministic listing)
                    # is the answer.
                    if text and not (operator_route == "agent" and res.get("tool_calls")):
                        workflow = outputs.get("browser_workflow")
                        if isinstance(workflow, Mapping):
                            wf_status = str(workflow.get("status") or "")
                            if wf_status == "waiting_for_operator":
                                suffix = (
                                    "Those browser steps are complete. What would you like to do next, "
                                    "or are we finished?"
                                )
                                if suffix not in text:
                                    text = f"{text}\n\n{suffix}"
                            elif wf_status == "blocked":
                                detail = str(workflow.get("last_error") or "browser action failed")[:500]
                                text = (
                                    f"Browser workflow blocked after repeated failures: {detail}\n\n"
                                    "What would you like to do next, or are we finished?"
                                )
                        outputs["response_text"] = text
                    elif not (operator_route == "agent" and tool_records):
                        # An agent batch's tool results are the step's outcome; Omega answers
                        # the operator when it settles the work.
                        failure_kind = "response_generation_failed"
                        failure_reason = "model returned empty response"
            for _response_attempt in range(0 if operator_turn else 2):
                if _response_attempt and token_sink is not None:
                    # The retry REPLACES the first attempt's tokens — it is the
                    # same answer generated again, not a continuation of it.
                    token_sink.restart()
                res = _generate(
                    operation="auto_response",
                    prompt=response_prompt,
                    system=system,
                    # The reply carries ITS episode's temporary physical pipeline
                    # context; operator and autonomous work have no fixed affinity.
                    token_sink=token_sink,
                    max_tokens=_max_tokens(
                        "TEXT_REASONING"
                        if reasoning_response
                        else "TEXT_FAST"
                    ),
                    # Thinking budget ruling (operator 2026-07-19): chat reply templates
                    # run think-OFF except REASONING — the think-WITH turn keeps its
                    # reasoning channel; meta-mind passes govern themselves upstream.
                    enable_thinking=thinking_was_on,
                    # A reasoning reply (think-then-answer) can legitimately run long — the
                    # default ~180s ceiling cut it mid-think -> None -> runtime_unavailable
                    # (operator 2026-07-22, live RESOLVE_QUERY at 183s). Give it headroom;
                    # a plain (think-off) reply keeps the fast default.
                    timeout=(600.0 if thinking_was_on else None),
                )
                if res is None:
                    failure_kind = "runtime_unavailable"
                    failure_reason = "response generation preflight declined"
                    break
                text = str(res.get("text") or "").strip()
                if not text and thinking_was_on:
                    # The model generated only thinking tokens with no answer text.
                    # Retry once with thinking disabled so it produces the answer directly.
                    thinking_was_on = False
                    continue
                if not text:
                    failure_kind = "response_generation_failed"
                    failure_reason = "model returned empty response"
                else:
                    outputs["response_text"] = text
                break
            # Durable answer-lane record. ``response_prompt`` here is the final
            # text the answer model saw -- the host-built prompt after
            # recent-turn context and the clock line were prepended -- so a bad
            # answer can be read back against its exact input rather than
            # reconstructed. Guardian log only; the HUD keeps its live one-liner.
            from auto_loop.scaffold.runtime import emit_answer_event

            emit_answer_event(
                "answer_generated",
                ok=not failure_kind,
                query=user,
                hit_ids=list(answer_hits),
                memory_required=answer_memory_required,
                final_prompt=response_prompt,
                response_text=str(outputs.get("response_text") or ""),
                thinking=thinking_was_on,
                failure_kind=failure_kind or "",
            )
            if failure_kind and not str(outputs.get("response_text") or "").strip():
                # The response action still has a terminal outcome when the
                # model seat fails. Preserve the cause for foldback and resolve
                # the waiting HUD turn with it; an empty output is otherwise
                # indistinguishable from a reply that is still in flight.
                detail = str(failure_reason or failure_kind).strip()
                outputs["response_text"] = f"I could not generate a response: {detail}."
                outputs["deterministic_response_kind"] = "response_failure"

    elif action == "IDLE":
        pass

    return {
        "outputs": outputs,
        "failure_kind": failure_kind,
        "failure_reason": failure_reason,
        "model_id": model_id,
        "warnings": warnings,
        "tokens": inference_tokens,
        "inference_latency_s": inference_latency_s if inference_tokens else None,
        "memory_envelope": mril_memory_envelope or memory_envelope,
    }


def _build_failure_outcome(
    ai: AutoInput,
    *,
    failure_kind: str,
    failure_reason: str,
    memory_envelope: Optional[MemoryEnvelope] = None,
) -> AutoOutcomePack:
    mem = memory_envelope or MemoryEnvelope(
        memory_required=bool(ai.execution_pack.get("memory_required")),
        memory_status="skipped",
        memory_reason=ai.execution_pack.get("memory_reason"),
        memory_scope=tuple(ai.execution_pack.get("memory_scope") or ()),
        items=(),
        error=None,
    )
    auto = {
        "status": "failed",
        "cycle_kind": "fail",
        "memory_status": mem.memory_status,
        "model_profile": None,
        "outputs": {},
        "failure_kind": failure_kind,
        "failure_reason": failure_reason,
        "telemetry": {"runtime_ms": 0, "warnings": []},
    }
    return AutoOutcomePack(
        episode_id=ai.episode_id,
        source_action_goal=dict(ai.source_action_goal),
        execution_pack=dict(ai.execution_pack),
        auto=auto,
    )


def run(
    prompt_pack: PromptPack,
    model_profile: Optional[str],
    execution_pack: Mapping[str, Any],
    memory_envelope: MemoryEnvelope,
    episode_id: str,
    *,
    runtime_client: Any = None,
    tool_invoker: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    memory: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    return execute(
        prompt_pack,
        model_profile,
        execution_pack,
        memory_envelope,
        episode_id,
        runtime_client=runtime_client,
        tool_invoker=tool_invoker,
        memory=memory,
    )
