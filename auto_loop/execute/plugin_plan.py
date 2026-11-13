"""AUTO-only planning and execution for generic signed plugin operations."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Literal

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.ingress import (
    McpPlannerTextReceipt,
    QUARANTINE_MARKER,
    _guardian_result,
    screen_mcp_planner_text,
    validate_mcp_planner_text,
)
from mcp_platform.operation_contracts import (
    McpConnectionSnapshot,
    McpOperationProposal,
    McpToolAuthority,
)
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_authority import (
    PluginConnectionAuthority,
    PluginExecutionPolicy,
    plugin_plan_digest,
)
from mcp_platform.plugin_catalog import PluginToolBinding
from mcp_platform.plugin_permissions import PluginPermissionRequest, PluginPlanGrant
from mcp_platform.plugin_runtime import (
    PluginRetryAfterFence,
    PluginRuntimeError,
    plugin_retry_context,
)
from mcp_platform.plugin_planning import (
    plain_arguments,
    PluginOperationPlan,
    PluginOperationStep,
    PluginPlanDecisionKind,
    PluginPlannerContext,
    PluginPlannerMetadata,
    PluginStepResult,
    deterministic_plugin_arguments,
    parse_plugin_plan_decision,
    retrieve_plugin_candidates,
    validate_plugin_plan_provenance,
)

from auto_loop.execute.plugin_tool import run_plugin_plan_step

_MAX_STEPS = 4
_DIGEST = set("0123456789abcdef")
_SETTINGS_FAILURES = frozenset(
    {
        "plugin_connection_not_found",
        "plugin_connection_unavailable",
        "plugin_settings_required",
    }
)


_LOG = logging.getLogger("omega.core.auto.plugin")

def _raising_site(exc: BaseException) -> str:
    """Where in Omega's own source this was raised, as ``file.py:line in func``.

    `plugin_operation_failed` is raised from nineteen places in
    `plugin_backend` and reads identically at every one, so the reason alone
    could not say which. The first real plugin call typed into chat on
    2026-09-08 reached execution and returned exactly that, with nothing to
    work from. Only Omega's own file, line and function are read -- never a
    provider's text, arguments or result.
    """
    frame = None
    traceback = exc.__traceback__
    while traceback is not None:
        frame = traceback
        traceback = traceback.tb_next
    if frame is None:
        return "no traceback"
    code = frame.tb_frame.f_code
    return f"{os.path.basename(code.co_filename)}:{frame.tb_lineno} in {code.co_name}"


def _screen_failure(exc: BaseException) -> str:
    """Keep the cause while fencing provider text before operator presentation.

    The SITE is logged rather than returned. It belongs in the operator's
    answer least of all -- and adding it to the returned string tripped the
    planner-text screener, which then discarded the cause entirely and left an
    even barer `plugin_execution_failed`.
    """
    _LOG.warning(
        "plugin execution failed: %s raised at %s",
        type(exc).__name__,
        _raising_site(exc),
    )
    reason = str(exc)
    if type(exc) is PluginRuntimeError and re.fullmatch(r"plugin_[a-z0-9_]+", reason):
        return reason
    raw = f"plugin_execution_failed: {exc!r}"
    try:
        return screen_mcp_planner_text(raw, source="plugin-failure").value
    except ValueError:
        return "plugin_execution_failed"


@dataclass(frozen=True, slots=True)
class PluginExecutionOutcome:
    status: Literal["completed", "ask_user", "permission_required", "failed"]
    plan: PluginOperationPlan | None
    step_results: tuple[PluginStepResult, ...]
    permission_request: PluginPermissionRequest | None
    failure_reason: str | None
    plugin_result: Mapping[str, Any] | None = None


def _failed(
    reason: str,
    *,
    plan: PluginOperationPlan | None = None,
    results: Sequence[PluginStepResult] = (),
) -> PluginExecutionOutcome:
    return PluginExecutionOutcome("failed", plan, tuple(results), None, reason)


def _ask(
    question: str,
    *,
    plan: PluginOperationPlan | None = None,
) -> PluginExecutionOutcome:
    return PluginExecutionOutcome("ask_user", plan, (), None, question)


def _digest(value: object) -> bool:
    return type(value) is str and len(value) == 64 and set(value) <= _DIGEST


def _bindings_for(runtime_client: object) -> tuple[PluginToolBinding, ...]:
    resolver = getattr(runtime_client, "plugin_tool_bindings", None)
    if callable(resolver):
        result = resolver()
    else:
        result = getattr(runtime_client, "bindings", None)
        if result is None:
            service = getattr(runtime_client, "plugin_lifecycle_service", None)
            from mcp_platform.plugin_dispatch import plugin_tool_bindings

            result = plugin_tool_bindings(service=service)
    if not isinstance(result, Sequence) or any(
        type(item) is not PluginToolBinding for item in result
    ):
        raise ValueError("plugin_catalog_unavailable")
    return tuple(result)


def _objective_receipt(objective: str) -> McpPlannerTextReceipt:
    return screen_mcp_planner_text(objective, source="operator-objective")


def _metadata(bindings: Sequence[PluginToolBinding]) -> tuple[PluginPlannerMetadata, ...]:
    return tuple(
        PluginPlannerMetadata(
            item.candidate_id,
            screen_mcp_planner_text(item.title, source="catalog-title"),
            screen_mcp_planner_text(item.description, source="catalog-description"),
            screen_mcp_planner_text(item.capability, source="catalog-capability"),
            screen_mcp_planner_text(item.effect, source="catalog-effect"),
        )
        for item in bindings
    )


def _planner_words(value: str) -> list[str]:
    """Words for matching, with punctuation and separators treated as spaces.

    Two things this fixes, both measured live on 2026-09-08. Only ``'`` used to
    be replaced, so a word carrying a comma or a full stop never matched -- and
    the last word of a sentence is usually the operator's most specific one.
    "…to list my calendars." scored `list_events` ABOVE `list_calendars`,
    because "calendars." matched nothing while "calendar" appeared bare earlier
    in the sentence. And a capability was split on whitespace alone, so
    `list_calendars` was a single token and a tool's own name could never match
    at all.
    """
    return "".join(character if character.isalnum() else " " for character in value.casefold()).split()


def _score(objective: str, binding: PluginToolBinding) -> int:
    words = set(_planner_words(objective))
    fields = (binding.title, binding.description, binding.capability)
    return sum(10 for field in fields for word in _planner_words(field) if word in words)


#: Words that identify a plugin to a person but carry no provider meaning, so
#: naming one must not be read as naming that plugin.
_PLUGIN_NAME_STOPWORDS = frozenset(
    {"official", "com", "app", "desktop", "workspace", "mcp", "plugin", "the", "my"}
)


def _plugin_name_words(plugin_id: str) -> frozenset[str]:
    """The words a person would use for this plugin, from its own id.

    `google.workspace.calendar` -> {google, calendar}; `neon.official` -> {neon}.
    Nothing here is invented: the id is signed package identity, and the
    stopwords are the structural parts of it that name no provider.
    """
    parts = plugin_id.casefold().replace(".", " ").replace("_", " ").replace("-", " ").split()
    return frozenset(part for part in parts if part not in _PLUGIN_NAME_STOPWORDS)


def _named_plugin(objective: str, candidates: tuple[PluginToolBinding, ...]) -> str | None:
    """The one installed plugin this objective names, or None.

    Returns a plugin_id only when EXACTLY one installed plugin has all of its
    identifying words present in the objective. Naming none leaves the field
    alone; naming two is not a disambiguation and leaves it alone as well.
    """
    words = set(objective.casefold().replace("'", " ").replace(".", " ").split())
    named = {
        binding.plugin_id
        for binding in candidates
        if (identity := _plugin_name_words(binding.plugin_id)) and identity <= words
    }
    return next(iter(named)) if len(named) == 1 else None


def _catalog_digest(bindings: Sequence[PluginToolBinding]) -> str:
    return sha256_hex(
        canonical_bytes(
            [
                {
                    "candidate_id": item.candidate_id,
                    "input": item.input_schema_sha256,
                    "output": item.output_schema_sha256,
                    "capability_kind": item.capability_kind,
                    "connection": item.connection_revision,
                }
                for item in sorted(bindings, key=lambda item: item.candidate_id)
            ]
        )
    )


def _plan_catalog_digest(
    bindings: Sequence[PluginToolBinding], plan: PluginOperationPlan
) -> str | None:
    selected = tuple(
        next((item for item in bindings if item.candidate_id == step.candidate_id), None)
        for step in plan.steps
    )
    if any(item is None for item in selected):
        return None
    return _catalog_digest(tuple(item for item in selected if item is not None))


def _deterministic_plan(
    objective: McpPlannerTextReceipt,
    *,
    episode_id: str,
    candidates: tuple[PluginToolBinding, ...],
) -> PluginOperationPlan | None:
    text = validate_mcp_planner_text(objective)
    # Naming a plugin NARROWS the field before anything is scored. It can only
    # remove candidates, never promote one, so this cannot select a tool the
    # scorer would otherwise have passed over, and it cannot reach a plugin the
    # operator did not name. Without it the more explicit request was the one
    # that failed: "use the Google Calendar plugin to list my calendars" tied
    # six ways against four Neon `list_*` tools while the bare "list my
    # calendars" resolved cleanly.
    # Narrowing decides WHICH tool is chosen. It must not touch `candidates`
    # itself, because that tuple is also the authority basis: it is what
    # `parse_plugin_plan_decision` hashes into `plan.authority_digest`, and the
    # executor recomputes that digest over the caller's full candidate set
    # before it will run a step. Rebinding the name here made every resolved
    # plan fail at execution with `plugin_catalog_changed` -- the plan was
    # authorized against nine calendar tools and checked against all 431.
    pool = candidates
    named = _named_plugin(text, candidates)
    if named is not None:
        pool = tuple(item for item in candidates if item.plugin_id == named)
    scored = sorted(
        ((-_score(text, item), item.candidate_id, item) for item in pool),
        key=lambda row: (row[0], row[1]),
    )
    if not scored or scored[0][0] == 0:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        raise ValueError("plugin_plan_ambiguous")
    binding = scored[0][2]
    arguments = deterministic_plugin_arguments(text, binding)
    if arguments is None:
        return None
    context = PluginPlannerContext(
        episode_id,
        sha256_hex(canonical_bytes(text)),
        _MAX_STEPS,
    )
    from mcp_platform.plugin_planning import parse_plugin_plan_decision

    decision = parse_plugin_plan_decision(
        {
            "decision": "call_tool",
            "steps": [
                {
                    "step_id": "step-1",
                    "candidate_id": binding.candidate_id,
                    "arguments": arguments,
                    "consumes_step_ids": [],
                }
            ],
        },
        candidates=candidates,
        repair_used=False,
        context=context,
        prior_steps=(),
    )
    return decision.plan


def _explicit_plan(
    operation: Mapping[str, object],
    *,
    objective: McpPlannerTextReceipt,
    episode_id: str,
    bindings: tuple[PluginToolBinding, ...],
) -> PluginOperationPlan:
    arguments = operation.get("arguments", {})
    if not isinstance(arguments, Mapping):
        raise ValueError("plugin_arguments_invalid")
    matches = tuple(
        item
        for item in bindings
        if item.plugin_id == operation.get("plugin_id")
        and item.app_id == operation.get("app_id")
        and item.capability == operation.get("capability")
        and (
            operation.get("candidate_id") is None
            or item.candidate_id == operation.get("candidate_id")
        )
    )
    if len(matches) != 1:
        raise ValueError("plugin_candidate_ambiguous")
    binding = matches[0]
    context = PluginPlannerContext(
        episode_id,
        sha256_hex(canonical_bytes(validate_mcp_planner_text(objective))),
        _MAX_STEPS,
    )
    from mcp_platform.plugin_planning import parse_plugin_plan_decision

    decision = parse_plugin_plan_decision(
        {
            "decision": "call_tool",
            "steps": [
                {
                    "step_id": "step-1",
                    "candidate_id": binding.candidate_id,
                    "arguments": dict(arguments),
                    "consumes_step_ids": [],
                }
            ],
        },
        candidates=(binding,),
        repair_used=False,
        context=context,
        prior_steps=(),
    )
    if decision.plan is None:
        raise ValueError("plugin_plan_invalid")
    return decision.plan


def _decode_model_body(body: object) -> object:
    if isinstance(body, Mapping):
        for key in ("json", "parsed"):
            if key in body and isinstance(body[key], (Mapping, str)):
                return (
                    json.loads(body[key]) if type(body[key]) is str else body[key]
                )
        if isinstance(body.get("decision"), Mapping):
            return body["decision"]
        text = body.get("text")
        if type(text) is str:
            return json.loads(text)
        return body
    if type(body) is str:
        return json.loads(body)
    raise ValueError("plugin_plan_model_output_invalid")


def _normalize_bounded_model_proposal(body: object) -> object:
    """Translate a bounded proposal wrapper into the planner decision shape.

    Some OpenAI-compatible seats, including the Mistral endpoint, honor JSON
    mode but return the requested operation under ``proposal`` rather than
    reproducing the host's internal ``decision/steps`` envelope. Only the two
    closed proposal forms emitted by the plugin prompt are accepted here; the
    normal candidate and argument validators still own the security boundary.
    """
    if not isinstance(body, Mapping) or "decision" in body:
        return body
    proposal = body.get("proposal")
    if not isinstance(proposal, Mapping):
        return body

    bounded = proposal.get("bounded_mcp") or proposal.get("bounded_mcp_proposal")
    if isinstance(bounded, Mapping):
        calls = bounded.get("calls")
        if isinstance(calls, list) and len(calls) == 1 and isinstance(calls[0], Mapping):
            call = calls[0]
            candidate_id = call.get("candidate_id")
            arguments = call.get("input")
            if type(candidate_id) is str and isinstance(arguments, Mapping):
                return {
                    "decision": "call_tool",
                    "steps": [
                        {
                            "step_id": "step-1",
                            "candidate_id": candidate_id,
                            "arguments": dict(arguments),
                            "consumes_step_ids": [],
                        }
                    ],
                }

        candidate_id = bounded.get("candidate_id")
        arguments = bounded.get("input")
        if type(candidate_id) is str and isinstance(arguments, Mapping):
            return {
                "decision": "call_tool",
                "steps": [
                    {
                        "step_id": "step-1",
                        "candidate_id": candidate_id,
                        "arguments": dict(arguments),
                        "consumes_step_ids": [],
                    }
                ],
            }

    candidate_id = proposal.get("candidate_id")
    arguments = proposal.get("input")
    if type(candidate_id) is str and isinstance(arguments, Mapping):
        return {
            "decision": "call_tool",
            "steps": [
                {
                    "step_id": "step-1",
                    "candidate_id": candidate_id,
                    "arguments": dict(arguments),
                    "consumes_step_ids": [],
                }
            ],
        }
    return body


def _model_plan(
    objective: McpPlannerTextReceipt,
    *,
    episode_id: str,
    candidates: tuple[PluginToolBinding, ...],
    runtime_client: object,
    request_context: Mapping[str, object],
) -> tuple[PluginPlanDecisionKind, PluginOperationPlan | None, str | None]:
    generate = getattr(runtime_client, "generate_completion", None)
    if not callable(generate):
        return PluginPlanDecisionKind.FINISH, None, "plugin_plan_generation_unavailable"
    objective_text = validate_mcp_planner_text(objective)
    context = PluginPlannerContext(
        episode_id,
        sha256_hex(canonical_bytes(objective_text)),
        _MAX_STEPS,
    )
    from organ.promptorgan import PromptContext, phrase

    request = phrase(
        PromptContext(
            kind="plugin",
            question=objective_text,
            data={
                "objective": objective,
                "candidates": candidates,
                "prior_steps": (),
                "context": context,
                "metadata": _metadata(candidates),
            },
        )
    )
    prompt = request.user
    closed_schema = request.response_format
    raw: object
    repair_used = False
    for attempt in range(2):
        try:
            body = generate(
                prompt,
                response_format=closed_schema,
                system=request.system,
                **request_context,
            )
            raw = _normalize_bounded_model_proposal(_decode_model_body(body))
            decision = parse_plugin_plan_decision(
                raw,
                candidates=candidates,
                repair_used=repair_used,
                context=context,
                prior_steps=(),
            )
            return decision.kind, decision.plan, decision.question or decision.summary
        except ValueError:
            if attempt:
                return PluginPlanDecisionKind.FINISH, None, "plugin_plan_repair_exhausted"
            repair_used = True
            prompt = f"{prompt}\nRepair the previous decision using only this schema."
    return PluginPlanDecisionKind.FINISH, None, "plugin_plan_repair_exhausted"


def _context_value(runtime_client: object, name: str, default: object = None) -> object:
    value = getattr(runtime_client, name, default)
    return value() if callable(value) and name.startswith("resolve_") else value


def _authority_context(
    runtime_client: object,
    plan: PluginOperationPlan,
    binding: PluginToolBinding,
    step: PluginOperationStep,
    active_contributor: Mapping[str, object] | None = None,
) -> dict[str, object]:
    resolver = getattr(runtime_client, "resolve_plugin_authority", None)
    if callable(resolver):
        value = resolver(plan, binding, step)
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, tuple) and len(value) == 4:
            resolved_contributor = value[3] or active_contributor
            if isinstance(resolved_contributor, Mapping):
                resolved_contributor = {
                    **dict(resolved_contributor),
                    "source": "operator",
                    "type": "command",
                    "episode_id": plan.episode_id,
                    "objective_sha256": plan.objective_sha256,
                    "entailed_plan_digest": plugin_plan_digest(plan),
                }
            return {
                "connection_authority": value[0],
                "connection": value[1],
                "authority_service": value[2],
                "active_contributor": resolved_contributor,
            }
        raise ValueError("plugin_authority_context_invalid")
    return {
        "connection_authority": getattr(runtime_client, "connection_authority", None),
        "connection": getattr(runtime_client, "connection_snapshot", None),
        "authority_service": getattr(runtime_client, "authority_service", None),
        "active_contributor": active_contributor
        or getattr(runtime_client, "active_contributor", None),
        "standing_grant": getattr(runtime_client, "standing_grant", None),
    }


def _tool_authority(binding: PluginToolBinding) -> McpToolAuthority:
    required = binding.input_schema.get("required", [])
    return McpToolAuthority(
        tool_name=binding.tool_name,
        input_schema_sha256=binding.input_schema_sha256,
        capability_tags=(f"tool:{binding.capability}",),
        risk_class=binding.risk_class,
        effect=binding.effect,
        requires_revision=binding.requires_revision,
        provider_object_id_pointer=None,
        revision_map_required=isinstance(required, list)
        and "expected_provider_revisions" in required,
    )


def _proposal(
    *,
    plan: PluginOperationPlan,
    step: PluginOperationStep,
    binding: PluginToolBinding,
    connection: McpConnectionSnapshot,
    authority: object,
    tool: McpToolAuthority,
    action_id: str,
) -> McpOperationProposal:
    unsigned = {
        "request_id": action_id,
        "episode_id": plan.episode_id,
        "server_id": connection.server_id,
        "server_version": connection.server_version,
        "artifact_sha256": connection.artifact_sha256,
        "provider_id": connection.provider_id,
        "account_ref": connection.account_ref,
        "connection_id": connection.connection_id,
        "connection_revision": connection.connection_revision,
        "tool_name": tool.tool_name,
        "tool_schema_sha256": tool.input_schema_sha256,
        "arguments": plain_arguments(step.arguments),
        "source_constraints": dict(connection.source_constraints),
        "destination_constraints": dict(connection.destination_constraints),
        "expected_provider_revisions": {},
        "authority_kind": authority.authority_kind,
        "authority_id": authority.authority_id,
        "authority_revision": authority.authority_revision,
        "plan_digest": plugin_plan_digest(plan),
        "candidate_id": binding.candidate_id,
        "plan_step_id": step.step_id,
        "connection_authority_revision": authority.connection_authority_revision,
        "idempotency_key": action_id,
        "evidence_memory_ids": [],
    }
    return McpOperationProposal.from_dict(
        {**unsigned, "proposal_digest": sha256_hex(canonical_bytes(unsigned))}
    )


def _screen_result(result: Mapping[str, Any], binding: PluginToolBinding) -> dict[str, Any]:
    data = result.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("plugin_result_invalid")
    projection, guardian = _guardian_result(data, source_name="plugin-result")
    if guardian["quarantined"] or _contains_quarantine_marker(projection):
        raise ValueError("plugin_result_quarantined")
    payload = {
        "plugin_id": binding.plugin_id,
        "app_id": binding.app_id,
        "capability": binding.capability,
        "status": "completed",
        "data": projection,
    }
    if len(canonical_bytes(payload)) > binding.max_result_bytes:
        raise ValueError("plugin_result_limit_exceeded")
    return payload


def _contains_quarantine_marker(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_quarantine_marker(child) for child in value.values())
    if type(value) in {list, tuple}:
        return any(_contains_quarantine_marker(child) for child in value)
    return value == QUARANTINE_MARKER


def _resolve_step_arguments(
    value: object,
    outputs: Mapping[str, Mapping[str, object]],
) -> object:
    if isinstance(value, Mapping) and set(value) == {"$from_step"}:
        reference = value["$from_step"]
        if (
            not isinstance(reference, Mapping)
            or set(reference) != {"step_id", "path"}
            or type(reference["step_id"]) is not str
            or not isinstance(reference["path"], (list, tuple))
        ):
            raise ValueError("plugin_step_reference_invalid")
        current: object = outputs.get(reference["step_id"])
        if current is None:
            raise ValueError("plugin_step_reference_missing")
        for part in reference["path"]:
            if type(part) is not str or not isinstance(current, Mapping) or part not in current:
                raise ValueError("plugin_step_reference_invalid")
            current = current[part]
        return current
    if isinstance(value, Mapping):
        return {
            key: _resolve_step_arguments(child, outputs) for key, child in value.items()
        }
    if type(value) in {list, tuple}:
        return [_resolve_step_arguments(child, outputs) for child in value]
    return value


def _permission(
    runtime_client: object,
    plan: PluginOperationPlan,
    binding: PluginToolBinding,
    *,
    chat_cycle_id: str | None = None,
    job_id: str | None = None,
) -> PluginExecutionOutcome:
    store = getattr(runtime_client, "permission_store", None)
    issue = getattr(store, "issue", None)
    if not callable(issue):
        return _failed("plugin_permission_store_unavailable", plan=plan)
    try:
        request = issue(
            plan,
            account_key_hash=binding.account_key_hash,
            prior_episode_id=plan.episode_id,
            # Keep the originating chat cycle in the permission lineage so an
            # Allow/Cancel continuation can settle the same HUD bubble.
            task_id=(
                str(chat_cycle_id or "").strip()
                or str(job_id or "").strip()
                or getattr(runtime_client, "task_id", f"plugin:{plan.plan_id}")
            ),
        )
    except Exception as exc:
        return _failed(str(exc) or "plugin_permission_issue_failed", plan=plan)
    if not isinstance(request, PluginPermissionRequest):
        return _failed("plugin_permission_request_invalid", plan=plan)
    return PluginExecutionOutcome("permission_required", plan, (), request, None)


def _authority_for_step(
    runtime_client: object,
    plan: PluginOperationPlan,
    binding: PluginToolBinding,
    step: PluginOperationStep,
    context: dict[str, object],
) -> tuple[object | None, dict[str, object]]:
    service = context.get("authority_service")
    durable_connection = context.get("connection_authority")
    snapshot = context.get("connection")
    contributor = context.get("active_contributor")
    if (
        service is not None
        and isinstance(durable_connection, PluginConnectionAuthority)
        and isinstance(snapshot, McpConnectionSnapshot)
        and callable(getattr(service, "authorize_direct_turn", None))
        and isinstance(contributor, Mapping)
        and durable_connection.execution_policy is PluginExecutionPolicy.DIRECT_TURN
    ):
        authority = service.authorize_direct_turn(
            plan,
            connection=durable_connection,
            connection_snapshot=snapshot,
            candidate_id=step.candidate_id,
            step_id=step.step_id,
            active_contributor=contributor,
        )
        return authority, context
    standing = context.get("standing_grant")
    if (
        service is not None
        and isinstance(durable_connection, PluginConnectionAuthority)
        and isinstance(snapshot, McpConnectionSnapshot)
        and callable(getattr(service, "authorize_standing_grant", None))
        and durable_connection.execution_policy is PluginExecutionPolicy.CONFIRM_EACH
        and isinstance(standing, Mapping)
    ):
        required = (
            "authority_id",
            "job_id",
            "job_revision",
            "execution_revision",
        )
        if all(type(standing.get(key)) is str for key in required):
            authority = service.authorize_standing_grant(
                plan,
                authority_id=standing["authority_id"],
                job_id=standing["job_id"],
                job_revision=standing["job_revision"],
                execution_revision=standing["execution_revision"],
                candidate_id=step.candidate_id,
                step_id=step.step_id,
                connection=durable_connection,
            )
            return authority, context
    return None, context


def plan_and_execute_plugin_request(
    objective: str,
    *,
    episode_id: str,
    runtime_client: object,
    grant: PluginPlanGrant | None = None,
    explicit_operation: Mapping[str, object] | None = None,
    plan_override: PluginOperationPlan | None = None,
    pipeline_id: int | None = None,
    work_kind: str | None = None,
    cycle_id: str | None = None,
    job_id: str | None = None,
    active_contributor: Mapping[str, object] | None = None,
) -> PluginExecutionOutcome:
    """Plan and execute one generic plugin objective inside AUTO only."""
    if type(objective) is not str or type(episode_id) is not str:
        return _failed("plugin_plan_input_invalid")
    execution_scope: object | None = None

    def release_execution_scope() -> None:
        nonlocal execution_scope
        scope = execution_scope
        execution_scope = None
        if scope is not None:
            scope.__exit__(None, None, None)

    request_context = {
        key: value
        for key, value in {
            "pipeline_id": pipeline_id,
            "work_kind": work_kind,
            "cycle_id": cycle_id,
            "job_id": job_id,
        }.items()
        if value not in (None, "")
    }
    try:
        selector_only = explicit_operation is not None and set(explicit_operation) == {
            "plugin_id"
        }
        if selector_only and (
            type(explicit_operation.get("plugin_id")) is not str
            or not explicit_operation.get("plugin_id")
        ):
            raise ValueError("plugin_selector_invalid")
        full_explicit = explicit_operation is not None and not selector_only
        bindings = _bindings_for(runtime_client)
        override_execution = plan_override is not None
        if plan_override is not None:
            if type(plan_override) is not PluginOperationPlan:
                return _failed("plugin_plan_override_invalid")
            selected_ids = frozenset(step.candidate_id for step in plan_override.steps)
            selected_bindings = tuple(
                item for item in bindings if item.candidate_id in selected_ids
            )
            candidates = selected_bindings
            plan = plan_override
        else:
            objective_receipt = _objective_receipt(objective)
            candidates = (
                tuple(
                    item
                    for item in bindings
                    if explicit_operation is not None
                    and item.plugin_id == explicit_operation.get("plugin_id")
                    and (
                        selector_only
                        or (
                            item.app_id == explicit_operation.get("app_id")
                            and item.capability == explicit_operation.get("capability")
                        )
                    )
                )
                if explicit_operation is not None
                else retrieve_plugin_candidates(objective, bindings, limit=12)
            )
            plan = None
        if not candidates:
            return _ask("No eligible plugin operation was found.")
        if plan_override is None:
            plan = (
                _explicit_plan(
                    explicit_operation,
                    objective=objective_receipt,
                    episode_id=episode_id,
                    bindings=bindings,
                )
                if full_explicit
                else _deterministic_plan(
                    objective_receipt,
                    episode_id=episode_id,
                    candidates=candidates,
                )
            )
        if plan is None and not full_explicit:
            kind, plan, message = _model_plan(
                objective_receipt,
                episode_id=episode_id,
                candidates=candidates,
                runtime_client=runtime_client,
                request_context=request_context,
            )
            if kind is PluginPlanDecisionKind.ASK_USER:
                return _ask(message or "Please clarify the plugin operation.")
            if kind is not PluginPlanDecisionKind.CALL_TOOL or plan is None:
                return _failed(message or "plugin_plan_generation_failed")
        if grant is not None and (
            grant.episode_id != plan.episode_id
            or grant.plan_digest != plugin_plan_digest(plan)
            or grant.authority_digest != plan.authority_digest
        ):
            return _failed("plugin_permission_binding_invalid", plan=plan)
        initial_bindings = _bindings_for(runtime_client)
        authority_candidate_ids = (
            frozenset(step.candidate_id for step in plan.steps)
            if full_explicit or override_execution
            else frozenset(item.candidate_id for item in candidates)
        )
        initial_catalog = tuple(
            item for item in initial_bindings if item.candidate_id in authority_candidate_ids
        )
        if not override_execution and _catalog_digest(initial_catalog) != plan.authority_digest:
            return _failed("plugin_catalog_changed", plan=plan)
        plan_bindings = tuple(
            next(
                (item for item in initial_bindings if item.candidate_id == step.candidate_id),
                None,
            )
            for step in plan.steps
        )
        if any(item is None for item in plan_bindings):
            return _failed("plugin_candidate_changed", plan=plan)
        typed_bindings = tuple(item for item in plan_bindings if item is not None)
        try:
            validate_plugin_plan_provenance(plan, typed_bindings)
        except ValueError:
            return _failed("plugin_plan_provenance_invalid", plan=plan)
        if len({item.account_key_hash for item in typed_bindings}) > 1:
            return _failed("plugin_permission_mixed_accounts", plan=plan)
        # Plugin authority is selected in Settings and revalidated from the
        # signed connection/capability catalog. Ordinary chat permission modes
        # govern the agent tool rail, not plugin scopes.
        scope_factory = getattr(
            runtime_client,
            "plugin_execution_mutation_scope",
            None,
        )
        candidate_scope = (
            nullcontext() if not callable(scope_factory) else scope_factory()
        )
        candidate_scope.__enter__()
        execution_scope = candidate_scope
        grant_authorities: tuple[object, ...] | None = None
        if grant is None:
            for item, candidate_step in zip(typed_bindings, plan.steps, strict=True):
                if item.risk_class is RiskClass.READ_ONLY:
                    continue
                context = _authority_context(
                    runtime_client,
                    plan,
                    item,
                    candidate_step,
                    active_contributor,
                )
                durable_connection = context.get("connection_authority")
                service = context.get("authority_service")
                contributor = context.get("active_contributor")
                standing = context.get("standing_grant")
                direct_ready = (
                    isinstance(durable_connection, PluginConnectionAuthority)
                    and durable_connection.execution_policy
                    is PluginExecutionPolicy.DIRECT_TURN
                    and callable(getattr(service, "authorize_direct_turn", None))
                    and isinstance(contributor, Mapping)
                )
                standing_ready = (
                    isinstance(durable_connection, PluginConnectionAuthority)
                    and durable_connection.execution_policy
                    is PluginExecutionPolicy.CONFIRM_EACH
                    and callable(getattr(service, "authorize_standing_grant", None))
                    and isinstance(standing, Mapping)
                )
                if not (direct_ready or standing_ready):
                    permission_binding = next(
                        (
                            candidate
                            for candidate in typed_bindings
                            if candidate.risk_class is not RiskClass.READ_ONLY
                        ),
                        item,
                    )
                    outcome = _permission(
                        runtime_client,
                        plan,
                        permission_binding,
                        chat_cycle_id=cycle_id,
                        job_id=job_id,
                    )
                    release_execution_scope()
                    return outcome
        results: list[PluginStepResult] = []
        projections: list[dict[str, Any]] = []
        step_outputs: dict[str, Mapping[str, object]] = {}
        for index, step in enumerate(plan.steps):
            current = _bindings_for(runtime_client)
            current_catalog = tuple(
                item for item in current if item.candidate_id in authority_candidate_ids
            )
            if not override_execution and _catalog_digest(current_catalog) != plan.authority_digest:
                outcome = _failed("plugin_catalog_changed", plan=plan, results=results)
                release_execution_scope()
                return outcome
            binding = next(
                (item for item in current if item.candidate_id == step.candidate_id),
                None,
            )
            if binding is None:
                outcome = _failed("plugin_candidate_changed", plan=plan, results=results)
                release_execution_scope()
                return outcome
            resolved_step = PluginOperationStep(
                step_id=step.step_id,
                candidate_id=step.candidate_id,
                arguments=_resolve_step_arguments(step.arguments, step_outputs),
                consumes_step_ids=step.consumes_step_ids,
            )
            context = _authority_context(
                runtime_client,
                plan,
                binding,
                resolved_step,
                active_contributor,
            )
            if grant is not None and grant_authorities is None:
                store = getattr(runtime_client, "permission_store", None)
                consume = getattr(store, "consume", None)
                if not callable(consume):
                    raise ValueError("plugin_permission_store_unavailable")
                consumed = consume(
                    grant.grant_id,
                    plan=plan,
                    authority_digest=plan.authority_digest,
                    account_key_hash=binding.account_key_hash,
                )
                if not isinstance(consumed, tuple) or len(consumed) != len(plan.steps):
                    raise ValueError("plugin_permission_authority_invalid")
                grant_authorities = consumed
            authority = (
                grant_authorities[index]
                if grant_authorities is not None
                else None
            )
            if authority is None and binding.risk_class is not RiskClass.READ_ONLY:
                authority = _authority_for_step(
                    runtime_client, plan, binding, resolved_step, context
                )[0]
            if authority is None and binding.risk_class is not RiskClass.READ_ONLY:
                if results:
                    outcome = _failed(
                        "plugin_authority_missing_after_dispatch",
                        plan=plan,
                        results=results,
                    )
                    release_execution_scope()
                    return outcome
                outcome = _permission(
                    runtime_client,
                    plan,
                    binding,
                    chat_cycle_id=cycle_id,
                    job_id=job_id,
                )
                release_execution_scope()
                return outcome
            connection = context.get("connection")
            tool = _tool_authority(binding)
            action_id = f"plugin-step-{plan.plan_id}-{step.step_id}"
            proposal = (
                _proposal(
                    plan=plan,
                    step=resolved_step,
                    binding=binding,
                    connection=connection,
                    authority=authority,
                    tool=tool,
                    action_id=action_id,
                )
                if authority is not None and isinstance(connection, McpConnectionSnapshot)
                else None
            )
            step_capture = _PluginStepCapture.open(episode_id, binding, action_id)
            try:
                retry_key = None
                while True:
                    try:
                        with plugin_retry_context(retry_key):
                            raw = run_plugin_plan_step(
                                binding,
                                plan,
                                resolved_step,
                                runtime_client=runtime_client,
                                connection=connection,
                                authority=authority,
                                tool=tool if authority is not None else None,
                                proposal=proposal,
                                authority_service=context.get("authority_service"),
                                action_id=action_id,
                            )
                        break
                    except PluginRetryAfterFence as deferred:
                        # The plan owns the outer action fence. Runtime can only
                        # report a retry token while that fence is held; release
                        # it so the lifecycle drains and resolves the update.
                        release_execution_scope()
                        if deferred.outcome is None:
                            raise
                        if deferred.outcome.status != "applied":
                            raise deferred.final_error()
                        runtime = getattr(runtime_client, "plugin_runtime", None)
                        prepare_retry = getattr(
                            getattr(runtime, "_backend", None), "prepare_plugin_retry", None
                        )
                        if callable(prepare_retry):
                            prepare_retry(deferred.request_key, deferred.recovery_status)
                        retry_key = deferred.request_key
                        candidate_bindings = _bindings_for(runtime_client)
                        rebound = next(
                            (
                                item
                                for item in candidate_bindings
                                if item.plugin_id == binding.plugin_id
                                and item.app_id == binding.app_id
                                and item.capability == binding.capability
                            ),
                            None,
                        )
                        if rebound is None:
                            raise PluginRuntimeError("plugin_authority_invalid")
                        binding = rebound
                        resolved_step = PluginOperationStep(
                            step_id=resolved_step.step_id,
                            candidate_id=rebound.candidate_id,
                            arguments=resolved_step.arguments,
                            consumes_step_ids=resolved_step.consumes_step_ids,
                        )
                        candidate_scope = (
                            nullcontext()
                            if not callable(scope_factory)
                            else scope_factory()
                        )
                        candidate_scope.__enter__()
                        execution_scope = candidate_scope
                        continue
                screened = _screen_result(raw, binding)
            except BaseException as exc:
                step_capture.failed(exc)
                raise
            step_capture.succeeded(screened)
            projections.append(screened)
            step_outputs[step.step_id] = screened["data"]
            summary = screen_mcp_planner_text(
                f"{binding.title} completed",
                source="plugin-result",
            )
            results.append(
                PluginStepResult(
                    step_id=step.step_id,
                    candidate_id=step.candidate_id,
                    status="completed",
                    summary=summary,
                )
            )
        plugin_result = projections[-1] if projections else None
        outcome = PluginExecutionOutcome(
            "completed",
            plan,
            tuple(results),
            None,
            None,
            plugin_result,
        )
        release_execution_scope()
        return outcome
    except ValueError as exc:
        release_execution_scope()
        reason = str(exc) or "plugin_plan_failed"
        if reason in {"plugin_plan_ambiguous", "plugin_candidate_ambiguous"}:
            return _ask("Which plugin or connected account should I use?")
        if reason in _SETTINGS_FAILURES:
            return PluginExecutionOutcome(
                "ask_user",
                locals().get("plan"),
                (),
                None,
                "plugin_settings_required",
            )
        if not reason.startswith("plugin_"):
            return _failed(_screen_failure(exc))
        return _failed(reason)
    except Exception as exc:
        release_execution_scope()
        return _failed(_screen_failure(exc))


class _PluginStepCapture:
    """WO-05 carry-over: one plugin step's attempt, its result, and — for a send — its delivery.

    Only what can be proved is claimed. A read-only step that failed moved nothing; a write or
    send that failed may have landed, so its effect is unknown and a send is ``uncertain``, never
    marked safe to retry. A send is ``confirmed`` only with the provider's own message id.
    Observes only: it never retries or re-sends.
    """

    _RECEIPT_KEYS = ("messageId", "message_id", "id", "ts")

    def __init__(self, recorder: Any, tool: str, target: str, *, read_only: bool,
                 send: bool) -> None:
        self.recorder, self.tool, self.target = recorder, tool, target
        self.read_only, self.send, self.token = read_only, send, ""

    @classmethod
    def open(cls, episode_id: str, binding: Any, action_id: str) -> "_PluginStepCapture":
        """Record against the episode RUNNING the step. An approved continuation runs a
        plan made in an earlier episode; capturing into that episode is refused by the
        owner binding (EpisodeOwnershipError) and the approved action never runs."""
        from memory.auto_evidence import attempt_recorder, capture_delivery_observation

        recorder = attempt_recorder(str(episode_id or ""), lane="plugin")
        capture = cls(recorder, f"{binding.plugin_id}:{binding.capability}", action_id,
                      read_only=binding.risk_class is RiskClass.READ_ONLY,
                      send=binding.risk_class is RiskClass.SEND_PUBLISH)
        if recorder is not None:
            capture.token = recorder.attempt(capture.tool)
            if capture.send:
                capture_delivery_observation(recorder, target=action_id, state="attempted")
        return capture

    def succeeded(self, screened: Any) -> None:
        if self.recorder is None:
            return
        from memory.auto_evidence import capture_delivery_observation

        self.recorder.result(self.token, self.tool, ok=True)
        if not self.send:
            return
        data = screened.get("data") if isinstance(screened, dict) else None
        receipt = ""
        if isinstance(data, dict):
            receipt = next((str(data[key]) for key in self._RECEIPT_KEYS
                            if isinstance(data.get(key), (str, int)) and str(data[key]).strip()), "")
        if receipt:
            capture_delivery_observation(self.recorder, target=self.target, state="confirmed",
                                         provider_receipt=receipt)
        else:
            capture_delivery_observation(
                self.recorder, target=self.target, state="uncertain",
                reason="the provider reported success but returned no message id")

    def failed(self, exc: BaseException) -> None:
        if self.recorder is None:
            return
        from memory.auto_evidence import capture_delivery_observation

        reason = f"{type(exc).__name__}: {exc}"[:2000]
        self.recorder.result(self.token, self.tool, ok=False, error_kind="handler_raised",
                             error_detail=reason, effect="no_effect" if self.read_only else "")
        if self.send:
            capture_delivery_observation(self.recorder, target=self.target, state="uncertain",
                                         reason=reason)


def plugin_plan_grant_from_contributor(
    runtime_client: object,
    contributor: object,
) -> PluginPlanGrant:
    """Revalidate and approve one queued Allow decision for the continuation episode."""
    from pressure_loop.plugin_continuations import (
        plugin_permission_continuation_from_contributor,
        plugin_permission_contributor_from_metadata,
    )

    if isinstance(contributor, Mapping):
        contributor = plugin_permission_contributor_from_metadata(contributor)
    continuation = plugin_permission_continuation_from_contributor(contributor)
    store = getattr(runtime_client, "permission_store", None)
    if continuation.decision.decision != "allow":
        cancel = getattr(store, "cancel", None)
        if not callable(cancel):
            raise ValueError("plugin_permission_store_unavailable")
        cancel(
            continuation.decision.request_id,
            expected_revision=continuation.decision.expected_revision,
        )
        raise ValueError("plugin_permission_cancelled")
    approve = getattr(store, "approve", None)
    if not callable(approve):
        raise ValueError("plugin_permission_store_unavailable")
    grant = approve(
        continuation.decision.request_id,
        expected_revision=continuation.decision.expected_revision,
    )
    if not isinstance(grant, PluginPlanGrant):
        raise ValueError("plugin_plan_grant_invalid")
    if (
        grant.episode_id != continuation.lineage.prior_episode_id
        or grant.plan_digest != continuation.lineage.plan_digest
    ):
        raise ValueError("plugin_permission_binding_invalid")
    return grant


__all__ = [
    "PluginExecutionOutcome",
    "plan_and_execute_plugin_request",
    "plugin_plan_grant_from_contributor",
]
