"""
recall_compiler.py
Compile a QuerySpec into a RecallPlan.

This module MUST NOT import from memory.api at module import time to avoid
circular dependencies. It accepts duck-typed query_spec objects.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from memory.recall_posture import (
    POSTURE_CATALOG,
    RecallPosture,
    select_recall_posture,
)
from memory.recall_plan import RecallPlan
from memory.recall_aliases import (
    derive_task_tags,
    expand_recall_terms,
    extract_entities,
)
import logging

log = logging.getLogger("omega.memory.recall_compiler")

# Singular legacy keys from early posture catalog — canonical lane is plural.
_LANE_KEY_ALIASES = {"workflow": "workflows"}
_HISTORICAL_SOURCE_PATTERNS = (
    r"\bwhat\s+did\b",
    r"\bwhat\s+was\b",
    r"\bwhat\s+were\b",
    r"\bprevious(?:ly)?\b",
    r"\bprior\b",
    r"\bhistorical\b",
    r"\bhistory\b",
    r"\bearlier\b",
    r"\byesterday\b",
    r"\blast\s+(?:time|week|month|year|january|february|march|april|may|june|july|august|september|october|november|december)\b",
    r"\bin\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)\b",
)
_CURRENT_SOURCE_PATTERNS = (
    r"\bcurrent(?:ly)?\b",
    r"\blatest\b",
    r"\bnow\b",
    r"\btoday\b",
)


def _canonical_lane_key(lane: str) -> str:
    return _LANE_KEY_ALIASES.get(lane, lane)


def _normalize_lane_weight_map(weights: Dict[str, float]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for lane, weight in (weights or {}).items():
        key = _canonical_lane_key(str(lane))
        out[key] = max(out.get(key, 0.0), float(weight or 0.0))
    return out


def _normalize_query(text: str) -> str:
    """Lowercase, strip extra whitespace, remove redundant punctuation."""
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text.lower()


_TOKEN_EDGE_PUNCT_RE = re.compile(r"^[\W_]+|[\W_]+$")
_TOKEN_POSSESSIVE_RE = re.compile(r"(?:'s|’s)$")


def _clean_query_tokens(query: str) -> List[str]:
    """Query tokens with edge punctuation and the possessive suffix removed.

    Live 2026-09-05: "What is my dog's name?" compiled exact terms
    ``["what", "dog's", "name?"]``. The lanes score records by substring
    term-overlap (matched / total terms), so ``dog's`` and ``name?`` never
    matched "Operator's dog: Biscuit" and still counted in the denominator —
    the persona record scored 0 and Omega denied knowing the dog's name.
    Posture selection keeps the raw normalized query (its rules use shapes
    like "'s name"); only the term sets are built from the clean tokens.
    """
    out: List[str] = []
    for raw in (query or "").split():
        token = _TOKEN_EDGE_PUNCT_RE.sub("", raw)
        token = _TOKEN_POSSESSIVE_RE.sub("", token)
        if token:
            out.append(token)
    return out


def _include_historical_sources(query: str, posture: RecallPosture) -> bool:
    if any(re.search(pattern, query) for pattern in _CURRENT_SOURCE_PATTERNS):
        return False
    if posture.temporal_bias and posture.temporal_bias != "recent":
        return True
    return any(re.search(pattern, query) for pattern in _HISTORICAL_SOURCE_PATTERNS)


def _derive_coord_hypotheses(
    primary_posture: RecallPosture,
    secondary_postures: List[RecallPosture],
    query: str,
) -> List[Tuple[int, int | None, str | None]]:
    """Build coordinate hypotheses from posture lobes and query hints.

    Returns list of (X, Y, domain_hint) tuples.
    """
    hypotheses: List[Tuple[int, int | None, str | None]] = []
    seen = set()

    # Primary posture lobes
    for x in primary_posture.preferred_lobes:
        key = (x, None, None)
        if key not in seen:
            seen.add(key)
            hypotheses.append(key)

    # Secondary posture lobes
    for sp in secondary_postures:
        for x in sp.preferred_lobes:
            key = (x, None, None)
            if key not in seen:
                seen.add(key)
                hypotheses.append(key)

    # Domain hint extraction from query (simple keyword -> lobe mapping).
    # Coding subdomain hints mirror memory.dewey_map.SUBDOMAIN_MAP[8].
    # Use the generic language subdomain unless the query gives a more
    # exact concern signal elsewhere.
    # Order matters: longer / more-specific keywords first so e.g.
    # "typescript" wins before "type" would have matched generically.
    q = query.lower()
    domain_hints = {
        # Knowledge subdomain hints
        "photo": (2, None, "science"),
        "neuroscience": (2, 0, "neuroscience"),
        "math": (2, 1, "mathematics"),
        # Coding (lobe 8) — every Y from SUBDOMAIN_MAP[8]
        "python": (8, 0, "python"),
        "typescript": (8, 8, "typescript"),
        "rust": (8, 12, "rust"),
        "golang": (8, 15, "go"),
        "go-lang": (8, 15, "go"),
        "go pkg": (8, 15, "go"),
        "go module": (8, 15, "go"),
        "cpp": (8, 19, "cpp"),
        "c++": (8, 19, "cpp"),
        "sql": (8, 22, "sql"),
        "shell": (8, 25, "shell"),
        "bash": (8, 25, "shell"),
        "html": (8, 27, "html_css"),
        "css": (8, 27, "html_css"),
        # Generic "code" still maps to lobe 8 with no subdomain pin
        "code": (8, None, "coding"),
        # Skills (lobe 5)
        "tool": (5, None, "tools"),
        "skill": (5, None, "skills"),
        # Workflows (lobe 10)
        "workflow": (10, None, "workflows"),
        # Temporal (lobe 9)
        "session": (9, None, "sessions"),
        "event": (9, None, "events"),
        # Identity / personas
        "identity": (0, None, "identity"),
        "persona": (1, None, "personas"),
    }
    for keyword, hint in domain_hints.items():
        if keyword in q and hint not in seen:
            seen.add(hint)
            hypotheses.append(hint)

    # Special case: bare "go" is too short / ambiguous to keyword-match
    # safely (it appears in "good", "going", etc). Token-level match
    # only — require "go" as a standalone word to add the lobe-8 go
    # hypothesis.
    tokens = q.split()
    if "go" in tokens:
        go_hint = (8, 15, "go")
        if go_hint not in seen:
            seen.add(go_hint)
            hypotheses.append(go_hint)
    # Same caution for bare "c" (single letter) — token-only match
    # avoids matching every word containing 'c'.
    if "c" in tokens:
        c_hint = (8, 17, "c")
        if c_hint not in seen:
            seen.add(c_hint)
            hypotheses.append(c_hint)

    return hypotheses


def _merge_lane_weights(
    primary: RecallPosture,
    secondaries: List[RecallPosture],
) -> Dict[str, float]:
    """Merge lane weights from primary and secondary postures.

    Primary weights win; secondaries contribute at reduced strength.
    """
    merged = _normalize_lane_weight_map(primary.lane_weights)
    for sp in secondaries:
        for lane, weight in _normalize_lane_weight_map(sp.lane_weights).items():
            if lane not in merged:
                merged[lane] = weight * 0.5
            else:
                merged[lane] = max(merged[lane], weight * 0.5)
    return merged


def _apply_posture_lane_bias(
    lane_weights: Dict[str, float],
    posture: RecallPosture,
) -> Dict[str, float]:
    """Enable dedicated lanes from posture bias fields (audit MEDIUM-8)."""
    out = dict(lane_weights)
    if getattr(posture, "strength_bias", None) is not None:
        out["strength"] = max(float(out.get("strength") or 0.0), 1.0)
    if float(out.get("knowledge") or 0.0) >= 1.0:
        out["embedding"] = max(float(out.get("embedding") or 0.0), 0.5)
    return out


# Modality strings that activate the lobe-7 vision recall lane. The robotics
# vision pipeline tags perception queries with one of these.
_VISUAL_MODALITIES = frozenset({"vision", "image", "visual"})


def _apply_modality_lane_bias(
    lane_weights: Dict[str, float],
    modality: str,
) -> Dict[str, float]:
    """Enable the vision lane (lobe 7) for visual-modality queries.

    No text-driven posture weights the vision lane, so a robot's visual query
    would recall nothing from lobe 7. When the query modality is visual
    (vision/image/visual), weight the vision lane as a co-primary lane so past
    visual memories surface alongside whatever the text posture selected.
    Scoped to visual modalities — text recall is unchanged; max() never lowers
    a weight a posture set explicitly.
    """
    out = dict(lane_weights)
    if (modality or "").strip().lower() in _VISUAL_MODALITIES:
        out["vision"] = max(float(out.get("vision") or 0.0), 2.0)
    return out


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def compile_recall_plan(query_spec: Any) -> RecallPlan:
    """Compile a QuerySpec (or duck-typed object) into a RecallPlan.

    Pipeline:
        1. Normalize query text (lowercase, whitespace squeeze).
        2. Select primary posture via deterministic rules.
        3. Resolve secondary postures (max 2) and merge their lane
           weights at half strength so the primary still dominates.
        4. Derive coord hypotheses from posture preferred lobes plus
           query-keyword domain hints.
        5. Extract exact / expanded / entity / task-tag term sets via
           memory.recall_aliases.
        6. Clamp budgets (limit_per_lane, final_limit) to safe ranges,
           tightening when fallback is in play.
        7. Set min_strength threshold for lane gating.
    """
    text = getattr(query_spec, "text", "") or ""
    limit = int(getattr(query_spec, "limit", 10) or 10)
    domain = getattr(query_spec, "domain", None) or ""
    task_scope = getattr(query_spec, "task_scope", None) or ""

    query = _normalize_query(text)

    # Posture selection
    decision = select_recall_posture(query)

    # Resolve primary posture
    if decision.posture == "MULTI_POSTURE":
        primary_name = (
            decision.secondary_postures[0] if decision.secondary_postures else "BACKGROUND_LOOKUP"
        )
        primary_posture = POSTURE_CATALOG.get(primary_name, POSTURE_CATALOG["BACKGROUND_LOOKUP"])
        # Preserve MULTI_POSTURE as the plan posture name while using the
        # first secondary for lane weight derivation.
        primary_posture = RecallPosture(
            name="MULTI_POSTURE",
            confidence=decision.confidence,
            intent_family="mixed",
            answer_shape="mixed",
            preferred_lobes=list(primary_posture.preferred_lobes),
            suppressed_lobes=list(primary_posture.suppressed_lobes),
            preferred_memory_types=list(primary_posture.preferred_memory_types),
            suppressed_memory_types=list(primary_posture.suppressed_memory_types),
            lane_weights=dict(primary_posture.lane_weights),
            required_evidence_roles=list(primary_posture.required_evidence_roles),
            allowed_evidence_roles=list(primary_posture.allowed_evidence_roles),
            temporal_bias=primary_posture.temporal_bias,
            strength_bias=primary_posture.strength_bias,
            notes=["multi-posture fallback"] + list(primary_posture.notes),
        )
    else:
        primary_posture = POSTURE_CATALOG.get(decision.posture)
        if primary_posture is None:
            primary_posture = POSTURE_CATALOG["BACKGROUND_LOOKUP"]

    # Resolve secondary postures
    secondary_names = decision.secondary_postures[:2]
    secondary_postures = [
        POSTURE_CATALOG.get(name, POSTURE_CATALOG["BACKGROUND_LOOKUP"]) for name in secondary_names
    ]

    # Coord hypotheses
    coord_hypotheses = _derive_coord_hypotheses(primary_posture, secondary_postures, query)

    # Terms and entities.
    # `query` is the lowercased / whitespace-normalized form. Term
    # extraction wants that. Entity extraction's proper-noun
    # heuristic depends on capitalization, so we feed it the
    # original `text` (case-preserved). Without this, "Noah" / "Bob"
    # never get picked up because they were lowercased before reaching
    # extract_entities. (Surfaced by Uncle Frank F3.a, 2026-05-08.)
    clean_tokens = _clean_query_tokens(query)
    exact_terms = [t for t in clean_tokens if len(t) > 2]
    expanded_terms = expand_recall_terms(" ".join(clean_tokens), domain=domain)
    entities = extract_entities(text, domain=domain)
    task_tags = derive_task_tags(query, task_scope=task_scope)

    # Lane weights
    lane_weights = _apply_posture_lane_bias(
        _merge_lane_weights(primary_posture, secondary_postures),
        primary_posture,
    )
    # Visual-modality queries (robotics vision) activate the lobe-7 vision
    # lane, which no text-driven posture weights. Scoped — text recall is
    # unchanged.
    lane_weights = _apply_modality_lane_bias(
        lane_weights,
        getattr(query_spec, "modality", "text"),
    )

    # Evidence roles
    required_evidence_roles = list(primary_posture.required_evidence_roles)
    allowed_evidence_roles = list(primary_posture.allowed_evidence_roles)

    # Budgets
    if decision.fallback_used:
        limit_per_lane = _clamp(limit // max(1, len(secondary_names)), 2, 5)
        final_limit = _clamp(limit, 5, 15)
    else:
        limit_per_lane = _clamp(limit, 3, 10)
        final_limit = _clamp(limit, 3, 20)

    # Minimum strength threshold
    min_strength = 0.15

    plan = RecallPlan(
        original_query=query,
        posture=primary_posture,
        secondary_postures=secondary_postures,
        intent=primary_posture.intent_family,
        query_type=primary_posture.answer_shape,
        coord_hypotheses=coord_hypotheses,
        exact_terms=exact_terms,
        expanded_terms=expanded_terms,
        entities=entities,
        task_tags=task_tags,
        lane_weights=lane_weights,
        required_evidence_roles=required_evidence_roles,
        allowed_evidence_roles=allowed_evidence_roles,
        min_strength=min_strength,
        limit_per_lane=limit_per_lane,
        final_limit=final_limit,
        suppressed_lobes=list(primary_posture.suppressed_lobes),
        include_historical_sources=_include_historical_sources(query, primary_posture),
    )
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "compile_recall_plan",
            cycle_id=str(getattr(query_spec, "cycle_id", "") or ""),
            summary=(f"posture={plan.posture.name} preferred={plan.posture.preferred_lobes}"),
            posture_name=plan.posture.name,
            intent=plan.intent,
            preferred_lobes=list(plan.posture.preferred_lobes),
            suppressed_lobes=list(plan.suppressed_lobes),
            min_strength=plan.min_strength,
        )
    except Exception:
        pass
    return plan
