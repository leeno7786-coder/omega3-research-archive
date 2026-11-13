"""DeepSleep Phase 1 — episodic consolidation into JournalEntry."""

from __future__ import annotations

from typing import Any

from deepsleep_loop._deepsleep_state import DeepSleepInput, JournalEntry
from deepsleep_loop.journal_store import append_journal_entry, find_journal_entry


def _now_iso() -> str:
    from core.symphony_time import now_iso

    return now_iso()


def build_journal_entry(
    ds_input: DeepSleepInput,
    *,
    mastery_effects: dict | None = None,
    identity_effects: dict | None = None,
    semantic_effects: dict | None = None,
    trajectory_effects: dict | None = None,
    failure_effects: dict | None = None,
    query_effects: list | None = None,
    scope: str | None = None,
) -> JournalEntry:
    """Build a JournalEntry from DeepSleepInput."""
    # A per-episode input holds the producer envelopes in its one episode body.
    # The window properties summarize a cohort and cannot stand in for those envelopes.
    episode_scope = scope == "episode"
    if episode_scope:
        if len(ds_input.episode_window) != 1:
            raise ValueError("an episode journal requires exactly one episode body")
        body = ds_input.episode_window[0]
        global_outcome = dict(body.get("outcome") or {})
        omega_result = dict(body.get("omega_result") or {})
        trajectory_update = dict(body.get("trajectory_update") or {})
        reflection = {}
        omega_reasoning = {
            key: omega_result[key]
            for key in ("reflection", "synthesis", "control_decision", "episode_assessment",
                        "what_changed", "trajectory_influence")
            if omega_result.get(key) is not None
        }
    else:
        omega = ds_input.omega_outcome or {}
        reflection = dict(omega.get("reflection") or {})
        trajectory_update = dict(omega.get("trajectory_update") or {})
        global_outcome = ds_input.global_outcome or {}
        omega_result = {}
        omega_reasoning = dict(reflection.get("reasoning") or {})

    intent_raw = ds_input.action_goal.get("intent")
    intent = str(intent_raw) if isinstance(intent_raw, str) and intent_raw.strip() else None

    action_raw = ds_input.execution_pack.get("action")
    action = str(action_raw) if isinstance(action_raw, str) and action_raw.strip() else None

    outcome_block = global_outcome.get("outcome")
    outcome = dict(outcome_block) if isinstance(outcome_block, dict) else None

    trajectory_snapshot = trajectory_update or dict(reflection.get("trajectory") or {})

    observations = global_outcome.get("observations")
    artifacts = (
        dict(global_outcome.get("artifacts") or {})
        if isinstance(global_outcome.get("artifacts"), dict)
        else {}
    )

    from pressure_loop.policy import deepsleep_knowledge_consume_on_consolidate

    if deepsleep_knowledge_consume_on_consolidate():
        try:
            from knowledge_extractor.consume import knowledge_artifact_for_episode

            knowledge_row = knowledge_artifact_for_episode(ds_input.auto_outcome or {})
            if knowledge_row:
                artifacts = dict(artifacts)
                artifacts["knowledge"] = knowledge_row
        except Exception as exc:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "deepsleep",
                "consolidate",
                status="degraded",
                reason="knowledge_artifact_failed",
                ok=False,
                error=str(exc),
            )

    return JournalEntry(
        episode_id=ds_input.episode_id,
        timestamp=_now_iso(),
        intent=intent,
        action=action,
        outcome=outcome,
        trajectory_snapshot=dict(trajectory_snapshot),
        omega_reasoning=omega_reasoning,
        observations=dict(observations) if isinstance(observations, dict) else {},
        artifacts=artifacts,
        mastery_effects=dict(mastery_effects or {}),
        identity_effects=dict(identity_effects or {}),
        semantic_effects=dict(semantic_effects or {}),
        trajectory_effects=dict(trajectory_effects or {}),
        failure_effects=dict(failure_effects or {}),
        query_effects=list(query_effects or []),
        scope=scope,
        global_outcome=dict(global_outcome),
        omega_result=omega_result,
    )


def consolidate(
    ds_input: DeepSleepInput,
    *,
    journal_path: Any = None,
    mastery_effects: dict | None = None,
    identity_effects: dict | None = None,
    semantic_effects: dict | None = None,
    trajectory_effects: dict | None = None,
    failure_effects: dict | None = None,
    query_effects: list | None = None,
    persist: bool = True,
    scope: str | None = None,
) -> dict:
    """Phase 1: build (and optionally persist) JournalEntry.

    WO-07: one terminus writes one row. A retried terminus finds the row its earlier attempt
    appended and returns THAT entry — same timestamp, same effects — so neither the journal nor
    the canonical journal memory built from it gains a second copy.
    """
    if persist:
        existing = find_journal_entry(ds_input.episode_id, scope, path=journal_path)
        if existing is not None:
            return existing.to_dict()
    entry = build_journal_entry(
        ds_input,
        mastery_effects=mastery_effects,
        identity_effects=identity_effects,
        semantic_effects=semantic_effects,
        trajectory_effects=trajectory_effects,
        failure_effects=failure_effects,
        query_effects=query_effects,
        scope=scope,
    )
    if persist:
        append_journal_entry(entry, path=journal_path)
    return entry.to_dict()
