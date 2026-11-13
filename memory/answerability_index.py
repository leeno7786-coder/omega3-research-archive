"""
answerability_index.py
Derive answerability metadata from memory dicts and score against plans.

Read-only / side-effect-free. No durable sidecar writes — the slice is
purely derivational against fields already on the memory record.

What "answerability" means here:
    Walking the corpus by what each memory CAN answer, not just by what
    each memory said. The walker scores by surface tokens; the
    answerability surface scores by intent fit. Together they let the
    posture-aware arbitrator pick the right memory for a question
    instead of the most-similar paragraph.

What we derive per memory:
    - evidence_role:        single tag (identity_statement / explanation /
                            tool_spec / procedure / decision_record /
                            event / persona_record / status_record / ...)
    - answerable_questions: short list of question shapes this memory
                            plausibly answers ("who is X", "what does X
                            do", "explain X", "when did X happen", ...)
    - recall_aliases:       lowercased title + summary tokens + tag tokens
    - coord_aliases:        readable lobe.subdomain hint strings, e.g.
                            "lobe:5", "subdomain:tools", "domain:python"

Scoring against a RecallPlan:
    A weighted sum of:
      * exact-term overlap (plan.exact_terms ∩ recall_aliases)
      * entity overlap     (plan.entities ∩ recall_aliases)
      * task-tag overlap   (plan.task_tags ∩ memory tags)
      * evidence-role fit  (role ∈ required > role ∈ allowed)
      * coord-hypothesis fit (lobe in plan coord hypotheses)
      * answerable-question fit (any plan token matches an answerable
        question template)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set
import logging

log = logging.getLogger("omega.memory.answerability_index")


# Single-token evidence role mapping. Stays small + deterministic.
_TYPE_TO_ROLE: Dict[str, str] = {
    "concept": "explanation",
    "fact": "fact",
    "explanation": "explanation",
    "knowledge": "explanation",
    "synthesis": "synthesis",
    "procedure": "procedure",
    "tool": "tool_spec",
    "skill": "skill_record",
    "capability": "capability",
    "identity": "identity_statement",
    "constitution": "identity_statement",
    "persona": "persona_record",
    "background": "background",
    "session": "session",
    "event": "event",
    "milestone": "event",
    "history": "temporal_record",
    "workflow": "workflow",
    "project": "project_record",
    "decision": "decision_record",
    "goal": "decision_record",
    "constraint": "constraint",
    "error": "error_record",
    "incident": "error_record",
    "log": "log",
    "status": "status_record",
    "metric": "status_record",
    "reminder": "reminder",
    "journal": "journal",
    "observation": "journal",
    "context": "context",
    "relationship": "relationship",
}


# Question-shape templates per evidence role. Used to populate
# `answerable_questions` so future intent-overlap scoring can reason
# "this memory answers 'who is X' shape" without re-running the type
# heuristic.
_ROLE_TO_QUESTION_TEMPLATES: Dict[str, List[str]] = {
    "identity_statement": ["who am i", "who are you", "what is your identity", "describe yourself"],
    "persona_record": ["who is", "tell me about", "what role does", "background on"],
    "background": ["tell me about", "background on", "history of"],
    "fact": ["what is", "is it true that", "define"],
    "explanation": ["what is", "explain", "how does", "why does", "what do you know about"],
    "synthesis": ["how does X relate to Y", "summarize", "compare"],
    "tool_spec": ["what tools", "what can you do", "list capabilities", "available tools"],
    "skill_record": ["how do you", "your skills", "what abilities"],
    "capability": ["what can you do", "your capabilities"],
    "procedure": ["how do i", "how to", "steps to", "walk me through"],
    "decision_record": ["why did you choose", "why did you decide", "what was the decision"],
    "constraint": ["why not", "what constraints", "what limits"],
    "event": ["when did", "what happened", "history of"],
    "temporal_record": ["when", "what happened", "history of", "timeline"],
    "session": ["what did we do", "where did we leave off", "last session"],
    "workflow": ["how do we run", "the workflow for", "process for"],
    "project_record": ["where are we on", "project status", "the codebase"],
    "error_record": ["what went wrong", "why did it fail", "error in"],
    "log": ["what happened", "system log", "event log"],
    "status_record": ["how are you doing", "system status", "health check"],
    "reminder": ["remind me to", "did i forget", "reminder for"],
    "journal": ["what did you observe", "system observations"],
    "context": ["what is the context", "background for"],
    "relationship": ["who knows", "who works with"],
    "general": [],
}


_TOKEN_RE = re.compile(r"[a-z0-9_]+")
# Stopwords skipped when tokenising titles/summaries for aliases. Lower
# than the meta tool_selector list because here we want broader recall.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "with",
        "from",
        "by",
        "is",
        "are",
        "was",
        "were",
        "be",
        "as",
        "if",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
    }
)


def _tokens(text: str) -> Set[str]:
    if not text:
        return set()
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 3 and t not in _STOPWORDS}


def _coord_aliases(coord: List[int]) -> List[str]:
    """Build short readable coord hint strings the matcher can OR
    against plan coord hypotheses without re-implementing coord math."""
    if not coord:
        return []
    aliases: List[str] = []
    aliases.append(f"lobe:{coord[0]}")
    if len(coord) >= 2:
        aliases.append(f"sub:{coord[0]}.{coord[1]}")
    return aliases


def derive_answerability_metadata(memory_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Return derived answerability metadata for a memory dict.

    Pure derivation — no I/O, no writes. Stable for a given input.
    """
    title = (memory_dict.get("title") or "").strip()
    summary = (memory_dict.get("summary") or "").strip()
    text = (memory_dict.get("text") or "").strip()
    tags = list(memory_dict.get("tags") or [])
    coord = list(memory_dict.get("coord") or [])
    domain = (memory_dict.get("domain") or "").strip()
    subtopic = (memory_dict.get("subtopic") or "").strip()
    mtype = (memory_dict.get("type") or "unknown").strip()

    evidence_role = _TYPE_TO_ROLE.get(mtype, "general")

    # recall_aliases: lowercased fragments worth matching against plan
    # exact_terms / entities / expanded_terms.
    alias_set: Set[str] = set()
    if title:
        alias_set.add(title.lower())
        alias_set |= _tokens(title)
    if summary:
        alias_set |= _tokens(summary)
    # Body text contributes too but capped — long bodies would otherwise
    # dominate the alias set; keep it semantically meaningful.
    if text:
        alias_set |= _tokens(text[:1500])
    for tag in tags:
        if isinstance(tag, str) and tag:
            alias_set.add(tag.lower())
    if domain:
        alias_set.add(domain.lower())
    if subtopic:
        alias_set.add(subtopic.lower())
    recall_aliases = sorted(alias_set)

    coord_aliases = _coord_aliases(coord)
    if domain:
        coord_aliases.append(f"domain:{domain.lower()}")

    # answerable_questions: take the templates for this role; if the
    # title is short and looks like a name/term, also include the
    # "who/what is <title>" shapes so a plan with the title in
    # exact_terms can light up via the answer-shape scorer.
    answerable_questions = list(_ROLE_TO_QUESTION_TEMPLATES.get(evidence_role, []))
    if title and len(title) < 80:
        title_lower = title.lower()
        # Prepend specific instantiations so they outrank generic shapes
        # when scoring identifies them.
        instantiated = [
            f"what is {title_lower}",
            f"who is {title_lower}",
            f"tell me about {title_lower}",
            f"explain {title_lower}",
        ]
        # Dedupe while preserving order; instantiated first.
        seen: Set[str] = set()
        merged: List[str] = []
        for q in instantiated + answerable_questions:
            if q not in seen:
                seen.add(q)
                merged.append(q)
        answerable_questions = merged

    return {
        "answerable_questions": answerable_questions,
        "recall_aliases": recall_aliases,
        "coord_aliases": coord_aliases,
        "evidence_role": evidence_role,
    }


def _plan_term_set(plan: Any) -> Set[str]:
    """Union of plan tokens worth scoring against an alias set."""
    out: Set[str] = set()
    for attr in ("exact_terms", "expanded_terms", "entities", "task_tags"):
        for t in getattr(plan, attr, None) or []:
            if isinstance(t, str) and t.strip():
                out.add(t.lower().strip())
    return out


def _plan_lobes(plan: Any) -> Set[int]:
    out: Set[int] = set()
    for hyp in getattr(plan, "coord_hypotheses", None) or []:
        try:
            out.add(int(hyp[0]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def answerability_score(
    memory_dict: Dict[str, Any],
    plan: Any,  # RecallPlan
) -> float:
    """Score how well a memory answers the plan's posture.

    Returns a value in [0, 1]. Components:

        +0.40   evidence_role ∈ plan.required_evidence_roles
        +0.20   evidence_role ∈ plan.allowed_evidence_roles (and not required)
        +0.10   per overlap token between plan terms and recall_aliases
                (capped at +0.30)
        +0.10   coord lobe ∈ plan coord hypothesis lobes
        +0.05   any plan term appears in an answerable_questions template
        +0.10   exact title containment in plan original_query (or vice versa)

    Intentionally bounded: this is one signal among many in
    posture_score_evidence; the posture's lane weights and
    suppressions are the dominant levers. answerability_score
    contributes the "this memory's shape matches the question"
    component without overpowering the rest.
    """
    meta = derive_answerability_metadata(memory_dict)
    plan_terms = _plan_term_set(plan)
    plan_lobes = _plan_lobes(plan)

    score = 0.0

    role = meta["evidence_role"]
    required = list(getattr(plan, "required_evidence_roles", []) or [])
    allowed = list(getattr(plan, "allowed_evidence_roles", []) or [])
    if role in required:
        score += 0.40
    elif role in allowed:
        score += 0.20

    if plan_terms:
        alias_set = set(meta["recall_aliases"])
        overlap = plan_terms & alias_set
        score += min(0.30, 0.10 * len(overlap))

    coord = memory_dict.get("coord") or []
    if coord and plan_lobes:
        try:
            if int(coord[0]) in plan_lobes:
                score += 0.10
        except (TypeError, ValueError):
            pass

    if plan_terms and meta["answerable_questions"]:
        for q in meta["answerable_questions"]:
            ql = q.lower()
            if any(t in ql for t in plan_terms):
                score += 0.05
                break

    title = (memory_dict.get("title") or "").lower()
    query = (getattr(plan, "original_query", "") or "").lower()
    if title and query and (title in query or query in title):
        score += 0.10

    return min(1.0, score)
