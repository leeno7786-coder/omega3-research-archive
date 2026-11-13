"""
Supersedes-chain management for canonical memory.

When the operator revises a fact ("actually my favorite color is green
now"), the new write should NOT silently overwrite the old, AND it
should NOT create a parallel record that confuses recall. The right
shape is a chain: both files persist on disk, the new record carries
`supersedes=<old_id>` + `version=N+1`, the old record gets
`superseded_by=<new_id>` written back.

Recall walks the chain: superseded records are filtered from primary
results; their content is attached to the HEAD as `history` so the
model can articulate the revision ("X — previously Y").

This module owns:
  - `detect_collision(...)` — find an existing same-lobe record whose
    title is similar enough to the new write that it's a revision, not
    a distinct fact.
  - `link_supersedes(...)` — write the bidirectional supersedes /
    superseded_by edge after a successful commit.
  - `is_superseded(meta)` — single source of truth for "skip this".
  - `walk_to_head(idstr, COORD_INDEX, ...)` — follow superseded_by
    pointers to the current head of a chain.
  - `collect_history(head_meta, COORD_INDEX, ...)` — list older
    versions (oldest first) for a given chain head.

Companion to entity_linker: linker stitches related facts; supersedes
collapses revisions of the same fact. Both write to the .links / chain
fields after commit; recall uses both to surface the right answer.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

log = logging.getLogger("omega.memory.supersedes")


# ── Collision threshold tuning ───────────────────────────────────────
# Jaccard token overlap on titles. 0.45 catches "my favorite color is
# blue" vs "my favorite color is green" (overlap 3/5 = 0.6 with the
# stopword filter; 2/4 = 0.5 without "is"). Higher would miss real
# revisions; lower would conflate distinct facts.
_JACCARD_THRESHOLD = 0.45

# Leading-prefix match: same first N non-stopword tokens. Catches
# revisions where the value changes at the END ("color is blue" vs
# "color is green") even when Jaccard alone wouldn't fire.
_PREFIX_MATCH_TOKENS = 3

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS: Set[str] = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "of",
    "to",
    "for",
    "in",
    "on",
    "at",
    "by",
    "with",
    "from",
    "as",
    "that",
    "this",
    "it",
    "and",
    "or",
    # Correction-marker words. The brain routes "actually X" to the
    # same lobe as the original X (per memory_operations._route_write
    # correction branch), but the literal "actually" / "now" tokens
    # would defeat title overlap. Treating them as stopwords lets
    # the chain detect "my favorite color is purple" ↔ "actually my
    # favorite color is teal now" as a revision pair.
    "actually",
    "instead",
    "rather",
    "now",
    "wrong",
    "correction",
    "update",
}

# Negation / antonym markers. When the new title contains one of these
# and the prior does NOT (or vice versa), the records describe
# semantically OPPOSITE facts — not revisions of the same fact. Without
# this guard, "my LEAST favorite color is blue" would supersede "my
# favorite color is blue" via Jaccard 0.75 (3 shared tokens out of 4).
# The "no" stopword above was deliberately removed from STOPWORDS and
# pulled into this set instead — it carries semantic polarity ("X is
# my dad" vs "X is no relation of mine") that must NOT be normalized
# away during collision detection.
_NEGATION_MARKERS: Set[str] = {
    "least",
    "no",
    "not",
    "never",
    "without",
    "hate",
    "dislike",
    "loathe",
    "ex",
}

# Lobes that participate in supersedes chains. Sourced from
# memory.dewey_map.ENTITY_BEARING_LOBES — the single canonical set
# also used by entity_linker, brain.retrieve, and the post-commit
# hooks. Identity (0) is constitution-shaped and uses different write
# paths; coding / knowledge / journal records are domain-stable.
# Add lobes in dewey_map, not here.
from memory.dewey_map import ENTITY_BEARING_LOBES as _CHAIN_LOBES


def _tokens(s: str) -> List[str]:
    """Non-stopword tokens. Negation markers stay (they're load-bearing
    polarity signal for `_negation_mismatch` below)."""
    lowered = (s or "").lower()
    return [t for t in _TOKEN_RE.findall(lowered) if t not in _STOPWORDS and len(t) > 1]


def _token_set(s: str) -> Set[str]:
    return set(_tokens(s))


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _has_negation(tokens_or_text) -> bool:
    """Detect any negation/antonym marker in token list or raw text."""
    if isinstance(tokens_or_text, str):
        toks = set(_TOKEN_RE.findall(tokens_or_text.lower()))
    else:
        toks = set(tokens_or_text)
    return bool(toks & _NEGATION_MARKERS)


def _negation_mismatch(new_title: str, prior_title: str) -> bool:
    """True iff exactly one of the two titles contains a negation
    marker. Indicates semantic inversion — the records describe
    OPPOSITE facts even when token overlap is high. Skip collision
    detection when this fires; both records persist as distinct
    facts rather than collapsing into a chain.
    """
    return _has_negation(new_title) != _has_negation(prior_title)


def _prefix_match(a: List[str], b: List[str], n: int = _PREFIX_MATCH_TOKENS) -> bool:
    """True iff the first `n` non-stopword tokens are identical.

    Catches end-of-sentence revisions where Jaccard alone misses
    (e.g. "color is blue" vs "color is green" share 2/3 = 0.67 Jaccard
    but the trailing token differs). Effective N adapts to the shorter
    of the two lists so SHORT preference facts ("my age is 30" → tokens
    ["age","30"]) can still chain — the original `len < n` guard was
    silently dropping all 2-token revisions.
    """
    effective = min(n, len(a), len(b))
    if effective < 2:
        # Need at least 2 shared leading tokens to call this a revision
        # of the same fact; 1-token overlap is too noisy (every "I X"
        # statement would prefix-match every other "I X").
        return False
    return a[:effective] == b[:effective]


def is_superseded(meta: Optional[Dict[str, Any]]) -> bool:
    """Single check used by walker, entity_linker, and recall to skip
    chain-replaced records."""
    if not meta:
        return False
    return bool(meta.get("superseded_by"))


def detect_collision(
    new_title: str,
    new_content: str,
    new_id: str,
    lobe: int,
    coord_index: Dict[str, Dict[str, Any]],
    load_meta: callable,
    new_domain: str = "",
    new_subtopic: str = "",
) -> Optional[Dict[str, Any]]:
    """Find an existing same-lobe HEAD whose title overlaps enough with
    `new_title` to be a revision target. Returns the prior record's full
    meta (loaded) or None.

    "HEAD" = no superseded_by. We never link a new write to an
    already-superseded record — it would form a side-chain that recall
    can't reconcile. Skip records where content is identical (no-op
    revision); those don't need a chain edge.
    """
    if lobe not in _CHAIN_LOBES:
        return None
    new_tokens_list = _tokens(new_title)
    new_tokens_set = set(new_tokens_list)
    if not new_tokens_set:
        return None
    new_content_clean = (new_content or "").strip().lower()
    new_domain_clean = str(new_domain or "").strip().lower()
    new_subtopic_clean = str(new_subtopic or "").strip().lower()

    best: Optional[Tuple[float, Dict[str, Any]]] = None
    for other_id, entry in coord_index.items():
        if other_id == new_id:
            continue
        other_coord = entry.get("coord") or []
        if not other_coord or other_coord[0] != lobe:
            continue
        other_path = entry.get("path")
        if not other_path:
            continue
        try:
            other_meta = load_meta(other_path)
        except Exception:
            continue
        if not other_meta:
            continue
        if other_meta.get("superseded_by"):
            continue  # don't chain into already-superseded records
        # Structured leaves can share a strong lexical prefix while representing
        # independent facts ("Mitchell Ray — friend" vs "Mitchell Ray — business
        # partner", or the operator-side reverse projection of either). Revision
        # chains are valid only inside the same declared entity/facet namespace.
        # Legacy records without these fields retain the title-based fallback.
        other_domain = str(other_meta.get("domain") or "").strip().lower()
        if new_domain_clean and other_domain and new_domain_clean != other_domain:
            continue
        other_subtopic = str(other_meta.get("subtopic") or "").strip().lower()
        if new_subtopic_clean and other_subtopic and new_subtopic_clean != other_subtopic:
            continue
        other_title = other_meta.get("title") or ""
        other_tokens_list = _tokens(other_title)
        other_tokens_set = set(other_tokens_list)
        if not other_tokens_set:
            continue
        # Semantic-inversion guard. "my LEAST favorite color is blue"
        # would otherwise supersede "my favorite color is blue" via
        # Jaccard alone — they share 3/4 = 0.75 of tokens but mean
        # opposite things. Skip collision when negation polarity
        # differs between the two titles.
        if _negation_mismatch(new_title, other_title):
            continue
        sim = _jaccard(new_tokens_set, other_tokens_set)
        prefix_hit = _prefix_match(new_tokens_list, other_tokens_list)
        if sim < _JACCARD_THRESHOLD and not prefix_hit:
            continue
        # No-op revision: content unchanged. Skip — same record, no
        # need to bump a version.
        other_content = (other_meta.get("text") or other_meta.get("summary") or "").strip().lower()
        if other_content and other_content == new_content_clean:
            continue
        # Prefer the highest-similarity match. Ties broken by recency
        # via lexicographic ID compare (newer hex IDs sort later for
        # the same wall-clock second, harmless either way).
        score = sim + (0.5 if prefix_hit else 0.0)
        if best is None or score > best[0]:
            best = (score, other_meta)
    return best[1] if best else None


def link_supersedes(
    new_meta_path: str,
    new_meta: Dict[str, Any],
    prior_meta: Dict[str, Any],
    prior_path: str,
) -> Dict[str, Any]:
    """Write the bidirectional supersedes / superseded_by edge.

    Atomic-ish: if the prior back-link write fails AFTER the new-record
    write succeeded, REVERT the new record's `supersedes`/`version`
    fields on disk so the chain is not left half-formed. Without this
    revert, the new record points at a prior that has no back-link,
    `walk_to_head` started from the prior returns the prior as head
    (no `superseded_by`), and recall surfaces both records as competing
    HEADs — the exact deterministic-recall break the supersedes module
    exists to prevent.

    Returns a status dict; `ok=False` callers MUST log .error and emit
    a phase status="fail" so chain corruption is observable, not just
    silently swallowed.
    """
    prior_id = prior_meta.get("id") or ""
    new_id = new_meta.get("id") or ""
    prior_version = int(prior_meta.get("version", 1) or 1)
    new_version = prior_version + 1

    # Snapshot the new record's pre-mutation chain fields so we can
    # revert if the prior back-link write fails.
    pre_supersedes = new_meta.get("supersedes")
    pre_version = new_meta.get("version", 1)
    pre_superseded_by = new_meta.get("superseded_by")

    # Cut 4 rework 4 (CUT4_REVIEW_REWORK_4 §5): every canonical rewrite below
    # is an exact-path ATOMIC replacement — a failure leaves the previous
    # canonical bytes byte-identical; the revert restores the EXACT pre-hook
    # bytes of the new record (not a re-serialisation).
    from memory.atomic_replace import atomic_replace_bytes, atomic_replace_json

    try:
        pre_new_bytes: bytes | None = Path(new_meta_path).read_bytes()
    except OSError:
        pre_new_bytes = None

    # Set forward edge on the new record.
    new_meta["supersedes"] = prior_id
    new_meta["version"] = new_version
    new_meta.setdefault("superseded_by", None)
    try:
        atomic_replace_json(new_meta_path, new_meta)
    except Exception as exc:
        # Restore the in-memory dict so the caller doesn't proceed
        # with a half-mutated record.
        new_meta["supersedes"] = pre_supersedes
        new_meta["version"] = pre_version
        if pre_superseded_by is None:
            new_meta.pop("superseded_by", None)
        else:
            new_meta["superseded_by"] = pre_superseded_by
        log.error("link_supersedes: write_new failed for %s: %s", new_meta_path, exc)
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "supersedes_link",
            status="fail",
            ok=False,
            reason="write_new_failed",
            new_id=new_id,
            error=str(exc),
        )
        return {
            "ok": False,
            "phase": "write_new",
            "new_id": new_id,
            "prior_id": prior_id,
            "error": str(exc),
        }

    # Set back-edge on the prior record.
    pre_prior_superseded_by = prior_meta.get("superseded_by")
    pre_prior_version = prior_meta.get("version")
    prior_meta["superseded_by"] = new_id
    prior_meta.setdefault("version", prior_version)
    try:
        atomic_replace_json(prior_path, prior_meta)
    except Exception as exc:
        # Back-link write failed AFTER the new record was already
        # persisted with `supersedes=<prior_id>`. Revert BOTH:
        #   - in-memory prior_meta (we mutated it above)
        #   - on-disk new record (clear its chain fields)
        # If THAT revert also fails, log .critical — the substrate is
        # in an inconsistent state that needs operator attention.
        if pre_prior_superseded_by is None:
            prior_meta.pop("superseded_by", None)
        else:
            prior_meta["superseded_by"] = pre_prior_superseded_by
        if pre_prior_version is None:
            prior_meta.pop("version", None)
        else:
            prior_meta["version"] = pre_prior_version

        new_meta["supersedes"] = pre_supersedes
        new_meta["version"] = pre_version
        if pre_superseded_by is None:
            new_meta.pop("superseded_by", None)
        else:
            new_meta["superseded_by"] = pre_superseded_by
        try:
            if pre_new_bytes is not None:
                atomic_replace_bytes(new_meta_path, pre_new_bytes)  # exact pre-hook bytes
            else:
                atomic_replace_json(new_meta_path, new_meta)
            revert_status = "reverted"
        except Exception as revert_exc:
            log.critical(
                "link_supersedes: prior back-link FAILED and new-record "
                "revert ALSO FAILED — chain is half-formed on disk. "
                "new=%s prior=%s back_link_error=%s revert_error=%s",
                new_meta_path,
                prior_path,
                exc,
                revert_exc,
            )
            revert_status = "revert_failed"
        log.error(
            "link_supersedes: write_prior failed for %s: %s (new-record revert: %s)",
            prior_path,
            exc,
            revert_status,
        )
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "supersedes_link",
            status="fail",
            ok=False,
            reason="write_prior_failed",
            prior_id=prior_id,
            error=str(exc),
        )
        return {
            "ok": False,
            "phase": "write_prior",
            "new_id": new_id,
            "prior_id": prior_id,
            "error": str(exc),
            "revert": revert_status,
        }

    return {
        "ok": True,
        "new_id": new_id,
        "prior_id": prior_id,
        "new_version": new_version,
        "prior_version": prior_version,
    }


def walk_to_head(
    start_id: str,
    coord_index: Dict[str, Dict[str, Any]],
    load_meta: callable,
    *,
    max_hops: int = 32,
) -> Tuple[str, Optional[Dict[str, Any]], str]:
    """Follow superseded_by pointers until we reach the HEAD.

    Returns ``(head_id, head_meta, status)`` where status is one of:

    - ``"ok"`` — clean walk, head_meta is the current chain head
    - ``"missing_entry"`` — chain pointer references an id not in the
      coord_index (deleted record, partial wipe, missing reindex);
      head_meta is the LAST successfully loaded meta, head_id is the
      orphan pointer
    - ``"load_failed"`` — disk read raised; head_meta is the last
      successfully loaded meta
    - ``"max_hops"`` — chain depth exceeded (likely a cycle); head_meta
      is whatever the cycle landed on
    - ``"empty"`` — start_id resolved nothing (start of chain itself
      was missing or unloadable)

    Callers MUST branch on status. Treating non-"ok" as "ok" silently
    surfaces stale or misattributed content into recall — which was
    the whole class of bugs this distinction exists to prevent.
    """
    cur_id = start_id
    last_good_id = start_id
    last_good_meta: Optional[Dict[str, Any]] = None
    visited: Set[str] = set()
    for _ in range(max_hops):
        if cur_id in visited:
            # Cycle detected. last_good_meta is the most recent
            # well-loaded record on the walk.
            return last_good_id, last_good_meta, "max_hops"
        visited.add(cur_id)
        entry = coord_index.get(cur_id)
        if not entry:
            status = "missing_entry" if last_good_meta is not None else "empty"
            return cur_id, last_good_meta, status
        try:
            cur_meta = load_meta(entry.get("path"))
        except Exception:
            return last_good_id, last_good_meta, "load_failed"
        if not cur_meta:
            return last_good_id, last_good_meta, "load_failed"
        last_good_id, last_good_meta = cur_id, cur_meta
        next_id = cur_meta.get("superseded_by")
        if not next_id:
            return cur_id, cur_meta, "ok"
        cur_id = next_id
    return last_good_id, last_good_meta, "max_hops"


def collect_history(
    head_meta: Dict[str, Any],
    coord_index: Dict[str, Dict[str, Any]],
    load_meta: callable,
    *,
    max_hops: int = 32,
) -> List[Dict[str, Any]]:
    """Walk BACKWARD from the head via `supersedes` and return the
    older versions oldest-first. The head itself is NOT included.

    Used by recall to attach `history` to a returned candidate so the
    model can articulate revisions ("color is green — was blue").
    """
    history_rev: List[Dict[str, Any]] = []
    cur_id = head_meta.get("supersedes")
    for _ in range(max_hops):
        if not cur_id:
            break
        entry = coord_index.get(cur_id)
        if not entry:
            break
        try:
            prev = load_meta(entry.get("path"))
        except Exception:
            break
        if not prev:
            break
        history_rev.append(
            {
                "id": prev.get("id"),
                "version": int(prev.get("version", 1) or 1),
                "title": prev.get("title"),
                "summary": (prev.get("summary") or prev.get("text") or "")[:240],
                "created_at": (prev.get("provenance") or {}).get("ingested_at"),
            }
        )
        cur_id = prev.get("supersedes")
    history_rev.reverse()  # oldest first
    return history_rev
