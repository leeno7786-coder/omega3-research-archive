"""DeepSleep Phase 6 — trajectory maintenance from omega trajectory updates."""

from __future__ import annotations

from typing import Any

from deepsleep_loop._deepsleep_state import DeepSleepInput, TrajectoryRecord
from deepsleep_loop.store_locks import TRAJECTORY, store_transaction
from deepsleep_loop.trajectory_store import (
    load_trajectory,
    save_trajectory,
    trajectory_key,
)


def trajectory_effects_from_records(
    records: list[TrajectoryRecord],
    *,
    prior: dict[str, TrajectoryRecord] | None,
) -> dict[str, dict[str, float | int | str | list]]:
    """Build journal trajectory_effects from updated records."""
    prior = prior or {}
    effects: dict[str, dict[str, float | int | str | list]] = {}
    for record in records:
        key = trajectory_key(record.target_domain, record.target_capability)
        old = prior.get(key)
        prior_confidence = float(old.confidence) if old else 0.0
        prior_count = int(old.reinforcement_count) if old else 0
        confidence_delta = float(record.confidence) - prior_confidence
        count_delta = int(record.reinforcement_count) - prior_count
        if (
            confidence_delta == 0.0
            and count_delta == 0
            and (not old or old.status == record.status)
        ):
            if not old:
                effects[key] = {
                    "target_domain": record.target_domain,
                    "target_capability": record.target_capability,
                    "confidence": float(record.confidence),
                    "reinforcement_count": int(record.reinforcement_count),
                    "status": record.status,
                }
            continue
        effects[key] = {
            "target_domain": record.target_domain,
            "target_capability": record.target_capability,
            "confidence_delta": confidence_delta,
            "reinforcement_delta": count_delta,
            "confidence": float(record.confidence),
            "reinforcement_count": int(record.reinforcement_count),
            "status": record.status,
        }
    return effects


def _apply_decay(store: dict[str, TrajectoryRecord], *, decay_after: int, decay_inc: float) -> None:
    from dataclasses import replace

    for key, record in list(store.items()):
        if record.status != "active":
            continue
        passes = int(record.passes_since_reinforcement) + 1
        if passes >= decay_after:
            new_confidence = max(0.0, float(record.confidence) - decay_inc)
            store[key] = replace(
                record,
                confidence=new_confidence,
                supporting_episodes=list(record.supporting_episodes),
                passes_since_reinforcement=0,
            )
        else:
            store[key] = replace(
                record,
                supporting_episodes=list(record.supporting_episodes),
                passes_since_reinforcement=passes,
            )


def _mark_superseded(
    store: dict[str, TrajectoryRecord],
    *,
    new_domain: str,
    new_confidence: float,
) -> None:
    for key, record in list(store.items()):
        if record.status != "active":
            continue
        if record.target_domain == new_domain:
            continue
        if float(new_confidence) > float(record.confidence):
            from dataclasses import replace

            store[key] = replace(
                record, supporting_episodes=list(record.supporting_episodes), status="superseded"
            )


def _publish_active_trajectory(
    record: TrajectoryRecord, *, window_id: str, influencer_text: str, durable_event_id: str = ""
) -> dict | None:
    """Republish the winning trajectory to the slot the NEXT ignition reads.

    This is the return channel, and without it the project's thesis does not hold. Omega could
    already reflect, conclude, and have that conclusion upserted into the trajectory store by the
    phase below — and then nothing carried it any further. ``pressure_loop/pressure.py`` reads
    ``LAST_OMEGA_TRAJECTORY`` and nothing else for the next episode's trajectory, and that slot's
    only writers were an operator directive and onboarding. So the operator set Omega's learning
    target and Omega's own learning never could.

    Not a new mechanism. ``pressure_loop/operator_pressure.seed_trajectory_from_operator_directive``
    already documents this as the intended design — its seed is single-cycle because "the omega
    loop's own phase_4_commit will later supersede via the canonical trajectory_store path (which
    writes the durable identity-lobe record + republishes the SS slot)". This is that republish.
    It also settles precedence: superseding the operator's seed is the designed behaviour, not an
    authority invented here.

    ``intent`` is the one judgement. ``OmegaTrajectory.intent`` is a four-value enum and the
    window emits a different vocabulary (establish|continue|adjust|complete), so there is no
    direct source. Rather than map two unrelated vocabularies and present it as forced, it is
    derived from what the STORE record says happened: a first-time target is exploration, a
    target a second window reinforced is deepening. Both are claims the record supports.

    Returns the published dict, or ``None`` when no pressure runtime is registered — a process
    without one is not running cognition, so there is no next ignition to steer. That is a real
    absence rather than a swallowed failure, and it is pinned by a test.
    """
    from pressure_loop.runtime import try_get_default_runtime

    runtime = try_get_default_runtime()
    if runtime is None:
        return None

    from core.shared_state_keys import LAST_OMEGA_TRAJECTORY
    from schema.omega_trajectory import make_trajectory

    reinforced = int(record.reinforcement_count) > 1
    episodes = list(record.supporting_episodes) or ([window_id] if window_id else [])
    trajectory = make_trajectory(
        cycle_id=str(window_id or "window_reflection"),
        # Every real trajectory record on disk carries 0 here and nothing reads it; a
        # synthesised value would look like a signal that no consumer asked for.
        epoch_emitted=0,
        intent="deepen_capability" if reinforced else "explore_domain",
        target_domain=record.target_domain,
        target_capability=record.target_capability,
        # Provenance, so the operator can ask "why am I being steered here?" and get episodes
        # back. A trajectory that steers without saying what produced it steers silently.
        rationale=(
            f"window reflection over {', '.join(episodes[-3:]) or 'this window'}; "
            f"reinforced {int(record.reinforcement_count)}x, confidence "
            f"{float(record.confidence):.2f}"
        ),
        influencer_text=str(influencer_text or ""),
    )
    published = trajectory.to_dict()
    # write_canonical validates the OmegaTrajectory shape; a rejection raises, matching this
    # phase's fail-hard contract. The trajectory store is already saved at this point, so a
    # rejected publish is a loud inconsistency rather than a silent half-write.
    runtime.shared_state.write_canonical(LAST_OMEGA_TRAJECTORY, published)
    _write_durable_trajectory(published, event_id=durable_event_id)
    return published


def _write_durable_trajectory(published: dict, *, event_id: str = "") -> bool:
    """The durable half: the lobe-9 record Omega's OWN per-episode prompt reads.

    There are three trajectory locations and the slot above reaches only two readers. The
    third, ``omega_loop/episode/packet._trajectory_influencer_block``, is how a trajectory
    reaches Omega's per-episode prompt — and Omega is where "what happens next" is chosen
    (AGENTS.md section 3). It reads the latest lobe-9 ``trajectory`` record, whose only
    production writer was ``hud/operator_onboarding``. With the slot alone, Omega's own prompt
    would still only ever see what the operator set at onboarding.

    Written through ``temporal_store.write_trajectory``, which enters ``memory.api.intake_memory``
    with ``bypass_guardian=True`` — the single memory lane (section 5) and the mind's direct
    self-write (section 6, which names temporal_store's pattern as the correct reference). It
    lands in the owning episode's SRAM bucket before DeepSleep's flush, which drains "every
    DeepSleep producer above" in this same pass, so the next episode reads it from canonical
    memory: the past, honestly, per section 5.0.1.

    Only when this thread owns an episode bucket. A thread that owns none has nowhere lawful to
    put a memory record — writing around SRAM would be the second memory lane section 5 forbids
    — so it writes nothing and says so by returning False. The shared-state slot is not memory
    and is published regardless.
    """
    from memory import episode_sram

    if episode_sram.bound_episode_id() is None:
        return False

    from pressure_loop.temporal_store import write_trajectory

    write_trajectory(published, event_id=event_id or None)
    return True


def maintain_trajectory(
    ds_input: DeepSleepInput,
    *,
    trajectory_path: Any = None,
) -> list[TrajectoryRecord]:
    """Upsert trajectory records from omega trajectory_update.

    Fail-hard: the trajectory store is a named continuity structure. A load/save
    failure propagates and CRASHES — it is not swallowed to []."""
    from pressure_loop.policy import (
        deepsleep_trajectory_decay_after,
        deepsleep_trajectory_decay_increment,
        deepsleep_trajectory_path,
    )

    omega = ds_input.omega_outcome or {}
    update = dict(omega.get("trajectory_update") or {})
    target_domain = str(update.get("target_domain") or "").strip()
    if not target_domain:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "deepsleep",
                "trajectory_skip",
                status="skip",
                summary="no_target_domain",
                reason="no_target_domain",
            )
        except Exception:
            pass
        return []

    target_capability = str(update.get("target_capability") or "").strip()
    confidence = float(update.get("confidence", 0.0) or 0.0)
    supporting_episode = str(update.get("supporting_episode") or ds_input.episode_id or "")

    from deepsleep_loop.effect_receipts import effect_id, with_receipt

    resolved_path = trajectory_path or deepsleep_trajectory_path()
    key = trajectory_key(target_domain, target_capability)
    terminus_id = str(ds_input.episode_id or "")
    receipt_id = effect_id("trajectory", terminus_id, key) if terminus_id else ""
    # Phase 4 cut 13 review: ONE transaction, because this phase does not upsert
    # a key — it reads the whole map, decays EVERY record in it, supersedes some,
    # and writes the whole map back. Lane-local deepsleep (cut 13) lets two lanes
    # run this at once, and without the region the second ``save_trajectory``
    # writes a map built from a read that predates the first one: the other
    # lane's new target, its decay pass and its supersessions all vanish with no
    # error anywhere. Measured before this region existed: 6 threads × 12 writes
    # landed 33 of 72 records.
    with store_transaction(TRAJECTORY):
        store = load_trajectory(resolved_path)
        landed = store.get(key)
        reconciled = bool(receipt_id and landed is not None and receipt_id in landed.effect_receipts)
    if reconciled:
        # WO-07: an earlier attempt of THIS terminus already saved its whole pass. Apply nothing
        # again; republish (the slot is idempotent, and the durable record reconciles by its
        # event id) so a first attempt that failed after its save still reaches the next ignition.
        _publish_active_trajectory(
            landed,
            window_id=str(ds_input.episode_id or ""),
            influencer_text=str(update.get("reason") or update.get("rationale") or ""),
            durable_event_id=f"trajectory:{receipt_id}",
        )
        return [landed]

    with store_transaction(TRAJECTORY):
        store = load_trajectory(resolved_path)
        _apply_decay(
            store,
            decay_after=deepsleep_trajectory_decay_after(),
            decay_inc=deepsleep_trajectory_decay_increment(),
        )

        prior = store.get(key)
        receipts = dict(prior.effect_receipts) if prior else {}
        if receipt_id:
            receipts = with_receipt(receipts, receipt_id, {
                "episode_id": terminus_id,
                "reinforcement_before": int(prior.reinforcement_count) if prior else 0,
                "reinforcement_after": int(prior.reinforcement_count) + 1 if prior else 1,
            })
        # Any existing prior under this key carries its history forward — whether it is
        # still active (reinforcement) or was superseded/decayed and is now being revived.
        # reinforcement_count is the projection tiebreaker, so resetting it to 1 on revival
        # would silently drop the trajectory's accumulated weight + audit trail (I12).
        if prior:
            episodes = list(prior.supporting_episodes)
            if supporting_episode and supporting_episode not in episodes:
                episodes.append(supporting_episode)
            record = TrajectoryRecord(
                target_domain=target_domain,
                target_capability=target_capability,
                confidence=max(float(prior.confidence), confidence),
                reinforcement_count=int(prior.reinforcement_count) + 1,
                supporting_episodes=episodes,
                status="active",
                passes_since_reinforcement=0,
                effect_receipts=receipts,
            )
        else:
            record = TrajectoryRecord(
                target_domain=target_domain,
                target_capability=target_capability,
                confidence=confidence,
                reinforcement_count=1,
                supporting_episodes=[supporting_episode] if supporting_episode else [],
                status="active",
                passes_since_reinforcement=0,
                effect_receipts=receipts,
            )

        _mark_superseded(store, new_domain=target_domain, new_confidence=confidence)
        store[key] = record
        save_trajectory(store, path=resolved_path)

    # The return channel. Inside the store transaction the map is consistent; the slot is
    # published AFTER it, so a reader of the slot can never see a target the store does not
    # hold. The window's own reason travels as the influencer text, because the sentence Omega
    # wrote about its own window is better guidance than anything generated from the enum.
    _publish_active_trajectory(
        record,
        window_id=str(ds_input.episode_id or ""),
        influencer_text=str(update.get("reason") or update.get("rationale") or ""),
        durable_event_id=f"trajectory:{receipt_id}" if receipt_id else "",
    )
    return [record]
