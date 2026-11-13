"""Deterministic recall features derived from canonical memory text.

These features are deliberately lexical and auditable.  Both filesystem
ranking and the recall verifier derive them from the same canonical bytes so
the scorer cannot select a compositional operand that the verifier then
rejects merely because the operand uses different surface words than the
question.
"""

from __future__ import annotations

import re
from collections import Counter

_ATTENDANCE_PHRASE_RX = re.compile(
    r"\b(?:"
    r"attended|participated\s+in|volunteered\s+at|joined|competed\s+in|"
    r"(?:have|'ve|had|'d)\s+been\s+to|"
    r"(?:just\s+)?got\s+back\s+from|"
    r"(?:just\s+)?returned\s+from|"
    r"went\s+to|"
    r"(?:was|were)\s+(?:an?\s+)?bridesmaid\s+at"
    r")\b",
    re.I,
)
_EXPLICIT_QUANTITY_RX = re.compile(
    r"\b\d+(?:\.\d+)?\s+"
    r"(?!gallon\b|minute\b|hour\b|day\b|week\b|month\b|year\b)"
    r"[a-z]",
    re.I,
)
_EXPLICIT_DURATION_RX = re.compile(
    r"\b(?:"
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"[ -]?(?:day|week|month|year)s?(?:-long)?|"
    r"(?:day|week|month|year)-long"
    r")\b",
    re.I,
)
_PERSONAL_AGE_RX = re.compile(r"\bI(?:'m| am)\s+\d{1,3}\b", re.I)
_COMPLETED_COURSE_COUNT_RX = re.compile(
    r"\b(?:"
    r"\d+\s+(?:edx|coursera|udemy|datacamp)\s+courses?|"
    r"\d+\s+courses?\s+on\s+(?:edx|coursera|udemy|datacamp)"
    r")\b",
    re.I,
)
_PROJECT_LEADERSHIP_RX = re.compile(
    r"\b(?:"
    r"projects?\b.{0,160}\b(?:lead|led|leading|headed|managed)\b|"
    r"(?:lead|led|leading|headed|managed)\b.{0,160}\bprojects?\b|"
    r"solo\s+project"
    r")\b",
    re.I | re.S,
)


def derived_recall_features(text: object) -> Counter[str]:
    """Return weighted, truth-derived recall features for canonical text."""

    body = str(text or "").rsplit("]", 1)[-1]
    features: Counter[str] = Counter()
    if _ATTENDANCE_PHRASE_RX.search(body):
        features["attended"] += 5
    if _EXPLICIT_QUANTITY_RX.search(body):
        features["quantity"] += 6
    if _EXPLICIT_DURATION_RX.search(body):
        features["durationoperand"] += 8
    if _PERSONAL_AGE_RX.search(body):
        features["personalage"] += 8
    if _COMPLETED_COURSE_COUNT_RX.search(body):
        features["coursecount"] += 8
    if _PROJECT_LEADERSHIP_RX.search(body):
        features["projectlead"] += 10
    return features
