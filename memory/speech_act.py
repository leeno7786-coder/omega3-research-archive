"""Classify how an utterance ENTERED memory: asked, told, or commanded.

The third axis of the organ. A stored turn already carries WHAT it is (its
Dewey coordinate) and WHEN it arrived (its episode); this adds HOW, which is
what lets recall prefer a fact the user ASSERTED over the assistant restating
it, and lets a question match an episode where a similar question was asked.

Deterministic and auditable by design. A rule that misfires can be read and
corrected; a model that misfires has to be retrained. The SetFit lane is the
upgrade path only if these rules plateau.
"""

from __future__ import annotations

import re

_QUESTION_TAIL = re.compile(r"\?\s*$")
_WH_OPENER = re.compile(
    r"^(?:who|whom|whose|what|when|where|why|how|which)\b",
    re.I,
)
_AUX_OPENER = re.compile(
    r"^(?:do|does|did|is|are|was|were|am|can|could|will|would|shall|should"
    r"|may|might|must|have|has|had)\b",
    re.I,
)
# A subject-inversion question puts a subject right after the auxiliary
# ("Do YOU like it?", "Did I ever..."); an imperative headed by the same word
# does not ("Do the dishes.", "Have a seat." -- "the dishes"/"a seat" are the
# object, not a subject). A personal pronoun immediately after the auxiliary
# is the one following-context signal that cannot also be that verb's direct
# object in idiomatic English, so it is trustworthy on its own.
#
# What this deliberately does NOT catch: a determiner+noun subject ("Did the
# meeting run late") is indistinguishable, by surface word shape alone, from
# a determiner+noun OBJECT ("Do the dishes") -- telling them apart needs the
# verb that follows the noun phrase ("run late" vs. nothing), and finding
# that without a verb lexicon or POS tagger is not something plain regex can
# do reliably. Rather than guess, that case is left as a known gap.
_PRONOUN_SUBJECT = re.compile(r"^\s+(?:i|you|he|she|it|we|they)\b", re.I)
# Of the _AUX_OPENER words, only "do" and "have" also have a bare-form (base,
# subjectless) reading as an ordinary imperative main verb -- "Do the
# dishes.", "Have a seat.", "Do not disturb.", "Have a look at this." The
# rest (is/are/was/..., the modals) have no such subjectless-imperative
# reading, so when they fail the subject check they fall through to
# "statement" below rather than being force-fit into "command".
_BARE_IMPERATIVE_AUX = frozenset({"do", "have"})
_IMPERATIVE_OPENER = re.compile(
    r"^(?:please\b|remember\b|give\b|show\b|tell\b|list\b|find\b|make\b|write\b"
    r"|create\b|add\b|remove\b|delete\b|update\b|correct\b|explain\b|summari[sz]e\b"
    r"|suggest\b|help\b|set\b|open\b|close\b|run\b|stop\b)",
    re.I,
)


def classify_speech_act(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped:
        return "unknown"
    # A terminal question mark outranks an imperative opener: "Tell me, did I
    # ever visit a dermatologist?" is asking, not commanding.
    if _QUESTION_TAIL.search(stripped):
        return "question"
    first = stripped.lstrip("\"'([-* ")
    if _WH_OPENER.match(first):
        return "question"
    aux_match = _AUX_OPENER.match(first)
    if aux_match:
        if _PRONOUN_SUBJECT.match(first[aux_match.end() :]):
            return "question"
        if aux_match.group(0).lower() in _BARE_IMPERATIVE_AUX:
            return "command"
        return "statement"
    if _IMPERATIVE_OPENER.match(first):
        return "command"
    return "statement"
