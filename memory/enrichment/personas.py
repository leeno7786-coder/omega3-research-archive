"""Personal enrichment entry point (``enrich``).

Grammar facts — names, relationships, occupation, workplace, location, education,
business, preferences — are extracted by the dependency parser
(``memory.enrichment.parse_extract``), which reads grammatical structure and so
covers every phrasing. This module keeps only the FORMAT patterns (age, email) as
regex, where regex is the right tool, and merges the two.
"""

import logging
import re
from typing import Dict, Optional, List

log = logging.getLogger("omega.memory.enrichment.personas")

# Grammar-fact regexes (name, relationships, preference, location, workplace,
# occupation, education) are RETIRED — the dependency parser (parse_extract) owns
# every phrasing of them now. Only FORMAT patterns remain here: an age or an email
# has one canonical form, so regex is the right tool (they are not the phrasing-
# varied facts that made a grammar-regex library a losing game).

# ── Age (format) ─────────────────────────────────────────────
_AGE = re.compile(r"\b(?:i am|i'm|he is|she is|he's|she's)\s+(\d{1,3})\s+(?:years?\s+old|yr)", re.I)
_AGE_ALT = re.compile(r"\b(\d{1,3})\s*[-–]\s*year\s*[-–]?\s*old\b", re.I)
_EMAIL = re.compile(r"\b([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)\b")


def enrich(text: str) -> Optional[Dict]:
    """Extract personal entities from text.

    Returns dict with entities list, each having:
      name, facet, confidence, and optionally relationship/linked_name
    """
    entities: List[Dict] = []

    # Grammar facts — name, relationships, workplace, location, occupation,
    # education, business, preferences — are owned by the dependency parser (the
    # parse_extract merge below). The hand-written per-phrasing regexes that used to
    # live here are RETIRED: one parse rule covers every phrasing, where a regex
    # needed one pattern each and still missed ("I own a business called X"). Only
    # true FORMAT patterns stay as regex — an age or an email has one canonical
    # form, not the infinite phrasings that made grammar-regex a losing game.

    # ── Age (format) ─────────────────────────────────────────
    age_match = _AGE.search(text) or _AGE_ALT.search(text)
    if age_match:
        age_val = age_match.group(1)
        if 1 <= int(age_val) <= 150:
            entities.append(
                {
                    "name": age_val,
                    "facet": "age",
                    "confidence": 0.85,
                }
            )

    # ── Email (format) ───────────────────────────────────────
    email_match = _EMAIL.search(text)
    if email_match:
        entities.append(
            {
                "name": email_match.group(1),
                "facet": "contact",
                "confidence": 0.95,
            }
        )

    # Deduplicate by (name, facet)
    seen = set()
    deduped = []
    for e in entities:
        key = (e["name"], e["facet"], e.get("linked_name"))
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    # ── Parse-based extraction (the "language cortex") ────────────────────────
    # THE primary grammar extractor now — name, relationships, workplace, location,
    # occupation, education, business, preferences, all off the dependency parse.
    # Deterministic spaCy parse; an unavailable model fails the ingest boundary
    # with its original cause. The format facets above (age/email)
    # take precedence on the rare key collision; case-insensitive dedup.
    from memory.enrichment.parse_extract import extract_facts

    parse_facts = extract_facts(text)
    if parse_facts:
        existing_keys = {
            (str(e["name"]).lower(), e["facet"], str(e.get("linked_name") or "").lower())
            for e in deduped
        }
        for f in parse_facts:
            key = (
                str(f.get("name", "")).lower(),
                f.get("facet"),
                str(f.get("linked_name") or "").lower(),
            )
            if key not in existing_keys:
                existing_keys.add(key)
                deduped.append(f)

    if not deduped:
        return None
    return {
        "decomposer": "personal",
        "kind": "personal",
        "entities": deduped,
        "speaker": "user",
    }
