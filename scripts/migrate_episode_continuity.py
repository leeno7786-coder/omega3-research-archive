"""Offline, restartable conversion of historical lobe-9 episode records (WO-08).

    .venv-brain\\Scripts\\python.exe -m scripts.migrate_episode_continuity --source <dir> \\
        --destination <dir> (--dry-run | --stage | --resume | --verify)

**What it converts.** A canonical lobe-9 episode document written before WO-07 sealed records —
the enriched body (with a finish-line stamp and memory epochs, but no thread id, open time or
clock epoch), or the older distilled ``PriorEpisode`` (no stamp, no epoch, often no work kind) —
into the sealed ``EpisodeRecord`` the new-format reader accepts, riding the same canonical body.

**What it never does.** It invents nothing. A field the source recorded is extracted with the
path it came from; a field it did not record is ``not_captured`` with this converter named as
the author of the absence. No date from a file time, no thread made up from the episode id, no
finish-line position, no success check, no delivery receipt, no model reflection that never ran:
a deterministic window verdict stays a diagnostic. The original episode id is kept; the new
representation takes the deterministic record id (``episode.<id>``) and a conversion version.

**Offline and isolated.** :func:`migrate_episode_document` is pure. The CLI has no default source,
refuses any destination inside the repository (the live brain), writes converted records into the
destination's canonical layout, and keeps its journal, manifest and quarantine in
``migration_control/`` beside ``memory/`` — outside every recallable path. Every source is
journalled with its file hash and its output hash: a replay is a no-op, an interrupted run resumes
to exactly one conversion per source version, and a source or destination that changed under the
journal is a :class:`~schema.continuity_json.MigrationConflict` that overwrites nothing.

Exit codes: ``0`` complete; ``2`` incomplete (a malformed mandatory source, reported and
quarantined — it is never dropped to pass); ``1`` conflict, refusal or failed verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Optional

from schema.continuity_json import (
    ContinuityContractError,
    MigrationConflict,
    Observation,
    content_hash,
)
from schema.episode_record import (
    CASCADE_STATUSES,
    OUTCOME_AXES,
    OUTCOME_PACK_STATUS_PATH,
    EpisodeOutcome,
    EpisodeRecord,
    cascade_and_task_from_outcome_pack,
)

CONVERSION_VERSION = "omega.episode_continuity.migration.v1"
CONVERTER = "scripts.migrate_episode_continuity"
ATTRIBUTION = f"converted by {CONVERTER} {CONVERSION_VERSION}"
CONTROL_DIR = "migration_control"
EPISODE_DIR = Path("memory") / "900_temporal" / "events" / "episode"
NO_CONTRACT = "none: the objective recorded no acceptance contract"

#: Legacy body keys the sealed record now carries, and what replaces them. They are PRESERVED in
#: the converted body until the consumers named here are switched (the WO-18 cutover).
COMPATIBILITY_REMOVAL_MAP: dict[str, dict[str, Any]] = {
    "outcome": {"replaced_by": ["episode_record.outcome", "outcome_axes"],
                "readers_at_wo08": ["pressure_loop.temporal_store.hydrate_reflection_window",
                                    "omega_loop.window.run (window view, deterministic verdict)",
                                    "pressure_loop.episode_journal.prior_episode_from_episode"]},
    "cascade_status": {"replaced_by": ["episode_record.outcome.cascade_status"],
                       "readers_at_wo08": ["omega_loop.window.run", "deepsleep intake"]},
    "timekeeper": {"replaced_by": ["episode_record.temporal.finish_line"],
                   "readers_at_wo08": ["pressure_loop.episode_journal (finish-line order)"]},
    "memory_epoch": {"replaced_by": ["episode_record.temporal.read_epoch_start",
                                     "episode_record.temporal.read_pin",
                                     "episode_record.temporal.concurrent_commits"],
                     "readers_at_wo08": ["omega window view"]},
    "omega_result": {"replaced_by": ["episode_record.metacognition"],
                     "readers_at_wo08": ["deepsleep_loop.mastery (mastery_effects)",
                                         "deepsleep intake (identity patterns)"]},
    "pressure": {"replaced_by": ["episode_record.origin.trigger"],
                 "readers_at_wo08": ["pressure_loop.temporal_store._episode_gist"]},
    "motor_program": {"replaced_by": ["episode_record.objective"],
                      "readers_at_wo08": ["deepsleep_loop.mastery (motor credit)"]},
}

_ALLOWED_TOP_LEVEL = (
    "audit", "coord", "created", "domain", "enrichment", "flags", "id", "links", "modified",
    "provenance", "source", "speech_act", "subtopic", "summary", "tags", "temporal_epoch_us",
    "text", "title", "type", "version",
)


class RefusedDestination(RuntimeError):
    """The destination is the live repository (or inside it)."""


@dataclass(frozen=True)
class MigrationResult:
    """One source document's conversion: its reference, the typed result, and every decision."""

    source_ref: Mapping[str, str]
    episode_id: str
    status: str  # "converted" | "current" | "malformed"
    conversion_version: str = CONVERSION_VERSION
    converted_document: Optional[dict] = None
    record: Optional[EpisodeRecord] = None
    decisions: tuple[Mapping[str, str], ...] = ()
    errors: tuple[str, ...] = field(default=())


# ── the pure converter ──────────────────────────────────────────────────────


class _Decisions:
    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []

    def extracted(self, name: str, source: str) -> None:
        self.rows.append({"field": name, "decision": "extracted", "source": source})

    def absent(self, name: str, reason: str) -> Observation:
        self.rows.append({"field": name, "decision": "not_captured", "reason": reason})
        return Observation.missing("not_captured", reason=f"{reason} ({ATTRIBUTION})")

    def diagnostic(self, name: str, reason: str) -> None:
        self.rows.append({"field": name, "decision": "diagnostic", "reason": reason})

    def attributed(self, name: str, source: str) -> None:
        self.rows.append({"field": name, "decision": "preserved_attributed", "source": source})


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _identity(body: Mapping[str, Any], episode_id: str, d: _Decisions) -> dict[str, Any]:
    identity: dict[str, Any] = {"episode_id": episode_id}
    d.extracted("identity.episode_id", "body.episode_id")
    thread = _text(body.get("thread_id"))
    if thread:
        identity["thread_id"] = thread
        d.extracted("identity.thread_id", "body.thread_id")
    else:
        identity["thread_id"] = d.absent("identity.thread_id",
                                         "the source record carries no thread id")
    pipeline = body.get("pipeline_id")
    if isinstance(pipeline, int) and not isinstance(pipeline, bool) and pipeline >= 0:
        identity["pipeline_id"] = pipeline
        d.extracted("identity.pipeline_id", "body.pipeline_id")
    else:
        identity["pipeline_id"] = -1  # the absent sentinel (WO-02 decision 7)
        d.rows.append({"field": "identity.pipeline_id", "decision": "not_captured",
                       "reason": "the source record carries no physical pipeline id"})
    work_kind = _text(body.get("work_kind"))
    omega = body.get("omega_result") if isinstance(body.get("omega_result"), Mapping) else {}
    decision = omega.get("intent_decision") if isinstance(omega.get("intent_decision"), Mapping) else {}
    lane = _text(decision.get("omega_lane"))
    if lane:
        identity["lane"] = lane
        d.extracted("identity.lane", "body.omega_result.intent_decision.omega_lane")
    elif work_kind:
        identity["lane"] = work_kind
        d.extracted("identity.lane", "body.work_kind")
    else:
        identity["lane"] = d.absent("identity.lane", "the source record names no lane or work kind")
    if work_kind:
        identity["work_kind"] = work_kind
        d.extracted("identity.work_kind", "body.work_kind")
    else:
        identity["work_kind"] = d.absent("identity.work_kind",
                                         "the source record carries no work kind")
    for name in ("turn_id", "continuation_id", "job_id", "attempt_id"):
        identity[name] = d.absent(f"identity.{name}",
                                  "the source era did not bind this host identity")
    return identity


def _temporal(document: Mapping[str, Any], body: Mapping[str, Any], d: _Decisions) -> dict[str, Any]:
    enrichment = document.get("enrichment") if isinstance(document.get("enrichment"), Mapping) else {}
    temporal: dict[str, Any] = {}
    opened = _text(body.get("opened_at"))
    if opened:
        temporal["opened_at"] = Observation.observed(opened)
        d.extracted("temporal.opened_at", "body.opened_at")
    else:
        temporal["opened_at"] = d.absent("temporal.opened_at",
                                         "the source record carries no episode open time")
    closed = _text(enrichment.get("closed_at")) or _text(body.get("closed_at"))
    if closed:
        temporal["closed_at"] = Observation.observed(closed)
        d.extracted("temporal.closed_at", "enrichment.closed_at" if _text(enrichment.get("closed_at"))
                    else "body.closed_at")
    else:
        temporal["closed_at"] = d.absent("temporal.closed_at",
                                         "the source record carries no close time")
    temporal["queue_arrival_seq"] = d.absent("temporal.queue_arrival_seq",
                                             "arrival order was not recorded before WO-04")
    stamp = body.get("timekeeper") if isinstance(body.get("timekeeper"), Mapping) else {}
    seq = stamp.get("commit_seq")
    if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1:
        temporal["finish_line"] = {
            "commit_seq": seq,
            "commit_ms": int(stamp.get("commit_ms") or 0),
            "event_date": str(stamp.get("event_date") or ""),
            "pipeline_id": int(stamp.get("pipeline_id", -1)),
            "work_kind": str(stamp.get("work_kind") or ""),
        }
        d.extracted("temporal.finish_line", "body.timekeeper")
    else:
        temporal["finish_line"] = d.absent(
            "temporal.finish_line",
            "no finish-line stamp was recorded; a place in the memory sequence is never invented")
    temporal["coverage_clock"] = d.absent(
        "temporal.coverage_clock", "the clock epoch the stamp was issued under was not recorded")
    epoch = body.get("memory_epoch") if isinstance(body.get("memory_epoch"), Mapping) else None
    start = epoch.get("start") if epoch else None
    if isinstance(start, int) and not isinstance(start, bool) and start >= 0:
        temporal["read_epoch_start"] = start
        d.extracted("temporal.read_epoch_start", "body.memory_epoch.start")
    else:
        temporal["read_epoch_start"] = d.absent("temporal.read_epoch_start",
                                                "the source record carries no read epoch")
    pin = epoch.get("pin") if epoch and isinstance(epoch.get("pin"), Mapping) else {}
    if pin:
        generation = pin.get("coord_index_generation")
        surfaces = {
            "graph_snapshot_hash": str(pin.get("graph_snapshot_hash") or ""),
            "coord_index_generation": -1 if generation is None else int(generation),
            "dme_pack_hash": str(pin.get("dme_snapshot_hash") or ""),
        }
        temporal["read_pin"] = {
            **surfaces,
            "unpinnable_surfaces": sorted({str(x) for x in pin.get("degraded") or ()}
                                          | {k for k, v in surfaces.items() if v in ("", -1)}),
            "attribution": f"historical carrier pin as recorded ({ATTRIBUTION})",
        }
        d.extracted("temporal.read_pin", "body.memory_epoch.pin")
    else:
        temporal["read_pin"] = d.absent("temporal.read_pin",
                                        "the source record carries no read pin")
    if epoch is not None:
        temporal["concurrent_commits"] = {
            "episode_ids": [str(x) for x in epoch.get("causal_parent_episode_ids") or ()],
            "complete": bool(epoch.get("causal_parents_complete", False)),
        }
        d.extracted("temporal.concurrent_commits", "body.memory_epoch.causal_parent_episode_ids")
    else:
        temporal["concurrent_commits"] = {"episode_ids": [], "complete": False}
        d.rows.append({"field": "temporal.concurrent_commits", "decision": "not_captured",
                       "reason": "no concurrent commits were recorded; the empty set says it may "
                                 "be short (complete=false)"})
    return temporal


def _origin(body: Mapping[str, Any], d: _Decisions) -> dict[str, Any]:
    pressure = body.get("pressure") if isinstance(body.get("pressure"), Mapping) else {}
    opened = _text(pressure.get("opened_by"))
    ignition = _text(body.get("ignition_reason"))
    if opened:
        trigger: dict[str, Any] = {"opened_by": opened}
        d.extracted("origin.trigger.opened_by", "body.pressure.opened_by")
    elif ignition:
        trigger = {"opened_by": ignition}
        d.extracted("origin.trigger.opened_by", "body.ignition_reason")
    else:
        trigger = {"opened_by": d.absent("origin.trigger.opened_by",
                                         "the source record names no opener")}
    return {
        "trigger": trigger,
        "authority_snapshot_ref": d.absent("origin.authority_snapshot_ref",
                                           "no connection authority snapshot was recorded"),
        "initial_trajectory_ref": d.absent("origin.initial_trajectory_ref",
                                           "no trajectory revision was recorded at open"),
    }


def _request(body: Mapping[str, Any]) -> tuple[str, str]:
    pressure = body.get("pressure") if isinstance(body.get("pressure"), Mapping) else {}
    for index, row in enumerate(pressure.get("contributors") or ()):
        if not isinstance(row, Mapping):
            continue
        for key in ("metadata", "detail"):
            holder = row.get(key)
            message = holder.get("message") if isinstance(holder, Mapping) else None
            if _text(message):
                return _text(message), f"body.pressure.contributors[{index}].{key}.message"
    program = body.get("motor_program") if isinstance(body.get("motor_program"), Mapping) else {}
    contract = program.get("outcome_contract") if isinstance(program.get("outcome_contract"), Mapping) else {}
    if _text(contract.get("objective")):
        return _text(contract["objective"]), "body.motor_program.outcome_contract.objective"
    return "", ""


def _objective(body: Mapping[str, Any], d: _Decisions) -> dict[str, Any]:
    from meta_loop.checks.run import criteria_of

    objective: dict[str, Any] = {"preconditions": []}
    request, where = _request(body)
    if request:
        objective["original_request"] = request
        d.extracted("objective.original_request", where)
    else:
        objective["original_request"] = d.absent("objective.original_request",
                                                 "the source record carries no request text")
    if "motor_program" not in body:
        objective["acceptance_criteria"] = []
        objective["criteria_revision"] = d.absent(
            "objective.criteria_revision", "the source record has no objective contract slot")
        return objective
    program = body.get("motor_program")
    if isinstance(program, Mapping):
        criteria, revision = criteria_of(program)
        objective["acceptance_criteria"] = list(criteria)
        objective["criteria_revision"] = revision or NO_CONTRACT
        contract = program.get("outcome_contract") if isinstance(program.get("outcome_contract"), Mapping) else {}
        objective["preconditions"] = [str(x) for x in contract.get("preconditions") or ()]
        d.extracted("objective.criteria_revision", "body.motor_program.outcome_contract")
    else:
        objective["acceptance_criteria"] = []
        objective["criteria_revision"] = NO_CONTRACT
        d.extracted("objective.criteria_revision", "body.motor_program (null: no contract)")
    return objective


def _outcome(body: Mapping[str, Any], d: _Decisions) -> EpisodeOutcome:
    raw = _text(body.get("cascade_status"))
    cascade = raw if raw in CASCADE_STATUSES else "unknown"
    task, note = "unknown", "the source recorded no outcome pack"
    pack = body.get("outcome")
    if isinstance(pack, Mapping):
        try:
            split = cascade_and_task_from_outcome_pack(pack, cascade_status=cascade)
        except ContinuityContractError as exc:
            note = f"the source outcome is not the nested pack: {exc}"
        else:
            task = split["task_outcome"]
            note = (f"extracted from {OUTCOME_PACK_STATUS_PATH}="
                    f"{split['evidence']['producer_status']!r}")
    d.extracted("outcome.cascade_status", "body.cascade_status")
    if task == "unknown":
        d.rows.append({"field": "outcome.task_outcome", "decision": "not_captured", "reason": note})
    else:
        d.extracted("outcome.task_outcome", OUTCOME_PACK_STATUS_PATH)
    return EpisodeOutcome(
        cascade_status=cascade,
        action_status="unknown",
        task_outcome=task,
        verification_status="unknown",
        feedback_status="unknown",
        assessments={
            "cascade_status": {"author": "historical_conversion",
                               "note": f"body.cascade_status={raw!r} ({ATTRIBUTION})"},
            "task_outcome": {"author": "historical_conversion", "note": f"{note} ({ATTRIBUTION})"},
        },
    )


def _metacognition(body: Mapping[str, Any], episode_id: str, d: _Decisions) -> dict[str, Any]:
    from pressure_loop.episode_journal import metacognition_content

    omega = body.get("omega_result") if isinstance(body.get("omega_result"), Mapping) else None
    window = omega.get("window_reflection") if omega and isinstance(omega.get("window_reflection"), Mapping) else None
    if window and _text(window.get("reflection_source")) == "deterministic":
        summary = _text((window.get("reflection") or {}).get("summary")) if isinstance(window.get("reflection"), Mapping) else ""
        reason = ("no model reflection was produced; the archived window verdict was deterministic "
                  f"(reflection_source=deterministic), a diagnostic counted from recorded outcomes: "
                  f"{summary} ({ATTRIBUTION})")
        d.diagnostic("metacognition", "deterministic window verdict kept as a diagnostic")
        return {"control": Observation.missing("unavailable", reason=reason),
                "model_assessment": {}, "proposals": []}
    if omega is None:
        return {"control": d.absent("metacognition", "the source recorded no Omega fold"),
                "model_assessment": {}, "proposals": []}
    carrier = SimpleNamespace(episode_id=episode_id, meta={},
                              global_={"omega_result": dict(omega)})
    try:
        content = metacognition_content(carrier)
    except ContinuityContractError as exc:
        d.diagnostic("metacognition", f"the recorded control decision does not validate: {exc}")
        return {"control": Observation.missing(
                    "unavailable", reason=f"the recorded control decision does not validate: "
                                          f"{exc} ({ATTRIBUTION})"),
                "model_assessment": {}, "proposals": []}
    if not content.is_observed:
        d.diagnostic("metacognition", content.reason)
        return {"control": Observation.missing(
                    content.state, reason=f"{content.reason} ({ATTRIBUTION})"),
                "model_assessment": {}, "proposals": []}
    value = dict(content.to_dict()["value"])
    assessment = dict(value["model_assessment"])
    assessment["provenance"] = ("historical: generated text preserved as the model's attributed "
                                f"assessment, never a verified event ({ATTRIBUTION})")
    if window:
        assessment["window_reflection"] = dict(window)
    value["model_assessment"] = assessment
    d.attributed("metacognition", "body.omega_result")
    return value


def _coverage(d: _Decisions) -> dict[str, Any]:
    captured = sorted({row["field"] for row in d.rows if row["decision"] in
                       ("extracted", "preserved_attributed")})
    unrecorded = sorted({row["field"] for row in d.rows if row["decision"] == "not_captured"})
    gaps = [f"historical record ({ATTRIBUTION}): no phase events, check receipts or effect "
            "receipts were ever retained"]
    gaps += [f"{row['field']}: {row['reason']}" for row in d.rows if row["decision"] == "diagnostic"]
    return {"captured": captured, "unrecorded": unrecorded, "inaccessible": [], "redacted": [],
            "causal_gaps": gaps}


def _converted_document(document: Mapping[str, Any], body: Mapping[str, Any], record: EpisodeRecord,
                        source_ref: Mapping[str, str]) -> dict[str, Any]:
    from memory.episode_record_seal import episode_record_id
    from memory.temporal_hash import build_temporal_hash

    record_id = episode_record_id(record.episode_id)
    new_body = dict(body)
    new_body["episode_record"] = record.to_dict()
    new_body["outcome_axes"] = {axis: getattr(record.outcome, axis) for axis in OUTCOME_AXES}
    converted = {key: document[key] for key in _ALLOWED_TOP_LEVEL if key in document}
    enrichment = dict(document.get("enrichment") or {})
    enrichment.update({
        "episode_id": record.episode_id,
        "canonical": True,
        "conversion": {"version": CONVERSION_VERSION, "converter": CONVERTER,
                       "source_record_id": source_ref["record_id"],
                       "source_sha256": source_ref["sha256"]},
    })
    converted.update({
        "id": record_id,
        "coord": [9, 1, 0],
        "type": "episode",
        "text": json.dumps(new_body, ensure_ascii=False, sort_keys=True),
        "enrichment": enrichment,
    })
    ingested = _text((document.get("provenance") or {}).get("ingested_at")) if isinstance(document.get("provenance"), Mapping) else ""
    if ingested:
        converted["temporal_hash"] = build_temporal_hash(ingested, record_id)
    return converted


def _is_episode_document(document: Any) -> bool:
    return (isinstance(document, Mapping) and document.get("type") == "episode"
            and list(document.get("coord") or ())[:2] == [9, 1])


def migrate_episode_document(document: dict) -> MigrationResult:
    """Convert ONE historical lobe-9 episode document. Pure: no clock, no I/O, no store."""
    record_id = str(document.get("id") or "") if isinstance(document, Mapping) else ""
    try:
        source_ref = {"record_id": record_id, "sha256": content_hash(document)}
    except ContinuityContractError as exc:
        return MigrationResult(source_ref={"record_id": record_id, "sha256": ""}, episode_id="",
                               status="malformed", errors=(f"the source is not JSON data: {exc}",))

    def malformed(message: str, episode_id: str = "") -> MigrationResult:
        return MigrationResult(source_ref=source_ref, episode_id=episode_id, status="malformed",
                               errors=(message,))

    if not _is_episode_document(document):
        return malformed("the document is not a lobe-9 episode record (type 'episode', coord 9.1)")

    text = document.get("text")
    try:
        body = json.loads(text) if isinstance(text, str) else None
    except ValueError as exc:
        return malformed(f"the record text is not JSON: {exc}")
    if not isinstance(body, dict):
        return malformed("the record text is not a JSON object")
    enrichment = document.get("enrichment") if isinstance(document.get("enrichment"), Mapping) else {}
    episode_id = _text(body.get("episode_id")) or _text(enrichment.get("episode_id"))
    if not episode_id:
        return malformed("the record names no episode id")
    if isinstance(body.get("episode_record"), Mapping):
        try:
            EpisodeRecord.from_dict(body["episode_record"])
        except ContinuityContractError as exc:
            return malformed(f"the record carries a sealed record that does not validate: {exc}",
                             episode_id)
        return MigrationResult(source_ref=source_ref, episode_id=episode_id, status="current")

    d = _Decisions()
    try:
        identity = _identity(body, episode_id, d)
        temporal = _temporal(document, body, d)
        origin = _origin(body, d)
        objective = _objective(body, d)
        outcome = _outcome(body, d)
        metacognition = _metacognition(body, episode_id, d)
        record = EpisodeRecord(
            identity=identity,
            temporal=temporal,
            origin=origin,
            objective=objective,
            events={"refs": [], "by_value": [], "integrity": {},
                    "coverage": {"retained": 0, "referenced": 0, "unavailable_refs": [],
                                 "reason": "this record predates episode event capture (WO-03); "
                                           "no event bytes were ever retained"}},
            outcome=outcome,
            metacognition=metacognition,
            learning={"proposals": [
                {"proposal_id": f"{episode_id}:proposal:{index}", "kind": str(row.get("kind") or ""),
                 "status": str(row.get("status") or "")}
                for index, row in enumerate(metacognition.get("proposals") or ())
                if isinstance(row, Mapping)], "effects": [], "applied_receipts": []},
            coverage=_coverage(d),
        )
    except ContinuityContractError as exc:
        return malformed(f"the source cannot be represented without inventing a value: {exc}",
                         episode_id)
    return MigrationResult(
        source_ref=source_ref,
        episode_id=episode_id,
        status="converted",
        converted_document=_converted_document(document, body, record, source_ref),
        record=record,
        decisions=tuple(d.rows),
    )


# ── the offline CLI ─────────────────────────────────────────────────────────


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _document_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _publish(path: Path, data: bytes) -> None:
    """Atomic publication: a temp file in the same directory, fsynced, then renamed over."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _append_journal(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_journal(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["source"]] = row
    return rows


def _refuse_live(destination: Path, source: Path) -> None:
    from core.paths import OMEGA_ROOT

    resolved = destination.resolve()
    repository = Path(OMEGA_ROOT).resolve()
    if resolved == repository or resolved.is_relative_to(repository):
        raise RefusedDestination(
            f"refusing destination {resolved}: it is inside the live repository {repository}; "
            "the live migration is a separate authorized deployment operation (WO-18)")
    if resolved == source.resolve():
        raise RefusedDestination("the destination must not be the source")


def _sources(source: Path) -> list[tuple[str, Path]]:
    return sorted((p.relative_to(source).as_posix(), p) for p in source.rglob("*.json")
                  if p.is_file() and CONTROL_DIR not in p.relative_to(source).parts)


def _plan_one(rel: str, path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    entry: dict[str, Any] = {"source": rel, "source_sha256": _sha256(data), "raw": data}
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        entry.update(status="malformed", error=f"the source file is not JSON: {exc}")
        return entry
    if not isinstance(document, dict):
        entry.update(status="malformed", error="the source file is not a JSON object")
        return entry
    if not _is_episode_document(document):
        entry["status"] = "ignored"  # another canonical record kind: not a conversion source
        return entry
    result = migrate_episode_document(document)
    entry["result"] = result
    entry["status"] = result.status
    if result.status == "malformed":
        entry["error"] = "; ".join(result.errors)
    elif result.status == "converted":
        entry["output"] = (EPISODE_DIR / f"{result.converted_document['id']}.json").as_posix()
        entry["output_bytes"] = _document_bytes(result.converted_document)
    return entry


def _manifest(all_entries: list[dict[str, Any]], *, conflicts: list[str]) -> dict[str, Any]:
    entries = [e for e in all_entries if e["status"] != "ignored"]
    totals = {"sources": len(entries),
              "converted": sum(1 for e in entries if e["status"] == "converted"),
              "current": sum(1 for e in entries if e["status"] == "current"),
              "malformed": sum(1 for e in entries if e["status"] == "malformed"),
              "conflicts": len(conflicts)}
    unknown: dict[str, int] = {}
    for entry in entries:
        result = entry.get("result")
        for row in (result.decisions if result is not None else ()):
            if row["decision"] in ("not_captured", "diagnostic"):
                unknown[row["field"]] = unknown.get(row["field"], 0) + 1
    return {
        "conversion_version": CONVERSION_VERSION,
        "converter": CONVERTER,
        "totals": totals,
        "complete": totals["malformed"] == 0 and not conflicts,
        "errors": [{"source": e["source"], "error": e["error"]}
                   for e in entries if e["status"] == "malformed"],
        "conflicts": list(conflicts),
        "unknown_fields": dict(sorted(unknown.items())),
        "compatibility_removal_map": COMPATIBILITY_REMOVAL_MAP,
        "ignored": [e["source"] for e in all_entries if e["status"] == "ignored"],
        "records": [{"source": e["source"], "status": e["status"],
                     "record_id": e["output"].rsplit("/", 1)[-1][:-5] if e.get("output") else None}
                    for e in entries],
    }


def _stage(source: Path, destination: Path, *, require_journal: bool) -> dict[str, Any]:
    control = destination / CONTROL_DIR
    journal_path = control / "journal.jsonl"
    journal = _read_journal(journal_path)
    if require_journal and not journal:
        raise MigrationConflict(f"nothing to resume: no journal at {journal_path}")
    entries = []
    for rel, path in _sources(source):
        entry = _plan_one(rel, path)
        entries.append(entry)
        if entry["status"] == "ignored":
            continue
        row = journal.get(rel)
        if row is not None:
            if row["source_sha256"] != entry["source_sha256"]:
                raise MigrationConflict(
                    f"source {rel} changed after it was journalled "
                    f"({row['source_sha256'][:12]} -> {entry['source_sha256'][:12]})")
            if row.get("output"):
                output = destination / row["output"]
                if not output.exists() or _sha256(output.read_bytes()) != row["output_sha256"]:
                    raise MigrationConflict(
                        f"destination {row['output']} no longer matches its journalled hash")
            continue
        if entry["status"] == "malformed":
            _publish(control / "quarantine" / rel, entry["raw"])
            _append_journal(journal_path, {"source": rel, "source_sha256": entry["source_sha256"],
                                           "status": "malformed", "error": entry["error"]})
            continue
        if entry["status"] == "current":
            _append_journal(journal_path, {"source": rel, "source_sha256": entry["source_sha256"],
                                           "status": "current"})
            continue
        output = destination / entry["output"]
        data = entry["output_bytes"]
        if output.exists():
            if _sha256(output.read_bytes()) != _sha256(data):
                raise MigrationConflict(
                    f"destination {entry['output']} already exists with different content; "
                    "it is never overwritten")
        else:
            _publish(output, data)
        _append_journal(journal_path, {"source": rel, "source_sha256": entry["source_sha256"],
                                       "status": "converted", "output": entry["output"],
                                       "output_sha256": _sha256(data)})
    manifest = _manifest(entries, conflicts=[])
    _publish(control / "manifest.json",
             (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return manifest


def _verify(source: Path, destination: Path) -> tuple[bool, dict[str, Any]]:
    from pressure_loop.temporal_store import read_canonical_episode_record

    journal = _read_journal(destination / CONTROL_DIR / "journal.jsonl")
    if not journal:
        raise MigrationConflict("nothing to verify: no journal in the destination")
    problems: list[str] = []
    entries = []
    for rel, path in _sources(source):
        entry = _plan_one(rel, path)
        entries.append(entry)
        if entry["status"] == "ignored":
            continue
        row = journal.get(rel)
        if row is None:
            problems.append(f"{rel}: never staged")
            continue
        if row["source_sha256"] != entry["source_sha256"]:
            problems.append(f"{rel}: source changed after staging")
        if row.get("output"):
            output = destination / row["output"]
            if not output.exists():
                problems.append(f"{row['output']}: missing")
                continue
            data = output.read_bytes()
            if _sha256(data) != row["output_sha256"]:
                problems.append(f"{row['output']}: content differs from its journalled hash")
                continue
            try:
                read_canonical_episode_record(json.loads(data.decode("utf-8")))
            except (ContinuityContractError, ValueError) as exc:
                problems.append(f"{row['output']}: the new-format reader refuses it: {exc}")
    manifest = _manifest(entries, conflicts=problems)
    return not problems, manifest


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog=CONVERTER, description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, type=Path,
                        help="an ISOLATED copy of canonical memory; there is no default")
    parser.add_argument("--destination", required=True, type=Path,
                        help="an isolated output root; never inside the live repository")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="convert in memory; write nothing")
    mode.add_argument("--stage", action="store_true", help="convert and publish; replay-safe")
    mode.add_argument("--resume", action="store_true", help="continue an interrupted stage")
    mode.add_argument("--verify", action="store_true", help="re-check a staged destination")
    args = parser.parse_args(argv)
    source, destination = args.source, args.destination
    try:
        _refuse_live(destination, source)
        if not source.is_dir():
            raise RefusedDestination(f"source {source} is not a directory")
        if args.dry_run:
            entries = [_plan_one(rel, path) for rel, path in _sources(source)]
            manifest = _manifest(entries, conflicts=[])
            print(json.dumps(manifest, indent=2, sort_keys=True))
            return 0 if manifest["complete"] else 2
        if args.verify:
            ok, manifest = _verify(source, destination)
            print(json.dumps(manifest, indent=2, sort_keys=True))
            if not ok:
                for problem in manifest["conflicts"]:
                    print(f"verification failed: {problem}", file=sys.stderr)
                return 1
            return 0 if manifest["complete"] else 2
        manifest = _stage(source, destination, require_journal=args.resume)
        return 0 if manifest["complete"] else 2
    except MigrationConflict as exc:
        print(f"MigrationConflict: {exc}", file=sys.stderr)
        return 1
    except RefusedDestination as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


__all__ = ["CONVERSION_VERSION", "COMPATIBILITY_REMOVAL_MAP", "MigrationResult",
           "RefusedDestination", "main", "migrate_episode_document"]


if __name__ == "__main__":
    sys.exit(main())
