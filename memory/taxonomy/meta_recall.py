"""Phase 7N — Meta recall context for skill selection.

``recall_skill_context()`` runs the deterministic recall engine (7J) over the compiled
graph for a USE_SKILL objective and derives the signals Meta uses to select WITH memory:

  - ``skill_penalties`` — per-skill nudge DOWN: the skill produced a failure before, or its
    capability was weakened (7K failure topology surfaces as ``produced_status=failure`` /
    ``capability_signal=weakened`` claims).
  - ``skill_boosts``    — per-skill nudge UP: a proven, grounded successful workflow exists
    for a similar objective (7L ``workflow_memory`` node), or the capability was reinforced.
  - ``cautions``        — tool / bridge / safety failures recalled for the objective
    (``failed_because`` claims) — context Meta may surface without blocking selection.

Plus the recall evidence (``evidence_event_ids``) and the ``snapshot_hash`` / ``query_hash``
for the SkillExecutionPack metadata. Recall INFORMS Meta — it never chooses the action and
never executes. Deterministic (same graph + objective => identical context), model-free,
Guardian-free. Errors propagate so the caller can record the degraded-selection warning.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Optional

from memory.taxonomy.recall import recall_skill_graph as recall

_FAIL_STATUSES = frozenset({"failure", "failed", "error"})
_WEAKEN_SIGNALS = frozenset({"weakened", "weaken"})
_REINFORCE_SIGNALS = frozenset({"reinforced", "reinforce", "strengthened"})

# Modest deltas: recall NUDGES the deterministic selection score, never dominates it. The
# deterministic skill-fit floor (intent/kind/tool/safety match) stays primary.
_PENALTY = 0.15
_BOOST_WORKFLOW = 0.12
_BOOST_REINFORCE = 0.06


def _bump(table: dict[str, float], key: str, value: float) -> None:
    if key:
        table[key] = max(table.get(key, 0.0), value)


def _project_key(row: dict) -> str:
    explicit = str(row.get("project_key") or "").strip().lower()
    if explicit:
        return explicit
    for raw in row.get("files_changed") or ():
        parts = [part for part in str(raw or "").replace("\\", "/").split("/") if part]
        if len(parts) >= 2 and parts[0] not in {".", ".."}:
            return re.sub(r"[^a-z0-9_-]+", "-", parts[0].lower()).strip("-_")
    return re.sub(r"[^a-z0-9]+", "-", str(row.get("objective") or "").lower()).strip("-")


def _freshness(row: dict) -> tuple[str, str]:
    return (
        str(row.get("created_at") or ""),
        str(row.get("source_event_id") or ""),
    )


def recall_skill_context(objective: str, *, root: Optional[Path | str] = None) -> dict:
    """Return deterministic skill-selection context recalled for ``objective``.

    Reads the COMPILED graph only (never the model, never embeddings). Raises on a recall /
    graph error so the Meta hook can record the warning + degrade — it never silently
    returns a partial context.
    """
    from memory.projection.generation import projection_read_transaction

    with projection_read_transaction(root):  # Cut 13: one generation view for the whole context
        packet = recall(objective, query_kind="skill_use", root=root)

    penalties: dict[str, float] = {}
    boosts: dict[str, float] = {}
    cautions: list[dict] = []
    prior_builds: list[dict] = []
    build_in_progress: list[dict] = []

    for result in packet.results:
        node_type = result.get("node_type")
        payload = result.get("payload") or {}
        if str(payload.get("status") or "") in {"quarantined", "rejected", "superseded"}:
            continue
        evidence = list(result.get("evidence_event_ids") or [])

        if node_type == "workflow_memory":
            outcome = str(payload.get("outcome"))
            skill = str(payload.get("skill_name") or "").strip()
            if outcome == "success":
                _bump(boosts, skill, _BOOST_WORKFLOW)
                # The ARTIFACT recall: WHAT this kind of objective produced before — so Omega's
                # inference can reason over "I've already built this" instead of only being
                # quietly more confident. This is the half that makes the mind REMEMBER, not just
                # get better at selecting.
                prior_builds.append(
                    {
                        "skill_name": skill,
                        "objective": str(payload.get("objective_pattern") or "").strip(),
                        "summary": str(payload.get("summary") or "").strip(),
                        "files_changed": list(payload.get("files_changed") or ()),
                        "source_episode_id": str(payload.get("source_episode_id") or ""),
                        "evidence_event_ids": evidence,
                        "project_key": _project_key(payload),
                        "created_at": str(payload.get("created_at") or ""),
                        "source_event_id": str(payload.get("source_event_id") or ""),
                    }
                )
            elif outcome == "in_progress":
                # The FOLD-BACK recall: an unfinished build's progress digest. Omega reasons over
                # this to decide continue|finish, and it threads into the resume directive so qwen
                # picks up oriented. NOT a boost — unfinished work doesn't reinforce the skill.
                build_in_progress.append(
                    {
                        "skill_name": skill,
                        "objective": str(payload.get("objective_pattern") or "").strip(),
                        "progress_digest": str(payload.get("progress_digest") or "").strip(),
                        "files_changed": list(payload.get("files_changed") or ()),
                        "rounds_used": int(payload.get("rounds_used") or 0),
                        "source_episode_id": str(payload.get("source_episode_id") or ""),
                        "evidence_event_ids": evidence,
                        "project_key": _project_key(payload),
                        "created_at": str(payload.get("created_at") or ""),
                        "source_event_id": str(payload.get("source_event_id") or ""),
                    }
                )
            continue

        if node_type != "claim":
            continue
        subject = str(payload.get("subject") or "").strip()
        predicate = str(payload.get("predicate") or "")
        obj = str(payload.get("object") or "").strip().lower()

        if predicate == "produced_status":
            if obj in _FAIL_STATUSES:
                _bump(penalties, subject, _PENALTY)
        elif predicate == "capability_signal":
            if obj in _WEAKEN_SIGNALS:
                _bump(penalties, subject, _PENALTY)
            elif obj in _REINFORCE_SIGNALS:
                _bump(boosts, subject, _BOOST_REINFORCE)
        elif predicate == "failed_because":
            cautions.append({"failure_class": obj, "evidence_event_ids": evidence})

    # Select one authoritative state per project. A completion only supersedes an older
    # checkpoint; a newer checkpoint must supersede an older completion so stale success prose
    # cannot steer a resume after verification regresses.
    states: dict[str, tuple[str, dict]] = {}
    for outcome, rows in (("success", prior_builds), ("in_progress", build_in_progress)):
        for row in rows:
            key = _project_key(row)
            current = states.get(key)
            if current is None or _freshness(row) > _freshness(current[1]):
                states[key] = (outcome, row)
    prior_builds = [row for outcome, row in states.values() if outcome == "success"]
    build_in_progress = [row for outcome, row in states.values() if outcome == "in_progress"]

    return {
        "objective": objective,
        "query_hash": packet.query_hash,
        "snapshot_hash": packet.snapshot_hash,
        "skill_penalties": penalties,
        "skill_boosts": boosts,
        "cautions": cautions,
        "prior_builds": prior_builds,
        "build_in_progress": build_in_progress,
        "evidence_event_ids": list(packet.evidence_event_ids),
    }
