"""Pure helpers for the ten-episode window reflection: view, contract, fallback.

A window episode runs the canonical cascade (Global A → Meta A → AUTO → Meta B →
Global B → Omega). Its one model pass is Omega's window-lane fold, selected by
``foldback_spec_for`` like every other episode; nothing in this module invokes a model.

What only the window can see is the shape ACROSS ten already-folded episodes — what
repeated, what stalled, where the mind is heading. So Global B packages a COMPACTED
view (``build_window_view``: each episode's spine, never its full body), and the
window fold's reflection payload is validated here (``validate_window_payload``) and
shaped into the outcome the archive and the window-scope DeepSleep read
(``coerce_window_outcome``):

    reflection.summary                → the window's synthesis memory
    reflection.reasoning              → journal omega_reasoning + the synthesis
    reflection.emergent_observations  → the synthesis memory
    reflection.trajectory             → journal trajectory snapshot fallback
    trajectory_update                 → the trajectory store's upsert

Those consumers narrowed with the operator's 2026-08-11 scope ruling (risk 7): the
window terminus does not rerun mastery/failure/semantic/identity/query_lifecycle,
because the ten episodes each consolidated that evidence at their own terminus.

**THE WINDOW MUST NOT FAIL** (operator ruling 2026-08-11, risk 6). A reflection that
could not be produced used to raise into the fail-hard relay while the window was
still staged, so every restart re-ignited the same window into the same crash. When
the Omega window fold is contained as a failed thought, the cascade archives
``deterministic_window_verdict`` instead: a real verdict counted from the ten
episodes' own recorded outcomes, never fabricated, naming no trajectory it has no
basis to name. Every outcome carries ``reflection_source`` ("model" or
"deterministic"), so the archive, the journal row and the synthesis memory always
say which mind produced them.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, List, Optional

log = logging.getLogger("omega.omega_loop.window")

# Which mind produced a window verdict. Rides every outcome (operator ruling
# 2026-08-11 (d)) so the archive is never ambiguous about its own authorship.
REFLECTION_SOURCE_MODEL = "model"
REFLECTION_SOURCE_DETERMINISTIC = "deterministic"

# The trajectory vocabulary the prompt asks for. An empty decision is legal and
# means "this window changed nothing", which the trajectory phase already
# handles by skipping on an empty target_domain.
_TRAJECTORY_DECISIONS = frozenset({"establish", "continue", "adjust", "complete"})

# How many derived observations the deterministic verdict will name. It counts
# every episode but reports only what recurs, strongest first — a list of ten
# one-off rows is noise, not a pattern.
_DERIVED_OBSERVATION_LIMIT = 4

# How much of each episode's spine rides the prompt. Ten bodies at full fidelity
# would be tens of thousands of tokens of packs, brain turns and evidence — the
# 2026-06 window burn this cut must not repeat (commit 4c3e73380, "stop the
# window-reflection GPU-burn"). The spine is what a reflection over a WINDOW
# actually needs; the detail is already consolidated per episode.
_TEXT_CLIP = 240
_TURN_CLIP = 200
_TURNS_PER_EPISODE = 2
_SUPPLEMENTAL_EVENTS_PER_EPISODE = 2
# Omega's own synthesis is the single most discriminating field an episode carries
# (EARLY-V01: 20 distinct values across 20 real records, where `action`, `cascade_status`
# and `outcome_status` each held ONE). It gets a longer bound than _TEXT_CLIP because a
# clipped synthesis loses the "next step" clause that makes two episodes comparable.
_SYNTHESIS_CLIP = 600
# Ten members must fit _json_data_block's budget in the window pack. That budget is
# enforced by _harden_bounded's binary search, which cuts the TAIL -- the most recent
# episodes -- so overflow would silently discard exactly what the window needs. Pinned by
# tests/core/test_window_spine_contract.py.
_FILES_PER_EPISODE = 12


def _clip(value: Any, limit: int = _TEXT_CLIP) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:limit]


def _episode_verdict(body: Dict[str, Any]) -> Dict[str, Any]:
    """The episode's own verdict, at the level Global B actually writes it.

    ``write_outcome`` sets ``episode.global_["outcome"]`` to ``asdict(GlobalOutcomePack)``,
    so the body's ``outcome`` is the whole PACK and the verdict a window judges by is
    ``pack["outcome"]`` — with ``status`` and ``summary`` inside it.

    Reading those at the pack's top level found nothing, every window ran on empty
    fields, and the lane folded over ten episodes reporting "empty outcome_status and
    outcome_detail ... all episodes are hollow and indistinguishable" while an operator's
    build went round in a circle inside them (live 2026-09-20). Its remaining fallback was
    ``goal_state``, which law section 3 retired — the tell that this reader was written
    against an older shape and never revisited.

    One accessor, one contract: the shape is asserted end to end against the real
    producer in tests/core/test_window_spine_contract.py.
    """
    pack = body.get("outcome")
    if not isinstance(pack, dict):
        return {}
    verdict = pack.get("outcome")
    return verdict if isinstance(verdict, dict) else {}


def _omega_receipts(body: Dict[str, Any]) -> Dict[str, Any]:
    """What Omega itself concluded in this episode, from the body's ``omega_result``.

    cognition_chain writes ``global_["omega_result"] = OmegaEpisodeResult.to_dict()`` and
    episode_journal lifts it into the body, so these are the mind's own words about its own
    episode. The window dropped all of them, which is why ten episodes of a looping build
    read as one row (EARLY-V01, 2026-09-21).
    """
    # A STABLE key set, always. Returning {} for an episode with no omega_result would
    # make the spine's SHAPE vary per row, so a reader could not tell "this episode
    # recorded no conclusion" from "this projection does not carry conclusions" without
    # branching on every member. Absent values render empty; absence of the key does not.
    result = body.get("omega_result")
    result = result if isinstance(result, dict) else {}
    control = result.get("control_decision")
    control = control if isinstance(control, dict) else {}
    intent = result.get("intent_decision")
    intent = intent if isinstance(intent, dict) else {}
    params = result.get("pressure_params")
    return {
        "synthesis": _clip(control.get("synthesis") or result.get("synthesis"), _SYNTHESIS_CLIP),
        "control_decision": _clip(control.get("decision"), 24),
        "intent_reason": _clip(intent.get("reason"), _TEXT_CLIP),
        "intent_confidence": intent.get("confidence"),
        # Debt that accumulates rather than resolves is what separates a stall from
        # iteration; the sign matters, so the numbers are passed through unrounded.
        "pressure_params": dict(params) if isinstance(params, dict) else {},
    }


def _names(paths: Any) -> List[str]:
    """File basenames. A window compares WHICH files moved; the root repeats every row."""
    if not isinstance(paths, list):
        return []
    out: List[str] = []
    for item in paths[:_FILES_PER_EPISODE]:
        text = str(item or "").replace("\\", "/").rstrip("/")
        if text:
            out.append(text.rsplit("/", 1)[-1])
    return out


def _agent_receipts(body: Dict[str, Any]) -> Dict[str, Any] | None:
    """The build receipts, or ``None`` when this episode ran no agent at all.

    Law section 2.1 lives here. ``touched_files: []`` means the agent RAN and changed
    nothing -- the single most important fact in a stalled window, and the one the trial's
    winning arm cited most. ``None`` means there was no agent run to report, as on a chat
    turn. Collapsing the two would render a stalled build identically to a conversation,
    so the absent case is a DIFFERENT value, never an empty one.
    """
    outputs = body.get("outputs")
    run = (outputs or {}).get("agent_run") if isinstance(outputs, dict) else None
    if not isinstance(run, dict):
        return None
    return {
        # One job id across a whole window is what makes repetition a stall rather than
        # a series of fresh attempts.
        "job_id": _clip(run.get("job_id"), 64),
        "status": _clip(run.get("status"), 32),
        "steps": run.get("steps"),
        "project_files": _names(run.get("project_files")),
        "touched_files": _names(run.get("touched_files")),
        "stopped_reason": _clip(run.get("stopped_reason"), _TEXT_CLIP),
    }


def _episode_spine(body: Dict[str, Any], index: int) -> Dict[str, Any]:
    verdict = _episode_verdict(body)
    turns: List[str] = []
    for row in list(body.get("brain_turns") or [])[-_TURNS_PER_EPISODE:]:
        if not isinstance(row, dict):
            continue
        text = _clip(row.get("text") or row.get("response_summary") or "", _TURN_CLIP)
        if text:
            turns.append(text)
    spine = {
        "n": index + 1,
        "episode_id": _clip(body.get("episode_id"), 64),
        "intent": _clip(body.get("intent"), 40),
        "action": _clip(body.get("action"), 40),
        "cascade_status": _clip(body.get("cascade_status"), 24) or "completed",
        "outcome_status": _clip(verdict.get("status"), 40),
        "outcome_detail": _clip(verdict.get("summary")),
        "turns": turns,
        "work_kind": _clip(body.get("work_kind"), 40),
    }
    # EARLY-V01, 2026-09-21. Everything above this line held at most two distinct values
    # across twenty real records of one build; everything below already sat in the same
    # bodies and was discarded here. Projecting it took the trial's assessment from 0/9 to
    # 9/9 with no change to what is captured. Nothing here reads a new source: these are
    # the same episode bodies the window always received.
    spine.update(_omega_receipts(body))
    spine["agent_run"] = _agent_receipts(body)
    supplements = list(body.get("supplemental_events") or ())
    spine["supplemental_events"] = {
        "count": len(supplements),
        "recent": [
            {
                "event_id": event["event_id"],
                "phase": event["phase"],
                "event_kind": event["event_kind"],
                "capture_status": event["status"],
                "reason": _clip((event.get("payload") or {}).get("reason")),
            }
            for event in supplements[-_SUPPLEMENTAL_EVENTS_PER_EPISODE:]
        ],
    }
    return spine


def build_window_view(window: Any) -> Dict[str, Any]:
    """The compacted view of a locked window the fold reasons over."""
    bodies = [b for b in (getattr(window, "episode_bodies", None) or []) if isinstance(b, dict)]
    snapshot = getattr(window, "trajectory_snapshot", None)
    statuses = getattr(window, "cascade_status_by_episode", None) or {}
    return {
        "window_id": str(getattr(window, "window_id", "") or ""),
        "episode_count": int(getattr(window, "episode_count", 0) or 0),
        "current_trajectory": dict(snapshot) if isinstance(snapshot, dict) else {},
        "episodes": [_episode_spine(body, i) for i, body in enumerate(bodies)],
        "failed_episodes": sorted(
            eid for eid, status in dict(statuses).items() if str(status) != "completed"
        ),
    }


def _require_str(container: Dict[str, Any], key: str, where: str) -> str:
    value = container.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{where}.{key} must be a string, got {type(value).__name__}")
    return value.strip()


def _require_str_list(container: Dict[str, Any], key: str, where: str) -> List[str]:
    rows = container.get(key)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError(f"{where}.{key} must be a list of strings")
    out: List[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, str):
            raise ValueError(f"{where}.{key}[{index}] must be a string")
        if row.strip():
            out.append(row.strip())
    return out


def validate_window_payload(payload: Any) -> None:
    """Contract-validate the model's object BEFORE a single field of it is shaped.

    Operator ruling 2026-08-11 (a): "schema-validated model output — mirror the
    strictest existing pattern". That pattern is
    ``omega_loop.episode.prompts._parse_pressure_lifecycle``: type-check every
    field the consumers will read and raise a ValueError that NAMES the
    violation, because the repair pass feeds that exact sentence back to the
    model.

    What the predecessor let through, measured against this contract: it checked
    two keys and ``str()``-coerced the rest, so ``{"reflection": {"reasoning":
    "sure thing"}}`` validated and archived a window whose reasoning was a
    sentence in every place the journal renders an object; a ``confidence`` of
    ``"high"`` silently became 0.0; a ``target_capability`` with no
    ``target_domain`` was silently dropped by the trajectory phase; and a
    reflection whose every field was empty was archived as a thought.

    Raises ValueError; never returns a value.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"window reflection must be a JSON object, got {type(payload).__name__}")

    reflection = payload.get("reflection")
    if not isinstance(reflection, dict):
        raise ValueError("window reflection missing the 'reflection' object")

    summary = _require_str(reflection, "summary", "reflection")

    reasoning = reflection.get("reasoning")
    if not isinstance(reasoning, dict):
        raise ValueError("reflection.reasoning must be an object with what/why/identity")
    what = _require_str(reasoning, "what", "reflection.reasoning")
    why = _require_str(reasoning, "why", "reflection.reasoning")
    _require_str(reasoning, "identity", "reflection.reasoning")
    for key, value in reasoning.items():
        if not isinstance(value, str):
            raise ValueError(f"reflection.reasoning.{key} must be a string")
    # A reflection that says nothing is not a reflection. The window is archived
    # AND consolidated into memory, so an all-empty object would be the mind
    # recording that it looked back and thought literally nothing.
    if not (summary or what or why):
        raise ValueError("reflection carries no summary and no reasoning — nothing was reflected")

    emergent = reflection.get("emergent_observations")
    if emergent is not None:
        if not isinstance(emergent, dict):
            raise ValueError("reflection.emergent_observations must be an object")
        _require_str_list(emergent, "repeated_behaviors", "reflection.emergent_observations")
        _require_str_list(emergent, "stable_patterns", "reflection.emergent_observations")

    trajectory = reflection.get("trajectory")
    if trajectory is not None:
        if not isinstance(trajectory, dict):
            raise ValueError("reflection.trajectory must be an object")
        decision = _require_str(trajectory, "decision", "reflection.trajectory").lower()
        if decision and decision not in _TRAJECTORY_DECISIONS:
            raise ValueError(
                f"invalid reflection.trajectory.decision {decision!r} — expected one of "
                f"{sorted(_TRAJECTORY_DECISIONS)} or an empty string"
            )
        _require_str(trajectory, "target_domain", "reflection.trajectory")
        _require_str(trajectory, "target_capability", "reflection.trajectory")
        _require_str(trajectory, "reason", "reflection.trajectory")

    update = payload.get("trajectory_update")
    if update is not None:
        if not isinstance(update, dict):
            raise ValueError("trajectory_update must be an object")
        domain = _require_str(update, "target_domain", "trajectory_update")
        capability = _require_str(update, "target_capability", "trajectory_update")
        _require_str(update, "reason", "trajectory_update")
        raw_confidence = update.get("confidence")
        if raw_confidence is not None:
            if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
                raise ValueError(
                    "trajectory_update.confidence must be a number between 0.0 and 1.0"
                )
            if not 0.0 <= float(raw_confidence) <= 1.0:
                raise ValueError(
                    f"trajectory_update.confidence {float(raw_confidence)} is outside [0.0, 1.0]"
                )
        if capability and not domain:
            # The trajectory phase keys on target_domain and skips without one,
            # so this shape names a capability the store can never record.
            raise ValueError(
                "trajectory_update.target_capability was given with no target_domain — "
                "a capability with no domain cannot be recorded"
            )


def coerce_window_outcome(payload: Dict[str, Any], view: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a VALIDATED model object into the omega_outcome the archive + DeepSleep read.

    Structuring only — every VALUE is the model's. Missing keys become empty
    containers rather than invented content: an absent trajectory_update means
    "this window changed nothing", which the trajectory phase already handles by
    skipping on an empty target_domain. All rejection lives in
    ``validate_window_payload`` above, so nothing here has to defend itself.
    """
    validate_window_payload(payload)

    reflection = dict(payload["reflection"])
    reasoning = dict(reflection.get("reasoning") or {})
    emergent = dict(reflection.get("emergent_observations") or {})
    trajectory = dict(reflection.get("trajectory") or {})
    update = dict(payload.get("trajectory_update") or {})

    target_domain = str(
        update.get("target_domain") or trajectory.get("target_domain") or ""
    ).strip()
    target_capability = str(
        update.get("target_capability") or trajectory.get("target_capability") or ""
    ).strip()
    confidence = float(update.get("confidence") or 0.0)

    return {
        "status": "reflected",
        "window_id": view["window_id"],
        "episode_count": view["episode_count"],
        "reflection_source": REFLECTION_SOURCE_MODEL,
        "reflection": {
            "summary": str(reflection.get("summary") or "").strip(),
            "reasoning": {str(k): v for k, v in reasoning.items()},
            "emergent_observations": {
                "repeated_behaviors": _require_str_list(
                    emergent, "repeated_behaviors", "reflection.emergent_observations"
                ),
                "stable_patterns": _require_str_list(
                    emergent, "stable_patterns", "reflection.emergent_observations"
                ),
            },
            "trajectory": {
                "decision": str(trajectory.get("decision") or "").strip().lower(),
                "target_domain": target_domain,
                "target_capability": target_capability,
                "reason": str(trajectory.get("reason") or "").strip(),
            },
        },
        "trajectory_update": (
            {
                "target_domain": target_domain,
                "target_capability": target_capability,
                "confidence": confidence,
                "supporting_episode": view["window_id"],
                "reason": str(update.get("reason") or trajectory.get("reason") or "").strip(),
            }
            if target_domain
            else {}
        ),
    }


def _window_evidence(window: Any) -> Dict[str, Any]:
    """Count what the ten episodes RECORDED about themselves.

    Every number here is read off a closed episode's own body — cascade_status,
    the motor attempt's verification verdict and failure kind, the intent and
    action it ran under. Nothing is inferred and nothing is generated, which is
    what makes a verdict built from it honest without a model (operator ruling
    2026-08-11 (c)).
    """
    bodies = [b for b in (getattr(window, "episode_bodies", None) or []) if isinstance(b, dict)]
    intents: Counter = Counter()
    actions: Counter = Counter()
    failure_kinds: Counter = Counter()
    attempt_domains: Counter = Counter()
    failed_episodes: List[str] = []
    unrecorded_episodes: List[str] = []
    completed = 0
    verified = 0
    unverified = 0
    repeated_failures = 0

    for body in bodies:
        episode_id = _clip(body.get("episode_id"), 64)
        # WO-11: an ABSENT status was defaulted to "completed", so an episode that recorded
        # nothing was counted as one that finished — a completion nobody observed. An
        # unrecorded or unknown status is unknown, and unknown is not a failure kind.
        status = (_clip(body.get("cascade_status"), 24) or "").lower()
        if status == "completed":
            completed += 1
        elif not status or status == "unknown":
            unrecorded_episodes.append(episode_id)
        else:
            failed_episodes.append(episode_id)
            failure_kinds[status] += 1
        intent = _clip(body.get("intent"), 40)
        if intent:
            intents[intent] += 1
        action = _clip(body.get("action"), 40)
        if action:
            actions[action] += 1
        attempt = body.get("motor_attempt_assessment")
        if not isinstance(attempt, dict):
            continue
        if _clip(attempt.get("verification_verdict"), 24) == "verified":
            verified += 1
        else:
            unverified += 1
        domain = _clip(attempt.get("domain"), 40)
        if domain:
            attempt_domains[domain] += 1
        if attempt.get("repeated_failure"):
            repeated_failures += 1
        evidence = attempt.get("execution_evidence")
        if isinstance(evidence, dict):
            kind = _clip(evidence.get("failure_kind"), 40)
            if kind:
                failure_kinds[kind] += 1

    return {
        "total": len(bodies),
        "completed": completed,
        "failed_episodes": failed_episodes,
        "unrecorded_episodes": unrecorded_episodes,
        "failure_kinds": failure_kinds,
        "intents": intents,
        "actions": actions,
        "attempt_domains": attempt_domains,
        "verified": verified,
        "unverified": unverified,
        "repeated_failures": repeated_failures,
    }


def _recurrences(counts: Counter, total: int, label: str) -> List[str]:
    """Render only what RECURRED, strongest first. One occurrence is not a pattern."""
    rows = [(value, name) for name, value in counts.items() if value >= 2]
    rows.sort(key=lambda row: (-row[0], row[1]))
    return [
        f"{label} {name!r} in {value} of {total} episodes"
        for value, name in rows[:_DERIVED_OBSERVATION_LIMIT]
    ]


def deterministic_window_verdict(
    window: Any,
    *,
    view: Optional[Dict[str, Any]] = None,
    reason: str = "",
) -> Dict[str, Any]:
    """The verdict the window produces when the model pass could not (ruling 2c).

    Derived, never fabricated: every clause is a count taken off the ten closed
    episodes' own recorded outcomes. It reports what happened and deliberately
    names NO trajectory — ``trajectory_update`` is always empty, which the
    trajectory phase already reads as "this window changed nothing".

    That emptiness is the honest choice and it is load-bearing. Carrying the
    window's existing target forward would reinforce it (``reinforcement_count``
    +1, ``passes_since_reinforcement`` reset), so a runtime that is down for a
    week would hold a stale trajectory alive by decay-suppression alone, on
    evidence no mind ever weighed. Naming a NEW target would be the fabrication
    the whole fail-hard doctrine exists to prevent. So the deterministic verdict
    says what it knows and stops.

    ``reflection.summary`` states its own authorship in words, because this text
    is what lands in the synthesis memory — a reader of that memory must not
    have to cross-reference the archive to learn no model wrote it.
    """
    resolved_view = view if view is not None else build_window_view(window)
    facts = _window_evidence(window)
    total = int(facts["total"])
    completed = int(facts["completed"])
    failed = len(facts["failed_episodes"])
    unrecorded = len(facts["unrecorded_episodes"])

    if total:
        outcome_clause = f"{completed} of {total} episodes completed"
        if failed:
            kinds = ", ".join(
                f"{name}×{count}"
                for name, count in facts["failure_kinds"].most_common(_DERIVED_OBSERVATION_LIMIT)
            )
            outcome_clause += f"; {failed} did not" + (f" ({kinds})" if kinds else "")
        if unrecorded:
            # Said separately, because "did not complete" is a different claim from "nobody
            # recorded whether it completed".
            outcome_clause += f"; {unrecorded} did not record a cascade status"
    else:
        outcome_clause = "the window carried no episode bodies"

    attempts = int(facts["verified"]) + int(facts["unverified"])
    if attempts:
        motor_clause = f"{facts['verified']} of {attempts} motor attempts verified"
        if facts["repeated_failures"]:
            motor_clause += f", {facts['repeated_failures']} repeating a known failure"
    else:
        motor_clause = "no motor attempts were made"

    summary = (
        "Deterministic window verdict — no model reflection was produced, so this "
        "is counted from the episodes' own recorded outcomes: "
        f"{outcome_clause}; {motor_clause}."
    )
    what = (
        "; ".join(
            f"{name}×{count}"
            for name, count in facts["intents"].most_common(_DERIVED_OBSERVATION_LIMIT)
        )
        or "no intent was recorded"
    )
    actions = (
        "; ".join(
            f"{name}×{count}"
            for name, count in facts["actions"].most_common(_DERIVED_OBSERVATION_LIMIT)
        )
        or "no action was recorded"
    )

    return {
        "status": "reflected",
        "window_id": resolved_view["window_id"],
        "episode_count": resolved_view["episode_count"],
        "reflection_source": REFLECTION_SOURCE_DETERMINISTIC,
        "reflection_fallback_reason": str(reason or "")[:300],
        "reflection": {
            "summary": summary,
            "reasoning": {
                "what": f"intents: {what}. actions: {actions}.",
                "why": f"{outcome_clause}; {motor_clause}.",
                # Never derivable. A trait is a judgement about the self, and a
                # counter has not made one.
                "identity": "",
            },
            "emergent_observations": {
                "repeated_behaviors": (
                    _recurrences(facts["intents"], total, "intent")
                    + _recurrences(facts["actions"], total, "action")
                )[:_DERIVED_OBSERVATION_LIMIT],
                "stable_patterns": (
                    _recurrences(facts["failure_kinds"], total, "failure kind")
                    + _recurrences(facts["attempt_domains"], total, "motor domain")
                )[:_DERIVED_OBSERVATION_LIMIT],
            },
            "trajectory": {
                "decision": "",
                "target_domain": "",
                "target_capability": "",
                "reason": (
                    "no trajectory change: recorded outcomes carry no basis for naming "
                    "a target, and a deterministic pass must not guess one"
                ),
            },
        },
        "trajectory_update": {},
    }


def emit_window_fallback(view: Dict[str, Any], reason: str) -> None:
    """Loud telemetry for the fallback. A deterministic window is never silent."""
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "omega",
            "window_reflection_fallback",
            episode_id=view["window_id"],
            status="degraded",
            ok=False,
            summary=(
                f"window {view['window_id']} folded DETERMINISTICALLY over "
                f"{view['episode_count']} episodes: {reason[:200]}"
            ),
            reflection_source=REFLECTION_SOURCE_DETERMINISTIC,
            reason=str(reason)[:400],
        )
    except Exception:
        pass
