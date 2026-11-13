"""Persona-statement ingest orchestrator.

Slice 2 of the persona-routing-decompose fix (Slice 1 landed the
extraction fix in ``memory.enrichment.personas.enrich``). This module
fans a personal statement ("I'm Noah, I have a sister named Miranda,
a son named Abel, and I go to college for cybersecurity.") out into
DISCRETE, correctly-routed, leaf-tagged records:

  * the speaker's own attributes (name, occupation, education,
    location, preference, workplace, age, contact) each become their
    own leaf under the operator namespace ``(1, 0)`` — never one
    monolithic blob;
  * each relationship entity (facet=="relationship", carries a
    ``linked_name``) routes the named person through
    ``core.persona_router.route_persona`` to ``(1, 1, person_id)`` and
    writes a person_stub record stamped with the relationship
    (``relationship:<type>``) and linked back to the speaker
    (``linked_to:<speaker>`` tag + ``enrichment.linked_persons`` so the
    ``entity_linker`` post-commit hook forms a real bidirectional
    link, per ``memory.post_commit._run_entity_linker``).

Both live chat ingest and structured HUD onboarding call this orchestrator.
Structured onboarding uses ``ingest_operator_profile`` after SourceLedger +
Guardian admission so every accepted field gets a discrete lobe-1 leaf without
re-parsing generated prose.

Design note — no ``root`` override: every write surface this module
touches (``memory.api.write_memory`` and ``core.persona_router``)
resolves paths from module-level globals (``core.paths.MEMORY_ROOT``,
``core.persona_router.OMEGA_ROOT``/``_INDEX_PATH``, ...), not a
threaded root argument. Reusing them faithfully means this module
carries no root parameter either — tests isolate via the shared
``tests.helpers.memory_isolation.redirect_memory_to_tmp`` fixture PLUS
a direct monkeypatch of ``core.persona_router.OMEGA_ROOT`` /
``_INDEX_PATH`` (that module's own persona directory + persona_index.json
path are not covered by the shared redirect), exactly as
``tests/core/test_persona_router_fallback.py`` already does.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from memory.deterministic import stable_hash

# Facets that describe the SPEAKER themself (as opposed to a relationship
# entity's own name, or the relationship link entity itself). Anything
# outside this set from `enrich()` is ignored by the orchestrator.
_SELF_FACETS = frozenset(
    {
        "name",
        "occupation",
        "education",
        "location",
        "preference",
        "workplace",
        "age",
        "contact",
        "business",
        # The named school itself, separate from the field of study. Enrollment
        # and credential are different propositions and "University of Phoenix"
        # must stay lexically findable -- the old extraction reduced it to the
        # bare token "phoenix", which no query naming the university could match.
        "education_institution",
        "organization_role",
        "children",
        "preferred_name",
        "birthday",
        "pronouns",
        "timezone",
        "relationship",
        "marital_status",
        "notes",
        "picture_path",
    }
)

_FACET_SENTENCES = {
    "name": "{speaker}'s name is {value}.",
    "occupation": "{speaker} works as {value}.",
    "education": "{speaker} studies {value}.",
    "education_institution": "{speaker} attends {value}.",
    "location": "{speaker} lives in {value}.",
    "preference": "{speaker} prefers {value}.",
    "workplace": "{speaker} works at {value}.",
    "age": "{speaker} is {value} years old.",
    "contact": "{speaker}'s contact is {value}.",
    "business": "{speaker} owns a business called {value}.",
    "preferred_name": "{speaker} prefers to be called {value}.",
    "birthday": "{speaker}'s birthday is {value}.",
    "pronouns": "{speaker}'s pronouns are {value}.",
    "timezone": "{speaker}'s timezone is {value}.",
    # "2 daughters" / "married" -- stated as a COUNT or a STATE with nobody
    # named. Both facets were reachable in _SELF_FACETS but had no template
    # here, which is how a fact renders to nothing and lands as an empty record.
    "children": "{speaker} has {value}.",
    "marital_status": "{speaker} is {value}.",
    "relationship": "{speaker}'s relationship to Omega is {value}.",
    "notes": "{speaker}'s onboarding notes are: {value}.",
    "picture_path": "{speaker}'s profile picture is stored at {value}.",
}

_PRIVATE_PROFILE_FACETS = frozenset(
    {
        "birthday",
        "location",
        "timezone",
        "notes",
        "picture_path",
        "contact",
    }
)

# The canonical operator-name anchor (written by onboarding + boot migration).
_OPERATOR_NAME_ANCHOR_ID = "1.00.operator_name"

_EXPLICIT_MEMORY_DIRECTIVE_RE = re.compile(
    r"^(?:please\s+)?(?:remember|note)(?:\s+that)?\s+",
    re.IGNORECASE,
)
_NAMED_PERSON_ANCHOR_RE = re.compile(
    r"^([A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*){1,3})\s*[.!]\s*(.+)$",
    re.DOTALL,
)
_THIRD_PERSON_PRONOUN_RE = re.compile(r"^(?:he|she|they)\b", re.IGNORECASE)
_ONBOARDING_NAMED_BUSINESS_RE = re.compile(
    r"\b(?:i|we)\s+(?:also\s+)?(?:own|run|founded|started|operate|launched|manage)\s+"
    r"(?:(?:an?|the)\s+)?(?:business|company|startup|firm|corporation|venture)\s+"
    r"(?:called|named)\s+(?P<organization>[^.!?\r\n]{2,160})",
    re.IGNORECASE,
)

_SOURCE_CITATION_FIELDS = (
    "source_id",
    "source_type",
    "span_start",
    "span_end",
    "span_hash",
    "source_content_hash",
    "source_session_id",
    "source_session_timestamp",
)


def _citation_fields(source_citation: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    return {
        key: source_citation[key]
        for key in _SOURCE_CITATION_FIELDS
        if source_citation and source_citation.get(key) is not None
    }


def _operator_name_anchor_head() -> tuple[str, Dict[str, Any]] | None:
    """Resolve the current head of the operator-name anchor's revision chain."""
    from memory.engine import walker
    from memory.supersedes import walk_to_head

    walker.load_coord_index()
    coord_index = walker.coord_index_snapshot()
    if _OPERATOR_NAME_ANCHOR_ID not in coord_index:
        return None
    head_id, head_meta, status = walk_to_head(
        _OPERATOR_NAME_ANCHOR_ID,
        coord_index,
        walker.load_meta_by_path,
    )
    if status != "ok" or head_meta is None:
        raise RuntimeError(
            f"operator-name anchor chain could not resolve: status={status}, head={head_id}"
        )
    return head_id, head_meta


def _operator_name_from_anchor(record: Mapping[str, Any]) -> Optional[str]:
    import re

    name = str((record.get("enrichment") or {}).get("operator_name") or "").strip()
    if name:
        return name
    summary = str(record.get("summary") or "")
    match = re.fullmatch(r"\s*The operator is (.+?)\.?\s*", summary)
    return match.group(1).strip() if match else None


def _resolve_operator_name() -> Optional[str]:
    """Read the current name from the canonical operator-name anchor head."""
    anchor = _operator_name_anchor_head()
    if anchor is None:
        return None
    head_id, record = anchor
    name = _operator_name_from_anchor(record)
    if not name:
        raise ValueError(f"operator-name anchor {head_id} has no readable name")
    return name


def _display_name(value: str) -> str:
    return " ".join(part[:1].upper() + part[1:] for part in str(value).split())


def _replace_operator_possessive(text: str, operator_name: Optional[str]) -> str:
    """Normalize "Noahs"/"Noah's"/"Noah Lee's" to the speaker-relative
    possessive understood by the persona grammar.

    Only the configured operator's full name and first name are eligible; an
    arbitrary third-party possessive must never be rewritten as operator-owned.
    """
    aliases: List[str] = []
    clean_name = str(operator_name or "").strip()
    if clean_name:
        aliases.extend((clean_name, clean_name.split()[0]))
    out = text
    for alias in sorted(set(aliases), key=len, reverse=True):
        words = r"\s+".join(re.escape(part) for part in alias.split())
        out = re.sub(
            rf"\b{words}(?:['’]?s)\b",
            "my",
            out,
            flags=re.IGNORECASE,
        )
    return out


def _prepare_explicit_persona_memory(
    text: str,
    operator_name: Optional[str],
) -> tuple[str, List[Dict[str, Any]]]:
    """Resolve the deterministic structure of an explicit named-person memory.

    The chat statement remains the immutable cited source. This helper only
    produces a parse projection: it removes the imperative wrapper, resolves
    sentence-leading he/she/they to the name anchor, recognizes the operator's
    possessive even when the apostrophe was omitted, and emits facts the generic
    dependency grammar cannot represent (coordinated roles and marital status).
    """
    match = _EXPLICIT_MEMORY_DIRECTIVE_RE.match(str(text or "").strip())
    if match is None:
        return text, []
    payload = str(text or "").strip()[match.end() :].strip()
    anchor_match = _NAMED_PERSON_ANCHOR_RE.match(payload)
    if anchor_match is None:
        return payload, []

    person_name = anchor_match.group(1).strip()
    person_key = person_name.lower()
    tail = anchor_match.group(2).strip()
    resolved_sentences: List[str] = []
    supplemental: List[Dict[str, Any]] = []

    for raw in re.split(r"(?<=[.!?])\s+", tail):
        sentence = raw.strip().rstrip(".!?")
        if not sentence:
            continue
        sentence = _THIRD_PERSON_PRONOUN_RE.sub(person_name, sentence, count=1)
        sentence = re.sub(
            r"\b(and|but)\s+(?:he|she|they)\b",
            lambda match: f"{match.group(1)} {person_name}",
            sentence,
            flags=re.IGNORECASE,
        )
        sentence = _replace_operator_possessive(sentence, operator_name)
        resolved_sentences.append(f"{sentence}.")

        relation_match = re.match(
            rf"^{re.escape(person_name)}\s+is\s+(?:also\s+)?my\s+(.+)$",
            sentence,
            re.IGNORECASE,
        )
        descriptor = relation_match.group(1) if relation_match else ""
        for pattern, relation, base_relation, qualifiers in (
            (r"\bbest\s+friend\b", "best_friend", "friend", ["best"]),
            (
                r"\bbusiness\s+partner\b",
                "business_partner",
                "partner",
                ["business"],
            ),
        ):
            if re.search(pattern, descriptor, re.IGNORECASE):
                supplemental.append(
                    {
                        "name": relation,
                        "base_relation": base_relation,
                        "qualifiers": qualifiers,
                        "facet": "relationship",
                        "linked_name": person_key,
                        "confidence": 0.95,
                        "subject": "operator",
                        "source": "explicit_memory_projection",
                    }
                )

        organization_match = re.search(
            rf"\b{re.escape(person_name)}\s+is\s+(?:also\s+)?"
            r"(?P<role>part[ -]+owner|co[ -]?owner|an?\s+owner)\s+of\s+"
            r"(?P<organization>[^,.;]+)",
            sentence,
            re.IGNORECASE,
        )
        if organization_match:
            role = re.sub(r"\s+", " ", organization_match.group("role")).strip()
            organization = organization_match.group("organization").strip()
            supplemental.append(
                {
                    "name": f"{role} of {organization}",
                    "facet": "organization_role",
                    "confidence": 0.95,
                    "subject": person_key,
                    "role": role,
                    "organization": organization,
                    "source": "explicit_memory_projection",
                }
            )

        children_match = re.search(
            rf"\b{re.escape(person_name)}\s+(?:has|have)\s+"
            r"(?P<count>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
            r"(?P<kind>daughters?|sons?|children|kids)\b",
            sentence,
            re.IGNORECASE,
        )
        if children_match:
            children = f"{children_match.group('count')} {children_match.group('kind')}"
            supplemental.append(
                {
                    "name": children,
                    "facet": "children",
                    "confidence": 0.95,
                    "subject": person_key,
                    "count": children_match.group("count"),
                    "child_kind": children_match.group("kind"),
                    "source": "explicit_memory_projection",
                }
            )

        if re.search(
            rf"\b{re.escape(person_name)}\s+is\s+(?:also\s+)?married\b",
            sentence,
            re.IGNORECASE,
        ):
            supplemental.append(
                {
                    "name": "married",
                    "facet": "marital_status",
                    "confidence": 0.95,
                    "subject": person_key,
                    "source": "explicit_memory_projection",
                }
            )

    return " ".join(resolved_sentences) or payload, supplemental


def _person_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _recognized_person_names(
    source_text: str,
    parse_text: str,
    entities: List[Dict[str, Any]],
    operator_name: Optional[str],
) -> List[str]:
    """Collect high-confidence people that need stable profile roots."""
    from core.persona_router import detect_persons

    candidates: List[str] = [
        *detect_persons(source_text),
        *detect_persons(parse_text),
    ]
    non_person_subjects = {
        "",
        "operator",
        "user",
        "speaker",
        "i",
        "me",
        "my",
        "we",
        "us",
        "our",
        "he",
        "she",
        "they",
    }
    for entity in entities:
        if entity.get("facet") == "relationship" and entity.get("linked_name"):
            candidates.append(str(entity["linked_name"]))
        subject = str(entity.get("subject") or "").strip()
        if subject.lower() not in non_person_subjects:
            candidates.append(subject)
        if (
            entity.get("facet") == "name"
            and entity.get("name")
            and subject
            and _person_key(subject) == _person_key(str(entity["name"]))
        ):
            candidates.append(str(entity["name"]))

    operator_aliases: set[str] = set()
    clean_operator = str(operator_name or "").strip()
    if clean_operator:
        operator_aliases.add(_person_key(clean_operator))
        operator_aliases.add(_person_key(clean_operator.split()[0]))

    seen: set[str] = set()
    people: List[str] = []
    for raw in candidates:
        display = _display_name(str(raw or "").strip())
        key = _person_key(display)
        if not key or key in operator_aliases or key in seen:
            continue
        seen.add(key)
        people.append(display)
    return people


def _ensure_persona_profile_record(
    person_name: str,
    *,
    source_citation: Optional[Mapping[str, Any]] = None,
) -> Optional["IngestedRecord"]:
    from core.persona_router import ensure_persona_profile, route_persona

    memory_id = ensure_persona_profile(
        person_name,
        source_citation=source_citation,
    )
    if not memory_id:
        return None
    _, _, person_id = route_persona(person_name)
    return IngestedRecord(
        kind="persona_profile",
        facet="name",
        value=person_name,
        coord=[1, 1],
        tags=["persona", "profile_root", "persona_ingest"],
        id=memory_id,
        path=_resolve_record_path(memory_id),
        person_id=person_id,
        person_name=person_name,
    )


@dataclass(frozen=True)
class IngestedRecord:
    """One record written by `ingest_persona_statement`, for test/inspection.

    `coord` is the schema coord ([lobe, subdomain]); `person_id` (when
    set) is the 3rd-tier directory slug carried via the `domain`
    payload field — NOT a schema coord component (matches
    `core.persona_router.route_persona`'s own (lobe, subdomain,
    person_id) triple convention).
    """

    kind: str  # "operator_attribute" | "person_stub"
    facet: str
    value: str
    coord: List[int]
    tags: List[str]
    id: Optional[str] = None
    path: Optional[str] = None
    person_id: Optional[str] = None
    person_name: Optional[str] = None
    relation: Optional[str] = None


@dataclass(frozen=True)
class PersonaIngestResult:
    speaker: str
    text: str
    records: List[IngestedRecord] = field(default_factory=list)

    @property
    def operator_records(self) -> List[IngestedRecord]:
        return [r for r in self.records if r.kind == "operator_attribute"]

    @property
    def person_records(self) -> List[IngestedRecord]:
        return [r for r in self.records if r.kind == "person_stub"]

    @property
    def profile_records(self) -> List[IngestedRecord]:
        return [r for r in self.records if r.kind == "persona_profile"]

    @property
    def person_attribute_records(self) -> List[IngestedRecord]:
        return [r for r in self.records if r.kind == "person_attribute"]

    @property
    def relation_records(self) -> List[IngestedRecord]:
        return [r for r in self.records if r.kind == "operator_relation"]


def ingest_persona_statement(
    text: str,
    *,
    speaker: str = "operator",
    source_citation: Optional[Mapping[str, Any]] = None,
) -> PersonaIngestResult:
    """Decompose a personal statement into routed, leaf-tagged records.

    Runs `memory.enrichment.personas.enrich(text)` and fans each
    resulting entity out to its own record — operator attributes to
    `(1, 0)`, relationship targets to `(1, 1, person_id)` via
    `core.persona_router.route_persona`. Returns every record written
    so callers/tests can assert on routing + stamping.
    """
    from memory.enrichment.personas import enrich

    op_name = _resolve_operator_name()
    parse_text, supplemental = _prepare_explicit_persona_memory(text, op_name)
    enrichment = enrich(parse_text) or {}
    raw_entities: List[Dict[str, Any]] = [
        *list(enrichment.get("entities") or []),
        *supplemental,
    ]
    # The generic persona grammar intentionally recognizes broad family/social
    # relations.  When the explicit-memory projection has a qualified version
    # of that same edge (best_friend over friend, business_partner over
    # partner), retain the qualified assertion as the canonical fact instead
    # of writing both and making recall answer with the weaker duplicate.
    # Keyed WITHOUT the subject: the qualified assertion is projected from the
    # operator's side ("Noah's best friend"), while the generic grammar reads
    # the same edge from the other end ("HE is Noah's friend" -> subject is the
    # third party). Those are one edge stated from two directions, so requiring
    # the subjects to match let the weaker duplicate through and recall then had
    # both ("best_friend", X) and ("friend", X) to choose between.
    qualified_relationships = {
        (
            str(entity.get("linked_name") or "").strip().lower(),
            str(entity.get("base_relation") or "").strip().lower(),
        )
        for entity in raw_entities
        if str(entity.get("facet") or "").strip().lower() == "relationship"
        and str(entity.get("base_relation") or "").strip()
        and str(entity.get("name") or "").strip().lower()
        != str(entity.get("base_relation") or "").strip().lower()
    }
    entities: List[Dict[str, Any]] = []
    seen_entities: set[tuple[str, str, str, str]] = set()
    for entity in raw_entities:
        facet = str(entity.get("facet") or "").strip().lower()
        relation = str(entity.get("name") or "").strip().lower()
        subject = str(entity.get("subject") or "operator").strip().lower()
        linked_name = str(entity.get("linked_name") or "").strip().lower()
        if facet == "relationship" and (linked_name, relation) in qualified_relationships:
            continue
        key = (
            facet,
            relation,
            linked_name,
            subject,
        )
        if key in seen_entities:
            continue
        seen_entities.add(key)
        entities.append(entity)

    relationship_entities = [
        e for e in entities if e.get("facet") == "relationship" and e.get("linked_name")
    ]
    linked_names = {e["linked_name"] for e in relationship_entities}

    records: List[IngestedRecord] = []
    for person_name in _recognized_person_names(
        text,
        parse_text,
        entities,
        op_name,
    ):
        profile = _ensure_persona_profile_record(
            person_name,
            source_citation=source_citation,
        )
        if profile is not None:
            records.append(profile)

    # ── Speaker's own attributes → discrete operator (1,0) leaves ──────
    for entity in entities:
        facet = entity.get("facet")
        name = entity.get("name")
        if not facet or not name:
            continue
        if facet == "relationship":
            continue
        if facet == "name" and name in linked_names:
            # This name is the OBJECT of a relationship (e.g. "miranda"),
            # not the speaker's own name — handled below as a person_stub.
            continue
        if facet not in _SELF_FACETS:
            continue
        subject = str(entity.get("subject") or "operator").strip().lower()
        if subject and subject != "operator":
            records.append(
                _write_person_attribute(
                    speaker,
                    text,
                    subject,
                    entity,
                    source_citation=source_citation,
                )
            )
        else:
            records.append(
                _write_operator_attribute(speaker, text, entity, source_citation=source_citation)
            )

    # ── Relationship entities → BOTH directions ────────────────────────
    # person_stub (1,1) answers "who is <person>"; the reverse operator leaf
    # (1,0) answers "who is the operator's <relation>" WITHOUT the model having
    # to reverse "<person> is the operator's <relation>". Both records also carry
    # the operator's NAME when known ("Noah Lee's son is Abel") so a name query
    # ("tell me about Noah") matches lexically — not only "the operator" queries.
    for rel_entity in relationship_entities:
        records.append(
            _write_person_stub(
                speaker,
                text,
                rel_entity,
                op_name,
                source_citation=source_citation,
            )
        )
        records.append(
            _write_operator_relation_leaf(
                speaker,
                text,
                rel_entity,
                op_name,
                source_citation=source_citation,
            )
        )

    return PersonaIngestResult(speaker=speaker, text=text, records=records)


def ingest_operator_profile(
    profile: Mapping[str, Any],
    *,
    speaker: str = "operator",
    source_citation: Optional[Mapping[str, Any]] = None,
) -> PersonaIngestResult:
    """Project an accepted structured onboarding profile into discrete persona leaves.

    Field identity already arrived as structured data and crossed Guardian. Every
    accepted field receives its own lobe-1 leaf with the same immutable source
    citation, while the private monolithic onboarding record remains the lossless
    source-of-profile projection. Free-form notes then pass through the same
    deterministic persona-statement decomposition used by chat memory so facts such
    as "I own Omega AI LLC" become atomic persona leaves instead of living only in
    the private notes blob.
    """
    if not isinstance(profile, Mapping):
        raise ValueError("operator profile must be a mapping")
    source_text = json.dumps(dict(profile), sort_keys=True, ensure_ascii=False, default=str)
    records: List[IngestedRecord] = []
    for facet, raw in profile.items():
        facet_name = str(facet or "").strip()
        value = str(raw or "").strip()
        if not facet_name or not value:
            continue
        entity = {
            "facet": facet_name,
            "name": value,
            "confidence": 0.95,
            "private": facet_name in _PRIVATE_PROFILE_FACETS,
            "source": "structured_operator_onboarding",
        }
        records.append(
            _write_operator_attribute(
                speaker,
                source_text,
                entity,
                source_citation=source_citation,
            )
        )
        if facet_name == "notes":
            parsed = ingest_persona_statement(
                value,
                speaker=speaker,
                source_citation=source_citation,
            )
            records.extend(parsed.records)
            # The dependency parser is the primary language extractor, but it is
            # intentionally failure-soft when its local model is unavailable.
            # Onboarding cannot silently lose a plainly named organization in
            # that degraded posture, so retain one narrow deterministic safety
            # net for the structured profile's ownership fact.
            parsed_keys = {
                (str(record.facet).lower(), str(record.value).strip().lower())
                for record in parsed.records
            }
            for match in _ONBOARDING_NAMED_BUSINESS_RE.finditer(value):
                organization = match.group("organization").strip()
                key = ("business", organization.lower())
                if not organization or key in parsed_keys:
                    continue
                entity = {
                    "facet": "business",
                    "name": organization,
                    "confidence": 0.9,
                    "private": False,
                    "subject": "operator",
                    "source": "structured_operator_onboarding_fallback",
                }
                records.append(
                    _write_operator_attribute(
                        speaker,
                        value,
                        entity,
                        source_citation=source_citation,
                    )
                )
                parsed_keys.add(key)
    return PersonaIngestResult(speaker=speaker, text=source_text, records=records)


def _existing_operator_attribute(facet: str, value: str) -> Optional[str]:
    """The memory_id of an existing operator (1,0) attribute leaf with the same facet +
    value, else None. Re-stating a self-fact must reuse its record, not mint a duplicate
    (the live two-'operator's name is noah' records). Matched on the deterministic title
    'Operator <facet>: <value>' the write path stamps; fail-soft -> None (write anew)."""
    try:
        from memory.engine import walker

        walker.load_coord_index()
        want_title = f"Operator {facet}: {value}"[:80]
        with walker._COORD_INDEX_LOCK:
            for mid, entry in walker.COORD_INDEX.items():
                coord = entry.get("coord") or []
                if (
                    len(coord) >= 2
                    and int(coord[0]) == 1
                    and int(coord[1]) == 0
                    and str(entry.get("title") or "") == want_title
                ):
                    return str(mid)
    except Exception:
        return None
    return None


def _write_operator_attribute(
    speaker: str,
    source_text: str,
    entity: Dict[str, Any],
    *,
    source_citation: Optional[Mapping[str, Any]] = None,
) -> IngestedRecord:
    from memory.api import CanonicalEnvelope, intake_memory

    facet = str(entity["facet"])
    value = str(entity["name"])
    confidence = float(entity.get("confidence", 0.7))
    is_operator_name_anchor = facet == "name" and speaker.strip().lower() == "operator"
    anchor_name = _display_name(value) if is_operator_name_anchor else value
    if is_operator_name_anchor:
        sentence = f"The operator is {value}."
    elif facet == "preference" and ": " in value:
        # "favorite editor: vim" — a keyed preference reads as a sentence about
        # the key, so a later "what is my favorite editor" matches it whole.
        key, _, thing = value.partition(": ")
        sentence = f"{speaker}'s {key} is {thing}."
    else:
        sentence = _FACET_SENTENCES.get(facet, "{speaker}: {facet}={value}.").format(
            speaker=speaker,
            value=value,
            facet=facet,
        )
    tags = (
        ["operator_identity", "operator_name", "operator", "persona", "anchor"]
        if is_operator_name_anchor
        else [f"profile:{facet}", "operator_profile", "operator", "persona_ingest"]
    )

    # A repeated name returns the current anchor; a changed name is a new
    # canonical record that the post-commit supersedes hook links to the prior
    # anchor head. Immutable projection receipts prohibit rewriting one ID.
    anchor_head = _operator_name_anchor_head() if is_operator_name_anchor else None
    if is_operator_name_anchor and anchor_head is not None:
        head_id, head_meta = anchor_head
        existing_name = _operator_name_from_anchor(head_meta)
        if not existing_name:
            raise ValueError(f"operator-name anchor {head_id} has no readable name")
        if existing_name.casefold() == anchor_name.casefold():
            return IngestedRecord(
                kind="operator_attribute",
                facet=facet,
                value=anchor_name,
                coord=[1, 0, 0],
                tags=tags,
                id=head_id,
                path=_resolve_record_path(head_id),
            )

    # Other self-facts reuse their existing leaf instead of minting a duplicate.
    existing = None if is_operator_name_anchor else _existing_operator_attribute(facet, value)
    if existing is not None:
        return IngestedRecord(
            kind="operator_attribute",
            facet=facet,
            value=value,
            coord=[1, 0, 0],
            tags=tags,
            id=existing,
            path=_resolve_record_path(existing),
        )

    cycle_id = f"persona-ingest-op-{stable_hash('persona_ingest.operator_attribute', speaker, facet, value, source_text, length=10)}"

    envelope = CanonicalEnvelope(
        cycle_id=cycle_id,
        coord=[1, 0, 0],
        type="identity_anchor" if is_operator_name_anchor else "user_profile",
        payload={
            "title": (
                "Operator name"
                if is_operator_name_anchor
                else f"Operator {facet}: {value}"[:80]
            ),
            "summary": f"The operator is {anchor_name}." if is_operator_name_anchor else sentence,
            "text": (
                f"The operator I work with is {anchor_name}. {anchor_name} is my operator — "
                f"when a memory refers to 'the operator' (for example 'the "
                f"operator's son'), it means {anchor_name}."
                if is_operator_name_anchor
                else sentence
            ),
            "tags": tags,
            "domain": "operator",
            "subtopic": "operator_name" if is_operator_name_anchor else f"profile_{facet}",
            # Ordinary chat-derived self-facts remain recallable. Structured
            # onboarding marks sensitive facets (birthday/location/notes/file path)
            # private while keeping work identity available for task grounding.
            "private": False if is_operator_name_anchor else bool(entity.get("private", False)),
            "enrichment": (
                {"operator_name": anchor_name}
                if is_operator_name_anchor
                else {"decomposer": "personal_ingest", "entities": [entity]}
            ),
            **_citation_fields(source_citation),
        },
        source=("memory.persona_ingest.operator_name" if is_operator_name_anchor else "memory.persona_ingest"),
        confidence=1.0 if is_operator_name_anchor else confidence,
        provenance=[
            "memory.persona_ingest.operator_attribute",
            f"facet:{facet}",
            *(
                [f"source_id:{source_citation['source_id']}"]
                if source_citation and source_citation.get("source_id")
                else []
            ),
        ],
        id=(
            _OPERATOR_NAME_ANCHOR_ID
            if is_operator_name_anchor and anchor_head is None
            else None
        ),
        strength_index=1.0 if is_operator_name_anchor else 0.5,
    )
    receipt = intake_memory(envelope, propose=False, bypass_guardian=True, skip_embed=True)
    # NOTE: WriteReceipt.path is misleadingly named — memory.promotion.
    # commit_promotion sets it to `memory_dict["id"]` (the canonical
    # ref/memory id), never a filesystem path. Resolve the real on-disk
    # path via the coord_index for callers/tests that need it.
    memory_id = receipt.path if receipt.validation_status == "committed" else None
    return IngestedRecord(
        kind="operator_attribute",
        facet=facet,
        value=value,
        coord=[1, 0, 0],
        tags=tags,
        id=memory_id,
        path=_resolve_record_path(memory_id),
    )


def _write_person_attribute(
    speaker: str,
    source_text: str,
    person_name: str,
    entity: Dict[str, Any],
    *,
    source_citation: Optional[Mapping[str, Any]] = None,
) -> IngestedRecord:
    """Write one fact ABOUT a named third person under that person's namespace."""
    from core.persona_router import add_memory_for_person, route_persona

    facet = str(entity["facet"])
    value = str(entity["name"])
    confidence = float(entity.get("confidence", 0.7))
    display = _display_name(person_name)
    if facet == "marital_status" and value.lower() == "married":
        sentence = f"{display} is married."
    elif facet == "organization_role":
        sentence = f"{display} is {value}."
    elif facet == "children":
        sentence = f"{display} has {value}."
    elif facet == "location":
        sentence = f"{display} lives in {value}."
    elif facet == "occupation":
        sentence = f"{display} works as {value}."
    elif facet == "workplace":
        sentence = f"{display} works at {value}."
    else:
        sentence = f"{display}'s {facet.replace('_', ' ')} is {value}."

    _, _, person_id = route_persona(person_name)
    tags = [
        "persona_attribute",
        facet,
        f"profile:{facet}",
        f"linked_to:{speaker}",
        "persona_ingest",
    ]
    cycle_id = "persona-ingest-person-" + stable_hash(
        "persona_ingest.person_attribute",
        speaker,
        person_name,
        facet,
        value,
        source_text,
        length=10,
    )
    memory_id = add_memory_for_person(
        person_name,
        {
            "title": f"{display} {facet.replace('_', ' ')}: {value}"[:80],
            "summary": sentence,
            "text": sentence,
            "tags": tags,
            "subtopic": f"profile_{facet}",
            "enrichment": {
                "decomposer": "personal_ingest",
                "entities": [
                    {
                        "name": person_name,
                        "facet": "name",
                        "confidence": confidence,
                        "subject": person_name,
                    },
                    dict(entity),
                ],
                "linked_persons": [speaker],
            },
        },
        type="persona_attribute",
        cycle_id=cycle_id,
        confidence=confidence,
        human_sourced=True,
        source_citation=source_citation,
    )
    return IngestedRecord(
        kind="person_attribute",
        facet=facet,
        value=value,
        coord=[1, 1],
        tags=tags,
        id=memory_id,
        path=_resolve_record_path(memory_id),
        person_id=person_id,
        person_name=person_name,
    )


def _write_person_stub(
    speaker: str,
    source_text: str,
    rel_entity: Dict[str, Any],
    op_name: Optional[str] = None,
    *,
    source_citation: Optional[Mapping[str, Any]] = None,
) -> IngestedRecord:
    from core.persona_router import add_memory_for_person, route_persona

    relation = str(rel_entity["name"])
    person_name = str(rel_entity["linked_name"])
    confidence = float(rel_entity.get("confidence", 0.7))

    _, _, person_id = route_persona(person_name)

    display_name = _display_name(person_name)
    relation_label = relation.replace("_", " ")
    if op_name:
        sentence = (
            f"{display_name} is {op_name}'s {relation_label} — the {speaker}'s {relation_label}."
        )
    else:
        sentence = f"{display_name} is the {speaker}'s {relation_label}."
    tags = ["relationship", relation, f"linked_to:{speaker}", "persona_ingest"]
    cycle_id = f"persona-ingest-rel-{stable_hash('persona_ingest.person_stub', speaker, relation, person_name, source_text, length=10)}"

    # add_memory_for_person also returns a canonical ref/memory-id string
    # (see core.persona_router.add_memory_for_person), not a filesystem path.
    memory_id = add_memory_for_person(
        person_name,
        {
            "title": (
                f"{display_name} — {relation_label} of {op_name}"
                if op_name
                else f"{display_name} — {relation_label} of {speaker}"
            )[:80],
            "summary": sentence,
            "text": sentence,
            "tags": tags,
            "subtopic": f"relationship_{relation}",
            "links": [
                {
                    "kind": "persona_relationship",
                    "relation": relation,
                    "other_name": speaker,
                }
            ],
            "enrichment": {
                "decomposer": "personal_ingest",
                "entities": [
                    {
                        "name": person_name,
                        "facet": "name",
                        "confidence": confidence,
                    },
                    _relationship_projection_entity(rel_entity),
                ],
                # Shared link key with any operator (1,0) record — makes
                # the entity_linker post-commit hook (memory.post_commit.
                # _run_entity_linker) form a real bidirectional link back
                # to the speaker's own records, on top of the tag stamp.
                "linked_persons": [speaker],
            },
        },
        type="persona_relationship",
        cycle_id=cycle_id,
        confidence=confidence,
        # The external statement was already SourceLedger-captured and Guardian-
        # allowed at chat ingress.  This flag labels its derived source; the
        # canonical write itself remains a direct cognitive self-write.
        human_sourced=True,
        source_citation=source_citation,
    )
    return IngestedRecord(
        kind="person_stub",
        facet="relationship",
        value=person_name,
        coord=[1, 1],
        tags=tags,
        id=memory_id,
        path=_resolve_record_path(memory_id),
        person_id=person_id,
        person_name=person_name,
        relation=relation,
    )


def _write_operator_relation_leaf(
    speaker: str,
    source_text: str,
    rel_entity: Dict[str, Any],
    op_name: Optional[str] = None,
    *,
    source_citation: Optional[Mapping[str, Any]] = None,
) -> IngestedRecord:
    """The REVERSE of a person_stub: an operator-side (1,0) leaf so recall about
    the operator answers "who is the operator's <relation>" directly, without the
    model reversing "<person> is the operator's <relation>". Non-private +
    recallable + linked to the person. Carries the operator's NAME when known
    ("Noah Lee's son is Abel") so a name query resolves lexically, plus the
    "the operator's <relation>" phrasing so operator queries resolve too."""
    from memory.api import CanonicalEnvelope, intake_memory

    relation = str(rel_entity["name"])
    person_name = str(rel_entity["linked_name"])
    confidence = float(rel_entity.get("confidence", 0.7))
    display = _display_name(person_name)
    relation_label = relation.replace("_", " ")
    if op_name:
        sentence = (
            f"{op_name}'s {relation_label} is {display}. "
            f"The {speaker}'s {relation_label} is {display}."
        )
    else:
        sentence = f"The {speaker}'s {relation_label} is {display}."
    tags = [
        "relationship",
        relation,
        "reverse_relation",
        f"linked_to:{person_name}",
        "persona_ingest",
    ]
    cycle_id = f"persona-ingest-relrev-{stable_hash('persona_ingest.operator_relation', speaker, relation, person_name, source_text, length=10)}"

    envelope = CanonicalEnvelope(
        cycle_id=cycle_id,
        coord=[1, 0, 0],
        type="user_profile",
        payload={
            "title": (
                f"{op_name}'s {relation_label}: {display}"
                if op_name
                else f"Operator's {relation_label}: {display}"
            )[:80],
            "summary": sentence,
            "text": sentence,
            "tags": tags,
            "domain": "operator",
            "subtopic": f"relation_{relation}",
            "private": False,  # recallable — this is the reverse (operator) direction
            "enrichment": {
                "decomposer": "personal_ingest",
                "entities": [
                    {
                        "name": person_name,
                        "facet": "name",
                        "confidence": confidence,
                    },
                    _relationship_projection_entity(rel_entity),
                ],
                # Shared link key with the person_stub so entity_linker joins them.
                "linked_persons": [person_name],
            },
            **_citation_fields(source_citation),
        },
        source="memory.persona_ingest",
        confidence=confidence,
        provenance=[
            "memory.persona_ingest.operator_relation",
            f"relation:{relation}",
            *(
                [f"source_id:{source_citation['source_id']}"]
                if source_citation and source_citation.get("source_id")
                else []
            ),
        ],
    )
    receipt = intake_memory(envelope, propose=False, bypass_guardian=True, skip_embed=True)
    memory_id = receipt.path if receipt.validation_status == "committed" else None
    return IngestedRecord(
        kind="operator_relation",
        facet="relationship",
        value=person_name,
        coord=[1, 0, 0],
        tags=tags,
        id=memory_id,
        path=_resolve_record_path(memory_id),
        person_name=person_name,
        relation=relation,
    )


def _relationship_projection_entity(rel_entity: Mapping[str, Any]) -> Dict[str, Any]:
    """Persist the exact relation semantics used to render a relationship leaf.

    RecallVerifier derives relation facets from ``enrichment.entities``.  Keeping
    the qualified relation here gives forward and reverse leaves the same
    semantic fingerprint, while retaining the base relation and its modifiers
    for future ranking and natural-language rendering.
    """
    relation = str(rel_entity.get("name") or "").strip().lower()
    base_relation = str(rel_entity.get("base_relation") or relation).strip().lower()
    qualifiers = [
        str(item).strip().lower()
        for item in rel_entity.get("qualifiers") or []
        if str(item).strip()
    ]
    return {
        "name": relation,
        "base_relation": base_relation,
        "qualifiers": list(dict.fromkeys(qualifiers)),
        "facet": "relationship",
        "linked_name": str(rel_entity.get("linked_name") or "").strip().lower(),
        "subject": str(rel_entity.get("subject") or "operator").strip().lower(),
        "confidence": float(rel_entity.get("confidence", 0.7)),
        "source": str(rel_entity.get("source") or "persona_ingest"),
    }


def _resolve_record_path(memory_id: Optional[str]) -> Optional[str]:
    """Resolve a committed memory id to its absolute on-disk path via the
    coord_index — the canonical lookup used by `memory.engine.walker`
    consumers (see e.g. tests/memory/test_direct_write_post_commit_hooks.py's
    `_record` helper)."""
    if not memory_id:
        return None
    from memory.engine import walker

    walker.load_coord_index()
    entry = walker.COORD_INDEX.get(memory_id) or {}
    path = entry.get("path")
    return str(path) if path else None
