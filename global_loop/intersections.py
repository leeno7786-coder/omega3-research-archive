"""Cross-lane mail: an addressed payload that enters cognition at ONE point.

Cross-lane cognitive integration spec, section 2. Mail is how a lane, the operator or
a reflection hands something to another part of the mind without any lane calling
another and without anything applying a payload twice:

    ready -> leased            a consumer claimed it, with a lease id and a deadline
    leased -> ready            the lease expired; a crash before reasoning loses nothing
    leased -> (deleted)        acknowledgement: the consuming step's outcome is durable,
                               so the row is removed from the store

Delivery is therefore at-least-once, never claim-on-read: marking an operator
instruction consumed before it had been reasoned over could lose it outright.

**Acknowledgement deletes the row.** There is no stored ``acknowledged`` status.
The durable "acknowledged" signal lives on the binding and transaction disposition.
**Acknowledged is not fulfilled.** Whether a requirement was HONOURED lives in the
receipt and causal record — a receipted decision, a verified artifact, a job-control
stop receipt. Nothing may read prompt injection as completion, and a consumer must
not acknowledge until its own outcome is durable.

**Effect identity is minted at PIN time.** ``effect_id`` does not derive from the
receiving episode, so a redelivery after a lease expiry — possibly to a different
episode on a different pipeline — resolves to the same effect. Replaying cognition is
legal; repeating an external action is not. The identity ENABLES that distinction; it
does not enforce it. A consumer must consult its own durable record of performed effects
before replaying one, and nothing in this module can verify that it does — the property
is unproven until consumer integration (T9).

**The host owns every identifier.** mail_id, effect_id, lease_id and arrival order are
host-generated. Arrival order comes from ``cognition_queue.next_arrival_seq`` — the one
global sequence, never a second counter that could disagree with it.

Four store-level rules earned by review (2026-09-16), each of which had a live defect:

1. **Copy-on-write publication.** Mutators build an ISOLATED candidate and publish it to
   the cache only after the write succeeds. Mutating the cache first meant a failed
   acknowledgement dropped rows in memory and the next successful write deleted them
   from disk, despite the acknowledgement having raised.
2. **Deep isolation both ways.** Incoming payloads are deep-copied on pin; every
   returned row is a deep snapshot. Shallow copies let a caller mutate a returned
   payload — adding a forbidden command — and a later claim persisted it.
3. **Whole-row validation on load.** A row with ``status="readi"`` once loaded happily
   and then simply never matched a claim, which is indistinguishable from an empty
   store. Malformed state is explicitly unavailable, and timestamps are parsed
   strictly: a stamp that cannot be read is corruption, not "now".
4. **Consumer leases enforced where they matter.** Claim returns an abandoned lease to
   ready, and acknowledgement refuses a lease whose deadline has passed. The message
   itself never expires. Correctness never depends on a caller remembering to sweep first.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

# Version 4 removes mail TTLs. Versions 1-3 migrate on load; retained old ``expired`` rows
# become ``ready`` because an admitted message cannot silently lose its delivery eligibility.
# Version 3 added the fold ``bindings``, host-minted id table, and replay horizon.
#
# REPLAY EVIDENCE IS NEVER SILENTLY LOST (review 2026-09-17). A binding evicted for space
# raises ``horizon_seq`` to its fold's birth order (the ignition contributor's global arrival
# sequence). A fold born at or before the horizon with no binding on record may be a replay
# whose evidence is gone, so it is REFUSED — it binds nothing — instead of being treated as
# fresh and handed newer mail. Minted ids are never evicted: they are forgotten only
# when their transaction closes. The active pin limit counts pending mail, not
# retained identities of acknowledged mail; those hashes still prevent late replay.
MAIL_VERSION = 4
_MAX_BINDINGS = 1024
_MAX_MINTED = 4096  # simultaneous pending host-minted pins


class MailOwnershipLost(ValueError):
    """The rows a consumer meant to act on are no longer its own."""
# Distinguishes THIS process from the one that recorded a binding. A fold's leases may be
# taken over by its replay only when the recording process is gone (a restart).
_PROCESS_EPOCH = ""

DELIVERY_POINTS = frozenset({"omega_reason", "lane_entry"})
REQUIREMENTS = frozenset({"required", "preferred", "optional"})
LANES = frozenset({"chat", "browser", "agent", "window"})
STATUSES = frozenset({"ready", "leased", "rejected"})

# A payload may name WHAT the work concerns — a repository-relative path, a url, a job
# id — because a continuation cannot be specified without naming its subject. It may
# never carry EXECUTABLE AUTHORITY: the lane chooses its own tools, and mail that could
# name a command would be one lane driving another's hands. Checked at EVERY depth: a
# top-level blacklist accepted {"execution": {"tool": ..., "argv": [...]}}.
_FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "tool", "tool_name", "tools", "command", "commands", "cmd", "argv", "args",
    "shell", "exec", "entrypoint", "script", "run", "invoke", "call", "subprocess",
})
_PAYLOAD_SCALARS = (str, int, float, bool, type(None))
_MAX_PAYLOAD_DEPTH = 6
_MAX_PAYLOAD_CHARS = 16000

# Every field a row must carry, with the type it must carry. The set is EXACT: an extra
# or missing key is corruption, not a forward-compatible variation.
_FLOAT_FIELDS = frozenset({"lease_until"})
# ``sent_seq`` is the operator's SEND order (its reserved arrival), or 0 for mail no operator
# sent. ``arrival_seq`` stays the store's own pin-time sequence, which the cutoff relies on.
_INT_FIELDS = frozenset({"generation", "arrival_seq", "sent_seq"})
_STR_FIELDS = frozenset({
    "mail_id", "effect_id", "thread_id", "delivery_point", "purpose", "requirement",
    "target_lane", "target_job_id", "continuation_key", "source_actor",
    "source_episode_id", "status", "lease_id", "claimed_by", "created_at",
})
# ``origin`` is host metadata about where the mail came from — for operator mail, the turn's
# ingress decision, citation and permission snapshot — so the words can re-enter cognition
# as an ordinary turn with their attribution intact. It is NEVER rendered into a prompt.
# ``offered_to`` is the durable record of every consumer the row was ever LEASED to for
# reasoning — a fold that could render it into a prompt — in order, most recent last. It is
# how a handoff tells which folds were offered a row. A handoff's own lease is not an offer
# to cognition and is never recorded here.
_ROW_FIELDS = _STR_FIELDS | _INT_FIELDS | _FLOAT_FIELDS | {"payload", "provenance", "origin",
                                                           "offered_to"}
# Exact historical version-1 row shape, including its removed ttl_s field.
_V1_ROW_FIELDS = (_STR_FIELDS | {"generation", "arrival_seq", "lease_until", "ttl_s",
                                "payload", "provenance"})
_PRE_V4_ROW_FIELDS = _ROW_FIELDS | {"ttl_s"}
_MAX_OFFERS = 16
# A version-1 row that had been leased and returned names no consumer any more.
UNRECORDED_OFFER = "(offered before offers were recorded)"

_LOCK = threading.RLock()
_CACHE: Optional[dict[str, Any]] = None


def mail_path() -> Path:
    override = os.environ.get("OMEGA_CROSS_LANE_MAIL_PATH", "").strip()
    if override:
        return Path(override)
    from core.paths import GUARDIAN_STATE_ROOT

    return GUARDIAN_STATE_ROOT / "cross_lane_mail.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_epoch(stamp: Any) -> float:
    """Parse a stamp STRICTLY. An unreadable stamp is corruption, never "now".

    Substituting the current time made a row with a broken ``created_at``
    perpetually fresh, so it could never expire.
    """
    try:
        return datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unreadable cross-lane mail timestamp {stamp!r}") from exc


# ── payload ───────────────────────────────────────────────────────────────────

def _validate_payload(payload: Any, *, depth: int = 0, path: str = "payload") -> Any:
    """Recursively check the permitted payload structure and return a deep copy."""
    if depth > _MAX_PAYLOAD_DEPTH:
        raise ValueError(f"mail {path} nests deeper than {_MAX_PAYLOAD_DEPTH}")
    if isinstance(payload, Mapping):
        out: dict[str, Any] = {}
        for raw_key, value in payload.items():
            if not isinstance(raw_key, str):
                raise ValueError(f"mail {path} keys must be strings")
            if raw_key.lower() in _FORBIDDEN_PAYLOAD_KEYS:
                raise ValueError(
                    f"mail {path}.{raw_key} may not carry executable authority")
            out[raw_key] = _validate_payload(value, depth=depth + 1, path=f"{path}.{raw_key}")
        return out
    if isinstance(payload, (list, tuple)):
        return [_validate_payload(item, depth=depth + 1, path=f"{path}[]") for item in payload]
    if isinstance(payload, _PAYLOAD_SCALARS):
        # NaN and Infinity are not JSON. The loader used to accept them while the writer
        # refused them with allow_nan=False, so a persisted nested NaN read as AVAILABLE
        # mail and then failed the next claim mid-persist (review 2026-09-16) — the exact
        # inverse of "a corrupt store is explicitly unavailable". The writer safeguard
        # stays as defence in depth.
        if isinstance(payload, float) and not math.isfinite(payload):
            raise ValueError(f"mail {path} must be finite")
        return payload
    raise ValueError(f"mail {path} holds an unsupported type {type(payload).__name__}")


def payload_fits(payload: Any) -> bool:
    """Whether a valid payload fits durable mail; invalid structure always raises.

    Renderers have their own prompt budgets. Producers that select prompt evidence
    must also honor this storage bound without discarding schema failures as size.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("mail payload must be an object")
    return _serialized_size(_validate_payload(payload)) <= _MAX_PAYLOAD_CHARS


def _payload_for_pin(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("mail payload must be an object")
    checked = _validate_payload(payload)
    if not payload_fits(checked):
        raise ValueError(f"mail payload exceeds {_MAX_PAYLOAD_CHARS} characters")
    return checked


def _origin_for_pin(origin: Any) -> dict[str, Any]:
    """Origin follows the payload's structural contract: JSON only, bounded, no authority."""
    if origin is None:
        return {}
    if not isinstance(origin, Mapping):
        raise ValueError("mail origin must be an object")
    checked = _validate_payload(origin, path="origin")
    if _serialized_size(checked) > _MAX_PAYLOAD_CHARS:
        raise ValueError(f"mail origin exceeds {_MAX_PAYLOAD_CHARS} characters")
    return checked


def _serialized_size(payload: Any) -> int:
    """The SERIALIZED payload cost. Not a rendered-prompt bound: whoever renders mail
    into a slot owns its framing and provenance overhead (spec section 3)."""
    return len(json.dumps(payload or {}, ensure_ascii=False, sort_keys=True))


# ── durable store ─────────────────────────────────────────────────────────────

def _validate_row(mail_id: Any, row: Any) -> dict[str, Any]:
    """Validate one whole row and its identity relationships; return a deep copy."""
    if not isinstance(row, Mapping):
        raise ValueError("invalid cross-lane mail row")
    if set(row) != _ROW_FIELDS:
        missing = sorted(_ROW_FIELDS - set(row))
        extra = sorted(set(row) - _ROW_FIELDS)
        raise ValueError(f"invalid cross-lane mail row fields (missing={missing}, extra={extra})")
    for field in _STR_FIELDS:
        if not isinstance(row[field], str):
            raise ValueError(f"cross-lane mail {field} must be a string")
    for field in _INT_FIELDS:
        if type(row[field]) is not int or row[field] < 0:
            raise ValueError(f"cross-lane mail {field} must be a non-negative integer")
    for field in _FLOAT_FIELDS:
        _require_finite(row[field], f"cross-lane mail {field}")
    if row["delivery_point"] not in DELIVERY_POINTS:
        raise ValueError(f"unknown delivery_point {row['delivery_point']!r}")
    if not row["purpose"] or len(row["purpose"]) > 128:
        raise ValueError("cross-lane mail purpose must be non-empty descriptive text")
    if row["requirement"] not in REQUIREMENTS:
        raise ValueError(f"unknown mail requirement {row['requirement']!r}")
    if not row["source_actor"] or len(row["source_actor"]) > 64:
        raise ValueError("cross-lane mail source_actor must be non-empty text")
    if row["target_lane"] and row["target_lane"] not in LANES:
        raise ValueError(f"unknown target lane {row['target_lane']!r}")
    if row["status"] not in STATUSES:
        raise ValueError(f"unknown mail status {row['status']!r}")
    if not row["mail_id"] or not row["effect_id"] or not row["thread_id"]:
        raise ValueError("cross-lane mail requires mail_id, effect_id and thread_id")
    if str(mail_id) != row["mail_id"]:
        raise ValueError("cross-lane mail row is filed under the wrong id")
    offered = row["offered_to"]
    if (not isinstance(offered, list) or len(offered) > _MAX_OFFERS
            or any(not isinstance(one, str) or not one for one in offered)):
        raise ValueError("cross-lane mail offered_to must be a bounded list of consumers")
    if not isinstance(row["provenance"], list) or any(
            not isinstance(ref, str) for ref in row["provenance"]):
        raise ValueError("cross-lane mail provenance must be a list of strings")
    # A lease is a relationship, not a flag: leased rows name their holder, and rows
    # that are not leased hold no lease at all.
    leased = row["status"] == "leased"
    if leased and not (row["lease_id"] and row["claimed_by"]):
        raise ValueError("leased cross-lane mail must name its lease and holder")
    if not leased and (row["lease_id"] or row["claimed_by"] or row["lease_until"]):
        raise ValueError("unleased cross-lane mail must hold no lease")
    _as_epoch(row["created_at"])
    checked = dict(row)
    # The SAME contract as pinning, including root type and size: reload used only the
    # recursive key check, so a persisted payload replaced by a string — or an oversized
    # one — loaded happily (review 2026-09-16).
    checked["payload"] = _payload_for_pin(row["payload"])
    checked["origin"] = _origin_for_pin(row["origin"])
    checked["offered_to"] = list(row["offered_to"])
    checked["provenance"] = list(row["provenance"])
    checked["lease_until"] = float(row["lease_until"])
    return checked


def _read() -> dict[str, Any]:
    """Load the store. Absent is normal; CORRUPT is explicitly unavailable.

    A store that cannot be parsed — or whose ROWS cannot — must never read as "no
    mail": that would silently drop an operator instruction and look identical to a
    quiet system.
    """
    path = mail_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": MAIL_VERSION, "mail": {}, "bindings": {}, "minted": {}, "horizon_seq": 0}
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("cross-lane mail is unreadable") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("mail"), dict):
        raise ValueError("invalid cross-lane mail root")
    version = payload.get("version")
    if version == 1:
        return {"version": MAIL_VERSION, "bindings": {}, "minted": {}, "horizon_seq": 0,
                "mail": {str(key): _validate_row(key, _migrate_v1(key, row))
                         for key, row in payload["mail"].items()}}
    if version == 2:
        # Version 2 rows have the v3 row schema but no bindings or minted ids.
        if set(payload) != {"version", "mail"}:
            raise ValueError("invalid version-2 cross-lane mail root")
        return {"version": MAIL_VERSION, "bindings": {}, "minted": {}, "horizon_seq": 0,
                "mail": {str(key): _validate_row(key, _migrate_v3_row(key, row))
                         for key, row in payload["mail"].items()}}
    if version == 3:
        if set(payload) != {"version", "mail", "bindings", "minted", "horizon_seq"}:
            raise ValueError("invalid version-3 cross-lane mail root sections")
        if type(payload["horizon_seq"]) is not int or payload["horizon_seq"] < 0:
            raise ValueError("invalid cross-lane mail replay horizon")
        return {"version": MAIL_VERSION, "horizon_seq": payload["horizon_seq"],
                "mail": {str(key): _validate_row(key, _migrate_v3_row(key, row))
                         for key, row in payload["mail"].items()},
                "bindings": _validate_bindings(payload["bindings"]),
                "minted": _validate_minted(payload["minted"])}
    if version != MAIL_VERSION:
        raise ValueError("unsupported cross-lane mail version")
    if set(payload) != {"version", "mail", "bindings", "minted", "horizon_seq"}:
        raise ValueError("invalid cross-lane mail root sections")
    if type(payload["horizon_seq"]) is not int or payload["horizon_seq"] < 0:
        raise ValueError("invalid cross-lane mail replay horizon")
    return {"version": MAIL_VERSION, "horizon_seq": payload["horizon_seq"],
            "mail": {str(key): _validate_row(key, row) for key, row in payload["mail"].items()},
            "bindings": _validate_bindings(payload["bindings"]),
            "minted": _validate_minted(payload["minted"])}


_BINDING_FIELDS = frozenset({"fold_id", "episode_id", "process_epoch", "thread_id", "origin_seq",
                             "cutoff_seq", "mail_ids", "state", "bound_at", "manifest"})


def _valid_derivation(derivation: Any) -> bool:
    """``{episode_id, recall[], mail[]}`` — what the fold's conclusions rest on (T10)."""
    return (isinstance(derivation, dict) and set(derivation) == {"episode_id", "recall", "mail"}
            and isinstance(derivation["episode_id"], str)
            and all(isinstance(derivation[key], list)
                    and all(isinstance(one, str) for one in derivation[key])
                    for key in ("recall", "mail")))


def _valid_manifest(manifest: Any) -> bool:
    """A recorded manifest is the fold's WHOLE serialized result — and, since T10, the
    host-known derivation frozen beside it. A manifest written before T10 has none, and
    still loads: no stored row changes shape, so the store version does not move."""
    return (isinstance(manifest, dict) and isinstance(manifest.get("result"), dict)
            and set(manifest) in ({"result"}, {"result", "derivation"})
            and ("derivation" not in manifest or _valid_derivation(manifest["derivation"])))


def _validate_bindings(bindings: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(bindings, dict):
        raise ValueError("cross-lane mail bindings must be an object")
    checked: dict[str, dict[str, Any]] = {}
    for fold_id, row in bindings.items():
        if (not isinstance(row, dict) or set(row) != _BINDING_FIELDS
                or row["fold_id"] != fold_id or not fold_id
                or any(not isinstance(row[key], str) for key in
                       ("fold_id", "episode_id", "process_epoch", "thread_id", "state", "bound_at"))
                or row["state"] not in {"bound", "settled"}
                or type(row["cutoff_seq"]) is not int or row["cutoff_seq"] < 0
                or type(row["origin_seq"]) is not int or row["origin_seq"] < 0
                # An EMPTY set is a binding too: the fold decided to reason over nothing.
                or not isinstance(row["mail_ids"], list)
                # None = never recorded. A recorded manifest is the fold's WHOLE serialized
                # result — statements (possibly none), injections, control and everything else.
                or not (row["manifest"] is None or _valid_manifest(row["manifest"]))
                or any(not isinstance(one, str) or not one for one in row["mail_ids"])):
            raise ValueError(f"invalid cross-lane mail binding {fold_id!r}")
        checked[str(fold_id)] = copy.deepcopy(row)
    return checked


def _validate_minted(minted: Any) -> dict[str, str]:
    if not isinstance(minted, dict) or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value
            for key, value in minted.items()):
        raise ValueError("cross-lane mail minted ids must map mail ids to effect ids")
    return dict(minted)


def _migrate_v1(mail_id: Any, row: Any) -> dict[str, Any]:
    """One version-1 row as a version-2 row. Explicit, and exact about what it accepts.

    A version-1 row carries EXACTLY its historical fields; anything else is corruption.
    It loses ``ttl_s`` and gains ``sent_seq = 0`` — the store never recorded who sent it, so it
    claims no send order — and an empty ``origin``. Every identity is carried over
    untouched: mail_id, effect_id, arrival_seq, status, and a lease's id, holder,
    generation and deadline, so a consumer holding a version-1 receipt can still
    acknowledge or release it. The migrated store is written as version 2 by the next
    mutation; until then a restart simply migrates again.
    """
    if not isinstance(row, Mapping) or set(row) != _V1_ROW_FIELDS:
        found = sorted(row) if isinstance(row, Mapping) else type(row).__name__
        raise ValueError(f"invalid version-1 cross-lane mail row {mail_id!r}: {found}")
    offered: list[str] = []
    if row.get("status") == "leased" and row.get("claimed_by"):
        offered = [str(row["claimed_by"])]
    elif int(row.get("generation") or 0) > 0:
        # Leased once and since returned: a consumer may have reasoned over it, and version 1
        # did not say which. Claiming "never offered" would be a guess.
        offered = [UNRECORDED_OFFER]
    migrated = copy.deepcopy(dict(row))
    migrated.pop("ttl_s", None)
    if migrated.get("status") == "expired":
        migrated.update(status="ready", generation=int(migrated["generation"]) + 1)
    return {**migrated, "sent_seq": 0, "origin": {}, "offered_to": offered}


def _migrate_v3_row(mail_id: Any, row: Any) -> dict[str, Any]:
    """Remove the historical TTL field and restore rows previously marked expired."""
    if not isinstance(row, Mapping) or set(row) != _PRE_V4_ROW_FIELDS:
        found = sorted(row) if isinstance(row, Mapping) else type(row).__name__
        raise ValueError(f"invalid pre-v4 cross-lane mail row {mail_id!r}: {found}")
    migrated = copy.deepcopy(dict(row))
    migrated.pop("ttl_s", None)
    if migrated.get("status") == "expired":
        migrated.update(status="ready", generation=int(migrated["generation"]) + 1)
    return migrated


def _write(state: Mapping[str, Any]) -> None:
    path = mail_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            # allow_nan=False: NaN/Infinity are not JSON, and a non-finite deadline
            # written here would reload as permanently unexpirable mail.
            json.dump(dict(state), handle, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False)
            handle.write("\n")
            handle.flush()
            # Mail carries operator instructions, so it follows the successor ledger's
            # durability rather than the lighter workflow-store convention.
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _snapshot() -> dict[str, Any]:
    """The cached store. Callers must not mutate it — use :func:`_candidate`."""
    global _CACHE
    if _CACHE is None:
        _CACHE = _read()
    return _CACHE


def _candidate() -> dict[str, Any]:
    """An isolated copy to mutate, so a failed write cannot leave a dirty cache."""
    return copy.deepcopy(_snapshot())


def _publish(state: dict[str, Any]) -> None:
    """Persist first, then adopt; reload disk after an uncertain write outcome."""
    global _CACHE
    try:
        _write(state)
    except BaseException:
        # Replacement can have committed before the write reports a failure.
        # Reusing the old cache would let the next pin erase that durable mail.
        _CACHE = None
        raise
    _CACHE = state


def _out(row: Mapping[str, Any]) -> dict[str, Any]:
    """A deep snapshot for a caller. Nothing it does can reach back into the store."""
    return copy.deepcopy(dict(row))


def _require_finite(value: Any, name: str) -> float:
    """Reject NaN and infinity.

    A non-finite deadline compares false against everything, so it never expires and
    never releases: the mail becomes permanently unclaimable by anyone else, and it
    survives a reload because NaN round-trips through JSON (review 2026-09-16).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _bounded(value: Any, name: str, limit: int) -> str:
    text = str(value or "")
    if len(text) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return text


def _rank(row: Mapping[str, Any]) -> tuple[int, int]:
    """One FIFO order: reserved operator arrival when present, otherwise pin arrival."""
    arrival = int(row.get("arrival_seq") or 0)
    return int(row.get("sent_seq") or 0) or arrival, arrival


def mail_handoff_identity(mail_id: str) -> str:
    """The queue identity under which a row's words are handed off as an ordinary turn."""
    return hashlib.sha256(f"cross-lane-mail-handoff:{mail_id}".encode("utf-8")).hexdigest()


def handoff_holder(mail_id: str) -> str:
    """The only consumer that may take a row whose words were already handed off."""
    return f"handoff:{mail_id}"


def _committed_to_handoff(row: Mapping[str, Any]) -> bool:
    """The row's words were durably admitted as an ordinary turn and not yet retired here.

    THE ROUTING BOUNDARY (review 2026-09-16). Once an ownership-protected handoff admits
    the words, that ordinary turn owns them: no later fold may reclaim them — not after a
    failed acknowledgement returned the row to ready, not after a crash let the handoff
    lease lapse, not when a new arrival reopens the job. Only the handoff holder may take
    the row, and only to finish retiring it. The queue's receipt, published atomically with
    the admitted turn, is the durable evidence.
    """
    if row.get("purpose") != "operator_instruction":
        return False
    from pressure_loop.cognition_queue import handoff_accepted

    return handoff_accepted(mail_handoff_identity(str(row["mail_id"])))


def _awaits_specialist_admission(row: Mapping[str, Any]) -> bool:
    """Specialist mail becomes visible only after its durable FIFO admission.

    The mail's origin carries the frozen queue input for retry/boot recovery.
    Keeping it unclaimable until the queue receipt exists closes the interval
    between the two writes, including an interrupted or failed admission.
    """
    if row.get("purpose") != "specialist_lane_handoff":
        return False
    from pressure_loop.cognition_queue import handoff_accepted

    identity = row["payload"].get("handoff_id")
    if not isinstance(identity, str) or not identity:
        raise ValueError("specialist_lane_mail_handoff_identity_missing")
    return not handoff_accepted(identity)


def _awaits_earlier_operator_arrivals(row: Mapping[str, Any]) -> bool:
    """Operator mail is not deliverable while words the operator sent BEFORE it are still in intake.

    Screening runs concurrently, so an earlier message can still be on its way to the store
    when a later one is pinned; a fold claiming now would reason over the later words
    first. Such mail is not refused — it is simply not a candidate yet.

    It deliberately does NOT wait on the interpretation ledger job revisions consult: only
    the agent tool loop settles that ledger, so after one image or learn turn every later
    instruction would be held back until a reboot while ingress had already reported it
    passed (review-prep 2026-09-16).
    """
    sent = int(row.get("sent_seq") or 0)
    if not sent:
        return False
    from pressure_loop.cognition_queue import earlier_operator_arrivals_admitted

    return not earlier_operator_arrivals_admitted(sent)


def _emit_degraded(summary: str, **fields: Any) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("global", "cross_lane_mail", status="degraded", ok=False,
                  summary=summary[:200], **fields)
    except Exception:  # noqa: BLE001 - telemetry never fails cognition
        pass


# ── sweeps ────────────────────────────────────────────────────────────────────

def _release_expired(state: dict[str, Any], now: float) -> int:
    released = 0
    for row in state["mail"].values():
        if row["status"] == "leased" and float(row["lease_until"]) <= now:
            row.update(status="ready", lease_id="", lease_until=0.0, claimed_by="",
                       generation=int(row["generation"]) + 1)
            released += 1
    return released


def _sweep(state: dict[str, Any], now: float) -> int:
    """Only an abandoned consumer lease lapses; admitted mail remains durable."""
    return _release_expired(state, now)


# ── public surface ────────────────────────────────────────────────────────────

def pin_mail(
    *,
    thread_id: str,
    delivery_point: str,
    purpose: str,
    requirement: str,
    source_actor: str,
    source_episode_id: str,
    payload: Mapping[str, Any],
    target_lane: str = "",
    target_job_id: str = "",
    continuation_key: str = "",
    provenance: Iterable[str] = (),
    sent_seq: int = 0,
    origin: Optional[Mapping[str, Any]] = None,
    mail_id: str = "",
    effect_id: str = "",
) -> Optional[dict[str, Any]]:
    """Pin one addressed payload. The host mints its identities here, once.

    ``mail_id``/``effect_id`` let the HOST mint both identities beforehand — a cognitive
    transaction names its outgoing mail in its durable record before pinning it (T8). Such a
    pin is idempotent for good: the pair is remembered in the same publication as the row, so
    pinning it again returns the live row, or ``None`` once that row has been consumed —
    never a second copy. A different effect under a minted id is a conflict, not a replay.

    ``sent_seq`` is the operator's reserved arrival for mail an operator sent (0 otherwise).
    It orders delivery and holds the mail back behind earlier operator words; it never
    replaces ``arrival_seq``, which is allocated here so the reconciliation cutoff holds.
    ``origin`` is unrendered host metadata about the source (see ``_ROW_FIELDS``).
    """
    checked_payload, checked_origin = check_pin(
        thread_id=thread_id, delivery_point=delivery_point, purpose=purpose,
        requirement=requirement, source_actor=source_actor, source_episode_id=source_episode_id,
        payload=payload, target_lane=target_lane, target_job_id=target_job_id,
        continuation_key=continuation_key, provenance=provenance, sent_seq=sent_seq,
        origin=origin, mail_id=mail_id, effect_id=effect_id)

    with _LOCK:
        if mail_id:
            known = _snapshot()["minted"].get(mail_id)
            if known is not None:
                if known != effect_id:
                    raise ValueError(f"mail {mail_id!r} was minted for another effect")
                live = _snapshot()["mail"].get(mail_id)
                return _out(live) if live is not None else None
            if sum(identity in _snapshot()["minted"] for identity in _snapshot()["mail"]) >= _MAX_MINTED:
                # Completed identities remain replay evidence, without blocking
                # future work. No identity is evicted to admit another message.
                raise ValueError("pending minted mail capacity reached; acknowledge mail first")
        row = {
            "mail_id": mail_id or uuid.uuid4().hex[:16],
            # Minted HERE, so redelivery to another episode resolves the same effect.
            "effect_id": effect_id or uuid.uuid4().hex,
            "generation": 0,
            "arrival_seq": _next_arrival_seq(),
            "sent_seq": sent_seq,
            "thread_id": _bounded(thread_id, "thread_id", 128),
            "delivery_point": delivery_point,
            "purpose": purpose,
            "requirement": requirement,
            "target_lane": _bounded(target_lane, "target_lane", 32),
            "target_job_id": _bounded(target_job_id, "target_job_id", 128),
            "continuation_key": _bounded(continuation_key, "continuation_key", 256),
            "source_actor": source_actor,
            "source_episode_id": _bounded(source_episode_id, "source_episode_id", 128),
            "payload": checked_payload,
            "origin": checked_origin,
            "offered_to": [],
            "provenance": [str(ref) for ref in provenance or ()],
            "status": "ready",
            "lease_id": "",
            "lease_until": 0.0,
            "claimed_by": "",
            "created_at": _now_iso(),
        }
        state = _candidate()
        if row["mail_id"] in state["mail"]:
            raise ValueError(f"mail {row['mail_id']!r} already exists")
        state["mail"][row["mail_id"]] = _validate_row(row["mail_id"], row)
        if mail_id:
            state["minted"][mail_id] = effect_id
        _publish(state)
        return _out(row)


def check_pin(*, thread_id, delivery_point, purpose, requirement, source_actor,
              source_episode_id, payload, target_lane="", target_job_id="", continuation_key="",
              provenance=(), sent_seq=0, origin=None, mail_id="",
              effect_id=""):
    """Everything :func:`pin_mail` would refuse, WITHOUT pinning. Returns the checked
    ``(payload, origin)``.

    A cognitive transaction validates its complete outgoing mail with this BEFORE it opens
    its durable record: a record naming mail the store will always refuse could never be
    published, and boot recovery would fail on it forever (review 2026-09-17).
    """
    if not str(thread_id or "").strip():
        raise ValueError("mail requires a thread_id")
    if delivery_point not in DELIVERY_POINTS:
        raise ValueError(f"unknown delivery_point {delivery_point!r}")
    if type(purpose) is not str or not purpose.strip() or len(purpose) > 128:
        raise ValueError("mail purpose must be descriptive text of at most 128 characters")
    if requirement not in REQUIREMENTS:
        raise ValueError(f"unknown mail requirement {requirement!r}")
    if type(source_actor) is not str or not source_actor.strip() or len(source_actor) > 64:
        raise ValueError("mail source_actor must be text of at most 64 characters")
    checked_payload = _payload_for_pin(payload)
    checked_origin = _origin_for_pin(origin)
    if type(sent_seq) is not int or sent_seq < 0:
        raise ValueError("sent_seq must be a non-negative integer")
    if bool(mail_id) != bool(effect_id):
        raise ValueError("a host-minted pin names both its mail_id and its effect_id")
    for value, name, limit in ((thread_id, "thread_id", 128), (target_lane, "target_lane", 32),
                               (target_job_id, "target_job_id", 128),
                               (continuation_key, "continuation_key", 256),
                               (source_episode_id, "source_episode_id", 128),
                               (mail_id, "mail_id", 64), (effect_id, "effect_id", 64)):
        _bounded(value, name, limit)
    if any(not isinstance(ref, str) for ref in provenance or ()):
        raise ValueError("cross-lane mail provenance must be a list of strings")
    if target_lane and target_lane not in LANES:
        raise ValueError(f"unknown target lane {target_lane!r}")
    return checked_payload, checked_origin


def mail_was_acknowledged(*, mail_id: str, effect_id: str) -> bool:
    """Read retained effect identity without re-pinning or recalling current work."""
    with _LOCK:
        state = _snapshot()
        known = state["minted"].get(mail_id)
        if known is not None and known != effect_id:
            raise ValueError(f"mail {mail_id!r} was minted for another effect")
        return known is not None and mail_id not in state["mail"]


def forget_minted(mail_ids: Iterable[str]) -> int:
    """Forget minted ids whose transaction has CLOSED: published for good, never pinned again."""
    wanted = {str(one) for one in mail_ids or ()}
    with _LOCK:
        present = wanted & set(_snapshot()["minted"])
        if not present:
            return 0
        state = _candidate()
        for mail_id in present:
            del state["minted"][mail_id]
        _publish(state)
        return len(present)


def _next_arrival_seq() -> int:
    from pressure_loop.cognition_queue import next_arrival_seq

    return next_arrival_seq()


def _addressed(row: Mapping[str, Any], *, delivery_point: str, lane: str,
               thread_id: str, job_id: str, ready_only: bool = True,
               allow_unaddressed: bool = True) -> bool:
    if ready_only and row.get("status") != "ready":
        return False
    if row.get("delivery_point") != delivery_point:
        return False
    if str(row.get("thread_id") or "") != str(thread_id or ""):
        return False
    target_lane = str(row.get("target_lane") or "")
    if target_lane and target_lane != str(lane or ""):
        return False
    if not target_lane and not allow_unaddressed:
        return False
    target_job = str(row.get("target_job_id") or "")
    if target_job and target_job != str(job_id or ""):
        return False
    return True


def claim_mail_detailed(
    *,
    delivery_point: str,
    lane: str,
    thread_id: str,
    episode_id: str,
    lease_s: float,
    job_id: str = "",
    mail_ids: Optional[Iterable[str]] = None,
    payload_budget: Optional[int] = None,
    cutoff_seq: Optional[int] = None,
    max_rows: Optional[int] = None,
    admit: Optional[Callable[[list[dict[str, Any]]], bool]] = None,
    fold_id: str = "",
    origin_seq: int = 0,
    continuation_key: Optional[str] = None,
    allow_unaddressed: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Lease the mail addressed to this consumer, in deterministic class order.

    ``continuation_key`` (lane entry, T9) names the continuation the consumer is executing:
    a row bound to ANOTHER continuation is refused, never leased. ``None`` means the
    consumer states no continuation (``omega_reason`` claims).

    ``fold_id`` records the claimed set as that fold's BINDING in the same publication as
    the leases (see :func:`claim_for_fold`): a binding written afterwards could be lost to a
    crash that kept the leases, and the replay could then never name the identical set.

    Mail remains eligible until acknowledged or explicitly rejected. The temporary
    consumer lease is enforced here: an abandoned lease returns to ready before selection.

    ``mail_ids`` restricts the claim to exactly those ids — how a lane-entry consumer
    takes the mail its own continuation names rather than whatever is waiting.

    ``payload_budget`` bounds the SERIALIZED payload characters. Nothing is ever
    truncated: mail that does not fit is LEFT READY for a later episode, and a
    ``required`` payload that does not fit is additionally reported as degraded. The
    renderer owns its own framing and provenance overhead, and must surface a required
    omission to prerequisite enforcement rather than proceeding without it.

    ``max_rows`` and ``admit`` decide membership DURING selection, never after it. A
    consumer that lets more through than it will render, and then bounds or truncates
    the result, acknowledges mail its prompt never showed (review 2026-09-16). ``admit``
    receives the would-be accepted set as isolated snapshots and answers whether it still
    fits; refused mail is left ready, and a refused ``required`` message is reported.

    Returns ``(claimed, refused)``. ``refused`` names every addressed message refused at
    selection, with its reason, decided under the SAME lock as the claim. A consumer must
    record omissions from it, never by re-reading the store afterwards: between those two
    reads another consumer can lease a refused required message, and the omission then
    silently disappears (review 2026-09-16).

    Each returned row carries its OWN ``lease_id`` and ``generation``; pass the rows
    back to :func:`acknowledge_mail` as receipts.
    """
    if not str(episode_id or "").strip():
        raise ValueError("a claim requires the claiming episode")
    _require_finite(lease_s, "lease_s")
    wanted = {str(one) for one in mail_ids} if mail_ids is not None else None
    with _LOCK:
        state = _candidate()
        # Sampled INSIDE the lock, after the candidate load. A timestamp taken before
        # the wait goes stale while contending, and a lease that expired during the wait
        # could otherwise be claimed twice (review 2026-09-16).
        now = time.time()
        changed = _sweep(state, now)

        # FIFO is per addressed lane/thread. A predecessor holds later messages back when
        # it is leased elsewhere, past the fold cutoff, still arriving through intake, or
        # refused by this prompt's capacity. Purpose and requirement never create priority.
        held_back: tuple[int, int] | None = None

        def hold_back(row: Mapping[str, Any]) -> None:
            nonlocal held_back
            key = _rank(row)
            if held_back is None or key < held_back:
                held_back = key

        candidates = []
        lane_refused: list[dict[str, Any]] = []
        for row in state["mail"].values():
            if not _addressed(row, delivery_point=delivery_point, lane=lane,
                              thread_id=thread_id, job_id=job_id, ready_only=False,
                              allow_unaddressed=allow_unaddressed):
                continue
            if row["status"] == "leased":
                hold_back(row)
                continue
            if row["status"] != "ready":
                continue
            if _awaits_specialist_admission(row):
                hold_back(row)
                continue
            if (str(episode_id) != handoff_holder(str(row["mail_id"]))
                    and _committed_to_handoff(row)):
                # Already delivered as an ordinary turn: never this consumer's to reason
                # over, and later words wait until the retirement completes.
                hold_back(row)
                continue
            if wanted is not None and row["mail_id"] not in wanted:
                continue
            entry_refusal = ""
            if (continuation_key is not None
                    and str(row["continuation_key"]) != str(continuation_key)):
                entry_refusal = "it is bound to another continuation"
            if entry_refusal:
                lane_refused.append({"mail_id": row["mail_id"], "requirement": row["requirement"],
                                     "purpose": row["purpose"], "reason": entry_refusal})
                continue
            # The cutoff decides reconciliation membership once: mail pinned after it
            # belongs to a later episode even while this fold is still running.
            if ((cutoff_seq is not None and int(row["arrival_seq"]) > int(cutoff_seq))
                    or _awaits_earlier_operator_arrivals(row)):
                hold_back(row)
                continue
            candidates.append(row)
        candidates.sort(key=_rank)

        claimed: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = list(lane_refused)
        spent = 0
        for row in candidates:
            if held_back is not None and _rank(row) > held_back:
                # Not refused: an earlier message is not in this claim, so later mail waits.
                continue
            cost = _serialized_size(row["payload"])
            refusal = ""
            if max_rows is not None and len(claimed) >= int(max_rows):
                refusal = "the slot's row limit"
            elif payload_budget is not None and spent + cost > int(payload_budget):
                refusal = "the payload budget"
            # The WHOLE proposed set is deep-copied: earlier accepted rows are the very dicts
            # returned to the caller, and passing them by reference let a callback rewrite
            # a returned payload and invalidate its lease receipt (review 2026-09-16).
            elif admit is not None and not admit(copy.deepcopy(claimed) + [_out(row)]):
                refusal = "the consumer's admission budget"
            if refusal:
                # Left READY and never truncated: a consumer only ever renders what was
                # admitted here, so nothing it acknowledges can be missing from its prompt.
                if row["requirement"] == "required":
                    _emit_degraded(
                        f"required mail did not fit {refusal}; left ready",
                        mail_id=row["mail_id"], thread_id=row["thread_id"],
                        purpose=row["purpose"])
                refused.append({"mail_id": row["mail_id"], "requirement": row["requirement"],
                                "purpose": row["purpose"], "reason": refusal})
                hold_back(row)
                continue
            spent += cost
            row.update(status="leased", lease_id=uuid.uuid4().hex[:16],
                       lease_until=now + float(lease_s), claimed_by=str(episode_id),
                       generation=int(row["generation"]) + 1)
            if str(episode_id) != handoff_holder(str(row["mail_id"])):
                # Offered to cognition, in the same publication as the lease.
                offers = [one for one in row["offered_to"] if one != str(episode_id)]
                row["offered_to"] = (offers + [str(episode_id)])[-_MAX_OFFERS:]
            claimed.append(_out(row))
        if fold_id:
            # Recorded even when EMPTY: "this fold reasons over nothing" is a membership
            # decision too, and a replay that claimed afresh would consume mail pinned after
            # the fold's cutoff (review 2026-09-17).
            state["bindings"][fold_id] = {
                "fold_id": fold_id, "episode_id": str(episode_id),
                "process_epoch": process_epoch(), "thread_id": str(thread_id),
                "origin_seq": int(origin_seq or 0), "cutoff_seq": int(cutoff_seq or 0),
                "mail_ids": [row["mail_id"] for row in claimed],
                "state": "bound", "bound_at": _now_iso(), "manifest": None}
            _prune_bindings(state)
        if claimed or changed or fold_id:
            _publish(state)
        return claimed, refused


def process_epoch() -> str:
    """This process's identity, minted once per process (and per simulated restart)."""
    global _PROCESS_EPOCH
    with _LOCK:
        if not _PROCESS_EPOCH:
            _PROCESS_EPOCH = uuid.uuid4().hex
        return _PROCESS_EPOCH


def _prune_bindings(state: dict[str, Any]) -> None:
    """Bounded — and every eviction RAISES THE REPLAY HORIZON to the evicted fold's birth
    order, so the lost evidence turns into an explicit refusal rather than a silent gap.
    Settled bindings go first, oldest fold first; then the oldest of any state."""
    bindings = state["bindings"]
    for wanted in ("settled", "bound"):
        for fold_id in sorted((key for key, row in bindings.items() if row["state"] == wanted),
                              key=lambda key: (bindings[key]["origin_seq"], bindings[key]["bound_at"])):
            if len(bindings) <= _MAX_BINDINGS:
                return
            state["horizon_seq"] = max(int(state["horizon_seq"]), int(bindings[fold_id]["origin_seq"]))
            del bindings[fold_id]


_MAX_MANIFEST_CHARS = 200_000


def record_fold_manifest(fold_id: str, manifest: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Record the fold's WHOLE RESULT — ``{"result": <serialized OmegaEpisodeResult>}`` —
    once, BEFORE anything derived from it is applied, accepted or published. Returns the
    manifest on record, or None when the fold has no binding (no replay identity, so nothing
    can replay it).

    The result is frozen AS ONE, because every part of it is an authority for something.
    Freezing only the statements let a replay publish its own injections beside them —
    accepted "SQLite", published "switch to PostgreSQL". Freezing the control on the carrier
    alone still handed the replay's fresh result to the outcome artifacts and the build
    lifecycle — frozen PRESSURE, yet the build was ported to completed (review 2026-09-17).
    A replay therefore APPLIES, accepts, publishes and settles the original result.

    The first manifest is AUTHORITATIVE, and the caller accepts statements FROM IT, never
    from a replay's own output. Counts alone were not enough (review 2026-09-17): a first
    attempt that declared ``[A, B]``, journalled A and crashed was "recovered" by a replay
    emitting ``[B, A]`` into two durable A records, with B lost. With the payloads on record
    an unfinished slot is recovered from what the fold ORIGINALLY said, and no semantic
    matching is needed. A manifest recorded EMPTY is still recorded — ``[]`` is not ``None``
    — so a fold that committed nothing cannot gain statements on replay.
    """
    wanted = json.loads(json.dumps({"result": dict(manifest.get("result") or {})},
                                   ensure_ascii=False, default=str))
    if manifest.get("derivation") is not None:
        # The host-known derivation, frozen WITH the result and under the same first-wins
        # rule: a replay restores the original conclusion AND what it rested on (T10).
        derivation = manifest["derivation"]
        wanted["derivation"] = {
            "episode_id": str(derivation.get("episode_id") or ""),
            "recall": [str(one) for one in derivation.get("recall") or ()],
            "mail": [str(one) for one in derivation.get("mail") or ()]}
    if len(json.dumps(wanted, ensure_ascii=False)) > _MAX_MANIFEST_CHARS:
        raise ValueError(f"fold statement manifest exceeds {_MAX_MANIFEST_CHARS} characters")
    with _LOCK:
        binding = _snapshot()["bindings"].get(str(fold_id or ""))
        if binding is None:
            return None
        if binding["manifest"] is not None:
            return copy.deepcopy(binding["manifest"])
        state = _candidate()
        state["bindings"][fold_id]["manifest"] = wanted
        _publish(state)
        return copy.deepcopy(wanted)


def fold_binding(fold_id: str) -> Optional[dict[str, Any]]:
    """The durable binding recorded for a fold identity, or None. A snapshot."""
    with _LOCK:
        row = _snapshot()["bindings"].get(str(fold_id or ""))
        return copy.deepcopy(row) if row is not None else None


def claim_for_fold(
    *,
    fold_id: str,
    lane: str,
    thread_id: str,
    episode_id: str,
    lease_s: float,
    job_id: str = "",
    max_rows: Optional[int] = None,
    admit: Optional[Callable[[list[dict[str, Any]]], bool]] = None,
    origin_seq: int = 0,
    allow_unaddressed: bool = True,
) -> dict[str, Any]:
    """Bind a fold's ``omega_reason`` mail — restart-stable when the fold has an identity.

    ``origin_seq`` is the fold's birth order: its ignition contributor's global arrival
    sequence. A fold with an identity but NO binding on record, born at or before the replay
    horizon, gets ``beyond_replay_horizon``: its evidence may have been evicted, so it binds
    nothing rather than being mistaken for a fresh fold.

    Returns ``{claimed, refused, cutoff_seq, disposition}``. The cutoff is read and the set
    claimed in ONE critical section, and the binding (the binding INTENT the spec requires
    to be durable "before or atomically with the claim") is part of that same publication.

    A fold identity that already has a binding is a REPLAY of that fold:

    ``replayed``       the recording process is gone (a restart) and every recorded row is
                       still there — ready, or leased to the dead episode. They are leased
                       to this episode: the identical set, under the recorded cutoff.
    ``reconciled``     the binding is settled, or none of its rows remain: that fold's mail
                       was acknowledged. Acknowledged mail is never context again, and the
                       replay binds NOTHING — not even newer mail, which a fold that already
                       ran its course would consume without effect.
    ``abandoned``      only part of the set can be had (another consumer holds or took some).
                       Nothing is claimed; what is ready is redelivered to a later fold.
                       This proves redelivery only, never identical-set replay.
    ``held_elsewhere`` the recording process is THIS one and another episode holds the
                       binding: that episode may be alive, so nothing is taken from it.
    """
    if not str(episode_id or "").strip():
        raise ValueError("a claim requires the claiming episode")
    _require_finite(lease_s, "lease_s")
    fold_id = str(fold_id or "")
    with _LOCK:
        prior = _snapshot()["bindings"].get(fold_id) if fold_id else None
        if (prior is None and fold_id
                and 0 < int(origin_seq or 0) <= int(_snapshot()["horizon_seq"])):
            return {"claimed": [], "refused": [], "cutoff_seq": 0,
                    "disposition": "beyond_replay_horizon"}
        if prior is None or prior["episode_id"] == str(episode_id):
            cutoff = high_water_seq()
            claimed, refused = claim_mail_detailed(
                delivery_point="omega_reason", lane=lane, thread_id=thread_id,
                episode_id=episode_id, job_id=job_id, lease_s=lease_s, cutoff_seq=cutoff,
                max_rows=max_rows, admit=admit, fold_id=fold_id,
                origin_seq=origin_seq, allow_unaddressed=allow_unaddressed)
            return {"claimed": claimed, "refused": refused, "cutoff_seq": cutoff,
                    "disposition": "bound" if claimed else "none"}
        result = {"claimed": [], "refused": [], "cutoff_seq": int(prior["cutoff_seq"])}
        if prior["process_epoch"] == process_epoch():
            return {**result, "disposition": "held_elsewhere"}
        state = _candidate()
        now = time.time()
        _sweep(state, now)
        if not prior["mail_ids"] and prior["state"] == "bound":
            # The identical set is the EMPTY set: the replay reasons over nothing either.
            state["bindings"][fold_id] = {**state["bindings"][fold_id],
                                          "episode_id": str(episode_id),
                                          "process_epoch": process_epoch()}
            _publish(state)
            return {**result, "disposition": "replayed"}
        rows = [state["mail"].get(mail_id) for mail_id in prior["mail_ids"]]
        present = [row for row in rows if row is not None and row["status"] in {"ready", "leased"}]
        if prior["state"] == "settled" or not present:
            state["bindings"][fold_id] = {**state["bindings"][fold_id], "state": "settled"}
            _publish(state)
            return {**result, "disposition": "reconciled"}
        takeable = [row for row in present
                    if row["thread_id"] == str(thread_id)
                    and (row["status"] == "ready" or row["claimed_by"] == prior["episode_id"])]
        if len(takeable) != len(prior["mail_ids"]):
            _publish(state)
            return {**result, "disposition": "abandoned"}
        claimed = []
        for row in sorted(takeable, key=_rank):
            row.update(status="leased", lease_id=uuid.uuid4().hex[:16],
                       lease_until=now + float(lease_s), claimed_by=str(episode_id),
                       generation=int(row["generation"]) + 1)
            offers = [one for one in row["offered_to"] if one != str(episode_id)]
            row["offered_to"] = (offers + [str(episode_id)])[-_MAX_OFFERS:]
            claimed.append(_out(row))
        state["bindings"][fold_id] = {**state["bindings"][fold_id], "episode_id": str(episode_id),
                                      "process_epoch": process_epoch()}
        _publish(state)
        return {**result, "claimed": claimed, "disposition": "replayed"}


def claim_lane_entry(
    *,
    lane: str,
    thread_id: str,
    episode_id: str,
    mail_ids: Iterable[str],
    continuation_key: str,
    lease_s: float,
    job_id: str = "",
    payload_budget: Optional[int] = None,
) -> dict[str, Any]:
    """Lease exactly the ``lane_entry`` mail a continuation names (spec section 4, T9).

    Returns ``{claimed, undelivered}``. ``undelivered`` accounts for EVERY named id that was
    not leased, with the reason — refused at selection (another continuation or the budget),
    or not claimable at all (``unknown`` — acknowledged mail is deleted, so gone covers
    consumed and never-pinned — ``rejected``, leased
    elsewhere, addressed to another consumer). It is
    decided under the SAME lock as the claim: prerequisite enforcement must never learn of a
    missing ``required`` message from a second read that another consumer can race.
    """
    wanted = [str(one) for one in mail_ids or () if str(one)]
    with _LOCK:
        claimed, refused = claim_mail_detailed(
            delivery_point="lane_entry", lane=lane, thread_id=thread_id, episode_id=episode_id,
            lease_s=lease_s, job_id=job_id, mail_ids=wanted, payload_budget=payload_budget,
            continuation_key=str(continuation_key or ""))
        accounted = {row["mail_id"] for row in claimed} | {row["mail_id"] for row in refused}
        undelivered = [dict(row) for row in refused]
        stored = _snapshot()["mail"]
        for mail_id in wanted:
            if mail_id in accounted:
                continue
            row = stored.get(mail_id)
            if row is None:
                # Acknowledged mail is deleted, so "gone" covers consumed and never-pinned.
                reason, requirement, purpose = "it is unknown to the store", "", ""
            else:
                requirement, purpose = row["requirement"], row["purpose"]
                if row["status"] != "ready":
                    reason = f"it is {row['status']}"
                elif not _addressed(row, delivery_point="lane_entry", lane=lane,
                                    thread_id=thread_id, job_id=job_id):
                    reason = "it is not addressed to this consumer"
                else:
                    reason = "it is not deliverable yet"
            undelivered.append({"mail_id": mail_id, "requirement": requirement,
                                "purpose": purpose, "reason": reason})
        return {"claimed": claimed, "undelivered": undelivered}


def claim_mail(**kwargs: Any) -> list[dict[str, Any]]:
    """:func:`claim_mail_detailed` without its refusals — just the leased rows."""
    return claim_mail_detailed(**kwargs)[0]


def acknowledge_mail(receipts: Iterable[Mapping[str, Any]], *, episode_id: str) -> int:
    """End the lifecycle for mail this episode holds. Terminal, and validated per row.

    Each receipt names one row's ``mail_id``, ``lease_id`` and ``generation`` — the
    claimed rows themselves are valid receipts, so a consumer passes back what it
    claimed. A single lease id could not acknowledge a multi-row claim at all, because
    every row is leased under its own id.

    Ownership is checked per row, and so is the DEADLINE: a lease whose time has passed
    no longer authorises anything, because the mail may already have been re-leased.

    Successful acknowledgement deletes the rows. There is no stored ``acknowledged``
    status; honour lives on the binding and transaction record.
    """
    rows = [dict(receipt) for receipt in receipts or ()]
    if not rows:
        return 0
    if not str(episode_id or "").strip():
        raise ValueError("an acknowledgement requires the acknowledging episode")
    with _LOCK:
        state = _candidate()
        # Sampled inside the lock, for the same reason as the claim path.
        now = time.time()
        for receipt in rows:
            mail_id = str(receipt.get("mail_id") or "")
            row = state["mail"].get(mail_id)
            if row is None:
                raise ValueError(f"unknown mail {mail_id!r}")
            if row["status"] != "leased":
                raise ValueError(f"mail {mail_id!r} is {row['status']}, not leased")
            if str(row["claimed_by"]) != str(episode_id):
                raise ValueError(f"mail {mail_id!r} is held by {row['claimed_by']!r}")
            if str(row["lease_id"]) != str(receipt.get("lease_id") or ""):
                raise ValueError(f"mail {mail_id!r} lease does not match")
            if int(row["generation"]) != int(receipt.get("generation", -1)):
                raise ValueError(f"mail {mail_id!r} moved on since it was claimed")
            if float(row["lease_until"]) <= now:
                raise ValueError(f"mail {mail_id!r} lease expired before acknowledgement")
        for receipt in rows:
            state["mail"].pop(str(receipt.get("mail_id") or ""), None)
        for binding in state["bindings"].values():
            # Settled once none of its rows remain to be acknowledged by this episode.
            if binding["episode_id"] == str(episode_id) and not any(
                    mail_id in state["mail"] and state["mail"][mail_id]["status"] == "leased"
                    and state["mail"][mail_id]["claimed_by"] == str(episode_id)
                    for mail_id in binding["mail_ids"]):
                binding["state"] = "settled"
        _publish(state)
        return len(rows)


def release_mail(receipts: Iterable[Mapping[str, Any]], *, episode_id: str) -> int:
    """Hand back mail this episode leased WITHOUT an outcome. Returns the count released.

    The path for a fold that produced no valid control: its mail returns to ``ready``
    now rather than after the lease runs out, so the next episode can reason over it.

    Deliberately lenient where :func:`acknowledge_mail` is strict. Acknowledgement
    ASSERTS an outcome and must refuse anything it cannot vouch for; release only
    RELINQUISHES a claim. A receipt for a row this episode no longer holds — its lease
    already swept, or re-leased by another episode — is skipped rather than raised, and
    ownership is still checked per row, so a stale receipt can never release a lease
    somebody else now holds.
    """
    rows = [dict(receipt) for receipt in receipts or ()]
    if not rows or not str(episode_id or "").strip():
        return 0
    with _LOCK:
        state = _candidate()
        released = 0
        for receipt in rows:
            row = state["mail"].get(str(receipt.get("mail_id") or ""))
            if (row is None or row["status"] != "leased"
                    or str(row["claimed_by"]) != str(episode_id)
                    or str(row["lease_id"]) != str(receipt.get("lease_id") or "")
                    or int(row["generation"]) != int(receipt.get("generation", -1))):
                continue
            row.update(status="ready", lease_id="", lease_until=0.0, claimed_by="",
                       generation=int(row["generation"]) + 1)
            released += 1
        if released:
            # A fold that handed its mail back produced nothing over it: its binding is
            # forgotten, so a replay of that fold claims afresh like any other fold.
            for fold_id in [key for key, binding in state["bindings"].items()
                            if binding["episode_id"] == str(episode_id)
                            and binding["state"] == "bound"]:
                del state["bindings"][fold_id]
            _publish(state)
        return released


def owned_mail(receipts: Iterable[Mapping[str, Any]], *, episode_id: str) -> list[dict[str, Any]]:
    """The rows this episode STILL holds under these receipts. Read-only snapshots.

    A row counts only while it is leased by this episode under the same lease id and
    generation, with its lease deadline still live. A cached binding is
    not proof of ownership: the mail may have been released, or its lease may have run
    out and been taken by another episode. Time is sampled inside the lock.
    """
    rows = [dict(receipt) for receipt in receipts or ()]
    if not rows or not str(episode_id or "").strip():
        return []
    with _LOCK:
        state = _snapshot()
        now = time.time()
        held: list[dict[str, Any]] = []
        for receipt in rows:
            row = state["mail"].get(str(receipt.get("mail_id") or ""))
            if (row is not None and row["status"] == "leased"
                    and str(row["claimed_by"]) == str(episode_id)
                    and str(row["lease_id"]) == str(receipt.get("lease_id") or "")
                    and int(row["generation"]) == int(receipt.get("generation", -1))
                    and float(row["lease_until"]) > now):
                held.append(_out(row))
        return held


def while_owned(receipts: Iterable[Mapping[str, Any]], *, episode_id: str,
                action: Callable[[], Any]) -> Any:
    """Run ``action`` only while ``episode_id`` holds EVERY receipt, under the store lock.

    Ownership is checked and the action runs inside one critical section, so no other
    consumer can claim, acknowledge or release these rows between the check and the
    action. A check made outside the lock proves nothing: a worker could pass it, stall
    past its lease, and act after another worker had already finished the same mail
    (review 2026-09-16). Raises ``ValueError`` when ownership is gone.

    Lock order: the action may take the cognition queue's lock (this store already calls
    into the queue while locked), but it must never call back into a lock that can wait
    on this store.
    """
    rows = [dict(receipt) for receipt in receipts or ()]
    if not rows or not str(episode_id or "").strip():
        raise ValueError("ownership requires receipts and the holding episode")
    with _LOCK:
        if len(owned_mail(rows, episode_id=episode_id)) != len(rows):
            raise MailOwnershipLost("mail ownership lost before the action")
        return action()


def high_water_seq() -> int:
    """The highest ``arrival_seq`` held in the store, or 0 when it is empty.

    Usable as a reconciliation cutoff read OUTSIDE a claim: ``pin_mail`` allocates from
    the one monotonic global sequence, so anything pinned after this read is strictly
    greater and a ``cutoff_seq`` filter excludes it. No sequence number is consumed.
    """
    with _LOCK:
        return max((int(row["arrival_seq"]) for row in _snapshot()["mail"].values()),
                   default=0)


def release_expired_leases() -> int:
    """Return timed-out leases to ``ready``. A crash mid-reasoning loses nothing."""
    with _LOCK:
        state = _candidate()
        released = _release_expired(state, time.time())
        if released:
            _publish(state)
        return released


def all_mail() -> list[dict[str, Any]]:
    """Every row the store holds, leased ones included, as deep snapshots in claim order."""
    with _LOCK:
        rows = [_out(row) for row in _snapshot()["mail"].values()]
        rows.sort(key=_rank)
        return rows


def pending_mail() -> list[dict[str, Any]]:
    """Every row awaiting disposition — not currently held by a consumer.

    Leased rows are excluded because they are somebody's live obligation, and
    acknowledged rows are gone. Explicitly rejected rows remain visible as a terminal
    disposition with no automatic pruning.
    """
    with _LOCK:
        rows = [_out(row) for row in _snapshot()["mail"].values() if row["status"] != "leased"]
        rows.sort(key=_rank)
        return rows


def clear_mail() -> None:
    """Drop the whole store. Tests only; never part of cognition."""
    global _CACHE
    with _LOCK:
        try:
            mail_path().unlink()
        except FileNotFoundError:
            pass
        _CACHE = None


def reset_cache_for_tests() -> None:
    """Forget the in-memory view so the next read comes off disk — a simulated restart,
    so the process epoch is re-minted too."""
    global _CACHE, _PROCESS_EPOCH
    with _LOCK:
        _CACHE = None
        _PROCESS_EPOCH = ""
