"""Meta A tool phase — capability tool selection per intent."""

from __future__ import annotations

from typing import Dict, Mapping, Optional

from meta_loop._meta_a_state import MetaAState

_CAPABILITY_TOOL_MAP_DEFAULT: Dict[str, str] = {
    "external_information": "web_fetch",
    "ocr": "ocr_tool",
    "desktop": "desktop_tool",
    "file_read": "file_reader",
    "image": "image_tool",
    "document": "document_tool",
    "image_understanding": "vision_reasoning_tool",
    "video_understanding": "vision_reasoning_tool",
    "object_localization": "vision_reasoning_tool",
    "scene_analysis": "vision_reasoning_tool",
    "face_recognition": "face_tool",
    "age_gender": "face_tool",
    "head_pose": "face_tool",
}

_CAPABILITY_TOOL_MAP = _CAPABILITY_TOOL_MAP_DEFAULT


def _capability_tool_map() -> Dict[str, str]:
    from pressure_loop.policy import meta_capability_tool_map

    merged = dict(_CAPABILITY_TOOL_MAP_DEFAULT)
    merged.update(meta_capability_tool_map())
    return merged


def _learn_default_tool() -> str:
    from pressure_loop.policy import meta_learn_default_tool

    return meta_learn_default_tool()


# Meta A source routing (operator 2026-07-08): "meta can choose the best api to
# resolve it." A research/academic/quantitative topic is best served by arXiv
# (real papers); general topics fall back to the Wikipedia + DDG path. Applies to
# EVERY LEARN — awakening, operator-directed, onboarding, mind-chosen — because
# it keys on the resolved topic, not on how the topic was chosen.
_ARXIV_TOPIC_HINTS = (
    "algorithm",
    "machine learning",
    "deep learning",
    "neural",
    "transformer",
    "reinforcement",
    "optimization",
    "statistic",
    "probability",
    "theorem",
    "distributed system",
    "computer vision",
    "language model",
    "embedding",
    "attention",
    "gradient",
    "bayesian",
    "inference",
    "convolution",
    "diffusion",
    "quantum",
    "differential equation",
    "linear algebra",
    "graph theory",
    "complexity",
    "cryptograph",
    "information theory",
    "markov",
    "regression",
)


def _best_learning_source(topic: str, domain: str = "") -> Optional[str]:
    """Meta A picks the best search API for the SPECIFIC topic AND the learner's
    LEVEL. Research/academic/quantitative topics route to arXiv (real papers) —
    but only once the domain's knowledge mastery has left the novice band.

    Level gate (operator 2026-07-11, live loop): the mind was taught
    "encyclopedic sources for foundations, arXiv later," but this deterministic
    picker kept returning arXiv for a novice statistics domain — the fold
    correctly judged every fetch "too advanced" and had to escalate to the
    operator twice. Novices get textbooks, experts get papers: while the
    domain's mastery status is still "learning", the topic routes to the
    encyclopedic path regardless of the arXiv hints. Failure-soft: an unreadable
    mastery store keeps the topic-only behavior (never blocks a LEARN)."""
    blob = f"{topic or ''} {domain or ''}".lower()
    if not any(h in blob for h in _ARXIV_TOPIC_HINTS):
        return None
    try:
        from deepsleep_loop.mastery_store import (
            load_mastery,
            normalize_domain_id,
            status_for,
        )

        records = load_mastery()
        for target in (domain, topic):
            rec = records.get(normalize_domain_id(target)) if target else None
            if rec is not None:
                if status_for(float(rec.knowledge_mastery)) == "learning":
                    return None  # novice band -> encyclopedic path
                return "arxiv"
        # No mastery record at all = day-one novice on this domain.
        return None
    except Exception:
        return "arxiv"


def _search_phrase(topic: str) -> str:
    """Turn a LEARN topic into a real web-search phrase.

    A topic can arrive as a domain SLUG ('data-pipelines') — a poor query that
    returns off-topic content (the live incident fetched PHYSICAL pipelines for
    'data-pipelines'). De-slug a pure slug (hyphens/underscores, no spaces) into
    a phrase; a human-readable topic ('Data Pipelines', 'reinforcement learning')
    is returned unchanged. The routing 'domain' key keeps the slug — only the
    human-facing search query is de-slugged."""
    t = (topic or "").strip()
    if "://" in t or "/" in t:
        return t  # a URL/path topic must reach web_fetch verbatim
    if t and " " not in t and ("-" in t or "_" in t):
        return t.replace("-", " ").replace("_", " ").strip()
    return t


# Everything a terminal-agent continuation carries through this phase, which REBUILDS
# execution_inputs. job_id belongs here: without it the next batch lost the job its first
# batch claimed, so its mutable work was refused for want of start_job (live 2026-09-15).
_AGENT_CONTINUATION_KEYS = ("agent_continuation", "agent_next_step", "agent_observations",
                           "job_id", "job_cycle_id", "specialist_handoff")


def run(state: MetaAState) -> MetaAState:
    intent = state.intent
    text = state.text
    target_str = state.target_str
    # Action A stamps ingress metadata (including operator_route) before this
    # phase chooses a concrete tool.  Several branches rebuild execution_inputs;
    # retain the route across those replacements so AUTO sees the same lane the
    # operator selected at the HUD boundary.
    routed_operator_route = str(
        (state.execution_inputs or {}).get("operator_route")
        or (state.meta or {}).get("operator_route")
        or ""
    ).strip().lower()
    routed_operator_context = str(
        (state.execution_inputs or {}).get("operator_route_context") or ""
    ).strip()

    if isinstance((state.execution_inputs or {}).get("specialist_handoff"), Mapping):
        # Chat AUTO only persists this already-classified handoff and acknowledges
        # it. The target lane performs its own deterministic Meta A.
        state.tool = None
        state.tool_reason = None
        state.execution_intent = "Persist specialist work to its dedicated FIFO"
        return state

    if intent == "BROWSE":
        state.tool = "browser_agent"
        state.tool_reason = "AUTO runs the browser agent loop from Meta A's perception"
    elif intent == "RESPOND" or (state.execution_inputs or {}).get("agent_continuation"):
        # The operator's message is the input the fold-back Omega layer responds
        # to. Carry it in execution_inputs so it reaches the Omega packet —
        # Auto no longer reads the promptpack user_prompt (it does not generate).
        # A terminal-agent continuation keeps its task objective and earlier results.
        continuation = {key: state.execution_inputs[key] for key in _AGENT_CONTINUATION_KEYS
                        if key in (state.execution_inputs or {})}
        query = (state.execution_inputs or {}).get("query") if continuation else None
        state.execution_inputs = {"query": query or text or target_str, **continuation}
        operation = state.meta.get("plugin_operation")
        if isinstance(operation, dict) and set(operation) == {"plugin_id"}:
            # Carry the UI's app selection into the native AUTO tool exchange;
            # selection is scope, not a pre-authored tool plan.
            state.execution_inputs["plugin_operation"] = dict(operation)
        state.execution_intent = "Plan operator response (no text generation in Meta A)"
        if not continuation:
            from cognition.web_research import stamp_web_research, warrants_web_research

            seed = str(state.execution_inputs.get("query") or "")
            if warrants_web_research(intent="RESPOND", text=text, target=target_str) or (
                warrants_web_research(intent="RESPOND", text=seed, target="")
            ):
                stamp_web_research(state.execution_inputs, seed=seed)

    elif intent == "LEARN":
        from cognition.topic_resolution.resolve_topic import (
            NO_TOPIC_AVAILABLE,
            resolve_learning_topic,
        )

        resolved = resolve_learning_topic(state)
        if resolved is None or resolved.topic == NO_TOPIC_AVAILABLE:
            state.tool = None
            state.execution_inputs = {}
            state.execution_intent = "SKIP_NO_TOPIC"
            state.tool_reason = "NO_TOPIC_AVAILABLE — no resolvable learning target; aborting LEARN"
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "meta",
                    "tool_selection",
                    status="fail",
                    summary="NO_TOPIC_AVAILABLE: learning target resolution failed",
                    episode_id=state.episode_id,
                    intent="LEARN",
                )
            except Exception:
                pass
        else:
            state.tool = _learn_default_tool()
            state.tool_reason = f"Acquire knowledge on topic: {resolved.topic}"
            state.execution_inputs = {
                "query": _search_phrase(resolved.topic),
                "domain": resolved.domain,
                "topic_source": resolved.source,
            }
            if resolved.source == "trajectory_fold":
                state.execution_inputs["topic_fold"] = True
                if resolved.fold_question_id:
                    state.execution_inputs["fold_question_id"] = resolved.fold_question_id
                if resolved.fold_question_text:
                    state.execution_inputs["folded_question"] = resolved.fold_question_text
            from cognition.web_research import stamp_web_research

            stamp_web_research(
                state.execution_inputs,
                seed=state.execution_inputs["query"],
                domain=resolved.domain,
            )
            state.execution_intent = "Acquire knowledge inputs for learning"

    elif intent == "USE_TOOL":
        lowered = (text + " " + target_str).lower()
        if any(k in lowered for k in (".txt", ".pdf", ".csv", "file", "read file")):
            state.tool = "file_reader"
            state.tool_reason = "File-based task requires file reading capability"
            state.execution_inputs = {"query": target_str or text}
        elif any(
            k in lowered
            for k in (
                "understand image",
                "what's in this image",
                "describe the scene",
                "locate object",
                "where is the object",
                "analyze video",
                "spatial",
                "temporal",
                "scene analysis",
                "object localization",
                "video understanding",
            )
        ):
            state.tool = "vision_reasoning_tool"
            state.tool_reason = "Vision reasoning requires Cosmos vision-language capability"
            state.execution_inputs = {
                "image_ref": target_str or text,
                "question": text or target_str,
            }
        elif any(
            k in lowered
            for k in (
                "whose face",
                "recognize this person",
                "face match",
                "how old",
                "age/gender",
                "head pose",
                "facial landmarks",
                "face recognition",
                "identify face",
            )
        ):
            state.tool = "face_tool"
            state.tool_reason = "Face analysis requires InsightFace-style capability"
            operation = "detect_faces"
            if any(k in lowered for k in ("recognize", "identify", "whose face", "face match")):
                operation = "identify_faces"
            elif any(k in lowered for k in ("how old", "age", "gender")):
                operation = "age_gender"
            elif "head pose" in lowered:
                operation = "head_pose_estimation"
            elif "landmark" in lowered:
                operation = "landmark_detection"
            state.execution_inputs = {
                "image_ref": target_str or text,
                "operation": operation,
            }
        elif any(k in lowered for k in ("ocr", "image-to-text", "scan text")):
            state.tool = "ocr_tool"
            state.tool_reason = "Image-to-text requires OCR capability"
            state.execution_inputs = {"query": target_str or text}
        elif any(
            k in lowered for k in ("desktop", "ui automation", "click", "window", "automate ui")
        ):
            state.tool = "desktop_tool"
            state.tool_reason = "Desktop/UI interaction requires desktop capability"
            state.execution_inputs = {"query": target_str or text}
        else:
            state.tool = _learn_default_tool()
            state.tool_reason = "Default external information acquisition"
            state.execution_inputs = {"query": target_str or text}
            from cognition.web_research import stamp_web_research, warrants_web_research

            seed = str(state.execution_inputs.get("query") or "")
            if warrants_web_research(intent="USE_TOOL", text=text, target=target_str) or (
                warrants_web_research(intent="USE_TOOL", text=seed, target="")
            ):
                stamp_web_research(state.execution_inputs, seed=seed)
        state.execution_intent = "Plan tool execution for operator task"

    elif intent == "USE_SKILL":
        state.tool = None
        state.tool_reason = "Skill-mediated execution — Meta A selects ExecutionSkill; qwen tools are internal to the skill"
        state.execution_intent = (
            "Select ExecutionSkill for skill-mediated dispatch (no raw tools in Meta A)"
        )

    elif intent == "PLUGIN_OPERATION":
        # No raw Meta tool: AUTO receives only the objective and optional
        # explicit hint, then delegates the provider-neutral plan to runtime.
        state.tool = None
        state.tool_reason = "Generic plugin plan — capability dispatch is validated in AUTO"
        state.execution_intent = "Plan generic plugin operation (no execution in Meta A)"

    elif intent == "PURSUE_OUTCOME":
        transport = str(state.execution_inputs.get("motor_transport") or "")
        if transport == "USE_TOOL":
            state.tool = "web_fetch"
            state.tool_reason = "Emergent motor program requires deterministic tool transport"
        elif transport == "USE_SKILL":
            state.tool = None
            state.tool_reason = (
                "Emergent motor program uses skill-mediated transport with no raw Meta tool"
            )
        else:
            raise ValueError(f"invalid motor_transport: {transport!r}")

    elif intent == "IMAGE_CREATE":
        from core.directives_config import tool_for_directive_intent

        state.tool = tool_for_directive_intent(intent) or "image_tool"
        state.tool_reason = "Image generation requires image_tool capability"
        spec = target_str or text or "image"
        state.execution_inputs = {"image_spec": spec}
        state.execution_intent = "Plan image creation (no rendering in Meta A)"

    elif intent == "DOCUMENT_CREATE":
        from core.directives_config import tool_for_directive_intent

        state.tool = tool_for_directive_intent(intent) or "document_tool"
        state.tool_reason = "Document creation requires document_tool capability"
        state.execution_inputs = {
            "document_spec": {
                "format": state.meta.get("format", "md"),
                "template": state.meta.get("template", "generic"),
                "upload_ref": state.meta.get("upload_ref"),
                "text": target_str or text or "",
            }
        }
        state.execution_intent = "Plan document creation (no rendering in Meta A)"

    if routed_operator_route:
        state.execution_inputs["operator_route"] = routed_operator_route
    if routed_operator_context:
        state.execution_inputs["operator_route_context"] = routed_operator_context

    return state
