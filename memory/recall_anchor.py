"""Pre-verifier anchor filter (RATIFIED Task #12, 2026-07-04).

Sits between candidate recall and the RecallVerifier. A candidate may reach
the verifier only with an anchor basis, verbatim from the ratified spec:

    lexical anchor OR strong edge OR hop <= 1 OR compiler-proven
    alias/session bridge

Entity and session bridges land in the strong hop classes, so the strong-
class check covers the compiler-proven basis. What actually gets filtered:
WEAK-hop reach (CO_LOCATED / SHARES_* placement co-occurrence) with no
lexical tie to the query. Gate-1 live finding (2026-07-04): weak hop-1
arrivals with zero lexical anchor were the fabrication vector — isolated-
alias queries surfaced same-subdomain neighbors that the theme-level cosine
floor then admitted as "relevant" (3 fabricated recoveries). The hop<=1
basis therefore applies to PROVENANCE-bearing classes only; weak placement
reach needs a lexical anchor at ANY depth. That is also exactly the
operator's isolated-alias contract: no bridge, no token — honest UNKNOWN.

The filter only REMOVES candidates. An emptied list flows to UNKNOWN exactly
like an empty recall, so abstention is never blocked — the ratified
invariant. Non-DME candidates come from deterministic index lanes (the lane
IS their anchor) and always pass.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Tuple

from memory.taxonomy.canonicalize import content_tokens

# direct/union are coordinate-anchored by construction; strong hop classes
# carry compiler-proven provenance (record links, entity bridges, sessions)
_ANCHORED_CLASSES = frozenset({"direct", "union", "strong_hop1", "strong_hop2"})
# weak classes = placement/similarity co-occurrence — lexical anchor required
_WEAK_CLASSES = frozenset({"weak_hop1", "weak_hop2"})


def _get(candidate: Any, key: str, default: Any = None) -> Any:
    if isinstance(candidate, Mapping):
        return candidate.get(key, default)
    return getattr(candidate, key, default)


def filter_anchor_basis(
    query_text: str,
    candidates: List[Any],
) -> Tuple[List[Any], List[Any]]:
    """Split candidates into (kept, dropped) by the ratified anchor basis."""
    kept: List[Any] = []
    dropped: List[Any] = []
    query_tokens = content_tokens(query_text)
    for candidate in candidates or []:
        prov = _get(candidate, "provenance") or {}
        if not isinstance(prov, Mapping):
            prov = {}
        is_dme = str(prov.get("candidate_source") or "") == "dme" or "dme_hop" in prov
        if not is_dme:
            kept.append(candidate)
            continue
        edge_class = str(prov.get("dme_edge_class") or "")
        if edge_class in _ANCHORED_CLASSES:
            kept.append(candidate)
            continue
        if edge_class not in _WEAK_CLASSES:
            # classless DME provenance (pre-split rows): keep the hop<=1 basis
            try:
                hop = int(prov.get("dme_hop") or 0)
            except (TypeError, ValueError):
                hop = 0
            if hop <= 1:
                kept.append(candidate)
                continue
        text = f"{_get(candidate, 'title', '') or ''} {_get(candidate, 'summary', '') or ''}"
        if query_tokens & content_tokens(text):
            kept.append(candidate)
            continue
        dropped.append(candidate)
    return kept, dropped
