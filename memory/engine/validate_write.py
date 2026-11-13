"""
validate_write.py
Write a memory JSON atomically:
  1. Validate against schema
  2. Write to tmp_writes/<id>.tmp
  3. fsync and rename to final path
  4. Optionally update coord_index.json by calling reindex or patching index
"""

import json
import logging
import os
import re
import threading
from pathlib import Path
from jsonschema import Draft7Validator

from core.paths import MEMORY_ROOT, MEMORY_SCHEMA_PATH
from core.symphony_time import now_iso
from memory.deterministic import stable_hash
from memory.engine.coord_index_lock import COORD_INDEX_IO_LOCK

_log = logging.getLogger("omega.memory.engine")


def _emit(event_name: str, **data):
    try:
        from guardian.terminal_feedback import emit_guardian_event

        emit_guardian_event("omega.memory.engine", event_name, **data)
    except Exception:
        pass


# Shared with coordinate-index readers and Program 0's fixed-path builder so
# readers cannot race an atomic replacement on Windows.
_INDEX_LOCK = COORD_INDEX_IO_LOCK
_WRITE_LOCK = threading.Lock()

SCHEMA_PATH = MEMORY_SCHEMA_PATH
TMP_DIR = MEMORY_ROOT / "tmp_writes"
INDEX_PATH = MEMORY_ROOT / "indexes" / "coord_index.json"

# Cached validator — loaded once, not on every write
_VALIDATOR = None
_VALIDATOR_LOCK = threading.Lock()


def _get_validator() -> Draft7Validator:
    """Return the cached Draft7Validator, loading it on first call."""
    global _VALIDATOR
    if _VALIDATOR is not None:
        return _VALIDATOR
    with _VALIDATOR_LOCK:
        # Double-check after acquiring lock
        if _VALIDATOR is None:
            with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
                schema = json.load(f)
            _VALIDATOR = Draft7Validator(schema)
    return _VALIDATOR


def id_from_coord(coord):
    X, Y, Z = coord
    return f"{X}.{Y:02d}.{Z:04d}"


from memory.dewey_map import LOBE_MAP, KNOWLEDGE_DOMAINS
from memory.subdomain_allocator import resolve_subdomain_dir_name as _resolve_subdomain

log = logging.getLogger("omega.memory.engine.validate_write")


def _sanitize_id(raw_id: str) -> str:
    """Strip path traversal from memory IDs — keep only the basename."""
    # Remove any directory separators or traversal sequences
    sanitized = Path(str(raw_id)).name
    # Also strip leading dots to prevent hidden files
    sanitized = sanitized.lstrip(".")
    return sanitized or "unknown"


_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _sanitize_storage_dir(raw_value: str) -> str:
    """Return a filesystem-safe nested storage dirname."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(raw_value or "").strip().lower())
    sanitized = sanitized.strip("._-")
    sanitized = sanitized[:80]
    if not sanitized:
        return ""
    if sanitized in _WINDOWS_RESERVED_NAMES:
        sanitized = f"{sanitized}_item"
    return sanitized


def final_path_for_meta(meta, memory_root: Path = None) -> Path:
    """Resolve the canonical on-disk path for a memory object.

    Args:
        meta: Memory dict containing at least 'coord' and 'id'.
        memory_root: Override MEMORY_ROOT (used in tests via monkeypatching the
                     module-level name, but also accepted directly here).
    """
    root = memory_root if memory_root is not None else MEMORY_ROOT
    coord = meta["coord"]
    X, Y = coord[0], coord[1]
    lobe = LOBE_MAP.get(X, f"{X}xx")
    subdomain = _resolve_subdomain(X, Y)

    # Three-tier routing.
    #   X=2 (knowledge): lobe/subdomain/<dynamic_z_dirname>/
    #     Third level is content-driven, allocated by
    #     `memory.knowledge_router` on first write. Coord[2] is the
    #     Z index; the dirname comes from the knowledge_z_index
    #     sidecar that the router maintains. KNOWLEDGE_DOMAINS-based
    #     static second-taxonomy was retired 2026-05-08 in favor of
    #     this dynamic scheme — see the operator's Dewey-decimal
    #     design and `project_omega_mantra_deterministic_home`.
    #   X=1 (personas, 2026-05-06): lobe/subdomain/<person_id>/
    #     Each persona gets its own 3rd-tier namespace; the 'domain'
    #     field carries the person_id so search_by_decimal can scope
    #     recall. Per `project_memory_lobe_restructure`.
    #   Any other lobe: lobe/subdomain/ (flat, no 3rd tier).
    if X == 2:
        z = coord[2] if len(coord) > 2 else 0
        domain = meta.get("domain", "")
        z_dirname = ""
        # Preferred resolution: look up the Z dirname from the
        # knowledge_router's sidecar. That's where the canonical
        # mapping (subdomain, z) → dirname lives.
        if z > 0 and subdomain:
            try:
                idx_path = root.parent / "guardian" / "state" / "knowledge_z_index.json"
                if idx_path.exists():
                    z_idx = json.loads(idx_path.read_text(encoding="utf-8"))
                    sub_meta = (z_idx.get("subdomains") or {}).get(subdomain) or {}
                    for dn, cluster_meta in (sub_meta.get("clusters") or {}).items():
                        if int(cluster_meta.get("z") or 0) == z:
                            z_dirname = dn
                            break
            except Exception:
                pass
        # Fallback: caller-supplied 'domain' field (legacy KNOWLEDGE_DOMAINS
        # path or freeform string), or "general" if neither resolves.
        if not z_dirname:
            entry = KNOWLEDGE_DOMAINS.get(domain) if KNOWLEDGE_DOMAINS else None
            if entry:
                z_dirname = entry[1]
            else:
                z_dirname = domain or "general"
        dest_dir = root / lobe / subdomain / z_dirname
    elif X == 1:
        if Y == 0:
            dest_dir = root / lobe / subdomain
        else:
            # Non-operator personas sit DIRECTLY under the personas lobe, beside
            # the operator's own domain directory: `ls memory/100_personas` reads
            # operator, abel, mitchell_ray. The subdomain name for (1,1) is
            # "person_stub" -- a record TYPE -- and inserting it here put the type
            # in the tier the PERSON belongs in, so the operator and everyone else
            # had two different shapes (operator lobe/<domain>/, everyone else
            # lobe/<subdomain>/<person>/). Operator ruling 2026-07-31.
            #
            # The coordinate is unchanged: these records are still (1,1,*), and
            # `domain` remains the person_id slug used to scope search_by_decimal.
            # Only the on-disk tier moved. Must stay in lockstep with
            # core.persona_router._ensure_persona_dir, which creates it.
            domain = meta.get("domain", "")
            dest_dir = root / lobe / domain if domain else root / lobe / subdomain
    else:
        dest_dir = root / lobe / subdomain
    # Scripted / structured writers can opt into a type-specific storage folder by
    # setting `subtopic` (or legacy `topic`). This preserves coord-based routing
    # while letting memory families land in their own on-disk namespaces.
    storage_dir = _sanitize_storage_dir(meta.get("subtopic") or meta.get("topic") or "")
    if storage_dir:
        dest_dir = dest_dir / storage_dir

    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_id = _sanitize_id(meta["id"])
    final = (dest_dir / f"{safe_id}.json").resolve()
    # SECURITY: Verify resolved path is under MEMORY_ROOT
    try:
        final.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"Path traversal blocked: {meta['id']} resolves to {final}"
        ) from exc
    return final


def _stamp_write_ordinal(meta: dict) -> None:
    """Stamp the monotonic write-ordinal ONCE (recency ordering signal — record
    timestamps are deterministic content-hashes, so recency orders by this).

    Assign-once: preserved on re-commit (decay / embed-backfill re-write) so a
    record's learned-order never drifts. Lives in provenance, which the schema lets
    carry extra keys, and is excluded from the record ID (content/coord/source), so
    it perturbs neither identity nor determinism — the counter file sits under this
    module's MEMORY_ROOT, which test isolation and the determinism replay both
    redirect, giving each root a fresh counter (identical write order -> identical
    ordinals). Failure-soft: never blocks a write."""
    try:
        provenance = meta.get("provenance")
        if not isinstance(provenance, dict) or provenance.get("write_ordinal") is not None:
            return
        from memory.write_ordinal import next_write_ordinal

        counter_path = MEMORY_ROOT / "indexes" / "write_ordinal.json"
        provenance["write_ordinal"] = next_write_ordinal(counter_path)
    except Exception:
        pass  # recency is best-effort; never fail a canonical write on it


def _stamp_observed_at(meta: dict) -> None:
    """Record the record's real wall-clock creation DATE in a SIDECAR (not the record bytes — those
    stay deterministic content-hashes). Date-range temporal recall ("what did we build yesterday")
    reads it. Assign-once (a decay / embed re-write keeps the original date); the sidecar sits under
    this module's MEMORY_ROOT (isolation-correct) and is NOT in the determinism-replay whitelist, so
    the wall-clock value perturbs neither the record bytes nor replay reproducibility. Failure-soft:
    never blocks a write."""
    try:
        mid = meta.get("id")
        if not mid:
            return
        from memory.observed_at import stamp_observed_at

        stamp_observed_at(MEMORY_ROOT / "indexes" / "observed_at.json", mid)
    except Exception:
        pass  # temporal grounding is best-effort; never fail a canonical write on it


def atomic_write(
    meta: dict,
    update_index: bool = True,
    *,
    write_ordinal: int | None = None,
    stamp_observed: bool = True,
) -> str:
    """Validate *meta* against the schema, write atomically, optionally update the index.

    Returns the absolute path string of the written file.
    Raises ValueError on schema validation failure or path-traversal attempt.
    """
    memory_id = meta.get("id") or ""
    if write_ordinal is not None:
        provenance = meta.get("provenance")
        if isinstance(provenance, dict):
            provenance.setdefault("write_ordinal", int(write_ordinal))
    _stamp_write_ordinal(meta)
    if stamp_observed:
        _stamp_observed_at(meta)
    validator = _get_validator()
    errors = list(validator.iter_errors(meta))
    if errors:
        _emit("write_rejected", mem_id=memory_id, reason=errors[0].message[:120])
        raise ValueError(f"Schema validation failed: {errors[0].message}")
    _enforce_canonical_source_contract(meta)

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = _sanitize_id(meta["id"])
    tmp_file = TMP_DIR / f"{safe_id}.{stable_hash('atomic_write', meta, length=8)}.tmp"
    with _WRITE_LOCK:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())

        final_path = final_path_for_meta(meta)
        tmp_file.replace(final_path)

    if update_index:
        try:
            patch_index(meta)
        except Exception as exc:
            # HARD FAIL — silent index drift is worse than a loud
            # raise. Per user directive: "that's good system feedback
            # vs. it works but not always and no one sees it."
            #
            # The memory FILE was already written above. Caller is
            # responsible for unlinking it (e.g., via
            # `memory.rollback.rollback_canonical_write`) — atomic_write
            # raises so the caller knows to roll back.
            _emit(
                "index_patch_failed",
                mem_id=memory_id,
                reason=str(exc),
                path=str(final_path),
            )
            raise RuntimeError(
                f"index patch failed for {memory_id!r} "
                f"(file written at {final_path}; caller must roll back): "
                f"{exc}"
            ) from exc

    _emit(
        "write_complete",
        mem_id=memory_id,
        coord=meta.get("coord"),
        type=meta.get("type"),
        path=str(final_path),
    )

    return str(final_path)


def _enforce_canonical_source_contract(meta: dict) -> None:
    """Fail closed when a canonical memory bypasses the SourceLedger builder."""
    provenance = meta.get("provenance") or {}
    missing = [
        key
        for key in ("source_id", "span_hash", "source_content_hash", "ingested_at")
        if not str(provenance.get(key) or "").strip()
    ]
    audit = meta.get("audit") or []
    first_audit = audit[0] if audit and isinstance(audit[0], dict) else {}
    if not str(meta.get("id") or "").strip():
        missing.append("id")
    if not first_audit.get("when"):
        missing.append("audit[0].when")
    if provenance.get("ingested_at") and first_audit.get("when") != provenance.get("ingested_at"):
        missing.append("audit[0].when==provenance.ingested_at")
    if missing:
        raise ValueError(
            "canonical memory write missing deterministic SourceLedger contract: "
            + ", ".join(missing)
        )


def patch_index(meta: dict) -> None:
    """Thread-safe wrapper around _patch_index_unsafe."""
    with _INDEX_LOCK:
        _patch_index_unsafe(meta)


def _patch_index_unsafe(meta: dict) -> None:
    """Update coord_index.json — must be called under _INDEX_LOCK."""
    idx_path = Path(INDEX_PATH)
    if not idx_path.exists():
        # Index file not present — create it so entry can be added
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        index = {}
    else:
        try:
            with open(idx_path, "r", encoding="utf-8") as f:
                index = json.load(f)
        except json.JSONDecodeError:
            # Index corrupted — log and start fresh rather than silently wiping
            print("[validate_write] WARNING: coord_index.json corrupted, rebuilding")
            index = {}
        except Exception:
            index = {}

    # SINGLE index-entry schema. This incremental patch and the full
    # rebuild (reindex.build_index) MUST produce byte-identical entry
    # shapes, or a record's coord_index entry silently changes shape
    # the first time a full reindex runs after commit — `subtopic`
    # (absent here, present after reindex with topic-fallback) and the
    # `domain` default (None here vs "" after reindex) drifted, so
    # recall candidates (memory.api:392 / walker:892) read different
    # subtopic/domain for the SAME memory depending on index
    # provenance. Build via the canonical `extract_minimal` so there is
    # exactly one entry-schema source of truth (cannot re-drift).
    from memory.engine.reindex import extract_minimal, profile_relative_path

    entry = extract_minimal(meta)
    final_path = final_path_for_meta(meta)
    entry["path"] = profile_relative_path(final_path, memory_root=MEMORY_ROOT)
    index[meta["id"]] = entry
    # Atomic index write: write to temp then rename
    tmp_idx = idx_path.with_suffix(".tmp")
    with open(tmp_idx, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    tmp_idx.replace(idx_path)


def unpatch_index(memory_id: str) -> bool:
    """Remove a memory id from coord_index.json — the inverse of
    `patch_index`. Used by `memory.rollback` to revert a canonical
    write that needs to be undone (post-commit encoding failure).

    Returns True if the entry existed and was removed, False if the
    entry was already absent or the index file was missing. Either
    way the post-state is "id not in index" — the boolean just
    distinguishes "we did work" from "nothing to do."

    Index file write is atomic (tmp + fsync + replace) just like
    `patch_index` so a crash mid-rollback can't leave the index
    half-written.
    """
    if not memory_id:
        return False
    with _INDEX_LOCK:
        idx_path = Path(INDEX_PATH)
        if not idx_path.exists():
            return False
        try:
            with open(idx_path, "r", encoding="utf-8") as f:
                index = json.load(f)
        except (json.JSONDecodeError, OSError):
            return False
        if memory_id not in index:
            return False
        del index[memory_id]
        tmp_idx = idx_path.with_suffix(".tmp")
        try:
            with open(tmp_idx, "w", encoding="utf-8") as f:
                json.dump(index, f, indent=2, ensure_ascii=False, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            tmp_idx.replace(idx_path)
        except OSError:
            # Index write failed mid-rollback. Memory file may already
            # be deleted by the caller; index is now stale. Reindex
            # can repair. Return False so caller knows.
            return False
        return True


# Example usage
if __name__ == "__main__":
    sample_ts = now_iso()
    sample = {
        "coord": [5, 2, 44],
        "id": "5.02.0044",
        "type": "concept",
        "title": "Epistemology: justified true belief",
        "text": "Epistemology distinguishes justified true belief from mere opinion.",
        "summary": "Justified true belief as a classical account of knowledge.",
        "source": "conversation",
        "provenance": {
            "origin_id": "9.00.0150",
            "origin_type": "session",
            "author": "user:leeno",
            "ingested_at": sample_ts,
            "confidence": 0.92,
        },
        "created": sample_ts,
        "modified": sample_ts,
        "links": [[5, 9, 12], [2, 1, 3]],
        "tags": ["epistemology", "knowledge", "philosophy"],
        "flags": {
            "consolidated": False,
            "canonical": False,
            "private": False,
            "ttl_days": None,
        },
        "embeddings": {
            "symbolic": ["epistemology", "justified", "true", "belief"],
            "vector_ref": None,
        },
        "score": {
            "importance": 0.6,
            "recency": 0.8,
            "relevance_hint": 0.0,
        },
        "audit": [{"when": sample_ts, "what": "created", "by": "ingest"}],
    }
    path = atomic_write(sample, update_index=False)
    print(f"Wrote sample memory to {path}")
