"""Canonical contributor / ignition / intent-decision types (Slice 1).

Pressure gathers ``Contributor`` records; ``EpisodeIgnition`` is the
pressure output; ``IntentDecision`` is Global A's intent-only output.
Global A PARSES intent from pressure — it never creates a goal or a target.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Mapping, Optional, Tuple

ALLOWED_INTENTS: Tuple[str, ...] = (
    "BROWSE",
    "REFLECT_WINDOW",
    "RESPOND",
    "PROACTIVE_RESPONSE",
    "RESOLVE_QUERY",
    # The system-generated query to the operator — a FIRST-CLASS artifact
    # (operator directive 2026-06-12). Its own route through every layer
    # (intent → action → prompt pack → text app), never folded into
    # PROACTIVE_RESPONSE: clean separation keeps debugging deterministic
    # and every hop auditable.
    "ASK_OPERATOR",
    "LEARN",
    "USE_TOOL",
    "IMAGE_CREATE",
    "DOCUMENT_CREATE",
    "IDLE",
    # The canonical SKILL-MEDIATED agentic intent (Phase 6E). Global A emits it for
    # agentic work (inspect / repair / implement / test / review); Meta A selects the
    # exact ExecutionSkill, Auto loads it and dispatches qwen inside the skill box. Never
    # names a skill or tool at the Global level. Legacy APPLY is retired — canonicalized
    # to USE_SKILL at emit time when still seen in Omega store / operator directives.
    "USE_SKILL",
    # Omega-authored executable pressure crosses Global A as a stable intent; Meta A
    # deterministically selects the motor transport without adding inference.
    "PURSUE_OUTCOME",
    # All external applications enter through the signed MCP plugin runtime.
    "PLUGIN_OPERATION",
)

_OPERATOR_INGRESS_TO_TYPE: Dict[str, str] = {
    "operator_command": "operator_command",
    "epistemic_request": "operator_query",
    "phatic": "operator_query",
    "answer_to_question": "operator_response",
    "sensory_update": "operator_command",
    "evidence_reference": "operator_command",
    "authority_request": "operator_command",
}

_COMMAND_PREFIX = (
    r"^(?:please\s+)?(?:"
    r"(?:can|could|would|will)\s+you\s+|"
    r"i\s+(?:want|need)\s+(?:you\s+)?to\s+|"
    r"i(?:'d|\s+would)\s+like\s+you\s+to\s+"
    r")?"
)
_LEARN_COMMAND = re.compile(
    _COMMAND_PREFIX
    + r"(?:learn|study|research|investigate|explore)\b|"
    + _COMMAND_PREFIX
    + r"teach\s+me\b",
    re.IGNORECASE,
)
# Leading verbs that mean "perform work" rather than "talk about work". They
# ROUTE NOTHING (operator 2026-09-05: "one pipeline for chat ... it will chat,
# use tools, search the web etc on command"). A work request is conversation
# with a job in it: the chat brain reads it on the RESPOND rail and calls its
# bounded tools on demand inside AUTO (auto_loop/execute/run.py). The list
# survives only to corroborate the work-order SHAPE of a short go-ahead
# (pressure_loop.operator_chat._work_order_shaped).
OPERATOR_ACTION_VERBS: Tuple[str, ...] = (
    "add",
    "analyze",
    "build",
    "convert",
    "copy",
    "create",
    "delete",
    "deploy",
    "design",
    "download",
    "edit",
    "execute",
    "fetch",
    "find",
    "fix",
    "generate",
    "implement",
    "inspect",
    "install",
    "integrate",
    "make",
    "modify",
    "move",
    "open",
    "read",
    "refactor",
    "remove",
    "rename",
    "repair",
    "review",
    "run",
    "search",
    "set",
    "summarize",
    "test",
    "update",
    "upload",
    "validate",
    "verify",
    "write",
)
# The intent each deterministic command kind carries onto the directive rail.
# Stamped at intake so pressure ignites the right lane and Global A obeys rather
# than infers — one judgement per turn, made once, before anything can disagree
# about it.
#
# "remember" has no entry on purpose: it remains a RESPONSE action so AUTO can
# perform memory intake and return the actual store result on the same chat rail.
#
# "build" (2026-08-15 → 2026-09-05) is gone. It used to stamp USE_SKILL from a
# verb regex, which sent "write a hello world script" through Meta A skill
# selection and Omega motor planning before any model read the sentence; the
# regex decided, not the mind. Imperative work now rides RESPOND like every
# other operator utterance and the chat brain decides — per turn, from the
# words — whether to call a tool.
_COMMAND_KIND_DIRECTIVES = {
    "learn": "LEARN",
}

_REMEMBER_COMMAND = re.compile(
    _COMMAND_PREFIX + r"(?:remember|note)(?:\s+that)?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, eq=False)
class Contributor:
    source: str  # "operator" | "autonomous" | "system" | "memory_taxonomy" (7M advisory)
    type: str
    weight: float
    metadata: Dict[str, Any]
    __hash__ = None

    def __eq__(self, other: object) -> bool:
        if type(other) is not Contributor:
            return NotImplemented
        if (self.source, self.type, self.weight) != (other.source, other.type, other.weight):
            return False
        left = dict(self.metadata)
        right = dict(other.metadata)
        left.pop("arrival_seq", None)
        right.pop("arrival_seq", None)
        return left == right


@dataclass(frozen=True)
class EpisodeIgnition:
    episode_id: str
    aggregate_pressure: float
    opened_by: str  # dominant contributor's type
    dominant_contributor: Contributor  # the full dominant Contributor
    contributors: Tuple[Contributor, ...]
    trajectory_snapshot: Optional[Dict[str, Any]]
    # Chemistry is carried forward as advisory decision context. It is kept
    # separate from pressure so it cannot manufacture, suppress, or reorder
    # the stimulus that opened the episode.
    neuromod_state: Dict[str, float] = field(default_factory=dict)
    pipeline_id: int = -1
    work_kind: str = "autonomous"


@dataclass(frozen=True)
class IntentDecision:
    episode_id: str
    intent: str
    confidence: float
    reason: str
    omega_lane: Literal["chat", "agent", "browser", "window"] = "chat"

    def __post_init__(self):
        from schema.omega_control_decision import OMEGA_LANES

        if self.omega_lane not in OMEGA_LANES:
            raise ValueError("invalid omega_lane")


def make_contributor(
    source: str,
    type: str,
    weight: float,
    metadata: Optional[Dict[str, Any]] = None,
) -> Contributor:
    return Contributor(
        source=str(source or "").strip(),
        type=str(type or "").strip(),
        weight=max(0.0, float(weight)),
        metadata=dict(metadata or {}),
    )


def make_plugin_permission_decision_contributor(metadata: Dict[str, Any]) -> Contributor:
    """Build the sole structured operator envelope for a plugin decision.

    Validation of the exact closed metadata shape lives in
    ``pressure_loop.plugin_continuations``.  Keeping construction here makes
    the contributor provenance explicit while avoiding provider or authority
    dependencies in the canonical contributor module.
    """
    return Contributor(
        source="operator",
        type="plugin_permission_decision",
        weight=1.0,
        metadata=dict(metadata),
    )


def infer_operator_command_kind(message: str) -> Optional[str]:
    """Return the leading explicit operator speech act, or None for conversation.

    Two speech acts are deterministic at intake: an explicit learning ask
    ("learn", "study", "research", "teach me") and an explicit memory intake
    ("remember", "note that"). The first verb is authoritative for compound
    requests: "learn how to build" is learning.

    Everything else — including imperative work ("build", "write", "fix",
    "run") — is conversation for the chat brain, which calls its tools on
    demand. No verb list turns a sentence into a build here (2026-09-05).
    """
    text = str(message or "").strip()
    if not text:
        return None
    if _LEARN_COMMAND.match(text) is not None:
        return "learn"
    if _REMEMBER_COMMAND.match(text) is not None:
        return "remember"
    return None


def _infer_operator_type(message: str, ingress_class: str) -> str:
    ic = (ingress_class or "").strip().lower()
    if ic in _OPERATOR_INGRESS_TO_TYPE:
        return _OPERATOR_INGRESS_TO_TYPE[ic]
    text = (message or "").strip().lower()
    if infer_operator_command_kind(text) is not None:
        return "operator_command"
    if text.endswith("?"):
        return "operator_query"
    if any(k in text for k in ("remember", "note that", "update ", "fixed ")):
        return "operator_response"
    return "operator_command" if text else "operator_query"


def from_operator_message(msg: Mapping[str, Any]) -> Contributor:
    """Normalize an operator ingress record into a Contributor."""
    message = str(msg.get("message") or msg.get("text") or "")
    ingress_class = str(msg.get("ingress_class") or "")
    ctype = _infer_operator_type(message, ingress_class)
    weight = float(msg.get("weight", msg.get("magnitude", 1.0)) or 1.0)
    meta = dict(msg)
    if message and "message" not in meta:
        meta["message"] = message
    # Explicit directive — an authoritative intent from an unambiguous UI
    # affordance (e.g. the image create panel sends "IMAGE_CREATE"). When the
    # caller already KNOWS the intent, Global A obeys it verbatim instead of
    # re-inferring from message text. Validated against the canonical intent
    # set; anything else is dropped so inference stays the fallback.
    directive = str(msg.get("directive_intent") or msg.get("intent") or "").strip().upper()
    if directive == "APPLY":
        # Legacy operator/bridge directive — retired in favor of USE_SKILL (Phase 6M).
        meta["directive_intent"] = "USE_SKILL"
    elif directive in ALLOWED_INTENTS:
        meta["directive_intent"] = directive
    else:
        meta.pop("directive_intent", None)
    # A user QUESTION is itself an authoritative ask (operator directive
    # 2026-06-12): carry the exact intent keyword so Global A routes it on
    # the directive rail — the same mechanism as IMAGE_CREATE /
    # DOCUMENT_CREATE — never word inference. Commands keep their own
    # inference path; an explicit caller directive above is never overridden.
    if "directive_intent" not in meta and ctype == "operator_query":
        meta["directive_intent"] = "RESPOND"
    if ctype == "operator_command" and "directive_intent" not in meta:
        command_kind = infer_operator_command_kind(message)
        if command_kind is not None:
            meta["operator_command_kind"] = command_kind
            # STAMP THE INTENT HERE, before pressure ever sees the turn
            # (operator 2026-08-15: "set right outside the hud before pressure,
            # that way it can label the incoming input correctly so when it hits
            # pressure, the correct ignition can happen so when global gets it,
            # there is 0 ambiguity and the right channels always open").
            #
            # Everything downstream then READS a decision instead of re-deriving
            # one. Global A honors a directive_intent verbatim
            # (global_loop/intent/run.py), and core.lanes routes the directive to
            # its lane — so the same sentence can never be judged differently by
            # two stages. That is not hypothetical: on 2026-08-15 three places
            # derived a turn's lane independently, two disagreed, and the
            # appliance deadlocked with a build queued behind a chat carrier it
            # never asked for.
            #
            # The rail this lands on is already built end to end. LEARN: Global
            # A honors it, Meta A resolves the topic and picks the fetch tool
            # (meta_loop/tool/run.py), AUTO executes, and the normal foldback
            # closes it.
            #
            # "remember" is deliberately absent. Global A maps its stamped kind
            # to RESPOND, and AUTO performs deterministic memory intake before
            # resolving the same waiting chat turn with the store result.
            #
            # Imperative work stamps nothing: it is conversation, Global A maps
            # it to RESPOND, and the chat brain calls tools on demand in AUTO.
            #
            # Keep both the speech-act kind and resolved intent on the envelope.
            # Downstream stages read this one ingress decision rather than
            # reclassifying delayed text after it leaves the HUD.
            directive = _COMMAND_KIND_DIRECTIVES.get(command_kind)
            if directive:
                meta["directive_intent"] = directive
        else:
            meta.pop("operator_command_kind", None)
    else:
        meta.pop("operator_command_kind", None)
    return Contributor(
        source="operator",
        type=ctype,
        weight=max(0.0, weight),
        metadata=meta,
    )
