"""
recall_posture.py
Deterministic posture selection for memory recall.

No LLM in the selection path. Rules are keyword/pattern based with
confidence derived from match strength and coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple
import logging

log = logging.getLogger("omega.memory.recall_posture")


# =====================================================================
# Dataclasses
# =====================================================================


@dataclass(frozen=True)
class RecallPosture:
    name: str
    confidence: float
    intent_family: str
    answer_shape: str
    preferred_lobes: list[int]
    suppressed_lobes: list[int]
    preferred_memory_types: list[str]
    suppressed_memory_types: list[str]
    lane_weights: dict[str, float]
    required_evidence_roles: list[str]
    allowed_evidence_roles: list[str]
    temporal_bias: str | None
    strength_bias: str | None
    notes: list[str]
    # When True, suppressed_lobes act as a HARD gate — evidence in
    # those lobes is dropped before arbitration scoring (not just
    # penalized -0.30). This prevents pathological multi-lane
    # convergence (4 lanes × 0.85 + multi_lane_bonus = ~0.95) from
    # beating the suppression penalty (~-0.55) on a "wrong shape"
    # memory. Off by default to keep existing posture behavior; opt
    # in only on postures where misroutes would be especially harmful
    # (IDENTITY answering with a tool record, CAPABILITY answering
    # with a persona record, etc.). Calibrated 2026-05-08 from Uncle
    # Frank J1.a stress finding.
    hard_suppress: bool = False


@dataclass(frozen=True)
class PostureDecision:
    posture: str
    confidence: float
    matched_rules: list[str]
    fallback_used: bool
    secondary_postures: list[str]


# =====================================================================
# Posture Catalog — 14 postures seeded from operator spec
# =====================================================================

POSTURE_CATALOG: Dict[str, RecallPosture] = {
    "IDENTITY_RESOLUTION": RecallPosture(
        name="IDENTITY_RESOLUTION",
        confidence=0.0,
        intent_family="identity",
        answer_shape="self_description",
        preferred_lobes=[0, 1],
        suppressed_lobes=[5, 8, 10, 11],
        preferred_memory_types=["identity", "persona", "constitution", "background"],
        suppressed_memory_types=["tool", "skill", "procedure", "workflow", "code"],
        lane_weights={
            "identity": 2.0,
            "persona": 1.5,
            "knowledge": 0.5,
            "tools": 0.0,
            "skills": 0.0,
            "workflows": 0.0,
            "coding": 0.0,
            "dev": 0.0,
        },
        required_evidence_roles=["identity_statement"],
        allowed_evidence_roles=["identity_statement", "persona_record", "background"],
        temporal_bias=None,
        strength_bias="high",
        notes=[
            "Suppress tool and workflow noise when operator asks 'who are you'.",
            "Prefer constitution and personality records.",
        ],
        hard_suppress=True,
    ),
    "ROLE_BINDING": RecallPosture(
        name="ROLE_BINDING",
        confidence=0.0,
        intent_family="identity",
        answer_shape="role_description",
        preferred_lobes=[0, 1, 4],
        suppressed_lobes=[8, 10, 11],
        preferred_memory_types=["identity", "persona", "role", "relationship"],
        suppressed_memory_types=["tool", "workflow", "code"],
        lane_weights={
            "identity": 1.5,
            "persona": 1.5,
            "situational": 0.8,
            "knowledge": 0.5,
            "tools": 0.0,
            "workflows": 0.0,
            "coding": 0.0,
            "dev": 0.0,
        },
        required_evidence_roles=["persona_record"],
        allowed_evidence_roles=["persona_record", "relationship", "context"],
        temporal_bias=None,
        strength_bias="high",
        notes=["Answer 'what is your role' or 'who do you serve' shapes."],
    ),
    "BACKGROUND_LOOKUP": RecallPosture(
        name="BACKGROUND_LOOKUP",
        confidence=0.0,
        intent_family="identity",
        answer_shape="biographical_snippet",
        preferred_lobes=[1, 2, 4],
        suppressed_lobes=[5, 8, 10],
        preferred_memory_types=["background", "persona", "fact", "context"],
        suppressed_memory_types=["tool", "procedure", "workflow"],
        lane_weights={
            "persona": 1.5,
            "knowledge": 1.0,
            "situational": 1.0,
            "tools": 0.0,
            "workflows": 0.0,
        },
        required_evidence_roles=["background"],
        allowed_evidence_roles=["background", "persona_record", "fact", "context"],
        temporal_bias=None,
        strength_bias=None,
        notes=["Short background lookups — 'tell me about X' where X is a person or entity."],
    ),
    "TASK_CONTINUITY": RecallPosture(
        name="TASK_CONTINUITY",
        confidence=0.0,
        intent_family="task",
        answer_shape="progress_update",
        preferred_lobes=[4, 9, 10, 11],
        suppressed_lobes=[0, 6],
        preferred_memory_types=["session", "workflow", "task", "project", "event"],
        suppressed_memory_types=["identity", "journal"],
        lane_weights={
            "workflows": 2.0,
            "project": 1.5,
            "temporal": 1.5,
            "situational": 1.0,
            "coding": 1.0,
            "dev": 1.0,
            "identity": 0.0,
            "journal": 0.0,
        },
        required_evidence_roles=["task_record"],
        allowed_evidence_roles=["task_record", "session", "event", "workflow"],
        temporal_bias="recent",
        strength_bias=None,
        notes=["'Where did we leave off' — boost temporal and workflow lanes."],
    ),
    "REMINDER": RecallPosture(
        name="REMINDER",
        confidence=0.0,
        intent_family="task",
        answer_shape="reminder_item",
        preferred_lobes=[4, 9, 10],
        suppressed_lobes=[0, 6],
        preferred_memory_types=["reminder", "event", "task", "session"],
        suppressed_memory_types=["identity", "journal"],
        lane_weights={
            "temporal": 2.0,
            "situational": 1.5,
            "workflows": 1.0,
            "identity": 0.0,
            "journal": 0.0,
        },
        required_evidence_roles=["reminder"],
        allowed_evidence_roles=["reminder", "event", "task_record", "session"],
        temporal_bias="recent",
        strength_bias=None,
        notes=["Explicit reminder queries — 'remind me to...' or 'did I forget...'"],
    ),
    "CAPABILITY_ENUMERATION": RecallPosture(
        name="CAPABILITY_ENUMERATION",
        confidence=0.0,
        intent_family="capability",
        answer_shape="enumerated_list",
        preferred_lobes=[5, 8],
        suppressed_lobes=[0, 1, 6],
        preferred_memory_types=["tool", "skill", "procedure", "capability", "code"],
        suppressed_memory_types=["identity", "persona", "journal"],
        lane_weights={
            "tools": 2.0,
            "skills": 2.0,
            "coding": 1.5,
            "knowledge": 0.5,
            "identity": 0.0,
            "persona": 0.0,
            "journal": 0.0,
        },
        required_evidence_roles=["tool_spec"],
        allowed_evidence_roles=["tool_spec", "skill_record", "procedure", "capability"],
        temporal_bias=None,
        strength_bias=None,
        notes=["'What can you do' — surface tools, skills, coding abilities."],
        hard_suppress=True,
    ),
    "KNOWLEDGE_EXPLANATION": RecallPosture(
        name="KNOWLEDGE_EXPLANATION",
        confidence=0.0,
        intent_family="knowledge",
        answer_shape="explanation",
        preferred_lobes=[2, 3],
        suppressed_lobes=[0, 1, 5, 8, 10],
        preferred_memory_types=["concept", "fact", "knowledge", "explanation"],
        suppressed_memory_types=["identity", "persona", "tool", "workflow", "code"],
        lane_weights={
            "knowledge": 2.0,
            "reasoning": 1.0,
            "identity": 0.0,
            "persona": 0.0,
            "tools": 0.0,
            "workflows": 0.0,
            "coding": 0.0,
        },
        required_evidence_roles=["explanation"],
        allowed_evidence_roles=["explanation", "fact", "concept", "synthesis"],
        temporal_bias=None,
        strength_bias="high",
        notes=["'Explain X' — high-confidence knowledge path, suppress identity noise."],
    ),
    "PROCEDURAL_HOW": RecallPosture(
        name="PROCEDURAL_HOW",
        confidence=0.0,
        intent_family="procedural",
        answer_shape="step_by_step",
        preferred_lobes=[5, 8, 10],
        suppressed_lobes=[0, 1, 6],
        preferred_memory_types=["procedure", "tool", "skill", "workflow", "code"],
        suppressed_memory_types=["identity", "persona", "journal"],
        lane_weights={
            "skills": 2.0,
            "tools": 1.5,
            "workflows": 1.5,
            "coding": 1.0,
            "knowledge": 0.5,
            "identity": 0.0,
            "journal": 0.0,
        },
        required_evidence_roles=["procedure"],
        allowed_evidence_roles=["procedure", "tool_spec", "skill_record", "workflow"],
        temporal_bias=None,
        strength_bias=None,
        notes=["'How do I...' — procedural memories, tool chains, coding patterns."],
    ),
    "ARCHITECTURAL_WHY": RecallPosture(
        name="ARCHITECTURAL_WHY",
        confidence=0.0,
        intent_family="architectural",
        answer_shape="design_rationale",
        preferred_lobes=[3, 10, 11],
        suppressed_lobes=[1, 6],
        preferred_memory_types=["decision", "design", "rationale", "workflow"],
        suppressed_memory_types=["persona", "journal"],
        lane_weights={
            "reasoning": 2.0,
            "workflows": 1.5,
            "dev": 1.0,
            "knowledge": 0.5,
            "persona": 0.0,
            "journal": 0.0,
        },
        required_evidence_roles=["design_rationale"],
        allowed_evidence_roles=["design_rationale", "decision", "constraint", "workflow"],
        temporal_bias=None,
        strength_bias="high",
        notes=["'Why did you design X this way' — reasoning and decision records."],
    ),
    "TEMPORAL_RECALL": RecallPosture(
        name="TEMPORAL_RECALL",
        confidence=0.0,
        intent_family="temporal",
        answer_shape="chronological_summary",
        preferred_lobes=[9, 4, 6],
        suppressed_lobes=[0],
        preferred_memory_types=["event", "session", "milestone", "history", "journal"],
        suppressed_memory_types=["identity"],
        lane_weights={
            "temporal": 2.0,
            "situational": 1.0,
            "journal": 0.8,
            "identity": 0.0,
        },
        required_evidence_roles=["temporal_record"],
        allowed_evidence_roles=["temporal_record", "event", "session", "milestone"],
        temporal_bias="recent_first",
        strength_bias=None,
        notes=["'What happened yesterday' — temporal ordering, recent first."],
    ),
    "DECISION_RECALL": RecallPosture(
        name="DECISION_RECALL",
        confidence=0.0,
        intent_family="decision",
        answer_shape="decision_summary",
        preferred_lobes=[3, 4, 9],
        suppressed_lobes=[0, 6],
        preferred_memory_types=["decision", "goal", "constraint", "event"],
        suppressed_memory_types=["identity", "journal"],
        lane_weights={
            "reasoning": 2.0,
            "situational": 1.0,
            "temporal": 0.8,
            "identity": 0.0,
            "journal": 0.0,
        },
        required_evidence_roles=["decision_record"],
        allowed_evidence_roles=["decision_record", "goal", "constraint", "context"],
        temporal_bias=None,
        strength_bias="high",
        notes=["'Why did you choose X' — decision provenance and constraints."],
    ),
    "ERROR_RECALL": RecallPosture(
        name="ERROR_RECALL",
        confidence=0.0,
        intent_family="error",
        answer_shape="error_analysis",
        preferred_lobes=[4, 9, 11],
        suppressed_lobes=[0, 1],
        preferred_memory_types=["error", "failure", "incident", "log"],
        suppressed_memory_types=["identity", "persona"],
        lane_weights={
            "situational": 2.0,
            "dev": 1.5,
            "temporal": 1.0,
            "identity": 0.0,
            "persona": 0.0,
        },
        required_evidence_roles=["error_record"],
        allowed_evidence_roles=["error_record", "incident", "log", "context"],
        temporal_bias="recent_first",
        strength_bias=None,
        notes=["'What went wrong' — error and incident records."],
    ),
    "PROJECT_CONTEXT": RecallPosture(
        name="PROJECT_CONTEXT",
        confidence=0.0,
        intent_family="project",
        answer_shape="project_snapshot",
        preferred_lobes=[4, 10, 11, 8],
        suppressed_lobes=[0, 1, 6],
        preferred_memory_types=["project", "workflow", "artifact", "code", "task"],
        suppressed_memory_types=["identity", "persona", "journal"],
        lane_weights={
            "project": 2.0,
            "workflows": 1.5,
            "dev": 1.5,
            "coding": 1.0,
            "situational": 0.8,
            "identity": 0.0,
            "journal": 0.0,
        },
        required_evidence_roles=["project_record"],
        allowed_evidence_roles=["project_record", "artifact", "workflow", "task_record"],
        temporal_bias="recent",
        strength_bias=None,
        notes=["'Where are we on the project' — project, dev, workflow context."],
    ),
    "SYSTEM_STATUS": RecallPosture(
        name="SYSTEM_STATUS",
        confidence=0.0,
        intent_family="status",
        answer_shape="status_report",
        preferred_lobes=[4, 6, 9, 11],
        suppressed_lobes=[0, 1],
        preferred_memory_types=["status", "log", "event", "journal", "metric"],
        suppressed_memory_types=["identity", "persona"],
        lane_weights={
            "situational": 2.0,
            "journal": 1.5,
            "temporal": 1.0,
            "dev": 1.0,
            "identity": 0.0,
            "persona": 0.0,
        },
        required_evidence_roles=["status_record"],
        allowed_evidence_roles=["status_record", "log", "event", "metric"],
        temporal_bias="recent_first",
        strength_bias=None,
        notes=["'How are you doing' or system health — situational, journal, temporal."],
    ),
}


# =====================================================================
# Deterministic selection rules
# =====================================================================

# =====================================================================
# Deterministic code/workflow gate (recall-side convergence fix)
# =====================================================================
# The 12-lobe restructure added lobe 8 (coding) + lobe 10 (workflows)
# to INGEST (dewey_map + the 6-SetFit classifier route them correctly)
# but the recall posture *selector* below was never taught them: a
# coding/workflows query matched no keyword rule and fell through to
# the identity-family BACKGROUND_LOOKUP fallback, whose suppressed_lobes
# include 8 and 10 — so coding/workflows memories were write-perfect but
# recall-suppressed. This deterministic gate (operator-directed: "add
# deterministic script gating for code and its subdirectories") routes
# any code- or workflow-domain query to PROCEDURAL_HOW (preferred lobes
# [5, 8, 10]) so recall converges with ingest. No classifier/LLM — pure
# deterministic substring rules, language + workflow vocabulary DERIVED
# from dewey_map (single source of truth, so it cannot drift), plus a
# tight hand-audited lexicon of unambiguous code signals chosen to be
# zero-false-positive against identity/knowledge/situational recall
# (the already-perfect paths must NOT regress).


def _derive_code_workflow_patterns() -> List[str]:
    pats: set[str] = set()
    try:
        from memory.dewey_map import SUBDOMAIN_MAP
    except Exception:
        SUBDOMAIN_MAP = {}
    # Language names: prefix of every SUBDOMAIN_MAP[8] dir, expanded to
    # the natural-language forms users actually type. Bare ambiguous
    # stems ("c", "go") are deliberately NOT emitted — they would match
    # ordinary English and steal non-code recall.
    _LANG_FORMS = {
        "python": ["python"],
        "typescript": ["typescript", "javascript"],
        "rust": ["rust"],
        "go": ["golang"],
        "c": ["c programming", "embedded c"],
        "cpp": ["c++", "cpp"],
        "sql": ["sql"],
        "shell": ["bash", "powershell", "shell script"],
        "html": ["html", "css"],
    }
    for (lobe, _y), dirname in SUBDOMAIN_MAP.items():
        if lobe == 8:
            prefix = str(dirname).split("_", 1)[0]
            for form in _LANG_FORMS.get(prefix, []):
                pats.add(form)
        elif lobe == 10:
            # Workflow-kind dirs -> the phrase as a user would say it.
            pats.add(str(dirname).replace("_", " "))
    # Unambiguous code signals (NOT in dewey dir names verbatim — this
    # is how users phrase code recall in natural language). Hand-audited
    # for ~zero false positives against the perfect non-code paths.
    pats.update(
        [
            "asyncio",
            "coroutine",
            "async def",
            "docker",
            "kubernetes",
            "grpc",
            "ci/cd",
            "cicd",
            "regex",
            "compiler",
            "traceback",
            "stack trace",
            "segfault",
            "null pointer",
            "refactor",
            "unit test",
            "code review",
            "pull request",
            "merge conflict",
            "git ",
            "type hint",
            "package manager",
            "api endpoint",
        ]
    )
    return sorted(pats)


_CODE_WORKFLOW_PATTERNS: List[str] = _derive_code_workflow_patterns()

# Each rule is (posture_name, weight, patterns)
# Patterns are lowercase keyword/phrases; match count contributes to score.
_SELECTION_RULES: List[Tuple[str, float, List[str]]] = [
    # IDENTITY_RESOLUTION
    (
        "IDENTITY_RESOLUTION",
        1.0,
        [
            "who are you",
            "who is the operator",
            "who am i",
            "what is your name",
            "what are you",
            "describe yourself",
            "tell me about yourself",
            "your identity",
            "your purpose",
            "your constitution",
            "operator",
        ],
    ),
    # Extra rule to strongly weight "who is the operator" toward identity
    (
        "IDENTITY_RESOLUTION",
        1.2,
        [
            "who is the operator",
            "who are you",
        ],
    ),
    # High-weight specific-phrase rule: questions about Omega's
    # name/purpose/origin score harder for IDENTITY than for the
    # generic KNOWLEDGE "what is" pattern. Weight 1.5 lets these win
    # over "what is" + disambiguation boost in KNOWLEDGE_EXPLANATION.
    # Calibrated 2026-05-08 from Uncle Frank disambiguation fails.
    (
        "IDENTITY_RESOLUTION",
        1.5,
        [
            "what is your name",
            "what is your purpose",
            "what is your identity",
            "what is your constitution",
            "who created you",
            "who built you",
            "who designed you",
            "who made you",
        ],
    ),
    # ROLE_BINDING
    (
        "ROLE_BINDING",
        1.0,
        [
            "your role",
            "what do you do",
            "what is your job",
            "who do you serve",
            "why were you created",
            "your function",
        ],
    ),
    (
        "ROLE_BINDING",
        0.4,
        [
            "who is the operator",
            "who are you",
        ],
    ),
    # Persona relationship queries are role bindings between people. Keep
    # spouse-state and child terms in separate short rules so one precise
    # relationship word clears the confidence threshold without turning all
    # family vocabulary into an undifferentiated fallback lookup.
    (
        "ROLE_BINDING",
        1.0,
        [
            "spouse",
            "spouses",
            "wife",
            "wives",
            "husband",
            "husbands",
            "married",
            "marital",
        ],
    ),
    (
        "ROLE_BINDING",
        1.0,
        [
            "child",
            "children",
            "kid",
            "kids",
            "daughter",
            "daughters",
            "son",
            "sons",
        ],
    ),
    # BACKGROUND_LOOKUP
    (
        "BACKGROUND_LOOKUP",
        0.8,
        [
            "tell me about",
            "what do you know about",
            "background on",
            "who is",
            "information about",
            "details about",
        ],
    ),
    # TASK_CONTINUITY
    (
        "TASK_CONTINUITY",
        1.0,
        [
            "remind me about the",
            "where did we leave off",
            "continue",
            "pick up where",
            "what were we doing",
            "resume",
            "remind me about",
            "python work",
            "the work",
            "our task",
        ],
    ),
    # REMINDER
    (
        "REMINDER",
        1.0,
        [
            "remind me to",
            "did i forget",
            "don't let me forget",
            "set a reminder",
            "reminder for",
        ],
    ),
    # CAPABILITY_ENUMERATION
    (
        "CAPABILITY_ENUMERATION",
        1.0,
        [
            "what tools can you use",
            "what can you do",
            "your capabilities",
            "your skills",
            "what functions do you have",
            "list your tools",
            "what are you able to",
            "tools available",
        ],
    ),
    # BACKGROUND_LOOKUP — the operator asking about THEIR OWN world.
    # Live 2026-09-05: "What is my favorite editor?" / "What is my dog's
    # name?" scored only the generic "what is" (KNOWLEDGE_EXPLANATION,
    # persona lane weight 0.0, lobes 2/3), so lobe 1 was never searched and
    # the persona records on disk were never grounded — Omega answered "the
    # verified data does not contain that information" about facts it held.
    # The possessive "my" + a preference / relation / name shape is a
    # persona-lane lookup. Rules are kept SHORT on purpose: the scorer
    # divides by len(patterns) * 0.3, so one hit in a 3-5 pattern rule
    # clears the 0.30 threshold and beats "what is" (0.34) — a long list
    # would not.
    (
        "BACKGROUND_LOOKUP",
        1.0,
        ["my favorite", "my favourite", "my preferred"],
    ),
    (
        "BACKGROUND_LOOKUP",
        1.2,
        ["my dog", "my cat", "my pet", "my wife", "my husband"],
    ),
    (
        "BACKGROUND_LOOKUP",
        1.2,
        ["my son", "my daughter", "my kids", "my family", "my partner"],
    ),
    (
        "BACKGROUND_LOOKUP",
        1.2,
        ["my mom", "my mother", "my dad", "my father", "my brother", "my sister"],
    ),
    (
        "BACKGROUND_LOOKUP",
        1.2,
        ["my name", "my birthday", "about me", "do you remember my", "what do you know about me"],
    ),
    (
        "BACKGROUND_LOOKUP",
        1.2,
        ["what is my", "what's my", "who is my", "who's my", "'s name"],
    ),
    (
        "BACKGROUND_LOOKUP",
        1.2,
        ["do i like", "do i prefer", "do i enjoy", "i like best", "i prefer most"],
    ),
    # KNOWLEDGE_EXPLANATION
    (
        "KNOWLEDGE_EXPLANATION",
        1.0,
        [
            "what is",
            "how does",
            "explain",
            "why does",
            "what are",
            "describe how",
            "tell me how",
            "what do you know about",
            "explain how",
            "what does",
            "photosynthesis",
            "quantum",
            "neuroscience",
            "mathematics",
        ],
    ),
    # PROCEDURAL_HOW
    (
        "PROCEDURAL_HOW",
        1.0,
        [
            "how do i",
            "how to",
            "how can i",
            "steps to",
            "procedure for",
            "walk me through",
            "guide me through",
        ],
    ),
    # PROCEDURAL_HOW — deterministic code/workflow gate. Routes any
    # code- or workflow-domain query to PROCEDURAL_HOW (preferred lobes
    # [5, 8, 10]) so recall of the NEW lobes 8/10 converges with ingest.
    # Weight 4.0 is intentionally authoritative: _score_query_against_
    # posture divides by len(patterns)*0.3, so with this dewey-derived
    # list (~40 patterns) a single strong code token must still clear
    # _LOW_CONFIDENCE_THRESHOLD (0.30) outright — 1 match -> ~0.37,
    # saturating upward. This is a deterministic GATE, not a soft hint,
    # by design. Non-code queries match zero patterns -> score 0
    # regardless of weight, so a high weight cannot over-trigger.
    ("PROCEDURAL_HOW", 5.0, _CODE_WORKFLOW_PATTERNS),
    # ARCHITECTURAL_WHY
    (
        "ARCHITECTURAL_WHY",
        1.0,
        [
            "why did you design",
            "why was it built",
            "architectural reason",
            "design decision",
            "why this approach",
            "rationale for",
        ],
    ),
    # TEMPORAL_RECALL
    (
        "TEMPORAL_RECALL",
        1.0,
        [
            "what happened",
            "what did we do",
            "yesterday",
            "last time",
            "earlier",
            "previously",
            "recently",
            "last session",
            "when did",
            "history of",
        ],
    ),
    # TEMPORAL_RECALL — recency phrasings ("what did you learn last", "latest",
    # "most recent"). Kept as a SEPARATE rule (not merged into the list above): the
    # score is matched/(len(patterns)*0.3), so a short list scores each match high,
    # while merging these in would enlarge len() and drop already-working phrasings
    # like "what happened" below the lock threshold into MULTI_POSTURE. scores[]
    # takes the max across a posture's rules, so this only ever ADDS coverage.
    # ("most recent" also matches "most recently"; "learn last"/"learned last" cover
    # both tenses of the operator's canonical "what did you learn last".)
    (
        "TEMPORAL_RECALL",
        1.0,
        [
            "latest",
            "most recent",
            "lately",
            "last thing",
            "learn last",
            "learned last",
        ],
    ),
    # DECISION_RECALL
    (
        "DECISION_RECALL",
        1.0,
        [
            "why did you choose",
            "why did you decide",
            "what was the decision",
            "decision behind",
            "why that option",
        ],
    ),
    # ERROR_RECALL
    (
        "ERROR_RECALL",
        1.0,
        [
            "what went wrong",
            "error",
            "failure",
            "bug",
            "crash",
            "why did it fail",
            "exception",
            "stack trace",
        ],
    ),
    # PROJECT_CONTEXT
    (
        "PROJECT_CONTEXT",
        1.0,
        [
            "where are we on the project",
            "project status",
            "current project",
            "our project",
            "the codebase",
            "repo status",
            "build status",
        ],
    ),
    # SYSTEM_STATUS
    (
        "SYSTEM_STATUS",
        1.0,
        [
            "how are you doing",
            "system status",
            "are you ok",
            "health check",
            "status report",
            "how do you feel",
        ],
    ),
]

# Boost keywords that disambiguate between similar postures
_DISAMBIGUATION_BOOSTS: Dict[str, List[str]] = {
    "IDENTITY_RESOLUTION": ["who", "identity", "yourself"],
    "ROLE_BINDING": ["role", "job", "function", "serve"],
    "TASK_CONTINUITY": ["continue", "resume", "leave off", "were we doing"],
    "REMINDER": ["remind me to", "forget"],
    "CAPABILITY_ENUMERATION": ["tools", "capabilities", "skills", "functions"],
    "KNOWLEDGE_EXPLANATION": ["explain", "what is", "how does", "why does"],
    "PROCEDURAL_HOW": ["how do i", "how to", "steps", "walk me through"],
    "TEMPORAL_RECALL": ["yesterday", "last time", "recently", "history"],
    "PROJECT_CONTEXT": ["project", "codebase", "repo", "build"],
    "SYSTEM_STATUS": ["status", "health", "how are you"],
}

# Postures that are reasonable fallbacks for vague queries
_FALLBACK_POSTURES = ["ROLE_BINDING", "BACKGROUND_LOOKUP", "KNOWLEDGE_EXPLANATION"]

# Confidence below which posture selection fans out to MULTI_POSTURE.
# Calibrated 2026-05-08 (Uncle Frank stress pass): 0.45 was too tight —
# queries with deliberate but partial keyword coverage like
# "who am I", "describe yourself", "what was the last error" scored
# in the 0.30..0.42 band and got pushed to MULTI_POSTURE despite
# unambiguous intent. 0.30 keeps the catch-all behavior for genuinely
# vague single-token queries (which score < 0.30 due to single-word
# penalty + low pattern coverage) while letting clear questions lock
# onto their natural posture.
_LOW_CONFIDENCE_THRESHOLD = 0.30


def _score_query_against_posture(
    query: str, posture_name: str, weight: float, patterns: List[str]
) -> float:
    """Return a match score in [0, 1] for query against a rule."""
    q = query.lower()
    matched = 0
    for p in patterns:
        if p in q:
            matched += 1
    if matched == 0:
        return 0.0
    base = min(1.0, matched / max(1, len(patterns) * 0.3))
    score = base * weight

    # Disambiguation boost (capped at +0.20 total)
    boosts = _DISAMBIGUATION_BOOSTS.get(posture_name, [])
    boost_hits = sum(1 for b in boosts if b in q)
    if boost_hits:
        score = min(1.0, score + 0.10 * boost_hits)

    # Single-word query penalty: vague queries get less confidence
    if len(q.split()) == 1:
        score = score * 0.85

    return score


def select_recall_posture(query: str) -> PostureDecision:
    """Select the best recall posture for a query using deterministic rules.

    No LLM is used. Confidence is derived from keyword/pattern coverage.
    If confidence < 0.45, returns a multi-posture fallback.
    """
    if not query or not isinstance(query, str):
        return PostureDecision(
            posture="BACKGROUND_LOOKUP",
            confidence=0.0,
            matched_rules=["empty_query_fallback"],
            fallback_used=True,
            secondary_postures=[],
        )

    scores: Dict[str, float] = {}
    matched_rules: Dict[str, List[str]] = {}

    for posture_name, weight, patterns in _SELECTION_RULES:
        score = _score_query_against_posture(query, posture_name, weight, patterns)
        if score > 0:
            scores[posture_name] = max(scores.get(posture_name, 0.0), score)
            matched_rules.setdefault(posture_name, []).append(
                f"rule_{posture_name}_score_{score:.2f}"
            )

    if not scores:
        # No rules matched — fallback to background lookup
        return PostureDecision(
            posture="BACKGROUND_LOOKUP",
            confidence=0.3,
            matched_rules=["no_rule_matched_fallback"],
            fallback_used=True,
            secondary_postures=["ROLE_BINDING", "KNOWLEDGE_EXPLANATION"],
        )

    best_name = max(scores, key=lambda k: scores[k])
    best_score = scores[best_name]

    # Build secondary list from other postures above threshold
    secondary = [
        name
        for name, sc in sorted(scores.items(), key=lambda x: -x[1])
        if name != best_name and sc >= 0.2
    ]

    if best_score < _LOW_CONFIDENCE_THRESHOLD:
        # Low confidence — fan out to multiple postures with reduced budgets
        fallback_postures = [best_name] + secondary[:2]
        if len(fallback_postures) < 2:
            fallback_postures += [p for p in _FALLBACK_POSTURES if p not in fallback_postures][:2]
        fallback_postures = fallback_postures[:3]
        return PostureDecision(
            posture="MULTI_POSTURE",
            confidence=best_score,
            matched_rules=matched_rules.get(best_name, ["low_confidence_fallback"]),
            fallback_used=True,
            secondary_postures=fallback_postures,
        )

    return PostureDecision(
        posture=best_name,
        confidence=min(1.0, best_score),
        matched_rules=matched_rules.get(best_name, []),
        fallback_used=False,
        secondary_postures=secondary[:2],
    )
