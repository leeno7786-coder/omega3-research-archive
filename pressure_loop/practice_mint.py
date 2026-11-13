"""Autonomous practice — mint dispatchable objectives so application mastery is REAL.

Operator directive 2026-07-08 ("making application and application mastery
real"): until now a learned-but-unapplied domain could only WANT practice —
the execution_gap pressure fired, USE_SKILL dispatched, and skill selection
died with no objective (missing_skill_pack), so application_mastery could
never rise: the unclosable-gap loop the 2026-07-08 gate now suppresses.

This module closes the want→work seam DETERMINISTICALLY (v2 doctrine: the
system decides from grounded state; the model comprehends and executes —
never prose-triggered):

  * ``ensure_practice_objective()`` — when no objective exists in episode SRAM and a domain
    is practice-ready (knowledge >= target, application < target, own-warm
    substrate — the exact read_execution_gap conditions), mint a bounded
    template ``BuildObjective`` in SRAM with ``origin_opened_by="omega_practice"``.
    From that instant the execution report machinery carries it unchanged:
    the execution_gap gate sees a dispatchable objective and
    lifts, USE_SKILL dispatches the CLI seat into the jailed workspace, the
    deterministic battery verifies, stall/gate caps bound failure, and the
    workspace port files the artifact under <completed_root>/_practice/.

  * ``apply_practice_success(domain_id, episode_id=...)`` — the grounding: a VERIFIED-green
    practice build writes an application_mastery delta on the practiced
    domain (proven by battery, never claimed). Called from the completion
    site when the finished build's origin is omega_practice.

Kill switch: OMEGA_PRACTICE_ENABLED=0 disables minting entirely (the gate
then keeps autonomous application suppressed, exactly the pre-slice state).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger("omega.pressure.practice_mint")

# One verified practice build's worth of application growth. Three or four
# proven builds reach the 0.7 default target — application is EARNED slowly,
# mirroring how knowledge mastery accrues over multiple LEARN episodes.
PRACTICE_APPLICATION_DELTA = 0.2

# Mind-driven APPLY bar (operator + Omega, 2026-07-08: "make it where it can
# actually execute using a skill pack and an actual build command"): when the
# mind PREPARES an APPLY, it may practice a domain it has only PART-learned —
# knowledge >= this bar instead of the full autonomous-mint bar (knowledge >=
# target). Practice-by-doing is legitimate pedagogy; the battery still proves
# the outcome before any mastery is granted. Below this bar there is nothing
# real to practice and the APPLY downgrades honestly.
# TUNED 0.3 -> 0.5 (operator 2026-07-08, first live mint fired at 0.40 after
# only ~5 LEARNs: "we dont want it moving too fast. we need to find a happy
# median for learning and application") — half-learned before hands-on.
MIND_DRIVEN_KNOWLEDGE_BAR = 0.5


def _enabled() -> bool:
    return os.environ.get("OMEGA_PRACTICE_ENABLED", "1").strip() != "0"


# ── The mind's APPLY choice (idle awakening, 2026-07-10) ─────────────────────
# When the awakening decides EXECUTE it names a domain + a SPECIFIC topic.
# Stash it one-shot (mirror of learn_topic_choice) so the mint that backs the
# USE_SKILL dispatch practices what the MIND chose — not just the best-known
# ready domain. A stale choice self-clears on the next set.
_pending_apply: Optional[dict] = None


def set_apply_choice(domain: str, topic: str = "") -> None:
    global _pending_apply
    d = str(domain or "").strip()
    if not d:
        return
    _pending_apply = {"domain": d, "topic": str(topic or "").strip()}


def take_apply_choice() -> Optional[dict]:
    global _pending_apply
    v, _pending_apply = _pending_apply, None
    return v


def is_apply_ready(domain_id: str) -> bool:
    """Whether ONE domain meets the mind-driven practice bar: part-learned
    (knowledge >= MIND_DRIVEN_KNOWLEDGE_BAR), unapplied (application < target),
    own-warm substrate. The SAME conditions mint_for_prepared_apply enforces —
    the awakening checks this at decision time so an EXECUTE it cannot back
    never records dead pressure."""
    clean = str(domain_id or "").strip()
    if not clean:
        return False
    try:
        from deepsleep_loop.mastery_store import load_mastery
        from memory.symphony_strength import hot_count_for_domain
        from pressure_loop.pressure_field import EXECUTION_GAP_MIN_HOT

        record = load_mastery().get(clean)
        if record is None:
            return False
        return (
            float(record.knowledge_mastery) >= MIND_DRIVEN_KNOWLEDGE_BAR
            and float(record.application_mastery) < float(record.target_mastery)
            and int(hot_count_for_domain(clean)) >= EXECUTION_GAP_MIN_HOT
        )
    except Exception:
        log.debug("is_apply_ready: failure-soft False", exc_info=True)
        return False


def practice_ready_domain() -> Optional[Any]:
    """The top practice-ready MasteryRecord, or None.

    Practice-ready = learned (knowledge >= target) + unapplied
    (application < target) + own-warm substrate — computed with the SAME
    helpers read_execution_gap uses, so the mint and the pressure can
    never disagree about readiness. Deterministic tie-break: lowest
    application first (neediest domain practices first), then domain_id.
    """
    try:
        from deepsleep_loop.mastery_store import load_mastery
        from memory.symphony_strength import hot_count_for_domain
        from pressure_loop.pressure_field import (
            EXECUTION_GAP_MIN_HOT,
            _application_gap_domains,
        )

        gap_ids = set(_application_gap_domains())
        if not gap_ids:
            return None
        records = load_mastery()
        ready = [
            r
            for domain_id, r in sorted(records.items())
            if domain_id in gap_ids
            and int(hot_count_for_domain(domain_id)) >= EXECUTION_GAP_MIN_HOT
        ]
        if not ready:
            return None
        ready.sort(key=lambda r: (float(r.application_mastery), str(r.domain_id)))
        return ready[0]
    except Exception:
        log.exception("practice-ready domain lookup failed")
        raise


def mint_objective(domain_name: str, domain_id: str = "", focus_topic: str = "") -> str:
    """Deterministic practice work order for one domain. Bounded and
    self-verifying by construction — the battery needs tests to prove.

    ``focus_topic`` (2026-07-10): the mind's SPECIFIC chosen aspect from the
    idle awakening — the build practices THAT, not a generic domain survey.

    EVOLVE LOOP (operator 2026-07-08): fold this domain's recent build
    FAILURES into the order so Omega writes a better prompt than last
    time — the mind learns from what it did, not just what it read."""
    name = str(domain_name or "").strip()
    focus = str(focus_topic or "").strip()
    subject = f"{name} — specifically {focus}" if focus and focus.lower() != name.lower() else name
    # Project identity lives in the objective's FIRST 80 chars (the dev-lobe store names
    # each build's permanent record by objective slug, operator 2026-07-10). A focus topic
    # differentiates right here in the subject; a topic-LESS mint would otherwise collide
    # every generic "{name}" practice into one franken-record forever — stamp it so each
    # session's build is its own remembered artifact.
    lead = f"Practice project: {subject}."
    if not focus:
        from datetime import datetime

        lead = f"Practice project: {subject} [{datetime.now().strftime('%Y-%m-%d %H%M')}]."
    # Test-first, ONE sentence (2026-07-10): source-first builds yielded for an
    # hour without reaching tests; but the first paragraph-length phrasing of
    # this diluted the directive and the executor regressed to narrating.
    base = (
        f"{lead} Build a SMALL, self-contained Python "
        f"project in the workspace that demonstrates {subject} in working code "
        "— one focused example, not a survey. Write tests/test_*.py FIRST, "
        "then the source that makes it pass; run pytest and show it passing. "
        "Keep it under ~150 lines of source; finish inside this session."
    )
    try:
        from memory.execution_experience import failure_guidance_for_domain

        guidance = failure_guidance_for_domain(str(domain_id or domain_name))
        if guidance:
            return f"{base}\n\n{guidance}"
    except Exception:
        pass
    return base


def mint_for_prepared_apply() -> bool:
    """Mind-driven mint: the mind prepared an APPLY and SRAM has no objective.

    Relaxed readiness — knowledge >= MIND_DRIVEN_KNOWLEDGE_BAR (part-learned
    is practiceable), application < target, own-warm substrate. Holds one
    bounded objective exactly like the autonomous mint so USE_SKILL
    dispatches a real skill pack. True when an objective was held."""
    if not _enabled():
        return False
    try:
        from memory import episode_sram
        from memory.build_objective import (
            BuildObjective,
            current_build_objective,
            hold_build_objective,
        )

        if current_build_objective() is not None:
            return True  # already dispatchable — the APPLY rides what exists
        from deepsleep_loop.mastery_store import load_mastery
        from memory.symphony_strength import hot_count_for_domain
        from pressure_loop.pressure_field import EXECUTION_GAP_MIN_HOT

        records = load_mastery()
        ready = [
            r
            for domain_id, r in sorted(records.items())
            if float(r.knowledge_mastery) >= MIND_DRIVEN_KNOWLEDGE_BAR
            and float(r.application_mastery) < float(r.target_mastery)
            and int(hot_count_for_domain(domain_id)) >= EXECUTION_GAP_MIN_HOT
        ]
        if not ready:
            return False
        ready.sort(key=lambda r: (-float(r.knowledge_mastery), str(r.domain_id)))
        record = ready[0]  # best-known domain: practice what you know most
        # Fresh objective -> fresh sandbox (operator 2026-07-10): leftovers from
        # a build that never hit a deterministic ending port to _incomplete/
        # so the executor never starts inside a previous project's files.
        try:
            from core.workspace_port import sweep_workspace_for_fresh_build

            sweep_workspace_for_fresh_build(reason="mind-driven practice mint")
        except Exception:
            log.exception("pre-mint workspace sweep failed (non-fatal)")
        focus_topic = ""
        choice = take_apply_choice()
        if choice:
            # The mind's own EXECUTE choice (idle awakening) wins over the
            # best-known default WHEN its domain is in the ready set — the
            # mint backs what the mind decided, with its specific topic.
            from deepsleep_loop.mastery_store import normalize_domain_id

            chosen_id = normalize_domain_id(choice["domain"])
            chosen = next((r for r in ready if str(r.domain_id) == chosen_id), None)
            if chosen is not None:
                record = chosen
                focus_topic = str(choice.get("topic") or "")
        objective = mint_objective(record.domain_name, record.domain_id, focus_topic)
        hold_build_objective(
            BuildObjective(
                episode_id=str(episode_sram.current_episode_id() or ""),
                objective=objective,
                origin_opened_by="omega_practice",
                practice_domain=str(record.domain_id),
            )
        )
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "practice_objective_minted",
                status="ok",
                ok=True,
                summary=f"mind-driven APPLY minted: {record.domain_name[:70]}",
                domain=str(record.domain_id),
                mind_driven=True,
            )
        except Exception:
            pass
        log.info("mind-driven practice minted for domain %s", record.domain_id)
        return True
    except Exception:
        log.exception("mind-driven practice mint failed")
        raise


def ensure_practice_objective() -> bool:
    """Mint one episode-SRAM practice objective when the seat is idle and ready."""
    if not _enabled():
        return False
    try:
        from memory import episode_sram
        from memory.build_objective import (
            BuildObjective,
            current_build_objective,
            hold_build_objective,
        )

        if current_build_objective() is not None:
            return False  # one build at a time — never preempt real work
        record = practice_ready_domain()
        if record is None:
            return False
        # Fresh objective -> fresh sandbox (operator 2026-07-10; see the
        # mind-driven mint above for the why).
        try:
            from core.workspace_port import sweep_workspace_for_fresh_build

            sweep_workspace_for_fresh_build(reason="autonomous practice mint")
        except Exception:
            log.exception("pre-mint workspace sweep failed (non-fatal)")
        objective = mint_objective(record.domain_name, record.domain_id)
        hold_build_objective(
            BuildObjective(
                episode_id=str(episode_sram.current_episode_id() or ""),
                objective=objective,
                origin_opened_by="omega_practice",
                practice_domain=str(record.domain_id),
            )
        )
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "practice_objective_minted",
                status="ok",
                ok=True,
                summary=f"practice minted: {record.domain_name[:80]}",
                domain=str(record.domain_id),
            )
        except Exception:
            pass
        log.info("practice objective minted for domain %s", record.domain_id)
        return True
    except Exception:
        log.exception("practice mint failed")
        raise


def apply_practice_success(domain_id: str, *, episode_id: str) -> float:
    """Ground a battery-verified practice build into application_mastery.

    Returns the new application_mastery. Unknown or empty domains fail with the
    cause intact; a successful build can never be reported while its grounding
    was silently discarded. Bounded to [0, 1]; evidence_count increments."""
    clean = str(domain_id or "").strip()
    if not clean:
        raise ValueError("practice success domain is required")
    clean_episode = str(episode_id or "").strip()
    if not clean_episode:
        raise ValueError("practice success episode_id is required")
    import dataclasses

    from deepsleep_loop.mastery_store import load_mastery, upsert_mastery
    from deepsleep_loop.store_locks import MASTERY, store_transaction

    # Phase 4 cut 13 review: ONE transaction. Practice grounding runs in the
    # LANE-LOCAL half of the deepsleep terminus, so two lanes can be inside this
    # function at once — and the increment (and the once-per-episode guard on
    # last_practice_episode) are computed from the record just read. Split
    # across two acquisitions, the second write erases the first lane's
    # application credit with nothing to show for it.
    with store_transaction(MASTERY):
        records = load_mastery()
        record = records.get(clean)
        if record is None:
            raise KeyError(f"practice success domain is not registered: {clean}")
        if record.last_practice_episode == clean_episode:
            return float(record.application_mastery)
        new_val = min(1.0, float(record.application_mastery) + PRACTICE_APPLICATION_DELTA)
        updated = dataclasses.replace(
            record,
            application_mastery=new_val,
            evidence_count=int(record.evidence_count) + 1,
            last_practice_episode=clean_episode,
        )
        upsert_mastery(updated)
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "application_mastery_grounded",
            status="ok",
            ok=True,
            summary=f"{clean}: application -> {new_val:.2f}",
            domain=clean,
            application_mastery=new_val,
        )
    except Exception:
        log.exception("application mastery grounding telemetry failed after persistence")
    return new_val
