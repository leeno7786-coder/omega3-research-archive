"""Snapshot, ten-member manifest, history audit, metrics and the evidence packet.

CONTRACTS.md section 4. Two absences shape this module.

**There is no as-of read today.** ``pressure_loop/temporal_store.hydrate_reflection_window``
scans the current lobe-9 subdomain and keeps the greatest ``(closed_at, id)`` per episode, so
a sealed window is not bound to a retained generation and a later rewrite silently changes
what it reflects on. :class:`SnapshotRef` is the binding that makes an as-of read expressible;
WO-09 and WO-10 make it real.

**There is no token counter, and this module does not pretend to one.**
:meth:`WindowEvidencePacket.mandatory_item_count` counts STRUCTURAL ITEMS and says nothing
about tokens. An earlier version called it a token floor on the claim that every item costs
at least one token under every tokenizer; that claim is not established (a token may span
more than one serialized item), so it was withdrawn rather than defended. Verified
whole-request token accounting is WO-12's, through an identified counter, and its absence
raises ``TokenAccountingUnavailable`` there rather than being papered over here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from schema.continuity_json import (
    CONTINUITY_SCHEMA_VERSION,
    CanonicalRef,
    ContinuityContractError,
    CoverageClockRef,
    Observation,
    WindowCapacityExceeded,
    WindowMembershipError,
    WindowReferenceError,
    content_hash,
    freeze,
    require_enum,
    require_hex64,
    require_keys,
    require_non_negative_int,
    require_positive_int,
    require_string_keys,
    require_text,
    register_contract_type,
    unfreeze,
)
from schema.episode_record import (
    ACTION_STATUSES,
    CASCADE_STATUSES,
    FEEDBACK_STATUSES,
    MEMORY_COMMIT_STATUSES,
    TASK_OUTCOMES,
    VERIFICATION_STATUSES,
)

SCHEMA_VERSION = f"{CONTINUITY_SCHEMA_VERSION}.window_evidence"

WINDOW_PACKET_BLOCKS: tuple[str, ...] = (
    "schema_version",
    "manifest",
    "baseline",
    "episodes",
    "temporal_links",
    "historical_support",
    "learning_followthrough",
    "open_threads",
    "metrics",
    "limitations",
    "evidence_table",
)

#: Mirrors the configured reflection window size (config/reflection_window.yaml:
#: reflection_window.window_size = 10). This module holds no configuration reader — WO-09
#: owns asserting the live configured value against this constant at the producer.
FOCAL_MEMBER_COUNT = 10

WINDOW_SCOPES: tuple[str, ...] = ("focal", "historical_support")

#: A member whose canonical publication is incomplete is never represented as a member.
_PUBLISHED_COMMIT_STATES = frozenset({"canonical_committed", "recall_published"})

COUNTEREXAMPLE_SEARCH_STATES: tuple[str, ...] = (
    "search_not_run",
    "searched_no_matches",
    "searched_found_matches",
    "reader_failed",
)

SUPPORT_CLASSES: tuple[str, ...] = ("historical", "prior_finding", "direction_change")

UNCERTAINTY_LEVELS: tuple[str, ...] = ("low", "medium", "high", "unknown")

EVIDENCE_GRADES: tuple[str, ...] = ("observed_regularity", "hypothesis", "tested_association")

_SNAPSHOT_REQUIRED = (
    "generation_id",
    "coverage_clock",
    "visibility_cutoff",
    "trajectory_revision_at_start",
    "trajectory_revision_at_cutoff",
    "authority_snapshot_ref",
    "reader_policy_revision",
)

_MEMBER_REQUIRED = (
    "episode_id",
    "ref",
    "finish_line_commit_seq",
    "cascade_status",
    "memory_commit_status",
)

_AUDIT_REQUIRED = (
    "query_policy_revision",
    "scope",
    "considered_ids",
    "returned_ids",
    "admitted_ids",
    "omitted",
    "counterexample_search_status",
    "cap",
)

_METRIC_REQUIRED = (
    "metric_id",
    "numerator",
    "denominator",
    "population_refs",
    "excluded",
    "unknown",
    "stratification",
    "computation_version",
)

#: WO-11. A measured row has a real denominator — WO-02's refusal of a zero one stands. The only
#: way a row may carry denominator 0 is to declare itself an **undefined observation**, which
#: reports its counts (``excluded`` / ``unknown``) and says why, and can never be read as 0, 0.5
#: or 100%.
METRIC_OBSERVATIONS: tuple[str, ...] = ("measured", "undefined")

_METRIC_OPTIONAL = (
    "observation",
    "reason",
    "note",
    "numerator_refs",
    "omissions",
    "measurement",
)

_MEASUREMENT_REQUIRED = ("units", "clock_scope", "total")

_EVIDENCE_ROW_REQUIRED = ("evidence_class", "attributed_to", "value")


# ── SnapshotRef ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SnapshotRef:
    """The retained generation an as-of read is pinned to.

    Every field names an authority that already exists: the generation id and coverage clock
    come from ``memory/projection/generation.py``, the trajectory revisions from
    ``pressure_loop/temporal_store.py``, the authority snapshot from the connection contract.
    Nothing here is a new identity.
    """

    generation_id: str
    coverage_clock: CoverageClockRef
    visibility_cutoff: str
    trajectory_revision_at_start: str
    trajectory_revision_at_cutoff: str
    authority_snapshot_ref: str
    reader_policy_revision: str
    unsequenced_inventory_digest: str = ""

    def __post_init__(self) -> None:
        require_hex64(self.generation_id, where="snapshot.generation_id")
        clock = self.coverage_clock
        if not isinstance(clock, CoverageClockRef):
            clock = CoverageClockRef.from_dict(clock)
        object.__setattr__(self, "coverage_clock", clock)
        for name in (
            "visibility_cutoff",
            "trajectory_revision_at_start",
            "trajectory_revision_at_cutoff",
            "authority_snapshot_ref",
            "reader_policy_revision",
        ):
            require_text(getattr(self, name), where=f"snapshot.{name}", limit=256)
        if self.unsequenced_inventory_digest:
            require_hex64(
                self.unsequenced_inventory_digest, where="snapshot.unsequenced_inventory_digest"
            )

    def covers(self, clock: Any) -> bool:
        """Identity then number: a different clock epoch is a regression, whatever the integers."""
        other = clock if isinstance(clock, CoverageClockRef) else CoverageClockRef.from_dict(clock)
        return self.coverage_clock.covers(other)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "generation_id": self.generation_id,
            "coverage_clock": self.coverage_clock.to_dict(),
            "visibility_cutoff": self.visibility_cutoff,
            "trajectory_revision_at_start": self.trajectory_revision_at_start,
            "trajectory_revision_at_cutoff": self.trajectory_revision_at_cutoff,
            "authority_snapshot_ref": self.authority_snapshot_ref,
            "reader_policy_revision": self.reader_policy_revision,
        }
        if self.unsequenced_inventory_digest:
            out["unsequenced_inventory_digest"] = self.unsequenced_inventory_digest
        return out

    @classmethod
    def from_dict(cls, value: Any) -> "SnapshotRef":
        data = require_keys(
            value,
            required=_SNAPSHOT_REQUIRED,
            optional=("unsequenced_inventory_digest",),
            where="snapshot",
        )
        data["coverage_clock"] = CoverageClockRef.from_dict(data["coverage_clock"])
        return cls(**data)

    @classmethod
    def coerce(cls, value: Any) -> "SnapshotRef":
        return value if isinstance(value, cls) else cls.from_dict(value)


# ── WindowManifest ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WindowManifest:
    """The sealed cohort: exactly ten fresh, unique, eligible, non-window members."""

    window_id: str
    window_version: int
    configuration_fingerprint: str
    scope: str
    snapshot: SnapshotRef
    complete: bool
    members: tuple[Mapping[str, Any], ...]
    expected_member_count: int = FOCAL_MEMBER_COUNT

    def __post_init__(self) -> None:
        require_text(self.window_id, where="manifest.window_id", limit=256)
        require_positive_int(self.window_version, where="manifest.window_version")
        require_hex64(self.configuration_fingerprint, where="manifest.configuration_fingerprint")
        require_enum(self.scope, allowed=WINDOW_SCOPES, where="manifest.scope")
        object.__setattr__(self, "snapshot", SnapshotRef.coerce(self.snapshot))
        if not isinstance(self.complete, bool):
            raise ContinuityContractError("manifest.complete must be a boolean")

        if not isinstance(self.members, (list, tuple)):
            raise ContinuityContractError("manifest.members must be a JSON array")
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, row in enumerate(self.members):
            data = require_keys(row, required=_MEMBER_REQUIRED, where=f"manifest.members[{index}]")
            episode_id = require_text(
                data["episode_id"], where=f"manifest.members[{index}].episode_id", limit=256
            )
            if episode_id in seen:
                raise WindowMembershipError(
                    f"member {episode_id!r} appears twice; a cohort entry is unique"
                )
            if episode_id == self.window_id:
                raise WindowMembershipError(
                    "a window cannot be its own member: a reflection is not fresh experience"
                )
            seen.add(episode_id)
            data["ref"] = CanonicalRef.coerce(data["ref"], where=f"manifest.members[{index}].ref")
            require_non_negative_int(
                data["finish_line_commit_seq"], where=f"manifest.members[{index}].finish_line_commit_seq"
            )
            require_enum(
                data["cascade_status"],
                allowed=CASCADE_STATUSES,
                where=f"manifest.members[{index}].cascade_status",
            )
            commit_status = require_enum(
                data["memory_commit_status"],
                allowed=MEMORY_COMMIT_STATUSES,
                where=f"manifest.members[{index}].memory_commit_status",
            )
            if commit_status not in _PUBLISHED_COMMIT_STATES:
                raise WindowMembershipError(
                    f"member {episode_id!r} is {commit_status!r}: partial canonical publication "
                    "is never represented as a complete member"
                )
            rows.append(data)

        if self.scope == "focal" and len(rows) != self.expected_member_count:
            raise WindowMembershipError(
                f"a focal cohort is exactly {self.expected_member_count} members, got {len(rows)}; "
                "an already sealed cohort is never silently shrunk"
            )
        object.__setattr__(self, "members", freeze(rows))

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def member_ids(self) -> tuple[str, ...]:
        return tuple(str(row["episode_id"]) for row in self.members)

    @property
    def member_refs(self) -> tuple[CanonicalRef, ...]:
        return tuple(row["ref"] for row in self.members)

    def member_outcome(self, episode_id: str) -> str:
        for row in self.members:
            if row["episode_id"] == episode_id:
                return str(row["cascade_status"])
        raise WindowMembershipError(f"{episode_id!r} is not a member of {self.window_id!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "window_version": self.window_version,
            "configuration_fingerprint": self.configuration_fingerprint,
            "scope": self.scope,
            "snapshot": self.snapshot.to_dict(),
            "complete": self.complete,
            "members": unfreeze(self.members),
            "expected_member_count": self.expected_member_count,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "WindowManifest":
        data = require_keys(
            value,
            required=(
                "window_id",
                "window_version",
                "configuration_fingerprint",
                "scope",
                "snapshot",
                "complete",
                "members",
            ),
            optional=("expected_member_count",),
            where="manifest",
        )
        # Host POLICY, not data (WO-02A C2/C6): a stored manifest may restate the expected
        # count for round-trip fidelity, but it may never lower its own bar on reload.
        if "expected_member_count" in data and data.get("scope") == "focal":
            if data["expected_member_count"] != FOCAL_MEMBER_COUNT:
                raise WindowMembershipError(
                    "manifest.expected_member_count "
                    f"{data['expected_member_count']!r} does not match the configured focal "
                    f"count {FOCAL_MEMBER_COUNT}; a serialized manifest cannot lower its own bar"
                )
        data["snapshot"] = SnapshotRef.from_dict(data["snapshot"])
        # NOT tuple(...): the declared type is checked in __post_init__, so deserialization
        # and direct construction enforce the same rule (WO-02A C2).
        return cls(**data)

    @classmethod
    def coerce(cls, value: Any) -> "WindowManifest":
        return value if isinstance(value, cls) else cls.from_dict(value)


# ── history ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HistoryLimits:
    """A positive candidate cap plus the retrieval policy revision that produced it."""

    record_cap: int
    token_candidate_cap: int
    retrieval_policy_revision: str

    def __post_init__(self) -> None:
        require_positive_int(self.record_cap, where="history_limits.record_cap")
        require_positive_int(self.token_candidate_cap, where="history_limits.token_candidate_cap")
        require_text(
            self.retrieval_policy_revision,
            where="history_limits.retrieval_policy_revision",
            limit=256,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_cap": self.record_cap,
            "token_candidate_cap": self.token_candidate_cap,
            "retrieval_policy_revision": self.retrieval_policy_revision,
        }


@dataclass(frozen=True)
class HistoryBundle:
    """Separately labelled historical support, plus the audit of how it was retrieved.

    ``search_not_run`` is not ``searched_no_matches`` and neither is ``reader_failed``. A
    counterexample search that was never performed is not a search that found none.
    """

    primary_support: tuple[Mapping[str, Any], ...]
    prior_findings: tuple[Mapping[str, Any], ...]
    intervening_direction_changes: tuple[Mapping[str, Any], ...]
    links: tuple[Mapping[str, Any], ...]
    retrieval_audit: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in ("primary_support", "prior_findings", "intervening_direction_changes", "links"):
            value = getattr(self, name)
            if not isinstance(value, (list, tuple)):
                raise ContinuityContractError(f"history.{name} must be a JSON array")
            object.__setattr__(self, name, freeze(tuple(value)))
        audit = require_keys(self.retrieval_audit, required=_AUDIT_REQUIRED, where="history.retrieval_audit")
        require_text(audit["query_policy_revision"], where="history.retrieval_audit.query_policy_revision", limit=256)
        require_text(audit["scope"], where="history.retrieval_audit.scope", limit=2000)
        for name in ("considered_ids", "returned_ids", "admitted_ids", "omitted"):
            if not isinstance(audit[name], (list, tuple)):
                raise ContinuityContractError(f"history.retrieval_audit.{name} must be a JSON array")
        require_enum(
            audit["counterexample_search_status"],
            allowed=COUNTEREXAMPLE_SEARCH_STATES,
            where="history.retrieval_audit.counterexample_search_status",
        )
        require_positive_int(audit["cap"], where="history.retrieval_audit.cap")
        object.__setattr__(self, "retrieval_audit", freeze(audit))

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_support": unfreeze(self.primary_support),
            "prior_findings": unfreeze(self.prior_findings),
            "intervening_direction_changes": unfreeze(self.intervening_direction_changes),
            "links": unfreeze(self.links),
            "retrieval_audit": unfreeze(self.retrieval_audit),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "HistoryBundle":
        data = require_keys(
            value,
            required=(
                "primary_support",
                "prior_findings",
                "intervening_direction_changes",
                "links",
                "retrieval_audit",
            ),
            where="history",
        )
        return cls(**data)


# ── metrics ──────────────────────────────────────────────────────────────────


def _checked_refs(value: Any, *, where: str) -> tuple[str, ...]:
    """A reference array of nonempty strings. A metric cites ids, never free text."""
    if not isinstance(value, (list, tuple)):
        raise ContinuityContractError(f"{where} must be a JSON array")
    refs = []
    for index, ref in enumerate(value):
        if not isinstance(ref, str) or not ref.strip():
            raise ContinuityContractError(f"{where}[{index}] must be a nonempty string")
        refs.append(ref)
    return tuple(refs)


@dataclass(frozen=True)
class WindowMetrics:
    """Reproducible measurements with explicit denominators and populations."""

    computation_version: str
    rows: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        require_text(self.computation_version, where="metrics.computation_version", limit=256)
        if not isinstance(self.rows, (list, tuple)):
            raise ContinuityContractError("metrics.rows must be a JSON array")
        checked: list[dict[str, Any]] = []
        for index, row in enumerate(self.rows):
            where = f"metrics.rows[{index}]"
            data = require_keys(
                row, required=_METRIC_REQUIRED, optional=_METRIC_OPTIONAL, where=where
            )
            require_text(data["metric_id"], where=f"{where}.metric_id", limit=256)
            require_non_negative_int(data["numerator"], where=f"{where}.numerator")
            require_non_negative_int(data["excluded"], where=f"{where}.excluded")
            require_non_negative_int(data["unknown"], where=f"{where}.unknown")
            if not isinstance(data["stratification"], Mapping):
                raise ContinuityContractError(f"{where}.stratification must be an object")
            observation = data.get("observation", "measured")
            require_enum(observation, allowed=METRIC_OBSERVATIONS, where=f"{where}.observation")
            population = _checked_refs(data["population_refs"], where=f"{where}.population_refs")
            numerator_refs = (
                _checked_refs(data["numerator_refs"], where=f"{where}.numerator_refs")
                if "numerator_refs" in data else None
            )
            if observation == "undefined":
                # An undefined observation is not a measurement of zero: it states that the
                # population it would have been computed over does not exist, and reports the
                # counts it does have.
                if "reason" not in data:
                    raise ContinuityContractError(
                        f"{where} is an undefined observation and must say why (reason)"
                    )
                require_text(data["reason"], where=f"{where}.reason", limit=2000)
                if data["numerator"] or data["denominator"]:
                    raise ContinuityContractError(
                        f"{where}: an undefined observation carries no numerator and no "
                        "denominator; its counts belong in excluded/unknown"
                    )
                if population or numerator_refs:
                    raise ContinuityContractError(
                        f"{where}: an undefined observation cites no population"
                    )
                if "measurement" in data:
                    raise ContinuityContractError(
                        f"{where}: an undefined observation carries no measurement"
                    )
            else:
                require_positive_int(data["denominator"], where=f"{where}.denominator")
                if data["numerator"] > data["denominator"]:
                    raise ContinuityContractError(
                        f"{where}: a numerator above its denominator is not a measurement"
                    )
                if "reason" in data:
                    require_text(data["reason"], where=f"{where}.reason", limit=2000)
            if numerator_refs is not None:
                outside = [ref for ref in numerator_refs if ref not in set(population)]
                if outside:
                    raise ContinuityContractError(
                        f"{where}: numerator reference(s) {outside} lie outside their own "
                        "population; a numerator is a subset of what was counted"
                    )
                if len(numerator_refs) != data["numerator"]:
                    raise ContinuityContractError(
                        f"{where}: {len(numerator_refs)} numerator reference(s) do not match a "
                        f"numerator of {data['numerator']}"
                    )
            if "omissions" in data:
                omissions = data["omissions"]
                if not isinstance(omissions, (list, tuple)):
                    raise ContinuityContractError(f"{where}.omissions must be an array")
                for position, omission in enumerate(omissions):
                    entry = require_keys(
                        omission, required=("id", "reason"),
                        where=f"{where}.omissions[{position}]",
                    )
                    require_text(entry["id"], where=f"{where}.omissions[{position}].id", limit=256)
                    require_text(
                        entry["reason"], where=f"{where}.omissions[{position}].reason", limit=512
                    )
                if len(omissions) != data["excluded"] + data["unknown"]:
                    raise ContinuityContractError(
                        f"{where}: {len(omissions)} omission(s) do not account for "
                        f"{data['excluded']} excluded and {data['unknown']} unknown"
                    )
            if "note" in data:
                require_text(data["note"], where=f"{where}.note", limit=2000)
            if "measurement" in data:
                measurement = require_keys(
                    data["measurement"], required=_MEASUREMENT_REQUIRED,
                    where=f"{where}.measurement",
                )
                require_text(measurement["units"], where=f"{where}.measurement.units", limit=64)
                require_text(
                    measurement["clock_scope"], where=f"{where}.measurement.clock_scope", limit=256
                )
                require_non_negative_int(
                    measurement["total"], where=f"{where}.measurement.total"
                )
            checked.append(data)
        object.__setattr__(self, "rows", freeze(checked))

    def to_dict(self) -> dict[str, Any]:
        return {"computation_version": self.computation_version, "rows": unfreeze(self.rows)}

    @classmethod
    def from_dict(cls, value: Any) -> "WindowMetrics":
        data = require_keys(value, required=("computation_version", "rows"), where="metrics")
        return cls(computation_version=data["computation_version"], rows=data["rows"])

    @classmethod
    def coerce(cls, value: Any) -> "WindowMetrics":
        return value if isinstance(value, cls) else cls.from_dict(value)

    @property
    def metric_ids(self) -> tuple[str, ...]:
        return tuple(str(row["metric_id"]) for row in self.rows)


# ── the packet ───────────────────────────────────────────────────────────────

_BASELINE_REQUIRED = ("prior_window_ref", "trajectory_at_start", "mastery_at_start")

_EPISODE_ROW_REQUIRED = (
    "episode_id",
    "ref",
    "cascade_status",
    "action_status",
    "task_outcome",
    "verification_status",
    "feedback_status",
    "objective",
    "evidence_refs",
)

_LINK_REQUIRED = (
    "claim",
    "relation_kind",
    "episode_ids",
    "support_refs",
    "counterevidence_refs",
    "uncertainty",
)

_SUPPORT_REQUIRED = ("support_class", "ref", "as_of", "summary", "evidence_refs")

_FOLLOWTHROUGH_STATES = (
    "committed",
    "exposed",
    "reported_use",
    "behaviourally_applied",
    "eligible_opportunity",
    "evaluated_result",
)
_FOLLOWTHROUGH_REQUIRED = ("intervention_ref",) + _FOLLOWTHROUGH_STATES + ("support_refs",)

_THREAD_REQUIRED = ("thread_ref", "state", "condition", "evidence_refs")


def _citation_refs(rows: Iterable[Mapping[str, Any]], *keys: str) -> set[str]:
    cited: set[str] = set()
    for row in rows:
        for key in keys:
            for ref in row.get(key) or ():
                cited.add(str(ref))
    return cited


@dataclass(frozen=True)
class WindowEvidencePacket:
    """Exactly eleven blocks. A derived view; every citation resolves to shown evidence."""

    manifest: WindowManifest
    baseline: Mapping[str, Any]
    episodes: tuple[Mapping[str, Any], ...]
    temporal_links: tuple[Mapping[str, Any], ...]
    historical_support: tuple[Mapping[str, Any], ...]
    learning_followthrough: tuple[Mapping[str, Any], ...]
    open_threads: tuple[Mapping[str, Any], ...]
    metrics: WindowMetrics
    limitations: tuple[str, ...]
    evidence_table: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest", WindowManifest.coerce(self.manifest))
        object.__setattr__(self, "metrics", WindowMetrics.coerce(self.metrics))

        baseline = require_keys(self.baseline, required=_BASELINE_REQUIRED, where="packet.baseline")
        baseline["prior_window_ref"] = Observation.coerce(
            baseline["prior_window_ref"], where="packet.baseline.prior_window_ref"
        )
        require_text(baseline["trajectory_at_start"], where="packet.baseline.trajectory_at_start", limit=256)
        if not isinstance(baseline["mastery_at_start"], Mapping):
            raise ContinuityContractError("packet.baseline.mastery_at_start must be a JSON object")
        object.__setattr__(self, "baseline", freeze(baseline))

        # Keys validated BEFORE anything stringifies them (WO-02A closure F1). This site used
        # to write checked_table[str(key)] = data, so integer key 1 and string key "1" carrying
        # DIFFERENT evidence collapsed into one entry and only the second survived — inside an
        # otherwise valid packet, with no error. freeze() cannot catch that, because the
        # collision is already gone by the time it runs.
        table = require_string_keys(self.evidence_table, where="packet.evidence_table")
        checked_table: dict[str, Any] = {}
        for key, row in table.items():
            data = require_keys(
                row, required=_EVIDENCE_ROW_REQUIRED, optional=("ref",), where=f"packet.evidence_table[{key}]"
            )
            from schema.episode_event import EVIDENCE_CLASSES

            require_enum(
                data["evidence_class"],
                allowed=EVIDENCE_CLASSES,
                where=f"packet.evidence_table[{key}].evidence_class",
            )
            require_text(data["attributed_to"], where=f"packet.evidence_table[{key}].attributed_to", limit=256)
            if "ref" in data:
                data["ref"] = CanonicalRef.coerce(data["ref"], where=f"packet.evidence_table[{key}].ref")
            checked_table[key] = data
        object.__setattr__(self, "evidence_table", freeze(checked_table))

        # ONE capsule per sealed member, in the manifest's own order, each agreeing with the
        # manifest's canonical reference (WO-02A C4). The predecessor checked only the row
        # count and individual membership, so ten copies of the first capsule satisfied it
        # against ten distinct members, and a capsule could carry a different content digest
        # from the manifest reference for the same episode.
        if not isinstance(self.episodes, (list, tuple)):
            raise ContinuityContractError("packet.episodes must be a JSON array")
        member_ids = self.manifest.member_ids
        # Every field the manifest and the capsule BOTH carry has to agree, not just the
        # reference (WO-02A closure F4): a member saying completed beside a capsule saying
        # failed was accepted, with IDs, digests, membership and order otherwise intact.
        by_member = {
            str(row["episode_id"]): {"ref": row["ref"], "cascade_status": str(row["cascade_status"])}
            for row in self.manifest.members
        }
        episodes: list[dict[str, Any]] = []
        seen: list[str] = []
        for index, row in enumerate(self.episodes):
            data = require_keys(row, required=_EPISODE_ROW_REQUIRED, where=f"packet.episodes[{index}]")
            episode_id = str(data["episode_id"])
            if episode_id not in by_member:
                raise WindowMembershipError(
                    f"packet.episodes[{index}] names {episode_id!r}, which is not a member"
                )
            if episode_id in seen:
                raise WindowMembershipError(
                    f"packet.episodes[{index}] is a second capsule for {episode_id!r}; a sealed "
                    "member has exactly one"
                )
            seen.append(episode_id)
            data["ref"] = CanonicalRef.coerce(data["ref"], where=f"packet.episodes[{index}].ref")
            expected = by_member[episode_id]
            expected_ref = expected["ref"]
            if data["ref"] != expected_ref:
                raise WindowReferenceError(
                    f"packet.episodes[{index}] for {episode_id!r} cites "
                    f"{data['ref'].record_id!r} v{data['ref'].record_version} "
                    f"{data['ref'].content_sha256[:12]}…, but the sealed manifest names "
                    f"{expected_ref.record_id!r} v{expected_ref.record_version} "
                    f"{expected_ref.content_sha256[:12]}…; a duplicated authoritative field must "
                    "agree, never be resolved to whichever value is convenient"
                )
            for axis, allowed in (
                ("cascade_status", CASCADE_STATUSES),
                ("action_status", ACTION_STATUSES),
                ("task_outcome", TASK_OUTCOMES),
                ("verification_status", VERIFICATION_STATUSES),
                ("feedback_status", FEEDBACK_STATUSES),
            ):
                require_enum(data[axis], allowed=allowed, where=f"packet.episodes[{index}].{axis}")
            # cascade_status is the ONE axis the manifest also carries, so it is the one that
            # can disagree with itself. The other four are the capsule's alone and stay
            # independent: this check must never force task_outcome or verification_status to
            # follow cascade completion.
            if str(data["cascade_status"]) != expected["cascade_status"]:
                raise WindowMembershipError(
                    f"packet.episodes[{index}] for {episode_id!r} says cascade_status="
                    f"{data['cascade_status']!r} but the sealed manifest member says "
                    f"{expected['cascade_status']!r}; a duplicated authoritative field must "
                    "agree, and neither value may be silently preferred"
                )
            episodes.append(data)
        if tuple(seen) != member_ids:
            missing = [m for m in member_ids if m not in seen]
            raise WindowMembershipError(
                "packet.episodes must represent every sealed member exactly once, in the "
                f"manifest's finish-line order. missing={missing} got={seen}"
            )
        object.__setattr__(self, "episodes", freeze(episodes))

        object.__setattr__(self, "temporal_links", freeze(self._checked_rows(
            self.temporal_links, _LINK_REQUIRED, "packet.temporal_links", enum_fields={"uncertainty": UNCERTAINTY_LEVELS}
        )))
        object.__setattr__(self, "historical_support", freeze(self._checked_rows(
            self.historical_support, _SUPPORT_REQUIRED, "packet.historical_support",
            enum_fields={"support_class": SUPPORT_CLASSES}, ref_fields=("ref",)
        )))
        object.__setattr__(self, "learning_followthrough", freeze(self._checked_followthrough()))
        object.__setattr__(self, "open_threads", freeze(self._checked_rows(
            self.open_threads, _THREAD_REQUIRED, "packet.open_threads", ref_fields=("thread_ref",)
        )))

        if not isinstance(self.limitations, (list, tuple)):
            raise ContinuityContractError("packet.limitations must be a JSON array")
        object.__setattr__(
            self,
            "limitations",
            tuple(require_text(item, where="packet.limitations[]", limit=2000) for item in self.limitations),
        )

        missing = sorted(self.cited_keys() - set(self.evidence_table))
        if missing:
            raise WindowReferenceError(
                f"citation(s) {missing} do not resolve to shown evidence; a locator for an "
                "omitted payload is not evidence it was read"
            )
        metric_ids = set(self.metrics.metric_ids)
        for row in self.temporal_links:
            for ref in row.get("metric_refs") or ():
                if str(ref) not in metric_ids:
                    raise WindowReferenceError(f"metric reference {ref!r} is not in this packet")

    @staticmethod
    def _checked_rows(
        rows: Any,
        required: Sequence[str],
        where: str,
        *,
        enum_fields: Mapping[str, Sequence[str]] | None = None,
        ref_fields: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        if not isinstance(rows, (list, tuple)):
            raise ContinuityContractError(f"{where} must be a JSON array")
        out: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            data = require_keys(
                row, required=required, optional=("metric_refs", "note"), where=f"{where}[{index}]"
            )
            for field_name, allowed in (enum_fields or {}).items():
                require_enum(data[field_name], allowed=allowed, where=f"{where}[{index}].{field_name}")
            for field_name in ref_fields:
                data[field_name] = CanonicalRef.coerce(
                    data[field_name], where=f"{where}[{index}].{field_name}"
                )
            out.append(data)
        return out

    def _checked_followthrough(self) -> list[dict[str, Any]]:
        if not isinstance(self.learning_followthrough, (list, tuple)):
            raise ContinuityContractError("packet.learning_followthrough must be a JSON array")
        out: list[dict[str, Any]] = []
        for index, row in enumerate(self.learning_followthrough):
            data = require_keys(
                row, required=_FOLLOWTHROUGH_REQUIRED, where=f"packet.learning_followthrough[{index}]"
            )
            require_text(
                data["intervention_ref"],
                where=f"packet.learning_followthrough[{index}].intervention_ref",
                limit=256,
            )
            for state in _FOLLOWTHROUGH_STATES:
                data[state] = Observation.coerce(
                    data[state], where=f"packet.learning_followthrough[{index}].{state}"
                )
            out.append(data)
        return out

    def cited_keys(self) -> set[str]:
        """Every evidence key any block cites. The host resolves these, not the model."""
        cited: set[str] = set()
        cited |= _citation_refs(self.episodes, "evidence_refs")
        cited |= _citation_refs(self.temporal_links, "support_refs", "counterevidence_refs")
        cited |= _citation_refs(self.historical_support, "evidence_refs")
        cited |= _citation_refs(self.learning_followthrough, "support_refs")
        cited |= _citation_refs(self.open_threads, "evidence_refs")
        return cited

    # ── structural capacity: items, NOT tokens ────────────────────────────────

    def mandatory_item_count(self) -> int:
        """How many mandatory STRUCTURAL ITEMS this packet carries. Not a token count.

        WO-02A source-review obligation 1. The predecessor called this a
        ``mandatory_item_floor`` and claimed every item consumes at least one token under
        every tokenizer. That universal claim is **not established** — a tokenizer may emit a
        single token spanning more than one serialized item — so the token framing is removed
        rather than defended with an argument nobody verified.

        What remains is exactly what can be counted here: items. Verified whole-request token
        accounting is WO-12's, through an identified counter, and its absence raises
        ``TokenAccountingUnavailable`` there. Nothing in this module estimates tokens, and a
        character heuristic is never substituted for one.
        """
        return (
            self.manifest.member_count
            + len(self.episodes)
            + len(self.limitations)
            + len(self.evidence_table)
            + 1
        )

    def require_structural_capacity(self, *, available_items: int) -> None:
        """Raise when fewer admission slots exist than this packet has mandatory items.

        A structural check in the units it actually counts. It says nothing about tokens, and
        it never shrinks or reorders the sealed cohort — the exception carries the membership
        so the caller can see what was preserved.
        """
        require_non_negative_int(available_items, where="available_items")
        needed = self.mandatory_item_count()
        if available_items < needed:
            raise WindowCapacityExceeded(
                f"mandatory window input carries {needed} structural items and only "
                f"{available_items} admission slots are available; the sealed cohort is "
                "preserved intact",
                member_ids=self.manifest.member_ids,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "manifest": self.manifest.to_dict(),
            "baseline": unfreeze(self.baseline),
            "episodes": unfreeze(self.episodes),
            "temporal_links": unfreeze(self.temporal_links),
            "historical_support": unfreeze(self.historical_support),
            "learning_followthrough": unfreeze(self.learning_followthrough),
            "open_threads": unfreeze(self.open_threads),
            "metrics": self.metrics.to_dict(),
            "limitations": list(self.limitations),
            "evidence_table": unfreeze(self.evidence_table),
        }

    def content_sha256(self) -> str:
        return content_hash(self.to_dict())

    @classmethod
    def from_dict(cls, value: Any) -> "WindowEvidencePacket":
        data = require_keys(value, required=WINDOW_PACKET_BLOCKS, where="packet")
        if data.pop("schema_version") != SCHEMA_VERSION:
            raise ContinuityContractError("packet.schema_version does not match this contract")
        data["manifest"] = WindowManifest.from_dict(data["manifest"])
        data["metrics"] = WindowMetrics.from_dict(data["metrics"])
        # No tuple(...) coercion: every collection is type-checked in __post_init__, the same
        # check direct construction gets (WO-02A C2).
        return cls(**data)


__all__ = [
    "COUNTEREXAMPLE_SEARCH_STATES",
    "EVIDENCE_GRADES",
    "FOCAL_MEMBER_COUNT",
    "SCHEMA_VERSION",
    "SUPPORT_CLASSES",
    "UNCERTAINTY_LEVELS",
    "WINDOW_PACKET_BLOCKS",
    "WINDOW_SCOPES",
    "HistoryBundle",
    "HistoryLimits",
    "SnapshotRef",
    "WindowEvidencePacket",
    "WindowManifest",
    "WindowMetrics",
]


for _contract_type in (SnapshotRef, WindowManifest, WindowMetrics, HistoryBundle, HistoryLimits):
    register_contract_type(_contract_type)
del _contract_type
