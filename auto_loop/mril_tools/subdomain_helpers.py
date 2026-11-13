"""Deterministic per-lobe subdomain routing for MRIL recall."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from memory.dewey_map import SUBDOMAIN_MAP

_FIRST_PERSON = re.compile(r"\b(i|my|me|mine)\b", re.I)
_SECOND_PERSON = re.compile(r"\b(you|your|yourself)\b", re.I)
_THIRD_PERSON = re.compile(r"\b(he|she|they|him|her|them|his|hers|their|theirs)\b", re.I)

# ── Negation / hypothetical scope guard (Fix 1) ──
# A purely-lexical grammar over-fires on negated or hypothetical keyword
# carriers ("this is NOT a goal" → goals). A match is suppressed (the lobe
# widens) when a negation token sits within ``_NEG_WINDOW`` words BEFORE the
# matched keyword, or when the whole text is hypothetical/counterfactual.
# "cannot" / "must not" are deliberately NOT negation tokens: they are
# affirmative constraint markers and are themselves the matched keyword, so
# they survive. Negation that FOLLOWS a trigger does not suppress it.
_NEG_TOKENS = frozenset(
    {
        "not",
        "no",
        "never",
        "none",
        "neither",
        "nor",
        "nobody",
        "nothing",
        "without",
        "hardly",
        "barely",
        "scarcely",
        "rarely",
    }
)
_HYPOTHETICAL = re.compile(
    r"\b(imagine|suppose|hypothetically|what if|if only|had we|would have)\b"
    # counterfactual FIRST-PERSON disposition + dismissal idiom (stress battery
    # 2026-07-04): "I would never call X lightly" and "it's not like we have a
    # clear goal" are non-assertions — the whole text widens. Third-person
    # "she would never sign off" stays: that IS information about her
    # (redteam pin: test_third_party_never_quote_is_personality).
    r"|\b(?:i|we) (?:would|could) never\b"
    r"|\bit'?s not like\b"
)
_WORD_RE = re.compile(r"[a-z0-9']+")
_NEG_WINDOW = 5
# Look-AHEAD negation is deliberately narrower than look-back: only "never"
# immediately after a trigger noun negates the event itself ("the session
# never started"); a distant/soft negation after a trigger does not suppress
# ("the goal is important, not optional" keeps goals).
_NEG_AFTER_TOKENS = frozenset({"never"})
_NEG_AFTER_WINDOW = 2


def _is_hypothetical(text: str) -> bool:
    return _HYPOTHETICAL.search(text) is not None


def _negated_before(text: str, start: int) -> bool:
    """True if a negation token sits within _NEG_WINDOW words before ``start``.

    Negation does not cross a clause boundary: "the graft did not take AND
    failed" — the 'not' binds to 'take'; 'failed' is asserted (battery-1000
    paraphrase finding). The look-back window resets at a coordinating
    conjunction."""
    tokens = _WORD_RE.findall(text[:start])[-_NEG_WINDOW:]
    for boundary in ("and", "but", "then", "so", "yet"):
        if boundary in tokens:
            tokens = tokens[len(tokens) - tokens[::-1].index(boundary) :]
    return any(tok in _NEG_TOKENS or tok.endswith("n't") for tok in tokens)


def _negated_after(text: str, end: int) -> bool:
    """True if 'never' sits within _NEG_AFTER_WINDOW words after ``end``.

    'never mind' is a dismissal idiom, not negation ("the probe tool works,
    never mind the noise") — battery-1000 false-negative class."""
    tokens = _WORD_RE.findall(text[end:])[: _NEG_AFTER_WINDOW + 1]
    for i, tok in enumerate(tokens[:_NEG_AFTER_WINDOW]):
        if tok in _NEG_AFTER_TOKENS:
            if i + 1 < len(tokens) and tokens[i + 1] == "mind":
                continue
            return True
    return False


def _active(text: str, match: Optional[re.Match]) -> bool:
    """A grammar match counts only if not negated-before and not hypothetical.

    Look-AHEAD negation deliberately does NOT apply here: this guard also
    covers pronoun/relationship SUBJECT matches ("he never told me his real
    name" is still information about HIM — the telling is negated, not the
    fact). Content keyword triggers get the look-ahead via _content_active.
    """
    if match is None:
        return False
    if _is_hypothetical(text):
        return False
    return not _negated_before(text, match.start())


def _content_active(text: str, match: Optional[re.Match]) -> bool:
    """_active plus the 'never'-look-ahead — for content EVENT/topic triggers
    only ("the session never started" negates the session-event itself)."""
    if not _active(text, match):
        return False
    return not _negated_after(text, match.end())


def _hit(text: str, pattern: str) -> bool:
    """re.search guarded by the negation / hypothetical scope rule."""
    return _content_active(text, re.search(pattern, text))


# Pre-index subdomain names per lobe for term-match lobes.
_TERM_MATCH_INDEX: Dict[int, List[Tuple[int, str]]] = {}


def _term_match_index(lobe: int) -> List[Tuple[int, str]]:
    cached = _TERM_MATCH_INDEX.get(lobe)
    if cached is not None:
        return cached
    entries = [(y, name) for (x, y), name in SUBDOMAIN_MAP.items() if x == lobe]
    _TERM_MATCH_INDEX[lobe] = entries
    return entries


def _term_match_subdomain(text: str, lobe: int) -> Optional[int]:
    """Keyword match against SUBDOMAIN_MAP names for ``lobe``.

    An exact full-name match (the subdomain's whole name appears as words) wins
    over a part-word match, so a bare subdomain ("concurrency") is not shadowed
    by a composite ("go_concurrency") that merely shares a word. Among matches
    of equal exactness, the longest keyword wins; ties keep lobe order.
    """
    best_y: Optional[int] = None
    best_len = 0
    best_exact = False
    for y, name in _term_match_index(lobe):
        full = name.replace("_", " ")
        if re.search(rf"\b{re.escape(full)}\b", text):
            cand_len, cand_exact = len(full), True
        else:
            cand_len, cand_exact = 0, False
            for part in name.split("_"):
                p = part.strip().lower()
                if p and re.search(rf"\b{re.escape(p)}\b", text) and len(p) > cand_len:
                    cand_len = len(p)
        if cand_len == 0:
            continue
        if (cand_exact and not best_exact) or (cand_exact == best_exact and cand_len > best_len):
            best_y, best_len, best_exact = y, cand_len, cand_exact
    return best_y


# Kinship / relations — a fact ABOUT another person in the operator's orbit
# lives in the other-person bucket (0,1) regardless of phrasing, so the write
# ("she is my sister") and the recall ("who is the operator's sister") resolve
# the SAME Y (stress-battery symmetry miss, 2026-07-04).
_RELATIONSHIP = re.compile(
    r"\b(mother|father|mom|dad|brother|sister|son|daughter|wife|husband"
    r"|girlfriend|boyfriend|fiance|spouse|friend|colleague|coworker|boss"
    r"|mentor|uncle|aunt|cousin|grandma|grandmother|grandpa|grandfather"
    r"|roommate|neighbor|classmate)s?\b"  # plural/verb-form tolerant ("who mentors me")
)

# ── Discourse-marker normalization (grill 2026-07-04) ──
# Politeness formulas and trailing epistemic hedges are DISCOURSE, not
# content: "could you tell me who is Sarah" is about Sarah (the 'you'
# addresses Omega-as-interlocutor, not omega-as-subject); "Sarah runs the
# lab, i think" is about Sarah (the 'i' is a hedge, not the subject).
_POLITENESS_PREFIX = re.compile(
    r"^\s*(?:hey|hi|ok|okay|so|please)?[,\s]*"
    r"(?:could|can|would|will) you (?:please )?(?:tell|remind|show) me[,\s]*"
    r"|^\s*do you (?:know|remember)[,\s]*"
    r"|^\s*please tell me[,\s]*"
)
_TRAILING_HEDGE = re.compile(
    r"[,;\s]+(?:i think|i guess|i believe|i suppose|imo|in my opinion"
    r"|if i recall|iirc)\s*[.!?]*\s*$"
)


def _strip_discourse(text: str) -> str:
    text = _POLITENESS_PREFIX.sub("", text)
    return _TRAILING_HEDGE.sub("", text)


def _identity_subdomain(text: str) -> Optional[int]:
    if _hit(text, r"\b(traits?|characteristics?)\b"):
        return 2  # emergent_traits
    # evolution before the pronoun fallbacks: "my self-model has evolved"
    # is ABOUT change-over-time, not the constitution ('my' must not capture
    # it — stress-battery silent misroute, 2026-07-04).
    if _hit(text, r"\b(evol(?:ve|ved|ves|ving|ution)|self-model|self model)\b"):
        return 3  # evolution
    if _hit(text, r"\bopen questions?\b"):
        return 5  # self_inquiry
    # Strong rule markers are unambiguous → guardian.
    if _hit(
        text,
        r"\b(rules?|hard line|boundary|boundaries|non-negotiable"
        r"|forbidden|prohibited|must not|may not|off[ -]limits|inviolable"
        # POSITIVE obligations are rules too, not just prohibitions
        # ("you must always verify a backup") — battery-1000 invariant breach
        r"|(?:must|should|shall) always)\b",
    ):
        return 4  # guardian
    # Bare "never" is a rule ONLY when it isn't describing a third party
    # ("you must never X" = rule; "he never told me" = personality, not guardian).
    if _hit(text, r"\bnever\b") and not _THIRD_PERSON.search(text):
        return 4  # guardian
    if _active(text, _SECOND_PERSON.search(text)):
        return 6  # omega
    # A stance verb WITH a first-person pronoun anywhere is the believer's own
    # constitution ("tools outlasting their makers is what i believe in") —
    # belief statements belong to the believer regardless of pronoun position.
    if _hit(
        text,
        r"\b(believe|believes|think|thinks|hold|holds|value|values"
        r"|conviction|convinced)\b",
    ) and _FIRST_PERSON.search(text):
        return 0  # constitution
    # POSITIONAL pronoun resolution: the earliest active personal pronoun is
    # the subject/topic. "she is my sister" → third ("she" leads); "i believe
    # tools should outlast their makers" → first ("i" leads; "their" is an
    # inanimate possessive) — battery-1000 misroute class.
    third_m = _THIRD_PERSON.search(text)
    first_m = _FIRST_PERSON.search(text)
    third_ok = _active(text, third_m)
    first_ok = _active(text, first_m)
    if third_ok and (not first_ok or third_m.start() < first_m.start()):
        return 1  # personality / other-person perspective on identity
    # relationship words without a LEADING first-person subject ("who is the
    # operator's sister") are about the OTHER person.
    if _active(text, _RELATIONSHIP.search(text)) and not (first_ok and first_m.start() == 0):
        return 1
    if first_ok:
        return 0  # constitution (operator)
    if _hit(text, r"\b(operator|principal)\b"):
        return 0  # constitution — operator-as-subject when no pronoun present
    # stance/belief verbs with no pronoun subject ("Noah believes the device
    # should outlive its vendor"): the identity lobe's subject IS the operator
    # by construction — a stance statement here is constitution, not a widen.
    if _hit(text, r"\b(believes?|thinks?|holds?|maintains?|insists?|is committed to)\b"):
        return 0
    return None


_NAME_STOPWORDS = frozenset(
    {
        "i",
        "we",
        "you",
        "he",
        "she",
        "they",
        "it",
        "the",
        "a",
        "an",
        "my",
        "our",
        "your",
        "his",
        "her",
        "their",
        "this",
        "that",
        "these",
        "those",
        "dr",
        "mr",
        "ms",
        "mrs",
        "prof",
    }
)


def _has_proper_name(raw: str) -> bool:
    """A capitalized non-stopword token in the ORIGINAL casing — the persona
    lobe's signal that the text names a person ("Sarah runs the lab",
    "Dr. Lee teaches..."). Case is real information the lowered grammar was
    throwing away; honorifics are stopworded so the NAME token decides."""
    for tok in re.findall(r"[A-Za-z][a-z']*", raw or ""):
        if tok[0].isupper() and tok.lower() not in _NAME_STOPWORDS:
            return True
    return False


def _persona_subdomain(text: str, raw: str = "") -> Optional[int]:
    # POSITIONAL pronoun resolution (see _identity_subdomain).
    third_m = _THIRD_PERSON.search(text)
    first_m = _FIRST_PERSON.search(text)
    third_ok = _active(text, third_m)
    first_ok = _active(text, first_m)
    if third_ok and (not first_ok or third_m.start() < first_m.start()):
        return 1  # person_stub
    # relationship words outrank a NON-LEADING first-person token: "who is my
    # father" asks about the FATHER ("my" is not the subject).
    if _active(text, _RELATIONSHIP.search(text)) and not (first_ok and first_m.start() == 0):
        return 1  # person_stub
    if first_ok:
        return 0  # operator
    # a named person with no pronouns ("Sarah runs the lab next door") is a
    # person_stub — within the personas lobe, a proper name IS the subject.
    if not _is_hypothetical(text) and _has_proper_name(raw):
        return 1
    return None


_GOAL_RE = (
    r"\b(goal|goals|objective|objectives|aim|aims|target"
    r"|want|wants|wanted|intend|intends|aspire|aspires)\b"
)
_DECISION_RE = (
    r"\b(decision|decisions|decide|decided|chose|choose|choice"
    r"|settle|settled|opt|opted|resolve|resolved|picked|went with|going with"
    # tradeoff language IS decision-making ("we weighed latency against
    # privacy and kept it local")
    r"|weighed?|weighing|trade-?offs?)\b"
)


def _reasoning_subdomain(text: str) -> Optional[int]:
    """Lobe 3 — goals/decisions/constraints/heuristics/alignment.

    goals vs decisions is POSITIONAL when both fire: "the goal is to decide X"
    is a goal whose content mentions deciding; "we decided the goal for next
    week" is a decision about a goal. The leading token names the subject
    (stress-battery silent misroute, 2026-07-04). Single-token texts keep the
    original priority order exactly.
    """
    if _hit(
        text,
        r"\b(constraint|constraints|limit|limits|restriction|forbidden|must not|cannot"
        r"|limited to)\b",  # "we are limited to a two-acre water budget"
    ):
        return 2  # constraints
    # STRONG priority phrases are alignment and outrank weak decision tokens:
    # "soil health comes first in every planting CHOICE" is a value statement,
    # not a decision record — battery-1000 invariant breach (12 of 27).
    if _hit(
        text,
        r"\b(matters? (?:more|most)|at all costs|comes? first|takes? priority)\b",
    ):
        return 4  # alignment
    goal_m = re.search(_GOAL_RE, text)
    dec_m = re.search(_DECISION_RE, text)
    goal_ok = _content_active(text, goal_m)
    dec_ok = _content_active(text, dec_m)
    if goal_ok and dec_ok:
        return 0 if goal_m.start() < dec_m.start() else 1
    if dec_ok:
        return 1  # decisions
    if _hit(text, r"\b(heuristic|heuristics|rule of thumb|principle|principles)\b"):
        return 3  # heuristics
    if _hit(text, r"\b(alignment|aligned|align|values)\b"):
        return 4  # alignment
    if goal_ok:
        return 0  # goals
    return None


def _situational_subdomain(text: str) -> Optional[int]:
    """Lobe 4 — outcome/sentiment first, then 5W1H interrogatives."""
    if _hit(
        text,
        r"\b(outcome|result|results|resulted|consequence|consequences"
        r"|in the end|ended up|ended in|led to|wound up)\b",
    ):
        return 6  # outcome
    # success/failure event VERBS are outcomes when STATED ("the deploy failed
    # at 3am and we rolled back") — but a leading interrogative makes it a
    # cause/manner QUESTION and the 5W1H facet wins ("why did it fail" → why).
    if not re.match(r"\s*(why|how|what|when|where|who)\b", text) and _hit(
        text, r"\b(succeed(?:ed|s)?|fail(?:ed|s)?|crashed|rolled back)\b"
    ):
        return 6  # outcome
    if _hit(
        text,
        r"\b(sentiment|feel|feels|felt|feeling|feelings|emotion|emotions|mood"
        r"|frustrated|frustration|annoyed|drained|happy|sad|angry|anxious"
        r"|excited|proud|nervous|upset|stressed|relieved|delighted"
        r"|disappointed|worried"
        # emotion NOUNS ("there was real anxiety before the launch",
        # "there were real nerves ahead of the inspection")
        r"|anxiety|worry|dread|joy|excitement|pride|relief|anger|fear|sadness"
        r"|nerves|morale)\b",
    ):
        return 7  # sentiment
    if _hit(text, r"\bwho\b"):
        return 0
    if _hit(text, r"\bwhat\b"):
        return 1
    if _hit(text, r"\bwhen\b"):
        return 2
    if _hit(text, r"\bwhere\b"):
        return 3
    if _hit(text, r"\bwhy\b"):
        return 4
    if _hit(text, r"\bhow\b"):
        return 5
    return None


def _tools_subdomain(text: str) -> Optional[int]:
    """Lobe 5 — dev-pipeline tools vs cognitive (voice_box-invokable) tools."""
    if _hit(text, r"\b(dev|build|git|compile|pipeline|deploy|coding|cicd|debug(?:ging)?)\b|ci/cd"):
        return 1  # dev
    if _hit(
        text,
        r"\b(cognitive|web|fetch|search|browse|tool|tools"
        # tool-component vocabulary ("the helper that wraps the model endpoint")
        r"|helper|wrapper|wraps?|endpoint|adapter|client|utility)\b",
    ):
        return 0  # cognitive
    return None


def _journal_subdomain(text: str) -> Optional[int]:
    """Lobe 6 — only subdomain is (6,0) deepsleep; any journal text routes there."""
    return 0


def _vision_subdomain(text: str) -> Optional[int]:
    """Lobe 7 — face/scene/pattern before the generic image bucket."""
    if _hit(text, r"\b(face|faces|facial|visage|visages|countenance)\b"):
        return 1  # face_embeddings
    if _hit(text, r"\b(scene|scenes)\b"):
        return 2  # scene_descriptions
    if _hit(text, r"\bpatterns?\b"):
        return 3  # visual_patterns
    if _hit(
        text,
        r"\b(image|images|picture|pictures|photo|photos|photograph"
        # live visual surfaces ("what is on the screen right now")
        r"|screen|display|monitor)\b",
    ):
        return 0  # images
    return None


def _temporal_subdomain(text: str) -> Optional[int]:
    """Lobe 9 — sessions/events/milestones/history."""
    if _hit(text, r"\b(session|sessions)\b"):
        return 0  # sessions
    if _hit(
        text,
        r"\b(milestone|milestones|achievement|achievements|accomplished"
        r"|accomplishment|accomplishments|attained|attainment|breakthrough"
        # release language IS a milestone ("we shipped the first beta",
        # "the cider press order went out monday")
        r"|ship(?:ped|s)?|released?|launch(?:ed|es)?|delivered|went live|went out)\b",
    ):
        return 2  # milestones
    if _hit(text, r"\b(history|historical|timeline|past)\b"):
        return 3  # history
    if _hit(text, r"\b(event|events|happened|occurred)\b"):
        return 1  # events
    # relative-time phrases are temporal signals ("why did the pump fail
    # last night" — battery-1000 recall-lobe finding)
    if _hit(text, r"\b(last (?:night|week|month|time)|yesterday|this morning)\b"):
        return 1  # events
    return None


def _dev_subdomain(text: str) -> Optional[int]:
    """Lobe 11 — artifacts plus verified agent observations.

    The observation buckets are deliberately explicit.  A query for a tree or
    directory must not land in the generic artifact bucket, because that would
    make exact path evidence hard to recall and would force the answer model to
    infer the requested view from unrelated build records.
    """
    if _hit(
        text,
        r"\b(full\s+)?(?:tree|directory(?:\s+tree)?|folder(?:\s+tree)?|paths?)\b"
        r"|\bwhat (?:was|were) in (?:the )?(?:directory|folder|workspace)\b"
        r"|\b(?:files?|entries)\s+(?:(?:are|were|listed|stored)\s+)?(?:in|under|inside)\b",
    ):
        return 5  # full_tree
    if _hit(
        text,
        r"\b(strings?|contents?|text|lines?)\b"
        r"|\bwhat (?:did|has) (?:we|you|i) (?:see|read|find)\b",
    ):
        return 6  # strings
    if _hit(
        text,
        r"\b(?:touch(?:ed)?|change(?:d)?|modif(?:y|ied)|edit(?:ed)?|operations?|commands?|actions?)\b"
        r"|\bwhat (?:did|have) (?:we|you|i) (?:touch|change|modify|edit|run)\b",
    ):
        return 7  # operations
    if _hit(
        text,
        r"\b(?:project|projects|repository|repo|workspace|drive|roots?)\b",
    ):
        return 4  # projects
    if _hit(
        text,
        r"\b(artifact|artifacts|binary|binaries|output|deliverable|deliverables"
        r"|executable|executables"
        # production verbs + build-product nouns ("the build produced a signed
        # APK") — mirrors the router's own 'what did the build produce' recall
        r"|produce[ds]?|generate[ds]?|apk|installer|wheel|came out of"
        # NOUN-position bundle only ("the almanac bundle", "bundle from the
        # build") — the verb ("bundle the assets") is not an artifact
        # (synonym-precision pin)
        r"|(?:the|a|an|fresh|nightly|new|latest|final) \w+ bundle|bundle from)\b",
    ):
        return 0  # artifacts
    if _hit(
        text,
        r"\b(progress|status|update|updates"
        # remaining-work language ("we still need to wire the loader",
        # "the patch is still pending")
        r"|still need|left to|remaining|to-?do|underway|wip|pending)\b",
    ):
        return 1  # progress
    if _hit(text, r"\b(vision|roadmap|plan|plans)\b"):
        return 3  # vision
    if _hit(text, r"\b(context|background|setup)\b"):
        return 2  # context
    return None


def subdomain_for(text: str, lobe: int) -> Optional[int]:
    """Return deterministic subdomain Y for ``lobe``, or None to widen recall."""
    raw = str(text or "").strip()
    # whitespace-normalize BEFORE grammar: multi-space input must not break
    # literal-space phrase patterns ("matters  more") — battery-1000 finding.
    lowered = re.sub(r"\s+", " ", _strip_discourse(raw.lower())).strip()
    raw = _strip_discourse(raw)
    if not lowered:
        return None

    if lobe == 0:
        return _identity_subdomain(lowered)
    if lobe == 1:
        return _persona_subdomain(lowered, raw)
    if lobe in (2, 8, 10):
        return _term_match_subdomain(lowered, lobe)
    if lobe == 3:
        return _reasoning_subdomain(lowered)
    if lobe == 4:
        return _situational_subdomain(lowered)
    if lobe == 5:
        return _tools_subdomain(lowered)
    if lobe == 6:
        return _journal_subdomain(lowered)
    if lobe == 7:
        return _vision_subdomain(lowered)
    if lobe == 9:
        return _temporal_subdomain(lowered)
    if lobe == 11:
        return _dev_subdomain(lowered)
    return None
