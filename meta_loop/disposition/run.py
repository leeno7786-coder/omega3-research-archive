"""Meta B disposition phase — cycle_kind → disposition + meta status."""

from __future__ import annotations

from typing import Any, Mapping, Tuple

from meta_loop.consistency.run import MetaBInput

DISPOSITIONS = ("completed", "degraded", "failed", "skipped", "idle", "invalid")
META_STATUSES = ("success", "partial", "failed", "skipped", "idle", "invalid")


def _cycle_kind_map() -> dict:
    from pressure_loop.policy import meta_b_cycle_kind_map

    return meta_b_cycle_kind_map()


def _auto_payload(auto_outcome: Mapping[str, Any]) -> dict:
    inner = auto_outcome.get("auto")
    if isinstance(inner, Mapping):
        return dict(inner)
    return dict(auto_outcome)


def _effective_cycle_kind(auto: Mapping[str, Any]) -> str:
    """Resolve spec cycle_kind from Auto payload (folded or emit shape)."""
    ck_map = _cycle_kind_map()
    top = str(auto.get("cycle_kind") or "").strip().lower()
    if top in ck_map:
        return top

    tele = dict(auto.get("telemetry") or {})
    tck = str(tele.get("cycle_kind") or "").strip().lower()
    if tck in ck_map:
        return tck
    return ""


def derive_disposition(auto: Mapping[str, Any]) -> Tuple[str, str]:
    """Map auto cycle_kind → (disposition, meta status)."""
    from pressure_loop.policy import meta_b_unknown_cycle_kind

    ck_map = _cycle_kind_map()
    ck = _effective_cycle_kind(auto)
    pair = ck_map.get(ck)
    if pair is None:
        return meta_b_unknown_cycle_kind()
    return pair


def run(mbi: MetaBInput, critical: bool) -> Tuple[str, str]:
    auto_inner = _auto_payload(mbi.auto_outcome)
    disposition, status = derive_disposition(auto_inner)
    if critical:
        disposition = "invalid"
        status = "invalid"
    return disposition, status
