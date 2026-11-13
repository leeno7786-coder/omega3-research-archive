"""Strict synthetic factories for the episode-continuity contracts (WO-02).

Every factory produces a record that passes the **production** schema, never a permissive
mock shape. CONTRACTS.md section 9: "They are convenient unit inputs; the final end-to-end
tests must use real phase producers and canonical persistence, not these factories to
pretend a boundary works."

All values are distinct synthetic strings with fixed times and known hashes. Nothing here
encodes a developer's schedule, mailbox or machine — AGENTS.md section 0: a test that passes
because it encodes the author's life proves nothing about the product.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.paths import OMEGA_ROOT
from schema.continuity_json import CanonicalRef, Observation, SourceRef
from schema.episode_event import EpisodeEvent
from schema.episode_record import EpisodeOutcome, EpisodeRecord
from schema.window_evidence import (
    HistoryBundle,
    SnapshotRef,
    WindowEvidencePacket,
    WindowManifest,
    WindowMetrics,
)

FIXTURE_DIR = Path(OMEGA_ROOT) / "tests" / "continuity" / "fixtures" / "contracts"

# Fixed synthetic clock. Distinct from any real appliance time.
OBSERVED_AT = "2026-03-04T05:06:07.000000+00:00"
OCCURRED_AT = "2026-03-04T05:06:06.500000+00:00"
CLOSED_AT = "2026-03-04T05:06:09.000000+00:00"
BOOT_ID = "boot-00000000000000000000000000000001"
CLOCK_EPOCH = "epoch-a"

TEN_MEMBERS: tuple[str, ...] = tuple(f"ep-{n:04d}" for n in range(1, 11))

_SYNTHETIC_HASH = "0123456789abcdef" * 4          # 64 hex, obviously synthetic
_SYNTHETIC_HASH_B = "fedcba9876543210" * 4


def _hash(label: str) -> str:
    """A deterministic, obviously-synthetic 64-hex value keyed by label."""
    return hashlib.sha256(f"omega.continuity.fixture:{label}".encode("utf-8")).hexdigest()


def _host_producer(**extra: str) -> dict[str, str]:
    return {
        "component": "tests.helpers.continuity_records",
        "component_version": "wo02",
        "authority": "host",
        **extra,
    }


# ── events ───────────────────────────────────────────────────────────────────


def make_event(**overrides: Any) -> EpisodeEvent:
    """One valid EpisodeEvent. Every field is overridable so a test can break exactly one."""
    fields: dict[str, Any] = {
        "episode_id": "ep-0001",
        "thread_id": "thread-0001",
        "event_seq": 1,
        "event_id": "evt-0001",
        "operation_id": "op-0001",
        "phase_attempt": 1,
        "phase": "auto",
        "event_kind": "tool_call_completed",
        "observed_at": OBSERVED_AT,
        "occurred_at": Observation.observed(OCCURRED_AT),
        "boot_id": BOOT_ID,
        "elapsed_ns": Observation.observed(1_250_000),
        "producer": _host_producer(),
        "evidence_class": "host_observation",
        "status": "succeeded",
        "payload": {"tool": "synthetic_tool", "verdict": "passed"},
        "evidence_refs": ("ev-0001",),
        "artifact_refs": (),
    }
    fields.update(overrides)
    return EpisodeEvent(**fields)


# ── episode record ───────────────────────────────────────────────────────────


def _default_outcome() -> dict[str, Any]:
    return {
        "cascade_status": "completed",
        "action_status": "succeeded",
        "task_outcome": "success",
        "verification_status": "passed",
        "feedback_status": "not_received",
        "assessments": {
            "task_outcome": {
                "author": "host",
                "evidence_refs": ["evt-0001"],
                "criteria_revision": "acceptance@1",
            },
            "verification_status": {
                "author": "host",
                "verifier": "pytest",
                "evidence_refs": ["evt-0001"],
                "criteria_revision": "acceptance@1",
                "artifact_revision": "artifact@1",
            },
        },
    }


def _default_control() -> dict[str, Any]:
    return {
        "assessment": {
            "who": "the operator asked for a synthetic build",
            "what": "one tool call ran and its check passed",
            "when": "one episode, inside its own cascade",
            "where": "the agent lane, synthetic workspace",
            "why": "the acceptance criterion was already satisfied",
            "how": "a single bounded tool call",
            "desired_state": "the criterion is verified",
            "observed_state": "the criterion is verified",
            "gap": "",
            "evidence": ["evt-0001"],
        },
        "decision": "DONE",
        "pressure": None,
        "synthesis": "The synthetic build ran once and its check passed.",
    }


def make_episode_record(
    *,
    episode_id: str = "ep-0001",
    thread_id: str = "thread-0001",
    commit_seq: int = 1,
    event_count: int = 1,
    outcome: Mapping[str, Any] | None = None,
    objective: Mapping[str, Any] | None = None,
    temporal_overrides: Mapping[str, Any] | None = None,
    events_overrides: Mapping[str, Any] | None = None,
    coverage_overrides: Mapping[str, Any] | None = None,
    **overrides: Any,
) -> EpisodeRecord:
    events = tuple(
        make_event(
            episode_id=episode_id,
            thread_id=thread_id,
            event_id=f"evt-{n:04d}",
            operation_id=f"op-{n:04d}",
            event_seq=n,
        )
        for n in range(1, event_count + 1)
    )

    temporal: dict[str, Any] = {
        "opened_at": Observation.observed(OBSERVED_AT),
        "closed_at": Observation.observed(CLOSED_AT),
        "queue_arrival_seq": Observation.observed(41),
        "finish_line": {
            "commit_seq": commit_seq,
            "commit_ms": 1_772_600_000_000,
            "event_date": "2026-03-04",
            "pipeline_id": 0,
            "work_kind": "operator_turn",
        },
        "coverage_clock": {"clock_epoch_id": CLOCK_EPOCH, "commit_seq": commit_seq},
        "read_epoch_start": commit_seq - 1,
        "read_pin": {
            "graph_snapshot_hash": _hash("graph"),
            "coord_index_generation": 12,
            "dme_pack_hash": _hash("dme"),
            "unpinnable_surfaces": [],
            "attribution": "thread_bound",
        },
        "concurrent_commits": {"episode_ids": (), "complete": True},
    }
    temporal.update(dict(temporal_overrides or {}))

    events_block: dict[str, Any] = {
        "refs": tuple(
            CanonicalRef(
                record_id=event.event_id,
                record_version=1,
                content_sha256=event.content_sha256(),
            )
            for event in events
        ),
        "by_value": events,
        "integrity": {event.event_id: event.content_sha256() for event in events},
        "coverage": {
            "retained": len(events),
            "referenced": len(events),
            "unavailable_refs": (),
            "reason": "",
        },
    }
    events_block.update(dict(events_overrides or {}))

    coverage: dict[str, Any] = {
        "captured": ("admission", "auto", "omega", "deepsleep"),
        "unrecorded": ("global_a intent rationale",),
        "inaccessible": (),
        "redacted": (),
        "causal_gaps": ("no read receipt for the recall that preceded the tool call",),
    }
    coverage.update(dict(coverage_overrides or {}))

    fields: dict[str, Any] = {
        "identity": {
            "episode_id": episode_id,
            "thread_id": thread_id,
            "turn_id": Observation.observed("turn-0001"),
            "continuation_id": Observation.missing("not_applicable", reason="not a continuation"),
            "job_id": Observation.missing("not_applicable", reason="no durable job"),
            "attempt_id": Observation.observed("attempt-0001"),
            "pipeline_id": 0,
            "lane": "agent",
            "work_kind": "operator_turn",
        },
        "temporal": temporal,
        "origin": {
            "trigger": {
                "opened_by": "operator_command",
                "source_ref": SourceRef(
                    source_id="src-0001", source_type="operator_chat", revision="rev-1"
                ),
            },
            "authority_snapshot_ref": Observation.observed("connection@rev-1"),
            "initial_trajectory_ref": Observation.observed("trajectory@rev-1"),
        },
        "objective": dict(
            objective
            or {
                "original_request": "run the synthetic acceptance check",
                "acceptance_criteria": ["the synthetic check reports passed"],
                "criteria_revision": "acceptance@1",
                "preconditions": [],
            }
        ),
        "events": events_block,
        "outcome": EpisodeOutcome.from_dict(dict(outcome or _default_outcome())),
        "metacognition": {
            "control": _default_control(),
            "model_assessment": {"summary": "one bounded call, verified"},
            "proposals": (),
        },
        "learning": {
            "proposals": (),
            "effects": ({"effect_id": "eff-0001", "kind": "mastery", "domain": "synthetic"},),
            "applied_receipts": (),
        },
        "coverage": coverage,
    }
    fields.update(overrides)
    return EpisodeRecord(**fields)


# ── window records ───────────────────────────────────────────────────────────


def make_snapshot_ref(**overrides: Any) -> SnapshotRef:
    fields: dict[str, Any] = {
        "generation_id": _SYNTHETIC_HASH,
        "coverage_clock": {"clock_epoch_id": CLOCK_EPOCH, "commit_seq": 120},
        "visibility_cutoff": CLOSED_AT,
        "trajectory_revision_at_start": "trajectory@rev-1",
        "trajectory_revision_at_cutoff": "trajectory@rev-2",
        "authority_snapshot_ref": "connection@rev-1",
        "reader_policy_revision": "reader@1",
    }
    fields.update(overrides)
    return SnapshotRef(**fields)


def make_window_manifest(
    *,
    member_ids: Sequence[str] = TEN_MEMBERS,
    window_id: str = "window-0001",
    member_outcomes: Mapping[str, str] | None = None,
    member_commit_status: Mapping[str, str] | None = None,
    **overrides: Any,
) -> WindowManifest:
    outcomes = dict(member_outcomes or {})
    commits = dict(member_commit_status or {})
    fields: dict[str, Any] = {
        "window_id": window_id,
        "window_version": 1,
        "configuration_fingerprint": _hash("reflection_window@10"),
        "scope": "focal",
        "snapshot": make_snapshot_ref(),
        "complete": True,
        "members": tuple(
            {
                "episode_id": episode_id,
                "ref": CanonicalRef(
                    record_id=f"9.01.{episode_id}",
                    record_version=1,
                    content_sha256=_hash(episode_id),
                ),
                "finish_line_commit_seq": 100 + index,
                "cascade_status": outcomes.get(episode_id, "completed"),
                "memory_commit_status": commits.get(episode_id, "recall_published"),
            }
            for index, episode_id in enumerate(member_ids)
        ),
    }
    fields.update(overrides)
    return WindowManifest(**fields)


def _default_metric_rows() -> list[dict[str, Any]]:
    return [
        {
            "metric_id": "verified_rate",
            "numerator": 6,
            "denominator": 9,
            "population_refs": list(TEN_MEMBERS[:9]),
            "excluded": 1,
            "unknown": 0,
            "stratification": {"criteria_revision": "acceptance@1"},
            "computation_version": "metrics@1",
        }
    ]


def _default_evidence_table() -> dict[str, dict[str, Any]]:
    return {
        "ev-0001": {
            "evidence_class": "host_observation",
            "attributed_to": "deepsleep_loop.loop",
            "value": {"verdict": "passed"},
            "ref": {
                "record_id": "9.01.ep-0001",
                "record_version": 1,
                "content_sha256": _hash("ep-0001"),
            },
        },
        "ev-0002": {
            "evidence_class": "verification_result",
            "attributed_to": "pytest",
            "value": {"exit_code": 0},
            "ref": {
                "record_id": "9.01.ep-0002",
                "record_version": 1,
                "content_sha256": _hash("ep-0002"),
            },
        },
    }


def make_packet(
    *,
    member_ids: Sequence[str] = TEN_MEMBERS,
    metric_rows: Sequence[Mapping[str, Any]] | None = None,
    prior_window_ref: Observation | None = None,
    **overrides: Any,
) -> WindowEvidencePacket:
    manifest = make_window_manifest(member_ids=member_ids)
    fields: dict[str, Any] = {
        "manifest": manifest,
        "baseline": {
            "prior_window_ref": prior_window_ref
            if prior_window_ref is not None
            else Observation.observed("window-0000"),
            "trajectory_at_start": "trajectory@rev-1",
            "mastery_at_start": {"synthetic": {"knowledge": 0.4, "application": 0.2}},
        },
        "episodes": tuple(
            {
                "episode_id": episode_id,
                "ref": {
                    "record_id": f"9.01.{episode_id}",
                    "record_version": 1,
                    "content_sha256": _hash(episode_id),
                },
                "cascade_status": "completed",
                "action_status": "succeeded",
                "task_outcome": "success" if index % 3 else "failure",
                "verification_status": "passed" if index % 3 else "failed",
                "feedback_status": "not_received",
                "objective": f"synthetic objective {index + 1}",
                "evidence_refs": ["ev-0001"],
            }
            for index, episode_id in enumerate(member_ids)
        ),
        "temporal_links": (
            {
                "claim": "the second episode repeated the first attempt unchanged",
                "relation_kind": "repeats",
                "episode_ids": list(member_ids[:2]),
                "support_refs": ["ev-0001"],
                "counterevidence_refs": [],
                "uncertainty": "medium",
            },
        ),
        "historical_support": (
            {
                "support_class": "historical",
                "ref": {
                    "record_id": "6.00.window-0000",
                    "record_version": 1,
                    "content_sha256": _hash("window-0000"),
                },
                "as_of": CLOSED_AT,
                "summary": "a prior window named the same repeated failure",
                "evidence_refs": ["ev-0002"],
            },
        ),
        "learning_followthrough": (
            {
                "intervention_ref": "eff-0001",
                "committed": {"state": "observed", "value": True, "reason": ""},
                "exposed": {"state": "observed", "value": True, "reason": ""},
                "reported_use": {"state": "unknown", "value": None, "reason": "no model report"},
                "behaviourally_applied": {
                    "state": "not_captured",
                    "value": None,
                    "reason": "no tool receipt was retained for this build",
                },
                "eligible_opportunity": {"state": "observed", "value": True, "reason": ""},
                "evaluated_result": {
                    "state": "unknown",
                    "value": None,
                    "reason": "no comparable later attempt",
                },
                "support_refs": ["ev-0001"],
            },
        ),
        "open_threads": (
            {
                "thread_ref": {
                    "record_id": "9.01.thread-0001",
                    "record_version": 1,
                    "content_sha256": _hash("thread-0001"),
                },
                "state": "open",
                "condition": "the repeated failure has no verified fix",
                "evidence_refs": ["ev-0001"],
            },
        ),
        "metrics": WindowMetrics.from_dict(
            {
                "computation_version": "metrics@1",
                "rows": list(metric_rows or _default_metric_rows()),
            }
        ),
        "limitations": (
            "no token accounting existed when this packet was assembled",
            "counterexample search was not run",
        ),
        "evidence_table": _default_evidence_table(),
    }
    fields.update(overrides)
    return WindowEvidencePacket(**fields)


def make_history_bundle(**overrides: Any) -> HistoryBundle:
    """The separately-labelled history WO-10 collects. NOT a packet block: the packet has
    exactly eleven, and `historical_support` is what this bundle is rendered into."""
    fields: dict[str, Any] = {
        "primary_support": (),
        "prior_findings": (),
        "intervening_direction_changes": (),
        "links": (),
        "retrieval_audit": {
            "query_policy_revision": "history@1",
            "scope": "lobe-9 episodes at the cutoff",
            "considered_ids": [],
            "returned_ids": [],
            "admitted_ids": [],
            "omitted": [],
            "counterexample_search_status": "search_not_run",
            "cap": 32,
        },
    }
    fields.update(overrides)
    return HistoryBundle(**fields)


# ── the model's response ─────────────────────────────────────────────────────


def make_valid_response(
    *,
    member_ids: Sequence[str] = TEN_MEMBERS,
    trajectory_update: Mapping[str, Any] | None = ...,  # type: ignore[assignment]
    **overrides: Any,
) -> dict[str, Any]:
    """A plain dict, deliberately: this is what a model returns, before validation."""
    payload: dict[str, Any] = {
        "control": _default_control(),
        "reflection": {
            "summary": "Ten episodes circled one unverified fix; the same failure recurred three times.",
            "coverage": {
                "member_ids": list(member_ids),
                "declared_limitations": ["no counterexample search was run"],
            },
            "episode_assessments": [
                {
                    "episode_id": episode_id,
                    "assessment": f"episode {index + 1} attempted the synthetic fix",
                    "evidence_refs": ["ev-0001"],
                    "outcome": "success" if index % 3 else "failure",
                    "uncertainty": "low",
                    "unresolved_condition": "" if index % 3 else "the check still fails",
                }
                for index, episode_id in enumerate(member_ids)
            ],
            "temporal_findings": [
                {
                    "claim": "the fix was attempted three times without change",
                    "relation_kind": "repeats",
                    "episode_ids": list(member_ids[:3]),
                    "support_refs": ["ev-0001"],
                    "counterevidence_refs": [],
                    "uncertainty": "medium",
                }
            ],
            "patterns": [
                {
                    "claim": "an unverified fix is retried rather than re-diagnosed",
                    "applicability": "synthetic acceptance checks in this window only",
                    "support_refs": ["ev-0001"],
                    "counterevidence_refs": ["ev-0002"],
                    "metric_refs": ["verified_rate"],
                    "evidence_grade": "observed_regularity",
                    "falsifier": "a later episode that re-diagnoses before retrying",
                }
            ],
            "learning_followthrough": [
                {
                    "intervention_ref": "eff-0001",
                    "committed": {"state": "observed", "value": True, "reason": ""},
                    "exposed": {"state": "observed", "value": True, "reason": ""},
                    "reported_use": {"state": "unknown", "value": None, "reason": "no report"},
                    "behaviourally_applied": {
                        "state": "not_captured",
                        "value": None,
                        "reason": "no tool receipt retained",
                    },
                    "eligible_opportunity": {"state": "observed", "value": True, "reason": ""},
                    "evaluated_result": {
                        "state": "unknown",
                        "value": None,
                        "reason": "no comparable attempt",
                    },
                    "support_refs": ["ev-0001"],
                }
            ],
            "thread_updates": [
                {
                    "thread_ref": {
                        "record_id": "9.01.thread-0001",
                        "record_version": 1,
                        "content_sha256": _hash("thread-0001"),
                    },
                    "proposed_state": "open",
                    "change": "the repeated failure is still unverified",
                    "evidence_refs": ["ev-0001"],
                }
            ],
            "proactive_candidates": [
                {
                    "what": "tell the operator the acceptance check has never passed",
                    "why_now": "three attempts closed without a passing check",
                    "articulation": "The check this build targets has not passed in ten episodes.",
                    "delivery_conditions": ["only when the operator is not mid-build"],
                    "ownership_refs": ["job-0001"],
                    "dedup_refs": ["ev-0001"],
                }
            ],
            "limitations": ["no counterexample search was run"],
            "trajectory_assessment": {
                "start_snapshot_ref": "trajectory@rev-1",
                "cutoff_snapshot_ref": "trajectory@rev-2",
                "assessment": "the trajectory was followed but produced no verified result",
                "reason": "every attempt targeted the same unverified criterion",
                "evidence_refs": ["ev-0001"],
            },
        },
        "trajectory_update": (
            {
                "operation": "alter",
                "target_domain": "synthetic",
                "target_capability": "acceptance_verification",
                "confidence": 0.6,
                "reason": "retrying without re-diagnosis has not produced a passing check",
            }
            if trajectory_update is ...
            else trajectory_update
        ),
    }
    payload.update(overrides)
    return payload


# ── fixtures on disk ─────────────────────────────────────────────────────────


def load_contract_fixture(name: str) -> Any:
    """Parse one named fixture through PRODUCTION schema code.

    A fixture that only the reader accepts proves nothing (CONTRACTS.md section 9), so every
    fixture is admitted by the same `from_dict` a producer would use, via
    `parse_json_object` first so a duplicate key in the fixture is caught rather than
    silently resolved.
    """
    from schema.continuity_json import parse_json_object
    from schema.window_reflection import validate_window_response_payload

    raw = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    document = parse_json_object(raw)
    kind = document.get("fixture_kind")
    body = document.get("body")

    if kind == "event":
        return EpisodeEvent.from_dict(body)
    if kind == "episode":
        return EpisodeRecord.from_dict(body)
    if kind == "packet":
        return WindowEvidencePacket.from_dict(body)
    if kind == "window_result":
        return validate_window_response_payload(body, member_ids=tuple(document["member_ids"]))
    if kind == "event_pair":
        first = EpisodeEvent.from_dict(body["first"])
        second = EpisodeEvent.from_dict(body["second"])
        return first.require_same_occurrence(second)
    raise AssertionError(f"unknown fixture_kind {kind!r} in {name}.json")


def write_contract_fixture(name: str, *, fixture_kind: str, body: Any, **extra: Any) -> Path:
    """Emit a fixture with its canonical bytes. Used by the fixture-generation helper."""
    from schema.continuity_json import canonical_bytes

    document = {"fixture_kind": fixture_kind, "body": body, **extra}
    path = FIXTURE_DIR / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(document) + b"\n")
    return path


__all__ = [
    "TEN_MEMBERS",
    "load_contract_fixture",
    "make_episode_record",
    "make_event",
    "make_packet",
    "make_snapshot_ref",
    "make_valid_response",
    "make_history_bundle",
    "make_window_manifest",
    "write_contract_fixture",
]
