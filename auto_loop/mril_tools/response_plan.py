"""Subject-centered response planner — the synthesis layer.

The graph SCORES and the verifier STRIPS to filesystem truth; by the time a candidate
reaches here it is a verified winner carrying its ON-FILE relation facets
(``relation_facets`` — subject/facet/relation/object, stashed by recall_verifier from the
canonical record). The planner groups those winners into a subject-centered structure so
the model narrates the CONNECTED picture ("Noah owns Omega AI and his son is Abel") instead
of reciting a single fact. Buckets (operator spec 2026-07-15):

  DIRECT      — facts about the query subject (operator self-facts on a self-query, plus
                topical hits with no relation facets: a knowledge recall is a direct hit).
  RELATIONAL  — facts whose on-file subject is a DIFFERENT entity, reached because it is
                connected ("Abel likes trains" on a Noah query — present because Abel is
                Noah's son, but ABOUT Abel, so it never masquerades as a fact about Noah).
  EPISODIC    — temporal/episode records (lobe 9): "we talked about trains yesterday".
  CONFLICTING — contradicting singular claims the verifier flagged (the "operator name is
                Kim" class), quarantined so they never pollute the answer.

Pure + deterministic: same items -> same plan. No model calls, no graph reads.
"""

from __future__ import annotations

import re
from typing import Any, List, Mapping, Sequence

# operator relations rendered as short verbs so a subject line reads as prose.
_RELATION_VERB = {
    "relationship": "",  # the specific relation ("son") rides in relation, not the facet
    "business": "owns",
    "occupation": "works as",
    "workplace": "works at",
    "education": "studies",
    "location": "lives in",
    "preference": "likes",
    "name": "is named",
}

_EPISODE_LOBE = 9

# related-entity facts are subordinate context, not the answer — cap so a well-connected
# entity (the operator, a hub) can never flood the block with everyone else's facts.
_RELATIONAL_CAP = 5
_SUBJECT_ALIAS_NOISE = frozenset(
    {
        "a",
        "an",
        "has",
        "have",
        "is",
        "of",
        "operator",
        "the",
    }
)
_COORDINATED_FIRST_PERSON_RE = re.compile(r"\b(?:and\s+i|i\s+and)\b", re.IGNORECASE)


def _tokens(text: str) -> set:
    return {tok for tok in re.findall(r"[a-z0-9_]+", str(text or "").lower()) if len(tok) >= 2}


def _coord_lobe(item: Mapping[str, Any]) -> int | None:
    coord = item.get("coord") or []
    try:
        return int(coord[0])
    except (TypeError, ValueError, IndexError):
        return None


def _item_subjects(item: Mapping[str, Any]) -> set:
    """Distinct on-file subjects a record is ABOUT (from its verified relation facets)."""
    return {
        str(f.get("subject") or "operator").strip().lower()
        for f in item.get("relation_facets") or []
        if isinstance(f, Mapping)
    }


def _subject_alias_tokens(
    subject: str, items: Sequence[Mapping[str, Any]]
) -> set[str]:
    """Return query-visible labels for a verified relation subject.

    Named persona subjects already carry their label in the facet. The operator is stored under
    the canonical subject ``operator``, so derive its visible name from the fact text while
    subtracting the relation and object tokens (``Noah ... owner ... Omega AI`` -> ``noah``).
    """
    if subject != "operator":
        return _tokens(subject)

    aliases: set[str] = set()
    for item in items:
        facets = [
            facet
            for facet in item.get("relation_facets") or []
            if isinstance(facet, Mapping)
            and str(facet.get("subject") or "operator").strip().lower() == "operator"
        ]
        if not facets:
            continue
        text_tokens = _tokens(
            f"{item.get('title') or ''} {item.get('remembered_as') or ''}"
        )
        relation_tokens: set[str] = set()
        for facet in facets:
            relation_tokens.update(_tokens(facet.get("facet") or ""))
            relation_tokens.update(_tokens(facet.get("relation") or ""))
            relation_tokens.update(_tokens(facet.get("object") or ""))
        aliases.update(text_tokens - relation_tokens - _SUBJECT_ALIAS_NOISE)
    return aliases


def resolve_query_subjects(
    query: str, items: Sequence[Mapping[str, Any]]
) -> tuple[str, ...]:
    """Resolve every verified entity that the operator made a primary query subject.

    Comparisons are plural by contract: ``Mitchell and Noah`` must keep both entities' facts in
    the DIRECT answer set. Static code only binds query labels to verified relation subjects; the
    model remains responsible for comparing and articulating what those facts mean.
    """
    q_tokens = _tokens(query)
    query_lower = str(query or "").lower()
    subjects = {subject for item in items for subject in _item_subjects(item)}
    matched: list[tuple[int, str]] = []
    for subject in sorted(subjects):
        aliases = _subject_alias_tokens(subject, items)
        hits = aliases & q_tokens
        if not hits:
            continue
        position = min(
            (query_lower.find(token) for token in hits if query_lower.find(token) >= 0),
            default=len(query_lower),
        )
        matched.append((position, subject))

    ordered = [subject for _, subject in sorted(matched, key=lambda row: (row[0], row[1]))]
    if ordered and _COORDINATED_FIRST_PERSON_RE.search(str(query or "")):
        if "operator" not in ordered:
            ordered.append("operator")
    return tuple(ordered or ["operator"])


def resolve_query_subject(query: str, items: Sequence[Mapping[str, Any]]) -> str:
    """Who is the query about — 'operator' (a self-query) or a named person.

    A NAMED entity that is the SUBJECT of a recalled fact and appears in the query wins FIRST,
    even when the query also carries an incidental first-person pronoun: "tell me about Abel"
    is a query ABOUT Abel, not a self-query — the indirect-object "me" must not short-circuit
    it (doing so demoted Abel's own facts to background — the Slice-4 regression). Only when the
    query names no such recalled subject does it default to 'operator' (self/identity queries,
    and pronoun-only third-person forms like "my sister" that name no entity — those stay
    operator, and their facts, being subject != operator, still surface via the no-main render
    path). A topical query's items carry no subject and stay DIRECT regardless of subject."""
    return resolve_query_subjects(query, items)[0]


def _classify(item: Mapping[str, Any], query_subjects: Sequence[str]) -> str:
    if item.get("conflicting"):
        return "conflicting"
    if _coord_lobe(item) == _EPISODE_LOBE:
        return "episodic"
    item_subjects = _item_subjects(item)
    if not item_subjects:
        # a topical hit (knowledge/code) with no relation facets is a DIRECT hit.
        return "direct"
    if set(query_subjects) & item_subjects:
        return "direct"
    # the fact is about a DIFFERENT entity than the query subject — connected, not central.
    return "relational"


def build_response_plan(
    items: Sequence[Mapping[str, Any]], *, subject: str | Sequence[str] = "operator"
) -> dict[str, Any]:
    """Group verified winners into DIRECT/RELATIONAL/EPISODIC/CONFLICTING for the subject."""
    query_subjects = (subject,) if isinstance(subject, str) else tuple(subject)
    query_subjects = tuple(
        str(value).strip().lower() for value in query_subjects if str(value).strip()
    )
    if not query_subjects:
        query_subjects = ("operator",)
    plan: dict[str, Any] = {
        "subject": query_subjects[0],
        "subjects": list(query_subjects),
        "direct": [],
        "relational": [],
        "episodic": [],
        "conflicting": [],
    }
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        plan[_classify(item, query_subjects)].append(dict(item))
    return plan


def _fact_phrase(facet: Mapping[str, Any]) -> str:
    """A short 'relation object' phrase for one relation facet ("son Abel", "owns Omega AI")."""
    relation = str(facet.get("relation") or "").strip()
    obj = str(facet.get("object") or "").strip()
    if not obj:
        return ""
    facet_name = str(facet.get("facet") or "").strip().lower()
    if facet_name == "relationship" and relation:
        return f"{relation} {obj}"  # "son Abel"
    verb = _RELATION_VERB.get(facet_name, relation or facet_name)
    return f"{verb} {obj}".strip()


def _line_for_item(item: Mapping[str, Any]) -> str:
    """Render one fact line. Prefer the record's truth-verified text (natural, original
    casing — "Noah's son is Abel."); fall back to a compact relation phrase ("son Abel")
    only when the record carries no readable body. Grouping + the weave header — not terse
    phrasing — are what stop the model reciting a single fact."""
    title = str(item.get("title") or "").strip()
    body = str(item.get("remembered_as") or "").strip()
    if body:
        if title and title.lower() not in body.lower():
            return f"{title}: {body}"
        return body
    phrases = [p for p in (_fact_phrase(f) for f in item.get("relation_facets") or []) if p]
    if phrases:
        return " · ".join(dict.fromkeys(phrases))  # dedup, keep order
    return title


def render_response_plan(plan: Mapping[str, Any], *, query: str = "") -> str:
    """Render the plan as a subject-centered grounding block the model narrates from.

    Empty plan -> "" (fail-soft: the base grounding is used unchanged). The block leads with
    the connected DIRECT picture, then names related-entity and episodic context separately
    so the model can weave without conflating whose fact is whose."""
    direct = plan.get("direct") or []
    relational = plan.get("relational") or []
    episodic = plan.get("episodic") or []
    if not (direct or relational or episodic):
        return ""

    # The main block is the CONNECTED picture of the query subject (direct facts + episodic
    # context) — this is what the model weaves.
    lines: List[str] = ["Remembered (weave ALL of these into one connected answer):"]
    for item in direct:
        line = _line_for_item(item)
        if line:
            lines.append(f"- {line}")
    for item in episodic:
        line = _line_for_item(item)
        if line:
            lines.append(f"- (earlier) {line}")

    has_main = bool(direct or episodic)
    if not has_main:
        # No direct/episodic fact for the query subject: the related-entity facts are the ONLY
        # thing recalled, so they ARE the answer (e.g. "what is my sister into?" — the query
        # names no entity, subject resolves to operator, and only the sister's own facts came
        # back). Present them attributed but in the MAIN block, uncapped — demoting the sole
        # recall to "background only" would leave an empty answer and contradict the header.
        for item in relational:
            line = _line_for_item(item)
            if line:
                subj = next(iter(sorted(_item_subjects(item))), "")
                lines.append(f"- (about {subj}) {line}" if subj else f"- {line}")
        return "\n".join(lines)

    # A main answer exists -> relational facts are genuine background (operator doctrine:
    # "Abel's separate facts should not surface unless you ask about Abel"). SUBORDINATE and cap
    # them: context, never the answer, so "Abel likes trains" can't masquerade as a fact about
    # Noah, and one well-connected hub can't flood the block with everyone else's facts.
    related_lines: List[str] = []
    for item in relational[:_RELATIONAL_CAP]:
        line = _line_for_item(item)
        if line:
            subj = next(iter(sorted(_item_subjects(item))), "")
            related_lines.append(f"- (about {subj}) {line}" if subj else f"- {line}")
    if related_lines:
        lines.append("Related context (background only — do not make these the main answer):")
        lines.extend(related_lines)
    return "\n".join(lines)
