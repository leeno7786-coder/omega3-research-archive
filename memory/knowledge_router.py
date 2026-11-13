"""
knowledge_router.py — dynamic Z-level routing for the knowledge lobe
=====================================================================

The third level under `200_knowledge/<subdomain>/` is content-driven,
not pre-declared. When a write lands at (2, Y) — say (2, 24) AI — this
module decides which Z-cluster the content belongs to:

    * If a cluster with the derived name already exists → return its Z.
    * Otherwise → mkdir a new Z-dir, allocate next Z, persist.

Per the Omega Mantra (operator, 2026-05-08):

    > "we dont even need a helper script for anything but creating a
    >  coord file with mkdir and naming it. cause i bet you 100-1, you
    >  build a main set trained by lobe, a knowledge set trained by
    >  subcat, and a persona setfit trained on how to route personal
    >  / identity data, then script the easy ones … you almost never
    >  get a write routing fail."

This helper does exactly two things:

    1. derive a deterministic dirname from content title (or first
       salient body token)
    2. mkdir on first encounter, look up on subsequent encounters

**Live wiring (2026-06-23):** The gate write path allocates Y dynamically
but keeps ``coord[2] = 0`` (general cluster). ``route_knowledge_content`` is
the reserved Z allocator for a future consolidation hook — it has no gate
caller today. Do not assume Z clusters are populated on ingest.

No fingerprint overlap. No token-similarity scoring. No fuzzy match.
The SetFits predict (X, Y); the substrate allocates Z by exact-name
lookup. Reasoning lives at the read layer (walker + reranker), not
at the writer.

State sidecar:
    guardian/state/knowledge_z_index.json
        {
            "schema_version": 2,
            "updated_at": "...",
            "subdomains": {
                "ai": {
                    "next_z": 4,
                    "clusters": {
                        "transformers": {"z": 1, "created_at": "..."},
                        "rlhf":         {"z": 2, "created_at": "..."},
                        "diffusion":    {"z": 3, "created_at": "..."}
                    }
                }
            }
        }

Concurrency: in-process lock. Single-boot deployment shape — no
multi-process race. Cross-process safety is not required today.

Doctrine: empty-subdomain reflection IS curiosity. When a subdomain
has zero clusters, that's not a bug — it's a gap signal the curiosity
engine consumes (`empty_subdomain_count`).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from core.paths import OMEGA_ROOT

log = logging.getLogger("omega.memory.knowledge_router")


_INDEX_PATH = OMEGA_ROOT / "guardian" / "state" / "knowledge_z_index.json"
_KNOWLEDGE_LOBE_DIR = OMEGA_ROOT / "memory" / "200_knowledge"

_LOCK = threading.Lock()

# Stopwords for name derivation. Same shape as the walker's token
# filter; keeps signals aligned across the substrate.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "with",
        "from",
        "by",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "as",
        "if",
        "so",
        "what",
        "how",
        "why",
        "who",
        "when",
        "where",
        "which",
        "you",
        "your",
        "have",
        "has",
        "had",
        "can",
        "could",
        "should",
        "would",
        "may",
        "tell",
        "show",
        "give",
        "make",
        "take",
        "about",
        "into",
        "through",
    }
)

# Underscore is a SEPARATOR here, not a word character, which makes
# `_derive_name` idempotent — re-deriving a name it already produced returns the
# same name.
#
# It used to be part of the token class, so a derived key came back in as ONE
# token and line ~222 stripped the underscore out of the middle of it:
#
#     "Machine Learning"  -> ['machine','learning'] -> "machine_learning"   (key)
#     "machine_learning"  -> ['machine_learning']   -> "machinelearning"    (!!)
#
# Index keys are STORED in derived form, so any caller holding a canonical key
# derived a different one and the dynamic-Y allocator minted a duplicate
# subdomain for a topic that already had one. Splitting on it instead collapses
# every spelling of a topic — spaced, hyphenated, underscored — onto one name,
# and leaves every name derived from natural prose exactly as it was.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_index() -> Dict:
    if not _INDEX_PATH.exists():
        return {
            "schema_version": 2,
            "updated_at": _now_iso(),
            "subdomains": {},
        }
    try:
        return json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("knowledge_z_index parse failed (%s); rebuilding empty", exc)
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "knowledge_z_index_load_failed",
            ok=False,
            error=str(exc),
        )
        return {
            "schema_version": 2,
            "updated_at": _now_iso(),
            "subdomains": {},
        }


def _save_index(idx: Dict) -> None:
    idx["updated_at"] = _now_iso()
    _INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _INDEX_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_INDEX_PATH)


def _salient_tokens(text: str) -> List[str]:
    if not text:
        return []
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 3 and t not in _STOPWORDS]


# A dirname carries the WHOLE topic, but a directory name still has to be a
# directory name — bound it at token boundaries so nothing is ever cut mid-word.
_MAX_DIRNAME_TOKENS = 6
_MAX_DIRNAME_CHARS = 64


def _derive_name(text: str, title: Optional[str]) -> str:
    """Pick a stable, filesystem-safe dirname from content.

    Prefer the salient title tokens (titles are deliberate and short). Fall back
    to the salient body tokens. Fall back to "general" if nothing salient
    remains.

    Keeps the FULL salient phrase, not just its head. Taking ``toks[0]`` named
    the subdomain after the first word of the topic, so Omega's live curriculum
    landed on disk as ``machine`` ("Machine Learning"), ``deep`` ("Deep
    Learning"), ``statistical`` ("Statistical Algorithms for ML") and
    ``activation`` ("Activation Functions and Backpropagation") — the same
    name-reduced-to-a-fragment defect that turned "University of Phoenix" into
    "phoenix". That cost three ways: the dirname is a lexical retrieval target,
    so "deep learning" had to match a directory called ``deep``; subfields
    looked like unrelated siblings (``machine`` beside ``deep``); and
    near-duplicates split (``statistical`` vs ``statistics``).

    Stability is the hard requirement — routing resolves a title back to its
    directory — so this stays a pure function of the salient tokens, in order,
    with a deterministic bound."""
    for source in (title, text):
        toks = _salient_tokens(source or "")
        if not toks:
            continue
        kept: List[str] = []
        for tok in toks[:_MAX_DIRNAME_TOKENS]:
            safe_tok = re.sub(r"[^a-z0-9]+", "", tok.lower())
            if not safe_tok:
                continue
            candidate = "_".join([*kept, safe_tok])
            if kept and len(candidate) > _MAX_DIRNAME_CHARS:
                break
            kept.append(safe_tok)
        safe = "_".join(kept)[:_MAX_DIRNAME_CHARS].strip("_")
        if safe:
            return safe
    return "general"


def route_knowledge_content(
    *,
    subdomain_y: int,
    subdomain_dirname: str,
    text: str,
    title: Optional[str] = None,
) -> Tuple[int, str]:
    """Allocate or look up the (Z, dirname) for content under this
    subdomain.

    Args:
        subdomain_y: the (2, Y) value the classifier picked.
        subdomain_dirname: the canonical name of the subdomain dir
            (e.g., "ai", "astronomy") — must match dewey_map.
        text: memory body text.
        title: optional title; preferred over body for naming.

    Returns:
        (z_index, dirname).

    Side effects:
        - Updates ``guardian/state/knowledge_z_index.json``.
        - Creates the cluster dir on disk if new.
    """
    name = _derive_name(text, title)

    with _LOCK:
        idx = _load_index()
        subs = idx.setdefault("subdomains", {})
        sub = subs.setdefault(
            subdomain_dirname,
            {
                "next_z": 1,
                "clusters": {},
            },
        )
        clusters: Dict[str, Dict] = sub.setdefault("clusters", {})

        existing = clusters.get(name)
        if existing is not None:
            z = int(existing.get("z", 0))
            (_KNOWLEDGE_LOBE_DIR / subdomain_dirname / name).mkdir(parents=True, exist_ok=True)
            log.debug(
                "knowledge_router: hit %s/%s (z=%d)",
                subdomain_dirname,
                name,
                z,
            )
            return z, name

        z = int(sub.get("next_z", 1))
        sub["next_z"] = z + 1
        clusters[name] = {
            "z": z,
            "created_at": _now_iso(),
        }
        _save_index(idx)

        (_KNOWLEDGE_LOBE_DIR / subdomain_dirname / name).mkdir(parents=True, exist_ok=True)

        log.info(
            "knowledge_router: allocated %s/%s (z=%d)",
            subdomain_dirname,
            name,
            z,
        )
        return z, name


def list_clusters_for_subdomain(subdomain_dirname: str) -> List[Dict]:
    """Diagnostic: return the cluster registry for a subdomain.

    Returns list of {dirname, z, created_at} dicts. Used by the
    curiosity engine + reflection layer to surface "I have N clusters
    in <subdomain>" signals.
    """
    idx = _load_index()
    sub = idx.get("subdomains", {}).get(subdomain_dirname) or {}
    clusters = sub.get("clusters") or {}
    return [{"dirname": dn, **dict(meta)} for dn, meta in clusters.items()]


def empty_subdomain_count() -> int:
    """Count how many SUBDOMAIN_MAP[(2,*)] entries have zero clusters.

    Empty subdomains are gap signals that drive the system's eagerness
    to learn. Per operator doctrine — reflection on empty domains is
    the founding act of curiosity.
    """
    from memory.dewey_map import SUBDOMAIN_MAP

    idx = _load_index()
    subs = idx.get("subdomains") or {}
    empties = 0
    for (x, _y), name in SUBDOMAIN_MAP.items():
        if x != 2:
            continue
        sub = subs.get(name) or {}
        if not (sub.get("clusters") or {}):
            empties += 1
    return empties
