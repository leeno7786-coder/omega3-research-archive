"""Deterministic lexical/temporal recall over canonical Dewey files.

This is a cold/stale-projection safety path. It deliberately does not embed
content or replace graph/DME scoring; callers must still pass its candidates
through the canonical recall verifier.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memory.dewey_map import NON_RECALLABLE_LOBES
from memory.taxonomy.recall import morphological_forms
from memory.query_route import QueryRoute, coordinate_affinity, route_query
from memory.semantic_admission import SemanticAdmission, semantic_admission

# k=24 measured at the eval's operating point (budget 128): 2/21 -> 19/21 on
# gpt4_f2262a51. k=16 gives 14/21 and k=32 gives the same 19/21 for more rows,
# so 24 is the knee, not a fitted value.
_SEMANTIC_SEED_COUNT = 24
# Membership by meaning scales with the history: the floor above for small
# corpora, the top DECILE for large ones. 24 of 480 turns (5%) left gold
# answer turns at retina ranks 25-48 outside the lane on LongMemEval-S v1;
# the same turns sit inside the top 10%.
_SEMANTIC_SEED_SHARE = 0.10
# Similarity is VALUE, not only membership. A row the retina places one
# standard deviation nearer the question than its peers gains one standard
# deviation of score -- z-score fusion at equal weight (CombSUM over
# z-normalised scores, the classic late-fusion rule), untuned. Measured on the
# frozen-v1 failure dumps: mean first-gold session rank 3.77 -> 2.45 at this
# weight; without it a semantically-right, lexically-weak row ranks by its
# one shared token.
_SEMANTIC_VALUE_WEIGHT = 1.0

_LAST_SEMANTIC_ADMISSION: SemanticAdmission | None = None


def last_semantic_admission() -> SemanticAdmission | None:
    """Aggregate firing proof for the most recent search in this process.

    RecallCandidate forbids extra fields, so per-row provenance cannot reach
    evidence; these counters are the only way to tell "ran and admitted
    nothing" from "never ran".
    """
    return _LAST_SEMANTIC_ADMISSION


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SHA256_RE = re.compile(r"[0-9a-f]{64}", re.I)
_SKIP_PARTS = frozenset(
    {
        "indexes",
        "taxonomy_store",
        "source_ledger",
        "projection_receipts",
        "quarantine",
        "tombstones",
    }
)
_RECENT_WORDS = frozenset(
    {"current", "currently", "latest", "newest", "now", "recent", "recently", "today"}
)
_EARLY_WORDS = frozenset({"earliest", "first", "initial", "oldest", "originally"})
_RELATION_ONTOLOGY = {
    "relation_residence": frozenset(
        {
            "live",
            "lived",
            "lives",
            "living",
            "move",
            "moved",
            "moves",
            "moving",
            "relocate",
            "relocated",
            "relocates",
            "relocating",
            "reside",
            "resided",
            "resides",
            "residing",
        }
    ),
    "relation_employment": frozenset(
        {
            "employ",
            "employed",
            "employment",
            "job",
            "jobs",
            "work",
            "worked",
            "working",
            "works",
        }
    ),
    "relation_ownership": frozenset(
        {
            "acquire",
            "acquired",
            "bought",
            "buy",
            "have",
            "has",
            "own",
            "owned",
            "owns",
            "purchase",
            "purchased",
        }
    ),
    "relation_preference": frozenset(
        {
            "enjoy",
            "enjoyed",
            "enjoys",
            "favorite",
            "favourite",
            "like",
            "liked",
            "likes",
            "love",
            "loved",
            "loves",
            "prefer",
            "preferred",
            "prefers",
        }
    ),
    "relation_participation": frozenset(
        {
            "attend",
            "attended",
            "attending",
            "compete",
            "competed",
            "join",
            "joined",
            "participate",
            "participated",
            "participating",
            "volunteer",
            "volunteered",
        }
    ),
    "relation_recommendation": frozenset(
        {
            "advise",
            "advised",
            "recommend",
            "recommended",
            "suggest",
            "suggested",
        }
    ),
    "relation_viewing": frozenset(
        {
            "view",
            "viewed",
            "viewing",
            "watch",
            "watched",
            "watching",
        }
    ),
    "relation_completion": frozenset(
        {
            "complete",
            "completed",
            "finish",
            "finished",
        }
    ),
    "relation_leadership": frozenset(
        {
            "head",
            "headed",
            "lead",
            "leader",
            "leading",
            "led",
            "manage",
            "managed",
        }
    ),
}
_RELATION_BY_TERM = {
    term: relation for relation, terms in _RELATION_ONTOLOGY.items() for term in terms
}
_STATE_RELATIONS = frozenset(
    {
        "relation_employment",
        "relation_ownership",
        "relation_preference",
        "relation_residence",
    }
)
_CONCEPT_ONTOLOGY = {
    "concept_event": frozenset(
        {
            "anniversary",
            "birthday",
            "ceremony",
            "engagement",
            "event",
            "festival",
            "funeral",
            "gala",
            "graduation",
            "marriage",
            "retirement",
            "tournament",
            "wedding",
        }
    ),
    "concept_family": frozenset(
        {
            "aunt",
            "brother",
            "child",
            "children",
            "cousin",
            "daughter",
            "daughters",
            "family",
            "father",
            "grandfather",
            "grandmother",
            "husband",
            "husbands",
            "kid",
            "kids",
            "marital",
            "married",
            "mother",
            "nephew",
            "niece",
            "parent",
            "parents",
            "relative",
            "sister",
            "son",
            "sons",
            "spouse",
            "spouses",
            "uncle",
            "wife",
            "wives",
        }
    ),
    "concept_spouse": frozenset(
        {
            "husband",
            "husbands",
            "marital",
            "married",
            "spouse",
            "spouses",
            "wife",
            "wives",
        }
    ),
    "concept_child": frozenset(
        {
            "child",
            "children",
            "daughter",
            "daughters",
            "kid",
            "kids",
            "son",
            "sons",
        }
    ),
    "concept_doctor": frozenset(
        {
            "dermatologist",
            "dermatologists",
            "dentist",
            "dentists",
            "doctor",
            "doctors",
            "physician",
            "physicians",
            "surgeon",
            "surgeons",
            "vet",
            "veterinarian",
            "veterinarians",
            "vets",
        }
    ),
}
_CONCEPTS_BY_TERM = {
    term: frozenset(concept for concept, terms in _CONCEPT_ONTOLOGY.items() if term in terms)
    for term in set().union(*_CONCEPT_ONTOLOGY.values())
}
_QUERY_CONTROL_TERMS = frozenset(
    {
        "count",
        "current",
        "currently",
        "day",
        "days",
        "earliest",
        "first",
        "how",
        "latest",
        "many",
        "month",
        "months",
        "much",
        "newest",
        "now",
        "number",
        "oldest",
        "recent",
        "recently",
        "today",
        "total",
        "week",
        "weeks",
        "year",
        "years",
    }
)
_ANSWER_FEATURE_WEIGHTS = {
    "attended": 3.0,
    "durationoperand": 3.0,
    "personalage": 6.0,
    "projectlead": 3.0,
    "quantity": 1.0,
}
# Measured (Task 3, .artifacts/instruments/recall_probe.py against eac54add
# and gpt4_f2262a51): the lexical term on eac54add spans roughly 6-27, so a
# weight that reorders WITHIN a lexical tier without overriding a large
# lexical gap is appropriate -- large enough to lift a correctly-routed row
# past same-tier competitors, too small to override a strong lexical match in
# the wrong lobe. 5.0 held the halving gate (gpt4_f2262a51 kept all 3 doctor
# rows in evidence); see task-3-report.md for the before/after ranks.
_COORDINATE_AFFINITY_WEIGHT = 5.0


def _coordinate_bonus(route: QueryRoute, coord: object) -> float:
    return coordinate_affinity(route, coord) * _COORDINATE_AFFINITY_WEIGHT


_EXPLICIT_QUANTITY_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s+(?!day|week|month|year)[^\W\d_]+",
    re.I,
)
_EXPLICIT_DURATION_RE = re.compile(
    r"\b(?:\d+|an?|many|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"[ -]?(?:day|week|month|year)s?(?:-long)?\b",
    re.I,
)
_PERSONAL_AGE_RE = re.compile(
    r"\b(?:i(?:'m| am)|my age is)\s+\d{1,3}\b",
    re.I,
)
_PERSONAL_AGE_QUERY_RE = re.compile(
    r"\b(?:how\s+many\s+years|what\s+age)\b.{0,60}"
    r"\b(?:i|we|you|he|she|they)\b",
    re.I,
)
_FIRSTHAND_PARTICIPATION_RE = re.compile(
    r"\b(?:"
    r"attended|participated\s+in|volunteered\s+at|joined|competed\s+in|"
    r"(?:have|'ve|had|'d)\s+been\s+to|got\s+back\s+from|"
    r"returned\s+from|went\s+to"
    r")\b",
    re.I,
)
_PROJECT_LEAD_ASSERTION_RE = re.compile(
    r"(?:"
    r"\b(?:i|we)\s+(?:(?:am|are|'m|'re|have|had)\s+)?"
    r"(?:currently\s+)?(?:head|headed|lead|leading|led|manage|managed)\b"
    r".{0,80}\bprojects?\b|"
    r"\bprojects?\b.{0,80}\b(?:i|we)\s+"
    r"(?:(?:am|are|'m|'re|have|had)\s+)?(?:currently\s+)?"
    r"(?:head|headed|lead|leading|led|manage|managed)\b"
    r")",
    re.I,
)
_SOLO_PROJECT_ASSERTION_RE = re.compile(
    r"\b(?:i|we)\b.{0,60}\b(?:independent|solo)\s+projects?\b",
    re.I,
)
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
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
    }
)


# Imported, NOT reimplemented. This scorer and the verdict's topicality gate must
# agree on what a word IS; while each owned a copy they drifted, and the gate
# discarded rows this scorer had ranked 3rd. See morphological_forms' docstring.
_morphological_forms = morphological_forms


# A calendar date is ONE selective unit, not three unrelated digits.
# ``_query_core_terms`` drops bare digits on purpose -- '2023' alone matches
# nearly every record -- but ``_record_core_terms`` keeps them. That asymmetry
# meant a resolved relative-time anchor ("four weeks ago" -> 2023/02/28) could
# never satisfy the core-term gate in ``search_canonical_files``: the gate
# discarded the very records the anchor had just resolved to.
#
# These fused tokens are derived ON DEMAND for the gate and for the anchor
# bonus. They are deliberately NOT injected into ``_tokens``: every record
# carries a date in its title, so fusing there would add a term to every
# record, shifting ``average_length`` -- which sits in the BM25 denominator --
# and perturbing the score of every query, including ones with no date at all.
# An earlier revision did exactly that and reordered unrelated results.
# Digit-boundary lookarounds, NOT \b: an ISO stamp is "2023-06-15T10:02:00",
# where the day is followed by "T". Both are word characters, so a trailing \b
# never matches and every provenance timestamp silently yielded no date at all.
_DATE_LITERAL_RE = re.compile(r"(?<!\d)(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?!\d)")
_DATE_TOKEN_PREFIX = "datestamp"


def _date_tokens(value: object) -> frozenset[str]:
    """Return fused ``datestampYYYYMMDD`` tokens for every date literal found."""

    return frozenset(
        f"{_DATE_TOKEN_PREFIX}{int(year):04d}{int(month):02d}{int(day):02d}"
        for year, month, day in _DATE_LITERAL_RE.findall(str(value or ""))
    )


# "Four weeks ago" is how people speak, not a date-arithmetic instruction. Case
# eac54add was asked on 2023/03/28: the phrase resolves to exactly 2023/02/28,
# but the answer was written on 2023/03/01 -- twenty-seven days back. Treating
# the resolved day as an exact constraint promoted 21 of 28 evidence rows from
# the WRONG day and left the answer at pool rank 100 of 112.
#
# Three days each way covers ordinary conversational slack without reaching the
# neighbouring week, which would make "three weeks ago" and "four weeks ago"
# indistinguishable. Tolerance is not indifference: weight decays with distance
# so the resolved day still wins, and the bonus is IDF-scaled against the number
# of matching records, so a wider window matches more and is damped automatically.
_ANCHOR_DATE_RADIUS_DAYS = 3


def _anchor_date_tokens(value: object) -> dict[str, int]:
    """Map each in-window ``datestamp`` token to its distance in days from the anchor."""

    window: dict[str, int] = {}
    for year, month, day in _DATE_LITERAL_RE.findall(str(value or "")):
        try:
            anchor = datetime(int(year), int(month), int(day), tzinfo=UTC).date()
        except ValueError:
            continue
        for offset in range(-_ANCHOR_DATE_RADIUS_DAYS, _ANCHOR_DATE_RADIUS_DAYS + 1):
            moment = anchor + timedelta(days=offset)
            token = f"{_DATE_TOKEN_PREFIX}{moment.year:04d}{moment.month:02d}{moment.day:02d}"
            distance = abs(offset)
            if distance < window.get(token, distance + 1):
                window[token] = distance
    return window


def _record_date_tokens(record: Mapping[str, Any]) -> frozenset[str]:
    """Return the day this record was WRITTEN, never a day it merely mentions.

    Authority is the cryptographic source timestamp. Scanning ``text``/``summary``
    would treat a March turn that says "back on 2023/02/28 I..." as though it
    were written on February 28, handing an anchored-day bypass to records the
    anchor never resolved to. The structural title is a compatibility fallback
    only -- it is machine-generated (``session-000031:2023/02/28 (Tue) ...``),
    not user prose.
    """

    provenance = record.get("provenance")
    if isinstance(provenance, Mapping):
        stamp = provenance.get("source_session_timestamp") or provenance.get("source_timestamp")
        tokens = _date_tokens(stamp)
        if tokens:
            return tokens
    return _date_tokens(record.get("title"))


def _tokens(value: object) -> list[str]:
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKD", str(value or "").casefold())
        if not unicodedata.combining(character)
    )
    return [
        form
        for token in _TOKEN_RE.findall(normalized)
        if token not in _STOPWORDS
        for form in _morphological_forms(token)
    ]


def _relation_features(terms: list[str]) -> list[str]:
    return list(
        dict.fromkeys(
            relation for term in terms if (relation := _RELATION_BY_TERM.get(term)) is not None
        )
    )


def _concept_features(terms: list[str]) -> list[str]:
    return list(
        dict.fromkeys(concept for term in terms for concept in _CONCEPTS_BY_TERM.get(term, ()))
    )


def _structural_features(value: object, terms: list[str]) -> list[str]:
    text = str(value or "")
    features: list[str] = []
    term_set = set(terms)
    personal_age_query = bool(_PERSONAL_AGE_QUERY_RE.search(text))
    if {"count", "many", "number", "total"} & term_set or _EXPLICIT_QUANTITY_RE.search(text):
        features.append("quantity")
    if _EXPLICIT_DURATION_RE.search(text) and not personal_age_query:
        features.append("durationoperand")
    if {"age", "old"} & term_set or _PERSONAL_AGE_RE.search(text) or personal_age_query:
        features.append("personalage")
    if _FIRSTHAND_PARTICIPATION_RE.search(text):
        features.append("attended")
    if "project" in term_set and (
        _PROJECT_LEAD_ASSERTION_RE.search(text) or _SOLO_PROJECT_ASSERTION_RE.search(text)
    ):
        features.append("projectlead")
    return features


def _lexical_features(value: object) -> list[str]:
    terms = _tokens(value)
    return list(
        dict.fromkeys(
            [
                *terms,
                *_relation_features(terms),
                *_concept_features(terms),
                *_structural_features(value, terms),
            ]
        )
    )


def _query_core_terms(query: str) -> set[str]:
    terms = _tokens(query)
    lexical_core = {
        term
        for term in terms
        if term not in _QUERY_CONTROL_TERMS and term not in _RELATION_BY_TERM and not term.isdigit()
    } | set(_concept_features(terms))
    structural_core = set(_structural_features(query, terms)) & {
        "personalage",
    }
    core = lexical_core | structural_core
    if not core:
        # No content core term means the gate stays OFF, which is what lets a
        # query built entirely from relation words ("Where did I live as of
        # 2024-06-01?") reach any record at all. The date anchor must not be
        # able to switch that gate on by itself: it would then be the SOLE
        # requirement and admit only records that happen to carry the date.
        return core
    # A resolved anchor WIDENS an existing gate -- records written near that date
    # become admissible even when they share none of the query's content words,
    # which is exactly the case a relative-time question depends on. The gate
    # takes the whole tolerance window; ranking is what separates the days.
    return core | frozenset(_anchor_date_tokens(query))


def _record_core_terms(record: Mapping[str, Any]) -> set[str]:
    core: set[str] = set()
    for field in ("title", "summary", "text", "domain", "subtopic"):
        field_terms = _tokens(record.get(field))
        core.update(token for token in field_terms if token not in _RELATION_BY_TERM)
        core.update(_concept_features(field_terms))
        core.update(set(_structural_features(record.get(field), field_terms)) & {"personalage"})
    tag_terms = _tokens(" ".join(str(tag) for tag in record.get("tags") or ()))
    core.update(token for token in tag_terms if token not in _RELATION_BY_TERM)
    core.update(_concept_features(tag_terms))
    # Fused date tokens are gate-only: they let a resolved anchor match this
    # record without adding a term to the BM25 stream (see _date_tokens).
    core.update(_record_date_tokens(record))
    return core


def _expanded_query_terms(query: str) -> list[str]:
    return _lexical_features(query)


def expand_query_with_category_aliases(query: str) -> str:
    """Append deterministic ontology facets so retrieval and verification agree."""

    original = str(query or "").strip()
    terms = _tokens(original)
    aliases = [
        *_relation_features(terms),
        *_concept_features(terms),
        *_structural_features(original, terms),
    ]
    additions = [alias for alias in dict.fromkeys(aliases) if alias not in terms]
    if not additions:
        return original
    return f"{original} {' '.join(additions)}".strip()


def _preferred_source_role(query: str) -> str | None:
    lowered = str(query or "").lower()
    assistant_reference = re.search(
        r"(?:"
        r"\b(?:can|could|would|will)\s+you\s+remind\s+me\b|"
        r"\b(?:did|have|had)\s+you\s+"
        r"(?:recommend|suggest|say|tell|advise|mention)\b|"
        r"\b(?:you|your)\b.{0,50}\b"
        r"(?:recommended|suggested|said|told|advised|mentioned)\b"
        r")",
        lowered,
    )
    if assistant_reference:
        return "assistant"
    if re.search(r"\b(?:i|me|my|mine)\b", lowered):
        return "user"
    return None


def _record_source_role(record: Mapping[str, Any]) -> str | None:
    blob = " ".join(str(record.get(field) or "") for field in ("title", "summary", "text")).lower()
    if re.search(r"(?:^|[\[:·\s])assistant(?:[\]:\s]|$)", blob):
        return "assistant"
    if re.search(r"(?:^|[\[:·\s])user(?:[\]:\s]|$)", blob):
        return "user"
    return None


def expand_query_with_temporal_anchor(
    query: str,
    reference_timestamp: str | None = None,
) -> str:
    """Append an absolute day token for deterministic relative-time recall."""

    from memory.temporal_range import parse_temporal_range

    if reference_timestamp:
        try:
            reference = datetime.fromisoformat(reference_timestamp.strip().replace("Z", "+00:00"))
        except ValueError:
            return query
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
    else:
        reference = datetime.now(UTC)
    temporal_range = parse_temporal_range(query, reference)
    if not temporal_range:
        return query
    try:
        start = datetime.fromisoformat(str(temporal_range["start"]))
        end = datetime.fromisoformat(str(temporal_range["end"]))
    except (KeyError, TypeError, ValueError):
        return query
    if end - start != timedelta(days=1):
        return query
    return f"{query} {start:%Y/%m/%d}"


def _as_of_epoch_us(query: str) -> int | None:
    match = re.search(
        r"\bas\s+of\s+"
        r"(?P<year>\d{4})"
        r"(?:[-/](?P<month>\d{1,2})"
        r"(?:[-/](?P<day>\d{1,2}))?)?",
        str(query or ""),
        re.I,
    )
    if match is None:
        return None
    year = int(match.group("year"))
    month = int(match.group("month") or 1)
    day = int(match.group("day") or 1)
    try:
        start = datetime(year, month, day, tzinfo=UTC)
        if match.group("day"):
            end = start + timedelta(days=1)
        elif match.group("month"):
            end = (
                datetime(year + 1, 1, 1, tzinfo=UTC)
                if month == 12
                else datetime(year, month + 1, 1, tzinfo=UTC)
            )
        else:
            end = datetime(year + 1, 1, 1, tzinfo=UTC)
    except ValueError:
        return None
    return int(end.timestamp() * 1_000_000)


def _record_epoch(record: Mapping[str, Any], path: Path) -> int:
    raw_epoch = record.get("temporal_epoch_us")
    if isinstance(raw_epoch, int) and raw_epoch >= 0:
        return raw_epoch
    temporal_hash = str(record.get("temporal_hash") or "")
    prefix = temporal_hash.partition("-")[0]
    if len(prefix) == 16:
        try:
            return int(prefix, 16)
        except ValueError:
            pass
    try:
        return path.stat().st_mtime_ns // 1_000
    except OSError:
        return 0


def _source_epoch(record: Mapping[str, Any]) -> int | None:
    provenance = record.get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    raw = str(provenance.get("source_session_timestamp") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000)


def _canonical_records(
    root: Path,
    *,
    allowed_lobes: Collection[int] | None = None,
    denied_lobes: Collection[int] | None = None,
    allowed_subdomains: Collection[int] | None = None,
) -> list[dict[str, Any]]:
    memory_root = root / "memory"
    if not memory_root.is_dir():
        return []
    by_id: dict[str, dict[str, Any]] = {}
    allowed = None if allowed_lobes is None else {int(value) for value in allowed_lobes}
    denied = {int(value) for value in (denied_lobes if denied_lobes is not None else (6, 11))}
    subdomains = (
        None if allowed_subdomains is None else {int(value) for value in allowed_subdomains}
    )
    for path in memory_root.rglob("*.json"):
        if any(part.lower() in _SKIP_PARTS for part in path.parts):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        memory_id = str(payload.get("id") or "").strip()
        coord = payload.get("coord")
        if not memory_id or not isinstance(coord, list) or len(coord) != 3:
            continue
        try:
            lobe = int(coord[0])
            subdomain = int(coord[1])
        except (TypeError, ValueError):
            continue
        if (
            lobe in NON_RECALLABLE_LOBES
            or lobe in denied
            or (allowed is not None and lobe not in allowed)
            or (subdomains is not None and subdomain not in subdomains)
        ):
            continue
        if not any(str(payload.get(field) or "").strip() for field in ("title", "summary", "text")):
            continue
        from memory.workspace_retention import workspace_source_is_current

        if not workspace_source_is_current(payload):
            continue
        epoch = _record_epoch(payload, path)
        source_epoch = _source_epoch(payload)
        row = dict(payload)
        row["_path"] = path
        row["_epoch"] = epoch
        row["_source_epoch"] = source_epoch
        previous = by_id.get(memory_id)
        if previous is None or epoch >= int(previous["_epoch"]):
            by_id[memory_id] = row
    return list(by_id.values())


def _weighted_terms(record: Mapping[str, Any]) -> Counter[str]:
    terms: Counter[str] = Counter()
    for field, weight in (
        ("title", 4),
        ("summary", 3),
        ("text", 2),
        ("domain", 4),
        ("subtopic", 4),
        ("type", 2),
    ):
        terms.update({token: weight for token in _lexical_features(record.get(field))})
    for token in _lexical_features(" ".join(str(tag) for tag in record.get("tags") or ())):
        terms[token] += 4
    return terms


def _candidate_provenance(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("provenance")
    if not isinstance(raw, Mapping):
        return {}
    provenance: dict[str, Any] = {}
    for key in (
        "source_id",
        "source_type",
        "span_hash",
        "source_content_hash",
        "source_session_id",
        "source_session_timestamp",
    ):
        value = str(raw.get(key) or "").strip()
        if value:
            provenance[key] = value
    ordinal = raw.get("write_ordinal")
    if isinstance(ordinal, int) and not isinstance(ordinal, bool) and ordinal >= 1:
        provenance["write_ordinal"] = ordinal
    return provenance


def _provenance_position(
    record: Mapping[str, Any],
) -> tuple[str, int] | None:
    """Return a source-ledger-bound session position, when one is available."""

    provenance = record.get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    required = (
        "source_id",
        "span_hash",
        "source_content_hash",
        "source_session_id",
        "source_session_timestamp",
    )
    if not all(str(provenance.get(key) or "").strip() for key in required):
        return None
    if not all(
        _SHA256_RE.fullmatch(str(provenance.get(key) or "").strip())
        for key in ("span_hash", "source_content_hash")
    ):
        return None
    ordinal = provenance.get("write_ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
        return None
    return str(provenance["source_session_id"]), ordinal


def _has_cryptographic_source_binding(
    record: Mapping[str, Any],
    *,
    root: Path,
) -> bool:
    """Verify source hashes, session metadata, and the immutable claim receipt."""

    if _provenance_position(record) is None:
        return False
    provenance = record["provenance"]
    if not isinstance(provenance, Mapping):
        return False
    try:
        from memory.projection_receipt import verify_projection_receipt
        from memory.source_ledger.store import read_source

        artifact = read_source(str(provenance["source_id"]), root=root)
    except (ImportError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if artifact is None:
        return False
    if (
        artifact.source_id != str(provenance["source_id"])
        or artifact.source_session_id != str(provenance["source_session_id"])
        or artifact.source_session_timestamp != str(provenance["source_session_timestamp"])
    ):
        return False
    if not 0 <= artifact.span_start <= artifact.span_end <= len(artifact.content):
        return False
    content_hash = hashlib.sha256(artifact.content.encode("utf-8")).hexdigest()
    span = artifact.content[artifact.span_start : artifact.span_end]
    span_hash = hashlib.sha256(span.encode("utf-8")).hexdigest()
    if (
        content_hash != artifact.content_hash
        or span_hash != artifact.span_hash
        or artifact.content_hash != str(provenance["source_content_hash"])
        or artifact.span_hash != str(provenance["span_hash"])
    ):
        return False
    try:
        return bool(verify_projection_receipt(record, root=root).ok)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _candidate_from_record(
    record: Mapping[str, Any],
    *,
    root: Path,
    score: float,
) -> dict[str, Any]:
    ingest_epoch = int(record["_epoch"])
    epoch = int(record["_source_epoch"]) if record["_source_epoch"] is not None else ingest_epoch
    path = Path(record["_path"])
    candidate: dict[str, Any] = {
        "memory_id": str(record["id"]),
        "score": float(score),
        "title": str(record.get("title") or ""),
        "summary": str(record.get("summary") or record.get("text") or ""),
        "coord": list(record.get("coord") or ()),
        "domain": str(record.get("domain") or ""),
        "subtopic": str(record.get("subtopic") or ""),
        "tags": list(record.get("tags") or ()),
        "contradiction": record.get("contradiction") or record.get("contradiction_state"),
        "version": int(record.get("version") or 1),
        "path": path.relative_to(root).as_posix(),
        "filesystem_fallback": True,
        "temporal_epoch_us": ingest_epoch,
        "source_temporal_epoch_us": epoch,
    }
    flags = record.get("flags") if isinstance(record.get("flags"), Mapping) else {}
    candidate["flagged"] = bool(flags.get("flagged") or record.get("flagged"))
    candidate["flag_reason"] = flags.get("flag_reason") or record.get("flag_reason")
    candidate["flag_response_mode"] = flags.get("flag_response_mode") or record.get(
        "flag_response_mode"
    )
    provenance = _candidate_provenance(record)
    if provenance:
        candidate["provenance"] = provenance
    workspace_source = record.get("workspace_source")
    if isinstance(workspace_source, Mapping):
        candidate["workspace_source"] = dict(workspace_source)
    return candidate


def _source_session_id(record: Mapping[str, Any]) -> str | None:
    provenance = record.get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    session_id = str(provenance.get("source_session_id") or "").strip()
    return session_id or None


def _fuse_semantic_value(
    ranked: list[tuple[float, int, dict[str, Any]]],
    scores_by_id: dict[str, float],
    similarity: Mapping[str, float],
) -> list[tuple[float, int, dict[str, Any]]]:
    """Add the retina's similarity to every admitted row's score, as VALUE.

    z-score fusion at weight ``_SEMANTIC_VALUE_WEIGHT``: both signals are
    standardised over the admitted pool of THIS query, so a row one standard
    deviation nearer the question by meaning gains one standard deviation of
    score. Rows the retina has no code for keep their lexical score (z = 0);
    a cold retina (empty map) changes nothing.
    """
    if not similarity or len(ranked) < 2 or _SEMANTIC_VALUE_WEIGHT <= 0:
        return ranked
    scores = [item[0] for item in ranked]
    sims = [similarity.get(str(item[2].get("memory_id") or "")) for item in ranked]
    known = [value for value in sims if value is not None]
    if len(known) < 2:
        return ranked
    mean_score = sum(scores) / len(scores)
    sd_score = math.sqrt(sum((value - mean_score) ** 2 for value in scores) / len(scores))
    mean_sim = sum(known) / len(known)
    sd_sim = math.sqrt(sum((value - mean_sim) ** 2 for value in known) / len(known))
    if sd_score <= 0.0 or sd_sim <= 0.0:
        return ranked
    gain = _SEMANTIC_VALUE_WEIGHT * sd_score / sd_sim
    fused: list[tuple[float, int, dict[str, Any]]] = []
    admitted_ids: set[str] = set()
    for (score, order, candidate), sim in zip(ranked, sims, strict=True):
        memory_id = str(candidate.get("memory_id") or "")
        new_score = score if sim is None else score + gain * (sim - mean_sim)
        candidate["score"] = new_score
        scores_by_id[memory_id] = new_score
        admitted_ids.add(memory_id)
        fused.append((new_score, order, candidate))
    # Companion rows transported later carry their own score from scores_by_id;
    # value the non-admitted records on the same scale as the anchors.
    for memory_id, score in list(scores_by_id.items()):
        sim = similarity.get(memory_id)
        if sim is not None and memory_id not in admitted_ids:
            scores_by_id[memory_id] = score + gain * (sim - mean_sim)
    fused.sort(key=lambda item: (item[0], item[1], item[2]["memory_id"]), reverse=True)
    return fused


def _diversify_ranked_sessions(
    ranked: list[tuple[float, int, dict[str, Any]]],
    *,
    records: list[dict[str, Any]],
    limit: int,
) -> list[tuple[float, int, dict[str, Any]]]:
    """Defer repeated same-session anchors while retaining them as backfill."""

    records_by_id = {str(record["id"]): record for record in records}
    per_session_limit = min(3, max(1, limit // 2))
    session_counts: Counter[str] = Counter()
    primary: list[tuple[float, int, dict[str, Any]]] = []
    deferred: list[tuple[float, int, dict[str, Any]]] = []
    for item in ranked:
        candidate = item[2]
        record = records_by_id.get(str(candidate["memory_id"]))
        session_id = _source_session_id(record or {})
        if session_id is not None and session_counts[session_id] >= per_session_limit:
            deferred.append(item)
            continue
        primary.append(item)
        if session_id is not None:
            session_counts[session_id] += 1
    return [*primary, *deferred]


# How far an anchor may walk through its own proven, contiguous session to carry
# companion turns into evidence. An exchange routinely states a topic and answers
# it several turns later, so a fixed +/-1 window drops the answer-bearing turn
# whenever it does not itself match the query. The walk halts at the first
# missing or unproven ordinal, so this bounds effort without letting an anchor
# reach unrelated turns that merely share a session id.
_SESSION_CONTEXT_RADIUS = 3

# Collection questions invert the depth/breadth trade. Session transport and
# cross-session coverage draw on ONE evidence budget, so walking each anchor
# deep through its own session crowds out later sessions and undercounts. For a
# count the claim itself is the evidence, not the exchange around it, so the
# walk is narrowed and the freed budget buys more distinct sessions. Durations
# ("how many days") are excluded: those are single-fact questions, not
# collections.
_COLLECTION_RX = re.compile(
    r"\bhow many\b(?!\s+(?:days?|weeks?|months?|years?|hours?|minutes?|times))"
    r"|\bhow many different\b"
    r"|\blist (?:all|every|each)\b"
    r"|\b(?:all|every) (?:the )?(?:different|distinct)\b",
    re.IGNORECASE,
)
_COLLECTION_CONTEXT_RADIUS = 1

# Transport competes with ranking for one window. A per-anchor radius alone lets
# the first few anchors spend the whole budget on their own neighbours: measured
# on a live case, 24 of 28 admitted rows were companions and only 4 were ranked
# anchors, so the record that would have answered the question could not fit.
# Companions are therefore capped as a FRACTION of the window -- transport can
# still rescue a buried answer, but never at the cost of the ranking that found
# the anchors in the first place.
#
# The share cannot go arbitrarily low: an answer-bearing turn sitting a full
# radius from its anchor needs that many companion slots to be reachable at all,
# so too tight a cap silently re-breaks the case transport exists to fix. Half
# the window is the balance point -- it admits a full-radius walk while
# guaranteeing ranked anchors keep at least half the evidence.
_MAX_CONTEXT_SHARE = 0.5


def _promote_anchored_day(
    ranked: list[tuple[float, int, dict[str, Any]]],
    anchored_ids: set[str],
    *,
    limit: int,
) -> list[tuple[float, int, dict[str, Any]]]:
    """Reserve the front of the ranking for the resolved anchor day.

    Bounded to half the window so a mis-resolved anchor degrades recall instead
    of destroying it: the remaining half still comes from ordinary ranking. Order
    within each pool is preserved, so the day's own turns still arrive best-first.
    """

    # Half the window. A single resolved day is a tiny slice of a months-long
    # corpus, so this normally admits the day WHOLE -- which is the point: the
    # answer-bearing turn is routinely the day's weakest lexical match (it
    # answers the question in different words), so a quota that only takes the
    # day's best-scoring turns reproduces the original failure.
    quota = max(1, limit // 2)
    promoted: list[tuple[float, int, dict[str, Any]]] = []
    rest: list[tuple[float, int, dict[str, Any]]] = []
    for item in ranked:
        if len(promoted) < quota and str(item[2].get("memory_id") or "") in anchored_ids:
            promoted.append(item)
        else:
            rest.append(item)
    return [*promoted, *rest]


# A semantically-admitted row has no lexical overlap by construction, so its
# raw score is ~0 and it sorts to the bottom of `ranked` -- exactly where
# `_select_with_adjacent_context`'s fixed-budget loop cuts it. An additive
# score bonus cannot fix this: the gap to a real lexical hit is 20+ points, and
# closing it would distort every other ranking. Score inheritance does not
# apply either -- that design's seeds were the top of `ranked` (lexically
# strong); ours are semantic and score ~0, so there is nothing to inherit.
# Reserving slots is the only lever that survives truncation without touching
# score. 0.25 is a budget SHARE, not a per-case tuned constant: 32 slots at the
# eval's limit=128, 10 at the MCP default limit=40.
_SEMANTIC_RESERVE_SHARE = 0.25


def _reserve_semantic_rows(
    selected: list[dict[str, Any]],
    ranked_by_id: Mapping[str, dict[str, Any]],
    ranked_semantic_ids: Sequence[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Top up the FINAL selection with up to R semantically-admitted rows.

    Runs strictly AFTER `_select_with_adjacent_context` has already made its
    own merit and session-transport decisions, on that call's OUTPUT -- not by
    reordering `ranked` beforehand. An earlier revision reordered `ranked` to
    move reserved rows to its front, ahead of `_promote_anchored_day`'s own
    front reservation; measured on eac54add, that starved the anchor day's
    session-context transport of its shared companion budget and dropped one
    of its answer-bearing companions (6 reached evidence -> 5). Running as a
    pure post-hoc top-up leaves anchor promotion and companion transport their
    full, undisturbed budget; reservation only asks "is this admitted row
    already present, and if not, is there room" once that is all settled.

    Filled in SEMANTIC PRIORITY ORDER (`ranked_semantic_ids`), not any
    position in `ranked`, so a strong cosine seed is reserved before a weak
    closure row when the budget cannot hold every admitted id.

    When there is no free slot, a reserved row displaces the current WEAKEST
    row in `selected` (which is already best-first, via `_by_merit`) -- except
    a row carrying `context_for_memory_id`. That marks a row transported in
    specifically to accompany another ranked row into evidence (see
    `_select_with_adjacent_context`); evicting it on a raw-score comparison
    alone would undo the very mechanism reservation must not touch. If every
    current row is such a companion, that reserved row does not fit -- this
    never pads the result past `limit`.
    """

    if not ranked_semantic_ids or limit <= 0:
        return selected
    quota = max(1, int(limit * _SEMANTIC_RESERVE_SHARE))
    present = {str(row.get("memory_id") or "") for row in selected}
    missing: list[dict[str, Any]] = []
    for memory_id in ranked_semantic_ids:
        if len(missing) >= quota:
            break
        if memory_id in present:
            continue
        candidate = ranked_by_id.get(memory_id)
        if candidate is not None:
            missing.append(candidate)
            present.add(memory_id)
    if not missing:
        return selected

    # Evictions are computed as ONE batch against the ORIGINAL `selected`, not
    # one candidate at a time against a list that keeps growing. A per-
    # candidate loop that always evicts from the current tail cannibalises
    # itself: each freshly-appended reserved row (low score, no
    # `context_for_memory_id`) becomes the very next iteration's eviction
    # target, so only the LAST candidate processed would ever have survived.
    # Measured on gpt4_f2262a51 with that shape: 19/19 admitted, 6/19 reached
    # evidence -- the reservation quota was being spent purely on self-eviction.
    room = max(0, limit - len(selected))
    to_append, overflow = missing[:room], missing[room:]
    if overflow:
        # `selected` is already best-first (via `_by_merit`); the weakest
        # rows sit at its tail. A row carrying `context_for_memory_id` is
        # exempt -- it was transported in specifically to accompany another
        # ranked row into evidence, and evicting it on a raw-score comparison
        # alone would undo the very mechanism reservation must not touch.
        evictable = [
            index for index, row in enumerate(selected) if not row.get("context_for_memory_id")
        ]
        drop = set(evictable[-len(overflow) :])
        selected = [row for index, row in enumerate(selected) if index not in drop]
        # Fewer evictable rows than needed: only that many reserved rows fit.
        # This never pads the result past `limit`.
        overflow = overflow[: len(drop)]
    result = [*selected, *to_append, *overflow]
    result.sort(key=lambda row: float(row.get("score") or 0.0), reverse=True)
    return result


def _select_with_adjacent_context(
    ranked: list[tuple[float, int, dict[str, Any]]],
    *,
    records: list[dict[str, Any]],
    root: Path,
    limit: int,
    evidence_limit: int | None = None,
    context_radius: int = _SESSION_CONTEXT_RADIUS,
    scores_by_id: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Interleave ranked anchors with their proof-bearing session neighbors.

    Neighbors are admitted by walking outward from the anchor through
    consecutive write ordinals in the same session. Contiguity is the
    admission rule: the walk stops at the first ordinal that is absent or
    lacks cryptographic source binding.
    """

    records_by_id = {str(record["id"]): record for record in records}
    ranked_by_id = {str(candidate["memory_id"]): candidate for _s, _e, candidate in ranked}
    positions: dict[tuple[str, int], dict[str, Any]] = {}
    ambiguous_positions: set[tuple[str, int]] = set()
    for record in records:
        position = _provenance_position(record)
        if position is None or position in ambiguous_positions:
            continue
        if position in positions:
            positions.pop(position)
            ambiguous_positions.add(position)
            continue
        positions[position] = record
    source_binding_cache: dict[str, bool] = {}

    def has_source_binding(record: Mapping[str, Any]) -> bool:
        memory_id = str(record.get("id") or "")
        if memory_id not in source_binding_cache:
            source_binding_cache[memory_id] = _has_cryptographic_source_binding(
                record,
                root=root,
            )
        return source_binding_cache[memory_id]

    def _by_merit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Transport interleaves each anchor with its neighbours so an exchange
        # stays contiguous while it is being COLLECTED. The model reads a flat
        # list, though, so emitting that collection order let a companion of the
        # best anchor occupy rank 1 on adjacency alone. Order by merit at the end;
        # membership is transport's decision, rank is the scorer's.
        return sorted(rows, key=lambda row: float(row.get("score") or 0.0), reverse=True)

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    # Against the EVIDENCE window, never the retrieval pool. The caller fetches
    # 4x what the model reads and keeps the head, so a pool-sized budget hands
    # companions twice their intended share of the only rows that matter --
    # measured in production AFTER this cap shipped: 24 of 28 evidence rows were
    # still companions and only 4 were ranked anchors.
    context_window = limit if evidence_limit is None else min(max(evidence_limit, 0), limit)
    context_budget = max(1, int(context_window * _MAX_CONTEXT_SHARE))
    context_used = 0
    for _score, _epoch, candidate in ranked:
        memory_id = str(candidate["memory_id"])
        if memory_id in selected_ids:
            continue
        selected.append(candidate)
        selected_ids.add(memory_id)
        if len(selected) >= limit:
            return _by_merit(selected)
        if context_used >= context_budget:
            # Budget spent: keep admitting ranked anchors, carry no more context.
            continue

        anchor = records_by_id.get(memory_id)
        position = _provenance_position(anchor or {})
        if (
            position is None
            or position in ambiguous_positions
            or not has_source_binding(anchor or {})
        ):
            continue
        session_id, ordinal = position
        for direction in (-1, 1):
            for distance in range(1, context_radius + 1):
                neighbor_ordinal = ordinal + direction * distance
                neighbor = positions.get((session_id, neighbor_ordinal))
                if neighbor is None or not has_source_binding(neighbor):
                    # A missing or unproven ordinal ends this direction. Walking
                    # past it would let an anchor reach arbitrary far turns that
                    # share a session id but not a proven, contiguous exchange.
                    break
                neighbor_id = str(neighbor["id"])
                if neighbor_id not in selected_ids:
                    # Value the companion on its own match, not on its anchor's.
                    # Inheritance existed to rescue neighbours into a 28-row
                    # window; with the full verified pool reaching the model they
                    # arrive regardless, and inheriting a rank they did not earn
                    # put same-session filler above rows that answered the query.
                    # The epsilon only breaks ties among equally scoreless turns,
                    # keeping an exchange in reading order.
                    ranked_row = ranked_by_id.get(neighbor_id)
                    if ranked_row is not None:
                        # A companion that is ALSO a ranked candidate keeps the
                        # ranked row itself: its own score and its per-row marks
                        # (``semantic_recall`` from the retina admission above
                        # all). Rebuilding it here dropped those marks, and the
                        # flagged ranked row was then skipped as already
                        # selected. Live 2026-09-06: "what's the name of my pet?"
                        # — the retina admitted both dog records, transport
                        # rebuilt them unmarked, and the verifier's lexical
                        # topicality floor rejected them as atopical.
                        context = dict(ranked_row)
                    else:
                        own_score = (scores_by_id or {}).get(neighbor_id)
                        context = _candidate_from_record(
                            neighbor,
                            root=root,
                            score=(
                                own_score if own_score is not None else max(-0.001 * distance, -1.0)
                            ),
                        )
                    context["context_for_memory_id"] = memory_id
                    context["context_distance"] = distance
                    selected.append(context)
                    selected_ids.add(neighbor_id)
                    context_used += 1
                    if len(selected) >= limit:
                        return _by_merit(selected)
                    if context_used >= context_budget:
                        break
    return _by_merit(selected)


def search_canonical_files(
    profile_root: Path | str,
    query: str,
    *,
    limit: int = 8,
    evidence_limit: int | None = None,
    restrict_to: Collection[str] | None = None,
    include_ids: Collection[str] | None = None,
    allowed_lobes: Collection[int] | None = None,
    denied_lobes: Collection[int] | None = None,
    allowed_subdomains: Collection[int] | None = None,
    truncation_receipts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Score canonical files against a query, over the corpus or a recall lane's hits.

    The result shape is accepted by ``memory.recall_verifier.verify_candidates``.
    No result from this function is safe to expose before that verifier runs.

    ``limit`` sizes the pool handed to the verifier; ``evidence_limit`` is how
    many of those rows the answering model actually reads. Budgets that govern
    what the model sees -- session transport above all -- must be sized against
    the latter.

    ``restrict_to`` turns this into a deterministic RERANKER: the filesystem is
    truth, DME is recall, so when recall is warm the lane revalues exactly the
    rows recall surfaced by scoring them against their full canonical records,
    instead of running a rival retrieval and forcing a fusion to arbitrate. Corpus
    statistics are always computed over the WHOLE corpus -- inverse document
    frequency measures how rare a term is across everything ingested, so deriving
    it from the candidate subset would make a term's weight depend on how many
    rows recall happened to return. Proven session neighbours may still be
    transported in: contiguity is a filesystem-truth property, not a recall guess.

    ``include_ids`` carries the semantic/relational recall lane's hits into the
    SAME ranking. They are scored identically but exempt from the LEXICAL gates,
    because a relational hit routinely shares no content word with the question --
    that is the whole reason semantic recall exists, and letting the lexical gate
    veto it is the lane rivalry this design removes. Recall decides MEMBERSHIP,
    lexical decides VALUE, the verifier decides truth. Temporal cutoffs still
    apply to them: those are correctness, not relevance.
    """

    root = Path(profile_root).resolve()
    query_terms = _expanded_query_terms(query)
    if not query_terms or limit <= 0:
        return []
    records = _canonical_records(
        root,
        allowed_lobes=allowed_lobes,
        denied_lobes=denied_lobes,
        allowed_subdomains=allowed_subdomains,
    )
    if not records:
        return []

    global _LAST_SEMANTIC_ADMISSION
    try:
        _semantic = semantic_admission(
            records,
            query,
            k=max(_SEMANTIC_SEED_COUNT, math.ceil(_SEMANTIC_SEED_SHARE * len(records))),
        )
    except Exception as exc:  # never let recall die for want of a semantic lane
        _semantic = SemanticAdmission(frozenset(), 0, 0, 0, 0.0, 0, f"error:{type(exc).__name__}")
    _LAST_SEMANTIC_ADMISSION = _semantic

    weighted = [_weighted_terms(record) for record in records]
    document_frequency: Counter[str] = Counter()
    for terms in weighted:
        document_frequency.update(set(terms))
    average_length = sum(sum(terms.values()) for terms in weighted) / len(weighted)
    average_length = max(average_length, 1.0)
    epochs = [
        int(record["_source_epoch"])
        if record["_source_epoch"] is not None
        else int(record["_epoch"])
        for record in records
    ]
    oldest, newest = min(epochs), max(epochs)
    epoch_span = max(newest - oldest, 1)
    query_set = set(query_terms)
    query_core_terms = _query_core_terms(query)
    recent_intent = bool(query_set & _RECENT_WORDS)
    early_intent = bool(query_set & _EARLY_WORDS)
    state_intent = bool(query_set & _STATE_RELATIONS)
    as_of_epoch_us = _as_of_epoch_us(query)
    preferred_role = _preferred_source_role(query)
    query_route = route_query(query)
    # A resolved relative-time anchor ("four weeks ago" -> 2023/02/28) names the
    # day the answer was written, but the day's turns often share no content
    # word with the question ("significant business milestone" vs "collaborated
    # with an influencer"). Clearing the core-term gate only makes those turns
    # eligible; without a score they still never reach the evidence window. Rank
    # the anchored day by its own selectivity, on the same IDF scale as any
    # other term, so a day holding a handful of turns lifts them decisively and
    # a day holding most of the corpus barely moves them.
    anchor_window = _anchor_date_tokens(query)
    anchor_date_tokens = frozenset(anchor_window)
    anchor_bonus = 0.0
    scored_terms = query_terms
    if anchor_date_tokens:
        # DOCUMENT frequency of the UNION: a record is counted once however many
        # anchor dates it carries. Summing per-token counts double-counts records,
        # can exceed len(records), and then inverts the numerator -- turning the
        # bonus NEGATIVE (two dates over 500 records each scored -0.14) and
        # eventually driving math.log below its domain.
        matching = sum(1 for record in records if anchor_date_tokens & _record_date_tokens(record))
        if matching:
            anchor_bonus = math.log(1.0 + (len(records) - matching + 0.5) / (matching + 0.5))
        # REPLACE the date's ordinary term contributions rather than stacking on
        # top of them. expand_query_with_temporal_anchor appends "2023/02/28",
        # which tokenises to '2023','02','28' -- each already scoring against
        # every record title that shares a year, month number or day number.
        # Leaving those in alongside the fused bonus counts the same temporal
        # evidence twice and lets a same-day off-topic turn outrank a strong
        # lexical match. The fused bonus is the single temporal signal.
        components: set[str] = set()
        for year, month, day in _DATE_LITERAL_RE.findall(query):
            components.update(
                {
                    year,
                    month,
                    day,
                    str(int(month)),
                    str(int(day)),
                    f"{int(month):02d}",
                    f"{int(day):02d}",
                }
            )
        scored_terms = [term for term in query_terms if term not in components]

    ranked: list[tuple[float, int, dict[str, Any]]] = []
    anchored_ids: set[str] = set()
    scores_by_id: dict[str, float] = {}
    # Scoring is scoped to the recall lane's hits when reranking; corpus
    # statistics above are not, and must not be.
    scored_ids = None if restrict_to is None else {str(item) for item in restrict_to}
    # The semantic lane joins the SAME membership set the DME lane feeds, so a
    # semantically-seeded row takes the existing bypass at 1237/1240 rather than
    # needing a second one.
    recalled_ids = (
        frozenset() if include_ids is None else {str(item) for item in include_ids}
    ) | _semantic.ids
    for record, terms in zip(records, weighted, strict=True):
        record_id = str(record.get("id") or "")
        if scored_ids is not None and record_id not in scored_ids:
            continue
        recalled = record_id in recalled_ids
        # Gates decide RANKED MEMBERSHIP; the score is computed for every record
        # regardless, so a transported companion can be valued on its own merit
        # instead of inheriting the rank of the row it happens to sit beside.
        admitted = True
        length = max(sum(terms.values()), 1)
        lexical = 0.0
        matched = 0
        for token in scored_terms:
            frequency = terms.get(token, 0)
            if not frequency:
                continue
            matched += 1
            inverse_document_frequency = math.log(
                1.0
                + (len(records) - document_frequency[token] + 0.5)
                / (document_frequency[token] + 0.5)
            )
            denominator = frequency + 1.2 * (1.0 - 0.75 + 0.75 * length / average_length)
            lexical += inverse_document_frequency * frequency * 2.2 / denominator
        in_window = anchor_date_tokens & _record_date_tokens(record)
        on_anchored_day = bool(anchor_bonus) and bool(in_window)
        if on_anchored_day:
            anchored_ids.add(str(record.get("id") or ""))
            # Decay with distance from the resolved day: the anchor is the best
            # guess, the neighbours are the slack in how people say "four weeks
            # ago". Same-day keeps the full bonus, so tolerance never costs the
            # resolved day its lead.
            distance = min(anchor_window[token] for token in in_window)
            lexical += anchor_bonus / (1.0 + distance)
            # The anchor is a MATCH, not just a score. Coverage divides by the
            # query's term count, so leaving this uncounted gives a record that
            # matched the day and nothing else coverage 0 -- sinking the very
            # record the anchor resolved to, bonus and all.
            matched += 1
        elif matched == 0 and not recalled:
            admitted = False
        if (
            not recalled
            and not on_anchored_day
            and query_core_terms
            and not (query_core_terms & _record_core_terms(record))
        ):
            admitted = False
        # The resolved anchor counts as one query term on both sides of the
        # ratio, so coverage stays bounded by 1 and comparable across records.
        coverage = matched / max(len(scored_terms) + (1 if anchor_date_tokens else 0), 1)
        epoch = (
            int(record["_source_epoch"])
            if record["_source_epoch"] is not None
            else int(record["_epoch"])
        )
        if as_of_epoch_us is not None and epoch >= as_of_epoch_us:
            continue
        recency = (epoch - oldest) / epoch_span
        temporal = 0.0
        if early_intent:
            # An explicit first/earliest/original marker sets the DIRECTION of
            # the temporal term; a state relation only ever set its STRENGTH.
            # Testing state_intent first made this branch unreachable whenever
            # both fired ("what was the FIRST car I OWNED" mints
            # relation_ownership), so the largest term in this function applied
            # a 4.0x RECENCY boost to a question asking for the oldest value and
            # buried the correct record. Keep the state-relation magnitude, and
            # invert it.
            temporal = (4.0 if state_intent else 1.0) * (1.0 - recency)
        elif state_intent:
            temporal = 4.0 * recency
        elif recent_intent:
            temporal = recency
        role_authority = (
            5.0
            if preferred_role is not None and _record_source_role(record) == preferred_role
            else 0.0
        )
        answer_authority = sum(
            weight
            for feature, weight in _ANSWER_FEATURE_WEIGHTS.items()
            if feature in query_set and feature in terms
        )
        score = (
            lexical
            + coverage
            + temporal
            + role_authority
            + answer_authority
            + _coordinate_bonus(query_route, record.get("coord"))
        )
        scores_by_id[record_id] = score
        if not admitted:
            continue
        candidate = _candidate_from_record(record, root=root, score=score)
        if record_id in _semantic.ids:
            # Carried on the row itself so the verifier can tell WHY this is
            # a candidate, without the caller asserting it out of band.
            candidate["semantic_recall"] = True
        temporal_order = -epoch if early_intent else epoch
        ranked.append((score, temporal_order, candidate))

    ranked.sort(
        key=lambda item: (item[0], item[1], item[2]["memory_id"]),
        reverse=True,
    )
    if truncation_receipts is not None:
        from memory.recall_truncation import build_truncation_receipt

        truncation_receipts.append(
            build_truncation_receipt(
                "canonical_lexical_admission",
                [str(record.get("id") or "") for record in records],
                [str(item[2].get("memory_id") or "") for item in ranked],
                details={
                    "scoring_filters": "lexical_or_semantic_admission_and_temporal_cutoff",
                    "restricted_pool": scored_ids is not None,
                },
            )
        )
    ranked = _fuse_semantic_value(ranked, scores_by_id, getattr(_semantic, "similarity", {}) or {})
    ranked = _diversify_ranked_sessions(
        ranked,
        records=records,
        limit=limit,
    )
    # A resolved single-day anchor is a CONSTRAINT, not a preference: "what did
    # I mention four weeks ago" is only answerable from that day. A score bonus
    # cannot express that -- measured on case eac54add, the day's IDF is ~3.6
    # while richly-matching off-day turns score 25-31, so the gold turn (which
    # shares no content word with the question) loses by an order of magnitude
    # no matter how the bonus is tuned. Reserve the front of the ranking for the
    # day instead, and run it AFTER session diversification so the per-session
    # cap cannot defer the very turns the anchor resolved to.
    if anchored_ids:
        ranked = _promote_anchored_day(ranked, anchored_ids, limit=limit)
    selected = _select_with_adjacent_context(
        ranked,
        records=records,
        root=root,
        limit=limit,
        evidence_limit=evidence_limit,
        scores_by_id=scores_by_id,
        context_radius=(
            _COLLECTION_CONTEXT_RADIUS
            if _COLLECTION_RX.search(str(query or ""))
            else _SESSION_CONTEXT_RADIUS
        ),
    )
    if truncation_receipts is not None:
        from memory.recall_truncation import build_truncation_receipt

        truncation_receipts.append(
            build_truncation_receipt(
                "canonical_final_evidence_cap",
                [str(item[2].get("memory_id") or "") for item in ranked],
                [str(row.get("memory_id") or "") for row in selected],
                details={
                    "retrieval_limit": limit,
                    # This sizes adjacent-context allowance; it is not itself a
                    # second slice of the verifier's returned candidate pool.
                    "evidence_window_limit": evidence_limit,
                },
            )
        )
    # Reservation runs LAST, on the truncating selector's OUTPUT, never by
    # reordering `ranked` beforehand -- see _reserve_semantic_rows' docstring
    # for the eac54add regression that reordering caused.
    ranked_by_id = {str(item[2].get("memory_id") or ""): item[2] for item in ranked}
    reserved = _reserve_semantic_rows(selected, ranked_by_id, _semantic.ranked_ids, limit=limit)
    if truncation_receipts is not None:
        from memory.recall_truncation import build_truncation_receipt

        truncation_receipts.append(
            build_truncation_receipt(
                "canonical_semantic_reservation",
                [str(row.get("memory_id") or "") for row in selected],
                [str(row.get("memory_id") or "") for row in reserved],
                details={"limit": limit},
            )
        )
    return reserved
