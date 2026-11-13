"""Meta A memory phase — memory_required / scope / reason per intent."""

from __future__ import annotations

import re

from auto_loop.promptpack.question_shape import is_question_shaped as _is_question_shaped
from meta_loop._meta_a_state import MetaAState


def _memory_prior_re() -> re.Pattern[str]:
    from pressure_loop.policy import meta_memory_pattern

    pat = meta_memory_pattern(
        "respond_prior",
        default=r"\b(what did i tell|remember|previously)\b",
    )
    return re.compile(pat, re.I)


def _references_prior_conversation(text: str) -> bool:
    """True only when the operator points to a prior conversational turn.

    A bare request for stored knowledge (``what do you remember about Noah``)
    is not a reference to a prior *conversation*.  Treating every occurrence
    of ``remember`` as one collapsed the full question recall bundle to a
    single generic MRIL lane.
    """

    value = str(text or "").strip()
    if not value or not _memory_prior_re().search(value):
        return False
    if re.search(r"\bwhat\s+do\s+you\s+remember\s+about\b", value, re.I):
        return False
    return bool(
        re.search(
            r"\b(?:what\s+did\s+i\s+tell|previously|earlier|"
            r"remember\s+when|we\s+(?:talked|discussed)|"
            r"(?:our|the)\s+(?:prior|previous|earlier)\s+(?:chat|conversation))\b",
            value,
            re.I,
        )
    )


def _prior_history_re() -> re.Pattern[str]:
    from pressure_loop.policy import meta_memory_pattern

    pat = meta_memory_pattern(
        "learn_prior",
        default=r"\b(remember|my earlier|previously|what did i tell)\b",
    )
    return re.compile(pat, re.I)


def _apply_mril_fields(state: MetaAState, query: str) -> None:
    from auto_loop.mril_tools.router import route_memory_tool

    state.memory_query = query.strip()
    state.memory_delivery_mode = "inference_tool"
    state.memory_tools = (route_memory_tool(state.memory_query),)
    scopes = []
    for tool in state.memory_tools:
        lane = tool.get("lane")
        if lane and lane not in scopes:
            scopes.append(lane)
    if scopes:
        merged = tuple(dict.fromkeys(tuple(state.memory_scope or ()) + tuple(scopes)))
        state.memory_scope = merged


def _learn_recall_tool_for_domain(domain: str) -> str:
    """The remember tool for the DOMAIN's OWN lobe: a coding subcategory (e.g. "python") ->
    remember_code (lobe 8), everything else -> remember_knowledge (lobe 2). Uses the SAME
    deterministic coding-vs-knowledge discrimination that PLACED the subdomain at onboarding,
    so the LEARN grounding recall searches where the domain's facts actually live. Failure-soft
    -> remember_knowledge."""
    try:
        from memory.routing.coding_vs_knowledge import classify_topic_lobe

        return "remember_code" if classify_topic_lobe(domain) == 8 else "remember_knowledge"
    except Exception:
        return "remember_knowledge"


def _apply_learn_knowledge_recall(state: MetaAState, query: str) -> None:
    """LEARN recall searches the DOMAIN's own lobe — the subcategory Omega chose to learn decides
    where it recalls. A coding subcat ("python") recalls from coding (lobe 8) via remember_code; a
    knowledge subcat from knowledge (lobe 2) via remember_knowledge. The fixed remember_knowledge
    search missed coding facts entirely (a coding domain's memories live in lobe 8), so the mind
    could never see what it already knew about a coding topic and kept re-learning it."""
    from auto_loop.mril_tools.router import _tool_entry

    state.memory_query = query.strip()
    state.memory_delivery_mode = "inference_tool"
    domain = str((state.execution_inputs or {}).get("domain") or "").strip()
    tool = dict(_tool_entry(_learn_recall_tool_for_domain(domain), query.strip()))
    if domain:
        tool["domain"] = domain
    state.memory_tools = (tool,)
    scopes = []
    for tool in state.memory_tools:
        lane = tool.get("lane")
        if lane and lane not in scopes:
            scopes.append(lane)
    if scopes:
        merged = tuple(dict.fromkeys(tuple(state.memory_scope or ()) + tuple(scopes)))
        state.memory_scope = merged


_IDENTITY_RECALL_QUERY = "Omega's identity, character, traits, persona, and self"

# Whole-lobe grounding recalls (identity/persona) pull EVERY subdirectory of their lobe,
# exhaustively. The identity + persona lobes are small canonical sets (constitution, creator,
# omega-self, emergent traits, persona), so a generous cap returns all of them — the recall is
# EXACT (the full self), never a wording-shaped slice that can miss the creator.
_IDENTITY_RECALL_LIMIT = 50

def _is_project_build_query(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    return bool(
        re.search(
            r"\b("
            r"project|build|workspace|repo|repository|codebase|todo-app|todo app|"
            r"continue (?:our|the|this)?\s*work|resume|pick (?:it|this|that) up|"
            r"working on|we built|we were building|add tests?|test suite|"
            r"implementation|artifact|dev lobe"
            r")\b",
            lowered,
        )
    )


def _identity_recall_subdomains() -> tuple[int, ...]:
    from memory.identity.subdomains import identity_recall_subdomains

    return tuple(identity_recall_subdomains())


def _persona_recall_query(operator_text: str) -> str:
    """Persona (lobe 1) grounding query = the operator's own words FUSED with the
    fixed identity grounding terms.

    The persona recall is whole-lobe grounding either way (grounding_recall
    bypasses the relevance floor); the query only RANKS which lobe-1 records
    survive the downstream anchor cap. Keyed to the fixed identity string alone,
    operator facts and relation leaves ("Noah Lee's son is Abel") share zero
    tokens with it, so they ranked below Omega's self-records and got truncated —
    "tell me about Noah" then surfaced nothing about Noah's son. Fusing keeps the
    self/voice records ranked (they match the identity terms) AND lifts operator
    + relational records when the operator names a person. The result is never
    the bare operator text (extra terms), so the body-lane keying invariant
    (persona query != the raw question) still holds."""
    text = (operator_text or "").strip()
    if not text or text == _IDENTITY_RECALL_QUERY:
        return _IDENTITY_RECALL_QUERY
    return f"{text} {_IDENTITY_RECALL_QUERY}"


def _apply_freeform_recall(state: MetaAState, query: str) -> None:
    """Freeform grounding: force-recall Omega's self plus remembered context for operator chat.

    IDENTITY (lobe 0) pulls constitution + traits only (omega_loop seeds + evidenced traits).
    PERSONA (lobe 1) stays whole-lobe. KNOWLEDGE (2), CODING (8), and TEMPORAL (9) use the
    operator's freeform text. Situational (4) and journal/deepsleep (6) stay out of user-facing recall.
    """
    from auto_loop.mril_tools.router import _tool_entry

    id_subs = list(_identity_recall_subdomains())
    q = query.strip() or _IDENTITY_RECALL_QUERY
    project_build_query = _is_project_build_query(q)
    state.memory_query = q
    state.memory_delivery_mode = "inference_tool"
    tools = [
        _tool_entry(
            "remember_identity",
            _IDENTITY_RECALL_QUERY,
            scope_subdomain=False,
            limit=_IDENTITY_RECALL_LIMIT,
            allowed_subdomains=id_subs,
        ),
        _tool_entry(
            "remember_persona",
            _persona_recall_query(q),
            scope_subdomain=False,
            limit=_IDENTITY_RECALL_LIMIT,
        ),
        _tool_entry("remember_knowledge", q),
        _tool_entry("remember_code", q),
        _tool_entry("remember_time", q),
    ]
    if project_build_query:
        tools.append(_tool_entry("remember_workflow", q))
        tools.append(_tool_entry("remember_dev", q))
    state.memory_tools = tuple(tools)
    scopes = []
    for tool in state.memory_tools:
        lane = tool.get("lane")
        if lane and lane not in scopes:
            scopes.append(lane)
    if scopes:
        state.memory_scope = tuple(dict.fromkeys(tuple(state.memory_scope or ()) + tuple(scopes)))


# remember_* tools whose lobe holds the operator's own facts / Omega's own
# identity: a question routed there is answerable from memory or not at all.
_MEMORY_BOUND_TOOLS = frozenset({"remember_persona", "remember_identity"})


def _apply_question_recall(state: MetaAState, query: str) -> None:
    """Route a question through the single lobe that owns its answer.

    The MRIL router already carries the deterministic intent-to-lobe contract.
    Rebuilding an all-lobe sweep here bypassed that contract, repeated recall and
    verification eleven times, and admitted unrelated evidence into the answer.
    """
    _apply_mril_fields(state, query)
    # An operator-personal or identity question is memory-bound by nature: the
    # fact is on record or it is not, so an empty recall must become "not on
    # record", never base-model knowledge. Live 2026-09-06: "which text editor
    # do I like best?" recalled nothing and the model invented Notepad++.
    if any(str(tool.get("tool") or "") in _MEMORY_BOUND_TOOLS for tool in state.memory_tools or ()):
        state.memory_answer_required = True


# wording about work that was built: the temporal sweep adds the dev lobe for it
_BUILT_WORK_RE = re.compile(
    r"\b(?:build|built|make|made|write|wrote|create|created|code|coded|implement|implemented|script|program|app)\b",
    re.I,
)


def _apply_temporal_recall(state: MetaAState, query: str) -> None:
    """Temporal grounding (live 2026-07-19: the "temporal" chat intent had NO
    branch here — memory_required stayed False and "what was the last thing you
    learned?" answered from the base model with zero recall): dated episode
    records + the mind's own fold statements (lobe 4 "what" only) + knowledge,
    all keyed to the operator's wording. RESPONSE_TEMPORAL additionally composes
    its date-range block at AUTO; this envelope is what makes recall run at all."""
    from auto_loop.mril_tools.router import _tool_entry

    q = query.strip()
    state.memory_query = q
    state.memory_delivery_mode = "inference_tool"
    state.memory_tools = (
        _tool_entry("remember_time", q),
        _tool_entry("remember_situation", q, allowed_subdomains=(1,)),  # subdomain 1 = "what"
        _tool_entry("remember_knowledge", q),
        # Learned comprehension routes coding-adjacent material to lobe 8
        # (live 2026-07-19: kubernetes fetched_knowledge landed in coding) —
        # temporal "what did you learn" must sweep it too.
        _tool_entry("remember_code", q),
    )
    if _BUILT_WORK_RE.search(q):
        # "what did I build yesterday?" is answered by the builder artifacts in
        # lobe 11 (live 2026-09-06: this sweep never reached them and the model
        # read DeepSleep trait-promotion episodes as things the operator built).
        state.memory_tools = state.memory_tools + (_tool_entry("remember_dev", q),)
    scopes = []
    for tool in state.memory_tools:
        lane = tool.get("lane")
        if lane and lane not in scopes:
            scopes.append(lane)
    if scopes:
        state.memory_scope = tuple(dict.fromkeys(tuple(state.memory_scope or ()) + tuple(scopes)))


def _recall_imperative_re() -> re.Pattern[str]:
    """Imperative-form recall requests — the question-shape floor misses them
    (no '?'), so a classifier flake dropped "Tell me everything you know about
    Noah" to zero recall (live 2026-07-19, classification=None ok=False)."""
    from pressure_loop.policy import meta_memory_pattern

    pat = meta_memory_pattern(
        "respond_recall_imperative",
        default=(
            r"^(tell|show|remind)\s+me\b"
            r"|\b(what|everything)\s+(do\s+)?you\s+(know|remember|recall)\b"
            r"|\byou('ve| have)\s+learned\b"
        ),
    )
    return re.compile(pat, re.I)


def _requires_memory_bound_answer(text: str) -> bool:
    """Whether an empty recall must become an explicit memory gap.

    ``memory_required`` means AUTO should attempt recall.  This narrower flag
    means the operator asked about Omega's stored experience itself, so the
    answer may not silently fall back to base-model knowledge when recall is
    empty.  Ordinary factual questions still benefit from recall without being
    converted into false "I don't remember" refusals.
    """
    value = str(text or "").strip()
    if not value:
        return False
    return bool(
        _references_prior_conversation(value)
        or re.search(
            r"\b(?:remember|recall)\b|"
            r"\b(?:what|everything)\s+(?:do\s+)?you\s+know\s+about\b|"
            r"\bwhat\s+(?:have|did)\s+(?:you|we)\s+(?:learn|do|build|discuss)\b",
            value,
            re.I,
        )
    )


def _apply_reasoning_recall(state: MetaAState, query: str) -> None:
    """Reasoning grounding: a chain-of-thought reply that helps the operator reason OVER
    information. Unlike freeform/question this is NOT a "be Omega" turn — it pulls NO identity and
    NO persona (no dynamic personality directive is composed for reasoning). It grounds on two
    query-shaped lanes, keyed to the operator's actual wording so the chain of thought works over
    the relevant remembered material:

      - KNOWLEDGE (lobe 2) — facts relevant to what's being reasoned about
      - CODING   (lobe 8)  — code/implementation context relevant to it

    Both are query-shaped (``subdomain_for`` narrows when it maps the wording confidently, else
    whole-lobe). The recall executor runs both remember_* tools and aggregates the evidence into the
    user grounding block; execute injects no identity system for the reasoning prompt type."""
    from auto_loop.mril_tools.router import _tool_entry

    q = query.strip()
    state.memory_query = q
    state.memory_delivery_mode = "inference_tool"
    state.memory_tools = (
        _tool_entry("remember_knowledge", q),
        _tool_entry("remember_code", q),
    )
    scopes = []
    for tool in state.memory_tools:
        lane = tool.get("lane")
        if lane and lane not in scopes:
            scopes.append(lane)
    if scopes:
        state.memory_scope = tuple(dict.fromkeys(tuple(state.memory_scope or ()) + tuple(scopes)))


def _promote_mril_response(
    state: MetaAState,
    *,
    query: str,
    reason: str,
    scope: tuple[str, ...],
    execution_intent: str,
) -> None:
    """Memory-only episodes use MRIL in execute — never the legacy memory_recall tool."""
    state.memory_required = True
    state.memory_answer_required = True
    state.memory_reason = reason
    merged = tuple(dict.fromkeys(tuple(state.memory_scope or ()) + tuple(scope)))
    state.memory_scope = merged
    _apply_mril_fields(state, query)
    state.action = "RESPONSE"
    state.tool = None
    state.tool_reason = None
    state.execution_intent = execution_intent


def _run(state: MetaAState) -> MetaAState:
    if isinstance((state.execution_inputs or {}).get("specialist_handoff"), dict):
        # This is still the Chat admission episode. Coding recall belongs to the
        # target episode after its concrete run anchor is resolved.
        state.memory_required = False
        state.memory_answer_required = False
        state.memory_reason = None
        state.memory_scope = ()
        state.memory_query = None
        state.memory_delivery_mode = None
        state.memory_tools = ()
        return state
    # Browser capability turns are action-grounded.  The ingress route wins
    # over classifier labels that would otherwise stamp a recall contract
    # before the browser tool runs.
    operator_route = str(
        (state.execution_inputs or {}).get("operator_route")
        or (state.meta or {}).get("operator_route")
        or ""
    ).strip().lower()
    if operator_route == "browser":
        state.memory_required = False
        state.memory_answer_required = False
        state.memory_reason = None
        state.memory_scope = ()
        state.memory_query = None
        state.memory_delivery_mode = None
        state.memory_tools = ()
        return state
    # A direct workspace observation is grounded by the trusted-folder tool,
    # not by an older conversational transcript.  Keep agent/build turns on
    # their normal recall path; this narrow check only covers the deterministic
    # directory-inspection capability.
    if operator_route == "agent":
        from auto_loop.promptpack.operator_intent import is_workspace_inspection_request

        if is_workspace_inspection_request(state.text or state.target_str):
            state.memory_required = False
            state.memory_answer_required = False
            state.memory_reason = None
            state.memory_scope = ()
            state.memory_query = None
            state.memory_delivery_mode = None
            state.memory_tools = ()
            return state

    intent = state.intent
    text = state.text
    target_str = state.target_str
    query = target_str or text

    if intent == "RESPOND":
        # PER-INTENT recall grounding: each RESPONSE prompt-pack type gets its own recall set so
        # the reply is grounded for what it IS. The chat-intent rides on state.meta["chat_intent"]
        # (action/run.py threads the contributor metadata). STEP 1 wires freeform; reasoning /
        # question follow as their own steps.
        chat_intent = str((state.meta or {}).get("chat_intent") or "").strip().lower()
        # Ingress classification and response prompt-packs use different
        # vocabularies. A valid semantic request label must not suppress the
        # deterministic question floor (live miss: ``epistemic_request``
        # skipped remember_persona for the Noah query).
        if chat_intent in {"epistemic_request", "factual_question", "information_request"}:
            chat_intent = "question"
        if _references_prior_conversation(text) or _references_prior_conversation(target_str):
            # Prior-reference semantics outrank reply shape.  A classifier label
            # still determines how AUTO speaks, but recall must first retrieve the
            # conversation the operator explicitly referenced.
            state.memory_required = True
            state.memory_answer_required = True
            state.memory_reason = "Question references prior operator conversation"
            state.memory_scope = ("operator_history", "conversation")
            _apply_mril_fields(state, query)
        elif chat_intent == "freeform":
            # Freeform chit-chat is grounded by Omega's own self, remembered knowledge/code, and
            # temporal episode context so it sounds like Omega being Omega while staying context-aware.
            # Recalled identity/persona traits become the dynamic behavioral prompt at AUTO.
            state.memory_required = True
            state.memory_answer_required = _requires_memory_bound_answer(query)
            state.memory_reason = "Freeform chat — recall identity + persona + knowledge + coding + temporal episodes to respond as Omega"
            _apply_freeform_recall(state, query)
        elif chat_intent == "question":
            # The MRIL router selects the one lobe that owns the answer.  The
            # response composer then reasons over that lane's verified evidence.
            state.memory_required = True
            state.memory_answer_required = _requires_memory_bound_answer(query)
            state.memory_reason = "Question — recall the routed owner lobe for answer grounding"
            _apply_question_recall(state, query)
        elif chat_intent == "temporal":
            # Temporal (live gap 2026-07-19): "what did you learn / what did we do"
            # questions classify temporal — with no branch here they recalled
            # NOTHING and answered from the base model.
            state.memory_required = True
            state.memory_answer_required = True
            state.memory_reason = "Temporal recall — dated episodes + learned comprehension + knowledge for the referenced span"
            _apply_temporal_recall(state, query)
        elif chat_intent == "reasoning":
            # Reasoning (per-intent recall STEP 3): a chain-of-thought reply that helps the operator
            # reason OVER information — NOT a "be Omega" turn, so NO identity/persona. It grounds on
            # KNOWLEDGE + CODING keyed to the prompt so the chain of thought works over the relevant
            # remembered facts/code. (Classifier said "reasoning" — authoritative.)
            state.memory_required = True
            state.memory_answer_required = _requires_memory_bound_answer(query)
            state.memory_reason = (
                "Reasoning — recall knowledge + coding to reason over the information"
            )
            _apply_reasoning_recall(state, query)
        elif not chat_intent and (
            _is_question_shaped(text) or _recall_imperative_re().search(text)
        ):
            # RECALL-SKIP FIX (Stage 0) — the deterministic floor: when the SetFit chat-intent
            # classifier is OFF/unreachable, chat_intent is absent and a plain factual question
            # ("who is your creator?") used to fall through (memory_required stayed False and
            # read_memory was NEVER called). A non-prior-conversation question shape now grounds the
            # routed owner lobe here. The classifier still wins when it returns a label (above).
            state.memory_required = True
            state.memory_answer_required = _requires_memory_bound_answer(query)
            state.memory_reason = "Question — recall the routed owner lobe for answer grounding"
            _apply_question_recall(state, query)

    elif intent == "RESOLVE_QUERY":
        # The operator answered a system-initiated question (2026-07-10):
        # ground the reply on BOTH sides of the thread. The system's own
        # question text disambiguates short/polysemous answers ("python" the
        # language, not the snake — the sense filter refuses bare polysemes)
        # and clears the recall relevance floor (>=2 shared content tokens);
        # without it the answer turn recalled NOTHING (memory_required was
        # False) and Omega replied blind to a thread it started itself.
        thread_q = str((state.meta or {}).get("system_query_text") or "").strip()
        recall_query = " ".join(part for part in (thread_q, text) if part).strip()
        if recall_query:
            state.memory_required = True
            state.memory_reason = (
                "Operator answered a system question — recall the question "
                "topic to ground the reply"
            )
            _apply_question_recall(state, recall_query)
        else:
            state.memory_required = False

    elif intent == "LEARN":
        resolved_topic = str((state.execution_inputs or {}).get("query") or "").strip()
        if resolved_topic:
            state.memory_required = True
            state.memory_reason = f"Recall prior knowledge on the learning target: {resolved_topic}"
            _apply_learn_knowledge_recall(state, resolved_topic)
        elif _prior_history_re().search(text) or _prior_history_re().search(target_str):
            state.memory_required = True
            state.memory_reason = "Learning target references prior operator history"
            state.memory_scope = ("operator_history",)
            _apply_mril_fields(state, query)

    elif (
        intent == "PROACTIVE_RESPONSE"
        and str((state.execution_inputs or {}).get("presentation_kind") or "") == "PRESENT_RESULT"
    ):
        policy = (state.execution_inputs or {}).get("operator_policy") or {}
        if isinstance(policy, dict):
            recall_query = " ".join(
                part
                for part in (
                    str(policy.get("objective") or ""),
                    *(str(value) for value in policy.get("requested_demonstrations") or ()),
                )
                if part.strip()
            ).strip()
            if recall_query:
                state.memory_required = True
                state.memory_reason = (
                    "Recall learned knowledge for the operator-authorized dialogue application"
                )
                _apply_learn_knowledge_recall(state, recall_query)

    elif intent == "USE_SKILL":
        # Recall-before-apply (skill path): pull domain knowledge before qwen executes inside the skill.
        recall_target = query.strip()
        if recall_target:
            state.memory_required = True
            state.memory_reason = (
                f"Recall domain knowledge before skill-mediated execution: {recall_target[:80]}"
            )
            _apply_learn_knowledge_recall(state, recall_target)

    elif intent == "USE_TOOL":
        lowered = (text + " " + target_str).lower()
        if _prior_history_re().search(lowered):
            _promote_mril_response(
                state,
                query=query,
                reason="USE_TOOL references prior operator history",
                scope=("operator_history", "conversation"),
                execution_intent=(
                    "Plan operator response with MRIL recall (no external memory tool)"
                ),
            )

    return state


def _agent_recalls(state: MetaAState) -> bool:
    """A deterministic workspace inspection recalls nothing by design; every other agent task does."""
    from auto_loop.promptpack.operator_intent import is_workspace_inspection_request

    return not is_workspace_inspection_request(state.text or state.target_str)


def run(state: MetaAState) -> MetaAState:
    state = _run(state)
    if isinstance((state.execution_inputs or {}).get("specialist_handoff"), dict):
        return state
    operator_route = str(
        (state.execution_inputs or {}).get("operator_route")
        or (state.meta or {}).get("operator_route")
        or ""
    ).strip().lower()
    if operator_route != "agent" or not _agent_recalls(state):
        return state
    # An agent task always recalls the dev lobe: the trusted workspaces it may work in and
    # what earlier tool runs observed there (operator 2026-09-15: "have the dev mril tool
    # recall them when working so omega has context of where its allowed to work").
    if not any(tool.get("tool") == "remember_dev" for tool in state.memory_tools or ()):
        from auto_loop.mril_tools.router import _tool_entry

        q = str(state.memory_query or state.text or state.target_str or "")
        entry = _tool_entry("remember_dev", q)
        state.memory_tools = tuple(state.memory_tools or ()) + (entry,)
        lane = entry.get("lane")
        if lane and lane not in (state.memory_scope or ()):
            state.memory_scope = tuple(state.memory_scope or ()) + (lane,)
        state.memory_required = True
        if not state.memory_reason:
            state.memory_reason = "agent task: recall the trusted workspaces and earlier dev observations"
        if not state.memory_query:
            state.memory_query = q
    return state
