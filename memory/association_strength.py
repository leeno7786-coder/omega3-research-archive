"""Durable structural/experiential association strength sidecar.

Canonical memory and compiler graph edges remain factual/projection truth. This
sidecar stores only bounded accessibility/usefulness signals and may be mutated
only from a DeepSleep-owned update scope.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

ASSOCIATION_SCHEMA_VERSION = "omega.association_strength.v1"
ASSOCIATION_RECORD_SCHEMA_VERSION = "omega.association_strength_record.v1"
ASSOCIATION_OUTCOME_SCHEMA_VERSION = "omega.association_outcome.v1"
STRUCTURAL_PROVENANCE_CLASS = "structural"
STATUS_ACTIVE = "active"
STATUS_DORMANT = "dormant"
STATUS_RETIRED = "retired"
COMBINE_ALPHA = 0.6
COMBINE_BETA = 0.4
ASSOCIATION_DECAY_TAU_SECONDS = 7 * 24 * 3600.0
DORMANT_THRESHOLD = 0.05
RETIRE_THRESHOLD = 0.01

_MUTATION = threading.local()


class AssociationSidecarError(RuntimeError):
    """The adaptive sidecar could not be read or durably updated."""


class AssociationSidecarUnreadable(AssociationSidecarError):
    """The sidecar exists but its schema or bytes cannot be trusted."""


class AssociationMutationOutsideDeepSleep(AssociationSidecarError):
    """A caller attempted to mutate adaptive state outside the terminus."""


class AssociationRelationError(ValueError):
    """The relation type is not part of the current compiler vocabulary."""


class AssociationIntegrityError(ValueError):
    """The association endpoints or provenance are malformed."""


def association_path() -> Path:
    from core.paths import GUARDIAN_STATE_ROOT

    return Path(GUARDIAN_STATE_ROOT) / "association_strength.json"


def association_generation(persist_path: Path | str | None = None) -> str:
    """Return the deterministic content generation of the sidecar.

    ``updated_at`` is deliberately excluded. The value identifies the adaptive
    records that a projection was built against, not when the file was written.
    """
    path = Path(persist_path) if persist_path is not None else association_path()
    index = AssociationStrengthIndex(path)
    with index._lock:
        payload = {
            "schema_version": ASSOCIATION_SCHEMA_VERSION,
            "records": {key: dict(index._records[key]) for key in sorted(index._records)},
        }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@contextmanager
def deep_sleep_mutation() -> Iterator[None]:
    """Authorize one synchronous adaptive mutation batch."""
    prior = bool(getattr(_MUTATION, "active", False))
    _MUTATION.active = True
    try:
        yield
    finally:
        _MUTATION.active = prior


def _require_mutation() -> None:
    if not bool(getattr(_MUTATION, "active", False)):
        raise AssociationMutationOutsideDeepSleep(
            "association strength may be mutated only during DeepSleep"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _status_for(record: Mapping[str, Any]) -> str:
    if int(record.get("support_count") or 0) > 0:
        return STATUS_ACTIVE
    effective = effective_strength(
        structural=float(record.get("structural_strength") or 0.0),
        experiential=float(record.get("experiential_strength") or 0.0),
    )
    if effective < RETIRE_THRESHOLD:
        return STATUS_RETIRED
    if effective < DORMANT_THRESHOLD:
        return STATUS_DORMANT
    return STATUS_ACTIVE


def _relation_types() -> tuple[frozenset[str], frozenset[str]]:
    from memory.dme.hop_engine import STRONG_EDGE_TYPES, WEAK_EDGE_TYPES

    return frozenset(STRONG_EDGE_TYPES), frozenset(WEAK_EDGE_TYPES)


def association_id_for(
    source_id: str,
    relation_type: str,
    target_id: str,
    provenance_class: str,
    schema_version: str = ASSOCIATION_SCHEMA_VERSION,
) -> str:
    strong, weak = _relation_types()
    if relation_type not in strong | weak:
        raise AssociationRelationError(f"unlawful relation type: {relation_type!r}")
    source = str(source_id or "").strip()
    target = str(target_id or "").strip()
    provenance = str(provenance_class or "").strip()
    if not source or not target or source == target:
        raise AssociationIntegrityError("association endpoints must be distinct and nonempty")
    if not provenance:
        raise AssociationIntegrityError("association provenance_class is required")
    if relation_type in weak and source > target:
        source, target = target, source
    identity = json.dumps(
        {
            "schema_version": schema_version,
            "source_id": source,
            "relation_type": relation_type,
            "target_id": target,
            "provenance_class": provenance,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def effective_strength(*, structural: float, experiential: float) -> float:
    return _clamp(COMBINE_ALPHA * float(structural) + COMBINE_BETA * float(experiential))


class AssociationStrengthIndex:
    """JSON-backed adaptive association state with DeepSleep-only mutation."""

    def __init__(self, persist_path: Path | str | None = None):
        self._path = Path(persist_path) if persist_path is not None else association_path()
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AssociationSidecarUnreadable(f"association sidecar is unreadable: {self._path}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != ASSOCIATION_SCHEMA_VERSION:
            raise AssociationSidecarUnreadable(
                f"association sidecar schema mismatch: {self._path}"
            )
        records = raw.get("records")
        if not isinstance(records, dict):
            raise AssociationSidecarUnreadable("association sidecar records are not an object")
        loaded: dict[str, dict[str, Any]] = {}
        for key, value in records.items():
            if not isinstance(value, dict):
                raise AssociationSidecarUnreadable("association sidecar record is not an object")
            record = dict(value)
            if str(record.get("association_id") or "") != str(key):
                raise AssociationSidecarUnreadable("association sidecar record key does not match association_id")
            try:
                if record.get("schema_version") != ASSOCIATION_RECORD_SCHEMA_VERSION:
                    raise ValueError("record schema mismatch")
                _nonnegative_int(record.get("support_count"), "support_count")
                expected = association_id_for(
                    str(record.get("source_id") or ""),
                    str(record.get("relation_type") or ""),
                    str(record.get("target_id") or ""),
                    str(record.get("provenance_class") or ""),
                )
                if expected != str(key):
                    raise ValueError("record identity mismatch")
            except (TypeError, ValueError, AssociationSidecarError) as exc:
                raise AssociationSidecarUnreadable(
                    f"association sidecar record is invalid: {key}"
                ) from exc
            loaded[str(key)] = record
        self._records = loaded

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "schema_version": ASSOCIATION_SCHEMA_VERSION,
            "updated_at": _now(),
            "records": self._records,
        }
        try:
            with temp.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.flush()
                import os

                os.fsync(stream.fileno())
            temp.replace(self._path)
        except OSError as exc:
            raise AssociationSidecarError(
                f"association sidecar could not be saved: {self._path}"
            ) from exc

    def _mutated(self) -> None:
        _require_mutation()
        self._save()

    def register(
        self,
        source_id: str,
        relation_type: str,
        target_id: str,
        *,
        provenance_class: str,
        structural_strength: float = 0.0,
        experiential_strength: float = 0.0,
        support_count: int = 0,
        encode_tau: float = ASSOCIATION_DECAY_TAU_SECONDS,
    ) -> str:
        with self._lock:
            _require_mutation()
            association_id = association_id_for(
                source_id,
                relation_type,
                target_id,
                provenance_class,
            )
            if association_id in self._records:
                return association_id
            tau = _finite(encode_tau, "encode_tau")
            if tau <= 0.0:
                raise ValueError("encode_tau must be positive")
            support = _nonnegative_int(support_count, "support_count")
            source = str(source_id).strip()
            target = str(target_id).strip()
            _, weak = _relation_types()
            if relation_type in weak and source > target:
                source, target = target, source
            self._records[association_id] = {
                "association_id": association_id,
                "source_id": source,
                "target_id": target,
                "relation_type": relation_type,
                "provenance_class": str(provenance_class).strip(),
                "structural_strength": _clamp(_finite(structural_strength, "structural_strength")),
                "experiential_strength": _clamp(_finite(experiential_strength, "experiential_strength")),
                "support_count": support,
                "activation_count": 0,
                "positive_credit": 0.0,
                "negative_credit": 0.0,
                "ingested_at": _now(),
                "last_activation": None,
                "encode_tau": tau,
                "status": STATUS_ACTIVE,
                "schema_version": ASSOCIATION_RECORD_SCHEMA_VERSION,
            }
            self._mutated()
            return association_id

    def reconcile_structural(self, rows: Any) -> dict[str, int]:
        """Reconcile compiler evidence without changing experiential credit."""
        with self._lock:
            _require_mutation()
            desired: dict[str, dict[str, Any]] = {}
            for raw in rows or ():
                if not isinstance(raw, Mapping):
                    raise AssociationSidecarError("structural association row is not an object")
                source_id = str(raw.get("source_id") or "").strip()
                target_id = str(raw.get("target_id") or "").strip()
                relation_type = str(raw.get("relation_type") or "").strip()
                provenance_class = str(raw.get("provenance_class") or "").strip()
                if provenance_class != STRUCTURAL_PROVENANCE_CLASS:
                    raise AssociationIntegrityError(
                        "structural reconciliation requires structural provenance"
                    )
                support_count = _nonnegative_int(raw.get("support_count"), "support_count")
                if support_count <= 0:
                    raise AssociationIntegrityError("structural support_count must be positive")
                association_id = association_id_for(
                    source_id,
                    relation_type,
                    target_id,
                    provenance_class,
                )
                supplied_id = str(raw.get("association_id") or "")
                if supplied_id and supplied_id != association_id:
                    raise AssociationIntegrityError("structural association_id does not match its identity")
                _, weak = _relation_types()
                if relation_type in weak and source_id > target_id:
                    source_id, target_id = target_id, source_id
                desired[association_id] = {
                    "association_id": association_id,
                    "source_id": source_id,
                    "target_id": target_id,
                    "relation_type": relation_type,
                    "provenance_class": provenance_class,
                    "support_count": support_count,
                    "structural_strength": _clamp(
                        _finite(raw.get("structural_strength", 1.0), "structural_strength")
                    ),
                }

            changed = 0
            retired = 0
            for association_id, desired_record in desired.items():
                current = self._records.get(association_id)
                if current is None:
                    now = _now()
                    self._records[association_id] = {
                        **desired_record,
                        "experiential_strength": 0.0,
                        "activation_count": 0,
                        "positive_credit": 0.0,
                        "negative_credit": 0.0,
                        "ingested_at": now,
                        "last_activation": None,
                        "encode_tau": ASSOCIATION_DECAY_TAU_SECONDS,
                        "status": STATUS_ACTIVE,
                        "schema_version": ASSOCIATION_RECORD_SCHEMA_VERSION,
                    }
                    changed += 1
                    continue
                if current.get("provenance_class") != STRUCTURAL_PROVENANCE_CLASS:
                    raise AssociationIntegrityError(
                        f"association identity collides with non-structural record: {association_id}"
                    )
                for field in ("source_id", "target_id", "relation_type", "provenance_class"):
                    if current.get(field) != desired_record[field]:
                        raise AssociationIntegrityError(
                            f"association identity drift for {association_id}: {field}"
                        )
                before = (
                    current.get("support_count"),
                    current.get("structural_strength"),
                    current.get("status"),
                )
                current.update(
                    support_count=desired_record["support_count"],
                    structural_strength=desired_record["structural_strength"],
                )
                current["status"] = _status_for(current)
                if before != (
                    current["support_count"],
                    current["structural_strength"],
                    current["status"],
                ):
                    changed += 1

            for association_id, current in self._records.items():
                if current.get("provenance_class") != STRUCTURAL_PROVENANCE_CLASS:
                    continue
                if association_id in desired:
                    continue
                before = (current.get("support_count"), current.get("structural_strength"), current.get("status"))
                current["support_count"] = 0
                current["structural_strength"] = 0.0
                current["status"] = _status_for(current)
                if current["status"] == STATUS_RETIRED and before[2] != STATUS_RETIRED:
                    retired += 1
                if before != (current["support_count"], current["structural_strength"], current["status"]):
                    changed += 1

            if changed:
                self._mutated()
            return {"reconciled": len(desired), "changed": changed, "retired": retired}

    def get_record(self, association_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(str(association_id or ""))
            return None if record is None else dict(record)

    def snapshot_records(self) -> dict[str, dict[str, Any]]:
        """Return a read-only-shaped copy for disposable projection builders."""
        with self._lock:
            return {key: dict(value) for key, value in self._records.items()}

    def get_effective_strength(self, association_id: str) -> float:
        record = self.get_record(association_id)
        if record is None:
            return 0.0
        return effective_strength(
            structural=record["structural_strength"],
            experiential=record["experiential_strength"],
        )

    def touch(self, association_id: str) -> int | None:
        with self._lock:
            _require_mutation()
            record = self._records.get(str(association_id or ""))
            if record is None:
                return None
            record["activation_count"] = int(record.get("activation_count") or 0) + 1
            record["last_activation"] = _now()
            self._mutated()
            return int(record["activation_count"])

    def reinforce(
        self,
        association_id: str,
        delta: float,
        *,
        eligibility: float,
        verified: bool,
    ) -> float | None:
        with self._lock:
            _require_mutation()
            if not verified or _finite(eligibility, "eligibility") <= 0.0:
                return None
            record = self._records.get(str(association_id or ""))
            if record is None:
                return None
            amount = _finite(delta, "delta") * _clamp(_finite(eligibility, "eligibility"))
            record["experiential_strength"] = _clamp(
                float(record["experiential_strength"]) + amount
            )
            if amount >= 0.0:
                record["positive_credit"] += amount
            else:
                record["negative_credit"] += abs(amount)
            record["last_activation"] = _now()
            self._mutated()
            return float(record["experiential_strength"])

    def decay(self, *, elapsed_seconds: float) -> int:
        with self._lock:
            _require_mutation()
            elapsed = _finite(elapsed_seconds, "elapsed_seconds")
            if elapsed < 0.0:
                raise ValueError("elapsed_seconds must not be negative")
            changed = 0
            for record in self._records.values():
                if record["status"] == STATUS_RETIRED:
                    continue
                old = float(record["experiential_strength"])
                tau = float(record["encode_tau"])
                value = old * math.exp(-elapsed / tau) if tau > 0.0 else 0.0
                record["experiential_strength"] = _clamp(value)
                effective = self.get_effective_strength(record["association_id"])
                if effective < RETIRE_THRESHOLD and float(record["structural_strength"]) <= 0.0:
                    record["status"] = STATUS_RETIRED
                elif effective < DORMANT_THRESHOLD:
                    record["status"] = STATUS_DORMANT
                else:
                    record["status"] = STATUS_ACTIVE
                if value != old:
                    changed += 1
            if changed:
                self._mutated()
            return changed


def apply_deepsleep_updates(updates: Any) -> dict[str, Any]:
    """Apply explicit association updates in one DeepSleep-owned batch."""
    index = AssociationStrengthIndex()
    result = {"registered": 0, "reconciled": 0, "credited": 0, "decayed": 0, "retired": 0, "skipped": []}
    structural_rows: list[Mapping[str, Any]] = []
    with deep_sleep_mutation():
        for raw in updates or ():
            if not isinstance(raw, Mapping):
                raise AssociationSidecarError("association update is not an object")
            operation = str(raw.get("operation") or "")
            if operation == "register":
                index.register(
                    str(raw.get("source_id") or ""),
                    str(raw.get("relation_type") or ""),
                    str(raw.get("target_id") or ""),
                    provenance_class=str(raw.get("provenance_class") or ""),
                    structural_strength=float(raw.get("structural_strength") or 0.0),
                    experiential_strength=float(raw.get("experiential_strength") or 0.0),
                    support_count=int(raw.get("support_count") or 0),
                )
                result["registered"] += 1
            elif operation == "reconcile_structural":
                structural_rows.append(raw)
            elif operation == "credit":
                value = index.reinforce(
                    str(raw.get("association_id") or ""),
                    float(raw.get("delta") or 0.0),
                    eligibility=float(raw.get("eligibility") or 0.0),
                    verified=bool(raw.get("verified")),
                )
                if value is None:
                    result["skipped"].append(str(raw.get("association_id") or "missing"))
                else:
                    result["credited"] += 1
            elif operation == "decay":
                result["decayed"] += index.decay(
                    elapsed_seconds=float(raw.get("elapsed_seconds") or 0.0)
                )
            else:
                raise AssociationSidecarError(f"unknown association update: {operation!r}")
        if structural_rows:
            reconciliation = index.reconcile_structural(structural_rows)
            result["reconciled"] = reconciliation["reconciled"]
            result["retired"] = reconciliation["retired"]
    return result


def structural_updates_from_edges(edges: Any) -> list[dict[str, Any]]:
    """Collapse compiler edges into deterministic structural associations."""
    strong, weak = _relation_types()
    grouped: dict[str, dict[str, Any]] = {}
    for edge in edges or ():
        if isinstance(edge, Mapping):
            edge_id = str(edge.get("edge_id") or "")
            source_id = str(edge.get("source_id") or "")
            target_id = str(edge.get("target_id") or "")
            relation_type = str(edge.get("edge_type") or "")
            evidence_event_id = str(edge.get("evidence_event_id") or "")
        else:
            edge_id = str(getattr(edge, "edge_id", "") or "")
            source_id = str(getattr(edge, "source_id", "") or "")
            target_id = str(getattr(edge, "target_id", "") or "")
            relation_type = str(getattr(edge, "edge_type", "") or "")
            evidence_event_id = str(getattr(edge, "evidence_event_id", "") or "")
        if relation_type not in strong | weak:
            continue
        if not edge_id or not evidence_event_id:
            raise AssociationIntegrityError("structural edge requires edge_id and evidence_event_id")
        association_id = association_id_for(
            source_id,
            relation_type,
            target_id,
            STRUCTURAL_PROVENANCE_CLASS,
        )
        source, target = source_id, target_id
        if relation_type in weak and source > target:
            source, target = target, source
        group = grouped.setdefault(
            association_id,
            {
                "operation": "reconcile_structural",
                "association_id": association_id,
                "source_id": source,
                "target_id": target,
                "relation_type": relation_type,
                "provenance_class": STRUCTURAL_PROVENANCE_CLASS,
                "structural_strength": 1.0,
                "evidence_event_ids": set(),
            },
        )
        group["evidence_event_ids"].add(evidence_event_id)
    updates = []
    for association_id in sorted(grouped):
        row = grouped[association_id]
        evidence_ids = sorted(row.pop("evidence_event_ids"))
        row["support_count"] = len(evidence_ids)
        row["evidence_event_ids"] = evidence_ids
        updates.append(row)
    return updates


def build_verified_use_receipt(
    results: Iterable[Mapping[str, Any]] | None,
    used_memory_ids: Iterable[str],
) -> dict[str, Any] | None:
    """Capture the verified evidence actually handed to the answer/action."""
    used = {str(memory_id or "").strip() for memory_id in used_memory_ids or () if str(memory_id or "").strip()}
    if not used:
        return None
    verified_ids: set[str] = set()
    generations: set[str] = set()
    associations: dict[str, dict[str, Any]] = {}
    for result in results or ():
        if not isinstance(result, Mapping) or str(result.get("verdict") or "").lower() != "verified":
            continue
        for evidence in result.get("evidence") or ():
            if not isinstance(evidence, Mapping):
                continue
            memory_id = str(evidence.get("memory_id") or evidence.get("id") or "").strip()
            if memory_id in used:
                verified_ids.add(memory_id)
        receipt = result.get("receipt")
        traces = receipt.get("dme_activation_traces") if isinstance(receipt, Mapping) else ()
        for trace in traces or ():
            if not isinstance(trace, Mapping):
                continue
            generation = str(trace.get("projection_generation") or "").strip()
            if generation:
                generations.add(generation)
            for association in trace.get("associations") or ():
                if not isinstance(association, Mapping):
                    continue
                if association.get("attribution_status") != "proven":
                    continue
                association_id = str(association.get("association_id") or "").strip()
                source_id = str(association.get("source_id") or "").strip()
                target_id = str(association.get("target_id") or "").strip()
                if not association_id or not (source_id in used or target_id in used):
                    continue
                associations[association_id] = {
                    "association_id": association_id,
                    "source_id": source_id,
                    "target_id": target_id,
                    "relation_type": str(association.get("relation_type") or ""),
                    "provenance_class": str(association.get("provenance_class") or ""),
                    "activation_strength": _clamp(
                        _finite(association.get("activation_strength", 0.0), "activation strength")
                    ),
                    "attribution_status": "proven",
                }
    if not verified_ids:
        return None
    return {
        "schema_version": ASSOCIATION_OUTCOME_SCHEMA_VERSION,
        "verified": True,
        "used_memory_ids": sorted(used),
        "verified_memory_ids": sorted(verified_ids),
        "associations": [associations[key] for key in sorted(associations)],
        "projection_generations": sorted(generations),
        "relevance": 1.0,
        "delta": 0.0,
        "outcome_kind": "",
    }


def finalize_verified_use_receipt(receipt: Mapping[str, Any], neuromod: Mapping[str, Any]) -> dict[str, Any]:
    """Bind verified-use evidence to the episode's surprise outcome."""
    finalized = dict(receipt or {})
    dopamine = _finite((neuromod or {}).get("dopamine", 0.0), "dopamine")
    from autonomic.neuromod_config import dopamine_thresholds

    positive, negative = dopamine_thresholds()
    surprising = dopamine <= float(negative) or dopamine >= float(positive)
    associations = list(finalized.get("associations") or ())
    finalized["schema_version"] = ASSOCIATION_OUTCOME_SCHEMA_VERSION
    finalized["outcome_kind"] = str((neuromod or {}).get("cycle_kind") or "")
    finalized["delta"] = dopamine if surprising else 0.0
    finalized["verified"] = bool(finalized.get("verified")) and bool(
        finalized.get("verified_memory_ids")
    ) and bool(associations) and surprising
    return finalized


def updates_from_verified_episode(episode: Any) -> list[dict[str, Any]]:
    """Build credit updates from an explicit verified episode outcome.

    The outcome is intentionally not inferred from recall success, dopamine,
    or task completion. AUTO provides the bounded ``association_outcome``
    receipt after verified evidence was actually used; DeepSleep alone applies
    the resulting updates.
    """
    auto = getattr(episode, "auto", None) or {}
    outputs = auto.get("outputs") if isinstance(auto, Mapping) else getattr(auto, "outputs", {})
    outcome = outputs.get("association_outcome") if isinstance(outputs, Mapping) else None
    if not isinstance(outcome, Mapping) or outcome.get("verified") is not True:
        return []
    relevance = _clamp(_finite(outcome.get("relevance", 0.0), "association relevance"))
    delta = _finite(outcome.get("delta", 0.0), "association delta")
    if relevance <= 0.0 or delta == 0.0:
        return []
    explicit_associations = outcome.get("associations")
    results = outputs.get("remembered_knowledge_tool_results") if isinstance(outputs, Mapping) else ()
    updates: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(explicit_associations, (list, tuple)):
        traces = ({"associations": explicit_associations},)
    else:
        traces = tuple(
            trace
            for result in results or ()
            if isinstance(result, Mapping)
            for receipt in (result.get("receipt"),)
            if isinstance(receipt, Mapping)
            for trace in (receipt.get("dme_activation_traces") or ())
        )
    for trace in traces:
        if not isinstance(trace, Mapping):
            continue
        for association in trace.get("associations") or ():
            if not isinstance(association, Mapping):
                continue
            if association.get("attribution_status") != "proven":
                continue
            association_id = str(association.get("association_id") or "").strip()
            if not association_id or association_id in seen:
                continue
            seen.add(association_id)
            activation = _clamp(
                _finite(association.get("activation_strength", 0.0), "activation strength")
            )
            updates.append(
                {
                    "operation": "credit",
                    "association_id": association_id,
                    "delta": delta,
                    "eligibility": activation * relevance,
                    "verified": True,
                }
            )
    return updates


__all__ = [
    "AssociationIntegrityError",
    "AssociationMutationOutsideDeepSleep",
    "AssociationRelationError",
    "AssociationSidecarError",
    "AssociationSidecarUnreadable",
    "AssociationStrengthIndex",
    "ASSOCIATION_OUTCOME_SCHEMA_VERSION",
    "ASSOCIATION_RECORD_SCHEMA_VERSION",
    "STRUCTURAL_PROVENANCE_CLASS",
    "STATUS_ACTIVE",
    "STATUS_DORMANT",
    "STATUS_RETIRED",
    "association_id_for",
    "association_generation",
    "association_path",
    "apply_deepsleep_updates",
    "build_verified_use_receipt",
    "deep_sleep_mutation",
    "effective_strength",
    "finalize_verified_use_receipt",
    "structural_updates_from_edges",
    "updates_from_verified_episode",
]
