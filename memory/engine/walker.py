"""
walker.py
Deterministic filesystem walker for Omega's Dewey 3D cortex.

Requires:
  - memory/ corpus (JSON files organized by Dewey lobes)
  - memory/indexes/coord_index.json (id -> {coord, path, title, type})
  - Recall is deterministic: grammar router + token overlap.
"""

import copy
from contextlib import suppress
import json
import logging
import glob
import threading
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

from core.paths import OMEGA_ROOT
from memory.core import safe_coord
from memory.engine.coord_index_lock import COORD_INDEX_IO_LOCK

log = logging.getLogger("omega.memory.engine.walker")

MEMORY_ROOT = OMEGA_ROOT / "memory"
INDEX_PATH = MEMORY_ROOT / "indexes" / "coord_index.json"

# Tunable weights
WEIGHTS = {"w_g": 3.0, "w_l": 2.0, "w_r": 1.0, "w_t": 1.5, "w_p": 2.0, "w_s": 2.5, "w_d": 3.0}

_strength_index = None


def wire_strength_index(si) -> None:
    global _strength_index
    _strength_index = si


HOP_DECAY = 0.6
DEFAULT_NEIGHBOR_RADIUS = 2
TOP_K = 20
EXPAND_DEPTH = 2
TOKEN_BUDGET = 4096

# Coord index with auto-reload — protected by lock for thread safety.
#
# AUDIT-5: COORD_INDEX is the same dict object across the lifetime of
# the process. ~25 callers `from memory.engine.walker import COORD_INDEX`,
# so REBINDING (`COORD_INDEX = {...}`) would silently break them — they
# already hold a reference to the OLD object. Instead, the reload uses
# a diff-based in-place update (apply removals first, then adds/updates)
# to minimise the window where readers see a partial state.
#
# For SAFE ITERATION, callers should use `coord_index_snapshot()` —
# returns a shallow copy taken under the lock, so the iteration is
# guaranteed to see a consistent state. Direct `COORD_INDEX.items()`
# iteration without the snapshot is RACE-PRONE under concurrent reload
# (RuntimeError: dictionary changed size during iteration).
#
# `coord_index_generation()` returns a counter that bumps on every
# successful reload — useful for callers that want to invalidate
# their own derived caches when the index changes.
_COORD_INDEX_LOCK = threading.RLock()
_coord_index_mtime: float = 0.0
_coord_index_signature: tuple[int, int, int, int] | None = None
_coord_index_generation: int = 0
COORD_INDEX: Dict[str, Dict[str, Any]] = {}


class CoordIndexUnavailable(RuntimeError):
    """The coordinate index could not provide a trustworthy recall snapshot."""

    def __init__(self, reason: str, path: Path, detail: str | None = None) -> None:
        self.reason = str(reason)
        self.path = Path(path)
        self.detail = str(detail or "")
        message = f"coordinate index unavailable ({self.reason}) at {self.path}"
        if self.detail:
            message = f"{message}: {self.detail}"
        super().__init__(message)


def _clear_coord_index_snapshot() -> None:
    """Remove the last trusted snapshot after an index read failure.

    ``COORD_INDEX`` is intentionally kept as one dict object because several
    callers import that object directly.  Clearing it in place preserves that
    identity while ensuring a failed reload cannot leave an older coordinate
    index available to a caller that inspects the snapshot after handling the
    failure.
    """
    global _coord_index_mtime, _coord_index_signature
    with _COORD_INDEX_LOCK:
        COORD_INDEX.clear()
        _coord_index_mtime = 0.0
        _coord_index_signature = None


def _validate_coord_index(data: object) -> Dict[str, Dict[str, Any]]:
    if not isinstance(data, dict):
        raise CoordIndexUnavailable("invalid_root", INDEX_PATH, type(data).__name__)
    for memory_id, entry in data.items():
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise CoordIndexUnavailable("invalid_entry", INDEX_PATH, "memory id must be text")
        if not isinstance(entry, dict):
            raise CoordIndexUnavailable(
                "invalid_entry", INDEX_PATH, f"{memory_id}: entry must be an object"
            )
        coord = entry.get("coord")
        if not isinstance(coord, list) or len(coord) < 3:
            raise CoordIndexUnavailable(
                "invalid_entry", INDEX_PATH, f"{memory_id}: coord must contain three items"
            )
        if not isinstance(entry.get("path"), str) or not entry["path"].strip():
            raise CoordIndexUnavailable(
                "invalid_entry", INDEX_PATH, f"{memory_id}: path is required"
            )
    return data


def _tokenize(text: str) -> List[str]:
    """Deterministic local tokenization — mirrors HierarchicalClassifier.tokens
    (pure word-split, no model). The sole tokenizer for recall scoring."""
    return [t for t in str(text or "").lower().split() if len(t) > 2]


def _grammar_seed(text: str) -> Tuple[int, Optional[int]]:
    """Deterministic (lobe, subdomain|None) seed — the model-free replacement
    for the classifier on the unscoped recall path. Uses the grammar router
    (route_memory_tool, which itself calls subdomain_for). Lazy import is
    cycle-safe (recall_lanes imports walker lazily). Router failures and
    malformed route results propagate; a guessed knowledge-lobe seed would
    turn an unavailable classifier into misleading recall."""
    from auto_loop.mril_tools.router import route_memory_tool

    entry = route_memory_tool(text)
    if not isinstance(entry, dict):
        raise TypeError(
            f"grammar router returned {type(entry).__name__}; expected a route object"
        )
    lobes = entry.get("allowed_lobes")
    if not isinstance(lobes, (list, tuple)) or not lobes:
        raise ValueError("grammar router returned no allowed_lobes")
    lobe = int(lobes[0])
    sub = entry.get("subdomain")
    return lobe, (int(sub) if sub is not None else None)


def load_coord_index() -> Dict[str, Dict[str, Any]]:
    """Reload the coord index from disk if it's changed on the filesystem.

    AUDIT-5 swap pattern: rather than `clear()` + `update()` (which
    leaves a window where readers see an empty dict), build the new
    state OUTSIDE the lock, then INSIDE the lock apply removals first
    and updates/adds second. The window where a key is briefly missing
    is narrowed to a single key-by-key replace + the consumer's
    `.get(idstr)` returns None for that micro-window, which is the
    same code path as a key that simply doesn't exist yet.

    The cache identity includes the nanosecond timestamp, file size, change
    time, and filesystem file identity. Mtime alone is insufficient because
    an atomic replacement can preserve it while changing the contents; that
    would otherwise let stale or partially written recall data survive.
    """
    global _coord_index_mtime, _coord_index_signature, _coord_index_generation
    # The stat/open pair must not overlap a writer's temp-file replacement.
    # On Windows a concurrent ReplaceFile can briefly make an otherwise valid
    # index unreadable to a second thread even though each file state is whole.
    with COORD_INDEX_IO_LOCK:
        if not INDEX_PATH.exists():
            _clear_coord_index_snapshot()
            raise CoordIndexUnavailable("missing", INDEX_PATH)
        try:
            stat_result = INDEX_PATH.stat()
            current_mtime = stat_result.st_mtime
            current_signature = (
                int(stat_result.st_mtime_ns),
                int(stat_result.st_size),
                int(stat_result.st_ctime_ns),
                int(stat_result.st_ino),
            )
        except OSError as exc:
            _clear_coord_index_snapshot()
            raise CoordIndexUnavailable("unreadable", INDEX_PATH, str(exc)) from exc
        if (
            _coord_index_mtime != 0.0
            and current_signature == _coord_index_signature
        ):
            return COORD_INDEX

        # Parsing is kept under the same lock as the path stat: a writer cannot
        # replace the file between the cache identity and the bytes we validate.
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                new_data = json.load(f)
        except json.JSONDecodeError as exc:
            _clear_coord_index_snapshot()
            raise CoordIndexUnavailable("invalid_json", INDEX_PATH, str(exc)) from exc
        except OSError as exc:
            _clear_coord_index_snapshot()
            raise CoordIndexUnavailable("unreadable", INDEX_PATH, str(exc)) from exc
        try:
            new_data = _validate_coord_index(new_data)
        except CoordIndexUnavailable:
            _clear_coord_index_snapshot()
            raise

        with _COORD_INDEX_LOCK:
            # Double-check after acquiring lock — another thread may have
            # already reloaded this exact file signature.
            if (
                _coord_index_mtime != 0.0
                and current_signature == _coord_index_signature
            ):
                return COORD_INDEX

            # Diff-based swap: removals first, then adds/updates.
            # In the common case (a single new memory landing) this is
            # one dict-set operation — atomic in CPython under the GIL.
            # Removals are rare (rollback / day_zero_reset) so the window
            # for "key briefly missing" is small.
            existing_keys = set(COORD_INDEX.keys())
            new_keys = set(new_data.keys())
            for k in existing_keys - new_keys:
                COORD_INDEX.pop(k, None)
            for k, v in new_data.items():
                COORD_INDEX[k] = v
            _coord_index_mtime = current_mtime
            _coord_index_signature = current_signature
            _coord_index_generation += 1
            gen = _coord_index_generation
            n_keys = len(COORD_INDEX)
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "load_coord_index",
            summary=f"generation={gen} keys={n_keys}",
            generation=gen,
            key_count=n_keys,
            ok=True,
        )
    except Exception:
        pass
    return COORD_INDEX


def coord_index_snapshot() -> Dict[str, Dict[str, Any]]:
    """Return a SHALLOW COPY of the current coord index, taken under
    the lock. Callers that iterate (`for mid, entry in snapshot.items()`)
    are guaranteed to see a consistent state regardless of concurrent
    reloads.

    AUDIT-5: prefer this over `for mid, entry in COORD_INDEX.items()`.
    Direct iteration races with concurrent `load_coord_index` calls and
    can raise `RuntimeError: dictionary changed size during iteration`.
    """
    with _COORD_INDEX_LOCK:
        return dict(COORD_INDEX)


def coord_index_generation() -> int:
    """Return a monotonically-increasing counter that bumps on every
    successful reload. Callers can use this to invalidate derived
    caches when the index changes."""
    return _coord_index_generation


def id_from_coord(coord: List[int]) -> str:
    safe = safe_coord(coord)
    X, Y, Z = safe[0], safe[1], safe[2]
    return f"{X}.{Y:02d}.{Z:04d}"


def resolve_coord_index_path(raw_path: str | Path | None) -> Optional[Path]:
    """Resolve a coord_index path to an absolute filesystem path.

    Profile-relative paths (``memory/...``) resolve against the active profile
    root and must stay under ``MEMORY_ROOT``. Absolute paths are allowed only
    when they still point inside the active memory tree; serialized indexes
    are expected to contain profile-relative paths.

    Successful resolutions are memoised. This runs 3-5 filesystem syscalls per
    call -- two ``resolve``, one or two ``exists``, plus ``MEMORY_ROOT.resolve``
    every single time -- and recall calls it once per metadata load, ~2,500
    times for a 1,008-record corpus. Measured at ~29% of an 18s LME-V2 query.
    Only successes are cached: a miss can be a file that has not been written
    yet, and caching that would make a later write invisible.
    """
    if raw_path is None:
        return None
    text = str(raw_path).strip()
    if not text:
        return None
    cached = _RESOLVED_PATH_CACHE.get(text)
    if cached is not None:
        return cached
    resolved = _resolve_coord_index_path_uncached(text)
    if resolved is not None:
        with _RESOLVED_PATH_CACHE_LOCK:
            if len(_RESOLVED_PATH_CACHE) >= _RESOLVED_PATH_CACHE_MAX:
                _RESOLVED_PATH_CACHE.pop(next(iter(_RESOLVED_PATH_CACHE)), None)
            _RESOLVED_PATH_CACHE[text] = resolved
    return resolved


def _resolve_coord_index_path_uncached(text: str) -> Optional[Path]:
    path = Path(text)
    if path.is_absolute():
        candidate = path.resolve(strict=False)
        try:
            candidate.relative_to(MEMORY_ROOT.resolve(strict=False))
        except ValueError:
            log = logging.getLogger("omega.memory.engine.walker")
            log.warning("coord_index absolute path outside MEMORY_ROOT blocked: %s", candidate)
            return None
        return candidate

    # The profile root is derived from MEMORY_ROOT (not OMEGA_ROOT) so tests
    # that repoint MEMORY_ROOT get consistent resolution and containment.
    profile_root = MEMORY_ROOT.parent if MEMORY_ROOT.name == "memory" else MEMORY_ROOT
    candidate = (profile_root / path).resolve(strict=False)
    try:
        candidate.relative_to(MEMORY_ROOT.resolve(strict=False))
    except ValueError:
        log = logging.getLogger("omega.memory.walker")
        log.warning("coord_index relative path outside MEMORY_ROOT blocked: %s", candidate)
        return None
    return candidate


def path_for_id(idstr: str) -> Optional[str]:
    entry = COORD_INDEX.get(idstr)
    if entry:
        resolved = resolve_coord_index_path(entry.get("path"))
        return str(resolved) if resolved is not None else None

    # SECURITY: Validate idstr to prevent path traversal
    import re

    if not re.match(r"^[\w.]+$", idstr):
        log = logging.getLogger("omega.memory.walker")
        log.warning("Invalid idstr format (potential traversal): %s", idstr)
        return None
    if ".." in idstr or "/" in idstr or "\\" in idstr:
        log = logging.getLogger("omega.memory.walker")
        log.warning("Path traversal blocked in idstr: %s", idstr)
        return None

    pattern = str(MEMORY_ROOT / "**" / f"{idstr}.json")
    matches = sorted(glob.glob(pattern, recursive=True))

    # SECURITY: Verify all matches are within MEMORY_ROOT
    valid_matches = []
    for m in matches:
        try:
            Path(m).resolve().relative_to(MEMORY_ROOT.resolve())
            valid_matches.append(m)
        except ValueError:
            log = logging.getLogger("omega.memory.walker")
            log.warning("Glob match outside MEMORY_ROOT blocked: %s", m)
            continue

    return valid_matches[0] if valid_matches else None


_evicted_stale_paths: set = set()

# Path → (mtime_ns, size, meta) soft cache. Invalidates on mtime/size change.
# Caps growth so long-lived workers don't retain every historical record.
# Cached as raw JSON TEXT, not as a parsed dict. Callers mutate what they get
# back, so the cache has to hand out an independent object every time. It used
# to do that with copy.deepcopy, which profiled as the single largest cost in
# recall: 4,257,608 deepcopy calls, 12.0s of a 36.7s query, from only 2,508
# loads. Re-parsing the text gives exactly the same guarantee -- json.loads
# always builds a fresh graph -- for a fraction of the work, because deepcopy
# is generic and recurses through a memo dict while the payload here is only
# ever plain JSON.
_META_CACHE: Dict[str, tuple[int, int, str]] = {}
_META_CACHE_LOCK = threading.RLock()
# LOCAL DIVERGENCE (2026-08-07, same manifest rule as below): both cache
# maxima were sized for a ~1k-record V1 corpus. The LME-V2 enterprise
# profile holds 21,346 records and the walker scores the FULL coord index
# per query, so any cap below the corpus size FIFO-evicts the entire cache
# every cycle -- zero steady-state hits, ~21k path resolutions + JSON
# re-reads per query, measured at 4-8 MINUTES per recall (py-spy: 100% of
# samples in _resolve_coord_index_path_uncached). 65536 holds the whole
# V2-small corpus; ~30-80 MB, trivial next to the rebuild's 4 GB peak.
_META_CACHE_MAX = 65536

# Upstreamed 2026-08-07 from the omega-memory-mcp eval tree (now the canonical
# home of this cache). Worth ~20% of an LME-V2 query.
#
# coord_index path -> resolved absolute Path. Keyed on the raw text, and valid
# only while OMEGA_ROOT/MEMORY_ROOT hold still; `invalidate_meta_cache(None)`
# clears it, and a profile switch reloads this module anyway.
_RESOLVED_PATH_CACHE: Dict[str, Path] = {}
_RESOLVED_PATH_CACHE_LOCK = threading.RLock()
# LOCAL DIVERGENCE (2026-08-07): raised with _META_CACHE_MAX above -- see
# that comment; 8192 < the 21,346-record V2 corpus meant full FIFO
# eviction every query.
_RESOLVED_PATH_CACHE_MAX = 65536


def invalidate_meta_cache(path: str | Path | None = None) -> None:
    """Drop cached meta for one path, or the entire cache when path is None."""
    if path is None:
        # Resolution is root-relative, so a full flush must drop it too --
        # otherwise a profile switch keeps resolving against the old root.
        with _RESOLVED_PATH_CACHE_LOCK:
            _RESOLVED_PATH_CACHE.clear()
    with _META_CACHE_LOCK:
        if path is None:
            _META_CACHE.clear()
            return
        resolved = resolve_coord_index_path(path)
        if resolved is not None:
            _META_CACHE.pop(str(resolved), None)


def load_meta_by_path(path: str) -> Dict[str, Any]:
    import logging

    log = logging.getLogger("omega.memory.walker")
    resolved = resolve_coord_index_path(path)
    if resolved is None:
        return {}
    open_path = str(resolved)
    try:
        st = resolved.stat()
        stamp = (int(st.st_mtime_ns), int(st.st_size))
    except FileNotFoundError:
        st = None
        stamp = None
    except OSError:
        st = None
        stamp = None

    if stamp is not None:
        with _META_CACHE_LOCK:
            cached = _META_CACHE.get(open_path)
        if cached is not None and cached[0] == stamp[0] and cached[1] == stamp[1]:
            try:
                decoded = json.loads(cached[2])
            except json.JSONDecodeError:
                # Unreachable in practice -- it parsed once to get here. Drop
                # the entry and re-read rather than fail a recall over it.
                with _META_CACHE_LOCK:
                    _META_CACHE.pop(open_path, None)
            else:
                return decoded if isinstance(decoded, dict) else {}

    try:
        with open(open_path, "r", encoding="utf-8") as f:
            raw_text = f.read()
        meta = json.loads(raw_text)
    except FileNotFoundError:
        # Self-heal: coord_index points at a file that no longer
        # exists (e.g., manual deletion, failed rollback that left
        # an orphan index entry). Evict the matching id once so the
        # warning doesn't fire on every recall, then suppress further
        # warnings for the same path.
        with _META_CACHE_LOCK:
            _META_CACHE.pop(open_path, None)
        if open_path not in _evicted_stale_paths:
            _evict_stale_index_entry(open_path, log)
            _evicted_stale_paths.add(open_path)
        return {}
    except json.JSONDecodeError as e:
        log.error("Invalid JSON in memory file %s: %s", open_path, e)
        return {}
    except Exception as e:
        log.error("Failed to load memory %s: %s", open_path, e)
        return {}

    if stamp is not None and isinstance(meta, dict):
        with _META_CACHE_LOCK:
            if len(_META_CACHE) >= _META_CACHE_MAX and open_path not in _META_CACHE:
                # Drop an arbitrary oldest-ish entry; exact LRU is not required.
                _META_CACHE.pop(next(iter(_META_CACHE)), None)
            _META_CACHE[open_path] = (stamp[0], stamp[1], raw_text)
        # `meta` was just parsed from `raw_text` and is shared with nobody, so
        # it is already the caller's private copy.
        return meta
    return meta if isinstance(meta, dict) else {}


def _evict_stale_index_entry(path: str, log) -> None:
    """Find the coord_index id pointing at this missing file and
    remove it. Best-effort — failures don't propagate (recall must
    keep working even if eviction fails)."""
    try:
        norm = str(Path(path).resolve()) if path else ""
    except OSError:
        norm = path
    stale_id = None
    with _COORD_INDEX_LOCK:
        for mid, entry in COORD_INDEX.items():
            entry_path = entry.get("path") or ""
            try:
                if entry_path and str(Path(entry_path).resolve()) == norm:
                    stale_id = mid
                    break
            except OSError:
                if entry_path == path:
                    stale_id = mid
                    break
    if not stale_id:
        log.warning("Memory file not found: %s (no matching coord_index entry)", path)
        return
    try:
        from memory.engine.validate_write import unpatch_index

        removed = unpatch_index(stale_id)
        with _COORD_INDEX_LOCK:
            COORD_INDEX.pop(stale_id, None)
        if removed:
            log.warning(
                "coord_index self-heal: evicted stale entry %s pointing at missing %s",
                stale_id,
                path,
            )
        else:
            log.warning(
                "coord_index self-heal: index entry %s for %s already absent on disk",
                stale_id,
                path,
            )
    except Exception as exc:
        log.error("coord_index self-heal failed for %s: %s", path, exc)


# ── Candidate selection ──


def list_ids_in_subdomain(X: int, Y: int) -> List[str]:
    return [
        idstr
        for idstr, e in coord_index_snapshot().items()
        if e.get("coord") and e["coord"][0] == X and e["coord"][1] == Y
    ]


def list_ids_in_neighborhood(X: int, Y: int, radius: int) -> List[str]:
    return [
        idstr
        for idstr, e in coord_index_snapshot().items()
        if e.get("coord") and e["coord"][0] == X and abs(e["coord"][1] - Y) <= radius
    ]


def list_ids_in_lobe(X: int) -> List[str]:
    return [
        idstr
        for idstr, e in coord_index_snapshot().items()
        if e.get("coord") and e["coord"][0] == X
    ]


def list_ids_by_domain(domain: str) -> List[str]:
    """Return all IDs where metadata domain matches (for knowledge lobe recall)."""
    return [idstr for idstr, e in coord_index_snapshot().items() if e.get("domain") == domain]


# ── Scoring ──


def geometric_score(coord: List[int], seed: Tuple[int, Optional[int]]) -> float:
    sx, sy = seed
    safe = safe_coord(coord)
    X, Y = safe[0], safe[1]
    if sx != X:
        # Different lobe: tiny, mildly favoring near-Y when a Y seed exists.
        return 0.01 / (1 + abs(Y - (sy if sy is not None else 0)))
    if sy is None:
        # Lobe-only seed (whole-lobe fan-out): uniform same-lobe score so
        # composite_score's token overlap ranks within the lobe.
        return 1.0
    dy = abs(Y - sy)
    return 1.0 / (1 + dy)


def domain_score(meta: Dict[str, Any], query_domain: str) -> float:
    """Score based on domain match — high reward for exact domain match."""
    if not query_domain:
        return 0.0
    meta_domain = meta.get("domain", "")
    if meta_domain == query_domain:
        return 1.0
    # Partial match (e.g. "neuro" in "neuroscience")
    if query_domain in meta_domain or meta_domain in query_domain:
        return 0.5
    return 0.0


def tag_score(tokens: List[str], tags: List[str], summary: str, text: str = "") -> float:
    """Score relevance via token, tag, and phrase matching.

    Three layers:
      1. Tag overlap (exact match between query tokens and memory tags)
      2. Token-in-text (individual query words found in summary/text)
      3. Bigram phrases (consecutive token pairs found as phrases — high signal)
    """
    if not tokens:
        return 0.0
    tset = set(t.lower() for t in tokens)
    tagset = set(t.lower() for t in (tags or []))
    match = len(tset & tagset)

    summary_lower = (summary or "").lower()
    text_lower = (text or "").lower()
    combined = summary_lower + " " + text_lower

    # Single-token hits
    summary_hits = sum(1 for t in tokens if t.lower() in summary_lower)
    text_hits = sum(1 for t in tokens if t.lower() in text_lower)

    # Bigram phrase matching — consecutive token pairs as exact phrases
    # "self-attention" as a phrase is much stronger signal than "self" + "attention" separately
    bigram_hits = 0
    lower_tokens = [t.lower() for t in tokens]
    for i in range(len(lower_tokens) - 1):
        # Check hyphenated form: "self-attention"
        hyphenated = f"{lower_tokens[i]}-{lower_tokens[i + 1]}"
        # Check space-separated form: "self attention"
        spaced = f"{lower_tokens[i]} {lower_tokens[i + 1]}"
        if hyphenated in combined or spaced in combined:
            bigram_hits += 1

    return match * 1.0 + summary_hits * 0.5 + text_hits * 0.3 + bigram_hits * 2.0


def link_score(links, seed_xy: Tuple[int, Optional[int]]) -> float:
    """Score links that point at the seed (X, Y).

    Tolerates link entries shaped as either:
      - list/tuple of [X, Y, ...] coordinates (legacy / canonical)
      - dict like {"x": X, "y": Y, ...} or {"coord": [X, Y, ...]}
        (older synthesized memories sometimes carry this shape)

    Skips any other shape silently — no candidate should fail recall
    just because its links field is malformed.
    """
    if not links:
        return 0.0
    sx, sy = seed_xy
    hits = 0.0
    for link in links:
        try:
            if isinstance(link, (list, tuple)) and len(link) >= 2:
                if link[0] == sx and link[1] == sy:
                    hits += 1.0
            elif isinstance(link, dict):
                # Try common dict shapes
                coord = link.get("coord")
                if isinstance(coord, (list, tuple)) and len(coord) >= 2:
                    if coord[0] == sx and coord[1] == sy:
                        hits += 1.0
                elif (
                    "x" in link
                    and "y" in link
                    and link["x"] == sx
                    and link["y"] == sy
                ):
                    hits += 1.0
        except (TypeError, KeyError, IndexError):
            continue
    return hits


def composite_score(
    meta: Dict[str, Any],
    seed_xy: Tuple[int, Optional[int]],
    tokens: List[str],
    query_domain: str = "",
) -> float:
    """Weighted composite of geometric, link, recency, tag, provenance, domain, strength."""
    coord = safe_coord(meta.get("coord", [0, 0, 0]))
    G = geometric_score(coord, seed_xy)
    L = link_score(meta.get("links", []), seed_xy)
    R = meta.get("score", {}).get("recency", 0.0)
    T = tag_score(tokens, meta.get("tags", []), meta.get("summary", ""), meta.get("text", ""))
    D = domain_score(meta, query_domain)

    si = _strength_index
    if si is None:
        # Lazy-fetch the singleton if boot hasn't wired one (e.g. test paths)
        from memory.strength_index import ensure_strength_index

        si = ensure_strength_index()
        wire_strength_index(si)
    mid = meta.get("id", "")
    STR = si.get_strength(mid) if mid else 0.0
    # Live confidence lives on StrengthIndex after recall decay; the canonical
    # provenance.confidence field is immutable (projection receipt claim hash).
    P = meta.get("provenance", {}).get("confidence", 0.5)
    if mid:
        rec = si.get_record(mid) if hasattr(si, "get_record") else None
        if isinstance(rec, dict) and rec.get("write_strength") is not None:
            with suppress(TypeError, ValueError):
                P = float(rec["write_strength"])

    # A0-ii — query-independent floor suppression. P (provenance, w_p=2)
    # and STR (strength, w_s=2.5) are NOT query-relevance signals: a
    # constitution seed with provenance.confidence=1.0 scored a flat
    # ~2.0 on EVERY recall regardless of the query, dominating the top
    # ranks and burying genuine content hits (verified: identity seeds
    # `0.00.omega_*` flooded coding/knowledge recalls). If a record has
    # ZERO query relevance — no geometric proximity, no link to the
    # seed, no token/tag overlap, no domain match — then P/STR/recency
    # alone must NOT make it a top candidate. Identity queries are
    # unaffected: the identity seeds have G>0 (seed_xy near lobe 0)
    # and/or T>0 (token overlap) there, so a relevance signal exists
    # and full scoring applies. Deterministic, single-seam.
    has_relevance = (G > 0.0) or (L > 0.0) or (T > 0.0) or (D > 0.0)
    if not has_relevance:
        return 0.0

    return (
        WEIGHTS["w_g"] * G
        + WEIGHTS["w_l"] * L
        + WEIGHTS["w_r"] * R
        + WEIGHTS["w_t"] * T
        + WEIGHTS["w_p"] * P
        + WEIGHTS["w_d"] * D
        + WEIGHTS["w_s"] * STR
    )


# ── Link expansion ──


def expand_links(seed_meta, depth, visited_ids):
    expanded = []
    frontier = seed_meta[:]
    for d in range(depth):
        next_frontier = []
        for node in frontier:
            for link in node.get("links", []):
                # Links may be either a string memory id (preferred —
                # uniquely identifies the target even when multiple
                # records share a coord) OR a legacy [X, Y, Z] coord
                # triple. id_from_coord on a coord can only resolve to
                # a synthetic id like 'X.YY.0000' which fails to find
                # records whose true id has a hash suffix; the string
                # form is direct and unambiguous.
                if isinstance(link, str):
                    idstr = link
                else:
                    idstr = id_from_coord(link)
                if idstr in visited_ids:
                    continue
                path = path_for_id(idstr)
                if not path:
                    continue
                meta = load_meta_by_path(path)
                if not meta:
                    continue
                if meta.get("flags", {}).get("private"):
                    continue
                # Resolve to chain HEAD if this link target has been
                # superseded. Without this, an older Bob persona's
                # links to (now-superseded) chicken records dead-end at
                # the obsolete chicken — voice_box surfaces stale
                # values instead of the current truth. Walking through
                # the chain keeps relationships alive across revisions.
                #
                # walk_to_head returns (head_id, head_meta, status).
                # Status surfaces chain corruption explicitly so we
                # don't silently drop links — log + skip rather than
                # surface stale, but the log lets operators see when
                # chain integrity is broken.
                if meta.get("superseded_by"):
                    try:
                        from memory.supersedes import walk_to_head

                        head_id, head_meta, status = walk_to_head(
                            idstr,
                            COORD_INDEX,
                            load_meta_by_path,
                        )
                    except Exception as exc:
                        log.warning(
                            "expand_links: walk_to_head raised for "
                            "link target %s: %s — skipping link",
                            idstr,
                            exc,
                        )
                        continue
                    if status != "ok":
                        # Chain corruption surfaced: missing entry,
                        # load failure, or cycle/max_hops. Skip the
                        # link rather than surface stale content, but
                        # log so the breakage is observable.
                        log.warning(
                            "expand_links: chain corruption "
                            "(status=%s) walking from link target %s "
                            "(reached head_id=%s) — skipping",
                            status,
                            idstr,
                            head_id,
                        )
                        continue
                    if head_meta is None:
                        # Defensive — status="ok" should always carry
                        # head_meta but never trust it implicitly.
                        log.warning(
                            "expand_links: walk_to_head returned ok "
                            "but head_meta is None for %s — skipping",
                            idstr,
                        )
                        continue
                    if head_id != idstr:
                        idstr = head_id
                        meta = head_meta
                        if idstr in visited_ids:
                            continue
                    if meta.get("flags", {}).get("private"):
                        continue
                visited_ids.add(idstr)
                # Deep copy score dict to avoid mutating the original
                score_obj = copy.deepcopy(meta.get("score", {}))
                score_obj["importance"] = score_obj.get("importance", 0.5) * (HOP_DECAY ** (d + 1))
                meta["score"] = score_obj
                # Mark this candidate as link-expanded so downstream
                # filters can distinguish records that arrived via an
                # explicit relationship edge from records that arrived
                # via classifier-routed scoring. The lane-discipline
                # filter in memory.api.read_memory exempts via_link
                # candidates: an entity-linker edge is a deliberate
                # substrate statement that "these records belong
                # together for cross-lobe traversal", which is not the
                # classifier-mispicks-lobe scenario the lane discipline
                # was built to defend against.
                meta["via_link"] = True
                expanded.append(meta)
                next_frontier.append(meta)
        frontier = next_frontier
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "expand_links",
            summary=(
                f"seed={len(seed_meta)} expanded={len(expanded)} "
                f"visited={len(visited_ids)} depth={depth}"
            ),
            seed_count=len(seed_meta),
            expanded_count=len(expanded),
            visited_count=len(visited_ids),
            depth=int(depth),
        )
    except Exception:
        pass
    return expanded


# ── Context assembly ──


def assemble_context(nodes, token_budget=TOKEN_BUDGET):
    """Build slim context dicts from ranked nodes."""
    context = []
    tokens_used = 0
    for node in nodes:
        text = node.get("text") or ""
        summary = node.get("summary") or text[:512]
        tokens = len(summary.split())
        if tokens_used + tokens > token_budget:
            break
        context.append(
            {
                "id": node.get("id"),
                "coord": node.get("coord"),
                "title": node.get("title"),
                "text": text,
                "summary": summary,
                "content": summary,
                "domain": node.get("domain", ""),
                "subtopic": node.get("subtopic", node.get("topic", "")),
                "tags": node.get("tags", []),
                "provenance": node.get("provenance"),
                "enrichment": node.get("enrichment"),
                "flags": node.get("flags", {}),
                # Preserve the link-expansion marker so memory.api can
                # exempt entity-linked records from the lane-discipline
                # filter. Without this propagation, expand_links sets
                # via_link=True on the meta but assemble_context strips
                # it when building slim context dicts and the cross-lobe
                # chain dies before the filter sees it.
                "via_link": bool(node.get("via_link", False)),
                "links": list(node.get("links") or []),
                "score": copy.deepcopy(node.get("score") or {}),
            }
        )
        tokens_used += tokens
    return context


# ── Main walker ──


def walker(
    query: str,
    radius: int = DEFAULT_NEIGHBOR_RADIUS,
    top_k: int = TOP_K,
    depth: int = EXPAND_DEPTH,
    domain: str = "",
    force_lobe: Optional[int] = None,
    allowed_subdomains: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """Deterministic recall: grammar/forced seed -> select candidates -> score -> expand.

    Seed selection:
      - explicit ``allowed_subdomains`` + ``force_lobe`` → pinned subdomain(s)
      - ``force_lobe`` only → whole-lobe fan-out (token overlap ranks)
      - unscoped → grammar router seed + full-corpus candidate pool

    """
    load_coord_index()

    deterministic_subdomain = (
        allowed_subdomains is not None and force_lobe is not None and len(allowed_subdomains) > 0
    )

    tokens = _tokenize(query)
    if deterministic_subdomain:
        X = int(force_lobe)
        Y = int(allowed_subdomains[0])
    elif force_lobe is not None:
        # Caller scoped to a lobe but no subdomain: whole-lobe fan-out,
        # ranked by token overlap (lobe-only seed). No classifier.
        X = int(force_lobe)
        Y = None
    else:
        # Unscoped: the grammar router picks the lobe (+ subdomain) as the
        # ranking seed; the pool is the full corpus for cross-lobe safety.
        X, Y = _grammar_seed(query)

    if deterministic_subdomain:
        candidate_ids: List[str] = []
        for y in allowed_subdomains:
            candidate_ids.extend(list_ids_in_subdomain(X, int(y)))
        candidate_ids = list(dict.fromkeys(candidate_ids))
    elif force_lobe is not None:
        # Whole-lobe fan-out — deterministic, never excludes a right-lobe
        # record on a subdomain guess.
        candidate_ids = list_ids_in_lobe(X)
    else:
        # Unscoped: full corpus, ranked by token overlap; the grammar seed
        # (X, Y) gives the routed lobe a proximity boost without excluding
        # other lobes.
        candidate_ids = list(COORD_INDEX.keys())

    # When domain is provided, always merge domain-matched candidates.
    # The caller is explicitly requesting a knowledge domain — this
    # overrides classification when the query misroutes away from lobe 2.
    if domain:
        domain_ids = list_ids_by_domain(domain)
        candidate_ids = list(set(candidate_ids) | set(domain_ids))

    def _score_candidate_ids(ids):
        out = []
        suppressed = 0
        for _idstr in ids:
            _entry = COORD_INDEX.get(_idstr)
            if not _entry:
                continue
            _path = _entry.get("path")
            _meta = load_meta_by_path(_path) if _path else {}
            if not _meta:
                continue
            if _meta.get("flags", {}).get("private"):
                continue
            # Skip records replaced by a newer revision. The chain HEAD
            # carries the current fact + a `history` payload added at
            # recall time; surfacing both head and historical versions
            # in the candidate pool would have voice_box weighing
            # contradictory facts as peers.
            if _meta.get("superseded_by"):
                continue
            _S = composite_score(_meta, (X, Y), tokens, query_domain=domain)
            if _S <= 0.0:
                suppressed += 1
            out.append((_S, _meta))
        if suppressed or out:
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "memory",
                    "composite_score_suppression",
                    summary=f"suppressed={suppressed} scored={len(out)}",
                    suppressed_count=suppressed,
                    scored_count=len(out),
                    query_domain=str(domain or ""),
                )
            except Exception:
                pass
        return out

    # Score candidates
    scored = _score_candidate_ids(candidate_ids)

    scored.sort(key=lambda x: x[0], reverse=True)
    seeds = [m for _, m in scored[:top_k]]
    visited_ids = set(m.get("id") for m in seeds if m.get("id"))

    expanded = expand_links(seeds, depth, visited_ids)
    merged = {n.get("id"): n for n in (seeds + expanded)}
    ordered = sorted(
        merged.values(),
        key=lambda m: composite_score(m, (X, Y), tokens, query_domain=domain),
        reverse=True,
    )
    return assemble_context(ordered)


# ---------------------------------------------------------------------
# QuerySpec adapter — used by memory.api.read_memory
# ---------------------------------------------------------------------


def walker_query(query_spec) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """[DIAGNOSTIC ONLY] Run the legacy walker query.

    Demoted under Stage 3 in favor of unified recall graph engine.
    """
    load_coord_index()

    text = getattr(query_spec, "text", "") or ""
    limit = int(getattr(query_spec, "limit", TOP_K) or TOP_K)
    domain = getattr(query_spec, "domain", "") or ""
    allowed_subdomains = getattr(query_spec, "allowed_subdomains", None)
    allowed_lobes = getattr(query_spec, "allowed_lobes", None)
    # Force the lobe whenever the caller named one (the remember_* tools always
    # do); subdomains optional. No classifier instantiated.
    force_lobe = int(allowed_lobes[0]) if allowed_lobes else None

    contexts = walker(
        text,
        top_k=limit,
        domain=domain,
        force_lobe=force_lobe,
        allowed_subdomains=allowed_subdomains,
    )

    # Re-score under the SAME deterministic seed the walker used.
    tokens = _tokenize(text)
    conf = 1.0
    if allowed_subdomains and force_lobe is not None:
        safe = [force_lobe, int(allowed_subdomains[0]), 0]
        seed_xy = (safe[0], safe[1])
        label_id = f"deterministic:{safe[0]}:{safe[1]}"
    elif force_lobe is not None:
        safe = [force_lobe, 0, 0]
        seed_xy = (force_lobe, None)
        label_id = f"deterministic:{force_lobe}:lobe"
    else:
        gx, gy = _grammar_seed(text)
        safe = [gx, gy if gy is not None else 0, 0]
        seed_xy = (gx, gy)
        label_id = f"grammar:{gx}:{gy}"

    candidates: List[Dict[str, Any]] = []
    for ctx in contexts:
        score = composite_score(ctx, seed_xy, tokens, query_domain=domain)
        ctx_flags = ctx.get("flags") or {}
        # Flag-aware recall: surface the response_mode so the voice_box
        # can render the bounded posture when it sees this candidate.
        # Quarantined memories never reach this point (separate index),
        # so only ALLOW + FLAG memories appear in candidates.
        candidates.append(
            {
                "id": ctx.get("id"),
                "coord": ctx.get("coord"),
                "score": float(score),
                "provenance": ctx.get("provenance") or {},
                "title": ctx.get("title"),
                "summary": ctx.get("summary"),
                "domain": ctx.get("domain"),
                "subtopic": ctx.get("subtopic"),
                "tags": ctx.get("tags") or [],
                "contradiction": None,
                # Guardian-set flag info (None on canonical ALLOW writes).
                "flagged": bool(ctx_flags.get("flagged", False)),
                "flag_reason": ctx_flags.get("flag_reason"),
                "flag_response_mode": ctx_flags.get("flag_response_mode"),
                # Carry the link-expansion marker through to memory.api so
                # the lane filter can exempt records reached via explicit
                # relationship edges (entity_linker output).
                "via_link": bool(ctx.get("via_link", False)),
            }
        )

    meta = {
        "method": "walker_diagnostic",
        "seed_label": label_id,
        "seed_coord": list(safe),
        "seed_confidence": float(conf),
        "domain": domain,
        "top_k": limit,
        "candidates_returned": len(candidates),
        "diagnostic": True,
    }
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "walker_query",
            summary=f"candidates={len(candidates)}",
            seed_label=label_id,
            seed_coord=list(safe),
            candidate_count=len(candidates),
            top_k=limit,
            ok=True,
        )
    except Exception:
        pass
    return candidates, meta
