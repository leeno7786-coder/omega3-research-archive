"""Deterministic grammatical analysis of an operator turn.

This is the parsing half of the answer NLG. It replaces nothing the classifier
does — the classifier says WHAT KIND of turn this is (question, reasoning,
freeform, temporal), and that answer arrives from chat ingress already made.
What it cannot say is what the sentence is SHAPED like: whether it asks two
things, whether it leans on a pronoun whose antecedent is in the previous turn,
whether it wants a list or a comparison. Those are properties of the text, they
are decidable by rule, and they are exactly what the retired composer LoRA was
being asked to infer with a 2B model and a 2 GB memory spike.

Everything here is pure: no model, no I/O, no clock. Same text in, same shape
out, so an answer can always be explained by the sentence that produced it.

Deliberately NOT a parser. There is no POS tagging or dependency tree — those
buy accuracy on sentences Omega rarely sees and cost a dependency the appliance
would have to carry to every edge device. Closed-class word lists and clause
counting get the features the prompt assembler actually branches on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 5W1H + auxiliaries. Shared with question_shape's detector by intent, not by
# import: that module is the recall-skip FLOOR and must stay minimal, while this
# one may grow features freely.
_INTERROGATIVES = (
    "who", "what", "when", "where", "why", "how", "which", "whose", "whom",
)
_AUXILIARIES = (
    "is", "are", "am", "was", "were", "do", "does", "did", "can", "could",
    "will", "would", "should", "shall", "may", "might", "have", "has", "had",
)
_INTERROGATIVE_LEAD = re.compile(
    r"^\s*(" + "|".join(_INTERROGATIVES + _AUXILIARIES) + r")\b", re.I
)
_WH_LEAD = re.compile(r"^\s*(" + "|".join(_INTERROGATIVES) + r")\b", re.I)

# Bare-verb openers that make a turn a directive rather than a question. Kept to
# verbs Omega is actually asked to perform so ordinary sentences starting with a
# verb-like word are not misread as commands.
_IMPERATIVE_LEAD = re.compile(
    r"^\s*(please\s+)?(tell|show|give|list|name|explain|describe|summari[sz]e|"
    r"write|make|build|create|add|remove|delete|fix|update|change|set|run|"
    r"check|find|look|open|close|start|stop|send|call|remember|forget)\b",
    re.I,
)
_COMPARATIVE = re.compile(
    r"\b(compare|compared|contrast|versus|vs\.?|difference|differences|differ|"
    r"better|worse|best|worst|prefer|instead\s+of|rather\s+than|"
    r"both|either|neither)\b",
    re.I,
)
_ENUMERATIVE = re.compile(
    r"\b(list|enumerate|name\s+(?:them|all|the)|all\s+of\s+(?:them|the)|"
    r"every|each\s+of|how\s+many|which\s+ones|what\s+are\s+the)\b",
    re.I,
)
# Turns that only make sense against what was just said.
_CONTINUATION = re.compile(
    r"^\s*(and|but|so|then|also|what\s+about|how\s+about|why\s+not|ok(?:ay)?\s+so|"
    r"instead|otherwise|besides|plus)\b",
    re.I,
)
_FIRST_PERSON = re.compile(r"\b(i|me|my|mine|myself|we|us|our|ours)\b", re.I)
_SECOND_PERSON = re.compile(r"\b(you|your|yours|yourself)\b", re.I)
# Pronouns and demonstratives with no antecedent inside the turn itself.
_ANAPHORA = re.compile(
    r"\b(it|its|that|this|these|those|they|them|their|he|him|his|she|her|hers|"
    r"one|ones|there)\b",
    re.I,
)
_CLAUSE_SPLIT = re.compile(r"[.!?;]+|\band\s+(?:also\s+)?(?:what|who|when|where|why|how)\b", re.I)


@dataclass(frozen=True)
class QueryShape:
    """Rule-decidable features of one operator turn."""

    text: str
    is_question: bool
    interrogative: str | None
    is_imperative: bool
    is_comparative: bool
    is_enumerative: bool
    is_continuation: bool
    first_person: bool
    second_person: bool
    anaphora: tuple[str, ...]
    clause_count: int

    @property
    def needs_deixis_binding(self) -> bool:
        """The turn contains a reference the model could bind to the wrong party.

        This is the failure the composer's ``subject_binding`` field existed to
        prevent: "what do Mitchell and I both do" answered about Omega, or "your
        project" read as the model's own. Any first/second-person pronoun, or a
        demonstrative pointing outside the sentence, arms the binding directive.
        """
        return self.first_person or self.second_person or bool(self.anaphora)

    @property
    def is_compound(self) -> bool:
        """More than one thing is being asked, so a single answer under-serves it."""
        return self.clause_count > 1 or self.is_enumerative


def _clause_count(text: str) -> int:
    parts = [p.strip() for p in _CLAUSE_SPLIT.split(text) if p and p.strip()]
    return max(1, len(parts))


def analyze(text: str) -> QueryShape:
    """Return the deterministic shape of ``text``. Safe on empty input."""
    t = str(text or "").strip()
    if not t:
        return QueryShape(
            text="",
            is_question=False,
            interrogative=None,
            is_imperative=False,
            is_comparative=False,
            is_enumerative=False,
            is_continuation=False,
            first_person=False,
            second_person=False,
            anaphora=(),
            clause_count=1,
        )

    wh = _WH_LEAD.match(t)
    imperative = bool(_IMPERATIVE_LEAD.match(t))
    # "?" wins over an imperative opener: "tell me what you know?" is a question
    # being asked politely, not an order.
    is_question = t.endswith("?") or (bool(_INTERROGATIVE_LEAD.match(t)) and not imperative)

    # Order-preserved, de-duplicated: the prompt names these back to the model,
    # and a stable order keeps the rendered prompt deterministic.
    seen: dict[str, None] = {}
    for match in _ANAPHORA.finditer(t):
        seen.setdefault(match.group(0).lower(), None)

    return QueryShape(
        text=t,
        is_question=is_question,
        interrogative=wh.group(1).lower() if wh else None,
        is_imperative=imperative and not t.endswith("?"),
        is_comparative=bool(_COMPARATIVE.search(t)),
        is_enumerative=bool(_ENUMERATIVE.search(t)),
        is_continuation=bool(_CONTINUATION.match(t)),
        first_person=bool(_FIRST_PERSON.search(t)),
        second_person=bool(_SECOND_PERSON.search(t)),
        anaphora=tuple(seen),
        clause_count=_clause_count(t),
    )


__all__ = ["QueryShape", "analyze"]
