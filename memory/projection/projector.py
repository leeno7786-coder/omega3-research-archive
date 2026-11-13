"""Program 0 — the bounded, synchronous outbox consumer (Cut 13 Part B; plan d1673531 P0.1–P0.4).

Program 0 ALWAYS full-rebuilds from a CLOSED canonical snapshot (D-13-7/20): the Cut-4 outbox
is work evidence — it tells the drain there is work, classifies deltas against the snapshot the
generation was built from, and its consumption position is recorded separately (D-13-31). It
runs under the ONE publication authority token the DISPATCHER acquired (D-13-33/67) and takes
ONE outbox exclusion (D-13-56) for bootstrap + capture, released before the long build.

Order per ``drain_once_locked`` (D-13-62 — at most ONE build per invocation):
  resume rotation intent → bootstrap / resume bootstrap (D-13-52) → capture ONE OutboxSnapshot
  (D-13-48; a corrupt ESTABLISHED outbox ⇒ rotate FIRST, D-13-43) → verify + parse (D-13-31) →
  malformed receipts durable (D-13-66) → needs_build = initial_generation_required OR valid rows
  OR malformed rows OR dirty OR recovery_requires_full_build → ONE closed Candidate + ONE
  publish barrier → cursor settled from THIS invocation's snapshot → dirty compare-and-clear.
Model A (D-13-72/76): a crash after the barrier before the cursor ⇒ the next invocation reads the
still-old cursor, captures the CURRENT unconsumed range, performs ITS one build, accepts any
lawful barrier and settles ITS snapshot — never a zero-rebuild settlement inferred from an id.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Mapping

# The CRT opens descriptors in TEXT mode on Windows unless told otherwise, translating
# LF<->CRLF on os.read/os.write. Every fd in this contract carries bytes with byte-offset
# accounting, so every os.open here is binary. No-op on POSIX.
_O_BINARY = getattr(os, "O_BINARY", 0)

from core.timekeeper import CoverageClock
from memory.projection.build import BuildRejected, build_generation
from memory.projection.generation import (
    Candidate,
    CoverageClockRegression,
    GenerationIntegrityError,
    PublicationAuthorityBusy,
    PublicationBlockedOnLeases,
    PublicationIncomplete,
    PublicationLineageTampered,
    PublicationResult,
    RecoveryOperatorRequired,
    _crash,
    _read_json,
    _strict_write_json,
    assert_authority,
    manifest_state,
    projection_publication_authority,
    projection_root,
    publish_generation,
    read_active_manifest,
    recover_generation_intents,
    resolve_memory_root,
    sync_dir,
)
from memory.projection_delta import (
    OUTBOX_BOOTSTRAP_INTENT_FILE,
    OUTBOX_CURSOR_FILE,
    OUTBOX_EPOCH_FILE,
    OUTBOX_MALFORMED_FILE,
    ZERO_SHA256,
    OutboxAuthority,
    OutboxCorrupt,
    OutboxEpoch,
    OutboxSnapshot,
    ProjectionDelta,
    _assert_outbox_authority,
    capture_outbox_snapshot_locked,
    clear_projection_dirty,
    complete_outbox_recovery,
    outbox_exclusion,
    outbox_file_identity_of,
    outbox_path,
    pending_outbox_recovery,
    projection_dirty_path,
    projection_dirty_state,
    rotate_outbox_epoch_locked,
)
from memory.taxonomy.normalize import canonical_json

__all__ = [
    "ZERO_SHA256", "OutboxSnapshot", "OutboxEpoch", "OutboxCursor", "OutboxBootstrapIntent", "OutboxBootstrapReport",
    "ProjectionDrainReport", "MalformedRow", "bootstrap_outbox", "drain_once_locked", "drain_once", "consumption_state",
    "read_cursor", "read_epoch", "read_malformed_receipts",
]

INITIAL_EPOCH = 1
BOOTSTRAP_INTENT_SCHEMA = "omega.projection_outbox_bootstrap.v1"
CURSOR_SCHEMA = "omega.projection_outbox_cursor.v1"
MALFORMED_SCHEMA = "omega.projection_outbox_malformed.v1"

DrainStatus = Literal[
    "published_new", "adopted_orphan", "reactivated_retained", "reused_existing", "replaced_corrupt", "no_work", "bootstrapped",
    "blocked_on_obligations", "blocked_on_leases", "refused_authority", "snapshot_moved", "outbox_corrupt_recovered",
    "outbox_metadata_corrupt_recovered", "lineage_operator_required", "coverage_clock_regression", "coverage_clock_unavailable",
    "coverage_seal_unavailable", "publication_incomplete", "failed",
]
BootstrapState = Literal["virgin", "legacy_cut4", "established", "partial_bootstrap", "metadata_corrupt"]


# ── Program-0 types (never in Part A) ─────────────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class OutboxCursor:                       # projection/consumed.json (D-13-31/43)
    outbox_file_epoch: int
    outbox_file_identity: str
    observed_size: int
    observed_prefix_sha256: str
    outbox_byte_offset: int
    consumed_prefix_sha256: str
    last_delta_id: str | None
    generation_id: str | None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["schema_version"] = CURSOR_SCHEMA
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "OutboxCursor":
        return cls(
            outbox_file_epoch=int(d["outbox_file_epoch"]), outbox_file_identity=str(d["outbox_file_identity"]),
            observed_size=int(d["observed_size"]), observed_prefix_sha256=str(d["observed_prefix_sha256"]),
            outbox_byte_offset=int(d["outbox_byte_offset"]), consumed_prefix_sha256=str(d["consumed_prefix_sha256"]),
            last_delta_id=(None if d.get("last_delta_id") is None else str(d["last_delta_id"])),
            generation_id=(None if d.get("generation_id") is None else str(d["generation_id"])),
        )


def zero_cursor(epoch: int, identity: str) -> OutboxCursor:
    return OutboxCursor(outbox_file_epoch=int(epoch), outbox_file_identity=identity, observed_size=0, observed_prefix_sha256=ZERO_SHA256,
                        outbox_byte_offset=0, consumed_prefix_sha256=ZERO_SHA256, last_delta_id=None, generation_id=None)


@dataclass(frozen=True, kw_only=True)
class OutboxBootstrapIntent:              # projection/outbox_bootstrap_intent.json (D-13-52)
    schema_version: str = BOOTSTRAP_INTENT_SCHEMA
    kind: Literal["virgin", "legacy_cut4"]
    epoch: int
    file_identity: str | None
    prepared_at: str


@dataclass(frozen=True, kw_only=True)
class OutboxBootstrapReport:              # D-13-56 — constructed on EVERY bootstrap return path
    state: BootstrapState
    action: Literal["created", "adopted", "resumed", "none", "recovery_required"]
    epoch: int | None
    file_identity: str | None
    cursor: OutboxCursor | None
    pending_bytes: int
    malformed_tail_bytes: int
    delimiter_appended: bool
    initial_generation_required: bool


@dataclass(frozen=True, kw_only=True)
class MalformedRow:
    """One complete outbox row that is not a parseable ProjectionDelta (incl. an EMPTY or
    whitespace-only row — the producer never emits one; frozen policy D-13-66). Byte range
    is the row CONTENT [start, end) — the terminating LF excluded — so the bootstrap partial-
    tail receipt and the parse-stage receipt for the same row are ONE receipt."""

    epoch: int
    byte_start: int
    byte_end: int
    sha256: str
    error: str

    def key(self) -> tuple[int, int, int, str]:
        return (self.epoch, self.byte_start, self.byte_end, self.sha256)


@dataclass(frozen=True, kw_only=True)
class ProjectionDrainReport:
    status: DrainStatus
    generation_id: str | None
    bootstrap_state: BootstrapState
    needs_build: bool
    full_builds: int
    malformed_row_count: int
    malformed_forced_build: bool
    head_clock: CoverageClock | None
    live_clock: CoverageClock | None
    outbox_file_epoch: int | None
    outbox_file_identity: str | None
    observed_size: int
    observed_prefix_sha256: str
    saved_offset: int
    boundary: int
    consumed_prefix_sha256: str
    deltas_seen: int
    latest_per_memory: int
    sequenced_consumed: int
    unsequenced_consumed: int
    stale_delta_count: int
    tombstone_count: int
    malformed_lines: tuple[dict, ...]
    dirty_before_sha256: str | None
    dirty_cleared: bool
    watermark: int | None
    lag: int | None
    last_delta_id: str | None
    publication: PublicationResult | None
    cursor_settled: bool = False
    recovery_rotated: bool = False
    detail: dict = field(default_factory=dict)


# ── small durable files ───────────────────────────────────────────────────────

class OutboxMetadataCorrupt(RuntimeError):
    """A partial-bootstrap subset that is impossible under the write order, or unreadable
    metadata ⇒ D-13-43 rotate-first recovery, NEVER an inferred bootstrap (D-13-52)."""


def read_cursor(proj: Path) -> OutboxCursor | None:
    path = proj / OUTBOX_CURSOR_FILE
    if not path.exists():
        return None
    try:
        return OutboxCursor.from_dict(_read_json(path))
    except Exception as exc:  # noqa: BLE001
        raise OutboxMetadataCorrupt(f"consumed.json unreadable: {exc}") from exc


def read_epoch(proj: Path) -> OutboxEpoch | None:
    path = proj / OUTBOX_EPOCH_FILE
    if not path.exists():
        return None
    try:
        d = _read_json(path)
        return OutboxEpoch(epoch=int(d["epoch"]), file_identity=str(d["file_identity"]), origin=str(d["origin"]),
                           established_at=str(d.get("established_at") or ""))
    except Exception as exc:  # noqa: BLE001
        raise OutboxMetadataCorrupt(f"outbox_epoch.json unreadable: {exc}") from exc


def read_bootstrap_intent(proj: Path) -> OutboxBootstrapIntent | None:
    path = proj / OUTBOX_BOOTSTRAP_INTENT_FILE
    if not path.exists():
        return None
    try:
        d = _read_json(path)
        return OutboxBootstrapIntent(kind=str(d["kind"]), epoch=int(d["epoch"]),
                                     file_identity=(None if d.get("file_identity") is None else str(d["file_identity"])),
                                     prepared_at=str(d.get("prepared_at") or ""))
    except Exception as exc:  # noqa: BLE001
        raise OutboxMetadataCorrupt(f"outbox_bootstrap_intent.json unreadable: {exc}") from exc


def read_malformed_receipts(proj: Path) -> tuple[MalformedRow, ...]:
    path = proj / OUTBOX_MALFORMED_FILE
    if not path.exists():
        return ()
    try:
        d = _read_json(path)
        rows = d.get("receipts", []) if isinstance(d, Mapping) else []
        return tuple(MalformedRow(epoch=int(r["epoch"]), byte_start=int(r["byte_start"]), byte_end=int(r["byte_end"]),
                                  sha256=str(r["sha256"]), error=str(r.get("error") or "")) for r in rows)
    except Exception:  # noqa: BLE001 — an unreadable receipt file is itself surfaced by health; never silently empty here
        raise OutboxMetadataCorrupt("outbox_malformed.json unreadable")


def _write_cursor(proj: Path, cursor: OutboxCursor) -> None:
    _strict_write_json(proj / OUTBOX_CURSOR_FILE, cursor.to_dict())


def _write_epoch(proj: Path, epoch: OutboxEpoch) -> None:
    _strict_write_json(proj / OUTBOX_EPOCH_FILE, epoch.to_dict())


def _write_bootstrap_intent(proj: Path, intent: OutboxBootstrapIntent) -> None:
    _strict_write_json(proj / OUTBOX_BOOTSTRAP_INTENT_FILE, asdict(intent))


def _clear_bootstrap_intent(proj: Path) -> None:
    path = proj / OUTBOX_BOOTSTRAP_INTENT_FILE
    if path.exists():
        path.unlink()
        sync_dir(proj)


def persist_malformed_receipts(proj: Path, rows: list[MalformedRow]) -> int:
    """Idempotent by (epoch, byte_start, byte_end, sha256); atomic replace + parent sync.
    Returns the number of NEW receipts written. Must be durable BEFORE any build (D-13-31/43)."""
    if not rows:
        return 0
    from core.symphony_time import now_iso

    existing = list(read_malformed_receipts(proj))
    seen = {r.key() for r in existing}
    added = 0
    for r in rows:
        if r.key() not in seen:
            existing.append(r)
            seen.add(r.key())
            added += 1
    if added:
        _strict_write_json(proj / OUTBOX_MALFORMED_FILE, {
            "schema_version": MALFORMED_SCHEMA, "recorded_at": now_iso(),
            "receipts": [asdict(r) for r in existing],
        })
    return added


def _identity_of_path(path: Path) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | _O_BINARY)
    except FileNotFoundError:
        return None
    try:
        return outbox_file_identity_of(fd)
    finally:
        os.close(fd)


def _create_empty_outbox(root: Path) -> None:
    path = outbox_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o644)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    sync_dir(path.parent)


def _append_delimiter(root: Path) -> None:
    """Legacy partial final row: append + fsync ONE LF before any writer is released (D-13-52)."""
    path = outbox_path(root)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | _O_BINARY)
    try:
        os.write(fd, b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)


# ── bootstrap state machine (D-13-52/56/72) ───────────────────────────────────

def _classify(proj: Path, root: Path) -> tuple[BootstrapState, dict]:
    """Read-only classification. Returns (state, facts)."""
    facts: dict[str, Any] = {"outbox_exists": outbox_path(root).exists()}
    try:
        intent = read_bootstrap_intent(proj)
        epoch = read_epoch(proj)
        cursor = read_cursor(proj)
    except OutboxMetadataCorrupt as exc:
        facts["error"] = str(exc)
        return "metadata_corrupt", facts
    facts.update(intent=intent, epoch=epoch, cursor=cursor)
    if intent is not None:
        return "partial_bootstrap", facts
    if epoch is not None and cursor is not None:
        return "established", facts
    if epoch is None and cursor is None:
        return ("legacy_cut4" if facts["outbox_exists"] else "virgin"), facts
    facts["error"] = "epoch without cursor or cursor without epoch (impossible under the write order)"
    return "metadata_corrupt", facts


def _bootstrap_locked(root: Path, proj: Path, *, oa: OutboxAuthority, initial_generation_required: bool
                      ) -> tuple[OutboxBootstrapReport, OutboxSnapshot | None, list[MalformedRow]]:
    """The whole transition under the ONE held exclusion; every helper is ``_locked``.
    Returns (report, snapshot-to-carry-forward-or-None, bootstrap-stage malformed receipts)."""
    from core.symphony_time import now_iso

    _assert_outbox_authority(oa, root)
    state, facts = _classify(proj, root)
    if state == "metadata_corrupt":
        return OutboxBootstrapReport(state="metadata_corrupt", action="recovery_required", epoch=None, file_identity=None, cursor=None,
                                     pending_bytes=0, malformed_tail_bytes=0, delimiter_appended=False,
                                     initial_generation_required=initial_generation_required), None, []
    if state == "established":
        cur: OutboxCursor = facts["cursor"]
        return OutboxBootstrapReport(state="established", action="none", epoch=facts["epoch"].epoch, file_identity=facts["epoch"].file_identity,
                                     cursor=cur, pending_bytes=0, malformed_tail_bytes=0, delimiter_appended=False,
                                     initial_generation_required=initial_generation_required), None, []
    intent: OutboxBootstrapIntent | None = facts.get("intent")
    resumed = intent is not None
    kind = intent.kind if intent is not None else ("virgin" if state == "virgin" else "legacy_cut4")
    if resumed and intent.kind == "legacy_cut4":
        # D-13-52 (review a045ec4a §1): a resumed LEGACY intent must find the SAME PHYSICAL FILE it
        # bound. A missing outbox or a different inode (even with identical bytes) is NOT the legacy
        # backlog the intent named ⇒ recovery-required ⇒ D-13-43 rotate-first. Never adopt a
        # replacement into epoch 1 and never rewrite the intent to the new identity.
        # (A virgin intent carries file_identity=None by design — its empty file may not exist yet.)
        current_identity = _identity_of_path(outbox_path(root))
        if current_identity is None or (intent.file_identity is not None and current_identity != intent.file_identity):
            return OutboxBootstrapReport(state="metadata_corrupt", action="recovery_required", epoch=None,
                                         file_identity=current_identity, cursor=None, pending_bytes=0, malformed_tail_bytes=0,
                                         delimiter_appended=False, initial_generation_required=initial_generation_required), None, []
    if intent is None:
        identity = _identity_of_path(outbox_path(root)) if kind == "legacy_cut4" else None
        intent = OutboxBootstrapIntent(kind=kind, epoch=INITIAL_EPOCH, file_identity=identity, prepared_at=now_iso())
        _write_bootstrap_intent(proj, intent)                                            # (1) intent durable first
        _crash("p0:bootstrap_after_intent")
    if kind == "virgin":
        if not outbox_path(root).exists():
            _create_empty_outbox(root)                                                     # (2) empty writable outbox
        _crash("p0:bootstrap_after_outbox")
        snap = capture_outbox_snapshot_locked(root=root, epoch=INITIAL_EPOCH, saved_offset=0, prior_observed=None,
                                              consumed_prefix_expected=None, outbox_authority=oa)
        epoch = read_epoch(proj)
        if epoch is None:
            _write_epoch(proj, OutboxEpoch(epoch=INITIAL_EPOCH, file_identity=snap.file_identity, origin="virgin", established_at=now_iso()))  # (3)
        _crash("p0:bootstrap_after_epoch")
        if read_cursor(proj) is None:
            _write_cursor(proj, zero_cursor(INITIAL_EPOCH, snap.file_identity))                # (4) zero cursor
        _crash("p0:bootstrap_after_cursor")
        _clear_bootstrap_intent(proj)                                                          # (5)
        cur = read_cursor(proj)
        return OutboxBootstrapReport(state=("partial_bootstrap" if resumed else "virgin"), action=("resumed" if resumed else "created"),
                                     epoch=INITIAL_EPOCH, file_identity=snap.file_identity, cursor=cur, pending_bytes=snap.captured_size,
                                     malformed_tail_bytes=0, delimiter_appended=False, initial_generation_required=initial_generation_required), snap, []
    # legacy Cut-4 outbox (possibly 0 bytes; possibly a partial final row) — adopt as epoch 1 at offset 0
    if not outbox_path(root).exists():                                                         # crash-resume with the file gone ⇒ same lock path as virgin
        _create_empty_outbox(root)
    snap = capture_outbox_snapshot_locked(root=root, epoch=INITIAL_EPOCH, saved_offset=0, prior_observed=None,
                                          consumed_prefix_expected=None, outbox_authority=oa)
    receipts: list[MalformedRow] = []
    delimiter_appended = False
    tail_bytes = 0
    if snap.captured_size > snap.complete_boundary:                                             # partial final row (no LF)
        with open(outbox_path(root), "rb") as fh:
            fh.seek(snap.complete_boundary)
            tail = fh.read(snap.captured_size - snap.complete_boundary)
        tail_bytes = len(tail)
        receipts.append(MalformedRow(epoch=INITIAL_EPOCH, byte_start=snap.complete_boundary, byte_end=snap.captured_size,
                                     sha256=hashlib.sha256(tail).hexdigest(), error="legacy partial final row (LF delimiter appended by bootstrap)"))
        persist_malformed_receipts(proj, receipts)                                             # receipt durable BEFORE the delimiter
        _crash("p0:bootstrap_after_tail_receipt")
        _append_delimiter(root)
        delimiter_appended = True
        snap = capture_outbox_snapshot_locked(root=root, epoch=INITIAL_EPOCH, saved_offset=0, prior_observed=None,
                                              consumed_prefix_expected=None, outbox_authority=oa)   # incl. the delimiter
    _crash("p0:bootstrap_after_delimiter")
    if read_epoch(proj) is None:
        _write_epoch(proj, OutboxEpoch(epoch=INITIAL_EPOCH, file_identity=snap.file_identity, origin="legacy_cut4", established_at=now_iso()))
    _crash("p0:bootstrap_after_epoch")
    if read_cursor(proj) is None:
        _write_cursor(proj, OutboxCursor(outbox_file_epoch=INITIAL_EPOCH, outbox_file_identity=snap.file_identity,
                                         observed_size=snap.captured_size, observed_prefix_sha256=snap.observed_prefix_sha256,
                                         outbox_byte_offset=0, consumed_prefix_sha256=ZERO_SHA256, last_delta_id=None, generation_id=None))
    _crash("p0:bootstrap_after_cursor")
    _clear_bootstrap_intent(proj)
    cur = read_cursor(proj)
    return OutboxBootstrapReport(state=("partial_bootstrap" if resumed else "legacy_cut4"), action=("resumed" if resumed else "adopted"),
                                 epoch=INITIAL_EPOCH, file_identity=snap.file_identity, cursor=cur, pending_bytes=snap.captured_size,
                                 malformed_tail_bytes=tail_bytes, delimiter_appended=delimiter_appended,
                                 initial_generation_required=initial_generation_required), snap, receipts


def bootstrap_outbox(root: Path | str | None, *, authority: Any) -> OutboxBootstrapReport:
    """D-13-52/56 state machine under the publication authority + ONE blocking outbox
    exclusion; idempotent; consumes nothing (the enclosing drain settles after its barrier)."""
    proj = projection_root(root)
    assert_authority(authority, proj)
    memory_root = resolve_memory_root(root)
    proj.mkdir(parents=True, exist_ok=True)
    initial = manifest_state(proj).mode != "generation"
    with outbox_exclusion(memory_root, blocking=True) as oa:
        pending = pending_outbox_recovery(memory_root)
        if pending is not None:
            rotate_outbox_epoch_locked(root=memory_root, old_state=pending, outbox_authority=oa)
        report, _snap, _receipts = _bootstrap_locked(memory_root, proj, oa=oa, initial_generation_required=initial)
        if report.state == "metadata_corrupt":
            _recover_metadata_corrupt_locked(memory_root, proj, oa=oa)
            report2, _s, _r = _bootstrap_locked(memory_root, proj, oa=oa, initial_generation_required=initial)
            return replace(report2, state="metadata_corrupt", action="recovery_required")
        return report


def _recover_metadata_corrupt_locked(root: Path, proj: Path, *, oa: OutboxAuthority) -> OutboxEpoch:
    """Impossible metadata subset ⇒ D-13-43 rotate-first (old epoch = whichever survives, else 0)."""
    old_epoch = 0
    for reader in (read_epoch, read_cursor):
        try:
            obj = reader(proj)
        except OutboxMetadataCorrupt:
            obj = None
        if obj is not None:
            old_epoch = max(old_epoch, int(getattr(obj, "epoch", getattr(obj, "outbox_file_epoch", 0))))
    for stale in (OUTBOX_EPOCH_FILE, OUTBOX_CURSOR_FILE, OUTBOX_BOOTSTRAP_INTENT_FILE):
        p = proj / stale
        if p.exists():
            p.unlink()
    sync_dir(proj)
    return rotate_outbox_epoch_locked(root=root, old_state={"old_epoch": old_epoch, "old_identity": _identity_of_path(outbox_path(root)),
                                                            "reason": "outbox_metadata_corrupt"}, outbox_authority=oa)


# ── parsing (D-13-31/66) ──────────────────────────────────────────────────────

def parse_snapshot(snap: OutboxSnapshot) -> tuple[list[tuple[ProjectionDelta, int, int]], list[MalformedRow]]:
    """Split ``bytes_to_parse`` (complete LF-framed rows only) into (delta, start, end) in
    append order and MalformedRow receipts (row content byte ranges; LF excluded)."""
    deltas: list[tuple[ProjectionDelta, int, int]] = []
    malformed: list[MalformedRow] = []
    pos = snap.saved_offset
    data = snap.bytes_to_parse
    idx = 0
    while idx < len(data):
        nl = data.find(b"\n", idx)
        if nl < 0:
            break                                                                              # never happens: bytes_to_parse ends at the boundary
        row = data[idx:nl]
        start, end = pos + idx, pos + nl
        if not row.strip():
            malformed.append(MalformedRow(epoch=snap.epoch, byte_start=start, byte_end=end, sha256=hashlib.sha256(row).hexdigest(),
                                          error="empty or whitespace-only row"))
        else:
            try:
                deltas.append((ProjectionDelta.from_dict(json.loads(row.decode("utf-8"))), start, end))
            except Exception as exc:  # noqa: BLE001 — every parse failure is a receipt
                malformed.append(MalformedRow(epoch=snap.epoch, byte_start=start, byte_end=end, sha256=hashlib.sha256(row).hexdigest(),
                                              error=f"{type(exc).__name__}: {exc}"))
        idx = nl + 1
    return deltas, malformed


# ── the drain ─────────────────────────────────────────────────────────────────

def _dirty_sha(root: Path) -> str | None:
    path = projection_dirty_path(root)
    if not path.exists():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _live_clock() -> CoverageClock | None:
    from core import timekeeper as TK
    try:
        return TK.last_coverage_clock()
    except Exception:  # noqa: BLE001
        return None


def _report(**kw: Any) -> ProjectionDrainReport:
    base: dict[str, Any] = dict(
        status="failed", generation_id=None, bootstrap_state="established", needs_build=False, full_builds=0, malformed_row_count=0,
        malformed_forced_build=False, head_clock=None, live_clock=_live_clock(), outbox_file_epoch=None, outbox_file_identity=None,
        observed_size=0, observed_prefix_sha256=ZERO_SHA256, saved_offset=0, boundary=0, consumed_prefix_sha256=ZERO_SHA256,
        deltas_seen=0, latest_per_memory=0, sequenced_consumed=0, unsequenced_consumed=0, stale_delta_count=0, tombstone_count=0,
        malformed_lines=(), dirty_before_sha256=None, dirty_cleared=False, watermark=None, lag=None, last_delta_id=None, publication=None,
    )
    base.update(kw)
    return ProjectionDrainReport(**base)


def _semantic_retina_stale(proj: Path) -> bool:
    """True when the active generation's retina was not built under the pinned encoder
    contract — a generation from before the contract existed, or one built by another
    encoder. Its codes cannot serve today's queries, so the canonical snapshot is
    projected again (audit 2026-09-28 M05)."""
    from memory import retina

    active = read_active_manifest(proj)
    component = active.content.components.get("semantic_retina") if active is not None else None
    return not isinstance(component, dict) or component.get("contract_sha256") != retina.contract_sha256()


def drain_once_locked(root: Path | str | None, *, reason: str, authority: Any) -> ProjectionDrainReport:
    """See the module docstring. Asserts the DISPATCHER's publication token; NEVER acquires
    the publication lock (D-13-67). At most ONE build per invocation (D-13-62/72/76)."""
    from core import timekeeper as TK

    proj = projection_root(root)
    assert_authority(authority, proj)
    memory_root = resolve_memory_root(root)
    proj.mkdir(parents=True, exist_ok=True)
    initial_required = manifest_state(proj).mode != "generation"
    dirty_before = _dirty_sha(memory_root)                       # observed BEFORE the build (D-13-5 compare-and-clear)
    rotated = False
    metadata_recovered = False
    receipts: list[MalformedRow] = []
    with outbox_exclusion(memory_root, blocking=True) as oa:      # ONE exclusion for bootstrap + capture (released before the build)
        pending = pending_outbox_recovery(memory_root)
        if pending is not None:                                    # resume an interrupted rotate-first recovery (rotation and/or its rebuild)
            if not pending.get("rotated"):
                rotate_outbox_epoch_locked(root=memory_root, old_state=pending, outbox_authority=oa)
            rotated = True
        breport, snap, receipts = _bootstrap_locked(memory_root, proj, oa=oa, initial_generation_required=initial_required)
        bootstrap_state: BootstrapState = breport.state
        if breport.state == "metadata_corrupt":
            _recover_metadata_corrupt_locked(memory_root, proj, oa=oa)
            rotated = metadata_recovered = True
            breport, snap, receipts = _bootstrap_locked(memory_root, proj, oa=oa, initial_generation_required=initial_required)
            bootstrap_state = "metadata_corrupt"
        cursor = read_cursor(proj)
        assert cursor is not None
        if snap is None:                                           # ESTABLISHED: capture from the cursor; any verification failure ⇒ rotate FIRST
            try:
                snap = capture_outbox_snapshot_locked(root=memory_root, epoch=cursor.outbox_file_epoch, saved_offset=cursor.outbox_byte_offset,
                                                      prior_observed=(cursor.observed_size, cursor.observed_prefix_sha256),
                                                      consumed_prefix_expected=cursor.consumed_prefix_sha256, outbox_authority=oa)
                if snap.file_identity != cursor.outbox_file_identity:
                    raise OutboxCorrupt(f"outbox physical identity changed ({cursor.outbox_file_identity} → {snap.file_identity})")
            except OutboxCorrupt as exc:
                rotate_outbox_epoch_locked(root=memory_root, old_state={"old_epoch": cursor.outbox_file_epoch, "old_identity": cursor.outbox_file_identity,
                                                                        "reason": str(exc)}, outbox_authority=oa)
                rotated = True
                cursor = read_cursor(proj)
                assert cursor is not None
                snap = capture_outbox_snapshot_locked(root=memory_root, epoch=cursor.outbox_file_epoch, saved_offset=0, prior_observed=None,
                                                      consumed_prefix_expected=None, outbox_authority=oa)
    _crash("p0:after_capture")
    # ── verify/parse + malformed receipts durable BEFORE any build (D-13-31/43/66) ──
    deltas, malformed = parse_snapshot(snap)
    merged: dict[tuple, MalformedRow] = {}
    for r in receipts + malformed:                                # the bootstrap partial-tail receipt and its parse-stage row are ONE receipt
        merged.setdefault(r.key(), r)
    receipts = list(merged.values())
    persist_malformed_receipts(proj, receipts)
    _crash("p0:after_receipts")
    latest: dict[str, ProjectionDelta] = {}
    for d, _s, _e in deltas:                                       # latest-per-memory_id by APPEND order
        latest[d.memory_id] = d
    dirty_now = projection_dirty_state(root=memory_root).get("dirty", False)
    retina_stale = not initial_required and _semantic_retina_stale(proj)
    needs_build = bool(initial_required or deltas or malformed or receipts or dirty_now or rotated or retina_stale)
    common = dict(
        bootstrap_state=bootstrap_state, malformed_row_count=len(receipts), malformed_forced_build=(bool(receipts) and not deltas and not initial_required and not dirty_now),
        outbox_file_epoch=snap.epoch, outbox_file_identity=snap.file_identity, observed_size=snap.captured_size,
        observed_prefix_sha256=snap.observed_prefix_sha256, saved_offset=snap.saved_offset, boundary=snap.complete_boundary,
        consumed_prefix_sha256=snap.consumed_prefix_sha256, deltas_seen=len(deltas), latest_per_memory=len(latest),
        sequenced_consumed=sum(1 for d, _s, _e in deltas if d.sequenced), unsequenced_consumed=sum(1 for d, _s, _e in deltas if not d.sequenced),
        malformed_lines=tuple(asdict(r) for r in receipts), dirty_before_sha256=dirty_before, needs_build=needs_build,
        recovery_rotated=rotated, last_delta_id=(deltas[-1][0].delta_id if deltas else cursor.last_delta_id),
    )
    if not needs_build:
        status: DrainStatus = "bootstrapped" if breport.action in ("created", "adopted", "resumed") else "no_work"
        active = read_active_manifest(proj) if not initial_required else None
        return _report(status=status, generation_id=(active.generation_id if active else None),
                       head_clock=(active.coverage_clock if active else None), watermark=(active.coverage_clock.commit_seq if active else None),
                       lag=((TK.last_commit_seq() - active.coverage_clock.commit_seq) if active else None), **common)
    # ── ONE build + ONE publish barrier ──
    snapshot_out: dict[str, Any] = {}
    outcome = build_generation(root, reason=reason, authority=authority, snapshot_out=snapshot_out)
    if isinstance(outcome, BuildRejected):
        status = {"blocked_on_obligations": "blocked_on_obligations", "coverage_clock_unavailable": "coverage_clock_unavailable",
                  "snapshot_moved": "snapshot_moved", "source_ledger_moved": "snapshot_moved", "registry_moved": "snapshot_moved"}.get(outcome.reason, "failed")
        return _report(status=status, full_builds=1, detail={"reason": outcome.reason, **outcome.detail, "attempts": outcome.attempts}, **common)
    cand: Candidate = outcome
    members: dict[str, str] = snapshot_out.get("members", {})
    stale = sum(1 for mid, d in latest.items() if mid in members and members[mid] != d.canonical_hash)
    tomb = sum(1 for mid in latest if mid not in members)
    try:
        pub = publish_generation(root, cand, authority=authority)
    except CoverageClockRegression as exc:
        return _report(status="coverage_clock_regression", full_builds=1, stale_delta_count=stale, tombstone_count=tomb, detail={"error": str(exc)}, **common)
    except PublicationBlockedOnLeases as exc:
        return _report(status="blocked_on_leases", full_builds=1, stale_delta_count=stale, tombstone_count=tomb, detail={"error": str(exc)}, **common)
    except (PublicationLineageTampered, RecoveryOperatorRequired) as exc:
        return _report(status="lineage_operator_required", full_builds=1, stale_delta_count=stale, tombstone_count=tomb, detail={"error": str(exc)}, **common)
    except PublicationIncomplete as exc:
        return _report(status="publication_incomplete", full_builds=1, stale_delta_count=stale, tombstone_count=tomb, detail={"error": str(exc)}, **common)
    except GenerationIntegrityError as exc:
        st: DrainStatus = "coverage_seal_unavailable" if type(exc).__name__ == "CoverageSealUnavailable" else "failed"
        return _report(status=st, full_builds=1, stale_delta_count=stale, tombstone_count=tomb, detail={"error": f"{type(exc).__name__}: {exc}"}, **common)
    _crash("p0:after_barrier")
    # ── barrier reached (active manifest durable + valid): settle THIS snapshot, then compare-and-clear dirty ──
    new_cursor = OutboxCursor(outbox_file_epoch=snap.epoch, outbox_file_identity=snap.file_identity, observed_size=snap.captured_size,
                              observed_prefix_sha256=snap.observed_prefix_sha256, outbox_byte_offset=snap.complete_boundary,
                              consumed_prefix_sha256=snap.consumed_prefix_sha256,
                              last_delta_id=(deltas[-1][0].delta_id if deltas else cursor.last_delta_id), generation_id=pub.generation_id)
    _write_cursor(proj, new_cursor)
    _crash("p0:after_cursor")
    if rotated:                                                   # the final D-13-43 receipt: rebuild published + cursor settled
        complete_outbox_recovery(memory_root, pending_outbox_recovery(memory_root) or {"new_epoch": snap.epoch})
    cleared = False
    if dirty_before is not None:
        cleared = clear_projection_dirty(root=memory_root, expected_marker_sha256=dirty_before, authority=authority)
    status = ("outbox_metadata_corrupt_recovered" if metadata_recovered else ("outbox_corrupt_recovered" if rotated else pub.outcome))
    wm = pub.active_manifest.coverage_clock.commit_seq
    return _report(status=status, generation_id=pub.generation_id, full_builds=1, head_clock=pub.active_manifest.coverage_clock,
                   stale_delta_count=stale, tombstone_count=tomb, dirty_cleared=cleared, watermark=wm, lag=TK.last_commit_seq() - wm,
                   publication=pub, cursor_settled=True, **common)


def drain_once(root: Path | str | None, *, reason: str) -> ProjectionDrainReport:
    """Public convenience wrapper: acquires the publication authority ONCE and delegates to
    ``drain_once_locked``. Never called by the dispatcher (D-13-67)."""
    try:
        with projection_publication_authority(root) as authority:
            recover_generation_intents(root, authority=authority, reason=f"{reason}:recovery")   # stale Part-A intents first (same token)
            return drain_once_locked(root, reason=reason, authority=authority)
    except PublicationAuthorityBusy as exc:
        return _report(status="refused_authority", detail={"error": str(exc)})


def consumption_state(root: Path | str | None) -> dict[str, Any]:
    """Read-only Program-0 state for health / operators: bootstrap classification, epoch,
    cursor, pending recovery, malformed receipts (visible after settlement and restart)."""
    proj = projection_root(root)
    memory_root = resolve_memory_root(root)
    state, facts = _classify(proj, memory_root) if proj.is_dir() else ("virgin", {"outbox_exists": outbox_path(memory_root).exists()})
    epoch = facts.get("epoch")
    cursor = facts.get("cursor")
    try:
        receipts = read_malformed_receipts(proj) if proj.is_dir() else ()
        receipts_ok = True
    except OutboxMetadataCorrupt:
        receipts, receipts_ok = (), False
    return {
        "bootstrap_state": state, "error": facts.get("error"),
        "epoch": (epoch.to_dict() if epoch is not None else None),
        "cursor": (cursor.to_dict() if cursor is not None else None),
        "recovery_pending": pending_outbox_recovery(memory_root) is not None,
        "malformed_receipts": [asdict(r) for r in receipts], "malformed_receipts_readable": receipts_ok,
        "bootstrap_intent": (asdict(facts["intent"]) if facts.get("intent") is not None else None),
    }


def _canonical(doc: Any) -> str:   # pragma: no cover - re-exported convenience for tests
    return canonical_json(doc)
