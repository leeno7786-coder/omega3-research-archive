"""Pure, per-motor-pathway credit derived from verified outcomes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from schema.pathway_outcome import PathwayOutcome


def _clamp01(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def compute_pathway_credit(
    actual_success: bool,
    expected_success: float,
    novelty: float,
    repeated_failure: bool,
) -> dict[str, float]:
    """Return pathway-local reward-prediction and learning signals.

    This function is intentionally pure. It neither reads nor writes mastery;
    DeepSleep owns mastery consolidation before this credit is calculated.
    """
    expected = _clamp01(expected_success)
    actual = 1.0 if actual_success else 0.0
    novelty_value = _clamp01(novelty)
    dopamine = max(-1.0, min(1.0, actual - expected))
    plasticity = max(0.0, min(1.0, max(abs(dopamine), novelty_value)))
    stress = max(
        0.0,
        min(1.0, (0.7 if repeated_failure else 0.0) + (0.3 if not actual_success else 0.0)),
    )
    serotonin = max(
        0.0,
        min(1.0, 0.6 if actual_success and expected >= 0.5 else 0.2),
    )
    noradrenaline = max(0.0, min(1.0, 0.7 * stress + 0.3 * novelty_value))
    return {
        "dopamine": dopamine,
        "plasticity": plasticity,
        "stress": stress,
        "serotonin": serotonin,
        "noradrenaline": noradrenaline,
    }


def _mastery_pair(value: Any) -> dict[str, float]:
    state = dict(value) if isinstance(value, Mapping) else {}
    return {
        "knowledge": _clamp01(state.get("knowledge", state.get("knowledge_mastery", 0.0))),
        "application": _clamp01(state.get("application", state.get("application_mastery", 0.0))),
    }


def _record_pair(value: Any) -> tuple[str, dict[str, float]]:
    if isinstance(value, Mapping):
        domain = str(value.get("domain_id") or value.get("domain") or "")
        return domain, _mastery_pair(value)
    domain = str(getattr(value, "domain_id", "") or "")
    return domain, {
        "knowledge": _clamp01(getattr(value, "knowledge_mastery", 0.0)),
        "application": _clamp01(getattr(value, "application_mastery", 0.0)),
    }


def finalize_pathway_outcome(
    attempt_record: Mapping[str, Any] | None,
    updated_mastery: Sequence[Any] | None,
) -> PathwayOutcome | None:
    """Finalize one attempt from its pre-snapshot and real mastery return values.

    Model-claimed deltas are audit-only fields on ``attempt_record`` and are
    deliberately ignored. An unadmitted attempt receives no mastery transition,
    even if a conflicting post record is supplied.
    """
    if not isinstance(attempt_record, Mapping):
        return None
    program_id = str(attempt_record.get("program_id") or "").strip()
    pathway_id = str(attempt_record.get("pathway_id") or "").strip()
    domain = str(attempt_record.get("domain") or "").strip()
    if not all((program_id, pathway_id, domain)):
        return None

    pre = _mastery_pair(attempt_record.get("pre_mastery"))
    verdict = str(attempt_record.get("verification_verdict") or "unverified").strip().lower()
    admitted = verdict == "verified"
    post = dict(pre)
    if admitted:
        try:
            from deepsleep_loop.mastery_store import normalize_domain_id

            wanted = normalize_domain_id(domain)
        except Exception:
            wanted = domain.lower().replace(" ", "-")
        for record in updated_mastery or ():
            record_domain, pair = _record_pair(record)
            if record_domain == wanted or record_domain == domain:
                post = pair
                break

    knowledge_delta = post["knowledge"] - pre["knowledge"]
    application_delta = post["application"] - pre["application"]
    if attempt_record.get("expected_success") is None:
        # A missing expectation used to read as 0.0, so the attempt earned the whole reward as a
        # surprise. The producer always records one (measured or the labelled prior).
        raise ValueError(
            f"motor attempt {program_id!r} carries no expected_success; the reward prediction "
            "error cannot be computed without the expectation it was made against"
        )
    credit = compute_pathway_credit(
        actual_success=admitted,
        expected_success=_clamp01(attempt_record["expected_success"]),
        novelty=_clamp01(attempt_record.get("novelty", 0.0)),
        repeated_failure=bool(attempt_record.get("repeated_failure", False)),
    )
    progress_efficiency = _clamp01(max(0.0, knowledge_delta) + max(0.0, application_delta))
    return PathwayOutcome(
        pathway_id=pathway_id,
        revision=max(
            1,
            int(attempt_record.get("pathway_revision") or attempt_record.get("revision") or 1),
        ),
        task_context=str(
            attempt_record.get("task_context") or attempt_record.get("outcome_id") or program_id
        ),
        domain=domain,
        program_id=program_id,
        pre_mastery=pre,
        post_mastery=post,
        knowledge_delta=knowledge_delta,
        application_delta=application_delta,
        verification_verdict=verdict,
        progress_efficiency=progress_efficiency,
        failure_fingerprint=str(attempt_record.get("failure_fingerprint") or ""),
        neuromodulator_response=credit,
        episode_id=str(attempt_record.get("episode_id") or ""),
        outcome_id=str(attempt_record.get("outcome_id") or ""),
        pathway_method=str(attempt_record.get("pathway_method") or ""),
        material_signature=dict(attempt_record.get("material_signature") or {}),
    )


__all__ = ["compute_pathway_credit", "finalize_pathway_outcome"]
