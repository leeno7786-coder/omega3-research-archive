"""Pressure loop + neuromod observability — structured guardian/logs persistence.

Every gather/evaluate/neuromod/tonic transition writes to ``guardian/logs/pressure.jsonl``
via :func:`guardian.data_logger.emit_event` (failure-soft; never breaks ticks).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

_SUBSYSTEM = "omega.pressure.loop"


def _emit(event: str, **data: Any) -> None:
    try:
        from guardian.terminal_feedback import emit_guardian_event

        emit_guardian_event(_SUBSYSTEM, event, **data)
    except Exception:
        pass


def _clamp4(value: Any) -> float:
    try:
        return round(float(value or 0.0), 4)
    except (TypeError, ValueError):
        return 0.0


def neuromod_payload(neuromod: Optional[Mapping[str, Any]]) -> dict[str, float]:
    nm = dict(neuromod or {})
    return {
        "dopamine": _clamp4(nm.get("dopamine")),
        "serotonin": _clamp4(nm.get("serotonin")),
        "stress": _clamp4(nm.get("stress")),
        "spike": _clamp4(nm.get("spike")),
    }


def weights_map(contributors: Sequence[Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for c in contributors or ():
        try:
            out[str(getattr(c, "type", "") or "")] = _clamp4(getattr(c, "weight", 0.0))
        except Exception:
            continue
    return out


def contributors_payload(contributors: Sequence[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for c in contributors or ():
        try:
            rows.append(
                {
                    "source": str(getattr(c, "source", "") or ""),
                    "type": str(getattr(c, "type", "") or ""),
                    "weight": _clamp4(getattr(c, "weight", 0.0)),
                    "metadata": dict(getattr(c, "metadata", None) or {}),
                }
            )
        except Exception:
            continue
    return rows


def ranked_pairs(contributors: Sequence[Any]) -> list[list[Any]]:
    pairs = sorted(
        (
            (str(getattr(c, "type", "") or ""), _clamp4(getattr(c, "weight", 0.0)))
            for c in contributors or ()
        ),
        key=lambda kv: -kv[1],
    )
    return [[k, w] for k, w in pairs if w > 0.001]


def log_gather_snapshot(contributors: Sequence[Any], **extra: Any) -> None:
    _emit(
        "gather_contributors_snapshot",
        contributor_count=len(contributors or ()),
        aggregate_top=ranked_pairs(contributors)[:8],
        weights=weights_map(contributors),
        contributors=contributors_payload(contributors),
        **extra,
    )


def log_contributor_field(
    name: str, weight: float, details: Optional[dict] = None, **extra: Any
) -> None:
    _emit(
        "contributor_field_read",
        field_id=str(name or ""),
        weight=_clamp4(weight),
        details=dict(details or {}),
        **extra,
    )


def log_neuromod_tonic_transition(
    hook: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    **extra: Any,
) -> None:
    b = neuromod_payload(before)
    a = neuromod_payload(after)
    _emit(
        "neuromod_tonic_transition",
        hook=str(hook or ""),
        before=b,
        after=a,
        delta={
            k: _clamp4(a.get(k, 0.0) - b.get(k, 0.0))
            for k in ("dopamine", "serotonin", "stress", "spike")
        },
        **extra,
    )


def log_neuromod_apply_before(
    neuromod: Mapping[str, Any],
    contributors: Sequence[Any],
    *,
    context: str = "",
    **extra: Any,
) -> None:
    _emit(
        "neuromod_apply_before",
        context=str(context or ""),
        neuromod=neuromod_payload(neuromod),
        weights_before=weights_map(contributors),
        contributors=contributors_payload(contributors),
        **extra,
    )


def log_neuromod_apply_after(
    neuromod: Mapping[str, Any],
    contributors: Sequence[Any],
    *,
    gamma: float,
    patience: float,
    context: str = "",
    **extra: Any,
) -> None:
    _emit(
        "neuromod_apply_after",
        context=str(context or ""),
        neuromod=neuromod_payload(neuromod),
        gamma=_clamp4(gamma),
        patience=_clamp4(patience),
        weights_after=weights_map(contributors),
        contributors=contributors_payload(contributors),
        **extra,
    )


def log_evaluate_neuromod_source(source: str, neuromod: Mapping[str, Any], **extra: Any) -> None:
    _emit(
        "evaluate_neuromod_source",
        source=str(source or ""),
        neuromod=neuromod_payload(neuromod),
        **extra,
    )


def log_evaluate_raw_contributors(contributors: Sequence[Any], **extra: Any) -> None:
    _emit(
        "evaluate_contributors_raw",
        weights=weights_map(contributors),
        contributors=contributors_payload(contributors),
        **extra,
    )


def log_ignition_gate_detail(
    *,
    modulated: Sequence[Any],
    neuromod: Mapping[str, Any],
    ceiling: float,
    margin: float,
    dominant_type: Optional[str],
    suppress_reason: str,
    **extra: Any,
) -> None:
    pairs = ranked_pairs(modulated)
    top_key, top_w = pairs[0] if pairs else ("-", 0.0)
    second_w = pairs[1][1] if len(pairs) >= 2 else 0.0
    decision = f"IGNITE:{dominant_type}" if dominant_type else (suppress_reason or "no_ignition")
    _emit(
        "ignition_gate_detail",
        top=top_key,
        top_weight=top_w,
        second_weight=second_w,
        gap=_clamp4(top_w - second_w),
        ceiling=_clamp4(ceiling),
        margin=_clamp4(margin),
        neuromod=neuromod_payload(neuromod),
        decision=decision,
        ranked=pairs[:12],
        contributors=contributors_payload(modulated),
        **extra,
    )


def log_tick_summary(*, tick: int, ignited: bool, **extra: Any) -> None:
    _emit("pressure_tick_summary", tick=int(tick), ignited=bool(ignited), **extra)


def log_episode_neuromod_compute_before(
    *,
    episode_id: str,
    baseline: float,
    auto: Mapping[str, Any],
    **extra: Any,
) -> None:
    tele = dict((auto or {}).get("telemetry") or {})
    _emit(
        "episode_neuromod_compute_before",
        episode_id=str(episode_id or ""),
        baseline_success_rate=_clamp4(baseline),
        auto_status=str((auto or {}).get("status") or ""),
        cycle_kind=str(tele.get("cycle_kind") or ""),
        failure_kind=str((auto or {}).get("failure_kind") or ""),
        **extra,
    )


def log_episode_neuromod_compute_after(
    *,
    episode_id: str,
    neuromod: Mapping[str, Any],
    baseline: float,
    **extra: Any,
) -> None:
    _emit(
        "episode_neuromod_compute_after",
        episode_id=str(episode_id or ""),
        baseline_success_rate=_clamp4(baseline),
        neuromod=neuromod_payload(neuromod),
        **extra,
    )
