"""Personal decomposer — v1 memory engine port for identity/persona writes.

Adapted from ``<_frozen>\\memory_engine_v1_2026-04-04\\decomposer_personal.py``.
Output is enrichment-shaped (5W1H facets + chain anchor) for Dewey JSON + graph projection,
not Kuzu/DME coordinates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

_NAME_PATTERNS = [
    re.compile(
        r"(?:my\s+name\s+is|I\'m|I\s+am)\s+"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)(?=\s+(?:and|or|but|from|in|at|on)\b|\s*[.,;!?]|\s*$)",
        re.I,
    ),
    re.compile(r"(?:call\s+me|go\s+by|known\s+as)\s+(\w+)", re.I),
]
_ALIAS_PATTERNS = [
    re.compile(r"go\s+by\s+(\w+)\s+(?:online|on\s+\w+|everywhere)", re.I),
    re.compile(r"(?:also\s+known\s+as|aka|alias(?:es)?)\s+(\w+)", re.I),
]
_ROLE_PATTERNS = [
    re.compile(r"(?:I\s+am\s+a|I\'m\s+a)\s+(.+?)(?:\.|,|$)", re.I),
    re.compile(r"I\s+work\s+(?:as|in)\s+(.+?)(?:\.|,|$)", re.I),
]
_THIRD_PERSON = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+is\s+(?:a|an|my|the)\s+(.+?)(?:\.|,|$)",
    re.I,
)
_ACTION_VERBS = frozenset(
    {
        "built",
        "build",
        "created",
        "create",
        "designed",
        "design",
        "developed",
        "develop",
        "made",
        "make",
        "wrote",
        "write",
        "invented",
        "founded",
        "launched",
        "deployed",
        "run",
        "runs",
        "use",
        "uses",
        "operate",
        "manages",
        "manage",
    }
)
_PREF_VERBS = frozenset(
    {"prefer", "like", "love", "enjoy", "want", "need", "hate", "dislike", "avoid"}
)
_SKILL_VERBS = frozenset({"can", "know", "learned", "studied", "practice", "specialize", "focus"})
_LOCATION_PATTERNS = [
    re.compile(r"I\s+(?:live|am|reside)\s+(?:in|at)\s+(.+?)(?:\.|,|$)", re.I),
    re.compile(r"I\'m\s+(?:in|from|based\s+in)\s+(.+?)(?:\.|,|$)", re.I),
]
_TOOL_PREPS = re.compile(r"\b(?:on|using|with|via|through)\s+(?:an?\s+)?(.+?)(?:\.|,|$)", re.I)
_WHY_PATTERNS = [
    re.compile(r"because\s+(.+?)(?:\.|$)", re.I),
    re.compile(r"so\s+(?:that\s+)?(.+?)(?:\.|$)", re.I),
]


@dataclass
class PersonalFacet:
    text: str
    slot: str  # who|what|where|when|why|how
    facet_type: str
    rule: str


@dataclass
class PersonalDecomposition:
    original_text: str
    anchor_name: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    facets: List[PersonalFacet] = field(default_factory=list)
    third_person_subjects: List[str] = field(default_factory=list)

    def to_enrichment(self) -> Dict:
        entities = []
        for f in self.facets:
            entities.append(
                {
                    "name": f.text,
                    "facet": f.slot if f.slot in ("who", "name") else f.facet_type,
                    "slot": f.slot,
                    "rule": f.rule,
                }
            )
        out: Dict = {
            "decomposer": "personal_v1",
            "kind": "personal",
            "entities": entities,
            "facets": {
                "who": [f.text for f in self.facets if f.slot == "who"],
                "what": [f.text for f in self.facets if f.slot == "what"],
                "where": [f.text for f in self.facets if f.slot == "where"],
                "why": [f.text for f in self.facets if f.slot == "why"],
                "how": [f.text for f in self.facets if f.slot == "how"],
            },
        }
        if self.anchor_name:
            out["chain_anchor"] = self.anchor_name
            out["identity_stamp"] = f"id:{self.anchor_name}"
        if self.aliases:
            out["aliases"] = list(self.aliases)
        if self.third_person_subjects:
            out["linked_persons"] = list(dict.fromkeys(self.third_person_subjects))
        return out


class PersonalDecomposer:
    """Decompose first/third-person identity statements into chainable facets."""

    def __init__(self) -> None:
        self._anchor: Optional[str] = None
        self._aliases: List[str] = []

    def decompose(self, text: str) -> Optional[PersonalDecomposition]:
        if not text or not str(text).strip():
            return None
        decomp = PersonalDecomposition(original_text=text.strip())
        seen: Dict[str, Set[str]] = {k: set() for k in ("who", "what", "where", "why", "how")}
        for sent in self._split_sentences(text):
            self._read_sentence(sent, decomp, seen)
        decomp.anchor_name = self._anchor
        decomp.aliases = list(self._aliases)
        if not decomp.facets and not decomp.third_person_subjects:
            return None
        return decomp

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        parts = re.split(r"(?<=[.!?])\s+", text)
        out: List[str] = []
        for part in parts:
            for line in part.split("\n"):
                s = line.strip()
                if s and len(s) > 3:
                    out.append(s)
        return out or [text.strip()]

    def _read_sentence(
        self,
        sent: str,
        decomp: PersonalDecomposition,
        seen: Dict[str, Set[str]],
    ) -> None:
        self._extract_identity(sent, decomp, seen)
        if self._anchor and self._anchor.lower() not in seen["who"]:
            self._add(self._anchor, "who", "identity", "anchor", decomp, seen)
        for alias in self._aliases:
            if alias.lower() not in seen["who"]:
                self._add(alias, "who", "alias", "alias_link", decomp, seen)
        self._extract_role(sent, decomp, seen)
        self._extract_actions(sent, decomp, seen)
        self._extract_preferences(sent, decomp, seen)
        self._extract_location(sent, decomp, seen)
        self._extract_tools(sent, decomp, seen)
        self._extract_why(sent, decomp, seen)
        m = _THIRD_PERSON.search(sent)
        if m:
            name = m.group(1).strip()
            if name and name not in decomp.third_person_subjects:
                decomp.third_person_subjects.append(name)
                self._add(name, "who", "name", "third_person_subject", decomp, seen)
                role = m.group(2).strip().rstrip(".,;")
                if role:
                    self._add(role, "what", "role", "third_person_role", decomp, seen)

    def _extract_identity(
        self,
        sent: str,
        decomp: PersonalDecomposition,
        seen: Dict[str, Set[str]],
    ) -> None:
        for pat in _NAME_PATTERNS:
            m = pat.search(sent)
            if not m:
                continue
            name = m.group(1).strip()
            if len(name) < 2:
                continue
            if not self._anchor:
                self._anchor = name
            elif name.lower() != self._anchor.lower() and name.lower() not in [
                a.lower() for a in self._aliases
            ]:
                self._aliases.append(name)
        for pat in _ALIAS_PATTERNS:
            m = pat.search(sent)
            if not m:
                continue
            alias = m.group(1).strip()
            if (
                len(alias) >= 2
                and alias.lower() not in [a.lower() for a in self._aliases]
                and (not self._anchor or alias.lower() != self._anchor.lower())
            ):
                self._aliases.append(alias)

    def _extract_role(
        self, sent: str, decomp: PersonalDecomposition, seen: Dict[str, Set[str]]
    ) -> None:
        for pat in _ROLE_PATTERNS:
            m = pat.search(sent)
            if m:
                role = m.group(1).strip().rstrip(".,;")
                if len(role) >= 3:
                    self._add(role, "what", "role", "role_extract", decomp, seen)

    def _extract_actions(
        self, sent: str, decomp: PersonalDecomposition, seen: Dict[str, Set[str]]
    ) -> None:
        m = re.search(
            r"\bI\s+(\w+)\s+(.+?)(?:\s+(?:because|using|with|on|at|in|from|via|through)\b|[.,;]|$)",
            sent,
            re.I,
        )
        if not m:
            return
        verb = m.group(1).lower()
        obj = m.group(2).strip().rstrip(".,;")
        if verb in _ACTION_VERBS and len(obj) >= 2:
            self._add(f"{verb} {obj}", "what", "action", "action_extract", decomp, seen)

    def _extract_preferences(
        self, sent: str, decomp: PersonalDecomposition, seen: Dict[str, Set[str]]
    ) -> None:
        m = re.search(r"\bI\s+(\w+)\s+(.+?)(?:[.,;]|$)", sent, re.I)
        if m and m.group(1).lower() in _PREF_VERBS:
            obj = m.group(2).strip().rstrip(".,;")
            if len(obj) >= 2:
                self._add(
                    f"{m.group(1).lower()} {obj}",
                    "what",
                    "preference",
                    "pref_extract",
                    decomp,
                    seen,
                )

    def _extract_location(
        self, sent: str, decomp: PersonalDecomposition, seen: Dict[str, Set[str]]
    ) -> None:
        for pat in _LOCATION_PATTERNS:
            m = pat.search(sent)
            if m:
                loc = m.group(1).strip().rstrip(".,;")
                if len(loc) >= 2:
                    self._add(loc, "where", "location", "location_extract", decomp, seen)

    def _extract_tools(
        self, sent: str, decomp: PersonalDecomposition, seen: Dict[str, Set[str]]
    ) -> None:
        for m in _TOOL_PREPS.finditer(sent):
            tool = m.group(1).strip().rstrip(".,;")
            tool = re.split(
                r"\b(?:because|so\s+that|and\s+I|which|that|for)\b", tool, maxsplit=1, flags=re.I
            )[0].strip()
            if len(tool) >= 2:
                self._add(tool, "how", "tool", "tool_extract", decomp, seen)

    def _extract_why(
        self, sent: str, decomp: PersonalDecomposition, seen: Dict[str, Set[str]]
    ) -> None:
        for pat in _WHY_PATTERNS:
            m = pat.search(sent)
            if m:
                reason = m.group(1).strip().rstrip(".,;")
                if len(reason) >= 3:
                    self._add(reason, "why", "motivation", "why_extract", decomp, seen)
                break

    def _add(
        self,
        text: str,
        slot: str,
        facet_type: str,
        rule: str,
        decomp: PersonalDecomposition,
        seen: Dict[str, Set[str]],
    ) -> None:
        clean = text.strip()
        if len(clean) < 2:
            return
        key = clean.lower()
        if key in seen.get(slot, set()):
            return
        seen.setdefault(slot, set()).add(key)
        decomp.facets.append(PersonalFacet(text=clean, slot=slot, facet_type=facet_type, rule=rule))
