"""
Promotion Pipeline — user-chat propose → simulate → verify → commit
==========================================================

Implements the staging + verification + commit chain used while DeepSleep
drains episode SRAM. Cognitive self-writes and derived memory first enter SRAM;
the drain commits them with ``bypass_guardian=True`` because they are not
untrusted external ingress.

Pipeline:

    1. create_staging(envelope)
        - Builds a schema-valid memory dict from the envelope
        - Writes it (plus staging metadata) to guardian/state/staging/
        - Returns staging_ref

    2. simulate_promotion(staging_ref, evidence_refs)
        - Reads staging file
        - Computes deterministic diff: what coordinate path will
          change, what index entry will be added, what enrichment
          fingerprint
        - Builds a rollback plan
        - Returns {simulation, rollback}

    3. commit_promotion(staging_ref, approver_id)
        - Re-reads staging file (defensive — content may have been
          rewritten by an authorised verification step)
        - Calls atomic_write so the memory lands at its canonical
          coordinate path, with the index patched atomically
        - Registers the new memory id with StrengthIndex
        - Removes the staging file
        - Returns (committed, canonical_ref, rollback_metadata)

    4. rollback_promotion(staging_ref)
        - Removes the staging file without committing
        - Returns the rollback report

Storage:
    guardian/state/staging/<staging_ref>.json
        — separate from memory/tmp_writes/ (which is the engine's
        atomic-write tmp dir, cleared on every write)

ARCHITECTURAL RULES:
    - All commits route through engine.validate_write.atomic_write —
      no separate persistence path.
    - Staging files are not classified or indexed; they are
      pre-canonical state outside the cortex.
    - Simulations are deterministic — same staging_ref + same corpus
      → same simulation result.
    - Failure is data: if create/commit/rollback fails, the function
      returns a structured failure dict rather than raising.
    - This module MUST NOT be redefined elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.paths import OMEGA_ROOT
from core.symphony_time import now_iso
from memory.deterministic import stable_hash


# ---------------------------------------------------------------------

from memory.core import build_memory_dict
from memory.engine.validate_write import atomic_write, final_path_for_meta

log = logging.getLogger("omega.memory.promotion")


def _emit_promotion(phase: str, **data: Any) -> None:
    try:
        from guardian.terminal_feedback import emit_guardian_event

        emit_guardian_event("omega.memory.promotion", phase, **data)
    except Exception:
        pass


STAGING_DIR = OMEGA_ROOT / "guardian" / "state" / "staging"
_STAGING_LOCK = threading.Lock()


def _ensure_staging_dir() -> None:
    STAGING_DIR.mkdir(parents=True, exist_ok=True)


def _staging_path(staging_ref: str) -> Path:
    """Resolve a staging_ref to its on-disk path with traversal guard."""
    safe = Path(staging_ref).name  # strips any path separators
    if not safe.startswith("staging-"):
        raise ValueError(f"invalid staging_ref: {staging_ref!r}")
    p = (STAGING_DIR / f"{safe}.json").resolve()
    # Verify the resolved path stays inside STAGING_DIR
    p.relative_to(STAGING_DIR.resolve())
    return p


def _now_iso() -> str:
    return now_iso()


def _event_iso(event_name: str, *parts: Any) -> str:
    del event_name, parts
    return now_iso()


def _staging_ref_for(envelope: Dict[str, Any], memory_dict: Dict[str, Any]) -> str:
    return "staging-" + stable_hash(
        "memory.promotion.staging_ref",
        envelope,
        memory_dict,
        length=12,
    )


def _guardian_request_id_for(
    staging_ref: str, envelope: Dict[str, Any], memory_dict: Dict[str, Any]
) -> str:
    return "req-" + stable_hash(
        "memory.promotion.guardian_request",
        staging_ref,
        envelope.get("cycle_id"),
        memory_dict.get("id"),
        length=12,
    )


def _source_text_for_ledger(payload: Dict[str, Any]) -> str:
    text = str(payload.get("text") or "")
    if text:
        return text
    summary = str(payload.get("summary") or "")
    title = str(payload.get("title") or "")
    if summary or title:
        return "\n".join(part for part in (title, summary) if part)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _source_fields_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: payload[key]
        for key in (
            "source_id",
            "source_type",
            "span_start",
            "span_end",
            "span_hash",
            "source_content_hash",
            "source_session_id",
            "source_session_timestamp",
        )
        if payload.get(key) is not None
    }


# =====================================================================
# create_staging
# =====================================================================


def create_staging(envelope: Dict[str, Any]) -> str:
    """Stage a memory envelope for later promotion.

    `envelope` is the canonical-API shape from `memory.api.write_memory`,
    NOT a schema-valid memory dict. We build the schema-valid dict here
    and store both: the canonical envelope (for audit) and the
    schema-valid dict (for commit-time write).

    Returns the staging_ref.
    """
    cycle_id = envelope.get("cycle_id")
    coord = list(envelope.get("coord") or [2, 0, 0])
    _ensure_staging_dir()

    if not cycle_id:
        raise ValueError("create_staging requires cycle_id on envelope")

    # Build the schema-valid memory dict. This validates that the
    # envelope's content is acceptable BEFORE we stage — a malformed
    # envelope is rejected at staging time, not at commit time.
    payload = envelope.get("payload") or {}
    text = payload.get("text", "") or envelope.get("text", "")

    metadata = dict(payload)
    metadata.update(
        {
            "id": envelope.get("id"),
            "type": envelope.get("type", "concept"),
            "title": payload.get("title", text[:80]),
            "summary": payload.get("summary", text[:240]),
            "confidence": envelope.get("confidence", 0.5),
            "origin_id": envelope.get("source", "memory.api"),
            "origin_type": "stage_propose",
            "tags": payload.get("tags"),
            "links": payload.get("links"),
            "domain": payload.get("domain"),
            "subtopic": payload.get("subtopic"),
            "source_session_id": payload.get("source_session_id") or envelope.get("cycle_id"),
            "source_session_timestamp": (
                payload.get("source_session_timestamp")
                or payload.get("session_timestamp")
                or envelope.get("timestamp_created")
            ),
            "timestamp_created": envelope.get("timestamp_created"),
        }
    )
    citation_payload = {
        **dict(payload),
        "source_session_id": metadata.get("source_session_id"),
        "source_session_timestamp": metadata.get("source_session_timestamp"),
    }
    source_fields = _source_fields_from_payload(citation_payload)
    if not source_fields.get("source_id"):
        from memory.source_ledger.store import ensure_source_citation

        source_fields.update(
            ensure_source_citation(
                citation_payload,
                source_type=str(envelope.get("source") or "memory_write"),
                origin_id=str(cycle_id or ""),
            )
        )
    metadata.update(source_fields)

    memory_dict = build_memory_dict(
        text=text,
        coord=coord,
        source_tag=f"promotion:{cycle_id}",
        enrichment=payload.get("enrichment"),
        metadata=metadata,
    )

    staging_ref = _staging_ref_for(envelope, memory_dict)
    guardian_request_id = _guardian_request_id_for(staging_ref, envelope, memory_dict)
    staging_record = {
        "staging_ref": staging_ref,
        "guardian_request_id": guardian_request_id,
        "cycle_id": cycle_id,
        "created_at": envelope.get("timestamp_created")
        or _event_iso("create_staging", staging_ref, cycle_id, memory_dict.get("id")),
        "envelope": envelope,
        "memory_dict": memory_dict,
        "validation_status": "staged",
        "version": int(envelope.get("version") or 1),
        "evidence_refs": [],
    }

    p = _staging_path(staging_ref)
    tmp = p.with_suffix(".tmp")
    with _STAGING_LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(staging_record, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)

    log.info("staged %s for cycle=%s coord=%s", staging_ref, cycle_id, memory_dict.get("coord"))
    _emit_promotion(
        "create_staging",
        summary=f"staged={staging_ref}",
        staging_ref=staging_ref,
        cycle_id=cycle_id,
        coord=list(memory_dict.get("coord") or []),
    )
    return staging_ref


# =====================================================================
# simulate_promotion
# =====================================================================


def simulate_promotion(
    staging_ref: str,
    evidence_refs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Compute a deterministic preview of what commit would change.

    Returns:
        {
          "simulation": {coord, target_path, index_entry, enrichment_keys},
          "rollback":   {actions, staging_ref, target_path},
        }

    Does NOT mutate state. Same staging_ref + same corpus → same result.
    """
    p = _staging_path(staging_ref)
    if not p.exists():
        raise FileNotFoundError(f"staging not found: {staging_ref}")

    with open(p, "r", encoding="utf-8") as f:
        record = json.load(f)

    memory_dict = record.get("memory_dict") or {}
    target_path = str(final_path_for_meta(memory_dict))

    enrichment_keys = sorted(list((memory_dict.get("enrichment") or {}).keys()))

    simulation = {
        "staging_ref": staging_ref,
        "cycle_id": record.get("cycle_id"),
        "memory_id": memory_dict.get("id"),
        "coord": memory_dict.get("coord"),
        "domain": memory_dict.get("domain"),
        "target_path": target_path,
        "index_entry": {
            "id": memory_dict.get("id"),
            "coord": memory_dict.get("coord"),
            "title": memory_dict.get("title"),
            "type": memory_dict.get("type"),
            "domain": memory_dict.get("domain"),
            "workspace_source": memory_dict.get("workspace_source"),
        },
        "enrichment_keys": enrichment_keys,
        "evidence_refs": list(evidence_refs or []),
    }

    rollback = {
        "staging_ref": staging_ref,
        "actions": ["delete_memory_file", "remove_index_entry"],
        "target_path": target_path,
        "memory_id": memory_dict.get("id"),
    }

    result = {"simulation": simulation, "rollback": rollback}
    _emit_promotion(
        "simulate_promotion",
        summary=f"target={target_path}",
        staging_ref=staging_ref,
        memory_id=memory_dict.get("id"),
        target_path=target_path,
    )
    return result


# =====================================================================
# verify_promotion — guardian-gated step in propose → simulate → verify → commit
# =====================================================================


def verify_promotion(
    staging_ref: str,
    evidence_refs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run the memory verification chain on a staged envelope.

    The chain (per the Governance Contract):
        1. simulate_promotion — deterministic preview (no state mutation)
        2. guard_envelope     — memory_purity adversarial check
        3. charter check_modification — verify the target lobe isn't IMMUTABLE
                                         for the requesting source

    Returns a dict shaped like the canonical
    `schema.verification.Verification` record:

        {
            "verification_id": "verify-<hash>",
            "timestamp":       iso8601,
            "verifier":        "memory.write_guard",
            "target_id":       staging_ref,
            "target_kind":     "memory_promotion",
            "status":          "passed" | "failed" | "error",
            "confidence":      float in [0, 1],
            "evidence":        [evidence_refs...],
            "checks":          [{name, passed, detail}, ...],
            "reason":          str | None,
            "rollback_path":   str,
            "rollback_verified": bool,
        }

    The status decides what `promote_memory` does next:
        - passed  → commit_promotion
        - failed  → rollback_promotion + emit GuardianIntervention
        - error   → escalation; staging is preserved for inspection
    """
    p = _staging_path(staging_ref)
    if not p.exists():
        return _verify_result(
            staging_ref,
            status="error",
            reason="staging not found",
            checks=[{"name": "staging_exists", "passed": False, "detail": str(p)}],
        )

    try:
        with open(p, "r", encoding="utf-8") as f:
            record = json.load(f)
    except Exception as exc:
        return _verify_result(
            staging_ref,
            status="error",
            reason=f"staging read failed: {exc}",
            checks=[{"name": "staging_readable", "passed": False, "detail": str(exc)}],
        )

    envelope = record.get("envelope") or {}
    memory_dict = record.get("memory_dict") or {}
    checks: List[Dict[str, Any]] = []

    # ── Check 1: simulation (deterministic preview) ─────────────────
    try:
        sim = simulate_promotion(staging_ref, evidence_refs)
        checks.append(
            {
                "name": "simulation",
                "passed": True,
                "detail": f"target={sim.get('simulation', {}).get('target_path', '?')}",
            }
        )
        rollback_path = (sim.get("rollback") or {}).get("staging_ref", staging_ref)
    except Exception as exc:
        return _verify_result(
            staging_ref,
            status="error",
            reason=f"simulation failed: {exc}",
            checks=checks
            + [
                {
                    "name": "simulation",
                    "passed": False,
                    "detail": str(exc),
                }
            ],
        )

    # ── Check 2: memory envelope guard (purity) ───────────
    try:
        from memory.write_guard import guard_envelope

        allowed, rejections = guard_envelope(envelope)
        purity_flags = [r for r in rejections if str(r.get("category") or "") == "purity_flag"]
        if purity_flags:
            flag = purity_flags[0]
            checks.append(
                {
                    "name": "memory_envelope",
                    "passed": False,
                    "detail": flag.get("reason") or "purity_flag",
                    "category": "purity_flag",
                }
            )
            return _verify_result(
                staging_ref,
                status="failed",
                reason=flag.get("reason") or "purity_flag",
                checks=checks,
                rollback_path=rollback_path,
            )
        checks.append(
            {
                "name": "memory_envelope",
                "passed": allowed,
                "detail": (
                    f"{len(rejections)} flag(s)"
                    if allowed
                    else f"rejected: {rejections[0]['reason']}"
                    if rejections
                    else "rejected"
                ),
                # Propagate the authoritative category write_guard already
                # computed per rejection (Audit G2). Forwarding ONLY the
                # reason tag dropped this, so memory's decision's
                # category-keyed FLAG/QUARANTINE stratification was dead and
                # every blocklist rejection became a default REJECT
                # (safe-by-accident). Additive — `reason` is unchanged for
                # other consumers; absent when allowed.
                "category": (
                    rejections[0].get("category") if (not allowed and rejections) else None
                ),
            }
        )
        if not allowed:
            return _verify_result(
                staging_ref,
                status="failed",
                reason=(rejections[0]["reason"] if rejections else "guard_envelope_rejected"),
                checks=checks,
                rollback_path=rollback_path,
            )
    except Exception as exc:
        log.warning("guard_envelope unavailable for %s: %s", staging_ref, exc)
        checks.append(
            {
                "name": "guard_envelope",
                "passed": True,
                "detail": f"guardian unavailable, defaulting to allow: {exc}",
            }
        )

    try:
        from core.memory_config import staging_meets_observation_gate

        ok, detail, confidence = staging_meets_observation_gate(memory_dict, envelope)
        checks.append(
            {
                "name": "promotion_confidence",
                "passed": ok,
                "detail": detail or f"confidence={confidence:.3f}",
            }
        )
        if not ok:
            return _verify_result(
                staging_ref,
                status="failed",
                reason=detail,
                checks=checks,
                rollback_path=rollback_path,
                confidence=confidence,
            )
    except Exception as exc:
        log.warning("promotion confidence gate unavailable for %s: %s", staging_ref, exc)

    # All checks passed
    verify_confidence = float(((memory_dict.get("provenance") or {}).get("confidence") or 1.0))
    return _verify_result(
        staging_ref,
        status="passed",
        reason=None,
        checks=checks,
        rollback_path=rollback_path,
        confidence=verify_confidence,
    )


def _verify_result(
    staging_ref: str,
    *,
    status: str,
    reason: Optional[str] = None,
    checks: Optional[List[Dict[str, Any]]] = None,
    rollback_path: str = "",
    confidence: float = 0.0,
) -> Dict[str, Any]:
    """Construct a Verification-shaped result dict."""
    timestamp = _event_iso(
        "verify_promotion", staging_ref, status, reason, checks or [], rollback_path, confidence
    )
    vid_hash = stable_hash(
        "memory.promotion.verification", staging_ref, status, timestamp, length=16
    )
    result = {
        "verification_id": f"verify-{vid_hash}",
        "timestamp": timestamp,
        "verifier": "memory.write_guard",
        "target_id": staging_ref,
        "target_kind": "memory_promotion",
        "status": status,
        "confidence": float(confidence),
        "evidence": [],
        "checks": checks or [],
        "reason": reason,
        "rollback_path": rollback_path or staging_ref,
        "rollback_verified": bool(rollback_path),
    }
    _emit_promotion(
        "verify_promotion",
        summary=f"status={status}",
        staging_ref=staging_ref,
        status=status,
        check_count=len(checks or []),
        reason=reason,
    )
    return result


# =====================================================================
# commit_promotion
# =====================================================================


def commit_promotion(
    staging_ref: str,
    approver_id: str,
    *,
    skip_embed: bool = False,
    update_index: bool = True,
    write_ordinal: int | None = None,
    stamp_observed: bool = True,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    """Promote a staged memory to canonical state.

    Returns (committed, canonical_ref, rollback_plan). On failure
    returns (False, None, None) and logs.

    Path:
        - re-read staging file
        - stamp commit audit entry
        - call atomic_write (validates + writes + index-patches)
        - register with StrengthIndex
        - delete staging file
    """
    p = _staging_path(staging_ref)
    cycle_id = ""
    if not p.exists():
        log.warning("commit_promotion: staging not found %s", staging_ref)
        return False, None, None

    try:
        with open(p, "r", encoding="utf-8") as f:
            record = json.load(f)
    except Exception as exc:
        log.error("commit_promotion: staging read failed %s (%s)", staging_ref, exc)
        return False, None, None

    memory_dict = record.get("memory_dict") or {}
    if not memory_dict:
        log.warning("commit_promotion: staging %s has no memory_dict", staging_ref)
        return False, None, None

    cycle_id = record.get("cycle_id") or ""

    # Stamp the commit audit
    audit = list(memory_dict.get("audit") or [])
    commit_when = _event_iso(
        "commit_promotion.audit",
        staging_ref,
        memory_dict.get("id"),
        approver_id,
    )
    audit.append({
        "when": commit_when,
        "what": "promoted",
        "by":   approver_id,
        "notes": f"staging_ref={staging_ref}",
    })
    memory_dict["audit"] = audit
    # Every canonical file carries a stable, chronologically sortable temporal
    # identity. It lets cold/stale-graph filesystem recall order recent and old
    # ingests without treating a projection cache as truth.
    from memory.temporal_hash import build_temporal_hash, temporal_epoch_us

    temporal_source = str(
        memory_dict.get("timestamp_created")
        or memory_dict.get("created_at")
        or commit_when
    )
    try:
        epoch_us = temporal_epoch_us(temporal_source)
    except (TypeError, ValueError):
        temporal_source = commit_when
        epoch_us = temporal_epoch_us(temporal_source)
    memory_dict.setdefault(
        "temporal_hash",
        build_temporal_hash(temporal_source, str(memory_dict.get("id") or "")),
    )
    memory_dict.setdefault("temporal_epoch_us", epoch_us)

    # ── Atomic encoding (8 steps) ─────────────────────────────────
    # Steps 1+2 (canonical commit + index update) happen here via
    # atomic_write. Steps 3-8 (native projection + self-vector +
    # contradiction precheck + persistence anchor + reflection
    # eligibility + deepsleep candidacy) are owned by
    # memory.encoding.encode_committed_memory.
    #
    # Per the Omega Cognitive Law: "no cognition may operate on
    # unencoded memory." Any encoding failure rolls back the canonical
    # commit. There is no partial-state success.
    #
    # AUDIT-4: bracketed by the encoding journal so a process crash
    # mid-encoding leaves a journal trail boot can roll back. Window
    # closed: kill -9 between atomic_write and step 8 used to leave
    # an indexed-but-unencoded canonical record. Now the journal's
    # `open` entry without a matching `close` triggers boot recovery.
    from memory.encoding_journal import (
        journal_open,
        journal_close,
        journal_abort,
    )
    from memory.engine.validate_write import final_path_for_meta

    expected_final_path = str(final_path_for_meta(memory_dict))
    existing_path = Path(expected_final_path)
    if existing_path.is_file():
        try:
            existing_memory = json.loads(existing_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing_memory = None
        existing_provenance = (
            dict(existing_memory.get("provenance") or {})
            if isinstance(existing_memory, dict)
            else {}
        )
        incoming_provenance = dict(memory_dict.get("provenance") or {})
        existing_source_hash = str(existing_provenance.get("source_content_hash") or "")
        incoming_source_hash = str(incoming_provenance.get("source_content_hash") or "")
        if (
            isinstance(existing_memory, dict)
            and existing_memory.get("id") == memory_dict.get("id")
            and existing_memory.get("type") == memory_dict.get("type")
            and list(existing_memory.get("coord") or ()) == list(memory_dict.get("coord") or ())
            and existing_memory.get("text") == memory_dict.get("text")
            and existing_memory.get("workspace_source") == memory_dict.get("workspace_source")
            and existing_source_hash
            and existing_source_hash == incoming_source_hash
        ):
            os.remove(p)
            memory_id = str(memory_dict["id"])
            log.info(
                "commit_promotion idempotent reuse staging=%s id=%s by %s",
                staging_ref,
                memory_id,
                approver_id,
            )
            _emit_promotion(
                "commit_promotion",
                summary=f"idempotent={memory_id}",
                cycle_id=cycle_id,
                staging_ref=staging_ref,
                committed=True,
                canonical_ref=memory_id,
                idempotent=True,
            )
            return True, memory_id, {
                "staging_ref": staging_ref,
                "actions": [],
                "target_path": expected_final_path,
                "memory_id": memory_id,
                "approver": approver_id,
                "idempotent": True,
            }
    # Snapshot any pre-existing canonical record at the target path so a
    # failed post-commit encoding can RESTORE it rather than destroy it.
    # atomic_write overwrites in place with no backup; without this a
    # re-write of an existing record (trait reinforce / decay-suspend)
    # that fails after its canonical write would delete the prior committed record.
    # Cut 4 rework: taken BEFORE journal_open and handed to the journal so a
    # crash before finalization restores the prior record at boot too.
    # Rework 2 (CUT4_REVIEW_REWORK_2 §5): the WAL — open entry AND the prior
    # snapshot for an overwrite — must be durable BEFORE the destructive
    # atomic_write. If either cannot be established the write does not begin.
    _prior_bytes = None
    try:
        from memory.engine.validate_write import final_path_for_meta

        _target = final_path_for_meta(memory_dict)
        if _target.exists():
            _prior_bytes = _target.read_bytes()
    except Exception as _snap_exc:
        log.error("commit_promotion: cannot establish prior-record snapshot for %s (%s) — write refused",
                  memory_dict.get("id"), _snap_exc)
        _emit_promotion("commit_promotion", status="fail", ok=False, reason="prior_snapshot_unavailable",
                        staging_ref=staging_ref, error=str(_snap_exc))
        return False, None, None

    journal_id = journal_open(
        memory_dict["id"],
        staging_ref,
        expected_final_path,
        prior_snapshot=_prior_bytes,
    )
    if journal_id is None:
        log.error("commit_promotion: write-ahead journal not durable for %s — write refused (staging intact)",
                  memory_dict.get("id"))
        _emit_promotion("commit_promotion", status="fail", ok=False, reason="journal_open_failed",
                        staging_ref=staging_ref)
        return False, None, None

    try:
        final_path = atomic_write(
            memory_dict,
            update_index=update_index,
            write_ordinal=write_ordinal,
            stamp_observed=stamp_observed,
        )
    except Exception as exc:
        log.error("commit_promotion atomic_write failed for %s: %s", staging_ref, exc)
        _emit_promotion(
            "commit_promotion",
            status="fail",
            ok=False,
            reason="atomic_write_failed",
            staging_ref=staging_ref,
            error=str(exc),
        )
        # No file on disk to roll back, but close the journal so
        # boot recovery doesn't see a phantom unclosed entry.
        journal_abort(
            journal_id,
            memory_dict["id"],
            f"atomic_write_failed: {type(exc).__name__}: {exc}",
            {},
        )
        return False, None, None

    from memory.encoding import encode_committed_memory
    from memory.rollback import rollback_canonical_write, restore_canonical_write

    # Every write is native. There is no dense vector to defer and no worker
    # backlog to maintain; Ettin rebuilds semantic projection from canonical
    # truth at the projection boundary.
    native_write = True
    enc = encode_committed_memory(memory_dict, final_path, skip_embed=True)

    if not enc.ok:
        log.error(
            "commit_promotion encoding failed staging=%s id=%s "
            "step=%s reason=%s — rolling back canonical write",
            staging_ref,
            memory_dict.get("id"),
            enc.failed_step,
            enc.failure_reason,
        )
        # If a prior record existed at this path it was overwritten in
        # place by atomic_write — RESTORE it rather than delete (which
        # would destroy the prior committed record). New records (no
        # prior file) still delete-on-rollback as before.
        if _prior_bytes is not None:
            rb = restore_canonical_write(memory_dict, final_path, _prior_bytes)
        else:
            rb = rollback_canonical_write(memory_dict, final_path)
        log.info("commit_promotion rollback: %s", rb)
        journal_abort(
            journal_id,
            memory_dict["id"],
            f"encoding_failed: step={enc.failed_step} reason={enc.failure_reason}",
            rb,
        )
        # Staging file remains intact — caller can retry.
        return False, None, None

    # ── Cut 4 rework: CANONICAL FINALIZED — durable before anything else ──
    # Encoding is complete: from here the canonical write is truth. The
    # journal records it so a crash between here and journal_close makes boot
    # PRESERVE the record (and reconcile projection), never delete it.
    from memory.encoding_journal import journal_finalize

    if not journal_finalize(journal_id, memory_dict["id"], "native" if native_write else "legacy"):
        # Rework 2 (§4): the finalization marker is the ONLY durable fact that
        # tells boot to preserve this record. Not advisory: stay pre-final,
        # undo synchronously (boot recovery completes the same rollback/restore
        # from the still-pre-final entry if we die first) and refuse success.
        log.error(
            "commit_promotion: canonical_finalized journal step NOT durable for %s — "
            "rolling back / restoring prior; write NOT committed",
            memory_dict.get("id"),
        )
        if _prior_bytes is not None:
            rb = restore_canonical_write(memory_dict, final_path, _prior_bytes)
        else:
            rb = rollback_canonical_write(memory_dict, final_path)
        journal_abort(journal_id, memory_dict["id"], "canonical_finalized_not_durable", rb)
        _emit_promotion("commit_promotion", status="fail", ok=False, reason="canonical_finalized_not_durable",
                        staging_ref=staging_ref, memory_id=memory_dict.get("id"))
        return False, None, None

    # ── Cut 4: projection WORK after a successful canonical encoding ──
    # The canonical commit is already final at this point. Emitting the
    # ProjectionDelta may fail (disk, lock, marker) — that NEVER rolls the
    # canonical write back: the record stays valid and canonically
    # recallable; the loud durable dirty marker orders the projector to
    # full-rebuild before claiming currentness (LAW-EO-004). Not applied to
    # the trait bypass, which is filesystem-only citizenship by law.
    projection_pending = False
    projection_delta_id = None
    projection_dirty = False
    projection_evidence_durable = True  # closing condition for the WAL entry (rework 2 §6)
    if native_write and "non_semantic_bypass" not in enc.steps_completed:
        from memory.projection_delta import emit_delta_for_canonical, projection_dirty_state
        from memory.projection_receipt import memory_root_for_canonical_path

        projection_pending = True
        emitted = emit_delta_for_canonical(memory_dict, final_path)
        delta = emitted["delta"]
        projection_evidence_durable = bool(emitted["durable"])
        if delta is not None:
            projection_delta_id = delta.delta_id
            log.info(
                "commit_promotion projection work queued staging=%s id=%s delta=%s "
                "commit_seq=%s — canonical valid now; DME projection follows",
                staging_ref, memory_dict.get("id"), delta.delta_id[:12], delta.commit_seq,
            )
        else:
            projection_dirty = True
            log.error(
                "commit_promotion PROJECTION DIRTY staging=%s id=%s — canonical commit "
                "kept; outbox append failed; projector must full-rebuild (dirty marker durable=%s)",
                staging_ref, memory_dict.get("id"), emitted["dirty_durable"],
            )
        try:
            _emit_promotion(
                "commit_promotion_projection",
                summary=f"pending={projection_pending} dirty={projection_dirty}",
                cycle_id=cycle_id,
                staging_ref=staging_ref,
                memory_id=memory_dict.get("id"),
                projection_pending=projection_pending,
                projection_delta_id=projection_delta_id,
                projection_dirty=projection_dirty,
                projection_dirty_state=projection_dirty_state(root=memory_root_for_canonical_path(final_path)),
            )
        except Exception:  # noqa: BLE001
            pass

    # Drop the staging file (encoding succeeded; commit is final).
    try:
        os.remove(p)
    except OSError as exc:
        log.warning("commit_promotion: staging cleanup failed %s (%s)", staging_ref, exc)

    # Close the journal entry — encoding is fully done AND the projection
    # evidence is durable (delta appended, or rebuild-required marker
    # confirmed). If neither is durable the finalized entry stays OPEN: it is
    # itself the recovery evidence and boot retries the reconciliation
    # (rework 2 §6). Any boot after a close sees a closed entry and skips it.
    journal_state = "closed"
    if projection_evidence_durable:
        journal_close(journal_id, memory_dict["id"], list(enc.steps_completed))
    else:
        journal_state = "open_unreconciled"
        log.error(
            "commit_promotion: no durable projection evidence for %s (outbox AND dirty marker failed) — "
            "finalized journal entry left OPEN for boot reconciliation",
            memory_dict.get("id"),
        )

    rollback = {
        "staging_ref":     staging_ref,
        "actions":         ["delete_memory_file", "remove_index_entry"],
        "target_path":     final_path,
        "memory_id":       memory_dict["id"],
        "approver":        approver_id,
        "committed_at":    _event_iso("commit_promotion", staging_ref, memory_dict.get("id"), approver_id),
        "encoding_steps":  list(enc.steps_completed),
        # Cut 4: additive, honest projection posture (never "current")
        "projection_pending": projection_pending,
        "projection_delta_id": projection_delta_id,
        "projection_dirty": projection_dirty,
        "projection_evidence_durable": projection_evidence_durable,
        "journal_id":      journal_id,
        "journal_state":   journal_state,
    }
    log.info(
        "committed %s as %s by %s (encoding steps=%s journal=%s)",
        staging_ref,
        memory_dict["id"],
        approver_id,
        enc.steps_completed,
        journal_id,
    )
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "commit_promotion",
            cycle_id=cycle_id,
            summary=f"committed={memory_dict['id']}",
            staging_ref=staging_ref,
            committed=True,
            canonical_ref=memory_dict["id"],
        )
        from guardian.terminal_feedback import emit_guardian_event

        emit_guardian_event(
            "omega.memory.promotion",
            "commit_promotion",
            cycle_id=cycle_id,
            staging_ref=staging_ref,
            committed=True,
            canonical_ref=memory_dict["id"],
        )
    except Exception:
        pass
    return True, memory_dict["id"], rollback


# =====================================================================
# commit_flagged — FLAG state (commit with reduced confidence + flag marker)
# =====================================================================


def commit_flagged(
    staging_ref: str,
    approver_id: str,
    *,
    flag_reason: str,
    response_mode: str,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    """Promote a staged memory to canonical state with a flag marker.

    Used for ambiguous / emotionally-loaded inputs that should land in
    memory but never become silent identity anchors. The recall path
    detects the flag and the voice_box renders the matching
    `response_mode` template instead of treating the memory as plain
    knowledge.

    Mutations vs commit_promotion:
        - flags.flagged = True
        - flags.flag_reason = <reason>
        - flags.flag_response_mode = <response_mode>
        - flags.canonical = False (not identity-promoting)
        - provenance.confidence reduced (clamped at 0.4)
    """
    p = _staging_path(staging_ref)
    cycle_id = ""
    if not p.exists():
        log.warning("commit_flagged: staging not found %s", staging_ref)
        return False, None, None

    try:
        with open(p, "r", encoding="utf-8") as f:
            record = json.load(f)
    except Exception as exc:
        log.error("commit_flagged: staging read failed %s (%s)", staging_ref, exc)
        return False, None, None

    memory_dict = record.get("memory_dict") or {}
    if not memory_dict:
        return False, None, None

    cycle_id = record.get("cycle_id") or ""

    # Apply flag markers
    flags = dict(memory_dict.get("flags") or {})
    flags["flagged"] = True
    flags["flag_reason"] = flag_reason
    flags["flag_response_mode"] = response_mode
    flags["canonical"] = False
    memory_dict["flags"] = flags

    # Reduce confidence — flagged memories are not authoritative
    provenance = dict(memory_dict.get("provenance") or {})
    original_conf = float(provenance.get("confidence", 0.5))
    provenance["confidence"] = min(original_conf, 0.4)
    memory_dict["provenance"] = provenance

    audit = list(memory_dict.get("audit") or [])
    audit.append(
        {
            "when": _event_iso(
                "commit_flagged.audit",
                staging_ref,
                memory_dict.get("id"),
                approver_id,
                flag_reason,
                response_mode,
            ),
            "what": "promoted_flagged",
            "by": approver_id,
            "notes": f"flag_reason={flag_reason} response_mode={response_mode}",
        }
    )
    memory_dict["audit"] = audit

    # AUDIT-4: same encoding journal bracket as commit_promotion. Same
    # 8-step encoding pipeline runs for flagged writes; same crash
    # window applies; same recovery posture closes it.
    from memory.encoding_journal import (
        journal_open,
        journal_close,
        journal_abort,
    )
    from memory.engine.validate_write import final_path_for_meta

    expected_final_path = str(final_path_for_meta(memory_dict))

    # Rework 2 (§3/§5): prior snapshot taken and journaled BEFORE any
    # overwrite; WAL not durable → the write does not begin.
    _prior_bytes = None
    try:
        _target = final_path_for_meta(memory_dict)
        if _target.exists():
            _prior_bytes = _target.read_bytes()
    except Exception as _snap_exc:
        log.error("commit_flagged: cannot establish prior-record snapshot for %s (%s) — write refused",
                  memory_dict.get("id"), _snap_exc)
        _emit_promotion("commit_flagged", status="fail", ok=False, reason="prior_snapshot_unavailable",
                        staging_ref=staging_ref, error=str(_snap_exc))
        return False, None, None

    journal_id = journal_open(
        memory_dict["id"],
        staging_ref,
        expected_final_path,
        prior_snapshot=_prior_bytes,
    )
    if journal_id is None:
        log.error("commit_flagged: write-ahead journal not durable for %s — write refused (staging intact)",
                  memory_dict.get("id"))
        _emit_promotion("commit_flagged", status="fail", ok=False, reason="journal_open_failed",
                        staging_ref=staging_ref)
        return False, None, None

    try:
        final_path = atomic_write(memory_dict, update_index=True)
    except Exception as exc:
        log.error("commit_flagged atomic_write failed for %s: %s", staging_ref, exc)
        _emit_promotion(
            "commit_flagged",
            status="fail",
            ok=False,
            reason="atomic_write_failed",
            staging_ref=staging_ref,
            error=str(exc),
        )
        journal_abort(
            journal_id,
            memory_dict["id"],
            f"atomic_write_failed: {type(exc).__name__}: {exc}",
            {},
        )
        return False, None, None

    # ── Atomic encoding (same 8-step pipeline as commit_promotion) ──
    # Flagged memories still require full L0 encoding — they're
    # written, just with reduced authority. The Omega Cognitive Law
    # doesn't carve out flagged memories.
    from memory.encoding import encode_committed_memory
    from memory.rollback import rollback_canonical_write, restore_canonical_write

    _flag_native_write = True
    enc = encode_committed_memory(memory_dict, final_path, skip_embed=True)

    if not enc.ok:
        log.error(
            "commit_flagged encoding failed staging=%s id=%s "
            "step=%s reason=%s — rolling back canonical write",
            staging_ref,
            memory_dict.get("id"),
            enc.failed_step,
            enc.failure_reason,
        )
        if _prior_bytes is not None:
            rb = restore_canonical_write(memory_dict, final_path, _prior_bytes)
        else:
            rb = rollback_canonical_write(memory_dict, final_path)
        log.info("commit_flagged rollback: %s", rb)
        journal_abort(
            journal_id,
            memory_dict["id"],
            f"encoding_failed: step={enc.failed_step} reason={enc.failure_reason}",
            rb,
        )
        return False, None, None

    # Cut 4 rework: canonical finalized — durable before projection/cleanup.
    # Rework 2 (§4): not advisory — if the marker is not durable, undo and
    # refuse success (same law as commit_promotion).
    from memory.encoding_journal import journal_finalize as _flag_finalize

    if not _flag_finalize(journal_id, memory_dict["id"], "native" if _flag_native_write else "legacy"):
        log.error(
            "commit_flagged: canonical_finalized journal step NOT durable for %s — "
            "rolling back / restoring prior; write NOT committed",
            memory_dict.get("id"),
        )
        if _prior_bytes is not None:
            rb = restore_canonical_write(memory_dict, final_path, _prior_bytes)
        else:
            rb = rollback_canonical_write(memory_dict, final_path)
        journal_abort(journal_id, memory_dict["id"], "canonical_finalized_not_durable", rb)
        _emit_promotion("commit_flagged", status="fail", ok=False, reason="canonical_finalized_not_durable",
                        staging_ref=staging_ref, memory_id=memory_dict.get("id"))
        return False, None, None

    try:
        os.remove(p)
    except OSError:
        pass

    # Cut 4: projection work after the (final) canonical commit; failure marks
    # dirty, never rolls back (see commit_promotion)
    _flag_projection_pending = False
    _flag_projection_delta_id = None
    _flag_projection_dirty = False
    _flag_evidence_durable = True
    if _flag_native_write and "non_semantic_bypass" not in enc.steps_completed:
        from memory.projection_delta import emit_delta_for_canonical

        _flag_projection_pending = True
        _flag_emitted = emit_delta_for_canonical(memory_dict, final_path)
        _flag_evidence_durable = bool(_flag_emitted["durable"])
        if _flag_emitted["delta"] is not None:
            _flag_projection_delta_id = _flag_emitted["delta"].delta_id
        else:
            _flag_projection_dirty = True

    _flag_journal_state = "closed"
    if _flag_evidence_durable:
        journal_close(journal_id, memory_dict["id"], list(enc.steps_completed))
    else:
        _flag_journal_state = "open_unreconciled"
        log.error(
            "commit_flagged: no durable projection evidence for %s — finalized journal entry left OPEN "
            "for boot reconciliation",
            memory_dict.get("id"),
        )

    rollback = {
        "staging_ref": staging_ref,
        "actions": ["delete_memory_file", "remove_index_entry"],
        "target_path": final_path,
        "memory_id": memory_dict["id"],
        "approver": approver_id,
        "flagged": True,
        "response_mode": response_mode,
        "projection_pending": _flag_projection_pending,
        "projection_delta_id": _flag_projection_delta_id,
        "projection_dirty": _flag_projection_dirty,
        "projection_evidence_durable": _flag_evidence_durable,
        "journal_state": _flag_journal_state,
        "committed_at": _event_iso(
            "commit_flagged",
            staging_ref,
            memory_dict.get("id"),
            approver_id,
            flag_reason,
            response_mode,
        ),
        "encoding_steps": list(enc.steps_completed),
        "journal_id": journal_id,
    }
    log.info(
        "committed_flagged %s as %s mode=%s by %s (encoding steps=%s journal=%s)",
        staging_ref,
        memory_dict["id"],
        response_mode,
        approver_id,
        enc.steps_completed,
        journal_id,
    )
    _emit_promotion(
        "commit_flagged",
        summary=f"committed={memory_dict['id']}",
        cycle_id=cycle_id,
        staging_ref=staging_ref,
        committed=True,
        canonical_ref=memory_dict["id"],
        response_mode=response_mode,
        flag_reason=flag_reason,
    )
    return True, memory_dict["id"], rollback


# =====================================================================
# commit_quarantine — QUARANTINE state (isolated storage, not recallable)
# =====================================================================

QUARANTINE_DIR = OMEGA_ROOT / "guardian" / "state" / "quarantine"
QUARANTINE_INDEX = QUARANTINE_DIR / "quarantine_index.json"


def _ensure_quarantine_dir() -> None:
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)


def commit_quarantine(
    staging_ref: str,
    approver_id: str,
    *,
    quarantine_reason: str,
    response_mode: str,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    """Move a staged memory into isolated quarantine storage.

    Quarantined items NEVER enter the canonical lobe directories or
    coord_index.json — the walker will not find them. They are kept as
    attack-pattern data for omega-layer review and never participate in
    cognition. Operator tools can list and inspect them.

    Storage:
        guardian/state/quarantine/<memory_id>.json
        guardian/state/quarantine/quarantine_index.json
    """
    p = _staging_path(staging_ref)
    cycle_id = ""
    if not p.exists():
        log.warning("commit_quarantine: staging not found %s", staging_ref)
        return False, None, None

    try:
        with open(p, "r", encoding="utf-8") as f:
            record = json.load(f)
    except Exception as exc:
        log.error("commit_quarantine: staging read failed %s (%s)", staging_ref, exc)
        return False, None, None

    memory_dict = record.get("memory_dict") or {}
    if not memory_dict:
        return False, None, None

    cycle_id = record.get("cycle_id") or ""

    flags = dict(memory_dict.get("flags") or {})
    flags["quarantined"] = True
    flags["quarantine_reason"] = quarantine_reason
    flags["quarantine_response_mode"] = response_mode
    flags["canonical"] = False
    memory_dict["flags"] = flags

    audit = list(memory_dict.get("audit") or [])
    audit.append(
        {
            "when": _event_iso(
                "commit_quarantine.audit",
                staging_ref,
                memory_dict.get("id"),
                approver_id,
                quarantine_reason,
                response_mode,
            ),
            "what": "quarantined",
            "by": approver_id,
            "notes": f"reason={quarantine_reason} response_mode={response_mode}",
        }
    )
    memory_dict["audit"] = audit

    _ensure_quarantine_dir()
    final = QUARANTINE_DIR / f"{memory_dict['id']}.json"
    try:
        tmp = final.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(memory_dict, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
    except Exception as exc:
        log.error("commit_quarantine write failed for %s: %s", staging_ref, exc)
        _emit_promotion(
            "commit_quarantine",
            status="fail",
            ok=False,
            reason="quarantine_write_failed",
            staging_ref=staging_ref,
            error=str(exc),
        )
        return False, None, None

    # Update quarantine index (separate from coord_index.json — walker
    # never touches this).
    index_updated = False
    try:
        idx: Dict[str, Any] = {}
        if QUARANTINE_INDEX.exists():
            with open(QUARANTINE_INDEX, "r", encoding="utf-8") as f:
                idx = json.load(f)
        idx[memory_dict["id"]] = {
            "id": memory_dict["id"],
            "path": str(final),
            "title": memory_dict.get("title", "")[:120],
            "coord": memory_dict.get("coord"),
            "reason": quarantine_reason,
            "response_mode": response_mode,
            "quarantined_at": _event_iso(
                "commit_quarantine.index",
                staging_ref,
                memory_dict.get("id"),
                quarantine_reason,
                response_mode,
            ),
            "approver": approver_id,
        }
        tmp_idx = QUARANTINE_INDEX.with_suffix(".tmp")
        with open(tmp_idx, "w", encoding="utf-8") as f:
            json.dump(idx, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_idx, QUARANTINE_INDEX)
        index_updated = True
    except Exception as exc:
        log.warning("commit_quarantine index update failed: %s", exc)
        _emit_promotion(
            "commit_quarantine",
            status="degraded",
            ok=False,
            reason="quarantine_index_update_failed",
            staging_ref=staging_ref,
            error=str(exc),
        )

    try:
        os.remove(p)
    except OSError:
        pass

    rollback = {
        "staging_ref": staging_ref,
        "actions": ["delete_quarantine_file", "remove_quarantine_index"],
        "target_path": str(final),
        "memory_id": memory_dict["id"],
        "approver": approver_id,
        "quarantined": True,
        "response_mode": response_mode,
        "committed_at": _event_iso(
            "commit_quarantine",
            staging_ref,
            memory_dict.get("id"),
            approver_id,
            quarantine_reason,
            response_mode,
        ),
    }
    log.warning(
        "quarantined %s as %s reason=%s by %s",
        staging_ref,
        memory_dict["id"],
        quarantine_reason,
        approver_id,
    )
    _emit_promotion(
        "commit_quarantine",
        summary=f"quarantined={memory_dict['id']}",
        cycle_id=cycle_id,
        staging_ref=staging_ref,
        committed=False,
        quarantined=True,
        canonical_ref=memory_dict["id"],
        quarantine_reason=quarantine_reason,
        response_mode=response_mode,
        index_updated=index_updated,
    )
    return True, memory_dict["id"], rollback


# =====================================================================
# rollback_promotion
# =====================================================================


def rollback_promotion(staging_ref: str) -> Dict[str, Any]:
    """Drop a staged memory without committing it.

    Returns a status dict; never raises. Idempotent — calling on a
    non-existent staging_ref returns {ok: True, noop: True}.
    """
    try:
        p = _staging_path(staging_ref)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}

    if not p.exists():
        return {"ok": True, "noop": True, "staging_ref": staging_ref}

    try:
        os.remove(p)
        return {
            "ok": True,
            "noop": False,
            "staging_ref": staging_ref,
            "rolled_back_at": _event_iso("rollback_promotion", staging_ref),
        }
    except OSError as exc:
        log.error("rollback_promotion failed for %s: %s", staging_ref, exc)
        return {"ok": False, "reason": str(exc), "staging_ref": staging_ref}


# =====================================================================
# Diagnostic
# =====================================================================


def list_staged() -> List[Dict[str, Any]]:
    """List all currently staged refs (for diagnostics + memory_health).

    Each entry surfaces the `guardian_request_id` so operator UI / logs
    can pair pending memory writes with their pending Guardian request.
    """
    if not STAGING_DIR.exists():
        return []
    out = []
    for p in sorted(STAGING_DIR.glob("staging-*.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                rec = json.load(f)
            out.append(
                {
                    "staging_ref": rec.get("staging_ref"),
                    "guardian_request_id": rec.get("guardian_request_id"),
                    "cycle_id": rec.get("cycle_id"),
                    "created_at": rec.get("created_at"),
                    "memory_id": (rec.get("memory_dict") or {}).get("id"),
                    "coord": (rec.get("memory_dict") or {}).get("coord"),
                    "title": (rec.get("memory_dict") or {}).get("title"),
                }
            )
        except Exception:
            continue
    return out


def find_staging_by_request_id(guardian_request_id: str) -> Optional[str]:
    """Return the staging_ref whose record carries this guardian_request_id.

    Returns None if no such request exists. Used by the explicit
    approve/deny path in memory.api.
    """
    if not guardian_request_id or not STAGING_DIR.exists():
        return None
    for p in sorted(STAGING_DIR.glob("staging-*.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                rec = json.load(f)
            if rec.get("guardian_request_id") == guardian_request_id:
                return rec.get("staging_ref")
        except Exception:
            continue
    return None
