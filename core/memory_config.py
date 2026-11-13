"""Governed memory cortex tunables (config + policy bundle)."""

from __future__ import annotations

from typing import Any

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "promotion": {
        "observation_to_episodic": {
            "min_confidence": 0.50,
            "requires_validation_status": "validated",
            "requires_provenance": True,
        },
        "episodic_to_semantic": {
            "min_supporting_episodes": 3,
            "min_confidence": 0.65,
            "min_age_hours": 1.0,
        },
    },
    "demotion": {
        "contradiction_threshold": 2,
        "confidence_floor": 0.20,
        "sustained_floor_cycles": 5,
    },
    "decay": {
        "confidence_decay_per_cycle": 0.005,
        "confidence_floor_no_recall": 0.10,
        "canonical_immune": True,
    },
    "contradiction": {
        "resolution": "confidence_then_evidence_then_recency",
        "loser_confidence_penalty": 0.30,
        "demotion_eligible_below": 0.25,
    },
}

_RAW_CACHE: dict[str, Any] | None = None


def reset_memory_config_for_tests() -> None:
    global _RAW_CACHE
    _RAW_CACHE = None


def _merge_section(
    merged: dict[str, Any],
    live: dict[str, Any],
    key: str,
) -> None:
    section = live.get(key)
    if not isinstance(section, dict):
        return
    base = merged.get(key)
    if not isinstance(base, dict):
        merged[key] = dict(section)
        return
    for sub_key, value in section.items():
        if isinstance(value, dict) and isinstance(base.get(sub_key), dict):
            base[sub_key].update(value)
        else:
            base[sub_key] = value


def _raw() -> dict[str, Any]:
    global _RAW_CACHE
    if _RAW_CACHE is not None:
        return _RAW_CACHE
    import copy

    merged = copy.deepcopy(_DEFAULTS)
    try:
        from config.loader import load_subsystem_bundle

        live = (load_subsystem_bundle("memory").get("config") or {}).get("memory") or {}
        if isinstance(live.get("enabled"), bool):
            merged["enabled"] = live["enabled"]
        for key in ("promotion", "demotion", "decay", "contradiction"):
            _merge_section(merged, live, key)
    except Exception:
        pass
    _RAW_CACHE = merged
    return merged


def memory_enabled() -> bool:
    return bool(_raw().get("enabled"))


def promotion_rules() -> dict[str, Any]:
    section = _raw().get("promotion")
    return dict(section) if isinstance(section, dict) else {}


def demotion_rules() -> dict[str, Any]:
    section = _raw().get("demotion")
    return dict(section) if isinstance(section, dict) else {}


def decay_rules() -> dict[str, Any]:
    section = _raw().get("decay")
    return dict(section) if isinstance(section, dict) else {}


def contradiction_rules() -> dict[str, Any]:
    section = _raw().get("contradiction")
    return dict(section) if isinstance(section, dict) else {}


def observation_to_episodic_rules() -> dict[str, Any]:
    section = promotion_rules().get("observation_to_episodic")
    return dict(section) if isinstance(section, dict) else {}


def staging_meets_observation_gate(
    memory_dict: dict[str, Any],
    envelope: dict[str, Any],
) -> tuple[bool, str, float]:
    """Minimum confidence/provenance gate for staged promotion verify."""
    rules = observation_to_episodic_rules()
    min_conf = float(rules.get("min_confidence", 0.50))
    provenance = (
        memory_dict.get("provenance") if isinstance(memory_dict.get("provenance"), dict) else {}
    )
    raw_conf = provenance.get("confidence", envelope.get("confidence", 0.0))
    try:
        confidence = float(raw_conf)
    except (TypeError, ValueError):
        confidence = 0.0

    if confidence < min_conf:
        return (
            False,
            f"confidence {confidence:.3f} below min_confidence {min_conf:.3f}",
            confidence,
        )

    if rules.get("requires_provenance") and not provenance:
        return False, "provenance required for promotion", confidence

    required_status = rules.get("requires_validation_status")
    if required_status:
        status = envelope.get("validation_status")
        if status is not None and str(status) != str(required_status):
            return (
                False,
                f"validation_status {status!r} != {required_status!r}",
                confidence,
            )

    return True, "", confidence
