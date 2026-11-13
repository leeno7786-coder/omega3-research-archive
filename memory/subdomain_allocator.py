"""Dynamic Y-level subdomain allocator (feature-flag gated).

Mirrors the `memory.knowledge_router` pattern one level up: derive a stable
dirname from content (reuse the knowledge router helper), allocate a numeric
Y coordinate on first-first-write, persist the mapping under guardian/state,
and create the directory under `memory/<LOBE_DIR>/<dirname>`.

All new behavior is gated by the caller-provided `dynamic_y_lobes` set so the
static path remains intact until the feature is enabled by operator action.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import core.paths as core_paths
import os

try:
    from memory.knowledge_router import _derive_name
except Exception as exc:
    raise ImportError(
        "memory.knowledge_router._derive_name is required by subdomain_allocator"
    ) from exc

from memory.dewey_map import LOBE_MAP

log = logging.getLogger("omega.memory.subdomain_allocator")


class DynamicSubdomainDisabledError(RuntimeError):
    """Raised when a caller attempts dynamic Y allocation while the
    feature-flag does not include the requested lobe.
    """


def _index_path():
    return core_paths.OMEGA_ROOT / "guardian" / "state" / "dynamic_y_index.json"


_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DynamicSubdomainIndexCorrupt(RuntimeError):
    """The index exists but could not be read. NEVER treated as 'empty'."""


def _load_index() -> Dict:
    """Load the dynamic-Y index. Absent = empty; unreadable = FATAL.

    Those two are not the same thing, and conflating them cost the onboarding
    terminus on 2026-08-15:

      KeyError: dynamic subdomain not found for flagged lobe coord (8,0);
                dynamic_y_index has no Y=0

    A parse failure used to log a warning and "rebuild empty". On the READ path
    that turns every flagged coordinate into the KeyError above. On the WRITE
    path it is far worse: ``get_or_create_subdomain`` loads, mutates and then
    ``_save_index``es the result, so one transient read error OVERWRITES the
    real index with an empty one — every existing subdomain mapping gone,
    ``next_y`` reset to 0, and the next allocation handing y=0 to a different
    dirname while the memories already filed under the old y=0 become
    unresolvable. A silent rebuild of an index whose whole job is to be
    authoritative is data loss wearing a warning label.

    So: an ABSENT file is a legitimately empty index (genuine first run, and
    day_zero's "clears it back to dark"). A PRESENT file we cannot read is
    corruption and raises — the same rule ``resolve_subdomain_dir_name`` already
    applies one function down, for the same reason: never guess at a mapping
    that decides where a memory lives.
    """
    idx_path = _index_path()
    if not idx_path.exists():
        return {
            "schema_version": 1,
            "updated_at": _now_iso(),
            "lobes": {},
        }
    try:
        loaded = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DynamicSubdomainIndexCorrupt(
            f"dynamic_y_index at {idx_path} exists but could not be read ({exc}). "
            "Refusing to rebuild it empty: that would overwrite every subdomain "
            "mapping and orphan the memories filed under them."
        ) from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("lobes", {}), dict):
        raise DynamicSubdomainIndexCorrupt(
            f"dynamic_y_index at {idx_path} is not a valid index document "
            f"(got {type(loaded).__name__}). Refusing to treat it as empty."
        )
    return loaded


def _save_index(idx: Dict) -> None:
    idx["updated_at"] = _now_iso()
    idx_path = _index_path()
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = idx_path.with_suffix(".json.tmp")
    # write+fsync then atomic replace (match knowledge_router pattern)
    data = json.dumps(idx, indent=2, ensure_ascii=False)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(idx_path)


def _seed_mastery_gap(domain_name: Optional[str], parent: Optional[str] = None) -> None:
    """SEED -> GAP: register a brand-new subdomain as a learning gap
    (target>0, actual=0) so its creation raises learning_debt and topic
    resolution selects it for a first LEARN. ``parent`` (a capability slug) places
    this subcat UNDER a parent in the mastery tree and links it into the parent's
    ``subdomains`` (the mind grows the tree downward; "" = top-level).

    This is the mechanism behind "anytime a new sub-cat is created it gets added
    to pressure for learning" — onboarding, a work-order, and Omega's own
    web-discovery all create subdomains through here.

    Idempotent: never overwrites an existing record (so it won't reset the
    progress of a domain Omega already learned). Failure-soft but SURFACED — a
    mastery-store hiccup must not break subdomain allocation, yet it is emitted,
    never silently swallowed.
    """
    name = (domain_name or "").strip()
    if not name:
        return
    try:
        from deepsleep_loop._deepsleep_state import MasteryRecord
        from deepsleep_loop.mastery_store import (
            load_mastery,
            normalize_domain_id,
            status_for,
            upsert_mastery,
        )
        from pressure_loop.policy import deepsleep_mastery_default_target

        domain_id = normalize_domain_id(name)
        if not domain_id or domain_id in load_mastery():
            return
        parent_slug = normalize_domain_id(str(parent or "").strip()) if parent else ""
        upsert_mastery(
            MasteryRecord(
                domain_id=domain_id,
                domain_name=name,
                knowledge_mastery=0.0,
                application_mastery=0.0,
                confidence=0.0,
                evidence_count=0,
                subdomains={},
                target_mastery=deepsleep_mastery_default_target(),
                status=status_for(0.0),
                parent=parent_slug,
            )
        )
        # Link the child INTO the parent's subdomains (downward tree navigation), if the
        # parent record exists. Failure-soft — a missing parent never blocks the child.
        if parent_slug:
            try:
                parent_rec = load_mastery().get(parent_slug)
                if parent_rec is not None and domain_id not in (parent_rec.subdomains or {}):
                    from dataclasses import replace

                    kids = dict(parent_rec.subdomains or {})
                    kids[domain_id] = name
                    upsert_mastery(replace(parent_rec, subdomains=kids))
            except Exception:
                pass
    except Exception as exc:
        log.warning("subdomain seed mastery-gap failed for %r: %s", name, exc)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "subdomain_seed_mastery_gap_failed",
                status="degraded",
                ok=False,
                error=str(exc),
                domain=name,
            )
        except Exception:
            pass


def get_or_create_subdomain(
    *,
    lobe: int,
    seed_text: str,
    title: Optional[str] = None,
    dynamic_y_lobes: set,
    parent: Optional[str] = None,
) -> Tuple[int, str]:
    """Create or return a (Y, dirname) for a derived subdomain name.

    Args:
        lobe: lobe X coordinate (e.g., 2 for knowledge, 8 for coding).
        seed_text: body text used for deterministic name derivation.
        title: optional title, preferred for name derivation.
        dynamic_y_lobes: feature-flag set of lobes that permit dynamic Y.

    Returns:
        (y_index, dirname)

    Side effects:
        - updates `guardian/state/dynamic_y_index.json`
        - creates the directory under `memory/<lobe_dir>/<dirname>` on first write

    Raises:
        DynamicSubdomainDisabledError: if lobe not in `dynamic_y_lobes`.
    """
    if lobe not in dynamic_y_lobes:
        raise DynamicSubdomainDisabledError(f"dynamic Y allocation disabled for lobe {lobe}")

    # dirname derivation reuses memory.knowledge_router._derive_name (imported
    # at module top, fail-loud) so Y-names stay aligned with Z-level naming.
    lobe_dir = LOBE_MAP.get(lobe)
    if not lobe_dir:
        raise ValueError(f"unknown lobe: {lobe}")

    dirname = _derive_name(seed_text, title)

    with _LOCK:
        idx = _load_index()
        lobes = idx.setdefault("lobes", {})
        lob = lobes.setdefault(str(lobe), {})

        # Start next_y at 0 for the allocator's own counters. Decouple from
        # any static taxonomy; the integration step will reconcile static
        # removal before enabling dynamic in production.
        if "next_y" not in lob:
            lob["next_y"] = 0

        subs = lob.setdefault("subdomains", {})

        existing = subs.get(dirname)
        if existing is not None:
            y = int(existing.get("y", 0))
            # Ensure the directory exists on disk (idempotent)
            target = core_paths.OMEGA_ROOT / "memory" / lobe_dir / dirname
            target.mkdir(parents=True, exist_ok=True)
            log.debug("subdomain_allocator: hit %s/%s (y=%d)", lobe_dir, dirname, y)
            return y, dirname

        y = int(lob.get("next_y", 0))
        lob["next_y"] = y + 1
        subs[dirname] = {"y": y, "created_at": _now_iso(), "parent": str(parent or "")}
        _save_index(idx)

        target = core_paths.OMEGA_ROOT / "memory" / lobe_dir / dirname
        target.mkdir(parents=True, exist_ok=True)

        # A newly-created knowledge/coding subdomain is a justified learning gap —
        # placed UNDER its parent capability in the mastery tree (the mind grows the
        # tree by naming a child subtopic under a broad parent; "" = top-level).
        _seed_mastery_gap(title or seed_text, parent=parent)

        log.info("subdomain_allocator: allocated %s/%s (y=%d)", lobe_dir, dirname, y)
        return y, dirname


def resolve_subdomain_dir_name(x: int, y: int) -> str:
    """Resolve ``(X, Y)`` to a directory name.

    For lobes in the dynamic flag set, consult ``dynamic_y_index.json``
    to map the allocated Y back to the dirname the allocator created.
    For all other lobes, fall through to the static ``SUBDOMAIN_MAP``.

    This keeps ``final_path_for_meta`` on the write path aligned with
    the allocator without touching ``dewey_map``.
    """
    from memory.dewey_map import SUBDOMAIN_MAP
    from memory.subdomain_flag import dynamic_y_lobes

    if x in dynamic_y_lobes():
        # Flagged lobe: the dirname MUST come from the dynamic index. A
        # flagged (x, y) with no entry is corruption — raise, never silently
        # fall back to the static map (that would resolve the memory to the
        # wrong subdomain and break recall).
        idx = _load_index()
        subs = (idx.get("lobes", {}).get(str(x), {})).get("subdomains", {})
        for dn, meta in subs.items():
            if int(meta.get("y", -1)) == y:
                return dn
        # Say WHICH failure this is. The bare "has no Y=N" cost hours on
        # 2026-08-15 because it reads identically whether the index is missing,
        # empty, or simply lacks that one coordinate — three different causes
        # with three different fixes. The state is right here; report it.
        idx_path = _index_path()
        known = sorted(int(m.get("y", -1)) for m in subs.values()) if subs else []
        raise KeyError(
            f"dynamic subdomain not found for flagged lobe coord ({x},{y}); "
            f"index={idx_path} exists={idx_path.exists()} "
            f"lobes_in_index={sorted(idx.get('lobes', {}).keys())} "
            f"subdomains_for_lobe_{x}={len(subs)} known_Y={known}"
        )

    return SUBDOMAIN_MAP.get((x, y), f"{y:02d}")


def list_subdomains_for_lobe(lobe: int):
    idx = _load_index()
    lobes = idx.get("lobes", {})
    lob = lobes.get(str(lobe)) or {}
    subs = lob.get("subdomains") or {}
    return [{"dirname": dn, **dict(meta)} for dn, meta in subs.items()]
