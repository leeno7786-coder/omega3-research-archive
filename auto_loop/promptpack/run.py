"""AUTO promptpack phase — deterministic PromptPack construction."""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

from auto_loop._auto_state import MemoryEnvelope, PromptPack
from auto_loop.motor_adapter import effective_motor_action, motor_program_from_pack
from auto_loop.promptpack.question_shape import is_question_shaped as _is_question_shaped
from guardian.external_ingress import harden


def _dominant_text(pack: Mapping[str, Any], pressure: Mapping[str, Any]) -> str:
    parts: list[str] = []
    ag = pack.get("source_action_goal") or {}
    target = ag.get("target")
    if isinstance(target, dict):
        parts.append(str(target.get("question") or target.get("message") or ""))
    elif target is not None:
        parts.append(str(target))
    for row in (pressure or {}).get("contributors") or []:
        if isinstance(row, dict):
            meta = dict(row.get("metadata") or row.get("detail") or {})
            parts.append(str(meta.get("message") or meta.get("text") or ""))
    return " ".join(p for p in parts if p).strip()


def _chat_intent_from_pressure(pressure: Mapping[str, Any]) -> Optional[str]:
    """Read the single deterministic chat-intent label threaded at ingress."""
    from auto_loop.promptpack.intent_classifier import ALLOWED_INTENTS

    for row in (pressure or {}).get("contributors") or []:
        if isinstance(row, dict):
            meta = dict(row.get("metadata") or row.get("detail") or {})
            ci = str(meta.get("chat_intent") or "").strip()
            if ci in ALLOWED_INTENTS:
                return ci
    return None


def _query_thread_from_pressure(pressure: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    """The system-query thread riding the operator contributor (operator 2026-07-10):
    when the operator ANSWERS a system-initiated question (chat box or panel), the
    contributor carries the question so the reply prompt can see BOTH turns —
    without it the model answers with amnesia about the question it asked itself."""
    for row in (pressure or {}).get("contributors") or []:
        if not isinstance(row, dict):
            continue
        meta = dict(row.get("metadata") or row.get("detail") or {})
        question = str(meta.get("system_query_text") or "").strip()
        if question:
            answer = str(meta.get("message") or meta.get("text") or "").strip()
            return {"question": question, "answer": answer}
    return None


def _deterministic_response_from_pressure(
    pressure: Mapping[str, Any],
) -> Optional[Dict[str, str]]:
    """Return the one deterministic response attached to this episode's ingress."""
    found: list[Dict[str, str]] = []
    for row in (pressure or {}).get("contributors") or []:
        if not isinstance(row, dict):
            continue
        meta = dict(row.get("metadata") or row.get("detail") or {})
        response_text = str(meta.get("deterministic_response_text") or "").strip()
        response_kind = str(meta.get("deterministic_response_kind") or "").strip()
        if response_text and response_kind:
            found.append({"text": response_text, "kind": response_kind})
    # Scheduling admits at most one operator turn per episode.  Fail closed if
    # malformed pressure ever presents competing deterministic outputs.
    if len(found) != 1:
        return None
    return found[0]


def build_prompt_pack(
    execution_pack: Mapping[str, Any],
    memory_envelope: MemoryEnvelope,
    *,
    pressure: Optional[Mapping[str, Any]] = None,
    temporal: Optional[Mapping[str, Any]] = None,
) -> PromptPack:
    """PHASE 2 — deterministic PromptPack construction.

    ``temporal`` is the episode's temporal envelope (core.episode_temporal). Dedicated execution
    actions still receive it on their system directive. Dynamic RESPONSE actions carry the same
    deterministic line in ``context_blocks`` for the engineer/answer path instead of constructing
    a classifier-selected response template.
    """
    from pressure_loop.policy import (
        auto_proactive_ack_keywords,
        auto_response_ack_keywords,
        auto_system_prompt,
    )

    pack = dict(execution_pack)
    action = effective_motor_action(pack)
    motor_program = motor_program_from_pack(pack)
    context_blocks: Dict[str, Any] = {
        "remembered_knowledge_tool_results": () if pack.get("memory_required") else None,
        "memory_envelope": {
            "memory_required": bool(memory_envelope.memory_required),
            "memory_status": memory_envelope.memory_status,
            "memory_reason": memory_envelope.memory_reason,
            "memory_scope": list(memory_envelope.memory_scope),
            "item_count": len(memory_envelope.items),
            "error": memory_envelope.error,
        },
        "tool_block": None,
        "constraints_block": dict(pack.get("execution_constraints") or {}),
    }
    text = _dominant_text(pack, pressure or {})
    visual_turns = []
    for row in (pressure or {}).get("contributors") or []:
        if not isinstance(row, dict):
            continue
        metadata = {**dict(row.get("metadata") or {}), **dict(row.get("detail") or {})}
        if metadata.get("visual_intake"):
            if row.get("id") not in {"operator_query", "operator_command", "operator_response"}:
                raise ValueError("visual intake requires an operator contributor")
            visual_turns.append(metadata)
    if len(visual_turns) > 1:
        raise ValueError("multiple visual operator turns in one episode")
    if visual_turns:
        context_blocks["visual_intake"] = visual_turns[0]["visual_intake"]
        context_blocks["visual_source_citation"] = visual_turns[0].get("source_citation") or {}
    ag = pack.get("source_action_goal") or {}
    target = ag.get("target")
    target_str = (
        str(target.get("question") or target.get("message") or "")
        if isinstance(target, dict)
        else str(target or "")
    )
    execution_intent = str(pack.get("execution_intent") or "")
    inputs = dict(pack.get("execution_inputs") or {})
    if motor_program is not None:
        context_blocks["motor_program"] = json.dumps(
            {
                "objective": motor_program.outcome_contract.objective,
                "acceptance_criteria": list(motor_program.outcome_contract.acceptance_criteria),
                "method": motor_program.pathway.method,
                "operations": list(motor_program.execution_plan.operations),
                "constraints": list(motor_program.outcome_contract.constraints),
                "return_to_mind_conditions": list(
                    motor_program.execution_plan.return_to_mind_conditions
                ),
                "evidence_required": motor_program.evidence_contract.to_dict(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    prompt_type = "IDLE_SYSTEM"
    response_mode: Optional[str] = "reasoning"
    tool_call_spec = None
    image_spec = None
    user_prompt = text or target_str or None
    system_prompt = auto_system_prompt()

    if action == "RESPONSE":
        _deterministic_response = _deterministic_response_from_pressure(pressure or {})
        if _deterministic_response:
            context_blocks["deterministic_response_text"] = _deterministic_response["text"]
            context_blocks["deterministic_response_kind"] = _deterministic_response["kind"]
        # Conversation thread (operator 2026-07-10): when this reply answers the
        # operator's ANSWER to a system-initiated question, render both turns as a
        # context block so the model replies within the thread it started instead
        # of treating the answer as a fresh topic (the observed amnesia).
        _thread = _query_thread_from_pressure(pressure or {})
        if _thread:
            context_blocks["conversation_thread"] = (
                "CONVERSATION THREAD — you (Omega) asked the operator a question "
                "and they just answered it:\n"
                f"  You asked: {_thread['question']}\n"
                f"  Operator answered: {harden(_thread['answer'])}\n"
                "Reply to their ANSWER in the context of YOUR question. Do not "
                "treat the answer as a new topic; acknowledge what it resolves "
                "and continue from there."
            )
        # Reply-SHAPE selection only — the model still decides pressure routing in-prompt
        # (the classifier has no side effects). The chat-intent classifier picks the shape
        # when it is trained + flag-on; if it is off/absent it returns None and the
        # deterministic heuristics below take over. Question shape is checked BEFORE the
        # memory_required->reasoning fallback so a recalled-memory question stays a question
        # (grounding still attaches via memory_required); only NON-question memory turns reason.
        _intent_to_pack = {
            # A bare go-ahead ("ok do it", "yes go ahead", "thanks") is
            # CONVERSATION, not a receipt (operator 2026-09-05). Canned
            # RESPONSE_ACK answered "Acknowledged — on it." and then nothing
            # happened; the chat brain now reads the go-ahead with the recent
            # turns in context and acts with its tools on demand. RESPONSE_ACK
            # survives for the deterministic "remember" intake below.
            "ack": ("RESPONSE_FREEFORM", "freeform"),
            "question": ("RESPONSE_QUESTION", "question"),
            "freeform": ("RESPONSE_FREEFORM", "freeform"),
            "reasoning": ("RESPONSE_REASONING", "reasoning"),
            "temporal": ("RESPONSE_TEMPORAL", "temporal"),
        }
        _classified = _chat_intent_from_pressure(pressure or {})
        from memory.temporal_range import looks_like_temporal_recall
        from pressure_loop.contributors import infer_operator_command_kind

        if (
            infer_operator_command_kind(text) == "remember"
            or infer_operator_command_kind(target_str) == "remember"
        ):
            # Deterministic RESPONSE_ACK — an explicit "remember this" is an
            # INGEST, not a question, and the same high-precision override
            # pattern RESPONSE_TEMPORAL uses applies: an imperative directive
            # beats a fuzzy or stale classifier label.
            #
            # Live 2026-07-31 (episode 3238abd7): nothing in this chain claimed
            # a store command — "remember" is not in _intent_to_pack, the text
            # is not question-shaped, memory_required was False and no ack
            # keyword matched — so it fell to the RESPONSE_REASONING else-branch
            # and the composer authored a refusal against empty recall. The
            # operator was told "Got it — I'll remember that" AND "Not on record
            # — I do not have any memory records regarding ..." in one reply.
            # Recall is meaningless here: the facts are new BY DEFINITION.
            #
            # ACK is also the only shape that folds back through Omega
            # (cognition_chain.py:1105), which is the ingestion-then-foldback
            # route this turn should have taken.
            prompt_type, response_mode = "RESPONSE_ACK", "ack"
        elif looks_like_temporal_recall(target_str) or looks_like_temporal_recall(text):
            # Deterministic RESPONSE_TEMPORAL — a date-anchored recall ("what did we build yesterday")
            # is HIGH-PRECISION (date phrase + past-work-recall shape), so it OVERRIDES a fuzzy or
            # stale classifier (the same deterministic-override pattern intent_classifier uses to force
            # `question` on question-shaped turns). The classifier's `temporal` label below still
            # catches temporal intent this deterministic detector misses (no explicit date phrase).
            prompt_type, response_mode = "RESPONSE_TEMPORAL", "temporal"
        elif _classified in _intent_to_pack:
            # Intent is the PRIMARY signal for the OTHER shapes: it shapes the reply AND (in
            # cognition_chain) gates the cascade — only ACK folds back through Omega;
            # reasoning/question/freeform are terminal at AUTO. The Memory Response
            # Law is honored INSIDE each shape's prompt, so a recalled-memory question
            # stays a question (still grounded) instead of being force-reshaped to
            # reasoning — which would also misroute an ack-with-memory away from its
            # fold-back. memory_required still attaches the remembered-knowledge block.
            prompt_type, response_mode = _intent_to_pack[_classified]
        elif _is_question_shaped(target_str) or _is_question_shaped(text):
            # A question keeps its QUESTION shape (a direct, grounded answer) even when it
            # requires recall — the remembered-knowledge block still attaches (memory_required).
            # EXCEPTION: a prior-conversation recall ("what did I tell you about X") reasons over
            # the recalled history rather than answering directly; the recall gate marks it with
            # the operator_history/conversation memory scope. Deterministic so it holds with the
            # chat-intent classifier OFF (Stage 0 floor) and agrees with the recall gate. (Before
            # Stage 0 a fresh question's memory_required was wrongly False, so it reached the
            # endswith('?') branch; now it is True, so question shape must precede the
            # memory_required->reasoning fallback for fresh questions.)
            _mem_scope = set(pack.get("memory_scope") or ())
            if pack.get("memory_required") and (_mem_scope & {"operator_history", "conversation"}):
                prompt_type = "RESPONSE_REASONING"
                response_mode = "reasoning"
            else:
                prompt_type = "RESPONSE_QUESTION"
                response_mode = "question"
        elif pack.get("memory_required"):
            prompt_type = "RESPONSE_REASONING"
            response_mode = "reasoning"
        elif any(k in execution_intent.lower() for k in auto_response_ack_keywords()):
            prompt_type = "RESPONSE_ACK"
            response_mode = "ack"
        else:
            prompt_type = "RESPONSE_REASONING"
            response_mode = "reasoning"

    elif action == "PROACTIVE_RESPONSE":
        present_result = inputs.get("presentation_kind") == "PRESENT_RESULT"
        if present_result:
            policy = dict(inputs.get("operator_policy") or {})
            context_blocks["proactive_policy"] = json.dumps(
                policy, ensure_ascii=False, sort_keys=True
            )
            user_prompt = "\n".join(
                [
                    str(policy.get("objective") or ""),
                    *(str(value) for value in policy.get("learning_steps") or ()),
                    *(str(value) for value in policy.get("requested_demonstrations") or ()),
                    str(policy.get("continuation_rule") or ""),
                ]
            ).strip()
        if pack.get("memory_required"):
            prompt_type = "PROACTIVE_REASONING"
            response_mode = "reasoning"
        elif any(
            k in (str((pressure or {}).get("reason") or "") + execution_intent).lower()
            for k in auto_proactive_ack_keywords()
        ):
            prompt_type = "PROACTIVE_ACK"
            response_mode = "ack"
        else:
            prompt_type = "PROACTIVE_REASONING"
            response_mode = "reasoning"

    elif action == "ASK_OPERATOR":
        # The system-generated query — its OWN pack type, never
        # PROACTIVE_REASONING (clean route separation, operator directive
        # 2026-06-12). The mind's seed question arrives as the dominant text.
        prompt_type = "ASK_OPERATOR_QUERY"
        response_mode = "question"

    elif action == "USE_TOOL":
        prompt_type = "TOOL_CALL_SYSTEM"
        response_mode = "summary"
        tool = pack.get("tool")
        tool_reason = pack.get("tool_reason")
        params = {
            key: value
            for key, value in inputs.items()
            if key not in {"motor_program", "motor_transport", "skill_execution_pack"}
        }
        if motor_program is not None and not str(params.get("query") or "").strip():
            params["query"] = motor_program.execution_plan.operations[0]
        context_blocks["tool_block"] = f"TOOL: {tool}\nREASON: {tool_reason}\nPARAMS: {params}"
        tool_call_spec = {"tool": tool, "params": params}
        from organ.promptorgan import PromptContext, phrase

        tool_request = phrase(
            PromptContext(
                kind="tool",
                question=text or target_str,
                data={
                    "agent_type": str(pack.get("execution_kind") or "tool_executor"),
                    "tool": tool,
                    "tool_reason": tool_reason,
                    "params": params,
                },
            )
        )
        system_prompt = tool_request.system
        context_blocks["tool_prompt"] = tool_request.user

    elif action == "USE_SKILL":
        prompt_type = "SKILL_BUILD_SYSTEM"
        response_mode = "summary"
        skill_pack = inputs.get("skill_execution_pack") or {}
        if isinstance(skill_pack, Mapping):
            context_blocks["skill_execution_pack"] = json.dumps(
                skill_pack,
                ensure_ascii=False,
                sort_keys=True,
            )
            user_prompt = (
                str(
                    skill_pack.get("objective") or inputs.get("objective") or user_prompt or ""
                ).strip()
                or user_prompt
            )

    elif action == "IMAGE_CREATION":
        prompt_type = "IMAGE_SYSTEM"
        response_mode = "summary"
        raw_spec = inputs.get("image_spec") or target_str or text
        if isinstance(raw_spec, dict):
            image_spec = raw_spec
        else:
            image_spec = {"prompt": str(raw_spec)}
        user_prompt = str(image_spec.get("prompt") or raw_spec)

    elif action == "DOCUMENT_CREATION":
        prompt_type = "DOCUMENT_SYSTEM"
        response_mode = "summary"
        raw_spec = inputs.get("document_spec") or target_str or text
        if isinstance(raw_spec, dict):
            user_prompt = str(raw_spec.get("text") or raw_spec.get("message") or "")
        else:
            user_prompt = str(raw_spec or "")

    elif action == "IDLE":
        prompt_type = "IDLE_SYSTEM"
        response_mode = None
        user_prompt = None

    dynamic_response = action in {"RESPONSE", "PROACTIVE_RESPONSE"}
    # RESPONSE PromptPacks retain their classifier-selected route and original
    # turn, but no longer carry an answer template.  The per-turn scaffold
    # composer owns answer instructions immediately before response inference.
    # ACK still uses the deterministic receipt branch in execute/run.py, so an
    # empty response system prompt never causes an acknowledgement inference.
    system_prompt = (
        ""
        if dynamic_response
        else auto_system_prompt(
            prompt_type,
            memory_enriched=bool(pack.get("memory_required")),
        )
    )
    if action == "PROACTIVE_RESPONSE" and inputs.get("presentation_kind") == "PRESENT_RESULT":
        context_blocks["response_execution_directive"] = (
            "Complete the requested dialogue application now. Answer every requested item, "
            "explain each choice, expose uncertainty and mistakes, and do not claim mastery. "
            "Omega evaluates mastery after this execution."
        )

    try:
        from guardian.terminal_feedback import emit_data, emit_guardian_event

        ep_id = str(pack.get("episode_id") or "")
        emit_data(
            "auto",
            "promptpack",
            episode_id=ep_id or None,
            summary=f"prompt_type={prompt_type}",
            prompt_type=prompt_type,
            response_mode=response_mode,
            action=action,
        )
        emit_guardian_event(
            "omega.auto",
            "promptpack",
            episode_id=ep_id,
            prompt_type=prompt_type,
            response_mode=response_mode,
            action=action,
        )
    except Exception:
        pass

    # First-class time (operator 2026-07-16): surface the episode's wall-clock to the mind so it
    # knows *when it is* — grounds temporal reasoning, "what's tomorrow", and reminder resolution.
    # Applied last so it survives every per-shape system_prompt reassignment above. Deterministic:
    # derived from the fixed episode envelope, not a fresh clock read.
    if temporal and not dynamic_response:
        from core.episode_temporal import prepend_time_line

        system_prompt = prepend_time_line(system_prompt, temporal)
    elif temporal:
        # Execution gives this same fixed episode timestamp to the dynamically
        # composed answer prompt.  Keep it as data on the transport pack for
        # auditability without reviving a static response-system template.
        from core.episode_temporal import temporal_prompt_line

        context_blocks["temporal_prompt_line"] = temporal_prompt_line(temporal)

    return PromptPack(
        prompt_type=prompt_type,
        system_prompt=system_prompt,
        developer_prompt=None,
        user_prompt=user_prompt,
        context_blocks=context_blocks,
        response_mode=response_mode,
        tool_call_spec=tool_call_spec,
        image_spec=image_spec,
    )


def run(
    execution_pack: Mapping[str, Any],
    memory_envelope: MemoryEnvelope,
    *,
    pressure: Optional[Mapping[str, Any]] = None,
    temporal: Optional[Mapping[str, Any]] = None,
) -> PromptPack:
    return build_prompt_pack(execution_pack, memory_envelope, pressure=pressure, temporal=temporal)
