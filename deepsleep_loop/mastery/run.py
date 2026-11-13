"""DeepSleep Phase 3 — mastery update from episode outcome."""

from __future__ import annotations

import logging
from typing import Any

from deepsleep_loop._deepsleep_state import DeepSleepInput, MasteryRecord
from deepsleep_loop.effect_receipts import effect_id, receipts_for, with_receipt
from deepsleep_loop.mastery_store import (
    load_mastery,
    normalize_domain_id,
    status_for,
    upsert_mastery,
)
from deepsleep_loop.store_locks import MASTERY, store_transaction


log = logging.getLogger("omega.deepsleep.mastery")
_MOTOR_EVIDENCE_INCREMENT = 0.05


def _entry_dict(entry: Any) -> dict[str, Any]:
    return dict(entry) if isinstance(entry, dict) else {}


def _episode_target(entry: Any, *, fallback: str = "") -> str:
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


def _episode_success_entry(entry: Any) -> bool:
    from deepsleep_loop._deepsleep_state import _episode_entry_success

    return _episode_entry_success(entry)


def _episode_success(ds_input: DeepSleepInput) -> bool:
    window = ds_input.episode_window or []
    if window:
        return any(_episode_success_entry(entry) for entry in window)
    global_outcome = ds_input.global_outcome or {}
    outcome = global_outcome.get("outcome")
    if isinstance(outcome, dict) and outcome.get("goal_state") == "completed":
        return True
    auto = ds_input.auto_outcome or {}
    if auto.get("status") == "success":
        return True
    if auto.get("cycle_kind") == "success":
        return True
    return False


def _target_mastery(action_goal: dict[str, Any]) -> float:
    from pressure_loop.policy import deepsleep_mastery_default_target

    metadata = action_goal.get("metadata")
    if isinstance(metadata, dict):
        raw = metadata.get("target_mastery")
        if raw is not None:
            try:
                return max(0.0, min(1.0, float(raw)))
            except (TypeError, ValueError):
                pass
    raw = action_goal.get("target_mastery")
    if raw is not None:
        try:
            return max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            pass
    return deepsleep_mastery_default_target()


def mastery_effects_from_records(
    records: list[MasteryRecord],
    *,
    prior: dict[str, MasteryRecord] | None,
) -> dict[str, dict[str, float]]:
    """Build journal mastery_effects deltas from updated records."""
    prior = prior or {}
    effects: dict[str, dict[str, float]] = {}
    for record in records:
        old = prior.get(record.domain_id)
        prior_knowledge = float(old.knowledge_mastery) if old else 0.0
        prior_application = float(old.application_mastery) if old else 0.0
        knowledge_delta = float(record.knowledge_mastery) - prior_knowledge
        application_delta = float(record.application_mastery) - prior_application
        if knowledge_delta == 0.0 and application_delta == 0.0:
            continue
        effects[record.domain_id] = {
            "domain_name": record.domain_name,
            "knowledge_delta": knowledge_delta,
            "application_delta": application_delta,
            "knowledge_mastery": float(record.knowledge_mastery),
            "application_mastery": float(record.application_mastery),
            "status": record.status,
        }
    return effects


def episode_prior_mastery(
    records: list[MasteryRecord],
    *,
    episode_id: str,
    fallback: dict[str, MasteryRecord] | None,
) -> dict[str, MasteryRecord]:
    """The level each record stood at BEFORE this episode touched it, read from its receipts.

    ``fallback`` is the store as loaded at the start of this terminus. On a first pass the two
    agree; on a retry the store already carries this episode's credit, and measuring against it
    would report a zero delta and lose the effect (WO-07). A record this episode left no receipt
    on keeps its fallback prior.
    """
    from dataclasses import replace

    prior = dict(fallback or {})
    for record in records:
        mine = receipts_for(record.effect_receipts, episode_id)
        if not mine:
            continue
        first = next(iter(mine.values()))
        prior[record.domain_id] = replace(
            record,
            knowledge_mastery=float(first.get("knowledge_before", 0.0)),
            application_mastery=float(first.get("application_before", 0.0)),
        )
    return prior


def _clamp01(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _omega_mastery_effects(entry: Any) -> list[dict[str, Any]]:
    """Omega's grounded per-episode mastery (recall ∪ result vs. target).

    The single source of mastery now — there is NO free counter. An episode with
    no Omega cognition (e.g. IDLE) yields no mastery update.
    """
    payload = _entry_dict(entry)
    omega = payload.get("omega_result")
    if not isinstance(omega, dict):
        return []
    effects = omega.get("mastery_effects")
    # tuple OR list: OmegaEpisodeResult.to_dict() (asdict) preserves the tuple
    # type; a JSON round-trip yields a list. Accept both.
    if not isinstance(effects, (list, tuple)):
        return []
    return [e for e in effects if isinstance(e, dict) and str(e.get("domain") or "").strip()]


def _receipt_backed_domains(entry: Any) -> set[str]:
    """Domains with a COMMITTED knowledge receipt in this entry's omega fold.

    cognition_chain stamps omega_result.knowledge_receipts after
    ingest_knowledge_products — the receipt is proof the knowledge actually
    landed in canonical memory, the discriminator between receipt-backed
    knowledge and prose self-grading."""
    payload = _entry_dict(entry)
    omega = payload.get("omega_result")
    receipts = omega.get("knowledge_receipts") if isinstance(omega, dict) else None
    out: set[str] = set()
    for receipt in receipts or ():
        if not isinstance(receipt, dict):
            continue
        if str(receipt.get("status") or "").lower() != "committed":
            continue
        domain = str(receipt.get("domain") or "").strip()
        if domain:
            out.add(normalize_domain_id(domain))
    return out


def _motor_attempt_admitted(entry: Any) -> bool:
    """Require execution evidence admission for motor-authored mastery claims."""
    payload = _entry_dict(entry)
    if not isinstance(payload.get("motor_program"), dict):
        return True
    assessment = payload.get("motor_attempt_assessment")
    return (
        isinstance(assessment, dict)
        and str(assessment.get("verification_verdict") or "").lower() == "verified"
    )


def _apply_motor_evidence_mastery(
    *,
    entry: dict[str, Any],
    action_goal: dict[str, Any],
    resolved_path: Any,
    episode_id: str,
) -> MasteryRecord | None:
    """Apply bounded, system-owned mastery credit from factual limb evidence."""
    attempt = entry.get("motor_attempt_assessment")
    if not isinstance(attempt, dict):
        return None
    evidence = attempt.get("execution_evidence")
    if not isinstance(evidence, dict):
        return None
    domain_name = str(attempt.get("domain") or "").strip()
    if not domain_name:
        return None
    domain_id = normalize_domain_id(domain_name)
    # Cut 13 review: the increment below is computed FROM the stored level, so
    # the read and the write it feeds are one transaction. Re-entrant with
    # ``_apply_grounded_mastery``'s own region, which is where the write lands.
    with store_transaction(MASTERY):
        prior = load_mastery(resolved_path).get(domain_id)
        pre = attempt.get("pre_mastery")
        pre = pre if isinstance(pre, dict) else {}
        prior_knowledge = (
            float(prior.knowledge_mastery)
            if prior is not None
            else _clamp01(pre.get("knowledge_mastery", pre.get("knowledge", 0.0)))
        )
        prior_application = (
            float(prior.application_mastery)
            if prior is not None
            else _clamp01(pre.get("application_mastery", pre.get("application", 0.0)))
        )
        knowledge_evidence = bool(evidence.get("sources_consulted") or evidence.get("artifacts"))
        application_evidence = bool(evidence.get("files_changed") or evidence.get("tests_run"))
        knowledge = _clamp01(
            prior_knowledge + (_MOTOR_EVIDENCE_INCREMENT if knowledge_evidence else 0.0)
        )
        application = _clamp01(
            prior_application + (_MOTOR_EVIDENCE_INCREMENT if application_evidence else 0.0)
        )
        return _apply_grounded_mastery(
            domain_name=domain_name,
            effect={
                "knowledge_mastery": knowledge,
                "application_mastery": application,
            },
            action_goal=action_goal,
            resolved_path=resolved_path,
            episode_id=episode_id,
            source="motor",
        )


def _apply_grounded_mastery(
    *,
    domain_name: str,
    effect: dict[str, Any],
    action_goal: dict[str, Any],
    resolved_path: Any,
    episode_id: str = "",
    source: str = "grounded",
) -> MasteryRecord:
    """Persist Omega's ABSOLUTE grounded estimate (may rise OR fall). Keyed by
    domain — the same key recall, storage, and topic-resolution use.

    WO-07: the credit carries a receipt under a stable effect id — ``mastery:<episode>:
    <domain>:<source>`` — written in the SAME upsert. A retried terminus that finds its receipt
    returns the stored record untouched: one effect, one credit, whatever the retry."""
    # Phase 4 cut 13 review: ONE transaction. This function computes the new
    # record FROM the record it just read — evidence_count is prior+1, the
    # target and subdomains are carried forward — so a second lane that reads
    # between this read and the upsert writes a record built from the same
    # prior, and the first lane's credit disappears. ``guarded`` cannot see
    # that: it makes each accessor atomic, and the race lives BETWEEN them.
    # Lane-local deepsleep (cut 13) is what put two lanes in here at once.
    # Measured before this region existed: 72 credits against one domain
    # landed 23 — "a mastery credit that never happened, no error anywhere".
    with store_transaction(MASTERY):
        domain_id = normalize_domain_id(domain_name)
        prior = load_mastery(resolved_path).get(domain_id)
        episode_key = str(episode_id or "").strip()
        receipt_id = effect_id("mastery", episode_key, f"{domain_id}:{source}") if episode_key else ""
        if receipt_id and prior is not None and receipt_id in prior.effect_receipts:
            # Already applied by an earlier attempt of this terminus: reconcile from the
            # receipt, apply nothing, emit nothing.
            return prior

        knowledge = _clamp01(effect.get("knowledge_mastery"))
        application = _clamp01(effect.get("application_mastery"))
        evidence_count = (int(prior.evidence_count) if prior else 0) + 1
        # A seeded/prior record owns its target; only a brand-new domain derives one.
        target_mastery = float(prior.target_mastery) if prior else _target_mastery(action_goal)

        # Step 5: append this episode's improvement delta to the rolling per-domain trace
        # (the plateau substrate). Failure-soft — a bad buffer must NEVER fail the fail-hard
        # mastery upsert below; degrade to the prior trace.
        last_plateau_episode = str(prior.last_plateau_episode) if prior else ""
        try:
            from pressure_loop.learning_progress import append_delta, emit_plateau, plateau_from

            prior_k = float(prior.knowledge_mastery) if prior else 0.0
            prior_a = float(prior.application_mastery) if prior else 0.0
            recent_knowledge_deltas = append_delta(
                prior.recent_knowledge_deltas if prior else (), knowledge - prior_k
            )
            recent_application_deltas = append_delta(
                prior.recent_application_deltas if prior else (), application - prior_a
            )
            facts = plateau_from(recent_knowledge_deltas, evidence_count)
            plateau_flag = bool(facts.get("plateau"))
            episode_id = str(
                episode_id or action_goal.get("episode_id") or effect.get("episode_id") or ""
            )
            if plateau_flag and episode_id:
                last_plateau_episode = episode_id
            emit_plateau(domain_name, episode_id, facts)
        except Exception:
            recent_knowledge_deltas = prior.recent_knowledge_deltas if prior else ()
            recent_application_deltas = prior.recent_application_deltas if prior else ()
            plateau_flag = bool(prior.last_plateau_flag) if prior else False

        record = MasteryRecord(
            domain_id=domain_id,
            domain_name=domain_name,
            knowledge_mastery=knowledge,
            application_mastery=application,
            confidence=float(prior.confidence) if prior else 0.0,
            evidence_count=evidence_count,
            subdomains=dict(prior.subdomains) if prior else {},
            target_mastery=target_mastery,
            status=status_for(knowledge),
            recent_knowledge_deltas=recent_knowledge_deltas,
            recent_application_deltas=recent_application_deltas,
            last_plateau_flag=plateau_flag,
            last_plateau_episode=last_plateau_episode,
            last_practice_episode=str(prior.last_practice_episode) if prior else "",
            parent=str(prior.parent) if prior else "",
            effect_receipts=(
                with_receipt(
                    prior.effect_receipts if prior else {},
                    receipt_id,
                    {
                        "episode_id": episode_key,
                        "source": source,
                        "knowledge_before": float(prior.knowledge_mastery) if prior else 0.0,
                        "application_before": float(prior.application_mastery) if prior else 0.0,
                        "knowledge_after": knowledge,
                        "application_after": application,
                        "evidence_count_after": evidence_count,
                    },
                )
                if receipt_id
                else dict(prior.effect_receipts) if prior else {}
            ),
        )
        upsert_mastery(record, path=resolved_path)
    # Step 8: record the mastery change to the SignalEvent audit ledger (record-only,
    # failure-soft, gated SIGNALS_RECORD=False -> no-op). Honest fields derived from
    # the REAL measured knowledge delta; never breaks the fail-hard upsert above.
    try:
        from pressure_loop.signal_ledger import SignalEvent, record as record_signal

        prior_k = float(prior.knowledge_mastery) if prior else 0.0
        delta = knowledge - prior_k
        record_signal(
            SignalEvent(
                source="mastery_update",
                kind="mastery_delta",
                # Use the episode_id already derived above (line ~181) — it honors
                # the passed window episode_id. action_goal/effect never carry an
                # 'episode_id' key, so re-deriving from them always yielded ""
                # and dropped the plateau-episode provenance (audit MEDIUM-11).
                episode_id=episode_id,
                domain=domain_name,
                valence=(1.0 if delta > 0 else (-1.0 if delta < 0 else 0.0)),
                salience=min(1.0, abs(delta)),
                meta={"knowledge": knowledge, "application": application, "delta": round(delta, 4)},
            )
        )
    except Exception:
        log.exception("mastery signal record failed (non-fatal)")
    return record


def update_mastery(
    ds_input: DeepSleepInput,
    *,
    mastery_path: Any = None,
) -> list[MasteryRecord]:
    """Apply Application Credit Law updates across the episode window.

    Fail-hard: mastery consolidation writes the continuity substrate (the single
    source of truth). A malformed episode / bad policy / store-write failure
    propagates and CRASHES — it is not swallowed into an empty result."""
    from pressure_loop.policy import deepsleep_mastery_path

    if not _episode_success(ds_input):
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "deepsleep",
                "mastery_skip",
                status="skip",
                summary="episode_not_success",
                reason="episode_not_success",
            )
        except Exception:
            pass
        return []

    resolved_path = mastery_path or deepsleep_mastery_path()
    action_goal = ds_input.action_goal or {}

    by_domain: dict[str, MasteryRecord] = {}
    for entry in list(ds_input.episode_window or []):
        if not _episode_success_entry(entry):
            continue
        entry_episode_id = str(entry.get("episode_id") or ds_input.episode_id or "")
        if isinstance(entry.get("motor_program"), dict):
            if _motor_attempt_admitted(entry):
                record = _apply_motor_evidence_mastery(
                    entry=entry,
                    action_goal=action_goal,
                    resolved_path=resolved_path,
                    episode_id=entry_episode_id,
                )
                if record is not None:
                    by_domain[record.domain_id] = record
            # Receipt-backed KNOWLEDGE still banks (live 2026-07-19: every LEARN
            # episode carries a USE_TOOL motor program whose fetch can never be
            # "verified", so committed kubernetes receipts sat at 0.0 mastery
            # forever). Prose stays inert — only effects whose domain holds a
            # COMMITTED knowledge receipt apply, and knowledge-only: application
            # remains motor-evidence-owned (pinned to the stored level).
            receipt_domains = _receipt_backed_domains(entry)
            if receipt_domains:
                for effect in _omega_mastery_effects(entry):
                    domain_name = str(effect.get("domain") or "").strip()
                    domain_id = normalize_domain_id(domain_name)
                    if domain_id not in receipt_domains:
                        continue
                    # Cut 13 review: application is PINNED to the stored level,
                    # so the read and the write that carries it forward are one
                    # transaction — otherwise a concurrent motor-evidence credit
                    # landing between them is pinned away.
                    with store_transaction(MASTERY):
                        stored = load_mastery(resolved_path).get(domain_id)
                        pinned = dict(effect)
                        pinned["application_mastery"] = (
                            float(stored.application_mastery) if stored else 0.0
                        )
                        by_domain[domain_id] = _apply_grounded_mastery(
                            domain_name=domain_name,
                            effect=pinned,
                            action_goal=action_goal,
                            resolved_path=resolved_path,
                            episode_id=entry_episode_id,
                            source="receipt",
                        )
            continue
        for effect in _omega_mastery_effects(entry):
            domain_name = str(effect.get("domain") or "").strip()
            if not domain_name:
                continue
            by_domain[normalize_domain_id(domain_name)] = _apply_grounded_mastery(
                domain_name=domain_name,
                effect=effect,
                action_goal=action_goal,
                resolved_path=resolved_path,
                episode_id=entry_episode_id,
            )

    return list(by_domain.values())
