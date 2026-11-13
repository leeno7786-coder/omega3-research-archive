"""DeepSleep Phase 4 — identity trait consolidation from episode omega."""

from __future__ import annotations

from typing import Any

from deepsleep_loop._deepsleep_state import DeepSleepInput, Trait
from deepsleep_loop.identity_store import load_identity, normalize_trait_name, upsert_trait


def _collect_observations(ds_input: DeepSleepInput) -> list[str]:
    omega = ds_input.omega_outcome or {}
    reflection = dict(omega.get("reflection") or {})
    emergent = dict(reflection.get("emergent_observations") or {})
    observations: list[str] = []

    seen: set[str] = set()
    for key in ("repeated_behaviors", "stable_patterns"):
        for item in emergent.get(key) or []:
            if isinstance(item, str) and item.strip():
                trait_key = normalize_trait_name(item)
                if trait_key not in seen:
                    seen.add(trait_key)
                    observations.append(item.strip())

    # The agentic `identity` field carries the trait signal (was the 5W1H `who` bucket).
    reasoning = dict(reflection.get("reasoning") or {})
    identity_signal = reasoning.get("identity")
    if isinstance(identity_signal, str) and identity_signal.strip():
        trait_key = normalize_trait_name(identity_signal)
        if trait_key not in seen:
            observations.append(identity_signal.strip())

    return observations


def _collect_pattern_observations(ds_input: DeepSleepInput) -> list[dict[str, Any]]:
    """Post-chat IDENTITY PATTERNS emitted across the window's episodes. Each episode body carries
    the stashed omega_result whose identity_patterns are [{trait, pattern, evidence, status}]. Keyed
    by the STABLE `trait` slug (not the full pattern sentence — that would fragment across rewordings
    and never merge). The episode id is the independent evidence identity: retries overwrite while
    two separate chat foldbacks in the same window remain two observations. Malformed
    rows are filtered here, before any storage operation runs."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for body_index, body in enumerate(getattr(ds_input, "episode_window", None) or []):
        if not isinstance(body, dict):
            continue
        omega_result = body.get("omega_result") or {}
        if not isinstance(omega_result, dict):
            continue
        window_id = str(getattr(ds_input, "window_id", "window") or "window")
        episode_id = str(body.get("episode_id") or f"{window_id}:{body_index}")
        for row in omega_result.get("identity_patterns") or []:
            if not isinstance(row, dict):
                continue
            pattern = str(row.get("pattern") or "").strip()
            key = normalize_trait_name(str(row.get("trait") or "") or pattern)
            if not key:
                continue
            observation_id = f"episode:{episode_id}:{key}"
            rows[(key, observation_id)] = {
                "trait": key,
                "observation_id": observation_id,
                "pattern": pattern,
                "evidence": str(row.get("evidence") or "").strip(),
                "status": str(row.get("status") or "new").strip().lower(),
            }
    return list(rows.values())


def identity_effects_from_traits(
    traits: list[Trait],
    *,
    prior: dict[str, Trait] | None,
) -> dict[str, dict[str, float | int | str]]:
    """Build journal identity_effects deltas from updated traits."""
    prior = prior or {}
    effects: dict[str, dict[str, float | int | str]] = {}
    for trait in traits:
        key = normalize_trait_name(trait.name)
        old = prior.get(key)
        prior_strength = float(old.strength) if old else 0.0
        prior_evidence = int(old.evidence_count) if old else 0
        strength_delta = float(trait.strength) - prior_strength
        evidence_delta = int(trait.evidence_count) - prior_evidence
        if strength_delta == 0.0 and evidence_delta == 0:
            continue
        effects[key] = {
            "name": trait.name,
            "strength_delta": strength_delta,
            "evidence_delta": evidence_delta,
            "strength": float(trait.strength),
            "evidence_count": int(trait.evidence_count),
        }
    return effects


# Supporting observations stored per trait. The vote is decided long before
# this (promote_evidence floors at 2), and every DeepSleep re-observed the same
# ~8 already-promoted traits with near-identical text: live 2026-09-06, 219
# trait-observation records held 19 distinct texts (one repeated 52 times),
# 102 minted in one hour of chat. Past the cap a supporting observation adds a
# file and nothing else; tensions are always recorded, they can demote.
SUPPORTING_OBSERVATION_CAP = 12


def observation_capped(trait_key: str, status: str, stored_counts: dict[str, int]) -> bool:
    """True when a SUPPORTING observation for ``trait_key`` would only add a file."""
    if str(status or "").strip().lower() == "tensions_with_prior":
        return False
    return int(stored_counts.get(trait_key, 0) or 0) >= SUPPORTING_OBSERVATION_CAP


def update_identity(
    ds_input: DeepSleepInput,
    *,
    identity_path: Any = None,
) -> list[Trait]:
    """Strengthen traits from omega identity observations.

    Malformed post-chat rows are filtered during collection. Storage failures
    propagate to DeepSleep's episode failure capture boundary, which preserves
    the cause and leaves the episode open for retry."""
    from deepsleep_loop.identity_store import (
        load_trait_evidence,
        record_trait_observation,
    )
    from pressure_loop.policy import (
        deepsleep_identity_promote_evidence,
        deepsleep_identity_strength_increment,
    )

    observations = _collect_observations(ds_input)
    patterns = _collect_pattern_observations(ds_input)
    if not observations and not patterns:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "deepsleep",
                "identity_skip",
                status="skip",
                summary="no_observations",
                reason="no_observations",
            )
        except Exception:
            pass
        return []

    legacy_identity_path = identity_path
    prior_store = load_identity(legacy_identity_path)
    increment = deepsleep_identity_strength_increment()
    promote_evidence = deepsleep_identity_promote_evidence()
    updated: list[Trait] = []

    # Episode rows are the primary evidence. A window-level summary of the same
    # trait must not count again; it is a synthesis of those foldbacks, not a new
    # observation.
    pattern_keys = {str(row.get("trait") or "") for row in patterns}
    signals = list(patterns)
    for observation in observations:
        trait_key = normalize_trait_name(observation)
        if not trait_key or trait_key in pattern_keys:
            continue
        signals.append(
            {
                "trait": trait_key,
                "observation_id": f"window:{ds_input.window_id}:{trait_key}",
                "pattern": observation,
                "evidence": observation,
                "status": "new",
            }
        )

    touched: set[str] = set()
    latest_support: dict[str, dict[str, Any]] = {}
    stored_counts = {
        key: int(value.observation_count)
        for key, value in load_trait_evidence(include_episode_sram=False).items()
    }
    for row in signals:
        trait_key = normalize_trait_name(
            str(row.get("trait") or "") or (row.get("pattern") or "")
        )
        if not trait_key:
            continue
        status = str(row.get("status") or "new").strip().lower()
        if observation_capped(trait_key, status, stored_counts):
            continue
        record_trait_observation(
            name=trait_key,
            observation_id=str(row.get("observation_id") or ds_input.window_id),
            pattern=str(row.get("pattern") or ""),
            evidence=str(row.get("evidence") or ""),
            status=status,
            cycle_id=ds_input.window_id,
        )
        touched.add(trait_key)
        if status != "tensions_with_prior":
            latest_support[trait_key] = row

    evidence_store = load_trait_evidence(include_episode_sram=True)
    for trait_key in sorted(touched):
        evidence = evidence_store.get(trait_key)
        if evidence is None:
            continue
        prior = prior_store.get(trait_key)
        if prior is not None:
            new_support = max(
                0,
                evidence.evidence_count - int(prior.observation_count),
            )
            if new_support == 0:
                continue
            evidence_count = int(prior.evidence_count) + new_support
            strength = min(1.0, float(prior.strength) + increment * new_support)
        else:
            # Pre-migration one-offs in the old preference subdomain are hidden,
            # not trusted as provenance-free support. They need two fresh,
            # independently identified observations before replacement.
            evidence_count = evidence.evidence_count
            if evidence_count < promote_evidence:
                continue
            strength = min(1.0, increment * evidence.evidence_count)

        row = latest_support.get(trait_key, {})
        trait = Trait(
            name=trait_key,
            strength=strength,
            evidence_count=evidence_count,
            evidence=str(row.get("pattern") or evidence.pattern or evidence.evidence),
            status=str(row.get("status") or evidence.status or "confirmed"),
            observation_count=evidence.evidence_count,
        )
        if upsert_trait(
            trait,
            path=legacy_identity_path,
            cycle_id=ds_input.window_id,
        ):
            prior_store[trait_key] = trait
            updated.append(trait)

    return updated
