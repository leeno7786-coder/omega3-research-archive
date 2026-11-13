"""Dependency-parse fact extraction — the "language cortex" of ingest.

Turns a natural-language statement into structured facet entities by reading the
GRAMMATICAL STRUCTURE of the sentence (subject → verb → object + modifiers), not
by matching surface patterns. One grammar rule covers every phrasing of a fact —
"I own / run / founded / started a business called X" all resolve through the same
verb→object spine — which is the whole point: regex is per-phrasing whack-a-mole;
a parse generalises.

Determinism: spaCy's ``en_core_web_sm`` is a fixed-weight model run at inference
with no sampling — same text ⇒ same parse, every run (verified). That places it in
the SAME category as the SetFit classifier already in the ingest path, so this does
not break the reproducibility invariants; it just swaps a brittle regex for a much
larger deterministic language model.

Ceiling (honest): this reads STRUCTURE. It does not resolve semantics that need
world-knowledge or coreference — "I started it in 2019" (what is "it"?), "three
mouths to feed" ⇒ 3 kids, idiom. Those are the long tail; a clear declarative is
the common case and is fully covered here.

Output shape MATCHES ``personas.enrich`` so the persona-ingest orchestrator consumes
it unchanged: a list of ``{"name", "facet", "confidence"}`` dicts, with relationship
entities additionally carrying ``"linked_name"``.

Failure-soft: if spaCy or the model is unavailable, ``extract_facts`` returns [] so
the caller falls back to the regex enricher rather than crashing ingest.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ── Vocabularies (grammatical anchors, not phrasings) ──────────────────────────
_RELATION_NOUNS = frozenset(
    {
        "son",
        "daughter",
        "child",
        "kid",
        "sister",
        "brother",
        "sibling",
        "mother",
        "mom",
        "mum",
        "father",
        "dad",
        "parent",
        "wife",
        "husband",
        "spouse",
        "partner",
        "boyfriend",
        "girlfriend",
        "fiance",
        "fiancee",
        "cousin",
        "aunt",
        "uncle",
        "niece",
        "nephew",
        "grandmother",
        "grandfather",
        "grandma",
        "grandpa",
        "grandson",
        "granddaughter",
        "friend",
        "roommate",
    }
)
_BUSINESS_NOUNS = frozenset(
    {
        "business",
        "company",
        "startup",
        "firm",
        "shop",
        "store",
        "brand",
        "agency",
        "studio",
        "practice",
        "corporation",
        "nonprofit",
        "restaurant",
        "cafe",
        "bakery",
        "brewery",
        "label",
        "venture",
    }
)
# Verbs that assert ownership/founding of a business (NOT generic "build/make",
# which need the business-noun object to disambiguate "build a network").
_OWN_VERBS = frozenset({"own", "run", "found", "start", "operate", "launch", "manage"})
_EDU_INSTITUTIONS = frozenset({"college", "university", "school", "academy"})
# Academic anchors that make "for/in X" a field of study (vs "go to the store").
_EDU_ANCHORS = _EDU_INSTITUTIONS | frozenset(
    {
        "degree",
        "diploma",
        "doctorate",
        "phd",
        "bachelor",
        "bachelors",
        "master",
        "masters",
        "undergrad",
        "postgrad",
    }
)
_EDU_VERBS = frozenset({"go", "study", "attend", "major", "pursue", "get", "take"})
_PREFERENCE_VERBS = frozenset({"like", "love", "prefer", "enjoy", "favor", "adore"})
# copula attr subjects that mean the operator's OWN name (vs someone else's)
_NAME_SUBJECTS = frozenset({"i", "name"})
# First-person subjects mean the fact is the OPERATOR's; a proper-noun subject means the
# fact is about THAT entity ("Abel likes trains" is Abel's, not the operator's).
_FIRST_PERSON = frozenset({"i", "we", "me", "us", "my", "our", "myself", "ourselves"})

_NLP = None


def _nlp():
    """Load the required spaCy pipeline once and preserve any load failure."""
    global _NLP
    if _NLP is None:
        import spacy

        _NLP = spacy.load("en_core_web_sm")
    return _NLP


# ── Typo-tolerant classifier matching ─────────────────────────────────────────
# A CLASSIFIER word (relation/business/education noun, ownership/edu/preference verb)
# decides a fact's TYPE. A single-char typo ("comany", "sistr", "collge") leaves the
# dependency PARSE intact (spaCy keeps the dobj/acl/pobj roles) but breaks the exact
# vocabulary lookup, so the fact is silently GHOSTED. We fuzzy-match those classifier
# words — but NEVER entities/values (names, business names, fields stay verbatim; we do
# not know a proper noun's "correct" spelling). Two guards keep this from FABRICATING a
# memory out of a real word ("I have a SITTER named Miranda" must NOT become a sister):
#   (1) a real-English-word oracle (pyspellchecker OR wordfreq zipf >= 3.0) — only words
#       that are NOT real words are eligible for correction;
#   (2) a length-scaled Damerau distance with an ambiguity guard — a unique vocab term
#       within one/two edits, or nothing.
# Deterministic; failure-soft — no dictionary installed => every word is treated as real
# => pure exact matching (today's behavior), never a fabricated correction.

_ORACLE = None


def _word_oracle():
    """Lazy real-word oracle (pyspellchecker SpellChecker | None, wordfreq zipf | None).
    None when neither is installed — the caller then treats every word as real."""
    global _ORACLE
    if _ORACLE is None:
        spell = zipf = None
        try:
            from spellchecker import SpellChecker

            spell = SpellChecker()
        except Exception:
            spell = None
        try:
            from wordfreq import zipf_frequency

            zipf = zipf_frequency
        except Exception:
            zipf = None
        _ORACLE = (spell, zipf) if (spell or zipf) else False
    return _ORACLE or None


def _is_real_word(word: str) -> bool:
    """True if *word* is a real English word (so it must NOT be typo-corrected).
    pyspellchecker is precise on common words; wordfreq (zipf >= 3.0) adds technical terms
    its dictionary lacks while excluding low-frequency misspellings ("comany"~1.3). No
    dictionary => True (degrade to exact matching; never risk a fabricated correction)."""
    oracle = _word_oracle()
    if oracle is None:
        return True
    spell, zipf = oracle
    w = word.lower()
    if spell is not None:
        try:
            if spell.known([w]):
                return True
        except Exception:
            pass
    if zipf is not None:
        try:
            if zipf(w, "en") >= 3.0:
                return True
        except Exception:
            pass
    return False


def _damerau(a: str, b: str, cap: int) -> int:
    """Damerau-Levenshtein (optimal string alignment) edit distance, capped — returns
    cap+1 once exceeded. Counts a transposition ("brohter"->"brother") as ONE edit."""
    la, lb = len(a), len(b)
    if abs(la - lb) > cap:
        return cap + 1
    prev2: Optional[list] = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        row_min = i
        ai = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            val = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and ai == b[j - 2] and a[i - 2] == b[j - 1]:
                val = min(val, prev2[j - 2] + 1)
            cur[j] = val
            if val < row_min:
                row_min = val
        if row_min > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[lb] if prev[lb] <= cap else cap + 1


def _vocab_canonical(word: str, vocab: frozenset) -> Optional[str]:
    """Canonical classifier term for *word*: itself if an exact vocab member; else — only
    when *word* is NOT a real English word (a likely typo) — the UNIQUE vocab term within
    a length-scaled Damerau distance (<=3 chars exact-only; 4-7 -> 1 edit; 8+ -> 2). None
    if there is no confident, unambiguous match. Classifier vocab ONLY, never entities."""
    w = (word or "").lower().strip()
    if not w:
        return None
    if w in vocab:
        return w
    if len(w) <= 3 or _is_real_word(w):
        return None
    cap = 1 if len(w) <= 7 else 2
    match: Optional[str] = None
    for term in sorted(vocab):
        if abs(len(term) - len(w)) > cap:
            continue
        if _damerau(w, term, cap) <= cap:
            if match is not None and match != term:
                return None  # ambiguous — refuse to guess
            match = term
    return match


def _vocab_hit(word: str, vocab: frozenset) -> bool:
    """True if *word* classifies into *vocab* (exact or a confident typo correction)."""
    return _vocab_canonical(word, vocab) is not None


def _ent_span(token) -> Optional[str]:
    """If the token falls inside a named entity, return the whole entity text."""
    for ent in token.doc.ents:
        if ent.start <= token.i < ent.end:
            return ent.text
    return None


def _name_span(token) -> str:
    """The full name for a token: its named-entity span if any (so "Omega AI"
    stays whole), else the token plus its left compound modifiers."""
    ent = _ent_span(token)
    if ent:
        return ent
    parts = [w for w in token.lefts if w.dep_ == "compound"] + [token]
    return " ".join(w.text for w in sorted(parts, key=lambda w: w.i))


def _noun_phrase(token) -> str:
    """A common-noun value with its compound/amod modifiers (e.g. "cybersecurity
    student"), determiners dropped."""
    parts = [w for w in token.lefts if w.dep_ in ("compound", "amod")] + [token]
    return " ".join(w.text for w in sorted(parts, key=lambda w: w.i))


def _is_place(token) -> bool:
    """True if the token is (part of) a place — a city/country/region/facility. A place
    is NEVER a field of study ("school IN Phoenix" — Phoenix is a location, not a field)."""
    return token.ent_type_ in ("GPE", "LOC", "FAC")


# Roles held IN an organization. Distinct from _BUSINESS_NOUNS (which name the
# ORG itself): these name a PERSON'S POSITION, so the organization is carried in
# an "of/at" complement and must travel with the role. spaCy splits "co-owner"
# into co/-/owner, so the bare heads are what land here.
_ROLE_NOUNS = frozenset(
    {
        "owner",
        "coowner",
        "founder",
        "cofounder",
        "partner",
        "president",
        "ceo",
        "cto",
        "director",
        "member",
        "shareholder",
        "chair",
        "chairman",
        "chairwoman",
    }
)


_THIRD_PERSON_PRONOUNS = frozenset({"he", "she", "they", "him", "her", "them"})
# Child relations can be stated as a bare COUNT ("2 daughters"); other relations
# in the vocab are only meaningful here when they name someone.
_CHILD_RELATIONS = frozenset({"son", "daughter", "child", "kid"})
# Marital state arrives as a copula acomp ("he's married", "she is divorced").
# Both forms are needed: spaCy lemmatizes the predicate "married" as the
# ADJECTIVE "married" (not the verb "marry"), so a verb-only vocabulary never
# matched and the fact was silently dropped.
_MARITAL_LEMMAS = frozenset(
    {
        "marry",
        "married",
        "divorce",
        "divorced",
        "widow",
        "widowed",
        "engage",
        "engaged",
        "single",
    }
)


def _pronoun_antecedent(pronoun) -> Optional[str]:
    """The most recent PERSON named before a third-person pronoun.

    Deliberately conservative: only PERSON entities, only ones appearing
    earlier in the same text, nearest first. Returns None when nothing was
    named, so the caller keeps its existing behaviour rather than guessing."""
    if pronoun.lemma_.lower() not in _THIRD_PERSON_PRONOUNS:
        return None
    best = None
    for ent in pronoun.doc.ents:
        if ent.label_ == "PERSON" and ent.end <= pronoun.i:
            if best is None or ent.end > best.end:
                best = ent
    return best.text if best is not None else None


def _is_person(token) -> bool:
    """True when a copula subject names a person rather than an organization.

    Lets a third-party statement ("Mitchell Ray is a co-owner of ...") extract
    facts at all — every copula path used to be guarded to a first-person or
    possessive subject, so a sentence about someone else yielded nothing."""
    if token is None:
        return False
    if token.ent_type_ == "PERSON":
        return True
    return token.pos_ == "PROPN" and token.ent_type_ not in ("ORG", "GPE", "LOC", "FAC")


def _role_organization(role_noun) -> Optional[str]:
    """The organization a role is held in — "part owner OF Omega AI LLC"."""
    for p in role_noun.children:
        if p.dep_ == "prep" and p.lemma_ in ("of", "at", "in", "for"):
            for g in p.children:
                if g.dep_ == "pobj" and (g.pos_ == "PROPN" or g.ent_type_ == "ORG"):
                    return _name_span(g)
    return None


def _named_child(noun) -> Optional[str]:
    """Name given to a noun by an "X called/named Y" clause (acl→oprd/dobj/attr)
    or an apposition ("my sister Miranda")."""
    for child in noun.children:
        if child.dep_ == "acl" and child.lemma_ in ("call", "name"):
            for g in child.children:
                if g.dep_ in ("oprd", "dobj", "attr"):
                    return _name_span(g)
        if child.dep_ == "appos" and child.pos_ in ("PROPN", "NOUN"):
            return _name_span(child)
    return None


def _relation_name(noun) -> Optional[str]:
    """The person a relationship noun points to ("son named Abel" → Abel)."""
    return _named_child(noun)


def _dobj(verb):
    for child in verb.children:
        if child.dep_ in ("dobj", "attr", "oprd"):
            yield child


def _prep_pobj(verb, preps: frozenset) -> Optional[str]:
    """Object of a preposition hanging off the verb: work AT google, live IN X."""
    for child in verb.children:
        if child.dep_ == "prep" and child.lemma_ in preps:
            for g in child.children:
                if g.dep_ == "pobj":
                    return _name_span(g) if g.pos_ == "PROPN" else _noun_phrase(g)
    return None


def _business_from_verb(verb, *, allow_propn: bool = True) -> Optional[str]:
    """Business named by a verb: (a) a business-noun object carrying a "called X" name,
    or (b) a proper-noun / ORG object directly (I founded X). allow_propn=False (used for
    the weak possession verb "have") restricts to the business-noun path, so "I have a
    business called X" lands but "I have Google" does not mint a business."""
    for obj in _dobj(verb):
        if _vocab_hit(obj.lemma_, _BUSINESS_NOUNS):
            named = _named_child(obj)
            if named:
                return named
        elif allow_propn and (obj.pos_ == "PROPN" or obj.ent_type_ == "ORG"):
            return _name_span(obj)
    return None


def _shares_entity(a, b) -> bool:
    """True when two tokens fall inside the SAME named-entity span.

    "the University of Phoenix" is ONE ORG entity — every token in it, including
    "Phoenix", carries ``ent_type_ == "ORG"``. So ``_is_place`` (GPE/LOC/FAC)
    cannot tell that "Phoenix" is part of the institution's NAME rather than a
    field of study, and the guard below never fires. Measured live 2026-07-31:
    that is what wrote "operator studies phoenix." and lost both the
    institution and the degree."""
    if not (a.ent_type_ and b.ent_type_):
        return False
    return any(
        ent.start <= a.i < ent.end and ent.start <= b.i < ent.end for ent in a.doc.ents
    )


def _field_phrase(token) -> str:
    """A field of study with its nested qualifier ("Science in Cybersecurity").

    ``_noun_phrase`` stops at the head noun, which silently truncates a degree
    field to its most generic word — "Bachelors in Science in Cybersecurity"
    became "science", losing the only token anyone would ever search for."""
    parts = [t for t in token.subtree if t.dep_ != "det" and not t.is_punct]
    return " ".join(t.text for t in sorted(parts, key=lambda t: t.i)) or _noun_phrase(token)


def _institution_from_verb(verb) -> Optional[str]:
    """The named institution attended — "the University of Phoenix".

    An academic anchor that is part of an ORG entity IS the school, and its
    name is the whole entity span rather than the object of its internal
    "of"."""
    for node in verb.subtree:
        if _vocab_hit(node.lemma_, _EDU_ANCHORS) and node.ent_type_ == "ORG":
            span = _ent_span(node)
            if span:
                return span
    return None


def _anchor_field(anchor) -> Optional[str]:
    """The field hanging off an academic anchor via for/in/of ("college FOR X",
    "degree IN X"). Skips a pobj that is ITSELF an anchor ("college for my Bachelors
    in X" — 'Bachelors' is the degree, not the field), so it only ever returns a field."""
    for p in anchor.children:
        if p.dep_ == "prep" and p.lemma_ in ("for", "in", "of"):
            for g in p.children:
                if (
                    g.dep_ == "pobj"
                    and not _vocab_hit(g.lemma_, _EDU_ANCHORS)
                    and not _is_place(g)
                    # "University OF Phoenix" — the pobj is part of the school's
                    # name, not a subject it teaches.
                    and not _shares_entity(anchor, g)
                ):
                    return _field_phrase(g)
    return None


def _education_from_verb(verb) -> Optional[str]:
    """Field of study — requires an ACADEMIC ANCHOR, so it never fires on mundane
    "go to the store" / "study the problem every day":
      - "major in X" (major is inherently academic), or
      - an institution/degree anchor (college/university/school/degree/...) whose
        clause carries a "for/in X" field. The field may hang off the anchor
        ("degree IN X", "college FOR X") OR — when spaCy flattens the prepositional
        phrases as VERB siblings ("going [to college] [for Bachelors] [IN
        cybersecurity]") — off the verb itself. The anchor still confirms the clause
        is academic; the field is then the verb's for/in/of object that is NOT itself
        an anchor (the anchor object is the institution/degree, the other is the field).
    """
    if verb.lemma_.lower() == "major":
        return _prep_pobj(verb, frozenset({"in"}))
    has_anchor = False
    has_degree_anchor = False
    for node in verb.subtree:
        if _vocab_hit(node.lemma_, _EDU_ANCHORS):
            has_anchor = True
            if node.lemma_.lower() in {
                "degree",
                "diploma",
                "doctorate",
                "phd",
                "bachelor",
                "bachelors",
                "master",
                "masters",
                "undergrad",
                "postgrad",
            }:
                has_degree_anchor = True
            field = _anchor_field(node)
            if field:
                return field
    if not has_anchor:
        return None
    fields: list[str] = []
    place_like_fields: list[str] = []
    for child in verb.children:
        if child.dep_ == "prep" and child.lemma_.lower() in ("in", "of", "for"):
            for g in child.children:
                # the anchor object is the institution/degree (possibly a typo of one);
                # the field is the verb's other for/in/of object — but never a place
                # ("school in Phoenix" — Phoenix is a location, not a field of study).
                if g.dep_ == "pobj" and not _vocab_hit(g.lemma_, _EDU_ANCHORS):
                    value = _name_span(g) if g.pos_ == "PROPN" else _noun_phrase(g)
                    (place_like_fields if _is_place(g) else fields).append(value)
    # spaCy can label a technical field such as Cybersecurity as GPE. A real
    # academic field beside it (Science) is stronger evidence than that noisy
    # entity label; keep the place-like object only in that case.
    if fields and has_degree_anchor:
        fields.extend(place_like_fields)
    if fields:
        return " in ".join(dict.fromkeys(field for field in fields if field))
    return None


def _relation_modifier(name_token) -> Optional[str]:
    """When the copula subject 'name' is owned by a relation ("my MOTHER's name", "my
    UNCLE's name is..."), return the canonical relation term — the name belongs to that
    person, not the operator. Handles the possessive ("mother's name") and the compound
    ("mothers name") forms, typo-tolerant via the relation vocab; None for a plain
    "my name" (own name)."""
    for child in name_token.children:
        if child.dep_ in ("poss", "compound"):
            rel = _vocab_canonical(child.lemma_, _RELATION_NOUNS)
            if rel:
                return rel
            # "my DOG's name is Biscuit": the possessor is a noun that is neither
            # a relation term nor the operator. The name still belongs to IT,
            # never to the operator (live 2026-09-05: this minted the operator
            # profile record "name = biscuit").
            if child.pos_ in ("NOUN", "PROPN") and child.lemma_.lower() not in _FIRST_PERSON:
                return child.lemma_.lower()
    return None


# "my FAVORITE editor is vim" / "my preferred shell is zsh" — the copula subject
# carries a preference marker; the attribute is the preferred thing. Neither the
# preference-verb rule (like/love/prefer) nor the copula rules saw it, so the
# statement extracted nothing at all (live 2026-09-05).
_FAVORITE_LEMMAS = frozenset({"favorite", "favourite", "preferred", "go-to"})


def _favorite_preference(subj, attr) -> Optional[Dict[str, Any]]:
    marker = next(
        (c for c in subj.children if c.dep_ == "amod" and c.lemma_.lower() in _FAVORITE_LEMMAS),
        None,
    )
    if marker is None or subj.pos_ not in ("NOUN", "PROPN"):
        return None
    # the preferred thing is whatever the copula equates the subject with —
    # a noun, a name, an adjective ("blue"), or a token spaCy could not type
    # ("zsh" arrives as NUM, "vim" as ADJ)
    if attr.pos_ not in ("NOUN", "PROPN", "ADJ", "X", "NUM"):
        return None
    thing = _noun_phrase(attr)
    if not str(thing or "").strip():
        return None
    return {
        "name": f"{marker.lemma_.lower()} {subj.lemma_.lower()}: {thing}",
        "facet": "preference",
        "confidence": 0.85,
    }


def _copula_facts(be) -> List[Dict[str, Any]]:
    """Facts from a copula ("X is Y"):
    - "X is my <relation>" (relation-noun attr with a possessive) → relationship,
    - PROPN attr with a first-person subject ("I'm Noah") → the operator's NAME,
    - common-noun attr with an "I" subject ("I am a student") → OCCUPATION.
    """
    subj = next((c for c in be.children if c.dep_ == "nsubj"), None)
    if subj is None:
        return []
    subj_key = subj.lemma_.lower()
    out: List[Dict[str, Any]] = []
    # "My favorite editor is vim": spaCy files an unknown word like "vim" or
    # "blue" as an adjectival complement (acomp), not an attribute, so the
    # preference has to be looked for on both before the attr loop below.
    for comp in be.children:
        if comp.dep_ in ("attr", "acomp") and not comp.is_punct:
            favorite = _favorite_preference(subj, comp)
            if favorite is not None:
                out.append(favorite)
    for attr in be.children:
        if attr.dep_ != "attr":
            continue
        # spaCy splits "co-owner" into three attr siblings (co / - / owner);
        # only the head carries the role. Without this, the fragments minted
        # occupation records named "co" and "-".
        if attr.is_punct or attr.text == "-":
            continue
        if attr.i + 1 < len(attr.doc) and attr.doc[attr.i + 1].text == "-":
            continue
        attr_lemma = attr.lemma_.lower()
        has_poss = any(c.dep_ == "poss" for c in attr.children)
        if _favorite_preference(subj, attr) is not None:
            continue  # already filed as a keyed preference above
        rel = _vocab_canonical(attr_lemma, _RELATION_NOUNS)
        if rel and has_poss and subj.pos_ == "PROPN":
            out.append(
                {
                    "name": rel,
                    "facet": "relationship",
                    "linked_name": _name_span(subj),
                    "confidence": 0.85,
                }
            )
        elif _vocab_hit(attr_lemma, _BUSINESS_NOUNS) and has_poss and subj.pos_ == "PROPN":
            # "Omega AI is my business/company" → business = the proper-noun subject.
            out.append(
                {
                    "name": _name_span(subj),
                    "facet": "business",
                    "confidence": 0.85,
                }
            )
        elif attr.pos_ == "PROPN" and subj_key in _NAME_SUBJECTS:
            rel_of = _relation_modifier(subj)
            if rel_of:
                # "My mother's/dad's/uncle's name is X" — the name belongs to the
                # RELATION, not the operator (live bug: minted "operator's name is kim").
                out.append(
                    {
                        "name": rel_of,
                        "facet": "relationship",
                        "linked_name": _name_span(attr),
                        "confidence": 0.85,
                    }
                )
            else:
                out.append({"name": _name_span(attr), "facet": "name", "confidence": 0.9})
        elif attr.pos_ == "NOUN" and (subj_key == "i" or _is_person(subj)):
            # An ownership/leadership ROLE carries the organization it is a role
            # IN ("part owner OF Omega AI LLC"). Filing it as a bare occupation
            # both mis-files the fact and strips its object, leaving the same
            # subjectless fragment shape ("The creator.") that broke recall on
            # 2026-07-30. Live 2026-07-31: "I am also part owner of Omega AI
            # LLC" produced occupation='part owner' and a second, conflicting
            # profile_occupation record beside the real one.
            org = _role_organization(attr)
            if org and _vocab_hit(attr.lemma_, _ROLE_NOUNS):
                out.append(
                    {
                        "name": org,
                        "facet": "business",
                        "role": _noun_phrase(attr),
                        "confidence": 0.85,
                    }
                )
            else:
                out.append(
                    {"name": _noun_phrase(attr), "facet": "occupation", "confidence": 0.75}
                )
    # Attribute to the named third party when there is one ("Mitchell Ray is a
    # co-owner of ..."); otherwise the clause was first-person or possessive
    # and the fact is the operator's. Defaulting everything to "operator" wrote
    # third-party facts onto the operator's profile (measured 2026-07-31: a
    # statement about Mitchell minted an education fact for Noah).
    default_subject = _name_span(subj) if _is_person(subj) else "operator"
    for fact in out:
        fact.setdefault("subject", default_subject)
    return out


def _clause_subject(token) -> str:
    """The entity a clause is ABOUT — its resolved subject. First-person ("I own X",
    "my son") is the OPERATOR; a proper-noun subject ("Abel likes trains") is that
    person; a common-noun/absent subject defaults to the operator (speaker context).
    ``token`` may be the governing verb or a token whose head is one."""
    verb = token if token.pos_ in ("VERB", "AUX") else token.head
    for child in verb.children:
        if child.dep_ in ("nsubj", "nsubjpass"):
            key = child.lemma_.lower()
            if key in _FIRST_PERSON:
                return "operator"
            if child.pos_ == "PROPN":
                return _name_span(child).lower()
            # A third-person pronoun refers to someone already named, NOT to the
            # speaker. Defaulting it to "operator" wrote other people's facts
            # onto the operator's profile: measured 2026-07-31, "Mitchell Ray is
            # a co-owner ..., he also attends the University of Phoenix" minted
            # an education fact for the OPERATOR. Resolve to the nearest PERSON
            # named before the pronoun.
            antecedent = _pronoun_antecedent(child)
            if antecedent:
                return antecedent.lower()
            return "operator"
    return "operator"


def extract_facts(text: str) -> List[Dict[str, Any]]:
    """Extract structured facet entities from *text* via the dependency parse.

    Empty input legitimately has no facts; an unavailable parser raises."""
    if not str(text or "").strip():
        return []
    nlp = _nlp()
    doc = nlp(text)
    facts: List[Dict[str, Any]] = []

    for tok in doc:
        # Relationships — any relation-noun that names a person (works regardless
        # of the verb it hangs off, so conjunctions "a sister named M and a son
        # named A" both resolve).
        rel = _vocab_canonical(tok.lemma_, _RELATION_NOUNS)
        if rel:
            person = _relation_name(tok)
            if person:
                facts.append(
                    {
                        "name": rel,
                        "facet": "relationship",
                        "linked_name": person,
                        "confidence": 0.9,
                        "subject": _clause_subject(tok),
                    }
                )
            elif rel in _CHILD_RELATIONS:
                # "he has 2 daughters" — a COUNT with no names. Previously
                # dropped entirely because the relationship path requires a
                # named person, so "2 daughters" was simply lost.
                num = next((c for c in tok.children if c.dep_ == "nummod"), None)
                if num is not None:
                    facts.append(
                        {
                            "name": f"{num.text} {tok.text}".lower(),
                            "facet": "children",
                            "confidence": 0.85,
                            "subject": _clause_subject(tok),
                        }
                    )

        if (
            _vocab_hit(tok.lemma_, _MARITAL_LEMMAS)
            and tok.dep_ in ("acomp", "conj", "ROOT", "advcl")
            # "he married Sarah" states an event with an object; "he is married"
            # states a status. Only the status is a profile fact.
            and not any(c.dep_ == "dobj" for c in tok.children)
        ):
            # The state hangs off the copula ("he's married"), so the subject
            # lives on the HEAD rather than on this token — and in a compound
            # sentence ("he's married and he also attends ...") it arrives as a
            # conj instead of an acomp. Reading the subject off the token itself
            # resolved to nobody and the fact was silently lost.
            subject_source = tok.head if tok.dep_ in ("acomp", "conj") else tok
            facts.append(
                {
                    "name": tok.text.lower(),
                    "facet": "marital_status",
                    "confidence": 0.85,
                    "subject": _clause_subject(subject_source),
                }
            )

        if tok.pos_ in ("VERB", "AUX"):
            lemma = tok.lemma_.lower()
            subject = _clause_subject(tok)
            if _vocab_hit(lemma, _OWN_VERBS) or lemma == "have":
                # ownership verbs (own/run/found/...) allow a direct proper-noun object;
                # the weak possession verb "have" is restricted to "a business called X".
                biz = _business_from_verb(tok, allow_propn=_vocab_hit(lemma, _OWN_VERBS))
                if biz:
                    facts.append(
                        {"name": biz, "facet": "business", "confidence": 0.9, "subject": subject}
                    )
            if lemma == "work":
                wp = _prep_pobj(tok, frozenset({"at", "for"}))
                if wp:
                    facts.append(
                        {"name": wp, "facet": "workplace", "confidence": 0.85, "subject": subject}
                    )
            if lemma == "live":
                loc = _prep_pobj(tok, frozenset({"in", "from", "at"}))
                if loc:
                    facts.append(
                        {"name": loc, "facet": "location", "confidence": 0.8, "subject": subject}
                    )
            if _vocab_hit(lemma, _EDU_VERBS):
                edu = _education_from_verb(tok)
                if edu:
                    facts.append(
                        {"name": edu, "facet": "education", "confidence": 0.8, "subject": subject}
                    )
                # Enrollment and credential are separate propositions: "attends
                # the University of Phoenix" is a fact even when no field of
                # study is stated, and losing it left the institution
                # unmatchable ("phoenix" cannot answer a query naming the
                # University of Phoenix).
                institution = _institution_from_verb(tok)
                if institution:
                    facts.append(
                        {
                            "name": institution,
                            "facet": "education_institution",
                            "confidence": 0.85,
                            "subject": subject,
                        }
                    )
            if _vocab_hit(lemma, _PREFERENCE_VERBS):
                for obj in _dobj(tok):
                    facts.append(
                        {
                            "name": _noun_phrase(obj),
                            "facet": "preference",
                            "confidence": 0.7,
                            "subject": subject,
                        }
                    )
            if lemma == "be":
                facts.extend(_copula_facts(tok))

    return _dedup(_normalize(_add_companions(facts)))


def _add_companions(facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A relationship also yields the related person's NAME as a standalone entity
    ("son named Abel" → both the relationship AND name=Abel), matching the enrich()
    contract. The persona-ingest orchestrator later filters the name entity out when
    it is a relationship object, but downstream consumers expect it present."""
    out = list(facts)
    for f in facts:
        if f.get("facet") == "relationship" and f.get("linked_name"):
            # the companion name is the RELATED person's OWN name — it is about them,
            # so its subject is that person (not the fact's subject).
            out.append(
                {
                    "name": f["linked_name"],
                    "facet": "name",
                    "confidence": f.get("confidence", 0.85),
                    "subject": f["linked_name"],
                }
            )
    return out


def _normalize(facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Lower-case entity values to match the store's convention (the regex enricher
    always lower-cased; downstream slugging/display re-cases as needed)."""
    for f in facts:
        if "name" in f:
            f["name"] = str(f["name"]).lower()
        if "linked_name" in f:
            f["linked_name"] = str(f["linked_name"]).lower()
        if "subject" in f:
            f["subject"] = str(f["subject"]).lower()
    return facts


def _dedup(facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for f in facts:
        key = (
            f.get("facet"),
            str(f.get("name", "")),
            str(f.get("linked_name", "")),
            str(f.get("subject", "")),
        )
        if key in seen or not str(f.get("name", "")).strip():
            continue
        seen.add(key)
        out.append(f)
    return out
