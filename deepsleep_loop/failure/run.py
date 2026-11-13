"""DeepSleep Phase 7 — failure pattern consolidation."""

from __future__ import annotations

from typing import Any, Optional

from deepsleep_loop._deepsleep_state import DeepSleepInput, FailurePattern
from deepsleep_loop.failure_store import failure_key, load_failures, upsert_failure
from deepsleep_loop.store_locks import FAILURE, store_transaction
from cognition.topic_resolution.domain_key import canonical_domain_key


def episode_prior_failures(
    patterns: list[FailurePattern],
    *,
    episode_id: str,
    fallback: dict[str, FailurePattern] | None,
) -> dict[str, FailurePattern]:
    """Each pattern's count BEFORE this episode, read from its own receipt (WO-07).

    On a retry the store already carries this episode's count; measuring against it would
    report no change and drop the failure from the journal row and the sealed record.
    """
    from dataclasses import replace

    from deepsleep_loop.effect_receipts import receipts_for

    prior = dict(fallback or {})
    for pattern in patterns:
        mine = receipts_for(pattern.effect_receipts, episode_id)
        if not mine:
            continue
        before = int(next(iter(mine.values())).get("frequency_before", 0) or 0)
        key = failure_key(pattern.kind, pattern.context)
        if before <= 0:
            prior.pop(key, None)
        else:
            prior[key] = replace(pattern, frequency=before)
    return prior


def failure_effects_from_patterns(
    patterns: list[FailurePattern],
    *,
    prior: dict[str, FailurePattern] | None,
) -> dict[str, dict[str, int | str]]:
    """Build journal failure_effects from updated patterns."""
    prior = prior or {}
    effects: dict[str, dict[str, int | str]] = {}
    for pattern in patterns:
        key = failure_key(pattern.kind, pattern.context)
        old = prior.get(key)
        prior_freq = int(old.frequency) if old else 0
        freq_delta = int(pattern.frequency) - prior_freq
        if freq_delta == 0 and not old:
            effects[key] = {
                "kind": pattern.kind,
                "context": pattern.context,
                "frequency": int(pattern.frequency),
                "last_episode": pattern.last_episode,
            }
            continue
        if freq_delta != 0:
            effects[key] = {
                "kind": pattern.kind,
                "context": pattern.context,
                "frequency_delta": freq_delta,
                "frequency": int(pattern.frequency),
                "last_episode": pattern.last_episode,
            }
    return effects


def _classify_failure_kind(raw_kind: Optional[str]) -> str:
    from pressure_loop.policy import (
        global_b_followup_clarification_kinds,
        global_b_followup_tool_kinds,
    )

    kind = str(raw_kind or "unknown").lower()
    if kind in global_b_followup_tool_kinds() or kind in {
        "tool_failed",
        "missing_tool",
        "runtime_unavailable",
        "memory_required_unavailable",
    }:
        return "tool"
    if kind in global_b_followup_clarification_kinds():
        return "planning"
    return "response"


def _entry_dict(entry: Any) -> dict:
    return dict(entry) if isinstance(entry, dict) else {}


def _entry_target(entry: Any, *, fallback: str = "") -> str:
    payload = _entry_dict(entry)
    execution = payload.get("execution")
    if isinstance(execution, dict):
        pack = execution.get("execution_action_pack")
        if isinstance(pack, dict):
            inputs = pack.get("execution_inputs")
            if isinstance(inputs, dict):
                query = str(inputs.get("query") or "").strip()
                if query:
                    return query
    outcome = payload.get("outcome")
    if isinstance(outcome, dict):
        goal = outcome.get("goal")
        if isinstance(goal, dict):
            raw = goal.get("target")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return fallback.strip()


def _entry_failure(entry: Any, *, fallback_target: str = "") -> tuple[bool, str, str]:
    payload = _entry_dict(entry)
    outcome = payload.get("outcome")
    inner = outcome.get("outcome") if isinstance(outcome, dict) else None
    if isinstance(inner, dict):
        goal_state = str(inner.get("goal_state") or inner.get("status") or "").lower()
        if goal_state in {"failed", "failure"}:
            failure_kind = inner.get("failure_kind")
            context = _entry_target(entry, fallback=fallback_target) or str(
                failure_kind or "unknown"
            )
            return (
                True,
                _classify_failure_kind(str(failure_kind) if failure_kind else None),
                context,
            )
    auto = payload.get("auto")
    if isinstance(auto, dict):
        if auto.get("status") == "failed" or auto.get("cycle_kind") == "failure":
            failure_kind = auto.get("failure_kind")
            context = _entry_target(entry, fallback=fallback_target) or str(
                failure_kind or "unknown"
            )
            return (
                True,
                _classify_failure_kind(str(failure_kind) if failure_kind else None),
                context,
            )
    meta = payload.get("meta")
    if isinstance(meta, dict):
        cascade_failure = meta.get("cascade_failure")
        omega_failure = meta.get("omega_fold_failure")
        has_cascade_failure = isinstance(cascade_failure, dict) and bool(
            str(cascade_failure.get("error_type") or "").strip()
            or str(cascade_failure.get("message") or "").strip()
        )
        has_omega_failure = isinstance(omega_failure, str) and bool(omega_failure.strip())
        if has_cascade_failure or has_omega_failure:
            context = _entry_target(entry, fallback=fallback_target) or "cognition"
            return True, _classify_failure_kind("cognition_phase_failed"), context
    # A cascade can fail after a successful task result. Meta B, Global B, and Omega
    # producers record failures with cognition_capture.capture_phase_failure; phase_trail
    # resolves those captured episode events even when the producer populated a carrier slot
    # before raising. DeepSleep consumes that trail evidence so AUTO's successful task result
    # cannot hide a later cognition failure.
    phase_trail = payload.get("phase_trail")
    if isinstance(phase_trail, list):
        for row in phase_trail:
            if not isinstance(row, dict) or str(row.get("status") or "").lower() != "failed":
                continue
            phase = str(row.get("phase") or "cognition").strip() or "cognition"
            context = _entry_target(entry, fallback=fallback_target) or phase
            return True, _classify_failure_kind("cognition_phase_failed"), context
    return False, "", ""


def _episode_failure(ds_input: DeepSleepInput) -> tuple[bool, str, str]:
    fallback_target = str(ds_input.action_goal.get("target") or "")
    window = ds_input.episode_window or []
    if window:
        for entry in window:
            is_failure, kind, context = _entry_failure(entry, fallback_target=fallback_target)
            if is_failure:
                return True, kind, context
        return False, "", ""

    if not window:
        return False, "", ""

    global_outcome = ds_input.global_outcome or {}
    outcome = global_outcome.get("outcome")
    if isinstance(outcome, dict):
        goal_state = str(outcome.get("goal_state") or "")
        if goal_state and goal_state != "completed":
            failure_kind = outcome.get("failure_kind") or global_outcome.get("failure_kind")
            context = str(fallback_target or failure_kind or "unknown")
            return (
                True,
                _classify_failure_kind(str(failure_kind) if failure_kind else None),
                context,
            )

    auto = ds_input.auto_outcome or {}
    if auto.get("status") == "failed" or auto.get("cycle_kind") == "failure":
        failure_kind = auto.get("failure_kind")
        context = str(fallback_target or failure_kind or "unknown")
        return True, _classify_failure_kind(str(failure_kind) if failure_kind else None), context

    return False, "", ""


def consolidate_failures(
    ds_input: DeepSleepInput,
    *,
    failure_path: Any = None,
) -> list[FailurePattern]:
    """Aggregate window episode failures into FailurePatterns.

    Fail-hard: failure-pattern consolidation writes the substrate. A malformed
    episode / store-write failure propagates and CRASHES — not swallowed to []."""
    from pressure_loop.policy import deepsleep_failure_path

    from deepsleep_loop.effect_receipts import effect_id, with_receipt

    resolved_path = failure_path or deepsleep_failure_path()

    def _counted(kind: str, context: str, episode_id: str, prior: FailurePattern | None):
        """One failure, counted once per episode: a receipt names it in the same upsert."""
        key = failure_key(kind, context)
        receipt_id = effect_id("failure", episode_id, key) if episode_id else ""
        if receipt_id and prior is not None and receipt_id in prior.effect_receipts:
            return prior, False  # already counted by an earlier attempt of this terminus
        before = int(prior.frequency) if prior else 0
        receipts = dict(prior.effect_receipts) if prior else {}
        if receipt_id:
            receipts = with_receipt(receipts, receipt_id, {
                "episode_id": episode_id,
                "frequency_before": before,
                "frequency_after": before + 1,
            })
        return FailurePattern(
            kind=kind,
            context=context,
            domain_id=canonical_domain_key(context),
            frequency=before + 1,
            last_episode=episode_id,
            effect_receipts=receipts,
        ), True
    # Phase 4 cut 13 review: ONE transaction. ``frequency`` is prior+1, read
    # from the map loaded here and written back by the upserts at the bottom;
    # lane-local deepsleep (cut 13) puts two lanes inside that span, and the
    # second one's count silently replaces the first's. The early ``return []``
    # for 'no patterns' stays inside the region — it writes nothing, and
    # leaving the region early on a no-op path is the same lock discipline.
    with store_transaction(FAILURE):
        store = load_failures(resolved_path)
        fallback_target = str(ds_input.action_goal.get("target") or "")
        patterns: list[FailurePattern] = []
        window = ds_input.episode_window or []

        changed: list[FailurePattern] = []
        if window:
            for entry in window:
                is_failure, kind, context = _entry_failure(entry, fallback_target=fallback_target)
                if not is_failure:
                    continue
                key = failure_key(kind, context)
                episode_id = str(_entry_dict(entry).get("episode_id") or ds_input.episode_id)
                pattern, applied = _counted(kind, context, episode_id, store.get(key))
                store[key] = pattern
                patterns.append(pattern)
                if applied:
                    changed.append(pattern)
        else:
            is_failure, kind, context = _episode_failure(ds_input)
            if is_failure:
                key = failure_key(kind, context)
                pattern, applied = _counted(kind, context, str(ds_input.episode_id),
                                            store.get(key))
                store[key] = pattern
                patterns.append(pattern)
                if applied:
                    changed.append(pattern)

        if not patterns:
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "deepsleep",
                    "failure_skip",
                    status="skip",
                    summary="no_failure_patterns",
                    reason="no_failure_patterns",
                )
            except Exception:
                pass
            return []

        for pattern in changed:
            upsert_failure(pattern, path=resolved_path)
    return patterns
