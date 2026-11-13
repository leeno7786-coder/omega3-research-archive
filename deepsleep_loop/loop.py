"""Pure sequencer for DeepSleep phase chain (Slice 6a + 6b + 6c)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from guardian.terminal_feedback import emit_data, phase_span

from deepsleep_loop import consolidate as consolidate_phase
from deepsleep_loop import failure as failure_phase
from deepsleep_loop import identity as identity_phase
from deepsleep_loop import intake as intake_phase
from deepsleep_loop import mastery as mastery_phase
from deepsleep_loop import projection as projection_phase
from deepsleep_loop import query_lifecycle as query_lifecycle_phase
from deepsleep_loop import semantic as semantic_phase
from deepsleep_loop import trajectory as trajectory_phase
from deepsleep_loop._deepsleep_state import (
    DeepSleepInput,
    DeepSleepOutcomePack,
)
from deepsleep_loop.failure.run import episode_prior_failures, failure_effects_from_patterns
from deepsleep_loop.failure_store import load_failures
from deepsleep_loop.identity.run import identity_effects_from_traits
from deepsleep_loop.identity_store import load_identity
from deepsleep_loop.mastery_store import load_mastery
from deepsleep_loop.mastery.run import episode_prior_mastery, mastery_effects_from_records
from deepsleep_loop.query_lifecycle.run import query_effects_from_queries
from deepsleep_loop.query_store import load_queries
from deepsleep_loop.semantic.run import semantic_effects_from_entries
from deepsleep_loop.trajectory.run import trajectory_effects_from_records
from deepsleep_loop.trajectory_store import load_trajectory

log = logging.getLogger("omega.deepsleep_loop")

# ── the window fold's scope (operator ruling 2026-08-11, risk 7) ─────────────
# Phase 4 cut 10 wired the FULL twelve-phase terminus at window scope. Every one
# of these five phases re-derives from evidence the per-episode termini ALREADY
# CONSUMED — the ten episodes in a window each consolidated at their own
# terminus — so a domain touched in five of them was credited five times and then
# a sixth. That contradicts the operator's 2026-08-03 per-episode-only ruling, so
# a window fold does not run them. It commits only what the WINDOW alone can
# know: the trajectory verdict across ten episodes, its own synthesis memory, the
# window journal row, and the pressure it consumes by closing.
WINDOW_SKIPPED_PHASES = ("mastery", "failure", "semantic", "identity", "query_lifecycle")

# Telemetry must be HONEST about the skip — the operator's word. A phase that
# does not run says so with this status; it is never silent.
ALREADY_CONSOLIDATED = "skipped:already_consolidated_per_episode"
_PUBLICATION_AUTHORITY_WAIT_S = 120.0
_PROJECTION_SNAPSHOT_SETTLE_WAIT_S = 10.0
_PROJECTION_RETRY_POLL_S = 0.025


def _effect_count(value: Any) -> int:
    if isinstance(value, dict):
        total = 0
        for item in value.values():
            if isinstance(item, (list, tuple, set, dict)):
                total += len(item)
            elif item:
                total += 1
        return total
    if isinstance(value, (list, tuple, set)):
        return len(value)
    return 1 if value else 0


def _maintain_projection_inputs_after_deepsleep(
    *,
    context_id: str,
    scope: str,
    debounce_s: float = 0.25,
    before_projection: Any = None,
) -> dict[str, int]:
    """Finish canonical-side maintenance before Program 0 captures its snapshot."""
    del debounce_s  # retained in the call contract while delayed scheduling is removed
    from core import paths as core_paths
    from core.required_substrate import RequiredSubstrateError

    active_root = Path(core_paths.MEMORY_ROOT).parent
    repository_root = Path(core_paths.OMEGA_ROOT)
    root_override = None if active_root.resolve() == repository_root.resolve() else active_root
    if before_projection is not None:
        before_projection(root_override)
    from memory.source_validity import rebuild_from_canonical

    try:
        source_validity_counts = rebuild_from_canonical(root_override)
    except Exception as exc:  # noqa: BLE001 — source validity is required maintenance
        failure = RequiredSubstrateError.from_exception(
            "memory_source_validity",
            "rebuild",
            exc,
        )
        if failure is exc:
            raise
        raise failure from exc
    try:
        emit_data(
            "deepsleep",
            "projection_inputs_ready",
            episode_id=context_id,
            summary=f"canonical projection inputs ready after {scope} DeepSleep",
            scope=scope,
        )
    except Exception:
        pass
    try:
        emit_data(
            "deepsleep",
            "source_validity_rebuilt",
            episode_id=context_id,
            summary=(
                f"objects={source_validity_counts.get('objects', 0)} "
                f"revisions={source_validity_counts.get('revisions', 0)}"
            ),
            scope=scope,
            **source_validity_counts,
        )
    except Exception:
        pass
    return source_validity_counts


def _publish_projection_after_deepsleep(*, context_id: str, scope: str):
    """Publish the closed post-DeepSleep snapshot through Program 0 outside the commit lock."""
    import time

    from core import paths as core_paths
    from core.required_substrate import RequiredSubstrateError
    from memory.projection.dispatch import projection_dispatch
    from memory.projection.generation import PublicationAuthorityBusy

    active_root = Path(core_paths.MEMORY_ROOT).parent
    repository_root = Path(core_paths.OMEGA_ROOT)
    root_override = None if active_root.resolve() == repository_root.resolve() else active_root
    try:
        authority_deadline = None
        snapshot_deadline = None
        while True:
            attempt_started = time.monotonic()
            try:
                dispatch = projection_dispatch(root_override, reason="deepsleep")
            except PublicationAuthorityBusy as exc:
                # Another live lane owns the one shared memory publisher. This is ordinary
                # concurrency, not a failed publication; wait for its OS-backed lease to
                # release, but a stuck publisher must become an explicit substrate failure.
                # Time waiting for that authority does not consume the separate snapshot-
                # settle budget.
                now = time.monotonic()
                if authority_deadline is None:
                    authority_deadline = now + _PUBLICATION_AUTHORITY_WAIT_S
                if snapshot_deadline is not None:
                    snapshot_deadline += max(0.0, now - attempt_started)
                authority_remaining = authority_deadline - now
                if authority_remaining <= 0:
                    raise TimeoutError(
                        "publication authority wait timed out after "
                        f"{_PUBLICATION_AUTHORITY_WAIT_S:.1f}s"
                    ) from exc
                delay = min(_PROJECTION_RETRY_POLL_S, authority_remaining)
                sleep_started = time.monotonic()
                time.sleep(delay)
                if snapshot_deadline is not None:
                    snapshot_deadline += max(0.0, time.monotonic() - sleep_started)
                continue
            authority_deadline = None
            retryable_statuses = {
                "blocked_on_obligations",
                "snapshot_moved",
                "source_ledger_moved",
                "registry_moved",
            }
            if dispatch.drain_status not in retryable_statuses:
                break
            now = time.monotonic()
            if snapshot_deadline is None:
                snapshot_deadline = now + _PROJECTION_SNAPSHOT_SETTLE_WAIT_S
            remaining = snapshot_deadline - now
            if remaining <= 0:
                break
            # Another pipeline may finish a canonical write or one of the captured inputs may
            # move while Program 0 builds. It rejects that mixed snapshot; wait briefly and let
            # its next attempt capture the settled state. Persistent movement or obligations
            # remain a hard failure when the bounded wait expires.
            time.sleep(min(_PROJECTION_RETRY_POLL_S, remaining))
    except Exception as exc:  # noqa: BLE001 — projection publication is required terminus work
        failure = RequiredSubstrateError.from_exception(
            "memory_projection",
            "dispatch_execution",
            exc,
        )
        if failure is exc:
            raise
        raise failure from exc
    if dispatch.action == "failed":
        dispatch_detail = dict(dispatch.detail or {})
        raise RequiredSubstrateError(
            "memory_projection",
            "dispatch_execution",
            reason=str(dispatch_detail.get("reason") or dispatch.drain_status or "failed"),
            detail=str(dispatch_detail),
        )
    try:
        emit_data(
            "deepsleep",
            "projection_dispatched",
            episode_id=context_id,
            scope=scope,
            owner=dispatch.owner,
            action=dispatch.action,
            generation_id=dispatch.generation_id or "",
        )
    except Exception:
        pass
    return dispatch


def _reconcile_structural_associations(root: Path | str | None) -> dict[str, int]:
    """Reconcile compiler edges while DeepSleep owns adaptive mutation."""
    from memory.association_strength import (
        AssociationStrengthIndex,
        deep_sleep_mutation,
        structural_updates_from_edges,
    )
    from memory.taxonomy.compiler import compile_structural_edges

    updates = structural_updates_from_edges(compile_structural_edges(root))
    index = AssociationStrengthIndex()
    with deep_sleep_mutation():
        return index.reconcile_structural(updates)


def _consolidate_associations(closing_episode: Any, root: Path | str | None) -> dict[str, Any]:
    """Apply verified-use credit and reconcile structural edges before projection."""
    from memory.association_strength import (
        apply_deepsleep_updates,
        updates_from_verified_episode,
    )

    result: dict[str, Any] = {
        "registered": 0,
        "reconciled": 0,
        "credited": 0,
        "decayed": 0,
        "retired": 0,
        "skipped": [],
    }
    episode_meta = dict(getattr(closing_episode, "meta", None) or {})
    raw_updates = episode_meta.get("association_updates")
    if not raw_updates:
        raw_updates = updates_from_verified_episode(closing_episode)
    if raw_updates:
        result = apply_deepsleep_updates(raw_updates)
    structural = _reconcile_structural_associations(root)
    result["reconciled"] = int(result.get("reconciled") or 0) + structural["reconciled"]
    result["retired"] = int(result.get("retired") or 0) + structural["retired"]
    return result


def _write_hud_deepsleep_cycle(
    *,
    context_id: str,
    journal_entry: dict,
    journal_scope: str,
    mastery_effects: dict | None = None,
    identity_effects: dict | None = None,
    semantic_effects: dict | None = None,
    trajectory_effects: dict | None = None,
    failure_effects: dict | None = None,
    query_effects: list | None = None,
    mem_result: dict | None = None,
    pressure_seed: list | None = None,
    memory_integrity_projection: dict | None = None,
) -> None:
    """Publish the current DeepSleep cycle to the HUD's live ring."""
    try:
        from core.shared_state_keys import RECENT_DEEPSLEEP_CYCLES
        from pressure_loop.runtime import get_default_runtime

        ss = get_default_runtime().shared_state
        ring = ss.read(RECENT_DEEPSLEEP_CYCLES)
        rows = list(ring) if isinstance(ring, list) else []
        observations = journal_entry.get("observations")
        observations = dict(observations) if isinstance(observations, dict) else {}
        written = list((mem_result or {}).get("written") or [])
        failed = list((mem_result or {}).get("failed") or [])
        promoted = (
            _effect_count(mastery_effects or {})
            + _effect_count(identity_effects or {})
            + _effect_count(semantic_effects or {})
            + _effect_count(trajectory_effects or {})
            + len(query_effects or [])
        )
        failures = _effect_count(failure_effects or {})
        integrity = dict(memory_integrity_projection or {})
        integrity_issues = int(integrity.get("issue_count") or 0)
        record = {
            "cycle_id": context_id,
            "timestamp": journal_entry.get("timestamp") or "",
            "audit_id": context_id,
            "fragments_consolidated": max(
                1 if journal_entry else 0,
                len(journal_entry.get("artifacts") or {}),
                len(observations),
            ),
            "tools_added": 0,
            "global_insights": len(query_effects or []),
            "replay_observations": len(observations),
            "replay_history_kept": len(journal_entry.get("artifacts") or {}),
            "schema_candidates": _effect_count(semantic_effects or {}),
            "contradictions_detected": failures + integrity_issues,
            "contradictions_resolved": failures,
            "forgetting_candidates": failures,
            "forgetting_applied": 0,
            "memory_integrity": integrity,
            "consolidation": {
                "promoted": promoted + len(written),
                "reinforced": _effect_count(mastery_effects or {}),
                "skipped": len(failed),
            },
            "pruning": {"pruned": 0, "isolated": len(failed), "archived": 0},
            "pattern_type": journal_scope,
            "engine_sleep_cycle_id": context_id,
            "guardian_review_required": bool(failed),
            "source_cycles": [context_id],
            "pressure_seed_count": len(pressure_seed or []),
        }
        rows.append(record)
        ss.write(RECENT_DEEPSLEEP_CYCLES, rows[-5:])
    except Exception:
        pass


def _run_memory_integrity_housekeeping(*, context_id: str, scope: str) -> dict:
    """Run required DeepSleep memory repair and return its projection."""
    try:
        from memory.integrity.repair import repair_integrity_issues

        repair = repair_integrity_issues(if_due=True)
        repair_payload = repair.to_dict()
        report = dict(repair_payload.get("after") or {})
        projection = {
            "type": "memory_integrity_projection",
            "status": str(report.get("status") or "unknown"),
            "issue_count": len(report.get("issues") or []),
            "issues": list(report.get("issues") or []),
            "missing_intake_packets": list(report.get("missing_intake_packets") or []),
            "missing_source_artifacts": int(report.get("missing_source_artifacts") or 0),
            "source_hash_mismatches": int(report.get("source_hash_mismatches") or 0),
            "orphan_graph_leaves": list(report.get("orphan_graph_leaves") or []),
            "lobe_route_mismatches": list(report.get("lobe_route_mismatches") or []),
            "orphan_sense_rows": list(report.get("orphan_sense_rows") or []),
            "dme_parity_status": str(report.get("dme_parity_status") or "unknown"),
            "dme_missing_in_sram": list(report.get("dme_missing_in_sram") or []),
            "dme_orphan_in_sram": list(report.get("dme_orphan_in_sram") or []),
            "audited_at": str(report.get("audited_at") or ""),
            "repair": repair_payload,
            "repaired_count": int(repair_payload.get("repaired_count") or 0),
            "scope": scope,
        }
        try:
            emit_data(
                "deepsleep",
                "memory_integrity_housekeeping",
                episode_id=context_id,
                status=projection["status"],
                summary=(
                    f"status={projection['status']} issues={len(projection['issues'])} "
                    f"repaired={int(repair_payload.get('repaired_count') or 0)}"
                ),
                issue_count=len(projection["issues"]),
                repaired_count=int(repair_payload.get("repaired_count") or 0),
                issues=list(projection["issues"]),
                scope=scope,
            )
        except Exception:
            pass
        return projection
    except Exception as exc:
        from core.required_substrate import RequiredSubstrateError

        failure = RequiredSubstrateError.from_exception(
            "memory_integrity",
            "repair",
            exc,
        )
        if failure is exc:
            raise
        raise failure from exc


def _run_conflict_reconciliation(*, context_id: str, scope: str) -> dict:
    """DeepSleep memory housekeeping: reconcile queued contradiction candidates.

    Drains the deepsleep candidates queue for ``contradiction:`` entries and adjudicates each,
    weighing by lobe-wide corroboration and tombstoning decisively out-corroborated non-canonical
    losers (near-ties demote; canonical is immune). This ACTIVATES the previously-dormant
    reconciliation chain. It is required maintenance: a failure propagates and
    leaves the episode carrier open for retry. The frozen verifier is untouched;
    it only quarantines, this resolves."""
    from memory.contradiction_resolution import process_contradiction_candidates

    report = process_contradiction_candidates()
    try:
        emit_data(
            "deepsleep",
            "conflict_reconciliation",
            episode_id=context_id,
            status="ok",
            summary=(
                f"groups={report.get('groups_processed', 0)} "
                f"tombstoned={report.get('tombstoned_count', 0)} "
                f"demoted={report.get('demoted_count', 0)}"
            ),
            scope=scope,
            groups_processed=int(report.get("groups_processed", 0) or 0),
            tombstoned_count=int(report.get("tombstoned_count", 0) or 0),
            demoted_count=int(report.get("demoted_count", 0) or 0),
            queued_entries=int(report.get("queued_entries", 0) or 0),
        )
    except Exception:
        pass
    return report


def _flush_episode_sram(context_id: str) -> dict:
    """Flush THIS episode's SRAM bucket into system memory — the operator's
    "add them to the system memory all at once during deepsleep" (2026-07-16).

    The bucket (memory.episode_sram) held every fresh intake of the turn — the operator's
    stated facts + conversation turns — OUT of system memory for the whole episode, so this
    turn's recall could never surface this turn's own intake. Here, at the consolidation
    seam, it all lands at once. Per-item outcomes are reported by the consolidator; a
    failure of the flush itself propagates so DeepSleep cannot close an unpersisted episode.
    The drained bucket remains the live owner through the remaining required
    phases, so their failures still enter the same episode's memory lane."""
    from memory import episode_sram

    result = episode_sram.flush(context_id)
    if episode_sram.pending(context_id):
        raise RuntimeError(f"episode SRAM bucket {context_id} still has pending items after drain")
    try:
        # EVERY key of the flush's own contract, derived from the contract itself rather than
        # listed here. Measured 2026-09-22: the emission carried ingested/turns/errors/skipped
        # while the contract also carries recalls/memories/events, so a flush that consolidated
        # memories reported `ingested=0 turns=0 errors=0 skipped=0`. Across 50 live flushes
        # totalling 325s that reported 14 records between them — one spent 17.9s and reported
        # nothing — which made real work indistinguishable from an idle pass at the one seam that
        # exists to make the memory lane auditable (§2.1, §5.0). Row #15 added `skipped` and left
        # three keys behind; deriving the set means the next key cannot be forgotten either.
        reported = {
            key: list(value or [])
            for key, value in result.items()
            if key != "enabled" and isinstance(value, (list, tuple))
        }
        emit_data(
            "deepsleep",
            "episode_sram_flush",
            episode_id=context_id,
            summary=" ".join(f"{key}={len(value)}" for key, value in sorted(reported.items())),
            **reported,
        )
    except Exception:
        pass
    return result


def _process_workspace_retention(context_id: str) -> dict:
    """Apply metadata-only Workspace retention commands after their tombstones land."""
    from memory.workspace_retention import process_retention_commands

    result = process_retention_commands()
    try:
        emit_data(
            "deepsleep",
            "workspace_retention",
            episode_id=context_id,
            summary=(
                f"commands={int(result.get('commands') or 0)} "
                f"purged={int(result.get('purged') or 0)} "
                f"retained={int(result.get('retained') or 0)}"
            ),
            **result,
        )
    except Exception:
        pass
    return result


def _ground_practice_report_from_sram(context_id: str) -> float | None:
    """Ground an accepted practice report during DeepSleep, exactly once."""
    from dataclasses import replace

    from memory.execution_report import current_execution_report, hold_execution_report

    report = current_execution_report()
    if report is None:
        return None
    metadata = dict(report.metadata or {})
    if not bool(metadata.get("practice_grounding_pending")):
        return None
    if report.episode_id != context_id:
        raise RuntimeError(
            "practice report episode does not match the DeepSleep context: "
            f"{report.episode_id!r} != {context_id!r}"
        )
    domain = str(metadata.get("practice_domain") or "").strip()
    if not domain:
        raise RuntimeError("accepted practice report has no practice_domain")

    from pressure_loop.practice_mint import apply_practice_success

    grounded = apply_practice_success(domain, episode_id=context_id)
    metadata["practice_grounding_pending"] = False
    metadata["practice_grounded"] = True
    hold_execution_report(replace(report, metadata=metadata))
    return grounded


def _capture_closed_episode_to_taxonomy(context_id: str, episode_body: dict | None) -> dict:
    """Project one closed episode into canonical memory inside DeepSleep.

    Projection is required so the subsequent integrity pass and graph rebuild
    see every write from this episode. Failures block episode finalization.
    """
    view = dict(episode_body or {})
    view["episode_id"] = str(view.get("episode_id") or context_id)
    from memory.taxonomy.canonical_capture import capture_episode_to_canonical

    return dict(capture_episode_to_canonical(view) or {})


def _canonical_episode_view(episode: Any) -> dict[str, Any]:
    """The slice of a closing episode that canonical memory keeps.

    Phase 4 cut 13 lifted this to module level and made it callable from INSIDE
    the commit region. Both termini built the identical dict, and both built it
    before the stamp existed; ``timekeeper`` is in it, so building it early now
    means capturing an episode into canonical memory with no record of when it
    crossed the finish line. It is read once, after the stamp.
    """
    return {
        "episode_id": str(getattr(episode, "episode_id", "") or ""),
        "closed_at": getattr(episode, "closed_at", "") or "",
        "created_at": getattr(episode, "opened_at", "") or "",
        "pressure": getattr(episode, "pressure", None) or {},
        "global": getattr(episode, "global_", None) or {},
        "meta": getattr(episode, "meta", None) or {},
        "auto": getattr(episode, "auto", None) or {},
        "omega": getattr(episode, "omega", None) or {},
        "lane": getattr(episode, "lane", "") or "",
        "timekeeper": dict(getattr(episode, "timekeeper", None) or {}),
    }


# ── WO-07: the durable terminal protocol ─────────────────────────────────────
# One episode, one sealed record, one place in the memory sequence — whatever a retry finds.
# Every step below is an EXISTING authority; nothing here is a new store or a second clock.
#
#   stable effect ids   deepsleep_loop.effect_receipts.effect_id(kind, episode, target)
#   recorded intent     the sealed record's intake-journal row while it is ``pending`` — never
#                       a committed effect (memory.episode_commit_view reports it as pending)
#   required write      the drain commits it under the episode's one deterministic record id
#   canonical receipt   that row reaching ``committed``, bound to the id, plus the file itself
#   effect receipts     embedded in each store record, in the same atomic update as the effect
#   projection coverage legacy: the in-region reindex; generation owners: publication after the
#                       region, and no window cohort is released until its members are members
#   eligibility marker  the window append (after publication) and the journal row at close
#   ack / cleanup       the episode's own events, published by value in the record, leave
#                       ``pending`` — only after every required step above has held


def _sealed_record_rows(context_id: str) -> list:
    """Every intake-journal row that ever carried THIS episode's sealed record."""
    from memory import intake_journal
    from memory.episode_record_seal import episode_record_id

    record_id = episode_record_id(context_id)
    return [
        row
        for row in intake_journal.records()
        if row.kind == "memory" and str((row.payload or {}).get("id") or "") == record_id
    ]


def _row_sealed_sha(row: Any) -> str:
    """The content hash of the sealed record a journal row carries, recomputed from its bytes."""
    import json

    from schema.continuity_json import content_hash

    text = ((row.payload or {}).get("payload") or {}).get("text")
    body = json.loads(text) if isinstance(text, str) else {}
    record = body.get("episode_record") if isinstance(body, dict) else None
    return content_hash(record) if isinstance(record, dict) else ""


def _row_body(row: Any) -> dict:
    """The canonical body a sealed-record intake row carries (what the drain writes)."""
    import json

    text = ((row.payload or {}).get("payload") or {}).get("text")
    body = json.loads(text) if isinstance(text, str) else None
    if not isinstance(body, dict) or not isinstance(body.get("episode_record"), dict):
        raise RuntimeError(f"intake row {row.record_id} carries no sealed episode record")
    return body


def _canonical_sealed_body(record_id: str) -> tuple[dict | None, str]:
    """The canonical body stored under *record_id* and its path, or ``(None, "")``.

    The canonical filesystem is truth. A record can be committed with no intake row — the drain
    writes through directly whenever intake happens inside it — so a terminus that only asked
    the journal would not see it.
    """
    import json

    from memory.engine.walker import load_coord_index, path_for_id

    load_coord_index()
    where = path_for_id(record_id)
    if not where or not Path(where).is_file():
        return None, ""
    document = json.loads(Path(where).read_text(encoding="utf-8"))
    if str(document.get("id") or "") != record_id:
        return None, ""
    body = json.loads(document.get("text") or "{}")
    if not isinstance(body, dict) or not isinstance(body.get("episode_record"), dict):
        return None, ""
    return body, str(where)


def _file_sealed_sha(path: str) -> str:
    """The content hash of the sealed record in the canonical file at *path* ("" if none)."""
    import json

    from schema.continuity_json import content_hash

    document = json.loads(Path(path).read_text(encoding="utf-8"))
    body = json.loads(document.get("text") or "{}")
    record = body.get("episode_record") if isinstance(body, dict) else None
    return content_hash(record) if isinstance(record, dict) else ""


def _resume_sealed_record(context_id: str) -> dict | None:
    """Reconcile what an EARLIER attempt of this terminus left behind; return its committed body.

    * a committed row whose canonical record exists (the same verifier the intake replay uses
      before reusing a commit receipt), or a pending row whose exact sealed content is already
      on disk (its acknowledgement was lost), means the episode already holds its place: the
      retry REPLAYS it — no second seal, no second write, no new ``commit_seq``;
    * a pending row whose content never reached disk is SUPERSEDED — marked with its burned
      stamp and dropped from SRAM — so it can never land later under a number the timekeeper
      already spent;
    * rejected and superseded rows stay in the journal, visible, and change nothing here.
    """
    from core.required_substrate import RequiredSubstrateError
    from memory import episode_sram, intake_journal
    from memory.api import _canonical_binding_exists

    committed: dict | None = None
    for row in _sealed_record_rows(context_id):
        if row.status == intake_journal.COMMITTED:
            if not row.memory_id or not _canonical_binding_exists(row):
                raise RequiredSubstrateError(
                    "deepsleep",
                    "resume_sealed_record",
                    reason=f"sealed record row {row.record_id} is committed but its canonical "
                    f"record {row.memory_id or '(no id)'} is absent",
                )
            committed = _row_body(row)
            continue
        if row.status not in (intake_journal.PENDING, intake_journal.APPLYING):
            continue
        verdict, memory_id, where = intake_journal.verify_canonical_binding(row)
        if verdict == "ok" and _file_sealed_sha(where) == _row_sealed_sha(row):
            intake_journal.mark_committed(
                row.record_id, memory_id=memory_id, canonical_path=where,
                note="reconciled at terminus retry from the canonical record on disk",
            )
            committed = _row_body(row)
            continue
        if verdict not in ("ok", "missing"):
            raise RequiredSubstrateError(
                "deepsleep",
                "resume_sealed_record",
                reason=f"sealed record row {row.record_id} cannot be verified: {verdict}",
                detail=str(where or ""),
            )
        # Never landed — or the file under the id holds a DIFFERENT seal, so this row's content
        # never did. Either way it is superseded, visibly, with the stamp it would have claimed.
        stamp = dict(_row_body(row).get("timekeeper") or {})
        intake_journal.mark_consumed(
            row.record_id,
            reason=(
                "superseded by a terminus retry: this seal carried commit_seq "
                f"{stamp.get('commit_seq', 'unknown')}, which was burned when its attempt failed "
                "before the record reached disk, and is never reused"
            ),
        )
        episode_sram.remove_journal_record(row.record_id)
    if committed is None:
        from memory.episode_record_seal import episode_record_id

        committed, _where = _canonical_sealed_body(episode_record_id(context_id))
    return committed


def _bind_sealed_record(context_id: str, record: Any, *, capture_receipt: dict | None = None):
    """The canonical receipt: this exact sealed content committed under the episode's record id.

    The receipt is the intake row reaching ``committed`` — or, when the capture itself committed
    (intake inside a drain writes through), the capture's own receipt, verified against the file
    it names. Anything else blocks finalization — a rejected required record, a record still
    pending after the drain, or no receipt at all — and says which (AGENTS.md section 2.1).
    """
    from core.required_substrate import RequiredSubstrateError
    from memory import intake_journal
    from schema.continuity_json import CanonicalRef

    want = record.content_sha256()
    rows = [row for row in _sealed_record_rows(context_id) if _row_sealed_sha(row) == want]
    committed = [row for row in rows if row.status == intake_journal.COMMITTED]
    if committed:
        row = committed[-1]
        return CanonicalRef(record_id=str(row.memory_id), record_version=1, content_sha256=want)
    receipt = dict(capture_receipt or {})
    if not rows and receipt.get("validation_status") == "committed" and receipt.get("path"):
        from schema.continuity_json import content_hash

        body, _where = _canonical_sealed_body(str(receipt["path"]))
        if body is not None and content_hash(body["episode_record"]) == want:
            return CanonicalRef(record_id=str(receipt["path"]), record_version=1,
                                content_sha256=want)
    status = rows[-1].status if rows else "absent"
    note = rows[-1].note if rows else "no intake row carries this sealed record"
    word = "rejected" if status == intake_journal.REJECTED else status
    raise RequiredSubstrateError(
        "deepsleep",
        "episode_record_commit",
        reason=f"the sealed episode record of {context_id} was {word} by the drain",
        detail=str(note or ""),
    )


def _acknowledge_published_events(
    record: Any, ref: Any, *, supplements: tuple = (), owner_episode_id: str,
) -> int:
    """Acknowledge events proven published in the seal or its exact, immutable supplements."""
    from memory import intake_journal
    from memory.episode_events import EVENT_JOURNAL_KIND, read_episode_events

    published = {event.event_id: event for event in (*record.events["by_value"], *supplements)}
    for event in read_episode_events(record.episode_id):
        if published.get(event.event_id) != event:
            raise RuntimeError(f"episode event {event.event_id} is still awaiting publication")
    acknowledged = 0
    for row in intake_journal.records():
        if (row.kind != EVENT_JOURNAL_KIND or row.event_id not in published
                or row.owner_episode_id != owner_episode_id):
            continue
        if row.status != intake_journal.PENDING:
            continue
        intake_journal.mark_consumed(
            row.record_id,
            reason=f"published by value in episode record or its supplement {ref.record_id} "
            f"({ref.content_sha256})",
        )
        acknowledged += 1
    return acknowledged


def _terminus_effects(
    context_id: str,
    *,
    mastery_records: list,
    failure_patterns: list,
    pathway_effects: list,
    journal_entry: dict,
    scope: str,
) -> tuple[dict, ...]:
    """This terminus's applied per-episode effects, each with the receipt its store carries."""
    from deepsleep_loop.effect_receipts import effect_id, receipts_for

    effects: list[dict] = []
    for record in mastery_records:
        for key, receipt in receipts_for(getattr(record, "effect_receipts", None),
                                         context_id).items():
            effects.append({"effect_id": key, "kind": "mastery", "target": record.domain_id,
                            "receipt": receipt})
    for pattern in failure_patterns:
        for key, receipt in receipts_for(getattr(pattern, "effect_receipts", None),
                                         context_id).items():
            effects.append({"effect_id": key, "kind": "failure", "target": pattern.domain_id,
                            "receipt": receipt})
    effects.extend(dict(row) for row in pathway_effects)
    if journal_entry:
        effects.append({
            "effect_id": effect_id("journal", context_id, str(scope)),
            "kind": "journal",
            "target": str(scope),
            "receipt": {"episode_id": context_id, "timestamp": str(journal_entry.get("timestamp") or "")},
        })
    return tuple(effects)


def run_deepsleep_for_episode(
    store: Any,
    *,
    cascade_status: str = "completed",
) -> Optional[DeepSleepOutcomePack]:
    """Run the complete DeepSleep transaction and finalize the active episode.

    Phase 4 cut 13 removed the finish-line stamp from THIS function. It used to
    be the gate's first statement, and the stamp equalled the landed order for
    exactly one reason: the whole terminus ran under one process-wide lock, so
    nothing could arrive between the stamp and the writes. With the lane-local
    half now unserialized, a lane can stamp first and land last. The stamp is
    the first statement inside the commit lock instead — see
    ``core.commit_region.serialized_commit``, entered by
    ``_run_deepsleep_episode_core`` below. The gate is still the ONE place every
    episode passes; it just no longer decides the order at the door.
    """
    active_episode = store.current()
    if active_episode is None:
        raise RuntimeError("DeepSleep requires an active episode carrier")

    pack: Optional[DeepSleepOutcomePack] = None
    commit_ran = False

    def _commit(closing_episode: Any, episode_body: dict) -> None:
        nonlocal commit_ran, pack
        if closing_episode is not active_episode:
            raise RuntimeError("DeepSleep lost ownership of the active episode")
        episode_id = str(closing_episode.episode_id)
        # Recovery can rebind an existing carrier's SRAM ownership. Normal retries
        # retain their empty live bucket until the whole terminus succeeds.
        from memory import episode_sram

        if not episode_sram.is_open(episode_id):
            episode_sram.open_bucket(
                episode_id,
                pipeline_id=closing_episode.pipeline_id,
            )
        episode_temporal = dict(episode_body.get("temporal") or {})
        episode_learning_state = dict(episode_body.get("learning_state") or {})
        pack = _run_deepsleep_episode_core(
            store,
            episode_id,
            lambda: intake_phase.build_deepsleep_input_for_episode(
                episode_body, episode_id
            ),
            closing_episode=closing_episode,
            episode_body=episode_body,
            episode_temporal=episode_temporal,
            episode_learning_state=episode_learning_state,
        )
        commit_ran = True

    try:
        closed_episode = store.close(
            cascade_status=cascade_status,
            before_finalize=_commit,
            after_archive=_acknowledge_bound_mail,
        )
    except Exception as producer_error:
        _record_deepsleep_failure(active_episode, producer_error)
        raise
    _hand_off_orphaned_mail()
    if closed_episode is not active_episode or not commit_ran:
        raise RuntimeError("DeepSleep did not finalize its active episode")
    return pack


def _record_deepsleep_failure(episode: Any, producer_error: Exception) -> None:
    """Both termini retain failure evidence in the episode that owns their live SRAM."""
    from memory.cognition_capture import (
        DEEPSLEEP_FAILED,
        capture_phase_failure,
        new_invocation_id,
    )

    reason = f"DeepSleep raised {type(producer_error).__name__}: {producer_error}"[:1000]
    try:
        failed_entry = {
            "episode_id": str(episode.episode_id),
            "execution": {
                "execution_action_pack": dict(
                    dict(getattr(episode, "meta", None) or {}).get("execution_action_pack") or {}
                )
            },
            "phase_trail": [{
                "phase": "deepsleep", "status": "failed", "cause": reason, "evidence": [],
            }],
        }
        failure_phase.consolidate_failures(
            DeepSleepInput(
                window_id=str(episode.episode_id), window_reflection={},
                episode_window=[failed_entry], trajectory_snapshot={},
            )
        )
    except Exception as learning_error:
        producer_error.add_note(
            f"DeepSleep failure-pattern recording also failed: {learning_error!r}"
        )
        log.exception("could not learn from the failed DeepSleep phase")
    try:
        capture_phase_failure(
            episode, phase="deepsleep", event_kind=DEEPSLEEP_FAILED,
            reason=reason, invocation_id=new_invocation_id(),
        )
    except Exception as capture_error:
        producer_error.add_note(f"DeepSleep failure capture also failed: {capture_error!r}")
        log.exception("could not record the failed DeepSleep phase")
        if capture_error.__context__ is producer_error:
            capture_error.__context__ = producer_error.__context__
        raise producer_error


def _acknowledge_bound_mail(episode) -> None:
    """``after_archive`` seam: acknowledge the cross-lane mail this episode's Omega bound.

    Runs only once the episode's journal row is durably archived. Acknowledgement is
    failure-soft inside ``acknowledge_omega_mail``; the seam is failure-soft again around
    it, so mail bookkeeping can never fail a close whose memory already committed.
    """
    from omega_loop.episode.packet import acknowledge_omega_mail

    try:
        # T8: the drain just made this fold's accepted receipts canonical; record what is
        # still owed and close its transaction. Bookkeeping never fails a committed close.
        from omega_loop.episode.transaction import settle_cognitive_transaction

        settle_cognitive_transaction(episode)
    except Exception:  # noqa: BLE001 - see above
        log.warning("cognitive transaction could not be settled at the terminus", exc_info=True)
    acknowledge_omega_mail(episode)


def _hand_off_orphaned_mail() -> None:
    """Every terminus sweeps held operator mail whose job can no longer fold (T7).

    A fold's bound mail returns to ready — released, or its lease lapsed — possibly after
    its job already settled, when the settlement's own sweep could not see it. Failure-soft:
    a close whose memory committed never fails on mail bookkeeping.
    """
    try:
        from pressure_loop.held_instruction_handoff import hand_off_orphaned_instructions

        hand_off_orphaned_instructions()
    except Exception:  # noqa: BLE001 - see docstring
        log.warning("held operator mail handoff failed at the terminus", exc_info=True)


def run_deepsleep_for_window(
    store: Any,
    window: Any,
    omega_outcome: Any,
    *,
    cascade_status: str = "completed",
) -> Optional[DeepSleepOutcomePack]:
    """The WINDOW-scope terminus (Phase 4 cut 10) — consolidate over ten episodes.

    Same transaction as ``run_deepsleep_for_episode`` — same commit region and
    its stamp (cut 13), same ``store.close(before_finalize=...)`` seam, same SRAM
    drain — with one substitution: the intake is built from the reflected WINDOW
    instead of this episode's own body.

    **What it consolidates, and what it must not** (operator ruling 2026-08-11,
    risk 7). Cut 10 ran the full twelve phases here. Five of them — mastery,
    failure, semantic, identity, query_lifecycle — re-derive from evidence the
    ten episodes' OWN termini already consumed, so a domain touched in five of
    them was credited five times and then a sixth. Those five are skipped at this
    scope (``WINDOW_SKIPPED_PHASES``), loudly. What remains is what only a window
    can know: the TRAJECTORY verdict across ten episodes, the reflection's own
    SYNTHESIS memory, the window journal row keyed by ``ds_input.window_id``, and
    the pressure this episode consumes by closing. The global-substrate commit
    region is untouched — the synthesis memory is a real write and must land
    through the same serialized region and stamp as everything else.

    ``context_id`` stays the EPISODE id, not the window id. It is the identity of
    the SRAM bucket this episode opened at admission and of every telemetry span
    in the terminus; handing it the window id would flush a bucket that does not
    exist and leave the episode's own bucket pending, which
    ``_close_episode_sram`` then (correctly) refuses. The window id rides
    ``ds_input.window_id``, so the journal ROW is keyed by the window — which is
    whose consolidation it is.
    """
    active_episode = store.current()
    if active_episode is None:
        raise RuntimeError("window DeepSleep requires an active episode carrier")

    pack: Optional[DeepSleepOutcomePack] = None
    commit_ran = False

    def _commit(closing_episode: Any, episode_body: dict) -> None:
        nonlocal commit_ran, pack
        if closing_episode is not active_episode:
            raise RuntimeError("window DeepSleep lost ownership of the active episode")
        episode_id = str(closing_episode.episode_id)
        from memory import episode_sram

        if not episode_sram.is_open(episode_id):
            episode_sram.open_bucket(
                episode_id,
                pipeline_id=closing_episode.pipeline_id,
            )
        pack = _run_deepsleep_episode_core(
            store,
            episode_id,
            lambda: intake_phase.build_deepsleep_input_for_window(window, omega_outcome),
            closing_episode=closing_episode,
            episode_body=episode_body,
            episode_temporal=dict(episode_body.get("temporal") or {}),
            episode_learning_state=dict(episode_body.get("learning_state") or {}),
            scope="window",
        )
        commit_ran = True

    try:
        closed_episode = store.close(
            cascade_status=cascade_status,
            before_finalize=_commit,
            after_archive=_acknowledge_bound_mail,
        )
    except Exception as producer_error:
        _record_deepsleep_failure(active_episode, producer_error)
        raise
    _hand_off_orphaned_mail()
    if closed_episode is not active_episode or not commit_ran:
        raise RuntimeError("window DeepSleep did not finalize its active episode")
    return pack


def _phase_runs(phase: str, *, context_id: str, scope: str) -> bool:
    """Decide whether a consolidation phase runs at this scope — and RECORD a skip.

    One function owns both halves on purpose: a skip that is decided in one place
    and reported in another can drift, and a silently-skipped phase is
    indistinguishable from one that crashed before it could emit (AGENTS.md
    §2.1). ``WINDOW_SKIPPED_PHASES`` is therefore the single source of truth for
    the operator's 2026-08-11 ruling, and the telemetry cannot disagree with it.

    The span is emitted in full (enter → skip), so a reader of the phase feed
    sees the same phase names for a window terminus as for an episode one, five
    of them stamped ``skipped:already_consolidated_per_episode``.
    """
    if scope != "window" or phase not in WINDOW_SKIPPED_PHASES:
        return True
    with phase_span("deepsleep", phase, episode_id=context_id, scope=scope) as span:
        span.skip(ALREADY_CONSOLIDATED)
    try:
        emit_data(
            "deepsleep",
            f"{phase}_skipped",
            episode_id=context_id,
            status="skip",
            summary=(
                f"{phase} not run at {scope} scope: its evidence was already "
                "consolidated by each episode's own terminus"
            ),
            reason=ALREADY_CONSOLIDATED,
            scope=scope,
        )
    except Exception:
        pass
    return False


def _reconcile_pending_trait_commits(context_id: str) -> None:
    """Complete already-durable retained traits before consolidation reads them.

    This is the retry of the previous drain's required post-commit work. Fresh
    envelopes remain in SRAM; intake acknowledgement stays in the normal drain.
    Derived link repair is excluded by the existing commit lock but does not
    claim a new finish-line stamp or sequenced projection coverage.
    """
    from core.cognition_lock import commit_lock
    from memory import episode_sram, intake_journal
    from memory.post_commit import ensure_trait_revision

    retained = [item for item in episode_sram.pending(context_id)
                if item.kind == "memory" and getattr(item.envelope, "type", "") == "emergent_trait"]
    if not retained:
        return
    with commit_lock():
        for item in retained:
            record = intake_journal.record_by_id(item.journal_record_id)
            if record is None or record.owner_episode_id != context_id:
                raise intake_journal.IntakeJournalError("retained trait has no matching intake owner")
            if record.status not in {intake_journal.PENDING, intake_journal.COMMITTED}:
                continue
            verdict, memory_id, detail = intake_journal.verify_canonical_binding(record)
            if verdict in {"missing", "no_identity"}:
                if record.status == intake_journal.COMMITTED:
                    raise intake_journal.IntakeJournalError(f"committed trait {record.record_id} is missing")
                continue
            if verdict != "ok":
                raise intake_journal.IntakeJournalError(
                    f"cannot reconcile retained trait {record.record_id}: canonical binding {verdict} ({detail})"
                )
            ensure_trait_revision(memory_id, sequenced=False)


def _record_pathway_outcomes(ds_input: DeepSleepInput, mastery_records: list) -> list:
    """Finalize motor attempts only after mastery has returned its real records.

    Returns each outcome's effect with its intake receipt (WO-07): a retried terminus finds the
    outcome it already recorded and reconciles, so one attempt is one canonical outcome.
    """
    from autonomic.pathway_credit import finalize_pathway_outcome
    from deepsleep_loop.effect_receipts import effect_id
    from memory.pathway_performance import record_pathway_outcome

    effects = []
    for entry in ds_input.episode_window or ():
        if not isinstance(entry, dict):
            continue
        outcome = finalize_pathway_outcome(entry.get("motor_attempt_assessment"), mastery_records)
        if outcome is None:
            continue
        receipt = record_pathway_outcome(outcome)
        effects.append({
            "effect_id": effect_id("pathway_outcome", outcome.episode_id, outcome.program_id),
            "kind": "pathway_outcome",
            "target": outcome.program_id,
            "receipt": {
                "episode_id": outcome.episode_id,
                "journal_record_id": str(getattr(receipt, "journal_record_id", "") or ""),
                "event_id": str(getattr(receipt, "event_id", "") or ""),
            },
        })
    return effects


def _compact_episode_temporal_summary(episode_temporal: dict | None) -> dict:
    """Compact the episode temporal envelope or fail the DeepSleep terminus."""
    if not episode_temporal:
        return {}
    try:
        from core.episode_temporal import compact_temporal_summary

        summary = compact_temporal_summary(episode_temporal)
        if not isinstance(summary, dict):
            raise TypeError(
                "compact_temporal_summary must return a dict, "
                f"got {type(summary).__name__}"
            )
        return dict(summary)
    except Exception as exc:  # noqa: BLE001 — required outcome metadata cannot degrade
        from core.required_substrate import RequiredSubstrateError

        failure = RequiredSubstrateError.from_exception(
            "episode_temporal",
            "compact",
            exc,
        )
        if failure is exc:
            raise
        raise failure from exc


def _compact_episode_learning_state_summary(episode_learning_state: dict | None) -> dict:
    """Compact the episode learning envelope or fail the DeepSleep terminus."""
    if not episode_learning_state:
        return {}
    try:
        from pressure_loop.symphony_learning_state import compact_learning_state_summary

        summary = compact_learning_state_summary(episode_learning_state)
        if not isinstance(summary, dict):
            raise TypeError(
                "compact_learning_state_summary must return a dict, "
                f"got {type(summary).__name__}"
            )
        return dict(summary)
    except Exception as exc:  # noqa: BLE001 — required outcome metadata cannot degrade
        from core.required_substrate import RequiredSubstrateError

        failure = RequiredSubstrateError.from_exception(
            "learning_state",
            "compact",
            exc,
        )
        if failure is exc:
            raise
        raise failure from exc


def _run_deepsleep_episode_core(
    store: Any,
    context_id: str,
    intake_builder,
    *,
    closing_episode: Any,
    episode_body: dict | None = None,
    episode_temporal: dict = None,
    episode_learning_state: dict = None,
    scope: str = "episode",
) -> Optional[DeepSleepOutcomePack]:
    """Run the one complete DeepSleep terminus for an episode.

    ``scope`` (Phase 4 cut 10) labels WHAT was consolidated — "episode" for the
    per-episode terminus, "window" for the ten-episode reflection's. It rides the
    journal row, the reindex/integrity/reconciliation telemetry and the HUD cycle
    so a window consolidation is distinguishable from an episode one after the
    fact.

    Since the 2026-08-11 ruling it also DECIDES one thing: the five phases in
    ``WINDOW_SKIPPED_PHASES`` do not run at window scope, because the ten
    episodes already consolidated that evidence one by one. Every other phase,
    and the whole commit region, is identical at both scopes.

    **The two halves (Phase 4 cut 13).** Everything down to practice grounding is
    LANE-LOCAL: it reads this episode's intake and writes this episode's own SRAM
    bucket and the per-store consolidation files (which now carry their own locks
    — deepsleep_loop/store_locks.py — because two lanes consolidating at once
    would otherwise lose one of two read-modify-writes silently). The caller
    holds only its lane's cognition lock here, so another lane's terminus runs
    right alongside it. That is the point of the cut: consolidating episode A no
    longer stops lane B.

    Then ``serialized_commit`` — the ONE global commit lock — opens with the
    finish-line stamp and closes after the substrate sweeps. Everything inside
    it is either a write to a structure every lane shares (canonical memory, the
    reflection window, the taxonomy index, the integrity projection) or a read
    that must not see another lane's half-finished sweep. Stamp order is landed
    order because the stamp is that region's first statement.

    ``closing_episode`` is the carrier the region stamps and seals;
    ``episode_body`` is the enriched body it re-stamps and hands to the window
    append. Both are required in practice — the two termini always pass them —
    and neither is optional-by-shape: an episode that reached this function
    without a carrier could not be given a place in the memory sequence.
    """
    if closing_episode is None:
        raise RuntimeError("DeepSleep terminus requires the closing episode carrier")

    # Memory-organ Cut 13 (D-13-73/78): the finish-line reservation gate runs at the terminus
    # ENTRANCE — before mastery / failure / identity / SRAM staging or any other lane-local
    # durable phase. Confirmed headroom >= MIN_SAFE_STAMP_HEADROOM admits; below the floor it
    # raises TimekeeperUnavailable here, with NOTHING written, and the terminus is retried on
    # a later tick. The stamp inside serialized_commit therefore never needs disk I/O.
    from core.timekeeper import ensure_stamp_headroom

    ensure_stamp_headroom()
    with phase_span("deepsleep", "trait_commit_reconciliation", episode_id=context_id):
        _reconcile_pending_trait_commits(context_id)
    # Program 0 publishes once the canonical commit is complete. The existing generation
    # remains the reader view while this episode writes its candidate inputs.
    # WO-07: what an earlier attempt of THIS terminus left behind decides whether it seals a new
    # record or replays the one it already committed (and keeps that record's stamp).
    resumed_body = _resume_sealed_record(context_id)

    journal_entry: dict = {}
    mastery_records = []
    failure_patterns: list = []
    pathway_effects: list = []
    association_plasticity: dict = {
        "registered": 0,
        "reconciled": 0,
        "credited": 0,
        "decayed": 0,
        "retired": 0,
        "skipped": [],
    }
    # Operator ruling 2026-08-11 (risk 7). See WINDOW_SKIPPED_PHASES: at window
    # scope the five per-episode phases re-consume evidence their own episodes
    # already consolidated, so the window fold does not run them. ``_phase_runs``
    # is the gate and it records every skip it decides.
    window_scope = scope == "window"

    with phase_span("deepsleep", "intake", episode_id=context_id):
        ds_input = intake_builder()

    mastery_effects: dict = {}
    if _phase_runs("mastery", context_id=context_id, scope=scope):
        with phase_span("deepsleep", "mastery", episode_id=context_id):
            prior_mastery = load_mastery()
            mastery_records = mastery_phase.update_mastery(ds_input)
            # Motor attempts are credited by the episode that MADE them; a
            # window re-finalizing them would record a second outcome for one
            # attempt. It rides the mastery skip because it reads the same rows.
            pathway_effects = _record_pathway_outcomes(ds_input, mastery_records)
            # WO-07: the effect this episode applied is measured from ITS receipts, so a retry
            # reports the credit the first pass landed instead of a zero delta.
            mastery_effects = mastery_effects_from_records(
                mastery_records,
                prior=episode_prior_mastery(
                    mastery_records, episode_id=context_id, fallback=prior_mastery
                ),
            )

    failure_effects: dict = {}
    if _phase_runs("failure", context_id=context_id, scope=scope):
        with phase_span("deepsleep", "failure", episode_id=context_id):
            prior_failures = load_failures()
            failure_patterns = failure_phase.consolidate_failures(ds_input)
            failure_effects = failure_effects_from_patterns(
                failure_patterns,
                prior=episode_prior_failures(
                    failure_patterns, episode_id=context_id, fallback=prior_failures
                ),
            )

    semantic_effects: dict = {}
    if _phase_runs("semantic", context_id=context_id, scope=scope):
        with phase_span("deepsleep", "semantic", episode_id=context_id):
            semantic_entries = semantic_phase.promote_semantic(ds_input, mastery_records)
            semantic_effects = semantic_effects_from_entries(semantic_entries)

    identity_effects: dict = {}
    if _phase_runs("identity", context_id=context_id, scope=scope):
        with phase_span("deepsleep", "identity", episode_id=context_id):
            prior_identity = load_identity()
            identity_traits = identity_phase.update_identity(ds_input)
            identity_effects = identity_effects_from_traits(
                identity_traits,
                prior=prior_identity,
            )

    # TRAJECTORY RUNS AT BOTH SCOPES — it is the window's whole purpose. No
    # single episode can say where ten of them are heading, so this is the one
    # consolidation the window alone can produce (the operator's lane-3 diagram:
    # sealed window → trajectory verdict → pressure feedback → close).
    trajectory_effects: dict = {}
    with phase_span("deepsleep", "trajectory", episode_id=context_id, scope=scope):
        prior_trajectory = load_trajectory()
        trajectory_records = trajectory_phase.maintain_trajectory(ds_input)
        trajectory_effects = trajectory_effects_from_records(
            trajectory_records,
            prior=prior_trajectory,
        )

    query_effects: list = []
    if _phase_runs("query_lifecycle", context_id=context_id, scope=scope):
        with phase_span("deepsleep", "query_lifecycle", episode_id=context_id):
            prior_queries = load_queries()
            query_records = query_lifecycle_phase.run_query_lifecycle(ds_input)
            query_effects = query_effects_from_queries(
                query_records,
                prior=prior_queries,
            )

    with phase_span("deepsleep", "consolidate", episode_id=context_id):
        journal_entry = consolidate_phase.consolidate(
            ds_input,
            mastery_effects=mastery_effects,
            identity_effects=identity_effects,
            semantic_effects=semantic_effects,
            trajectory_effects=trajectory_effects,
            failure_effects=failure_effects,
            query_effects=query_effects,
            scope=scope,
        )

    with phase_span("deepsleep", "projection", episode_id=context_id):
        projection_state = projection_phase.build_projection()

    with phase_span("deepsleep", "consolidate_memory", episode_id=context_id):
        from deepsleep_loop.consolidate_memory import consolidate_to_memory

        mem_result = consolidate_to_memory(
            context_id,
            journal_entry,
            mastery_effects=mastery_effects,
            identity_effects=identity_effects,
            semantic_effects=semantic_effects,
            query_effects=query_effects,
            # The window's SYNTHESIS — what the mind concluded by looking across
            # ten episodes, produced by no single one of them (operator ruling
            # 2026-08-11). With the five re-consolidating phases skipped this and
            # the journal row are the window's whole memory footprint, so it is
            # written explicitly rather than left to fall out of empty effects.
            window_reflection=(ds_input.window_reflection if window_scope else None),
        )
        try:
            emit_data(
                "deepsleep",
                "consolidate_to_memory",
                episode_id=context_id,
                summary=f"written={len(mem_result.get('written') or [])}",
                written=list(mem_result.get("written") or []),
                failed=list(mem_result.get("failed") or []),
                enabled=bool(mem_result.get("enabled")),
            )
        except Exception:
            pass
    with phase_span("deepsleep", "practice_grounding", episode_id=context_id):
        _ground_practice_report_from_sram(context_id)

    # ── the serialized commit region (Phase 4 cut 13) ────────────────────────
    # Lane-local work ends here. Everything below writes a structure every lane
    # shares, so it runs under the ONE commit lock — and the lock's first
    # statement is the finish-line stamp, which is what makes stamp order and
    # landed order the same order for every lane, permanently and by
    # construction rather than by an argument about what else is serialized.
    from core.commit_region import replayed_commit, serialized_commit
    from schema.episode_record import EpisodeRecord

    effects = _terminus_effects(
        context_id,
        mastery_records=mastery_records,
        failure_patterns=failure_patterns,
        pathway_effects=pathway_effects,
        journal_entry=journal_entry,
        scope=scope,
    )
    # WO-07: an episode whose sealed record an earlier attempt already committed keeps that
    # record's place — the region is re-entered WITHOUT a new stamp. Everything else is stamped
    # here, as the lock's first statement, exactly as before.
    region = (
        replayed_commit(closing_episode, dict(resumed_body.get("timekeeper") or {}))
        if resumed_body is not None
        else serialized_commit(closing_episode)
    )
    with region:
        # The enriched body was built by ``EpisodeStore.close`` BEFORE the stamp
        # existed. Refresh the two blocks the stamp decides before anything
        # downstream (the reflection window, _last_closed_body) reads it.
        from pressure_loop.episode_journal import restamp_enriched_body

        restamp_enriched_body(episode_body, closing_episode)
        # Canonical capture carries the complete enriched body, its finish-line
        # stamp, the mastery effects produced by this terminus and — WO-07 — the
        # sealed EpisodeRecord, assembled ONCE, here, after the stamp. Reflection
        # checkpoints persist only episode ids; this lobe-9 record is the one
        # durable source from which a window is hydrated.
        with phase_span("deepsleep", "canonical_projection", episode_id=context_id):
            if resumed_body is not None:
                canonical_body = dict(resumed_body)
                sealed_record = EpisodeRecord.from_dict(canonical_body["episode_record"])
            else:
                from memory.episode_events import read_episode_events
                from memory.episode_record_seal import seal_episode_record

                sealed_record = seal_episode_record(
                    closing_episode, events=read_episode_events(context_id), effects=effects
                )
                canonical_body = dict(episode_body or _canonical_episode_view(closing_episode))
                canonical_body["mastery_effects"] = dict(mastery_effects or {})
                canonical_body["episode_record"] = sealed_record.to_dict()
                capture_report = _capture_closed_episode_to_taxonomy(context_id, canonical_body)
                capture_errors = list(capture_report.get("errors") or [])
                if capture_errors:
                    raise RuntimeError(
                        "canonical episode capture failed: " + "; ".join(map(str, capture_errors))
                    )
        from memory.episode_event_publication import (
            stage_event_publications,
            verify_committed_supplements,
            verify_published_supplements,
        )
        from memory.episode_record_seal import episode_record_id
        from schema.continuity_json import CanonicalRef

        # The original seal is immutable. A retry carries events captured after it
        # as ordinary envelopes through this same SRAM intake and drain.
        parent_ref = CanonicalRef(
            record_id=episode_record_id(context_id), record_version=1,
            content_sha256=sealed_record.content_sha256(),
        )
        event_publications = stage_event_publications(sealed_record, parent_ref)
        # Every DeepSleep producer above has now placed its envelopes in SRAM. Drain the
        # complete batch, rebuild recall from the resulting filesystem, then maintain it.
        with phase_span("deepsleep", "episode_sram_flush", episode_id=context_id):
            # The flush reports what did NOT land (`errors`, `skipped`).  Dropping the
            # return made a failed ingest silent at the one seam that is supposed to
            # make the single memory lane auditable (AGENTS.md §2.1, §5.0).
            episode_sram_flush = _flush_episode_sram(context_id)
        # WO-07: the canonical receipt. A rejected or still-pending required record stops the
        # terminus here, with the episode open and its cause attached.
        record_ref = _bind_sealed_record(
            context_id,
            sealed_record,
            capture_receipt=(
                None if resumed_body is not None
                else capture_report.get("episode_record_receipt")
            ),
        )
        if record_ref != parent_ref:
            raise RuntimeError("the canonical receipt names a different parent than its event publication")
        for publication in event_publications:
            verify_committed_supplements(publication.supplements, publication.parent_ref)
        with phase_span("deepsleep", "workspace_retention", episode_id=context_id):
            workspace_retention = _process_workspace_retention(context_id)
        # Contradiction maintenance may tombstone canonical records. It must finish
        # before the one synchronous rebuild so recall reflects those removals in
        # this same DeepSleep pass; a delayed second rebuild is a second lane.
        with phase_span("deepsleep", "conflict_reconciliation", episode_id=context_id):
            _run_conflict_reconciliation(context_id=context_id, scope=scope)
        # Canonical-side association and source-validity state must be current before Program 0
        # captures its closed input snapshot. The full candidate build and publication happen
        # after this serialized commit region so other lanes can continue committing.
        def consolidate_before_projection(root: Path | str | None) -> None:
            nonlocal association_plasticity
            with phase_span("deepsleep", "association_plasticity", episode_id=context_id):
                association_plasticity = _consolidate_associations(closing_episode, root)

        source_validity_counts = _maintain_projection_inputs_after_deepsleep(
            context_id=context_id,
            scope=scope,
            before_projection=consolidate_before_projection,
        )
        # Publish the ordered reflection checkpoint after the canonical episode
        # record and the projection inputs have landed. The body and
        # mastery fields below are process-local caches for the current run;
        # ReflectionWindow.to_dict deliberately excludes both. WO-09: the exact
        # reference just bound is pinned with the member — the checkpoint's
        # durable claim is "this episode joined as THIS sealed record".
        with phase_span("deepsleep", "reflection_window_append", episode_id=context_id):
            store.commit_reflection_window_append(
                closing_episode,
                canonical_body,
                mastery_delta=mastery_effects,
                record_ref=record_ref,
            )
        with phase_span("deepsleep", "pathway_projection", episode_id=context_id):
            from memory.pathway_performance import rebuild_pathway_projection

            rebuild_pathway_projection()
        with phase_span("deepsleep", "memory_integrity", episode_id=context_id):
            memory_integrity_projection = _run_memory_integrity_housekeeping(
                context_id=context_id,
                scope=scope,
            )

    # The build reads a closed canonical snapshot and runs outside the commit lock. Publication
    # preserves the previous immutable reader view until the single Program 0 barrier succeeds.
    with phase_span("deepsleep", "projection_publication", episode_id=context_id):
        projection_dispatch_report = _publish_projection_after_deepsleep(
            context_id=context_id,
            scope=scope,
        )

    identity_store = {key: trait.to_dict() for key, trait in load_identity().items()}
    mastery_store = {key: record.to_dict() for key, record in load_mastery().items()}
    open_query_store = {key: query.to_dict() for key, query in load_queries().items()}
    trajectory_store = {key: record.to_dict() for key, record in load_trajectory().items()}

    # Compact temporal and learning summaries for DEEPSLEEP threading
    # (DEFERRED_INTEGRATION.md §1-2). A failed compaction is required outcome
    # work, not an empty summary that could masquerade as success.
    temporal_summary = _compact_episode_temporal_summary(episode_temporal)
    learning_state_summary = _compact_episode_learning_state_summary(episode_learning_state)

    # Maintenance health is exposed in projection_state only.  It cannot
    # manufacture successor pressure or reopen cognition after episode close.
    pressure_seed = []

    pack = DeepSleepOutcomePack(
        episode_id=context_id,
        journal_entry=journal_entry,
        updated_memory=journal_entry,
        updated_identity=identity_store,
        updated_mastery=mastery_store,
        updated_open_queries=open_query_store,
        updated_trajectory=trajectory_store,
        pressure_seed=pressure_seed,
        projection_state={
            **projection_state.to_dict(),
            "memory_integrity_projection": memory_integrity_projection,
            "episode_sram_flush": episode_sram_flush,
            "workspace_retention": workspace_retention,
            "source_validity_projection": source_validity_counts,
            "projection_dispatch": {
                "owner": projection_dispatch_report.owner,
                "action": projection_dispatch_report.action,
                "generation_id": projection_dispatch_report.generation_id,
                "drain_status": projection_dispatch_report.drain_status,
            },
            "association_plasticity": association_plasticity,
            "episode_record": {
                "ref": record_ref.to_dict(),
                "replayed": resumed_body is not None,
                "acknowledged_events": 0,
            },
        },
        episode_closed=True,
        pressure_reset=True,
        temporal=temporal_summary,
        learning_state=learning_state_summary,
    )
    # Cleanup belongs after the final required outcome work. Until then an empty
    # SRAM buffer still owns late failure capture; already-drained content is gone.
    from memory import episode_sram

    for publication in event_publications:
        verify_published_supplements(
            publication.supplements, publication.parent_ref,
            episode_id=publication.record.episode_id,
        )
    if episode_sram.pending(context_id):
        raise RuntimeError(f"episode SRAM bucket {context_id} has unpublished late intake")
    acknowledged_events = sum(
        _acknowledge_published_events(
            publication.record, publication.parent_ref,
            supplements=publication.supplements, owner_episode_id=context_id,
        )
        for publication in event_publications
    )
    pack.projection_state["episode_record"]["acknowledged_events"] = acknowledged_events
    episode_sram.clear_bucket(context_id)
    _write_hud_deepsleep_cycle(
        context_id=context_id,
        journal_entry=journal_entry,
        journal_scope=scope,
        mastery_effects=mastery_effects,
        identity_effects=identity_effects,
        semantic_effects=semantic_effects,
        trajectory_effects=trajectory_effects,
        failure_effects=failure_effects,
        query_effects=query_effects,
        mem_result=mem_result,
        pressure_seed=pressure_seed,
        memory_integrity_projection=memory_integrity_projection,
    )
    try:
        emit_data(
            "deepsleep",
            "deepsleep_outcome_pack",
            episode_id=context_id,
            summary=f"{scope} consolidated",
            episode_closed=True,
            pressure_reset=True,
            reseed_count=0,
        )
    except Exception:
        pass
    return pack


__all__ = [
    "run_deepsleep_for_episode",
    "run_deepsleep_for_window",
    "_run_deepsleep_episode_core",
]
