"""Slice 1 B2 — pressure evaluation → EpisodeIgnition.

Gathers authoritative operator/Omega successors and opens when the **dominant raw-pressure**
contributor reaches the configured ceiling with a clear margin over the
runner-up. Neuromodulation is captured separately as advisory biological
context for downstream deliberation; it cannot change contributor weights,
the ignition boundary, or the winner. Summed partial pressure does not open
episodes; lexicographic ties are not used. Building-pressure accumulation
remains telemetry-only in pressure_field.
"""

from __future__ import annotations

import logging
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pressure_loop.contributors import Contributor, EpisodeIgnition, make_contributor
from core.shared_state import SharedState
from core.shared_state_keys import LAST_OMEGA_TRAJECTORY
from pressure_loop.policy import (
    pressure_neuromod_weight_modulation,
    pressure_winner_ceiling,
    pressure_winner_margin,
)

log = logging.getLogger("omega.core.pressure")

PRESSURE_CEILING_EPS = 1e-6

# Map pressure_field contributor id → canonical contributor type.
_FIELD_ID_TO_TYPE: Dict[str, str] = {
    "curiosity_gap": "curiosity_gap",
    "operator_intent": "operator_command",
    "operator_message": "operator_query",  # refined via ingress metadata
    "epistemic_emptiness": "epistemic_emptiness",
    "idle_probe_aggregate": "idle_probe_aggregate",
    "learning_debt": "learning_debt",
    "execution_gap": "execution_gap",
    "operator_priority": "operator_priority",
}

# (type, domain) -> the live signal. A dict, not a list: one signal per
# identity, replaced by a newer emission, retired by release_staged().
_RESEED_STAGING: Dict[Tuple[str, str], Contributor] = {}
_RESEED_SUPPRESS: Dict[str, float] = {}
# #13: the reseed/suppress stores are read-modify-written from BOTH the
# pressure-tick thread (boot main) AND the HUD request thread (operator chat ->
# evaluate_operator_ignition -> gather_contributors). Guard every access so a
# stage on one thread cannot interleave with a drain on the other (a lost
# staging entry silently drops a DeepSleep-prepared contributor). Mirrors the
# lock discipline operator_queue.py already applies to its own seam. RLock so a
# guarded helper can call another without self-deadlock.
_RESEED_LOCK = threading.RLock()


def reset_mind_primacy() -> None:
    """No-op retained for import-surface stability (boot.py + conftest call it).

    Mind-primacy's bootstrap is OPERATOR INTENT, NOT a deterministic latch:
    ``gather_contributors`` never reads the deterministic pressure-field floor, cold start
    included — the system stays QUIET until the operator onboards/messages (the operator
    path in evaluate() ignites). There is therefore no per-process latch state
    to reset (the earlier cold-start/re-arm latch was removed when the bootstrap moved
    to operator-intent per the operator's agentic-loop spec)."""
    return None


def _proactive_lifecycle_contributor() -> Optional[Contributor]:
    """Project one durable dialogue application onto the existing proactive lane."""
    from pressure_loop.proactive_lifecycle import claim_dialogue_dispatch

    dispatch = claim_dialogue_dispatch()
    if dispatch is None:
        return None
    if dispatch.get("kind") == "PARKED":
        try:
            from pressure_loop.proactive_chat_messages import append_update

            append_update(
                text=str(dispatch["message"]),
                episode_id=f"parked:{dispatch['dispatch_id']}",
                opened_by="proactive_update",
                kind="NOTICE",
                payload={
                    "thread_id": str(dispatch["thread_id"]),
                    "dispatch_id": str(dispatch["dispatch_id"]),
                    "attempts": int(dispatch.get("attempts") or 0),
                },
            )
        except Exception:
            log.warning("failed to publish proactive dispatch parking notice", exc_info=True)
        return None
    return make_contributor(
        "omega",
        "proactive_update",
        0.85,
        {
            "lane": "chat",
            "message": str(dispatch["message"]),
            "presentation_kind": "PRESENT_RESULT",
            "proactive_thread_id": str(dispatch["thread_id"]),
            "proactive_policy_id": str(dispatch["policy_id"]),
            "proactive_application_id": str(dispatch["dispatch_id"]),
            "operator_policy": dict(dispatch["operator_policy"]),
        },
    )


# ONLY operator input can queue. The pressure loop is HALTED while an episode
# is live (and through DeepSleep); it resets and resumes afterward. So
# autonomous drives (curiosity_gap, learning_debt, epistemic_emptiness,
# idle_probe_aggregate, evidence_terminal, novel_observation, tool_failure)
# are never read mid-episode — the loop re-derives them from state each time it
# runs between episodes, so they NEVER queue. The only stimulus that can hit
# the system during the halt is an async operator message; a trigger-happy
# operator firing back-to-back messages must lose none, so operator inputs
# hold in the durable FIFO queue box (pressure_loop/operator_queue.py — one
# entry per distinct message, ORDER OF INPUT) and apply ONE per pressure
# cycle in evaluate(), so every input gets its own episode (operator
# directive 2026-06-12).
_OPERATOR_INPUT_TYPES = frozenset({"operator_command", "operator_query", "operator_response"})


def pending_contributors() -> Tuple[Contributor, ...]:
    from pressure_loop.cognition_queue import peek_inputs

    pending = list(peek_inputs())
    rollback = _rollback_event_contributor()
    if rollback is not None:
        pending.append(rollback)
    return tuple(pending)


def clear_pending_contributors() -> None:
    from pressure_loop.cognition_queue import clear_inputs

    clear_inputs()
    try:
        from pressure_loop.system_event_queue import clear_rollback_events

        clear_rollback_events()
    except Exception:
        pass


def _rollback_event_contributor() -> Contributor | None:
    """Expose session rollback facts as one cognition-visible system contributor."""
    try:
        from pressure_loop.system_event_queue import peek_rollback_events

        events = list(peek_rollback_events())
    except Exception:
        return None
    if not events:
        return None
    messages = [str(e.get("message") or "").strip() for e in events]
    message = "; ".join(m for m in messages if m)[:800]
    if not message:
        message = f"{len(events)} workspace rollback event(s) pending"
    return make_contributor(
        "system",
        "dev_rollback",
        1.0,
        {
            "lane": "chat",
            "field_id": "dev_rollback",
            "message": message,
            "directive_intent": "IDLE",
            "rollback_events": events,
        },
    )


def _staging_key(contributor: Contributor) -> Tuple[str, str]:
    """(type, domain) — the identity a staged pressure is replaced by."""
    meta = contributor.metadata if isinstance(contributor.metadata, dict) else {}
    if contributor.type == "omega_successor":
        return ("omega_successor", str(meta["successor_id"]))
    return (
        str(contributor.type or "").strip(),
        str(meta.get("domain") or "").strip().lower(),
    )


def _successor_path():
    from core.paths import GUARDIAN_STATE_ROOT
    return GUARDIAN_STATE_ROOT / "omega_successors.json"


_TRANSACTION_FIELDS = frozenset({
    "fold_id", "episode_id", "thread_id", "decision_digest", "receipt_ids", "mail_cutoff_seq",
    "bound_mail_ids", "outgoing", "continuation_key", "staging", "published",
    "pending_canonical", "successor", "opened_at", "closed", "origin_seq", "withheld",
    "minted_forgotten", "deliveries"})
_TRANSACTION_STAGING = frozenset({"pending", "staged", "none"})
# Lane-entry delivery of one outgoing effect (T9): the prompt was recorded (``started``), the
# lane was let past its first inference (``executing``), its run returned (``performed``), or
# the delivery was given up before anything ran (``abandoned``).
_DELIVERY_STATES = frozenset({"started", "executing", "performed", "abandoned"})
_DELIVERY_FIELDS = frozenset({"effect_id", "mail_id", "episode_id", "process_epoch", "state",
                              "prompt_digest", "prompt_chars", "at", "reserved_until"})
_MAX_CLOSED_TRANSACTIONS = 512
_MAX_TERMINAL_CONTINUATIONS = 512
_TERMINAL_CONTINUATION_STATES = frozenset({"completed", "cancelled"})
_LEDGER_KEYS = frozenset({
    "pending", "admitted", "transactions", "transaction_horizon", "stopped",
    "continuations", "continuation_retired",
})
_CONTINUATION_EPOCH = uuid.uuid4().hex
_CLAIM_STATES = frozenset({"waiting", "claimed", "running", "completed", "released", "cancelled"})
_CLAIM_FIELDS = frozenset({"successor_id", "state", "process_epoch", "reason", "logical_key",
                          "expected_work_revision", "job_id", "origin_cycle_id"})


def _read_successors():
    path = _successor_path()
    if not path.exists():
        return {"pending": {}, "admitted": {}, "transactions": {}, "transaction_horizon": 0,
                "stopped": {}, "continuations": {}, "continuation_retired": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if type(data) is dict:
        # A ledger written before cognitive transactions existed simply has none (T8), and one
        # written before the stop doctrine records no stopped work (T12).
        data.setdefault("transactions", {})
        data.setdefault("transaction_horizon", 0)
        data.setdefault("stopped", {})
        data.setdefault("continuations", {})
        data.setdefault("continuation_retired", {})
    if (type(data) is not dict
            or set(data) != _LEDGER_KEYS
            or any(type(data[key]) is not dict
                   for key in ("pending", "admitted", "transactions", "stopped",
                               "continuations", "continuation_retired"))
            or type(data["transaction_horizon"]) is not int or data["transaction_horizon"] < 0
            or any(type(row) is not dict or set(row) != {"at", "reason"}
                   or any(type(value) is not str for value in row.values())
                   for row in data["stopped"].values())
            or any(type(key) is not str or len(key) != 64
                   or value not in _TERMINAL_CONTINUATION_STATES
                   for key, value in data["continuation_retired"].items())):
        raise ValueError("invalid durable Omega successor ledger")
    for key, claim in data["continuations"].items():
        if (type(key) is not str or len(key) != 64 or type(claim) is not dict
                or set(claim) != _CLAIM_FIELDS or claim["state"] not in _CLAIM_STATES
                or any(type(claim[field]) is not str for field in _CLAIM_FIELDS - {"expected_work_revision"})
                or (claim["expected_work_revision"] is not None
                    and type(claim["expected_work_revision"]) is not int)):
            raise ValueError("invalid durable continuation claim")
    for fold_id, record in data["transactions"].items():
        if type(record) is dict:
            # A record written before lane-entry delivery existed has delivered nothing (T9).
            record.setdefault("deliveries", {})
        if (type(record) is not dict or set(record) != _TRANSACTION_FIELDS
                or record["fold_id"] != fold_id or not fold_id
                or record["staging"] not in _TRANSACTION_STAGING
                or type(record["published"]) is not bool or type(record["closed"]) is not bool
                or (record["staging"] == "none") != (record["successor"] is None)
                or type(record["origin_seq"]) is not int or record["origin_seq"] < 0
                or type(record["withheld"]) is not list
                or type(record["minted_forgotten"]) is not bool
                or type(record["deliveries"]) is not dict
                or any(type(row) is not dict or set(row) != _DELIVERY_FIELDS
                       or row["effect_id"] != effect_id or row["state"] not in _DELIVERY_STATES
                       for effect_id, row in record["deliveries"].items())
                or any(type(record[key]) is not list for key in
                       ("receipt_ids", "bound_mail_ids", "outgoing", "pending_canonical"))):
            raise ValueError(f"invalid durable cognitive transaction {fold_id!r}")
    from pressure_loop.omega_successor import validate_successor
    for identity, row in data["pending"].items():
        if set(row) != {"source", "type", "weight", "metadata"} or row["source"] != "omega" or row["type"] != "omega_successor" or row["weight"] != 1.0:
            raise ValueError("invalid durable Omega successor")
        validate_successor(row["metadata"])
        if identity != row["metadata"]["successor_id"] or identity in data["admitted"]:
            raise ValueError("conflicting durable Omega successor identity")
    if any(type(value) is not str or len(value) != 64 for value in data["admitted"].values()):
        raise ValueError("invalid successor admission receipt")
    return data


def _compact_terminal_continuations(data):
    """Keep live claims; retire oldest completed/cancelled past the cap.

    Retired hashes still refuse reclaim, so compaction cannot authorize an old successor.
    """
    data.setdefault("continuation_retired", {})
    terminal = [
        key for key, claim in data["continuations"].items()
        if claim["state"] in _TERMINAL_CONTINUATION_STATES
    ]
    overflow = len(terminal) - _MAX_TERMINAL_CONTINUATIONS
    if overflow <= 0:
        return
    for key in terminal[:overflow]:
        data["continuation_retired"][key] = data["continuations"][key]["state"]
        del data["continuations"][key]


def _write_successors(data):
    _compact_terminal_continuations(data)
    path = _successor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _successor_row(contributor):
    metadata = dict(contributor.metadata)
    for key in ("arrival_seq", "queue_id", "work_kind", "job_id"):
        metadata.pop(key, None)
    return dict(source=contributor.source, type=contributor.type, weight=contributor.weight, metadata=metadata)


def _successor_digest(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def stage_reseed(contributors: Sequence[Contributor]) -> Tuple[Contributor, ...]:
    """Stage an Omega-emitted pressure signal until an episode ignites on it.

    Keyed by (type, domain) so a newer emission REPLACES the same signal rather
    than queueing a second copy of it. This is the contract omega_levels already
    documents for setpoints — "persist until their exact contributor is
    successfully admitted or a newer fold replaces the same (type, domain) key"
    — and staging now obeys it too, because there is one pressure lifecycle, not
    two (operator 2026-08-15).

    It did not obey it before, and that cost a live stall. Staging was a list
    drained read-and-clear on every gather, so a staged signal got exactly ONE
    tick: if it did not win that tick it was destroyed, unheard. On 2026-08-15
    an emergent pressure staged by a closing fold tied with the standing
    mastery_gap at 0.850 (winner_margin 0.05, gap 0.000 -> ambiguous_winner),
    so NOTHING ignited and the signal was gone by the next gather. Autonomous
    cognition parked for 7m23s.
    """
    accepted = []
    with _RESEED_LOCK:
        for c in contributors:
            if c.type == "omega_successor":
                from pressure_loop.continuation import work_lock

                with work_lock(c.metadata):
                    data = _read_successors()
                    identity = c.metadata["successor_id"]
                    if identity not in data["admitted"]:
                        reason = _claim_continuation_into(data, c.metadata)
                        if reason:
                            from pressure_loop.omega_successor import _decline

                            _decline(c.metadata["parent_episode_id"], reason)
                            continue
                    if _stage_successor_into(data, c) or identity not in data["admitted"]:
                        _write_successors(data)
                    accepted.append(c)
                continue
            if c.weight > 0.001:
                _RESEED_STAGING[_staging_key(c)] = c
                accepted.append(c)
    return tuple(accepted)


def _claim_continuation_into(data, metadata):
    from pressure_loop.continuation import identity, terminal_work_reason, work_revision

    claim = metadata.get("continuation_claim")
    if claim is None:
        return ""
    logical = identity(metadata, 0)["key"]
    successor_id = metadata["successor_id"]
    key = hashlib.sha256(successor_id.encode()).hexdigest()
    retired = data["continuation_retired"].get(key)
    if retired in _TERMINAL_CONTINUATION_STATES:
        return f"the continuation is {retired}"
    existing = _continuation_record(data, metadata)
    if (existing is not None and existing["successor_id"] == successor_id
            and existing["state"] in {"completed", "cancelled"}):
        return f"the continuation is {existing['state']}"
    try:
        terminal = terminal_work_reason(metadata)
        if terminal:
            return terminal
        current_revision = work_revision(metadata)
    except Exception as exc:
        current_revision, unreadable = None, str(exc)
    else:
        unreadable = ""
    expected = claim["expected_work_revision"]
    if expected is None and existing is not None:
        expected = existing["expected_work_revision"]
    if current_revision is not None and expected is not None and current_revision != expected:
        return "the continued work revision changed"
    revision = expected if expected is not None else current_revision
    for other in data["continuations"].values():
        if (other["logical_key"] == logical and other["state"] in {"waiting", "claimed", "running"}
                and other["successor_id"] != successor_id
                and (revision is None or other["expected_work_revision"] is None
                     or other["expected_work_revision"] == revision)):
            return f"logical continuation is owned by {other['successor_id']}"
    if existing and existing["state"] in {"claimed", "running"}:
        return ""
    data["continuations"][hashlib.sha256(successor_id.encode()).hexdigest()] = {
        "successor_id": successor_id, "state": "waiting" if unreadable else "claimed",
        "process_epoch": _CONTINUATION_EPOCH, "reason": unreadable,
        "logical_key": logical, "expected_work_revision": revision,
        "job_id": str(metadata.get("agent_job_id") or ""),
        "origin_cycle_id": str(metadata.get("job_cycle_id") or metadata.get("original_cycle_id") or "")}
    return ""


def _continuation_record(data, metadata):
    successor_id = str(metadata.get("successor_id") or "")
    key = hashlib.sha256(successor_id.encode()).hexdigest()
    live = data["continuations"].get(key)
    if live is not None:
        return live
    retired = (data.get("continuation_retired") or {}).get(key)
    if retired in _TERMINAL_CONTINUATION_STATES:
        return {
            "successor_id": successor_id, "state": retired, "process_epoch": "",
            "reason": "retired", "logical_key": "", "expected_work_revision": None,
            "job_id": str(metadata.get("agent_job_id") or ""),
            "origin_cycle_id": "",
        }
    return None


def _permanent_recovery_refusal(reason):
    return reason == "the continued work revision changed" or reason.startswith(
        "the continued job is ")


def _cancel_unpublished_transaction_into(data, metadata, reason):
    """Retire a transaction whose successor can never reacquire its work claim.

    Recovery keeps unreadable or contended claims retryable. A terminal job or a changed
    revision is different: retrying the successor would authorize stale work, while leaving
    the transaction open strands its mail forever. Marking the claim cancelled lets the normal
    transaction publisher convert lane-entry mail to durable withheld evidence.
    """
    successor_id = str(metadata.get("successor_id") or "")
    for record in data["transactions"].values():
        successor = record.get("successor") or {}
        successor_metadata = successor.get("metadata") or {}
        if record["published"] or successor_metadata.get("successor_id") != successor_id:
            continue
        claim = _continuation_record(data, successor_metadata)
        if claim is None or claim["state"] in {"completed", "cancelled"}:
            continue
        claim.update(state="cancelled", reason=str(reason), process_epoch=_CONTINUATION_EPOCH)
        return True
    return False


def _transition_continuation_into(data, metadata, state, reason=""):
    existing = _continuation_record(data, metadata)
    if (existing is None or existing["successor_id"] != metadata.get("successor_id")
            or existing["state"] not in {"waiting", "claimed", "running"}):
        return False
    existing.update(state=state, reason=str(reason), process_epoch=_CONTINUATION_EPOCH)
    return True


def complete_continuation(metadata):
    with _RESEED_LOCK:
        data = _read_successors()
        existing = _continuation_record(data, metadata)
        if existing is None or existing["state"] != "running":
            return
        if _transition_continuation_into(data, metadata, "completed"):
            _write_successors(data)


def release_continuation(metadata, reason):
    with _RESEED_LOCK:
        data = _read_successors()
        if _transition_continuation_into(data, metadata, "released", reason):
            _write_successors(data)


def cancel_job_continuations(job_id, reason):
    with _RESEED_LOCK:
        data = _read_successors()
        changed = False
        for claim in data["continuations"].values():
            if claim["job_id"] != job_id or claim["state"] not in {"waiting", "claimed", "running"}:
                continue
            claim.update(state="cancelled", reason=reason, process_epoch=_CONTINUATION_EPOCH)
            row = data["pending"].pop(claim["successor_id"], None)
            if row is not None:
                data["admitted"][claim["successor_id"]] = _successor_digest(row)
            changed = True
        if changed:
            _write_successors(data)


def job_has_continuation(job_id):
    with _RESEED_LOCK:
        return any(row["job_id"] == job_id and row["state"] in {"waiting", "claimed", "running"}
                   for row in _read_successors()["continuations"].values())


def settle_retired_jobs():
    """Release orphaned project ownership only after every continuation carrier is gone.

    Admission and publication take the successor lock before the job mutex. No job
    handoff runs until both locks have been released. Undecidable carriers retain the
    project, and the pressure tick retries once their disposition becomes readable.
    """
    from auto_loop.execute import job_control as jobs
    from pressure_loop.held_instruction_handoff import fold_pending

    settled = []
    with _RESEED_LOCK:
        data = _read_successors()
        candidates = {row["job_id"]: row for row in data["continuations"].values()
                      if row["job_id"] and row["state"] in {"released", "cancelled"}}
        for job_id, claim in candidates.items():
            if job_has_continuation(job_id) or fold_pending(job_id):
                continue
            try:
                job = jobs.get_job(job_id)
            except KeyError:
                continue
            if job.status != "running" or job.cycle_id != claim["origin_cycle_id"]:
                continue
            result = jobs.settle_if_idle(
                job_id, expected_owner=job.owner_cycle_id, expected_revision=job.latest_revision,
                status="stopped", summary=claim["reason"] or "no viable continuation remains")
            if result is not None:
                settled.append(result)
    if settled:
        jobs._hand_off_held_mail()
        jobs._wake()
    return tuple(settled)


def continuation_standing(metadata):
    from pressure_loop.continuation import terminal_work_reason, work_lock, work_revision

    if not metadata.get("continuation_claim"):
        return "", ""
    try:
        with _RESEED_LOCK, work_lock(metadata):
            terminal = terminal_work_reason(metadata)
            if terminal:
                return "stopped", terminal
            data = _read_successors()
            current_revision = work_revision(metadata)
            claim = _continuation_record(data, metadata)
            if claim is not None and claim["state"] == "waiting":
                reason = _claim_continuation_into(data, metadata)
                if reason:
                    return "stopped", reason
                _write_successors(data)
                claim = _continuation_record(data, metadata)
            if claim is not None and claim["state"] == "waiting":
                return "unknown", claim["reason"]
            if (claim is None or claim["successor_id"] != metadata["successor_id"]
                    or claim["state"] not in {"claimed", "running"}):
                return "stopped", "the continuation no longer owns its claim"
            if claim["expected_work_revision"] != current_revision:
                return "stopped", "the continued work revision changed"
            return "", ""
    except Exception as exc:
        return "unknown", f"continuation ownership cannot be read: {exc}"


def recover_continuations():
    from pressure_loop.cognition_queue import peek_inputs
    from pressure_loop.continuation import work_lock

    queued = tuple(peek_inputs())
    with _RESEED_LOCK:
        data = _read_successors()
        for claim in data["continuations"].values():
            if claim["process_epoch"] != _CONTINUATION_EPOCH and claim["state"] in {"waiting", "claimed", "running"}:
                claim.update(state="released", reason="owner process ended")
        rows = [row["metadata"] for row in data["pending"].values()]
        rows.extend(record["successor"]["metadata"] for record in data["transactions"].values()
                    if not record["published"] and record["successor"])
        rows.extend(row.metadata for row in queued if row.type == "omega_successor")
        for metadata in rows:
            with work_lock(metadata):
                reason = _claim_continuation_into(data, metadata)
                if reason and _permanent_recovery_refusal(reason):
                    _cancel_unpublished_transaction_into(data, metadata, reason)
        _write_successors(data)


def _stage_successor_into(data, c: Contributor) -> bool:
    """Stage one validated successor into a ledger candidate. True when the ledger changed.

    Replay-idempotent: the identical row is a no-op, a differing one is refused.
    """
    from pressure_loop.omega_successor import validate_successor

    validate_successor(c.metadata)
    if c.source != "omega" or c.weight != 1.0:
        raise ValueError("invalid Omega successor provenance")
    identity = c.metadata["successor_id"]
    row = _successor_row(c)
    digest = _successor_digest(row)
    prior = data["pending"].get(identity)
    admitted = data["admitted"].get(identity)
    if (prior is not None and prior != row) or (admitted is not None and admitted != digest):
        raise ValueError("conflicting replay of Omega successor")
    if prior is None and admitted is None:
        data["pending"][identity] = row
        return True
    return False


# ── the cognitive transaction record (cross-lane spec section 6, T8) ───────────────────
#
# One recoverable record per fold, in THIS ledger: the same atomic writer (mkstemp -> fsync
# -> os.replace), the same lock, the store admission already consults. No new log, scheduler
# or writer. The record is opened BEFORE any lane_entry mail is pinned or a successor staged,
# names everything it is about to publish (mail ids and effect ids are host-minted from the
# fold identity, so it can), and carries the successor row and the mail by value — so a crash
# after the record and before staging (boundary B2) is completed FROM THE RECORD at boot,
# exactly once. Staging the successor and marking the record staged are ONE write.

def transaction(fold_id: str):
    """The durable transaction record for a fold identity, or None. A snapshot."""
    with _RESEED_LOCK:
        record = _read_successors()["transactions"].get(str(fold_id or ""))
        return json.loads(json.dumps(record)) if record is not None else None


def transactions() -> dict:
    with _RESEED_LOCK:
        return json.loads(json.dumps(_read_successors()["transactions"]))


def unpublished_job_continuation(job_id):
    with _RESEED_LOCK:
        data = _read_successors()
        for record in data["transactions"].values():
            metadata = (record.get("successor") or {}).get("metadata") or {}
            if record["published"] or metadata.get("agent_job_id") != job_id:
                continue
            claim = _continuation_record(data, metadata)
            if claim is None or claim["state"] not in {"completed", "cancelled"}:
                return True
        return False


def prepare_transaction_publication(fold_id):
    """Honor terminal successor history before any recovery publication is attempted."""
    with _RESEED_LOCK:
        data = _read_successors()
        record = data["transactions"].get(fold_id)
        if record is None:
            raise ValueError(f"unknown cognitive transaction {fold_id!r}")
        if record["published"] or not record["successor"]:
            return json.loads(json.dumps(record))
        metadata = record["successor"]["metadata"]
        claim = _continuation_record(data, metadata)
        if claim is not None and claim["state"] in {"cancelled", "completed"}:
            retained = []
            for outgoing in record["outgoing"]:
                if outgoing["pin"]["delivery_point"] != "lane_entry":
                    retained.append(outgoing)
                    continue
                record["withheld"].append({
                    "disposition": "undeliverable", "reason": f"continuation {claim['state']}",
                    "requirement": outgoing["pin"]["requirement"],
                    "injection": outgoing["pin"]["payload"],
                    "provenance": outgoing["pin"]["provenance"]})
            record.update(successor=None, staging="none", outgoing=retained)
            _write_successors(data)
        elif metadata.get("continuation_claim") and (
                claim is None or claim["state"] not in {"waiting", "claimed", "running"}):
            raise RuntimeError("continuation publication awaits ownership recovery")
        return json.loads(json.dumps(record))


def transaction_horizon() -> int:
    """Folds born at or before this, with no record on the ledger, may not publish."""
    with _RESEED_LOCK:
        return int(_read_successors()["transaction_horizon"])


def open_transaction(record: dict):
    """Durably open a fold's record. Returns ``(record on the ledger, created)``.

    The FIRST record for a fold identity stands: a replay of the fold — a new episode after a
    restart — gets that record back and must complete or honour it, never replace it.

    Closed records are pruned for space, oldest fold first — and each pruning RAISES THE
    HORIZON to that fold's birth order. A fold born at or before the horizon that finds no
    record may be a replay whose record is gone: it is REFUSED here rather than allowed to
    publish a second time (review 2026-09-17). Returns ``(None, False)`` for such a fold.
    """
    if set(record) != _TRANSACTION_FIELDS:
        raise ValueError("cognitive transaction fields incomplete or unknown")
    from pressure_loop.continuation import work_lock

    metadata = dict((record.get("successor") or {}).get("metadata") or {})
    with _RESEED_LOCK, work_lock(metadata):
        data = _read_successors()
        existing = data["transactions"].get(record["fold_id"])
        if existing is not None:
            return json.loads(json.dumps(existing)), False
        if 0 < int(record["origin_seq"]) <= int(data["transaction_horizon"]):
            return None, False
        record = json.loads(json.dumps(record))
        reason = _claim_continuation_into(data, metadata) if metadata else ""
        if reason:
            from pressure_loop.omega_successor import _decline

            _decline(record["episode_id"], reason)
            for outgoing in record["outgoing"]:
                record["withheld"].append({
                    "target_lane": outgoing["pin"]["target_lane"],
                    "requirement": outgoing["pin"]["requirement"],
                    "disposition": "undeliverable", "reason": reason,
                    "injection": outgoing["pin"]["payload"],
                    "provenance": outgoing["pin"]["provenance"]})
            record.update(successor=None, staging="none", outgoing=[], minted_forgotten=True)
        data["transactions"][record["fold_id"]] = json.loads(json.dumps(record))
        # Only records whose minted ids are already forgotten: pruning one that still owes
        # that cleanup would strand its ids in the mail store for good.
        closed = sorted((key for key, row in data["transactions"].items()
                         if row["closed"] and row["minted_forgotten"]),
                        key=lambda key: (data["transactions"][key]["origin_seq"],
                                         data["transactions"][key]["opened_at"]))
        for key in closed[:max(0, len(closed) - _MAX_CLOSED_TRANSACTIONS)]:
            data["transaction_horizon"] = max(int(data["transaction_horizon"]),
                                              int(data["transactions"][key]["origin_seq"]))
            del data["transactions"][key]
        _write_successors(data)
        return json.loads(json.dumps(record)), True


def complete_transaction(fold_id: str):
    """Stage the record's successor (if any) and mark the record published — ONE write.

    Returns the staged successor contributor, or None when the record stages none. Replayed,
    it changes nothing: the ledger's own digest guard refuses a differing successor.
    """
    with _RESEED_LOCK:
        prepare_transaction_publication(fold_id)
        data = _read_successors()
        record = data["transactions"].get(str(fold_id or ""))
        if record is None:
            raise ValueError(f"unknown cognitive transaction {fold_id!r}")
        successor = make_contributor(**record["successor"]) if record["successor"] else None
        changed = not record["published"]
        if successor is not None:
            changed = _stage_successor_into(data, successor) or changed
            record["staging"] = "staged"
        record["published"] = True
        if changed:
            _write_successors(data)
        return successor


def settle_transaction(fold_id: str, *, pending_canonical: list) -> bool:
    """Record which accepted receipts still await their canonical drain; close when none do."""
    with _RESEED_LOCK:
        data = _read_successors()
        record = data["transactions"].get(str(fold_id or ""))
        if record is None:
            return False
        record["pending_canonical"] = [str(one) for one in pending_canonical]
        record["closed"] = record["published"] and not record["pending_canonical"]
        _write_successors(data)
        return record["closed"]


def mark_minted_forgotten(fold_id: str) -> None:
    with _RESEED_LOCK:
        data = _read_successors()
        record = data["transactions"].get(str(fold_id or ""))
        if record is not None and not record["minted_forgotten"]:
            record["minted_forgotten"] = True
            _write_successors(data)


# ── lane-entry delivery (spec sections 2, 4 and 6; T9) ───────────────────────────────────
#
# THE DURABLE RECORD OF PERFORMED EFFECTS. A continuation's lane-entry mail is delivered
# against the transaction record of the fold that pinned it, keyed by the mail's host-minted
# ``effect_id`` — which survives redelivery to a different episode on a different pipeline. A
# consumer consults this record BEFORE it lets the lane act: the mail store's lease protects
# only the mail's deletion, never the effect (spec section 2).

def _delivery_standing(row, *, episode_id: str, process_epoch: str, now: float) -> str:
    """Whether ``episode_id`` may take (or keep) one effect's delivery."""
    if row is None or row["state"] == "abandoned":
        return "free"
    mine = row["episode_id"] == episode_id and row["process_epoch"] == process_epoch
    if row["state"] == "performed":
        return "performed"
    if row["state"] == "started":
        if mine:
            return "free"
        # A RESERVATION: the lane was never let past its first inference, so the record
        # SHOWS the effect has not happened. It holds exactly as long as the mail lease it
        # rests on: delivery proves mail ownership first, so a holder whose lease lapsed can
        # never reach ``executing`` — whether its process died or merely stalled. (A dead
        # holder needs no separate rule: its mail cannot be claimed before that same
        # deadline either.)
        return "held_elsewhere" if float(row["reserved_until"]) > now else "free"
    # executing: the lane was inferring or acting. If its process is gone, nothing on record
    # says how far it got, so nothing on record shows the effect has NOT run.
    if mine:
        return "executing"
    return "held_elsewhere" if row["process_epoch"] == process_epoch else "interrupted"


def begin_lane_entry(fold_id: str, *, episode_id: str, process_epoch: str, effects: list,
                     at: str, reserved_until: float) -> str:
    """Take the delivery of ``effects`` (``{mail_id, effect_id}`` rows) for one episode.

    ``reserved_until`` is the deadline of the mail lease the delivery rests on.

    All or nothing, in ONE write. Returns ``started`` when every effect is now this
    episode's, else the reason none was taken: ``unknown_record`` (the fold's record is gone:
    nothing can show the effect has not run), ``unknown_effect`` (the record never published
    it), ``performed``, ``interrupted`` or ``held_elsewhere``.
    """
    with _RESEED_LOCK:
        data = _read_successors()
        now = time.time()
        record = data["transactions"].get(str(fold_id or ""))
        if record is None:
            return "unknown_record"
        published = {str(row["effect_id"]): str(row["mail_id"]) for row in record["outgoing"]}
        worst = ""
        for effect in effects:
            effect_id = str(effect["effect_id"])
            if published.get(effect_id) != str(effect["mail_id"]):
                return "unknown_effect"
            standing = _delivery_standing(record["deliveries"].get(effect_id),
                                          episode_id=str(episode_id),
                                          process_epoch=str(process_epoch), now=now)
            if standing != "free":
                # One answer for the set: the most final reason wins.
                order = ("performed", "interrupted", "executing", "held_elsewhere")
                worst = min((worst or standing, standing), key=order.index)
        if worst:
            return worst
        for effect in effects:
            record["deliveries"][str(effect["effect_id"])] = {
                "effect_id": str(effect["effect_id"]), "mail_id": str(effect["mail_id"]),
                "episode_id": str(episode_id), "process_epoch": str(process_epoch),
                "state": "started", "prompt_digest": "", "prompt_chars": 0, "at": str(at),
                "reserved_until": float(reserved_until)}
        if effects:
            _write_successors(data)
        return "started"


# ``abandoned`` is the OWNER's statement that nothing ran: before its first inference
# (``started``), or when the acknowledgement failed right after the prompt was recorded
# (``executing``, still before any inference).
_DELIVERY_MOVES = {"executing": {"started"}, "performed": {"executing"},
                   "abandoned": {"started", "executing"}}


def advance_lane_entry(fold_id: str, *, episode_id: str, process_epoch: str, effect_ids: list,
                       to: str, at: str, prompt_digest: str = "", prompt_chars: int = 0) -> bool:
    """Move this episode's deliveries ``started → executing → performed`` (or to
    ``abandoned``). All or nothing; False — and nothing written — unless EVERY named effect is
    this episode's, in this process, in the state the move starts from.

    ``executing`` records the BUILT PROMPT (its digest and size): the durable outcome the
    mail's acknowledgement waits for (spec section 2).
    """
    if to not in _DELIVERY_MOVES:
        raise ValueError(f"unknown lane-entry delivery move {to!r}")
    with _RESEED_LOCK:
        data = _read_successors()
        record = data["transactions"].get(str(fold_id or ""))
        if record is None:
            return False
        rows = [record["deliveries"].get(str(effect_id)) for effect_id in effect_ids]
        if not rows or any(
                row is None or row["episode_id"] != str(episode_id)
                or row["process_epoch"] != str(process_epoch)
                or row["state"] not in _DELIVERY_MOVES[to] for row in rows):
            return False
        for row in rows:
            row.update(state=to, at=str(at))
            if to == "executing":
                row.update(prompt_digest=str(prompt_digest), prompt_chars=int(prompt_chars))
        _write_successors(data)
        return True


def stage_reseed_suppress(contributor_type: str, weight: float) -> None:
    """Stage Omega pressure_feedback suppression for the next gather."""
    ctype = (contributor_type or "").strip()
    if not ctype:
        return
    w = _clamp_unit(weight)
    if w <= 0.001:
        return
    with _RESEED_LOCK:
        _RESEED_SUPPRESS[ctype] = max(_RESEED_SUPPRESS.get(ctype, 0.0), w)


def _read_reseed_staging() -> List[Contributor]:
    """Snapshot the staged signals WITHOUT consuming them.

    A signal is removed by :func:`release_staged` when an episode ignites on it,
    never by the act of being looked at. Observation does not consume volition —
    the same rule omega_levels states for setpoints ("Pressure polling is
    observational and NEVER consumes a setpoint").
    """
    with _RESEED_LOCK:
        return list(_RESEED_STAGING.values()) + [make_contributor(**row) for row in _read_successors()["pending"].values()]


def release_staged(contributor: Contributor) -> bool:
    """Retire the staged signal an episode just ignited on. True iff one went.

    THE death of a pressure signal (operator 2026-08-15): "it needs to die on
    episode ignition so omega can either emit another pressure signal or go
    idle." Called at
    EpisodeStore.open, so both halves of the one lifecycle end at the same
    instant. Unknown or already-released signals are harmless no-ops.
    """
    if contributor is None:
        return False
    key = _staging_key(contributor)
    with _RESEED_LOCK:
        if contributor.type == "omega_successor":
            data = _read_successors()
            row = data["pending"].get(key[1])
            if row is None:
                restored = _successor_row(contributor)
                claim = _continuation_record(data, contributor.metadata)
                if (data["admitted"].get(key[1]) == _successor_digest(restored)
                        and claim is not None and claim["state"] == "claimed"
                        and _transition_continuation_into(data, contributor.metadata, "running")):
                    _write_successors(data)
                return False
            if row is None or row != _successor_row(contributor):
                return False
            del data["pending"][key[1]]
            data["admitted"][key[1]] = _successor_digest(row)
            _transition_continuation_into(data, contributor.metadata, "running")
            _write_successors(data)
            return True
        return _RESEED_STAGING.pop(key, None) is not None


def retire_successor(contributor: Contributor, reason: str) -> bool:
    """End a staged Omega successor that can no longer be admitted. Loud, never raised.

    The retirement takes the admitted slot, so replaying the same staging is a no-op
    rather than a resurrection.
    """
    identity = str(contributor.metadata["successor_id"])
    with _RESEED_LOCK:
        data = _read_successors()
        row = data["pending"].get(identity)
        if row is None:
            if data["admitted"].get(identity) != _successor_digest(_successor_row(contributor)):
                return False
        else:
            del data["pending"][identity]
            data["admitted"][identity] = _successor_digest(row)
        from pressure_loop.omega_successor import cancellation_reason

        state = "cancelled" if cancellation_reason(contributor.metadata) else "released"
        transitioned = _transition_continuation_into(data, contributor.metadata, state, reason)
        if row is None and not transitioned:
            return False
        _write_successors(data)
    from pressure_loop.omega_successor import _decline

    _decline(str(contributor.metadata.get("parent_episode_id") or ""), f"staged successor retired: {reason}")
    settle_retired_jobs()
    return True


# Stops whose durable write FAILED. A suppressed exception is not a cancellation that
# succeeded (review 2026-09-18): the stop stays authority for this process, is reported to the
# caller, and is retried until the ledger takes it.
_UNRESOLVED_STOPS: Dict[str, str] = {}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _lineages_with_outstanding_work(data) -> Optional[set]:
    """Every turn whose work can still run, or None when that cannot be established.

    A stop record may only be dropped once nothing it governs can run again, so this reads
    EVERY place such work waits: staged successors here, the FIFO the dispatcher leases from,
    the pipeline envelopes of in-flight and restorable episodes (a fold that has not published
    yet has no staged row and no transaction record — its envelope is the only trace of it),
    and open cognitive transactions, whose replay may publish a successor after a restart
    (review 2026-09-18).

    None means undecidable, and the caller then drops nothing: an unreadable store must not
    be read as "that work is finished". An open transaction whose successor is not built yet
    names no lineage at all, so it makes the whole answer undecidable rather than silently
    protecting nothing.
    """
    from core import pipeline_state
    from pressure_loop import cognition_queue as queue

    def lineage_of(metadata) -> str:
        row = dict(metadata or {})
        return str(row.get("original_cycle_id") or row.get("cycle_id") or "")

    live = set()
    try:
        for row in data["pending"].values():
            live.add(lineage_of(row["metadata"]))
        for record in data["transactions"].values():
            if record["closed"]:
                continue
            successor = record["successor"]
            if successor is None:
                return None            # its lineage is not knowable yet
            live.add(lineage_of(dict(successor).get("metadata")))
        for contributor in queue.peek_inputs():
            live.add(lineage_of(getattr(contributor, "metadata", None)))
        for envelope in pipeline_state.envelopes():
            # The envelope's own cycle is the turn the pipeline is SERVING: a fold that is
            # still running has opened no transaction record yet, so this is the only place
            # its work appears at all (review 2026-09-18).
            # A CONTINUATION's own cycle is its successor id, not the turn it continues,
            # and ``mark_acted`` drops the continuation that carried the root lineage — so the
            # envelope records that lineage itself and keeps it through foldback (review
            # 2026-09-18). Both are read: either may name the stopped work.
            live.add(str(envelope.cycle_id or ""))
            live.add(str(getattr(envelope, "origin_cycle_id", "") or ""))
            carried = dict(dict((envelope.continuation or {}).get("contributor") or {})
                           .get("metadata") or {})
            live.add(lineage_of(carried))
    except Exception:  # noqa: BLE001 - undecidable, never "finished"; see docstring
        log.warning("could not establish which turns still have outstanding work", exc_info=True)
        return None
    live.discard("")
    return live


def _kept_stop_records(data, stopped: Dict[str, Any]) -> Dict[str, Any]:
    """Drop only the records whose work is provably finished — and never on a size cap.

    A bounded recent-history tail cannot establish completion: it evicted the record of work
    that was merely waiting for capacity, and the continuation was then admitted (review
    2026-09-18). These records are created by an operator's explicit stop, so they accrue at
    human rate and their steady-state size tracks LIVE stopped work rather than history.
    """
    live = _lineages_with_outstanding_work(data)
    if live is None:
        return stopped
    return {cycle: row for cycle, row in stopped.items() if cycle in live}


def record_work_stopped(cycle_ids: Sequence[str], *, reason: str) -> Dict[str, Tuple[str, ...]]:
    """Record DURABLY that the operator stopped the work of these turns.

    Returns ``{"recorded": (...), "unresolved": (...)}``. NEVER raises: the halt itself has
    already reached the work, and the operator's stop must not become an exception at their
    fingertips. A write that failed is reported as ``unresolved`` instead — retained in memory
    as authority for this process and retried by the pressure tick — because suppressing the
    exception must not imply the cancellation was made durable.

    The unambiguous stop is operator authority over execution (spec section 11, T12), and the
    work it stops can outlive it: a continuation may be staged, queued behind busy pipelines,
    in flight, or republished by a transaction replay after a restart. So the authority cannot
    live in the process-local work-halt flag the running batch polls — that flag is an
    execution signal with a lease, and a probe that advanced past its TTL gathered a stopped
    continuation successfully. It lives here, beside the continuations it governs, and
    publication (``build_omega_successor``) and admission (the gather and the dispatcher) both
    consult it.
    """
    wanted = [str(cycle or "").strip() for cycle in cycle_ids or ()]
    cycles = sorted({cycle for cycle in wanted if cycle})
    if not cycles:
        return {"recorded": (), "unresolved": ()}
    note = str(reason or "")[:200]
    with _RESEED_LOCK:
        return _persist_stops({cycle: note for cycle in cycles})


def _persist_stops(pending: Dict[str, str]) -> Dict[str, Tuple[str, ...]]:
    """Write these stops, holding whatever the ledger would not take. Caller holds the lock."""
    stamp = _now_iso()
    try:
        data = _read_successors()
        stopped = dict(data["stopped"])
        for cycle, note in pending.items():
            stopped.setdefault(cycle, {"at": stamp, "reason": note})
        data["stopped"] = _kept_stop_records(data, stopped)
        # A stop being recorded right now governs work that may not be visible in any store
        # yet — the fold that will publish it is still running — so it is never pruned here.
        for cycle, note in pending.items():
            data["stopped"].setdefault(cycle, {"at": stamp, "reason": note})
        _write_successors(data)
    except Exception as exc:  # noqa: BLE001 - retained and retried; see the docstring above
        for cycle, note in pending.items():
            _UNRESOLVED_STOPS[cycle] = note or "operator stop"
        log.error("the operator's stop could not be recorded durably for %s (%s); it is retained "
                  "in memory and retried", sorted(pending), exc, exc_info=True)
        return {"recorded": (), "unresolved": tuple(sorted(pending))}
    for cycle in pending:
        _UNRESOLVED_STOPS.pop(cycle, None)
    return {"recorded": tuple(sorted(pending)), "unresolved": ()}


def retry_unresolved_stops() -> Dict[str, Tuple[str, ...]]:
    """Persist stops whose ledger write failed. Cheap when there are none; run every tick."""
    with _RESEED_LOCK:
        if not _UNRESOLVED_STOPS:
            return {"recorded": (), "unresolved": ()}
        return _persist_stops(dict(_UNRESOLVED_STOPS))


def unresolved_stops() -> Tuple[str, ...]:
    """Turns whose stop is authority in this process but is not yet durable."""
    with _RESEED_LOCK:
        return tuple(sorted(_UNRESOLVED_STOPS))


def stop_standing(cycle_id: str) -> Tuple[str, str]:
    """``("stopped", why)``, ``("unknown", why)`` or ``("", "")`` for this turn's work.

    THREE states, not two (review 2026-09-18). A read failure used to answer with a
    cancellation reason, and admission acted on it: the queued item was acknowledged and
    removed, so an unreadable ledger destroyed work nobody had cancelled. Unknown now says so,
    and each caller decides — admission and the gather DEFER (the work exists and must not be
    consumed or retired on a guess), publication DECLINES (the work does not exist yet and
    must not be born against a stop that may be standing).
    """
    cycle = str(cycle_id or "").strip()
    if not cycle:
        return "", ""
    with _RESEED_LOCK:
        if cycle in _UNRESOLVED_STOPS:
            # In-memory authority: this process took the stop even though the ledger would
            # not. That is CONFIRMED, not unknown — the operator's stop did land here.
            return "stopped", (f"the operator stopped the work of turn {cycle}; that stop is not "
                               f"yet durable (retained in memory, retrying)")
        try:
            row = _read_successors()["stopped"].get(cycle)
        except Exception as exc:  # noqa: BLE001 - unknown, and unknown is not a cancellation
            log.warning("could not read the durable stop record", exc_info=True)
            # The cause rides the reason: a persistently unknown head is eventually reported
            # with it (AGENTS.md section 2.1 — the cause must survive).
            return "unknown", (f"the durable stop record for turn {cycle} cannot be read: "
                               f"{type(exc).__name__}: {exc}")
    if row is None:
        return "", ""
    return "stopped", (f"the operator stopped the work of turn {cycle}"
                       + (f": {row['reason']}" if row["reason"] else ""))


def work_stopped(cycle_id: str) -> str:
    """Why the operator's stop forbids continuing this turn's work — CONFIRMED stops only."""
    standing, why = stop_standing(cycle_id)
    return why if standing == "stopped" else ""


def staged_signals() -> Tuple[Contributor, ...]:
    """Read-only view of live staged signals (telemetry, tests, the dispatcher)."""
    with _RESEED_LOCK:
        return tuple(_read_reseed_staging())


def _drain_reseed_suppress() -> Dict[str, float]:
    """Atomically snapshot + clear the reseed-suppress store under _RESEED_LOCK."""
    with _RESEED_LOCK:
        suppress = dict(_RESEED_SUPPRESS)
        _RESEED_SUPPRESS.clear()
    return suppress


def _dedup_key(c: Contributor) -> Tuple[str, str]:
    """ONE authoritative turn identity, shared with the queue box.

    This used to key on `message` first while the queue keyed on
    `system_query_id`/`cycle_id` first. Two functions ranking the same fields in
    opposite order over the same objects meant pin selection and queue
    acknowledgement disagreed about which turn they were holding — the
    mechanical root of the deterministic double-fired episode on every chat
    answer to a system query (audit §1.3). Any cure that leaves two keys alive
    silently re-breaks it, so there is exactly one.

    Message-first was also wrong on its own terms: repeated text ("yes", "ok",
    "do it") is routine, and it collapsed two distinct turns into one key.
    """
    from pressure_loop.cognition_queue import _box_dedup_key

    return _box_dedup_key(c)


def _enqueue(contributors: Sequence[Contributor]) -> List[Contributor]:
    """Hold operator inputs that arrive during the halt in the durable queue
    box (deduped, order of input). Autonomous drives are skipped — they are
    re-derived from state each time the loop runs between episodes.

    Returns the operator inputs that were NEWLY enqueued this call (dedup-aware),
    so the caller can record their ids on the running episode (#E3)."""
    from pressure_loop.cognition_queue import enqueue_input

    enqueued: List[Contributor] = []
    deduped = 0
    for c in contributors:
        if c.weight <= 0.001:
            continue
        if c.type not in _OPERATOR_INPUT_TYPES:
            continue
        if enqueue_input(c, operator_activity=True):
            enqueued.append(c)
        else:
            deduped += 1
    if enqueued or deduped:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "operator_enqueue",
                summary=f"enqueued={len(enqueued)} deduped={deduped}",
                enqueued_count=len(enqueued),
                deduped_count=deduped,
            )
        except Exception:
            pass
    return enqueued




def _clamp_unit(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:
        return default
    return parsed


def _neuromod_from_last_body(episode_store: Optional[Any] = None) -> Dict[str, float]:
    """Gate modulation from last-closed episode body-state (never-raise).

    Absence of ``last_closed_body()`` or a missing/non-dict ``neuromod`` key
    yields all-zero modulation — legitimate baseline, not failure.
    """
    zeros = {"dopamine": 0.0, "serotonin": 0.0, "stress": 0.0, "spike": 0.0}
    if episode_store is None:
        return dict(zeros)
    try:
        body = episode_store.last_closed_body()
    except Exception as exc:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "evaluate",
            status="degraded",
            reason="neuromod_extraction_failed",
            ok=False,
            error=str(exc),
        )
        return dict(zeros)
    if not isinstance(body, dict):
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "neuromod_baseline",
                status="skip",
                summary="no_last_closed_body",
                reason="no_last_closed_body",
            )
        except Exception:
            pass
        return dict(zeros)
    neuromod = body.get("neuromod")
    if not isinstance(neuromod, dict):
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "neuromod_baseline",
                status="skip",
                summary="no_neuromod",
                reason="no_neuromod",
            )
        except Exception:
            pass
        return dict(zeros)
    try:
        from autonomic.symphony_neuromod import compute_closed_body_spike

        def _body_float(key: str) -> float:
            raw = neuromod.get(key)
            if raw is None:
                return 0.0
            parsed = float(raw or 0.0)
            if parsed != parsed:
                raise ValueError(f"invalid neuromod {key}")
            return parsed

        return {
            "dopamine": _body_float("dopamine"),
            "serotonin": _body_float("serotonin"),
            "stress": _body_float("stress"),
            "spike": float(compute_closed_body_spike(neuromod)),
        }
    except Exception as exc:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "evaluate",
            status="degraded",
            reason="neuromod_extraction_failed",
            ok=False,
            error=str(exc),
        )
        return dict(zeros)


def _serotonin_patience(neuromod: Dict[str, float]) -> float:
    """Patience axis [0,1]: persisted spike serotonin or aversive dopamine proxy."""
    raw = _safe_float(neuromod.get("serotonin", 0.0))
    if raw > PRESSURE_CEILING_EPS:
        return _clamp_unit(raw)
    dopamine = _safe_float(neuromod.get("dopamine", 0.0))
    return _clamp_unit(max(0.0, -dopamine))


def _dopamine_gamma(neuromod: Dict[str, float]) -> float:
    cfg = pressure_neuromod_weight_modulation()
    if not cfg.get("enabled", True):
        return 1.0
    dopamine = _safe_float(neuromod.get("dopamine", 0.0))
    coeff = float(cfg.get("dopamine_coeff", 0.5))
    g_min = float(cfg.get("gamma_min", 0.25))
    g_max = float(cfg.get("gamma_max", 2.5))
    return max(g_min, min(g_max, 1.0 + coeff * dopamine))


def _hybrid_effective_gate_params(
    neuromod: Optional[Dict[str, float]],
    *,
    ceiling_override: Optional[float] = None,
) -> Tuple[float, float]:
    """Return the pressure-only ignition boundary.

    ``neuromod`` remains in the signature for compatibility with callers that
    also carry chemistry, but chemistry is advisory decision context and has
    no authority to move the pressure gate.
    """
    _ = neuromod
    base_ceiling = pressure_winner_ceiling(override=ceiling_override)
    base_margin = pressure_winner_margin()
    return base_ceiling, base_margin


def _emit_neuromod_weights(
    neuromod: Dict[str, float],
    *,
    contributors: Optional[Sequence[Contributor]] = None,
    context: str = "",
) -> None:
    """Failure-soft observability for advisory neuromod state.

    The event name is retained for telemetry compatibility; no contributor
    weights are changed by this hook.
    """
    try:
        from guardian.terminal_feedback import emit_data

        gamma = _dopamine_gamma(neuromod)
        patience = _serotonin_patience(neuromod)
        emit_data(
            "pressure",
            "neuromod_weights",
            summary=(
                f"context={context} gamma={gamma:.3f} "
                f"dopamine={_safe_float(neuromod.get('dopamine')):.3f} "
                f"serotonin={_safe_float(neuromod.get('serotonin')):.3f}"
            )[:200],
            context=context,
            gamma=gamma,
            patience=patience,
            dopamine=_safe_float(neuromod.get("dopamine")),
            serotonin=_safe_float(neuromod.get("serotonin")),
            stress=_safe_float(neuromod.get("stress")),
            spike=_safe_float(neuromod.get("spike")),
            contributor_count=len(contributors) if contributors is not None else 0,
        )
    except Exception:
        pass


def _apply_neuromod_to_weights(
    contributors: List[Contributor],
    neuromod: Dict[str, float],
) -> List[Contributor]:
    """Compatibility adapter that preserves raw pressure weights.

    Chemistry used to mutate contributors here. Keeping the helper as an
    explicit no-op prevents old callers from regaining ignition authority while
    they migrate to the episode's advisory ``neuromod_state`` context.
    """
    _emit_neuromod_weights(
        neuromod,
        contributors=contributors,
        context="advisory_only",
    )
    return [
        Contributor(
            source=c.source,
            type=c.type,
            weight=c.weight,
            metadata=dict(c.metadata),
        )
        for c in contributors
    ]


def modulate_contributor_weight_map(
    contributors: Dict[str, float],
    neuromod: Optional[Dict[str, float]],
) -> Dict[str, float]:
    """Compatibility adapter returning unmodified pressure-field scalars."""
    if neuromod is not None:
        _emit_neuromod_weights(neuromod, context="advisory_only")
    return dict(contributors)


def _at_ceiling(weight: float, ceiling: float) -> bool:
    return float(weight) >= float(ceiling) - PRESSURE_CEILING_EPS


def resolve_hybrid_winner_weights(
    pairs: Sequence[Tuple[str, float]],
    *,
    ceiling: float,
    margin: float,
) -> Tuple[Optional[str], float, str]:
    """Ignition: the strongest signal that reaches the ceiling wins. Ties go to
    whoever arrived first.

    ``margin`` is accepted and ignored. It used to suppress ignition entirely
    when the top two weights were within ``winner_margin`` of each other
    ("ambiguous_winner"), and that was a category error (operator 2026-08-15):

        "it is structurally impossible for a lane to have a tie because each
        lane is run from a single ignited episode from a single pressure input
        ... and when the same loop sends signals back to back, its first in and
        the loser queues until next pressure ignition."

    There is no contest here to adjudicate — a lane runs ONE episode from ONE
    input. Two signals arriving together are a QUEUE, not an ambiguity, and the
    answer is order of arrival, which is the same law the operator line has
    always followed ("order of input is law"). Refusing to choose between them
    did not defer the decision, it dropped BOTH: on 2026-08-15 two of Omega's
    own drives landed on 0.850, the gate suppressed on gap=0.000, and autonomous
    cognition parked for 7m23s holding pressure eight times over its threshold.

    The sort is STABLE, so equal weights already rank in arrival order and
    ``ranked[0]`` is first-in by construction. The loser is not discarded: since
    the lifecycle fix, a staged signal persists until an episode ignites on it,
    so it simply waits for the next ignition — exactly "the loser queues".
    """
    del margin  # retained for call-site compatibility; see above
    active = [(k, _clamp_unit(w)) for k, w in pairs if w > 0.001]
    if not active:
        return None, 0.0, "below_ceiling"
    ranked = sorted(active, key=lambda item: -item[1])
    top_key, top_w = ranked[0]
    if top_w < float(ceiling) - PRESSURE_CEILING_EPS:
        return None, top_w, "below_ceiling"
    return top_key, top_w, ""


def _resolve_hybrid_contributor_winner(
    contributors: Sequence[Contributor],
    *,
    ceiling: float,
    margin: float,
) -> Tuple[Optional[Contributor], str]:
    """Map hybrid weight pairs back to Contributor objects (by type)."""
    pairs = [(c.type, c.weight) for c in contributors]
    key, _w, suppress = resolve_hybrid_winner_weights(
        pairs,
        ceiling=ceiling,
        margin=margin,
    )
    if suppress:
        return None, suppress
    for c in contributors:
        if c.type == key:
            return c, ""
    # The winning TYPE resolved to no contributor — a bug in the caller's list,
    # not an ambiguity. Name it as such: it used to report "ambiguous_winner",
    # which sent anyone reading the telemetry looking for a tie that was not
    # there.
    return None, "winner_type_missing"


def hybrid_ignition_gate(
    contributors: Dict[str, float],
    *,
    ceiling: Optional[float] = None,
    margin: Optional[float] = None,
    neuromod: Optional[Dict[str, float]] = None,
) -> Tuple[bool, str, Optional[str]]:
    """Pressure evaluation gate on field-id weights.

    ``neuromod`` is observed and forwarded as advisory context, but cannot
    alter weights, thresholds, margins, or the winning pressure. Returns
    (open_ok, suppress_reason, dominant_field_id).
    """
    weights = contributors
    if ceiling is not None and margin is not None:
        ceil, marg = _clamp_unit(ceiling), _clamp_unit(margin)
    else:
        ceil, marg = _hybrid_effective_gate_params(
            neuromod,
            ceiling_override=ceiling,
        )
    pairs = [(cid, float(w or 0.0)) for cid, w in weights.items() if cid != "_drive"]
    key, _w, suppress = resolve_hybrid_winner_weights(
        pairs,
        ceiling=ceil,
        margin=marg,
    )
    if suppress:
        if neuromod is not None:
            _emit_neuromod_weights(neuromod, context="hybrid_gate_suppressed")
        return False, suppress, None
    if neuromod is not None:
        _emit_neuromod_weights(neuromod, context="hybrid_gate_open")
    return True, "", key


def gather_contributors(
    shared_state: SharedState,
    *,
    episode_store: Optional[Any] = None,
    learning_state_envelope: Optional[Dict[str, Any]] = None,
) -> List[Contributor]:
    """Gather operator ingress and explicit Omega-authored successors.

    Deterministic pressure-field observations are not read: they may inform
    telemetry or Omega's fold elsewhere, but they do not own ignition.
    """
    from pressure_loop import pressure_field as pf

    del learning_state_envelope
    readers = pf.build_registry()
    out: List[Contributor] = []
    for field_id, reader in readers.items():
        if field_id not in {
            pf.CONTRIBUTOR_OPERATOR_INTENT,
            pf.CONTRIBUTOR_OPERATOR_MESSAGE,
        }:
            continue
        # Operator ingress is authoritative. A failing ingress reader must not
        # silently turn a real operator message into an idle tick.
        weight = reader(shared_state)
        weight = max(0.0, float(weight or 0.0))
        if weight <= 0.001:
            continue
        ctype = _FIELD_ID_TO_TYPE.get(field_id, field_id)
        source = "operator"
        meta: Dict[str, Any] = {"field_id": field_id}
        if field_id == pf.CONTRIBUTOR_OPERATOR_INTENT:
            from pressure_loop.operator_pressure import read_operator_directive

            directive = read_operator_directive(shared_state)
            if isinstance(directive, dict):
                meta.update(
                    {
                        "message": str(directive.get("text") or ""),
                        "domain": directive.get("domain"),
                        "cycle_id": str(directive.get("cycle_id") or ""),
                    }
                )
            out.append(make_contributor(source, ctype, weight, meta))
            continue
        if field_id == pf.CONTRIBUTOR_OPERATOR_MESSAGE:
            # The operator's turn is NEVER rebuilt here. It comes from the
            # durable queue, leased by evaluate() — one gate, one copy
            # (AGENTS.md §4.0). This branch used to synthesise a SECOND
            # operator contributor from the retired SharedState mirror, which
            # then out-ranked the queued turn in _apply_force_dominant and
            # replaced its metadata with a strict subset (audit §1.2, §1.3).
            # The reader above already reports whether anyone is in line; that
            # is all this lane contributes.
            continue
        out.append(make_contributor(source, ctype, weight, meta))
    rollback = _rollback_event_contributor()
    if rollback is not None:
        out.append(rollback)
    # Atomic read-and-clear under _RESEED_LOCK (#13) so a stage on the HUD/
    # operator thread cannot interleave with this drain on the tick thread.
    staged = _read_reseed_staging()
    if episode_store is not None:
        active = {str(ep.episode_id) for ep in episode_store.active_episodes()}
        staged = [c for c in staged if c.type != "omega_successor" or c.metadata["parent_episode_id"] not in active]
    from pressure_loop.idle_wake import governor_enabled
    from pressure_loop.omega_successor import unrunnable_successor_reason

    # Autonomy is Omega choosing to continue. Work the operator started continues through
    # its successors; work nobody started (a level Omega set, a chain with no operator
    # cycle) runs only while the operator has idle learning engaged. Otherwise the mind
    # sits idle instead of re-igniting itself tick after tick (live 2026-09-14).
    idle_learning = governor_enabled()
    runnable: List[Contributor] = []
    for contributor in staged:
        reason = unrunnable_successor_reason(contributor.metadata) if contributor.type == "omega_successor" else None
        if (not reason and not idle_learning and contributor.type == "omega_successor"
                and not contributor.metadata.get("original_cycle_id")):
            reason = "idle learning is paused and no operator turn started this chain"
        if not reason:
            runnable.append(contributor)
            continue
        try:
            retire_successor(contributor, reason)
        except Exception:  # noqa: BLE001 - it is not admitted either way; the next pass retires it
            # A ledger that cannot be written used to raise out of the gather and take the
            # pressure tick with it — including the tick that retries the unresolved stop
            # (review 2026-09-18). The row simply is not admitted, and retirement is repeated
            # on the next pass.
            log.warning("could not retire the unrunnable successor %s; it stays staged and "
                        "unadmitted", contributor.metadata.get("successor_id"), exc_info=True)
    staged = runnable
    reseed_staged = len(staged)
    if staged:
        out.extend(staged)
    # Queued operator inputs are NOT drained here — evaluate() leases exactly
    # ONE per pressure cycle from the queue box, so each input gets its own
    # episode in order of arrival (drain-all merged them into one contest
    # and silently discarded the losers).
    suppress_types: Dict[str, float] = _drain_reseed_suppress()
    if suppress_types:
        suppressed: List[Contributor] = []
        for c in out:
            delta = suppress_types.get(c.type, 0.0)
            w = max(0.0, c.weight - delta)
            if w > 0.001:
                suppressed.append(make_contributor(c.source, c.type, w, dict(c.metadata)))
        out = suppressed
    if reseed_staged or suppress_types:
        try:
            from guardian.terminal_feedback import emit_data, emit_guardian_event

            emit_data(
                "pressure",
                "reseed_staging",
                episode_id="no-episode",
                summary=(
                    f"staged={reseed_staged} "
                    f"suppressed={','.join(sorted(suppress_types.keys())) or 'none'}"
                )[:200],
                staged_count=reseed_staged,
                suppressed_types=sorted(suppress_types.keys()),
                suppress_weights={k: float(v) for k, v in suppress_types.items()},
            )
            emit_guardian_event(
                "omega.pressure",
                "reseed_staging",
                staged_count=reseed_staged,
                suppressed_types=sorted(suppress_types.keys()),
            )
        except Exception:
            pass
    try:
        proactive_application = _proactive_lifecycle_contributor()
    except Exception:
        # The lifecycle ledger is operational bookkeeping. Its failure must be
        # loud and contained, never an organism-wide tick terminator.
        log.exception("proactive lifecycle pressure projection failed")
        proactive_application = None
    if proactive_application is not None and not any(
        contributor.type == "proactive_update" for contributor in out
    ):
        out.append(proactive_application)

    # Phase 4 cut 10 — the staged reflection window projected as pressure. The
    # staged file IS the durable record (the omega-levels pattern: state on disk,
    # read into the tick, consumed by the episode that acts on it), so there is
    # no window operator line and nothing to lease. Fail-hard on a read error:
    # "the staging file could not be read" must never present as "no reflection
    # is owed" — that is ten episodes silently going unremembered.
    from pressure_loop import window_lane

    window_ready = window_lane.pressure_contributor()
    if window_ready is not None and not any(
        contributor.type == window_ready.type for contributor in out
    ):
        out.append(window_ready)

    # Autonomous ignition authority is intentionally narrow. A successor may
    # come from Omega's prior-episode fold (including proactive delivery and an
    # operator-enabled idle-learning wake), never from deterministic audits,
    # mastery scans, pending-build scans, or other background observations.
    # Operator ingress remains authoritative, and dev rollback is an explicit
    # operator-triggered system event rather than autonomous cognition.
    authorized: List[Contributor] = []
    rejected: List[Contributor] = []
    for contributor in out:
        if contributor.source in {"operator", "omega"} or (
            contributor.source == "system" and contributor.type == "dev_rollback"
        ):
            authorized.append(contributor)
        else:
            rejected.append(contributor)
    out = authorized
    if rejected:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "autonomous_authority_filter",
                episode_id="no-episode",
                summary=f"rejected={len(rejected)} non-Omega autonomous contributor(s)",
                rejected=[
                    {"source": c.source, "type": c.type, "weight": c.weight} for c in rejected
                ],
            )
        except Exception:
            pass
    try:
        from guardian.pressure_observability import log_gather_snapshot

        log_gather_snapshot(out, episode_id="no-episode")
    except Exception:
        pass
    return out




def _merge_operator_pin(
    contributors: Sequence[Contributor],
    pin: Contributor,
) -> List[Contributor]:
    """Merge a pinned operator contributor into a gathered contributor set."""
    pin_key = _dedup_key(pin)
    out: List[Contributor] = []
    for c in contributors:
        if c.type in _OPERATOR_INPUT_TYPES and _dedup_key(c) == pin_key:
            continue
        out.append(c)
    out.append(
        make_contributor(
            pin.source,
            pin.type,
            1.0,
            dict(pin.metadata or {}),
        )
    )
    return out


def _apply_force_dominant(
    modulated: Sequence[Contributor],
    pin: Contributor,
) -> Tuple[Contributor, List[Contributor]]:
    """Pin one operator turn; retain the non-operator pressure field.

    Exactly one external operator cycle may enter an episode. A newer live
    ingress mirror can coexist with an older durable FIFO head, but it belongs
    to a later episode and must not leak into this prompt or its foldback.
    """
    pin_key = _dedup_key(pin)
    dom: Optional[Contributor] = None
    out: List[Contributor] = []
    for c in modulated:
        if c.type in _OPERATOR_INPUT_TYPES:
            if _dedup_key(c) == pin_key and dom is None:
                dom = pin
                out.append(dom)
            continue
        out.append(c)
    if dom is None:
        dom = pin
        out.append(dom)
    return dom, out


def evaluate_operator_ignition(
    shared_state: SharedState,
    *,
    episode_store: Optional[Any] = None,
    pin: Contributor,
) -> Optional[EpisodeIgnition]:
    """Gather live pressure contributors, pin operator as dominant, evaluate."""
    active = episode_store.current() is not None if episode_store else False
    gathered = gather_contributors(shared_state, episode_store=episode_store)
    merged = _merge_operator_pin(gathered, pin)
    return evaluate(
        merged,
        active_episode=active,
        shared_state=shared_state,
        episode_store=episode_store,
        force_dominant=pin,
    )


def _emit_ignition_gate(
    modulated: Sequence[Contributor],
    nm: Dict[str, float],
    ceiling: float,
    margin: float,
    dom: Optional[Contributor],
    suppress: str,
) -> None:
    """Per-tick observability: emit the ignition-gate aggregate scores + the
    open/suppress decision so the terminal shows what EVERY pressure tick
    evaluated — raw top contributor weight vs the pressure-only ceiling/margin,
    plus the advisory dopamine/serotonin tonic and the resulting gate decision.
    Failure-soft: telemetry must never break the cognition tick.
    """
    try:
        from guardian.terminal_feedback import emit_data

        pairs = sorted(
            ((c.type, _clamp_unit(c.weight)) for c in modulated if c.weight > 0.001),
            key=lambda kv: -kv[1],
        )
        top_key, top_w = pairs[0] if pairs else ("-", 0.0)
        second_w = pairs[1][1] if len(pairs) >= 2 else 0.0
        gap = top_w - second_w
        dop = float(nm.get("dopamine") or 0.0)
        ser = float(nm.get("serotonin") or 0.0)
        spike = float(nm.get("spike") or 0.0)
        decision = f"IGNITE:{dom.type}" if dom is not None else (suppress or "no_ignition")
        emit_data(
            "pressure",
            "ignition_gate",
            summary=(
                f"top={top_key}:{top_w:.3f} 2nd={second_w:.3f} gap={gap:.3f} "
                f"ceil={ceiling:.2f} marg={margin:.2f} "
                f"dop={dop:+.2f} ser={ser:.2f} spike={spike:+.2f} -> {decision}"
            ),
        )
        from guardian.pressure_observability import log_ignition_gate_detail

        log_ignition_gate_detail(
            modulated=modulated,
            neuromod=nm,
            ceiling=ceiling,
            margin=margin,
            dominant_type=dom.type if dom is not None else None,
            suppress_reason=suppress or "",
        )
    except Exception:
        pass


def evaluate(
    contributors: Sequence[Contributor],
    *,
    active_episode: bool,
    trajectory_snapshot: Optional[Dict[str, Any]] = None,
    open_threshold: Optional[float] = None,
    neuromod: Optional[Dict[str, float]] = None,
    shared_state: Optional[SharedState] = None,
    episode_store: Optional[Any] = None,
    force_dominant: Optional[Contributor] = None,
) -> Optional[EpisodeIgnition]:
    """Evaluate only the supplied contributors; admission belongs to the queue."""
    merged = list(contributors)
    if active_episode:
        return None

    # Idle-wake is NOT gated here (operator 2026-07-09). The idle TIMER lives in the
    # loop driver (pressure_loop.loop): it sleeps while the mind is idle and fires the
    # wake on idle expiry — evaluate no longer polls it every tick. So evaluate just
    # composes pressure and ignites when it clears threshold; operator input still wins
    # via supremacy below. Rest is enforced by the loop NOT calling evaluate while idle.
    if not merged:
        return None

    # Operator supremacy (operator directive 2026-06-12): the user's signal
    # ALWAYS wins the ignition list when present — no hybrid-gate margin and no
    # autonomous aggregate may block or out-rank it. Neuromod never controls
    # ignition for either operator or autonomous pressure.
    # Keyed on source: USER input only ("operator"); system-initiated asks
    # (operator_priority from Omega, source "omega") compete normally.
    if force_dominant is None:
        operator_inputs = [c for c in merged if c.source == "operator"]
        if operator_inputs:
            force_dominant = max(operator_inputs, key=lambda c: c.weight)
    if force_dominant is None:
        rollback_inputs = [c for c in merged if c.source == "system" and c.type == "dev_rollback"]
        if rollback_inputs:
            force_dominant = max(rollback_inputs, key=lambda c: c.weight)

    if neuromod is not None:
        nm = neuromod
        nm_source = "caller"
    else:
        try:
            from pressure_loop.pressure_field import read_neuromod_tonic

            tonic = read_neuromod_tonic()
            nm = {
                "dopamine": _safe_float(tonic.get("dopamine")),
                "serotonin": _safe_float(tonic.get("serotonin")),
                "stress": _safe_float(tonic.get("stress")),
                "spike": 0.0,
            }
            nm_source = "tonic"
        except Exception:
            nm = _neuromod_from_last_body(episode_store)
            nm_source = "last_closed_body"
    try:
        from guardian.pressure_observability import (
            log_evaluate_neuromod_source,
            log_evaluate_raw_contributors,
        )

        log_evaluate_raw_contributors(
            merged,
            force_dominant=force_dominant is not None,
            active_episode=active_episode,
        )
        log_evaluate_neuromod_source(nm_source, nm)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "neuromod_source_selected",
                summary=f"source={nm_source}",
                source=nm_source,
                dopamine=_safe_float(nm.get("dopamine")),
                serotonin=_safe_float(nm.get("serotonin")),
                stress=_safe_float(nm.get("stress")),
            )
        except Exception:
            pass
    except Exception:
        pass
    evaluated = list(merged)
    if force_dominant is not None:
        dom, evaluated = _apply_force_dominant(evaluated, force_dominant)
        aggregate = 1.0
    else:
        ceiling, margin = _hybrid_effective_gate_params(
            nm,
            ceiling_override=open_threshold,
        )
        dom, _suppress = _resolve_hybrid_contributor_winner(
            evaluated,
            ceiling=ceiling,
            margin=margin,
        )
        # Emit the gate scores EVERY autonomous tick — ignite or not — so the
        # terminal shows the aggregate the loop weighed and why it held.
        _emit_ignition_gate(evaluated, nm, ceiling, margin, dom, _suppress)
        if dom is None:
            return None
        aggregate = _clamp_unit(dom.weight)
    traj = trajectory_snapshot
    if traj is None and shared_state is not None:
        # Fail-hard: a corrupt prior trajectory must not silently become None —
        # it is the prior the mind diffs against to author the next trajectory.
        # (A MISSING slot returns the read default and yields None legitimately.)
        raw = shared_state.read(LAST_OMEGA_TRAJECTORY, None)
        traj = dict(raw) if isinstance(raw, dict) else None

    ignition = EpisodeIgnition(
        episode_id=str(uuid.uuid4()),
        aggregate_pressure=aggregate,
        opened_by=dom.type,
        dominant_contributor=dom,
        contributors=tuple(evaluated),
        trajectory_snapshot=traj,
        neuromod_state={
            key: _safe_float(nm.get(key)) for key in ("dopamine", "serotonin", "stress", "spike")
        },
    )
    # No ingress record to consume: the operator's turn lives only in the
    # durable line, and it walks through the gate when the episode carrier
    # opens (AGENTS.md §4.0). The mirror-zeroing this replaced was hygiene for
    # a slot that should never have existed (audit §1.13).
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "episode_ignition",
            episode_id=ignition.episode_id,
            summary=f"opened_by={ignition.opened_by} pressure={aggregate:.3f}",
            aggregate_pressure=aggregate,
            opened_by=ignition.opened_by,
            dominant_contributor={
                "source": dom.source,
                "type": dom.type,
                "weight": dom.weight,
            },
            contributors=[
                {
                    "source": c.source,
                    "type": c.type,
                    "weight": c.weight,
                    "metadata": dict(c.metadata),
                }
                for c in evaluated
            ],
            trajectory_snapshot=traj,
            neuromod_state=dict(ignition.neuromod_state),
            neuromod_role="advisory",
        )
    except Exception:
        pass
    return ignition
