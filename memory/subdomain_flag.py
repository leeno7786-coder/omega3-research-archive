"""Feature-flag reader for dynamic Y-level subdomain allocation.

Returns the set of lobe IDs whose Y coordinate is allocated dynamically
(via ``memory.subdomain_allocator``) rather than predicted by SetFit.

Source of truth: ``config/memory.yaml`` → ``memory.dynamic_y_lobes``.
Default ``[]`` keeps the static path byte-for-byte unchanged.
"""

from __future__ import annotations

import json
from typing import FrozenSet, Iterable

_RAW_CACHE: list | None = None
_FLAG_CACHE: FrozenSet[int] | None = None


def reset_flag_cache() -> None:
    """Drop the cached flag set.  Useful for tests + operator hot-reload."""
    global _RAW_CACHE, _FLAG_CACHE
    _RAW_CACHE = None
    _FLAG_CACHE = None


def _override_path():
    """Runtime first-wake override file (under the isolated state dir).

    The committed config default stays ``[]`` (dark); onboarding's first-wake
    flip writes this override so the running system treats lobes 2/8 as dynamic
    without mutating tracked config. day_zero clears it back to dark.
    """
    from core import paths as core_paths

    return core_paths.OMEGA_ROOT / "guardian" / "state" / "dynamic_y_lobes.json"


def _read_override() -> list | None:
    try:
        path = _override_path()
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else None
    except Exception:
        return None


def dynamic_y_lobes() -> FrozenSet[int]:
    """Return the set of lobe IDs enabled for dynamic Y allocation.

    Reads the first-wake override (if present) with priority, else the governed
    memory config bundle (cached after first call). Returns an empty frozenset
    when neither names a lobe — the safe default that preserves the static path.
    """
    global _RAW_CACHE, _FLAG_CACHE
    if _FLAG_CACHE is not None:
        return _FLAG_CACHE

    raw = _read_override()
    if raw is None:
        raw = []
        try:
            from config.loader import load_subsystem_bundle

            bundle = load_subsystem_bundle("memory")
            mem = (bundle.get("config") or {}).get("memory") or {}
            candidate = mem.get("dynamic_y_lobes")
            if isinstance(candidate, list):
                raw = candidate
        except Exception:
            pass

    _RAW_CACHE = raw
    _FLAG_CACHE = frozenset(int(x) for x in raw if isinstance(x, (int, float)))
    return _FLAG_CACHE


def set_dynamic_y_lobes(lobes: Iterable[int]) -> FrozenSet[int]:
    """First-wake flip: persist the dynamic-Y lobe set to the runtime override
    and drop the cache. Idempotent; returns the new set."""
    wanted = sorted({int(x) for x in lobes})
    path = _override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(wanted), encoding="utf-8")
    reset_flag_cache()
    return frozenset(wanted)
