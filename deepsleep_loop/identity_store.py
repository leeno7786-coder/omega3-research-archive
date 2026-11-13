"""DeepSleep identity store — canonical identity-lobe backing with legacy import."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from deepsleep_loop._deepsleep_state import Trait

_LOG = logging.getLogger(__name__)
_IDENTITY_LOBE = 0
_TRAITS_SUBDOMAIN = 1  # 000_identity/traits (was emergent_traits at Y=2)
_LEGACY_TRAITS_SUBDOMAIN = 2
_EVOLUTION_SUBDOMAIN = 3
_TRAIT_TYPE = "emergent_trait"
_TRAIT_OBSERVATION_TYPE = "trait_observation"


@dataclass(frozen=True)
class TraitEvidence:
    """Aggregated candidate evidence; this is not a system preference."""

    name: str
    evidence_count: int
    observation_count: int
    pattern: str = ""
    evidence: str = ""
    status: str = ""


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = _repo_root() / p
    return p


def normalize_trait_name(observation: str) -> str:
    """Normalize an identity observation into THE stable trait key — a short
    snake_case slug (<=6 words, <=48 chars).

    One slug authority for every path (window-emergent observations, chat
    reflection, store load/upsert): a full-sentence key fragments across
    rewordings, splits the same trait into parallel records, and never
    accumulates evidence past the >=2 readback floor (live finding 2026-07-03:
    focus_on_definition existed under two keys). Idempotent on existing slugs."""
    text = str(observation or "").strip().lower()
    words = "".join(c if c.isalnum() else " " for c in text).split()[:6]
    return "_".join(words)[:48]


def _default_legacy_path() -> Path:
    from pressure_loop.policy import deepsleep_identity_path

    return _resolve_path(deepsleep_identity_path())


def _legacy_path(path: str | Path | None = None) -> Path:
    return _resolve_path(path or _default_legacy_path())


def _trait_record_id(name: str) -> str:
    trait_name = normalize_trait_name(name) or "trait"
    trait_name = re.sub(r"[^a-z0-9_]+", "_", trait_name).strip("_") or "trait"
    digest = hashlib.sha1(trait_name.encode("utf-8")).hexdigest()[:12]
    return f"trait.{trait_name}.{digest}"


def _trait_from_meta(meta: dict) -> Trait | None:
    trait_payload = meta.get("trait")
    if not isinstance(trait_payload, dict):
        return None
    name = normalize_trait_name(str(trait_payload.get("name") or ""))
    if not name:
        return None
    return Trait(
        name=name,
        strength=float(trait_payload.get("strength", 0.0) or 0.0),
        evidence_count=int(trait_payload.get("evidence_count", 0) or 0),
        # Read back the evidence sentence + status the envelope persisted
        # (_trait_envelope writes both). Omitting them rebuilt every canonical trait
        # with evidence="" -> the chat-reflection self-model block rendered
        # "- <slug> (seen ×N)" with no statement, so the mind lost the description of
        # what each trait means and re-derived it each reflection (operator 2026-07-09).
        evidence=str(trait_payload.get("evidence", "") or ""),
        status=str(trait_payload.get("status", "") or ""),
        observation_count=int(trait_payload.get("observation_count", 0) or 0),
    )


def _trait_observation_from_meta(meta: dict) -> dict | None:
    payload = meta.get("trait_observation")
    if not isinstance(payload, dict):
        return None
    name = normalize_trait_name(str(payload.get("name") or ""))
    observation_id = str(payload.get("observation_id") or "").strip()
    if not name or not observation_id:
        return None
    status = str(payload.get("status") or "new").strip().lower()
    return {
        "name": name,
        "observation_id": observation_id,
        "pattern": str(payload.get("pattern") or "").strip(),
        "evidence": str(payload.get("evidence") or "").strip(),
        "status": status,
        "supporting": bool(payload.get("supporting", status != "tensions_with_prior")),
    }


def _load_canonical_identity() -> dict[str, Trait]:
    from memory.engine.walker import (
        coord_index_snapshot,
        load_coord_index,
        load_meta_by_path,
    )

    load_coord_index()
    out: dict[str, Trait] = {}
    heads: dict[str, str] = {}
    for _memory_id, entry in sorted(coord_index_snapshot().items()):
        coord = list(entry.get("coord") or [])
        if len(coord) < 2:
            continue
        if int(coord[0]) != _IDENTITY_LOBE:
            continue
        if int(coord[1]) not in (_TRAITS_SUBDOMAIN, _LEGACY_TRAITS_SUBDOMAIN):
            continue
        path = entry.get("path")
        if not path:
            continue
        meta = load_meta_by_path(path)
        if not isinstance(meta, dict) or not meta:
            continue
        if meta.get("superseded_by"):
            continue
        trait = _trait_from_meta(meta)
        if trait is None:
            continue
        name = normalize_trait_name(trait.name)
        if name in heads:
            raise ValueError(
                f"competing active identity trait heads for {name!r}: "
                f"{heads[name]} and {_memory_id}"
            )
        heads[name] = _memory_id
        out[name] = trait
    return out


def _load_canonical_trait_observations() -> list[dict]:
    from memory.engine.walker import (
        coord_index_snapshot,
        load_coord_index,
        load_meta_by_path,
    )

    load_coord_index()
    observations: dict[tuple[str, str], dict] = {}
    for _memory_id, entry in sorted(coord_index_snapshot().items()):
        coord = list(entry.get("coord") or [])
        if len(coord) < 2:
            continue
        if [int(coord[0]), int(coord[1])] != [_IDENTITY_LOBE, _EVOLUTION_SUBDOMAIN]:
            continue
        path = entry.get("path")
        if not path:
            continue
        meta = load_meta_by_path(path)
        if not isinstance(meta, dict) or not meta:
            continue
        observation = _trait_observation_from_meta(meta)
        if observation is None:
            continue
        observations[(observation["name"], observation["observation_id"])] = observation
    return list(observations.values())


def _load_episode_sram_trait_observations() -> list[dict]:
    """Read this episode's held identity evidence without making it recallable."""
    from memory.episode_sram import pending

    observations: dict[tuple[str, str], dict] = {}
    for item in pending():
        envelope = item.envelope if item.kind == "memory" else None
        if getattr(envelope, "type", None) != _TRAIT_OBSERVATION_TYPE:
            continue
        payload = getattr(envelope, "payload", None)
        row = _trait_observation_from_meta(payload) if isinstance(payload, dict) else None
        if row is not None:
            observations[(row["name"], row["observation_id"])] = row
    return list(observations.values())


def load_trait_evidence(*, include_episode_sram: bool = False) -> dict[str, TraitEvidence]:
    """Load candidate observations grouped by trait outside preference recall."""
    grouped: dict[str, dict] = {}
    rows = _load_canonical_trait_observations()
    if include_episode_sram:
        by_identity = {(row["name"], row["observation_id"]): row for row in rows}
        for row in _load_episode_sram_trait_observations():
            by_identity[(row["name"], row["observation_id"])] = row
        rows = list(by_identity.values())
    for row in rows:
        name = row["name"]
        bucket = grouped.setdefault(
            name,
            {
                "evidence_count": 0,
                "observation_count": 0,
                "pattern": "",
                "evidence": "",
                "status": "",
            },
        )
        bucket["observation_count"] += 1
        if row["supporting"]:
            bucket["evidence_count"] += 1
            bucket["pattern"] = row["pattern"]
            bucket["evidence"] = row["evidence"]
            bucket["status"] = row["status"]
    return {name: TraitEvidence(name=name, **payload) for name, payload in grouped.items()}


def _load_legacy_identity(path: Path) -> dict[str, Trait]:
    if not path.is_file():
        return {}

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}

    out: dict[str, Trait] = {}
    for key, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        trait = Trait.from_dict(payload)
        trait_key = normalize_trait_name(trait.name or key)
        if not trait_key:
            continue
        out[trait_key] = Trait(
            name=trait_key,
            strength=float(trait.strength),
            evidence_count=int(trait.evidence_count),
            evidence=str(getattr(trait, "evidence", "") or ""),
            status=str(getattr(trait, "status", "") or ""),
        )
    return out


def _trait_envelope(trait: Trait, *, cycle_id: str):
    from memory.api import CanonicalEnvelope

    name = normalize_trait_name(trait.name)
    trait_id = _trait_record_id(name)
    trait_payload = {
        "name": name,
        "strength": float(trait.strength),
        "evidence_count": int(trait.evidence_count),
        "evidence": str(getattr(trait, "evidence", "") or ""),
        "status": str(getattr(trait, "status", "") or ""),
        "promoted": True,
    }
    observation_count = int(getattr(trait, "observation_count", 0) or 0)
    if observation_count:
        trait_payload["observation_count"] = observation_count
    family_id = trait_id
    revision = hashlib.sha256(json.dumps(
        [cycle_id, trait_payload], sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")).hexdigest()[:16]
    trait_id = f"{family_id}.{revision}"
    payload = {
        "id": trait_id,
        "text": (
            f"Emergent identity trait {name}: strength {float(trait.strength):.2f}, "
            f"evidence {int(trait.evidence_count)}."
        ),
        "title": f"Identity Trait - {name}",
        "summary": f"{name} emergent trait",
        "subtopic": _TRAIT_TYPE,
        "tags": ["identity", "trait", name],
        "trait": trait_payload,
        "enrichment": {"trait_revision": {"family_id": family_id}},
    }
    return CanonicalEnvelope(
        cycle_id=cycle_id,
        coord=[_IDENTITY_LOBE, _TRAITS_SUBDOMAIN, 0],
        type=_TRAIT_TYPE,
        payload=payload,
        source="deepsleep.identity_store",
        confidence=max(0.1, min(1.0, float(trait.strength) or 0.5)),
        provenance=["deepsleep.identity_store"],
        id=trait_id,
        strength_index=max(0.1, min(1.0, float(trait.strength) or 0.5)),
    )


def _trait_observation_record_id(name: str, observation_id: str) -> str:
    trait_name = normalize_trait_name(name) or "trait"
    digest = hashlib.sha1(f"{trait_name}\x00{observation_id}".encode("utf-8")).hexdigest()[:16]
    return f"trait-observation.{trait_name}.{digest}"


def record_trait_observation(
    *,
    name: str,
    observation_id: str,
    pattern: str = "",
    evidence: str = "",
    status: str = "new",
    cycle_id: str,
) -> None:
    """Persist one retry-safe observation without making it a preference."""
    from memory.api import CanonicalEnvelope, intake_memory, intake_receipt_accepted

    trait_name = normalize_trait_name(name)
    normalized_status = str(status or "new").strip().lower()
    if normalized_status not in {"new", "confirmed", "tensions_with_prior"}:
        normalized_status = "new"
    stable_observation_id = str(observation_id or cycle_id)
    record_id = _trait_observation_record_id(trait_name, stable_observation_id)
    supporting = normalized_status != "tensions_with_prior"
    envelope = CanonicalEnvelope(
        cycle_id=cycle_id,
        coord=[_IDENTITY_LOBE, _EVOLUTION_SUBDOMAIN, 0],
        type=_TRAIT_OBSERVATION_TYPE,
        payload={
            "id": record_id,
            "text": f"Candidate identity observation for {trait_name}: {pattern}",
            "title": f"Trait Observation - {trait_name}",
            "summary": f"unpromoted evidence for {trait_name}",
            "subtopic": _TRAIT_OBSERVATION_TYPE,
            "tags": ["identity", "trait_observation", trait_name],
            "trait_observation": {
                "name": trait_name,
                "observation_id": stable_observation_id,
                "pattern": str(pattern or "")[:300],
                "evidence": str(evidence or "")[:400],
                "status": normalized_status,
                "supporting": supporting,
            },
        },
        source="deepsleep.identity_store",
        confidence=0.5 if supporting else 0.1,
        provenance=["deepsleep.identity_store", stable_observation_id],
        id=record_id,
        strength_index=0.1,
    )
    receipt = intake_memory(envelope, propose=False, bypass_guardian=True)
    if not intake_receipt_accepted(receipt):
        msg = getattr(receipt, "message", None) or "trait observation commit rejected"
        raise RuntimeError(msg)


def _write_canonical_trait(trait: Trait, *, cycle_id: str) -> None:
    from memory.api import intake_memory, intake_receipt_accepted

    envelope = _trait_envelope(trait, cycle_id=cycle_id)
    receipt = intake_memory(envelope, propose=False, bypass_guardian=True)
    if not intake_receipt_accepted(receipt):
        msg = getattr(receipt, "message", None) or "trait commit rejected"
        raise RuntimeError(msg)


def _ensure_legacy_import(path: Path) -> bool:
    if _load_canonical_identity() or _load_canonical_trait_observations():
        return False

    legacy_traits = _load_legacy_identity(path)
    if not legacy_traits:
        return False

    from pressure_loop.policy import deepsleep_identity_promote_evidence

    floor = deepsleep_identity_promote_evidence()
    for trait_key, trait in legacy_traits.items():
        if int(trait.evidence_count) >= floor:
            _write_canonical_trait(
                Trait(
                    name=trait_key,
                    strength=float(trait.strength),
                    evidence_count=int(trait.evidence_count),
                    evidence=str(getattr(trait, "evidence", "") or ""),
                    status=str(getattr(trait, "status", "") or ""),
                ),
                cycle_id=f"legacy-trait-import:{trait_key}",
            )
        else:
            record_trait_observation(
                name=trait_key,
                observation_id=f"legacy:{trait_key}",
                pattern=str(getattr(trait, "evidence", "") or trait_key),
                evidence="legacy trait candidate",
                status="new",
                cycle_id=f"legacy-trait-import:{trait_key}",
            )
    _LOG.info("Imported %d legacy emergent traits into canonical memory", len(legacy_traits))
    return True


def load_identity(path: str | Path | None = None) -> dict[str, Trait]:
    """Load promoted system preferences keyed by normalized trait name.

    Canonical memory in lobe ``0/1`` is the durable owner. ``path`` is now a
    legacy-import location only: when canonical memory is empty, an existing
    legacy JSON file is migrated in-place on first access."""
    from pressure_loop.policy import deepsleep_identity_promote_evidence

    floor = deepsleep_identity_promote_evidence()
    store = {
        key: trait
        for key, trait in _load_canonical_identity().items()
        if int(trait.evidence_count) >= floor
    }
    if store:
        return store

    legacy_path = _legacy_path(path)
    imported = _ensure_legacy_import(legacy_path)
    if not imported:
        return {}
    return {
        key: trait
        for key, trait in _load_canonical_identity().items()
        if int(trait.evidence_count) >= floor
    }


def emergent_traits(min_evidence: int = 2, path: str | Path | None = None) -> list[Trait]:
    """The ESTABLISHED emergent traits — those repeated across at least ``min_evidence`` windows —
    sorted strongest-first.

    DeepSleep records each foldback separately and promotes only repeated independent support.
    This reader is the consumption seam and also filters migrated pre-floor records.
    Failure-soft: a missing store -> []."""
    try:
        store = load_identity(path)
    except Exception:
        return []
    traits = [
        t for t in store.values() if int(getattr(t, "evidence_count", 0)) >= int(min_evidence)
    ]
    traits.sort(key=lambda t: (float(t.strength), int(t.evidence_count)), reverse=True)
    return traits


def upsert_trait(
    trait: Trait,
    path: str | Path | None = None,
    *,
    cycle_id: str | None = None,
) -> bool:
    """Commit one established Trait into the canonical preference surface.

    Fail-hard: an identity write failure CRASHES — a trait update must not be
    silently lost."""
    from pressure_loop.policy import deepsleep_identity_promote_evidence

    if int(trait.evidence_count) < deepsleep_identity_promote_evidence():
        return False
    _ensure_legacy_import(_legacy_path(path))
    trait_key = normalize_trait_name(trait.name)
    _write_canonical_trait(
        Trait(
            name=trait_key,
            strength=float(trait.strength),
            evidence_count=int(trait.evidence_count),
            # chat-reflection metadata rides through to the canonical record —
            # _trait_envelope reads these; rebuilding the Trait without them
            # silently stripped every pattern statement + status on write.
            evidence=str(getattr(trait, "evidence", "") or ""),
            status=str(getattr(trait, "status", "") or ""),
            observation_count=int(getattr(trait, "observation_count", 0) or 0),
        ),
        cycle_id=cycle_id or f"deepsleep-trait:{trait_key}",
    )
    return True
