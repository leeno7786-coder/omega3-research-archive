"""Governed strength-index tunables (config + policy bundle)."""

from __future__ import annotations

from typing import Any

_DEFAULT_RECENCY_HOURS = {
    "write_window_hours": 6.0,
    "recall_window_hours": 2.0,
    "hot_age_hours": 1.0,
    "cold_age_hours": 72.0,
}

_RAW_CACHE: dict[str, Any] | None = None


def reset_strength_index_config_for_tests() -> None:
    global _RAW_CACHE
    _RAW_CACHE = None


def _recency_hours() -> dict[str, float]:
    global _RAW_CACHE
    if _RAW_CACHE is not None:
        return dict(_RAW_CACHE)
    merged = dict(_DEFAULT_RECENCY_HOURS)
    try:
        from config.loader import load_subsystem_bundle

        recency = (load_subsystem_bundle("strength_index").get("config") or {}).get(
            "strength_index", {}
        ).get("recency") or {}
        if isinstance(recency, dict):
            for key in _DEFAULT_RECENCY_HOURS:
                raw = recency.get(key)
                if raw is None:
                    continue
                try:
                    merged[key] = float(raw)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    _RAW_CACHE = merged
    return dict(merged)


def aggregate_recency_seconds() -> dict[str, float]:
    hours = _recency_hours()
    return {
        "write_window_seconds": hours["write_window_hours"] * 3600.0,
        "recall_window_seconds": hours["recall_window_hours"] * 3600.0,
        "hot_age_seconds": hours["hot_age_hours"] * 3600.0,
        "cold_age_seconds": hours["cold_age_hours"] * 3600.0,
    }
