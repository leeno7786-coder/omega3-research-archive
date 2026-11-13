"""Slice 4 — closed episodes persist as lobe-9 (temporal) records."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

TEMPORAL_EPISODE_COORD = [9, 1, 0]  # (9,1) events
TEMPORAL_TRAJECTORY_COORD = [9, 2, 0]  # (9,2) milestones


def _meta_enrichment(meta: dict[str, Any]) -> dict[str, Any]:
    enrichment = meta.get("enrichment")
    return dict(enrichment) if isinstance(enrichment, dict) else {}


def _meta_field(meta: dict[str, Any], key: str, *, default: str = "") -> str:
    enrichment = _meta_enrichment(meta)
    for source in (enrichment, meta):
        value = source.get(key)
        if value is not None and str(value).strip():
            return str(value)
    if key == "closed_at":
        from memory.temporal_authority import authoritative_event_timestamp

        ingested = authoritative_event_timestamp(meta)
        if ingested:
            return str(ingested)
    return default


_OPERATOR_CONTRIBUTORS = ("operator_query", "operator_command", "operator_response")


def _compact(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _episode_gist(ep: Any, body: dict[str, Any], episode_id: str, cascade_status: str) -> tuple[str, str]:
    """A title and summary a recall hit can be READ from.

    The record used to be titled with the episode id and summarised as
    "episode <id> (completed)", so every recalled episode looked the same to the
    answer model (live 2026-09-05: "9.01.2d715a1b — episode a922e2b0 (completed)"
    grounded nothing). The operator's words and the reply's gist are the
    episode; the id stays in enrichment for correlation.
    """
    intent = str(body.get("intent") or body.get("action") or "").strip()
    operator_text = ""
    pressure = getattr(ep, "pressure", None) if not isinstance(ep, dict) else ep.get("pressure")
    for row in list((pressure or {}).get("contributors") or []) if isinstance(pressure, dict) else []:
        if not isinstance(row, dict) or str(row.get("id") or "") not in _OPERATOR_CONTRIBUTORS:
            continue
        detail = dict(row.get("detail") or {})
        operator_text = str(detail.get("message") or detail.get("text") or "").strip()
        if operator_text:
            break
    reply_text = ""
    outcome = body.get("outcome")
    if isinstance(outcome, dict):
        artifacts = outcome.get("artifacts")
        if isinstance(artifacts, dict):
            reply_text = str(artifacts.get("response_text") or artifacts.get("tool_result_summary") or "").strip()
    head = operator_text or reply_text
    if head:
        title = f"{intent or 'episode'}: {_compact(head, 72)}"
    else:
        title = episode_id
    # Labelled as a TRANSCRIPT on purpose: live 2026-09-06 an earlier wrong
    # reply ("you built web-api-design ...") came back through remember_time as
    # a dated record and the next answer repeated it as fact.
    parts = []
    if operator_text:
        parts.append(f"operator said: {_compact(operator_text, 160)}")
    if reply_text:
        parts.append(f"omega replied: {_compact(reply_text, 240)}")
    summary = " → ".join(parts) if parts else f"episode {episode_id} ({cascade_status})"
    if cascade_status != "completed":
        summary = f"[{cascade_status}] {summary}"
    return title, summary


def write_episode_to_temporal(ep: Any) -> Any:
    """Write a closed CognitiveEpisode as a lobe-9 record (additive in 4a).

    Lossless: enriched body dicts (Omega post-reflection) or PriorEpisode distillation
    (CognitiveEpisode path) are stored as canonical JSON in the record text.
    Soft in 4a (additive); becomes the sole/fail-hard write in 4d.

    WO-07: a body carrying its sealed ``episode_record`` is written under the episode's one
    deterministic canonical id (``memory.episode_record_seal.episode_record_id``), so the drain
    can recognise a record it already wrote and every later reader can name it. The intake
    receipt is RETURNED: it is the recorded intent of this canonical write, and a refused
    intake raises here rather than leaving the terminus to believe the record was held.
    """
    from memory.api import CanonicalEnvelope, intake_memory, intake_receipt_accepted
    from pressure_loop.episode_journal import prior_episode_from_episode

    if isinstance(ep, dict):
        body_dict = dict(ep)
        episode_id = str(body_dict.get("episode_id") or "")
        # The enriched body carries no close time of its own; the sealed record inside it does.
        # Reading only the body wrote an empty closed_at beside an observed one.
        closed_at = str(body_dict.get("closed_at") or _sealed_close_time(body_dict))
        cascade_status = str(body_dict.get("cascade_status") or "completed")
        canonical_text = json.dumps(body_dict, ensure_ascii=False, sort_keys=True)
        body_for_gist = body_dict
    else:
        pe = prior_episode_from_episode(ep)
        closed_at = str(getattr(ep, "closed_at", "") or "")
        pe_dict = pe.to_dict()
        episode_id = str(pe_dict.get("episode_id") or getattr(ep, "episode_id", "") or "")
        cascade_status = str(pe_dict.get("cascade_status") or "completed")
        canonical_text = json.dumps(pe_dict, ensure_ascii=False, sort_keys=True)
        body_for_gist = pe_dict
    title, summary = _episode_gist(ep, body_for_gist, episode_id, cascade_status)
    record_id = None
    if isinstance(ep, dict) and isinstance(ep.get("episode_record"), dict):
        from memory.episode_record_seal import episode_record_id

        record_id = episode_record_id(episode_id)
    payload = {
        "title": title,
        "summary": summary,
        "text": canonical_text,
        "subtopic": "episode",
        "enrichment": {
            "episode_id": episode_id,
            "closed_at": closed_at,
            "cascade_status": cascade_status,
            "event_kind": "auto_outcome",
            "canonical": True,
        },
    }
    if record_id:
        payload["id"] = record_id
    env = CanonicalEnvelope(
        cycle_id=episode_id or f"episode-{closed_at}",
        coord=list(TEMPORAL_EPISODE_COORD),
        type="episode",
        payload=payload,
        source="lobe:temporal",
        id=record_id,
    )
    receipt = intake_memory(env, propose=False, bypass_guardian=True, skip_embed=False)
    if not intake_receipt_accepted(receipt):
        raise RuntimeError(
            f"episode {episode_id!r} lobe-9 intake was refused: "
            f"{getattr(receipt, 'message', None) or getattr(receipt, 'validation_status', '')}"
        )
    return receipt


def _sealed_close_time(body: dict[str, Any]) -> str:
    """The observed close time of the sealed record a body carries, or "" when it has none."""
    record = body.get("episode_record")
    temporal = record.get("temporal") if isinstance(record, dict) else None
    closed = temporal.get("closed_at") if isinstance(temporal, dict) else None
    if isinstance(closed, dict) and closed.get("state") == "observed":
        return str(closed.get("value") or "")
    return ""


def _episode_record_state(document: Any) -> tuple[str, Any]:
    """Classify one canonical lobe-9 episode document for the new-format reader (WO-08).

    ``("current", EpisodeRecord)`` — it carries a sealed record of the activated version;
    ``("requires_migration", reason)`` — it predates the sealed format, or carries an older
    version: an explicit conversion requirement, never empty history;
    ``("malformed", reason)`` — its text or its sealed record cannot be read at all.
    """
    from schema.continuity_json import ContinuityContractError
    from schema.episode_record import SCHEMA_VERSION, EpisodeRecord

    record_id = str(document.get("id") or "") if isinstance(document, dict) else ""
    text = document.get("text") if isinstance(document, dict) else None
    try:
        body = json.loads(text) if isinstance(text, str) else None
    except ValueError as exc:
        return "malformed", f"canonical record {record_id!r} text is not JSON: {exc}"
    if not isinstance(body, dict):
        return "malformed", f"canonical record {record_id!r} text is not a JSON object"
    sealed = body.get("episode_record")
    if not isinstance(sealed, dict):
        return "requires_migration", (
            f"canonical episode record {record_id!r} predates the sealed format; convert it with "
            "scripts/migrate_episode_continuity.py — it is not empty history")
    if sealed.get("schema_version") != SCHEMA_VERSION:
        return "requires_migration", (
            f"canonical episode record {record_id!r} carries sealed version "
            f"{sealed.get('schema_version')!r}, not the activated {SCHEMA_VERSION!r}")
    try:
        return "current", EpisodeRecord.from_dict(sealed)
    except ContinuityContractError as exc:
        return "malformed", f"canonical record {record_id!r} carries a sealed record that does not validate: {exc}"


def read_canonical_episode_record(document: dict) -> Any:
    """The NEW-FORMAT-ONLY reader of a canonical lobe-9 episode document (WO-08).

    Returns the sealed ``EpisodeRecord``. An old-format record is an explicit migration
    requirement (``CanonicalEpisodeUnavailable``, "migration required: ..."), never an empty or
    guessed record: there is no runtime dual-shape reader, and history is converted offline.
    """
    from schema.continuity_json import CanonicalEpisodeUnavailable

    state, value = _episode_record_state(document)
    if state == "current":
        return value
    if state == "requires_migration":
        raise CanonicalEpisodeUnavailable(f"migration required: {value}")
    raise CanonicalEpisodeUnavailable(value)


def episode_migration_preflight(memory_root: Any = None) -> dict[str, Any]:
    """Activation preflight: classify every canonical lobe-9 episode record under *memory_root*.

    Read-only. ``ready`` is true only when every episode record is current — an old record is
    listed under ``requires_migration`` and a broken one under ``malformed``, so a store that has
    not been converted reads as work owed, never as an empty history.
    """
    from pathlib import Path

    from core import paths as core_paths

    root = Path(memory_root) if memory_root is not None else Path(core_paths.MEMORY_ROOT)
    report: dict[str, Any] = {"current": [], "requires_migration": [], "malformed": [],
                              "reasons": {}}
    for path in sorted((root / "900_temporal").rglob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            report["malformed"].append(path.name)
            report["reasons"][path.name] = f"unreadable: {exc}"
            continue
        if not isinstance(document, dict) or document.get("type") != "episode" or list(
            document.get("coord") or ()
        )[:2] != [9, 1]:
            continue
        state, value = _episode_record_state(document)
        record_id = str(document.get("id") or path.stem)
        report[state].append(record_id)
        if state != "current":
            report["reasons"][record_id] = value
    report["ready"] = not report["requires_migration"] and not report["malformed"]
    return report


TEMPORAL_CONSOLIDATION_COORD = [9, 3, 0]  # (9,3) deepsleep/omega consolidation
TEMPORAL_CAPABILITY_COORD = [9, 4, 0]  # (9,4) capability reinforce/weaken signals


def _require_intake_accepted(receipt: Any, record_type: str, subject: str = "") -> Any:
    """Fail loudly when a lobe-9 intake refused, or answered with an unusable receipt.

    Same discipline `write_episode_to_temporal` already applies: the receipt is the
    recorded intent of the canonical write, so a rejected envelope — or a receipt
    carrying no `validation_status` to read at all — must never return to the caller
    looking exactly like a held one. These writers used to call `intake_memory` bare,
    so a dropped consolidation, capability signal or trajectory reported success on
    both sides of the boundary.
    """
    from memory.api import intake_receipt_accepted

    if intake_receipt_accepted(receipt):
        return receipt
    status = str(getattr(receipt, "validation_status", "") or "")
    detail = (
        str(getattr(receipt, "message", None) or "")
        or status
        or f"unusable receipt {type(receipt).__name__}"
    )
    named = f"{record_type} {subject!r}" if subject else record_type
    raise RuntimeError(f"{named} lobe-9 intake was refused: {detail}")


def _temporal_canonical_write(rec: dict[str, Any], coord: list[int], type_tag: str) -> None:
    """Shared lobe-9 canonical writer for consolidation/capability records.

    Guardian-free direct write (propose=False, bypass_guardian=True), embed ON.
    The full record is canonical JSON in `text`; event_kind is preserved in
    enrichment so the Stage-2 graph reader can reconstruct it.

    A refused intake raises: the caller may not treat a dropped record as written.
    """
    from memory.api import CanonicalEnvelope, intake_memory

    body = dict(rec or {})
    episode_id = str(body.get("episode_id") or "")
    event_kind = str(body.get("event_kind") or type_tag)
    closed_at = str(body.get("closed_at") or _now_iso())
    canonical_text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    env = CanonicalEnvelope(
        cycle_id=episode_id or f"{type_tag}-{closed_at}",
        coord=list(coord),
        type=type_tag,
        payload={
            "title": str(body.get("title") or f"{type_tag} {episode_id}".strip()),
            "summary": str(body.get("summary") or f"{type_tag} {episode_id}".strip()),
            "text": canonical_text,
            "subtopic": type_tag,
            "enrichment": {
                "episode_id": episode_id,
                "closed_at": closed_at,
                "event_kind": event_kind,
                "event_id": body.get("event_id") or f"{type_tag}-{episode_id}",
                "canonical": True,
            },
        },
        source="lobe:temporal",
    )
    receipt = intake_memory(env, propose=False, bypass_guardian=True, skip_embed=False)
    _require_intake_accepted(receipt, type_tag, episode_id)


def write_consolidation_to_temporal(rec: dict[str, Any]) -> None:
    """Persist a deepsleep/omega consolidation summary at coord [9,3,0]."""
    _temporal_canonical_write(rec, TEMPORAL_CONSOLIDATION_COORD, "consolidation")


def write_capability_to_temporal(rec: dict[str, Any]) -> None:
    """Persist a capability signal (reinforced/weakened) at coord [9,4,0]."""
    _temporal_canonical_write(rec, TEMPORAL_CAPABILITY_COORD, "capability")


def _now_iso() -> str:
    # Microsecond precision: read_trajectory resolves the active record by max
    # closed_at (lexicographic) — second resolution made two same-second
    # trajectory writes tie on store-iteration order.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _load_subdomain_metas(subdomain: int) -> list[dict[str, Any]]:
    from memory.engine.walker import (
        COORD_INDEX,
        list_ids_in_subdomain,
        load_coord_index,
        load_meta_by_path,
    )

    load_coord_index()
    metas: list[dict[str, Any]] = []
    for idstr in list_ids_in_subdomain(9, subdomain):
        path = (COORD_INDEX.get(idstr) or {}).get("path")
        if not path:
            continue
        meta = load_meta_by_path(path)
        if isinstance(meta, dict):
            metas.append(meta)
    return metas


def hydrate_reflection_window(window: Any) -> Any:
    """Seal a staged cohort and hydrate the exact canonical revisions it pins (WO-09).

    Reflection checkpoints persist ordered episode ids, the exact ``CanonicalRef`` each member
    joined as, and control metadata. At consumption the cohort is sealed against the served
    projection and every member is read by its record id with its content hash verified
    (``omega_loop.window.membership``) — a missing, corrupt or changed member raises. This used
    to scan today's lobe-9 subdomain and keep the document with the greatest ``(closed_at, id)``
    per episode, so any later document naming a member silently replaced its evidence.

    The bodies, sealed records and mastery effects set here are transient caches for the
    reflection that consumes the window; each body carries the ``canonical_ref`` it was read
    from, so everything derived from it can cite its exact source.
    """
    from omega_loop.window.membership import seal_and_hydrate

    manifest, members = seal_and_hydrate(window)
    bodies: list[dict[str, Any]] = []
    deltas: list[dict[str, Any]] = []
    for member in members:
        body = dict(member.body)
        body["canonical_ref"] = member.ref.to_dict()
        bodies.append(body)
        mastery = body.get("mastery_effects")
        if isinstance(mastery, dict) and mastery:
            deltas.append(
                {
                    "episode_id": member.record.episode_id,
                    "mastery_effects": dict(mastery),
                }
            )
    window.manifest = manifest
    window.episode_records = [member.record for member in members]
    window.episode_bodies = bodies
    window.episode_mastery_deltas = deltas
    return window


def _latest_record_by_closed_at(
    metas: list[dict[str, Any]], *, record_type: str
) -> dict[str, Any] | None:
    typed = [m for m in metas if str(m.get("type") or "") == record_type]
    if not typed:
        return None
    return max(
        typed,
        key=lambda m: (
            _meta_field(m, "closed_at"),
            _meta_field(m, "episode_id") or str(m.get("title") or ""),
        ),
    )


def _validated_trajectory_dict(blob: dict[str, Any]) -> dict[str, Any]:
    """Normalize + validate a trajectory blob; raise ValueError when malformed."""
    import uuid

    from schema.omega_trajectory import OmegaTrajectory

    raw = dict(blob or {})
    raw.pop("confidence", None)
    raw.pop("trajectory_diff", None)
    if not raw.get("cycle_id"):
        raw["cycle_id"] = str(raw.pop("source_window_id", None) or "legacy-baseline")
    if not raw.get("trajectory_id"):
        raw["trajectory_id"] = uuid.uuid4().hex[:12]
    traj = OmegaTrajectory.from_dict(raw)
    return traj.to_dict()


def read_trajectory() -> dict[str, Any]:
    """Latest trajectory payload from lobe 9 (or {})."""
    latest = _latest_record_by_closed_at(_load_subdomain_metas(2), record_type="trajectory")
    if latest is None:
        return {}
    text = latest.get("text")
    if isinstance(text, str) and text.strip():
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return _validated_trajectory_dict(parsed)
        except json.JSONDecodeError as exc:
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "pressure",
                    "read_trajectory",
                    status="fail",
                    reason="corrupt_record",
                    ok=False,
                    error=str(exc),
                )
            except Exception:
                pass
            raise RuntimeError(f"corrupt trajectory record in lobe 9: {exc}") from exc
    payload = {
        k: v
        for k, v in latest.items()
        if k
        not in {
            "title",
            "summary",
            "text",
            "type",
            "episode_id",
            "closed_at",
            "coord",
            "source",
        }
    }
    if not payload:
        return {}
    return _validated_trajectory_dict(payload)


def write_trajectory(
    traj: dict[str, Any], *, record_type: str = "trajectory", event_id: str | None = None
) -> None:
    """Persist a trajectory record to lobe 9 (continuity write).

    record_type="trajectory" is the ACTIVE setpoint read_trajectory resolves.
    record_type="trajectory_superseded" persists an abandoned trajectory as
    history (the supersedes chain) without ever shadowing the active read —
    read_trajectory filters on type=="trajectory" only.

    WO-07: a caller that names the write with a stable ``event_id`` (DeepSleep's trajectory
    pass does) gets it once. A retried terminus finds the intake row its earlier attempt
    journalled and reconciles from it, instead of a second record under a new close time.
    That reconciliation returns before intake is entered at all; a write that DOES enter
    intake and is refused raises, so no caller reads a dropped setpoint as persisted.
    """
    from memory.api import CanonicalEnvelope, intake_memory

    if event_id:
        from memory import intake_journal

        existing = intake_journal.record_by_id(intake_journal._record_id(event_id))
        if existing is not None and existing.status != intake_journal.REJECTED:
            return

    payload = _validated_trajectory_dict(dict(traj or {}))
    closed_at = _now_iso()
    title = "current_trajectory" if record_type == "trajectory" else "superseded_trajectory"
    env = CanonicalEnvelope(
        cycle_id=f"{record_type}-{closed_at}",
        coord=list(TEMPORAL_TRAJECTORY_COORD),
        type=record_type,
        payload={
            "title": title,
            "summary": "omega trajectory baseline",
            "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "subtopic": record_type,
            "enrichment": {"closed_at": closed_at, **payload},
        },
        source="lobe:temporal",
    )
    receipt = intake_memory(
        env, propose=False, bypass_guardian=True, skip_embed=True, event_id=event_id
    )
    _require_intake_accepted(receipt, record_type, str(payload.get("trajectory_id") or ""))
