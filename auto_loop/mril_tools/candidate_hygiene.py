"""Final query-aware cleanup for verified recall evidence.

Individual remember tools verify provenance and perform lane-local relevance
checks.  A multi-lobe recall still needs one final boundary that can compare
candidates across tools, remove semantic duplicates, and keep whole-lobe
identity/persona grounding from leaking unrelated anchors into the answer.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Sequence


FINAL_RECALL_CANDIDATE_LIMIT = 8

_WORD_RE = re.compile(r"[a-z0-9]+")
_POSSESSIVE_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]+)['’]s\b")
_UNAPOSTROPHIZED_PROPER_POSSESSIVE_RE = re.compile(r"\b([A-Z][A-Za-z0-9_-]{2,})s\b")

_QUERY_STOPWORDS = frozenset(
    {
        "a",
        "all",
        "an",
        "and",
        "any",
        "are",
        "about",
        "can",
        "could",
        "do",
        "does",
        "did",
        "for",
        "from",
        "give",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "know",
        "many",
        "me",
        "of",
        "on",
        "please",
        "recall",
        "remember",
        "show",
        "tell",
        "that",
        "the",
        "their",
        "them",
        "they",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "would",
        "you",
    }
)

_RELATION_CONCEPTS = {
    "friend": frozenset({"friend", "friends", "friendship"}),
    "partner": frozenset({"partner", "partners", "partnership"}),
    "child": frozenset(
        {
            "child",
            "children",
            "kid",
            "kids",
            "daughter",
            "daughters",
            "son",
            "sons",
        }
    ),
    "spouse": frozenset(
        {
            "spouse",
            "spouses",
            "wife",
            "wives",
            "husband",
            "husbands",
            "married",
            "marital",
        }
    ),
    "parent": frozenset({"parent", "parents", "mother", "mothers", "father", "fathers"}),
    "sibling": frozenset({"sibling", "siblings", "brother", "brothers", "sister", "sisters"}),
    "organization_role": frozenset(
        {
            "business",
            "cofounder",
            "cofounders",
            "coowner",
            "coowners",
            "employee",
            "employees",
            "employer",
            "founder",
            "founders",
            "job",
            "occupation",
            "own",
            "owned",
            "owner",
            "owners",
            "ownership",
            "owns",
            "role",
            "roles",
            "work",
            "works",
            "workplace",
        }
    ),
}
_RELATION_WORDS = frozenset().union(*_RELATION_CONCEPTS.values())
_RELATION_QUALIFIERS = frozenset(
    {
        "adopted",
        "best",
        "biological",
        "business",
        "close",
        "closest",
        "current",
        "ex",
        "former",
        "oldest",
        "romantic",
        "step",
        "youngest",
    }
)
_RELATION_EDGE_NOISE = frozenset({"operator", "relation", "relationship"})
_SUBJECT_QUERY_RE = re.compile(
    r"\b(?:remember|recall|know|tell\s+me|what(?:\s+all)?\s+do\s+you\s+remember)\b"
    r".*\babout\b|^\s*who\s+is\b",
    re.IGNORECASE,
)
_ORGANIZATION_ROLE_QUERY_RE = re.compile(
    r"\bwhat\s+(?:does|do)\b.{0,120}\b(?:do|at|for)\b|"
    r"\b(?:work|works|job|role|owner|founder)\b.{0,80}\b(?:at|for|of)\b",
    re.IGNORECASE,
)
_IDENTITY_QUERY_RE = re.compile(
    r"\b(?:who|what)\s+(?:are|is)\s+(?:you|omega(?!\s+ai\b)|guardian)\b|"
    r"\b(?:your|omega['’]?s)\s+(?:identity|creator|origin|origins|personality|traits?|model)\b|"
    r"\b(?:created|built|made)\s+(?:you|omega(?!\s+ai\b))\b|"
    r"\b(?:about|remember|know)\s+(?:yourself|omega(?!\s+ai\b)|guardian)\b",
    re.IGNORECASE,
)


#: Why a retrieved candidate was not admitted. Every value below is a real branch of this
#: function, not a taxonomy invented for the report: a reader can follow each one to the line
#: that produced it. WO-05 step 2 requires the reason, because "it was dropped" is not
#: evidence anyone downstream can act on.
EXCLUSION_REASONS: tuple[str, ...] = (
    "not_query_responsive",
    "duplicate_memory_id",
    "duplicate_semantics",
    "duplicate_content",
    "over_candidate_cap",
)


def _candidate_identity(
    evidence: Mapping[str, Any], *, tool: Any, lobe: Any, order: int
) -> Dict[str, Any]:
    """How one candidate is named in the exposure record.

    ``memory_id`` is the identity that matters, but it is not always present, so the
    normalized content is hashed alongside it. Without that, two id-less candidates would be
    indistinguishable and the partition below could not be checked.
    """
    text = _normalized_text(evidence.get("remembered_as") or evidence.get("title") or "")
    return {
        "memory_id": str(evidence.get("memory_id") or "").strip(),
        "tool": str(tool or ""),
        "lobe": lobe,
        "retrieval_order": order,
        "content_sha256": _sha256_text(text),
    }


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def finalize_recall_results(
    results: Sequence[Mapping[str, Any]],
    query: str,
    *,
    max_candidates: int = FINAL_RECALL_CANDIDATE_LIMIT,
    preserve_identity: bool = False,
) -> List[Dict[str, Any]]:
    """Return immutable-style result copies with one clean evidence set.

    Provenance verdicts remain intact.  Only the answer-facing ``evidence`` is
    filtered; whole-lobe but nonresponsive facts no longer count as recalled
    hits or enter the model prompt.

    Kept at its original shape because it has many callers; the exposure record lives on
    :func:`finalize_recall_results_with_exposure`, which this delegates to. What is admitted
    is decided in exactly one place either way.
    """
    finalized, _exposure = finalize_recall_results_with_exposure(
        results, query, max_candidates=max_candidates, preserve_identity=preserve_identity
    )
    return finalized


def finalize_recall_results_with_exposure(
    results: Sequence[Mapping[str, Any]],
    query: str,
    *,
    max_candidates: int = FINAL_RECALL_CANDIDATE_LIMIT,
    preserve_identity: bool = False,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The finalizer, plus an account of what it did NOT admit and why (WO-05 step 2).

    The admission decision is unchanged — this returns the same ``finalized`` list the
    plain finalizer always did. What is new is that the losses are named. Before this, the
    only record was ``candidate_hygiene.dropped``, a COUNT, so an excluded candidate was
    indistinguishable from one that was never retrieved, and nothing downstream could answer
    "was B supplied to the model?" — the question that matters when an answer looks like it
    used something it never saw.

    ``retrieved`` is every candidate the tools returned, in retrieval order. ``admitted`` is
    exactly the evidence that survived into ``finalized``. ``excluded`` is the remainder,
    each with a reason from :data:`EXCLUSION_REASONS`. The three partition: every retrieved
    candidate is accounted for on one side or the other.
    """
    limit = max(1, int(max_candidates))
    query_profile = _query_profile(query)
    query_profile["preserve_identity"] = bool(preserve_identity)
    finalized: List[Dict[str, Any]] = []
    dropped_counts: List[int] = []
    responsive: List[tuple[int, int, Dict[str, Any]]] = []
    order = 0
    retrieved: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    identity_by_order: Dict[int, Dict[str, Any]] = {}

    for result_index, raw_result in enumerate(results):
        result = dict(raw_result)
        result["evidence"] = []
        finalized.append(result)
        dropped_counts.append(0)
        for raw_evidence in raw_result.get("evidence") or []:
            evidence = dict(raw_evidence)
            identity = _candidate_identity(
                evidence,
                tool=raw_result.get("tool"),
                lobe=raw_result.get("lobe"),
                order=order,
            )
            retrieved.append(identity)
            identity_by_order[order] = identity
            if _is_query_responsive(evidence, query_profile):
                responsive.append((order, result_index, evidence))
            else:
                dropped_counts[result_index] += 1
                excluded.append({**identity, "reason": "not_query_responsive"})
            order += 1

    # Prefer a qualified edge over its legacy base edge regardless of retrieval
    # order or a small score difference.  This lets best_friend dominate friend
    # for the same subject/object while leaving unrelated facts independent.
    responsive.sort(
        key=lambda item: (
            -_relationship_specificity(item[2]),
            -float(item[2].get("confidence") or 0.0),
            item[0],
        )
    )

    seen_memory_ids: set[str] = set()
    seen_semantics: set[tuple[str, ...]] = set()
    seen_content: set[str] = set()
    retained = 0
    # The cap is shared by every forced tool, so admit each tool's best
    # responsive row FIRST (in tool order), then fill the rest by merit. With a
    # pure merit fill, one lane's rows starve the others: live 2026-09-06,
    # "do you remember what I built yesterday?" — remember_time filled all 8
    # slots with episode rows and the one builder artifact remember_dev had
    # found (the actual answer) was dropped as over the limit.
    first_by_result: Dict[int, tuple[int, int, Dict[str, Any]]] = {}
    for item in responsive:
        first_by_result.setdefault(item[1], item)
    admission_order = [first_by_result[index] for index in sorted(first_by_result)]
    admission_order += [item for item in responsive if item is not first_by_result.get(item[1])]
    admitted: List[Dict[str, Any]] = []
    for candidate_order, result_index, evidence in admission_order:
        memory_id = str(evidence.get("memory_id") or "").strip()
        semantic_keys = _semantic_keys(evidence)
        content_key = _normalized_text(evidence.get("remembered_as") or evidence.get("title") or "")
        # Each rule is checked separately rather than folded into one boolean, because the
        # exposure record has to say WHICH rule withheld the candidate. The admission
        # outcome is identical either way: any of them excludes.
        if memory_id and memory_id in seen_memory_ids:
            reason = "duplicate_memory_id"
        elif any(key in seen_semantics for key in semantic_keys):
            reason = "duplicate_semantics"
        elif content_key and content_key in seen_content:
            reason = "duplicate_content"
        elif retained >= limit:
            reason = "over_candidate_cap"
        else:
            reason = ""
        if reason:
            dropped_counts[result_index] += 1
            excluded.append(
                {**identity_by_order[candidate_order], "reason": reason}
            )
            continue

        admitted.append(identity_by_order[candidate_order])
        finalized[result_index]["evidence"].append(evidence)
        retained += 1
        if memory_id:
            seen_memory_ids.add(memory_id)
        seen_semantics.update(semantic_keys)
        if content_key:
            seen_content.add(content_key)

    for result_index, result in enumerate(finalized):
        clean_evidence = result["evidence"]
        result["confidence"] = max(
            (float(item.get("confidence") or 0.0) for item in clean_evidence),
            default=0.0,
        )
        if not clean_evidence and str(result.get("verdict") or "").lower() == "verified":
            result["grounding"] = "VERIFIED_NONRESPONSIVE"
        result["candidate_hygiene"] = {
            "retained": len(clean_evidence),
            "dropped": dropped_counts[result_index],
        }

    exposure = {
        "query": str(query or ""),
        "candidate_cap": limit,
        "retrieved": retrieved,
        # Retrieval order, not admission order: the record describes what the tools produced,
        # so two runs that admit the same set read the same.
        "admitted": sorted(admitted, key=lambda row: row["retrieval_order"]),
        "excluded": sorted(excluded, key=lambda row: row["retrieval_order"]),
    }
    return finalized, exposure


def _query_profile(query: str) -> Dict[str, Any]:
    normalized = _normalized_text(query, preserve_proper_possessive=True)
    tokens = set(normalized.split())
    concepts = {concept for concept, aliases in _RELATION_CONCEPTS.items() if tokens & aliases}
    if _ORGANIZATION_ROLE_QUERY_RE.search(str(query or "")):
        concepts.add("organization_role")
    qualifiers = tokens & _RELATION_QUALIFIERS
    anchors = tokens - _QUERY_STOPWORDS - _RELATION_WORDS - _RELATION_QUALIFIERS
    if "my" in tokens or "mine" in tokens:
        anchors.add("operator")
    anchors.discard("my")
    anchors.discard("mine")
    for anchor in tuple(anchors):
        if len(anchor) > 3 and anchor.endswith("s"):
            anchors.add(anchor[:-1])
    return {
        "tokens": tokens,
        "concepts": concepts,
        "qualifiers": qualifiers,
        "anchors": anchors,
        "subject_scoped": bool(_SUBJECT_QUERY_RE.search(str(query or ""))),
        "identity_scoped": bool(_IDENTITY_QUERY_RE.search(str(query or ""))),
    }


def _is_query_responsive(evidence: Mapping[str, Any], profile: Mapping[str, Any]) -> bool:
    if _is_profile_scaffolding(evidence):
        return False
    if (
        _is_identity_evidence(evidence)
        and not profile.get("identity_scoped")
        and not profile.get("preserve_identity")
    ):
        return False

    candidate_tokens = set(_evidence_text(evidence).split())
    query_concepts = set(profile.get("concepts") or ())
    query_qualifiers = set(profile.get("qualifiers") or ())
    anchors = set(profile.get("anchors") or ())

    # Relationship questions are precision-sensitive.  The subject, relation
    # family, and explicit modifier must all be present.  Thus a code-review
    # skill containing "best" and "friend" cannot answer a question about Noah,
    # and a generic friend edge cannot answer "best friend".
    if query_concepts:
        candidate_concepts = {
            concept for concept, aliases in _RELATION_CONCEPTS.items() if candidate_tokens & aliases
        }
        if not (query_concepts & candidate_concepts):
            return False
        if query_qualifiers and not query_qualifiers.issubset(candidate_tokens):
            return False
        if anchors and not (anchors & candidate_tokens):
            return False
        return True

    # Broad "remember/know about <subject>" and "who is <subject>" queries
    # should return multiple facts about that subject, but not global identity
    # seals or unrelated skills merely because persona/identity lobes were read.
    if profile.get("subject_scoped") and anchors:
        return bool(anchors & candidate_tokens)
    return True


def _is_identity_evidence(evidence: Mapping[str, Any]) -> bool:
    coord = evidence.get("coord") or []
    try:
        return bool(coord) and int(coord[0]) == 0
    except (TypeError, ValueError, IndexError):
        return False


def _evidence_text(evidence: Mapping[str, Any]) -> str:
    parts = [
        str(evidence.get("remembered_as") or ""),
        str(evidence.get("title") or ""),
    ]
    for facet in evidence.get("relation_facets") or []:
        if not isinstance(facet, Mapping):
            continue
        parts.extend(
            str(facet.get(key) or "") for key in ("subject", "facet", "relation", "object")
        )
    return _normalized_text(" ".join(parts))


def _semantic_key(evidence: Mapping[str, Any]) -> tuple[str, ...] | None:
    facets = [
        facet for facet in evidence.get("relation_facets") or [] if isinstance(facet, Mapping)
    ]
    meaningful = [
        facet for facet in facets if str(facet.get("facet") or "").strip().lower() != "name"
    ]
    relationship = [
        facet
        for facet in meaningful
        if str(facet.get("facet") or "").strip().lower() == "relationship"
    ]
    chosen = relationship[0] if relationship else (meaningful[0] if meaningful else None)
    if chosen is None:
        return _text_relationship_key(evidence)
    subject = _normalized_text(chosen.get("subject") or "")
    facet = _normalized_text(chosen.get("facet") or "")
    relation = _normalized_text(chosen.get("relation") or "")
    if facet == "relationship":
        relation = " ".join(
            token for token in relation.split() if token not in _RELATION_QUALIFIERS
        )
    obj = _normalized_text(chosen.get("object") or "")
    return subject, facet, relation, obj


def _semantic_keys(evidence: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    """Return canonical and compatibility keys for one recalled fact.

    Current persona leaves expose structured relation facets, while older
    verified leaves can expose only answer text.  Retaining both identities
    lets either generation deduplicate the other without weakening the
    structured key used by current records.
    """
    keys: list[tuple[str, ...]] = []
    canonical_key = _semantic_key(evidence)
    if canonical_key:
        keys.append(canonical_key)
    text_key = _text_relationship_key(evidence)
    if text_key and text_key not in keys:
        keys.append(text_key)
    return tuple(keys)


def _relationship_specificity(evidence: Mapping[str, Any]) -> int:
    """Count explicit modifiers on the candidate's relationship edge."""
    for facet in evidence.get("relation_facets") or []:
        if not isinstance(facet, Mapping):
            continue
        if _normalized_text(facet.get("facet") or "") != "relationship":
            continue
        relation_tokens = set(_normalized_text(facet.get("relation") or "").split())
        return len(relation_tokens & _RELATION_QUALIFIERS)
    tokens = set(_evidence_text(evidence).split())
    if not any(tokens & aliases for aliases in _RELATION_CONCEPTS.values()):
        return 0
    return len(tokens & _RELATION_QUALIFIERS)


def _text_relationship_key(evidence: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Infer the same edge key from the verifier's live text-only shape."""
    tokens = set(_evidence_text(evidence).split())
    concepts = tuple(
        sorted(concept for concept, aliases in _RELATION_CONCEPTS.items() if tokens & aliases)
    )
    if not concepts:
        return None
    entity_tokens = tuple(
        sorted(
            tokens
            - _QUERY_STOPWORDS
            - _RELATION_WORDS
            - _RELATION_QUALIFIERS
            - _RELATION_EDGE_NOISE
        )
    )
    if not entity_tokens:
        return None
    return ("relationship", *concepts, *entity_tokens)


def _is_profile_scaffolding(evidence: Mapping[str, Any]) -> bool:
    """Exclude persona-root/index markers that carry no operator-facing fact."""
    title = str(evidence.get("title") or "").strip().lower()
    remembered_as = _normalized_text(evidence.get("remembered_as") or "")
    return title.startswith("persona:") or remembered_as.startswith("first encounter with ")


def _normalized_text(value: Any, *, preserve_proper_possessive: bool = False) -> str:
    text = str(value or "").replace("_", " ")
    text = _POSSESSIVE_RE.sub(r"\1", text)
    if preserve_proper_possessive:
        text = _UNAPOSTROPHIZED_PROPER_POSSESSIVE_RE.sub(r"\1", text)
    return " ".join(_WORD_RE.findall(text.lower()))
