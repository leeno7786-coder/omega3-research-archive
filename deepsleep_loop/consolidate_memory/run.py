"""DeepSleep Phase 7 — promote durable products into memory.api."""

from __future__ import annotations

import logging
from typing import Any

from memory.api import CanonicalEnvelope, WriteReceipt, intake_memory, intake_receipt_accepted

log = logging.getLogger("omega.deepsleep_loop.consolidate_memory")

_SOURCE = "deepsleep.consolidate_memory"
_RESOLVED_STATUSES = frozenset({"resolved", "abandoned", "superseded", "archived"})


def _parent_capability_for(domain: str) -> str:
    """The parent capability a learned subtopic lives UNDER in the mastery tree — the
    mind's broad trajectory.target_capability, but ONLY when the ingested topic IS the
    mind's chosen child (trajectory.target_domain). So a mind-decomposed child ("async
    python") links under its parent ("python coding"); an off-trajectory learn stays
    top-level. Failure-soft -> "" (top-level)."""
    try:
        from pressure_loop.temporal_store import read_trajectory
        from deepsleep_loop.mastery_store import normalize_domain_id

        traj = read_trajectory() or {}
        child = normalize_domain_id(str(traj.get("target_domain") or ""))
        if child and child == normalize_domain_id(str(domain or "")):
            return str(traj.get("target_capability") or "").strip()
    except Exception:
        pass
    return ""


def _knowledge_coord(domain: str) -> list[int]:
    """Resolve a learned domain to its storage coord ``[lobe, Y, 0]`` — ROUTED to the
    proper lobe so a learned CODING topic lands in coding(8), a knowledge topic in
    knowledge(2). Code markers -> coding(8); otherwise knowledge(2) (deterministic; the
    classifier is RETIRED). Both lobes are dynamic-Y when onboarding flagged [2,8].

    When the routed lobe is in the dynamic-Y feature flag, the storage subdomain is
    allocated by the SAME allocator the gate write path and recall resolve through
    (:func:`memory.subdomain_allocator.get_or_create_subdomain`) — and creating the
    subdomain auto-registers its mastery topic — so DeepSleep consolidation writes where
    recall reads and the learned subcat becomes a mastery domain. Fail-HARD on a
    flagged-lobe allocation failure (a silent static fallback would mis-route + break
    recall). Flag-OFF (``dynamic_y_lobes() == frozenset()``) keeps the static
    ``SUBDOMAIN_MAP`` lobe-2 reverse-lookup byte-for-byte unchanged.
    """
    from memory.subdomain_flag import dynamic_y_lobes

    flag = dynamic_y_lobes()
    lobe = 2
    try:
        from memory.routing.code_workflow_disambiguation import suggest_code_workflow_lobe

        if suggest_code_workflow_lobe(domain) == 8:
            lobe = 8
    except Exception:
        lobe = 2

    if lobe in flag:
        from memory.subdomain_allocator import get_or_create_subdomain

        y, _dirname = get_or_create_subdomain(
            lobe=lobe,
            seed_text=domain,
            title=domain,
            dynamic_y_lobes=flag,
            parent=_parent_capability_for(domain),
        )
        return [lobe, y, 0]

    from memory.dewey_map import SUBDOMAIN_MAP

    rev = {name: y for (x, y), name in SUBDOMAIN_MAP.items() if x == 2}
    key = (domain or "").strip().lower().replace(" ", "_")
    y = rev.get(key, 24)
    return [2, y, 0]


def _render_journal_text(journal_entry: dict) -> str:
    parts: list[str] = []
    intent = journal_entry.get("intent")
    if intent:
        parts.append(f"intent: {intent}")
    action = journal_entry.get("action")
    if action:
        parts.append(f"action: {action}")
    outcome = journal_entry.get("outcome")
    if outcome:
        parts.append(f"outcome: {outcome}")
    omega_reasoning = journal_entry.get("omega_reasoning")
    if omega_reasoning:
        parts.append(f"omega_reasoning: {omega_reasoning}")
    return "; ".join(parts) if parts else ""


def _journal_envelope(episode_id: str, journal_entry: dict) -> CanonicalEnvelope | None:
    if not isinstance(journal_entry, dict):
        return None
    text = _render_journal_text(journal_entry)
    if not text:
        return None
    return CanonicalEnvelope(
        cycle_id=episode_id,
        coord=[6, 0, 0],
        type="consolidation_journal",
        payload={
            "text": text,
            "title": f"DeepSleep journal — {episode_id}",
            "summary": f"consolidation_journal (episode {episode_id})",
            "subtopic": "consolidation_journal",
            "tags": ["deepsleep", "journal"],
            # The rendered text is a recall preview; canonical enrichment keeps the
            # complete producer envelopes even when the preview exceeds its text budget.
            "enrichment": {"consolidation_journal": dict(journal_entry)},
        },
        source=_SOURCE,
        confidence=0.8,
    )


def _window_synthesis_envelope(
    episode_id: str,
    window_reflection: dict,
) -> CanonicalEnvelope | None:
    """The ONE memory a window fold writes that no episode in it could have.

    Operator ruling 2026-08-11 (risk 7): a window terminus does NOT re-run
    mastery/failure/semantic/identity/query_lifecycle, because each of the ten
    episodes already consolidated its own evidence. What survives that cut is
    what the window alone produced — the reflection across the ten — and it is a
    real write, so it lands here, through the same intake and therefore the same
    serialized commit region and finish-line stamp (cut 13) as every other
    DeepSleep product.

    Written explicitly rather than left to ``_journal_envelope``: the journal
    text renders ``omega_reasoning`` only, so with identity skipped the
    reflection's summary and its emergent observations — the parts that are
    genuinely about the WINDOW — would land nowhere at all.

    ``reflection_source`` rides the text and the tags because a reader of this
    memory must be able to tell a model reflection from a deterministic verdict
    (ruling 2 (d)) without holding the archive open beside it.
    """
    reflection = dict((window_reflection or {}).get("reflection") or {})
    if not reflection:
        return None
    source = str((window_reflection or {}).get("reflection_source") or "").strip() or "unknown"
    window_id = str((window_reflection or {}).get("window_id") or episode_id)
    reasoning = dict(reflection.get("reasoning") or {})
    emergent = dict(reflection.get("emergent_observations") or {})
    trajectory = dict(reflection.get("trajectory") or {})

    parts: list[str] = []
    summary = str(reflection.get("summary") or "").strip()
    if summary:
        parts.append(summary)
    for key in ("what", "why", "identity"):
        value = str(reasoning.get(key) or "").strip()
        if value:
            parts.append(f"{key}: {value}")
    for key in ("repeated_behaviors", "stable_patterns"):
        rows = [str(r).strip() for r in (emergent.get(key) or []) if str(r or "").strip()]
        if rows:
            parts.append(f"{key}: " + "; ".join(rows))
    decision = str(trajectory.get("decision") or "").strip()
    reason = str(trajectory.get("reason") or "").strip()
    if decision or reason:
        parts.append(f"trajectory: {decision or 'no change'}" + (f" — {reason}" if reason else ""))
    if not parts:
        return None

    from memory.cognition_write import cognition_envelope

    # STRUCTURAL PROVENANCE (integration spec section 7, T10). A reflection's lesson, pattern
    # or revised belief is Omega's own derived conclusion about its own history: this write
    # site assigns the actor ``reflection``, and the boundary requires it to name what it
    # was derived from — the archived window the ten episodes were reflected over.
    return cognition_envelope(
        actor="reflection",
        cycle_id=episode_id,
        # (6, 0) is "deepsleep" — the same shelf the consolidation journal lives
        # on, because this is a consolidation product about the mind's own run.
        coord=[6, 0, 0],
        type="window_synthesis",
        payload={
            "text": f"[{source} reflection over {window_id}] " + " | ".join(parts),
            "title": f"Window reflection — {window_id}",
            "summary": f"window_synthesis ({source}, window {window_id})",
            "subtopic": "window_synthesis",
            "tags": ["deepsleep", "window", "reflection", source],
        },
        confidence=0.8,
        derived_from=[f"window:{window_id}", f"episode:{episode_id}"],
    )


def _identity_envelopes(
    episode_id: str,
    identity_effects: dict,
) -> list[CanonicalEnvelope]:
    # Emergent traits are durably owned by canonical identity memory (0/2) via
    # deepsleep_loop.identity_store. The journal still carries identity deltas,
    # but Phase 7 must not mirror them into 0/3 and create a second store.
    return []


def _mastery_envelopes(
    episode_id: str,
    mastery_effects: dict,
) -> list[CanonicalEnvelope]:
    envelopes: list[CanonicalEnvelope] = []
    for _domain_id, record in (mastery_effects or {}).items():
        if not isinstance(record, dict):
            continue
        domain_name = str(record.get("domain_name") or _domain_id)
        knowledge = float(record.get("knowledge_mastery", 0.0) or 0.0)
        application = float(record.get("application_mastery", 0.0) or 0.0)
        status = str(record.get("status") or "learning")
        application_delta = float(record.get("application_delta", 0.0) or 0.0)
        coord = _knowledge_coord(domain_name)
        envelopes.append(
            CanonicalEnvelope(
                cycle_id=episode_id,
                coord=coord,
                type="mastery_progress",
                payload={
                    "text": (
                        f"Mastery of {domain_name}: knowledge {knowledge:.2f}, "
                        f"application {application:.2f} ({status})."
                    ),
                    "title": f"Mastery — {domain_name}",
                    "summary": f"{domain_name} mastery_progress (episode {episode_id})",
                    "subtopic": "mastery_progress",
                    "tags": ["deepsleep", "mastery", domain_name],
                    "domain": domain_name,
                },
                source=_SOURCE,
                confidence=0.8,
            )
        )
        if application_delta > 0:
            envelopes.append(
                CanonicalEnvelope(
                    cycle_id=episode_id,
                    coord=[5, 0, 0],
                    type="mastery_progress",
                    payload={
                        "text": (
                            f"Application mastery of {domain_name}: {application:.2f} ({status})."
                        ),
                        "title": f"Application — {domain_name}",
                        "summary": (f"{domain_name} application mastery (episode {episode_id})"),
                        "subtopic": "mastery_progress",
                        "tags": ["deepsleep", "mastery", "application", domain_name],
                        "domain": domain_name,
                    },
                    source=_SOURCE,
                    confidence=0.8,
                )
            )
    return envelopes


def _semantic_envelopes(
    episode_id: str,
    semantic_effects: dict,
) -> list[CanonicalEnvelope]:
    envelopes: list[CanonicalEnvelope] = []
    for _key, record in (semantic_effects or {}).items():
        if not isinstance(record, dict):
            continue
        domain = str(record.get("domain") or _key)
        statement = str(record.get("statement") or "").strip()
        if not statement:
            continue
        envelopes.append(
            CanonicalEnvelope(
                cycle_id=episode_id,
                coord=_knowledge_coord(domain),
                type="semantic_fact",
                payload={
                    "text": statement,
                    "title": f"Semantic — {domain}",
                    "summary": f"{domain} semantic_fact (episode {episode_id})",
                    "subtopic": "semantic_fact",
                    "tags": ["deepsleep", "semantic", domain],
                    "domain": domain,
                },
                source=_SOURCE,
                confidence=0.8,
            )
        )
    return envelopes


def _resolved_query_envelopes(
    episode_id: str,
    query_effects: list,
) -> list[CanonicalEnvelope]:
    envelopes: list[CanonicalEnvelope] = []
    for record in query_effects or []:
        if not isinstance(record, dict):
            continue
        if record.get("action") != "updated":
            continue
        status = str(record.get("status") or "")
        if status not in _RESOLVED_STATUSES:
            continue
        question = str(record.get("question") or "").strip()
        domain = str(record.get("domain") or "").strip()
        envelopes.append(
            CanonicalEnvelope(
                cycle_id=episode_id,
                coord=[3, 1, 0],
                type="resolved_inquiry",
                payload={
                    "text": f"{question} [{status}]" if question else f"[{status}]",
                    "title": f"Resolved inquiry — {domain or 'general'}",
                    "summary": f"resolved_inquiry {status} (episode {episode_id})",
                    "subtopic": "resolved_inquiry",
                    "tags": ["deepsleep", "query", status],
                    "domain": domain,
                },
                source=_SOURCE,
                confidence=0.8,
            )
        )
    return envelopes


def _build_envelopes(
    episode_id: str,
    journal_entry: dict,
    *,
    mastery_effects: dict | None = None,
    identity_effects: dict | None = None,
    semantic_effects: dict | None = None,
    query_effects: list | None = None,
    window_reflection: dict | None = None,
) -> list[CanonicalEnvelope]:
    envelopes: list[CanonicalEnvelope] = []
    journal_env = _journal_envelope(episode_id, journal_entry)
    if journal_env is not None:
        envelopes.append(journal_env)
    if window_reflection:
        synthesis_env = _window_synthesis_envelope(episode_id, window_reflection)
        if synthesis_env is not None:
            envelopes.append(synthesis_env)
    envelopes.extend(_identity_envelopes(episode_id, identity_effects or {}))
    envelopes.extend(_mastery_envelopes(episode_id, mastery_effects or {}))
    envelopes.extend(_semantic_envelopes(episode_id, semantic_effects or {}))
    envelopes.extend(_resolved_query_envelopes(episode_id, query_effects or []))
    return envelopes


def _write_envelope(envelope: CanonicalEnvelope) -> WriteReceipt:
    """Auto-commit a consolidation product into memory (the continuity substrate).

    Fail-hard: a write into memory is a continuity write — a failure propagates
    and CRASHES rather than being swallowed to None and counted as 'failed'."""
    return intake_memory(envelope, propose=False, bypass_guardian=True)


def consolidate_to_memory(
    episode_id: str,
    journal_entry: dict,
    *,
    mastery_effects: dict | None = None,
    identity_effects: dict | None = None,
    semantic_effects: dict | None = None,
    query_effects: list | None = None,
    window_reflection: dict | None = None,
    enabled: bool | None = None,
) -> dict:
    """Phase 7 — promote durable products into memory.api. Failure-soft.

    ``window_reflection`` is passed ONLY by the window-scope terminus and carries
    the reflection across ten episodes; see ``_window_synthesis_envelope``.
    """
    if enabled is None:
        from pressure_loop.policy import deepsleep_memory_consolidation_enabled

        enabled = deepsleep_memory_consolidation_enabled()

    if not enabled:
        return {"written": [], "failed": [], "enabled": False}

    if not episode_id or not str(episode_id).strip():
        return {"written": [], "failed": [], "enabled": True}

    written: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for envelope in _build_envelopes(
        episode_id,
        journal_entry,
        mastery_effects=mastery_effects,
        identity_effects=identity_effects,
        semantic_effects=semantic_effects,
        query_effects=query_effects,
        window_reflection=window_reflection,
    ):
        # Fail-hard boundary: _write_envelope CRASHES if write_memory THROWS (a
        # continuity write failure). A returned receipt outside the live intake
        # lane is memory.api's own validation VERDICT (e.g. a
        # rejected/deduped product) — a soft outcome the mind records as failed,
        # not a crash.
        receipt = _write_envelope(envelope)
        entry = {
            "coord": list(envelope.coord),
            "type": envelope.type,
            "cycle_id": envelope.cycle_id,
        }
        if intake_receipt_accepted(receipt):
            written.append(entry)
        else:
            failed.append(entry)

    result = {"written": written, "failed": failed, "enabled": True}
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "deepsleep",
            "consolidate_to_memory",
            episode_id=episode_id,
            summary=f"written={len(written)} failed={len(failed)}",
            written=written,
            failed=failed,
            enabled=True,
        )
    except Exception:
        pass
    return result
