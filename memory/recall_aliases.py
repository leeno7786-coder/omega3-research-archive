"""
recall_aliases.py
Deterministic term expansion, entity extraction, and task-tag derivation.

No LLM calls. All expansion is rule-based or lookup-based. Public
signatures (`extract_entities`, `expand_recall_terms`, `derive_task_tags`)
are the schema contract — only the internal logic and seed maps may grow.
"""

from __future__ import annotations

import re
from typing import List, Optional, Set
import logging

log = logging.getLogger("omega.memory.recall_aliases")


# Quoted-string regex: capture both "double" and 'single' quoted spans.
_QUOTED_RE = re.compile(r'"([^"]+)"|\'([^\']+)\'')

_PUNCT_STRIP = ".,;:!?()[]{}'\"`"

# Domain hints — used by extract_entities to recognise capitalised
# technical terms like "Python", "Rust", "PyTorch" without forcing the
# operator to quote them. Per-domain entity hints can be added without
# breaking callers.
_DOMAIN_ENTITY_HINTS: dict[str, Set[str]] = {
    "coding": {
        "python",
        "rust",
        "go",
        "typescript",
        "javascript",
        "node",
        "django",
        "flask",
        "fastapi",
        "pytorch",
        "tensorflow",
        "numpy",
        "pandas",
        "sklearn",
        "docker",
        "kubernetes",
    },
    "ai": {
        "transformer",
        "attention",
        "rlhf",
        "embedding",
        "llm",
        "gpt",
        "claude",
        "gemma",
        "anthropic",
        "openai",
    },
    "general": set(),
}


# Synonym/expansion map. Keys are lowercased canonical forms; values
# are alternate words/phrases the operator might use synonymously.
# Both the key and its values get added to the expanded term set when
# any of them is hit (so "py" lights up "python" too).
_SYNONYM_MAP: dict[str, list[str]] = {
    "operator": ["user", "creator", "human", "owner"],
    "ai": ["artificial intelligence", "model", "system"],
    "python": ["py", "cpython"],
    "code": ["source", "script", "program", "implementation"],
    "tool": ["utility", "function", "instrument", "capability"],
    "skill": ["ability", "competency", "capability"],
    "workflow": ["process", "pipeline", "sequence", "procedure"],
    "memory": ["recall", "engram", "record", "retention"],
    "project": ["initiative", "endeavor", "effort", "codebase", "repo"],
    "explain": ["describe", "clarify", "elaborate"],
    "remind": ["reminder", "remember", "recall"],
    "yesterday": ["recently", "past day", "last day"],
    "tools": ["tool", "utilities", "capabilities", "functions"],
    "skills": ["skill", "abilities", "competencies"],
    "decision": ["choice", "selection", "ruling"],
    "error": ["failure", "bug", "exception", "fault"],
    "status": ["state", "health", "condition"],
    # Persona relationship vocabulary must expand in both directions. A
    # profile leaf may state "married" while the operator asks for a spouse,
    # or state "daughters" while the operator asks about children.
    "spouse": ["spouses", "wife", "wives", "husband", "husbands", "married", "marital"],
    "child": ["children", "kid", "kids", "daughter", "daughters", "son", "sons"],
}


# Reverse index for fast hit detection on alias inputs (e.g., "py" -> "python")
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for canonical, aliases in _SYNONYM_MAP.items():
    for alias in aliases:
        _ALIAS_TO_CANONICAL[alias.lower()] = canonical


# Task-tag keyword map. First match wins per tag. Keep tags small +
# stable so downstream lane filtering can rely on them.
_TASK_TAG_KEYWORDS: dict[str, list[str]] = {
    "coding": [
        "python",
        "rust",
        "code",
        "script",
        "function",
        "class",
        "method",
        "library",
        "framework",
        "compile",
        "import",
    ],
    "tools": ["tool", "utility", "wrench", "use the", "call the"],
    "workflows": ["workflow", "pipeline", "process", "sequence", "chain"],
    "temporal": [
        "yesterday",
        "today",
        "session",
        "event",
        "earlier",
        "previously",
        "recently",
        "last time",
        "history",
    ],
    "reminder": ["remind", "forget", "reminder", "notify"],
    "project": ["project", "codebase", "repo", "build", "initiative"],
    "status": ["status", "health", "state", "condition", "how are"],
    "error": ["error", "bug", "fail", "crash", "exception", "broken"],
    "knowledge": ["explain", "what is", "describe", "tell me about", "what do you know"],
    "identity": ["who are you", "who is the operator", "your identity", "yourself", "constitution"],
    "capability": ["what can you", "your tools", "your skills", "what abilities", "what tools"],
}


# Tokens that don't deserve synonym expansion (we still keep them as
# raw tokens so `expanded_terms ≥ exact_terms` invariant holds — but
# we don't blow them up via the synonym map).
_NO_EXPAND_TOKENS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "what",
        "how",
        "why",
        "who",
        "when",
        "where",
        "which",
        "are",
        "was",
        "were",
        "you",
        "your",
        "yours",
        "have",
        "has",
        "had",
        "can",
        "could",
        "should",
        "would",
        "may",
        "might",
        "will",
        "tell",
        "show",
        "give",
        "make",
        "take",
    }
)


def _strip(token: str) -> str:
    return token.strip(_PUNCT_STRIP)


def extract_entities(query: str, domain: Optional[str] = None) -> List[str]:
    """Extract named entities from query text.

    Three sources, in order:
        1. Quoted spans (double or single) — operator's explicit emphasis.
        2. Capitalized words/phrases — proper noun heuristic.
        3. Domain-known terms — case-insensitive hits against the
           per-domain entity hint set.

    Returns a deduplicated list preserving discovery order.
    """
    if not query:
        return []

    entities: List[str] = []

    # 1) Quoted spans (both quote styles)
    for m in _QUOTED_RE.finditer(query):
        captured = m.group(1) or m.group(2)
        if captured and captured.strip():
            entities.append(captured.strip())

    # 2) Capitalized run heuristic. Build runs of consecutive
    # capitalized tokens so multi-word names ("Stanford Encyclopedia")
    # land as one entity. First-word-of-sentence is a known false
    # positive — we accept it; downstream will ignore unmatched terms.
    buf: List[str] = []
    for raw in query.split():
        token = _strip(raw)
        if token and token[:1].isupper() and len(token) > 1:
            buf.append(token)
        else:
            if buf:
                entities.append(" ".join(buf))
                buf = []
    if buf:
        entities.append(" ".join(buf))

    # 3) Domain-known entity hits (case-insensitive). Use the requested
    # domain plus the always-on "general" set.
    domain_key = (domain or "").lower()
    hint_pool: Set[str] = set()
    for key in (domain_key, "general"):
        hint_pool |= _DOMAIN_ENTITY_HINTS.get(key, set())
    if hint_pool:
        q_lower = query.lower()
        for hint in hint_pool:
            if hint in q_lower:
                # Preserve original case from the hint set (lowercase)
                # since we lookup by lower token elsewhere.
                entities.append(hint)

    # Dedup preserving order
    return list(dict.fromkeys(entities))


def expand_recall_terms(query: str, domain: Optional[str] = None) -> List[str]:
    """Expand query into a richer term set (synonyms, alias canonicals,
    domain shortcuts).

    Each token is normalized, then we add:
      - the token itself (if not stopword + length≥3)
      - synonyms via the canonical-key map (if token IS a canonical)
      - the canonical (if token is one of the canonical's aliases)
      - the canonical's other aliases too in that case

    Domain influence: if the operator's `domain` hint matches a
    known per-domain entity pool, those terms are added as well so the
    plan token set covers known per-domain vocabulary even when the
    operator didn't enumerate them.
    """
    if not query:
        return []

    expanded: List[str] = []

    raw_tokens = [_strip(t).lower() for t in query.split()]
    for t in raw_tokens:
        if not t or len(t) <= 2:
            # Mirror the exact_terms filter (`len(t) > 2`) so the
            # invariant `expanded_terms ≥ exact_terms` holds without
            # depending on synonym expansion to make up the count.
            continue
        expanded.append(t)

        # Skip synonym expansion for function-word-ish tokens; we keep
        # them in the raw set above but don't blow them up.
        if t in _NO_EXPAND_TOKENS:
            continue

        # If t is a canonical: add synonyms
        if t in _SYNONYM_MAP:
            expanded.extend(_SYNONYM_MAP[t])

        # If t is an alias: add canonical + canonical's other aliases
        canonical = _ALIAS_TO_CANONICAL.get(t)
        if canonical:
            expanded.append(canonical)
            for alt in _SYNONYM_MAP.get(canonical, []):
                if alt != t:
                    expanded.append(alt)

        # Substring canonical match — operator may type "tooling" and we
        # want "tool" to light up.
        for canonical, _aliases in _SYNONYM_MAP.items():
            if canonical != t and canonical in t:
                expanded.append(canonical)

    # Domain-specific augmentation
    if domain:
        for hint in _DOMAIN_ENTITY_HINTS.get(domain.lower(), set()):
            expanded.append(hint)

    return list(dict.fromkeys(expanded))


def derive_task_tags(query: str, task_scope: Optional[str] = None) -> List[str]:
    """Derive task-relevant tags from query + optional task scope.

    Returns a deduplicated list. task_scope (when provided) is the
    seed; keyword matches in the query layer additional tags.
    """
    tags: List[str] = []

    if task_scope:
        normalized = task_scope.strip().lower()
        if normalized:
            tags.append(normalized)

    if not query:
        return tags

    q = query.lower()
    for tag, keywords in _TASK_TAG_KEYWORDS.items():
        if tag in tags:
            continue
        if any(kw in q for kw in keywords):
            tags.append(tag)

    return tags
