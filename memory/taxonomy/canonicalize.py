"""Shared write+read canonicalization (ratified Task #13 module).

ONE module both the ingest path and the compiler import, so the write-side
and read-side vocabularies can never drift. First resident: deterministic,
MODEL-FREE named-entity extraction — the proper-name grammar that powers the
compiler's memory—entity—memory alias lane (Gate-1 autopsy: alias pairs were
linking only by the accident of shared subdomain placement).

Extraction doctrine mirrors the routing lexicon's proven signal: original
casing is information. Nameish forms: capitalized words, hyphen-digit
compounds (Aegir-9 — strong even lowercase, chat rarely capitalizes),
acronyms (NPU), and mixed-case product names (iPhone). Runs of adjacent
nameish tokens also emit the compound ("kindle paperwhite") so short-name
mentions ("the Kindle") bridge to full titles. Names are emitted normalized
(trim/collapse/lowercase) — the same normalization entity_id_for applies —
so casing variants converge on ONE entity node.

This surface is a FINDING lane, never authority: false entities make sparse
mediators the hop-plane degree cap, score floor, and RecallVerifier absorb.
"""

from __future__ import annotations

import re

from memory.taxonomy.normalize import normalize_id_field

# tokens that capitalize in prose without naming anything
_NAME_STOPWORDS = frozenset(
    """
    the a an and but or nor so yet i we you he she it they me us him her them
    my our your his its their this that these those what where when who whom
    whose which why how if then else not no yes never always please thanks
    thank ok okay oh hey hi hello also meanwhile however moreover because
    while after before during until since once now here there today tomorrow
    tonight yesterday
    monday tuesday wednesday thursday friday saturday sunday
    january february march april may june july august september october
    november december am pm mr mrs ms dr
    """.split()
)

# content-token grammar shared by the relevance floor (write/answer side) and
# the pre-verifier anchor filter (read side) — ONE vocabulary, zero drift.
CONTENT_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "do",
        "does",
        "did",
        "what",
        "who",
        "whom",
        "when",
        "where",
        "why",
        "how",
        "which",
        "i",
        "we",
        "you",
        "he",
        "she",
        "they",
        "it",
        "my",
        "our",
        "your",
        "his",
        "her",
        "their",
        "its",
        "of",
        "on",
        "in",
        "at",
        "for",
        "to",
        "with",
        "about",
        "before",
        "after",
        "while",
        "and",
        "or",
        "not",
        "no",
        "this",
        "that",
        "these",
        "those",
        "there",
        "must",
        "always",
        "never",
        "should",
    }
)


def _stem(word: str) -> str:
    """Deterministic light stemmer — merges inflection families
    (launch/launched/launching, define/defining, check/checks) with no
    lexicon and no model. Applied SYMMETRICALLY to both sides of every
    lexical anchor, so it can only merge related surface forms, never
    manufacture overlap from thin air."""
    stem = word
    for suffix in ("ing", "ed", "es", "s"):
        if stem.endswith(suffix) and len(stem) - len(suffix) >= 3:
            stem = stem[: -len(suffix)]
            break
    if len(stem) >= 4 and stem[-1] == stem[-2]:  # planned/dropped → plan/drop
        stem = stem[:-1]
    if len(stem) >= 4 and stem.endswith("e"):  # define/defining → defin
        stem = stem[:-1]
    return stem


def content_tokens(text: str) -> set:
    """Lowercased, stemmed content words (stopwords removed) — the
    lexical-anchor unit. Possessives normalize to the base word ("omega's" ↔
    "omega"); inflections merge via the light stemmer ("launched" ↔
    "launch", "defining" ↔ "define")."""
    out = set()
    for word in re.findall(r"[a-z][a-z'-]+", str(text or "").lower()):
        word = word.removesuffix("'s").rstrip("'")
        if not word or word in CONTENT_STOPWORDS:
            continue
        word = _stem(word)
        if len(word) >= 2:
            out.add(word)
    return out


# ── question-form canonicalization (ratified Task #13) ──────────────────────
# A question's FORM demands a matching evidence TYPE: a WHEN-question is only
# answerable by temporal payload, a WHO-question by an agent, a WHERE-question
# by location/placement. The gate only ever BLOCKS a non-matching admission
# (→ nonresponsive, preserved for observability) — it can never fabricate and
# never touches abstention. Deterministic lexicons, no models.

_FORM_WHEN = re.compile(
    r"^\s*(?:when\b|what (?:time|year|day|date)\b|how long ago\b)", re.IGNORECASE
)
_FORM_WHERE = re.compile(r"^\s*where\b", re.IGNORECASE)
_FORM_WHO = re.compile(r"^\s*(?:who|whose|whom)\b", re.IGNORECASE)

# hyphen guards: a lexicon word inside a hyphenated NAME compound is not a
# payload ("midnight-sun boat" carries no time; "the midnight watch" does)
_WHEN_PAYLOAD = re.compile(
    r"(?<!-)\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"january|february|march|april|may|june|july|august|september|october|"
    r"november|december|spring|summer|autumn|fall|winter|solstice|equinox|"
    r"yesterday|today|tomorrow|tonight|dawn|dusk|noon|midnight|morning|"
    r"afternoon|evening|overnight|nightly|daily|weekly|monthly|annual(?:ly)?|"
    r"hour|hourly|minute|week|month|year|decade|century|history|"
    r"ago|earlier|later|schedule[ds]?|\d{1,4})\b(?!-)",
    re.IGNORECASE,
)
_WHERE_PAYLOAD = re.compile(
    r"(?<!-)\b(?:north|south|east|west|aboard|ashore|underneath|nearby|"
    r"pier|dock|harbor|harbour|channel|bay|berth|breakwater|boathouse|"
    r"shore|deck|mole|estuary|reef|shelf|room|server|path|directory|"
    r"site|station|location|address"
    # production/containment provenance IS a location answer for artifacts
    # ("where is the atlas" ← "the chart build PRODUCED the atlas")
    r"|produced|emitted|generated|stored|kept|located|lives|resides|"
    r"runs|hosted|built|deployed|saved|written)\b(?!-)"
    r"|\b(?:at|on|in|near|under|inside|outside|behind|beside)\s+(?:the|a|an)\s+\w+",
    re.IGNORECASE,
)
_WHO_PAYLOAD = re.compile(
    r"(?<!-)\b(?:i|we|he|she|they|who(?:ever)?|someone|anyone|everyone|nobody|"
    r"person|people|face|figure|visitor|stranger|crew|team|operator|"
    r"apprentices?|master|keeper|pilots?|captain)\b(?!-)",
    re.IGNORECASE,
)


def question_form(text: str) -> str:
    """The question's evidence-type demand: 'when' | 'where' | 'who' | ''."""
    raw = str(text or "")
    if _FORM_WHEN.search(raw):
        return "when"
    if _FORM_WHERE.search(raw):
        return "where"
    if _FORM_WHO.search(raw):
        return "who"
    return ""


def _has_capitalized_entity(raw: str) -> bool:
    return any(
        _CAPITALIZED.match(tok) and normalize_id_field(tok) not in _NAME_STOPWORDS
        for tok in _TOKEN.findall(raw)
    )


def carries_form_payload(form: str, text: str) -> bool:
    """Does the candidate text carry the payload type the form demands?
    Unknown/empty forms constrain nothing (True). Capitalized named entities
    satisfy WHO (a name IS an agent) and WHERE (a place NAME answers a
    where-question — "at Serenity Yoga" needs no lexicon noun)."""
    raw = str(text or "")
    if form == "when":
        return bool(_WHEN_PAYLOAD.search(raw))
    if form == "where":
        return bool(_WHERE_PAYLOAD.search(raw)) or _has_capitalized_entity(raw)
    if form == "who":
        return bool(_WHO_PAYLOAD.search(raw)) or _has_capitalized_entity(raw)
    return True


_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*")
# at least one hyphen-joined segment carrying a digit: Aegir-9, delta-4, mk-2b
_HYPHEN_DIGIT = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]*\d[A-Za-z0-9]*)+$")
_CAPITALIZED = re.compile(r"^[A-Z][a-z0-9]+(?:-[A-Za-z0-9]+)*$")
_ACRONYM = re.compile(r"^[A-Z]{2,}\d*$")
_MIXED = re.compile(r"^[a-z]+[A-Z][A-Za-z0-9]*$")


def _is_nameish(token: str) -> bool:
    if normalize_id_field(token) in _NAME_STOPWORDS:
        return False
    return bool(
        _HYPHEN_DIGIT.match(token)
        or _CAPITALIZED.match(token)
        or _ACRONYM.match(token)
        or _MIXED.match(token)
    )


def extract_named_entities(text: str) -> tuple[str, ...]:
    """Deterministic named-entity extraction over free text.

    Returns sorted, deduped, normalized names. Runs of adjacent nameish
    tokens (whitespace-only gaps) emit each member AND the joined compound.
    Same input always yields the same tuple — no models, no wall-clock.
    """
    raw = str(text or "")
    toks = [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(raw)]
    names: set[str] = set()
    i, n = 0, len(toks)
    while i < n:
        if not _is_nameish(toks[i][0]):
            i += 1
            continue
        j = i
        while (
            j + 1 < n
            and _is_nameish(toks[j + 1][0])
            and raw[toks[j][2] : toks[j + 1][1]].strip() == ""
        ):
            j += 1
        members = [t[0] for t in toks[i : j + 1]]
        for member in members:
            names.add(normalize_id_field(member))
        if len(members) >= 2:
            names.add(normalize_id_field(" ".join(members)))
        i = j + 1
    return tuple(sorted(names))
