"""
Response Class Registry — policy-bound response behaviours
============================================================

Loads `config/guardian_response_classes.yaml` and exposes lookup +
classification helpers. The classes are DATA — `must` / `must_never`
constraint lists with an example posture — and the voice_box renders
language INSIDE those bounds. The model never invents the class.

Public API:

    get_class(subtype) -> ResponseClass
        Look up a class by its stable subtype identifier.

    get_class_for_decision(decision_state) -> ResponseClass
        Default class for a state when no specific subtype was
        identified by Guardian's evaluation. ALLOW → "normal";
        REJECT → "security_block".

    classify_subtype(text, decision_state) -> Optional[str]
        Match user text against the trigger phrases of every class
        whose decision_state matches. Returns the subtype id of the
        first hit, or None if nothing matched (caller defaults).

    list_classes(decision_state=None) -> List[ResponseClass]
        Enumerate registered classes (optionally filtered by state).

ARCHITECTURAL RULES:
    - The YAML is read once at first call and cached. `reload()`
      forces a re-read for tests / admin tools.
    - `classify_subtype` is the ONLY place pattern → subtype mapping
      lives. Identity_guard / memory_purity report a category; this
      module decides which class within that category.
    - Adding a class requires editing the YAML AND adding any new
      trigger phrases. No new code path needed.
    - This module MUST NOT be redefined elsewhere.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.paths import OMEGA_ROOT

log = logging.getLogger("omega.guardian.response_classes")

_CLASSES_PATH = OMEGA_ROOT / "config" / "guardian_response_classes.yaml"
_CACHE: Dict[str, "ResponseClass"] = {}
_TRIGGERS: List["_CompiledTrigger"] = []
_LOCK = threading.Lock()


def _emit_rc_signal(*, status: str, failure_class: str, **fields) -> None:
    """Structured degrade surface for the response-class registry. An
    empty registry forces decision.py into REJECT-under-uncertainty
    (posture binding disarmed). Lazy import keeps this module free of
    an import-time feedback dependency; best-effort, never raises."""
    try:
        from guardian.terminal_feedback import emit_phase

        emit_phase(
            "guardian",
            "decide",
            status=status,
            summary=f"response_classes decide {status}: {failure_class}",
            component="response_classes",
            operation="decide",
            failure_class=failure_class,
            **fields,
        )
    except Exception as exc:
        log.debug("response_classes: structured emit failed: %s", exc)


@dataclass(frozen=True)
class ResponseClass:
    """One Guardian response class — policy-bound behaviour profile."""

    decision_state: str  # ALLOW | FLAG | QUARANTINE | REJECT
    subtype: str  # stable identifier
    response_mode: str  # voice_box constraint identifier
    persistence_policy: str  # canonical_commit | flagged_memory |
    # isolated_storage | hard_deny
    escalation: bool
    goal: str
    must: List[str] = field(default_factory=list)
    must_never: List[str] = field(default_factory=list)
    example_posture: str = ""
    triggers: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_state": self.decision_state,
            "subtype": self.subtype,
            "response_mode": self.response_mode,
            "persistence_policy": self.persistence_policy,
            "escalation": self.escalation,
            "goal": self.goal,
            "must": list(self.must),
            "must_never": list(self.must_never),
            "example_posture": self.example_posture,
            "triggers": list(self.triggers),
        }


@dataclass(frozen=True)
class _CompiledTrigger:
    """Pre-compiled trigger pattern for fast subtype classification."""

    pattern: re.Pattern
    subtype: str
    decision_state: str


# =====================================================================
# Defaults — used when a state has no subtype-matched class
# =====================================================================

_DEFAULT_BY_STATE = {
    "ALLOW": "normal",
    "FLAG": "emotional_projection",  # most common FLAG; safe fallback
    "QUARANTINE": "delusional_identity",  # most common Q; safe fallback
    "REJECT": "security_block",
}


# =====================================================================
# Load + cache
# =====================================================================


def _load() -> None:
    global _CACHE, _TRIGGERS
    with _LOCK:
        if _CACHE:
            return
        if not _CLASSES_PATH.exists():
            log.error("guardian_response_classes.yaml not found at %s", _CLASSES_PATH)
            _emit_rc_signal(
                status="degraded",
                failure_class="response_classes_yaml_missing",
                classes_path=str(_CLASSES_PATH),
                reason="registry empty — posture binding disarmed",
            )
            return
        try:
            from config.loader import load_guardian_response_classes

            data = load_guardian_response_classes() or {}
        except Exception as exc:
            log.error("failed to parse guardian_response_classes.yaml: %s", exc)
            _emit_rc_signal(
                status="degraded",
                failure_class="response_classes_yaml_parse_failed",
                error_type=type(exc).__name__,
                reason=str(exc)[:120],
            )
            return

        rc = data.get("response_classes") or {}
        cache: Dict[str, ResponseClass] = {}
        triggers: List[_CompiledTrigger] = []

        for cid, body in rc.items():
            if not isinstance(body, dict):
                continue
            cls = ResponseClass(
                decision_state=str(body.get("decision_state") or "ALLOW"),
                subtype=str(body.get("subtype") or cid),
                response_mode=str(body.get("response_mode") or "accept_normal"),
                persistence_policy=str(body.get("persistence_policy") or "canonical_commit"),
                escalation=bool(body.get("escalation", False)),
                goal=str(body.get("goal") or ""),
                must=list(body.get("must") or []),
                must_never=list(body.get("must_never") or []),
                example_posture=str(body.get("example_posture") or "").strip(),
                triggers=list(body.get("triggers") or []),
            )
            cache[cls.subtype] = cls

            for trig in cls.triggers:
                try:
                    pat = re.compile(re.escape(trig), re.IGNORECASE)
                    triggers.append(
                        _CompiledTrigger(
                            pattern=pat,
                            subtype=cls.subtype,
                            decision_state=cls.decision_state,
                        )
                    )
                except re.error as exc:
                    # A trigger phrase that won't compile means that
                    # class's pattern→subtype mapping is silently
                    # missing — a posture-classification blind spot.
                    log.warning(
                        "response_classes: trigger regex invalid for subtype %s — skipped",
                        cls.subtype,
                    )
                    _emit_rc_signal(
                        status="degraded",
                        failure_class="trigger_regex_invalid_skipped",
                        error_type=type(exc).__name__,
                        subtype=cls.subtype,
                        decision_state=cls.decision_state,
                        reason=str(exc)[:120],
                    )
                    continue

        _CACHE = cache
        _TRIGGERS = triggers
        log.info("response_classes loaded: %d classes, %d triggers", len(cache), len(triggers))


def reload() -> None:
    """Force-reload the YAML. For tests / admin tools."""
    global _CACHE, _TRIGGERS
    try:
        from config.loader import clear_cache

        clear_cache()
    except Exception:
        pass
    with _LOCK:
        _CACHE = {}
        _TRIGGERS = []
    _load()


# =====================================================================
# Public API
# =====================================================================


def get_class(subtype: str) -> Optional[ResponseClass]:
    """Look up a response class by its stable subtype identifier."""
    if not subtype:
        return None
    if not _CACHE:
        _load()
    return _CACHE.get(subtype)


def get_class_for_decision(decision_state: str) -> Optional[ResponseClass]:
    """Default response class for a decision state, used when no
    specific subtype was identified by Guardian's evaluation."""
    if not _CACHE:
        _load()
    default_subtype = _DEFAULT_BY_STATE.get(decision_state)
    if default_subtype:
        return _CACHE.get(default_subtype)
    return None


def classify_subtype(
    text: str,
    decision_state: Optional[str] = None,
) -> Optional[str]:
    """Match `text` against trigger phrases. Returns the first matching
    subtype id, optionally filtered by decision_state.

    Pattern → subtype is the ONLY pattern-based classification path
    Guardian uses for response selection. The blocklist regex in
    identity_policy.json decides whether something is rejected; this
    decides WHICH CLASS of rejection.
    """
    if not text or not text.strip():
        return None
    if not _CACHE:
        _load()
    text_lower = text.lower()
    for trig in _TRIGGERS:
        if decision_state and trig.decision_state != decision_state:
            continue
        if trig.pattern.search(text_lower):
            return trig.subtype
    return None


def list_classes(
    decision_state: Optional[str] = None,
) -> List[ResponseClass]:
    """Enumerate registered classes (optionally filtered by state)."""
    if not _CACHE:
        _load()
    out = list(_CACHE.values())
    if decision_state:
        out = [c for c in out if c.decision_state == decision_state]
    return out


def status() -> Dict[str, Any]:
    """Diagnostic snapshot."""
    if not _CACHE:
        _load()
    by_state: Dict[str, int] = {}
    for c in _CACHE.values():
        by_state[c.decision_state] = by_state.get(c.decision_state, 0) + 1
    return {
        "path": str(_CLASSES_PATH),
        "total_classes": len(_CACHE),
        "by_state": by_state,
        "total_triggers": len(_TRIGGERS),
    }
