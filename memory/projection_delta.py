"""ProjectionDelta outbox — durable DME projection WORK, never truth (Cut 4).

Governing laws (16 INV-MO-018; 17 §3 LAW-EO-001..006):

  canonical commit clock  !=  DME projection clock

A canonical write is durable the moment it commits through the lawful
persistence seam. It does not wait for the graph/DME/NPU projection. Instead
the producer appends one deterministic ProjectionDelta here and a later
projector (Cut 14A) drains it at its own pace. Until then the record is
``projection_pending`` — canonically recallable/verifiable, not yet
DME-addressable. That state is LAG, not corruption, and it must be named,
never hidden.

What this file is / is not:

  * a derivative, reconstructable work journal under the profile's memory
    indexes — if it is missing/corrupt the recovery action is a full rebuild
    from canonical + durable adaptive sidecars (LAW-EO-003);
  * NOT factual authority: canonical files remain sufficient for a clean
    rebuild; nothing here is read to decide whether a memory exists;
  * NOT allowed to roll back a canonical write: if append fails after the
    canonical commit, the canonical write stays valid, ``mark_projection_dirty``
    sets a loud durable rebuild-required marker, and the projector must
    full-rebuild before claiming currentness (LAW-EO-004);
  * NOT a graph/DME writer: Cut 4 never mutates graph, DME, planes or NPU
    state (LAW-EO-005: one projector owns projection mutation).

``delta_id`` is a SHA-256 over canonical JSON of the stable identity fields
(operation, memory_id, canonical_path, canonical_hash, coord, commit_seq,
projection_class, schema_version). ``queued_at`` is diagnostic and excluded —
duplicate append/consume is idempotent by id; no O(N) dedupe scan at append.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

# The CRT opens descriptors in TEXT mode on Windows unless told otherwise, translating
# LF<->CRLF on os.read/os.write. Every fd in this contract carries bytes with byte-offset
# accounting, so every os.open here is binary. No-op on POSIX.
_O_BINARY = getattr(os, "O_BINARY", 0)

from memory.taxonomy.normalize import canonical_json

log = logging.getLogger("omega.memory.projection_delta")

PROJECTION_DELTA_SCHEMA_VERSION = "omega.projection_delta.v1"
PROJECTION_DIRTY_SCHEMA_VERSION = "omega.projection_dirty.v1"

PROJECTION_OPERATIONS = frozenset({"upsert", "supersede", "tombstone", "delete"})
PROJECTION_CLASSES = frozenset({"semantic", "filesystem_only"})

_LOCK = threading.RLock()

Coord = tuple[int, int, int]


# ── feature flag ─────────────────────────────────────────────────────────────


def native_citizenship_enabled() -> bool:
    """Cut 4 native non-identity citizenship. Default OFF until the live boot
    profile sets it (plan Task 7); explicit env override always wins."""
    return os.environ.get("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "0").strip().lower() in {
        "1",
        "true",
        "on",
        "yes",
    }


# ── paths (same active-profile conventions as DME/index code) ────────────────


def _memory_root(root: Path | str | None) -> Path:
    if root is not None:
        base = Path(root)
        return base if base.name == "memory" else base / "memory"
    from core.paths import MEMORY_ROOT

    return Path(MEMORY_ROOT)


def outbox_path(root: Path | str | None = None) -> Path:
    return _memory_root(root) / "indexes" / "projection_delta.jsonl"


def projection_dirty_path(root: Path | str | None = None) -> Path:
    return _memory_root(root) / "indexes" / "projection_dirty.json"


# ── record ───────────────────────────────────────────────────────────────────


def _coord3(raw: Any) -> Coord:
    try:
        seq = [int(v) for v in list(raw or ())]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"coord must be a sequence of ints: {raw!r}") from exc
    if len(seq) < 3:
        seq = seq + [0] * (3 - len(seq))
    return (seq[0], seq[1], seq[2])


def projection_delta_id(
    *,
    operation: str,
    memory_id: str,
    canonical_path: str,
    canonical_hash: str,
    coord: Iterable[int],
    commit_seq: int,
    projection_class: str,
    sequenced: bool = False,
    schema_version: str = PROJECTION_DELTA_SCHEMA_VERSION,
) -> str:
    identity = {
        "schema_version": str(schema_version),
        "operation": str(operation),
        "memory_id": str(memory_id),
        "canonical_path": str(canonical_path),
        "canonical_hash": str(canonical_hash),
        "coord": list(_coord3(coord)),
        "commit_seq": int(commit_seq),
        "projection_class": str(projection_class),
        "sequenced": bool(sequenced),
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProjectionDelta:
    delta_id: str
    operation: str
    memory_id: str
    canonical_path: str
    canonical_hash: str
    coord: Coord
    commit_seq: int
    projection_class: str
    # commit_seq posture (CUT4_REVIEW_REWORK.md §5.1). True only when the delta
    # was emitted INSIDE core.commit_region.serialized_commit — then commit_seq
    # is the owning episode's finish-line stamp and is source-order proof.
    # False (direct/gate writes outside the region): commit_seq is merely the
    # epoch read at emission — NOT serialized source order; Cut 13 must
    # reconcile unsequenced deltas (or treat them as rebuild-required) before
    # any watermark relies on delta order. Identity-participating.
    sequenced: bool = False
    queued_at: str = ""  # diagnostic only — never part of delta_id
    schema_version: str = PROJECTION_DELTA_SCHEMA_VERSION

    @classmethod
    def build(
        cls,
        *,
        operation: str,
        memory_id: str,
        canonical_path: str,
        canonical_hash: str,
        coord: Iterable[int],
        commit_seq: int,
        projection_class: str,
        sequenced: bool = False,
        queued_at: str | None = None,
    ) -> "ProjectionDelta":
        op = str(operation or "")
        cls_ = str(projection_class or "")
        mid = str(memory_id or "").strip()
        if op not in PROJECTION_OPERATIONS:
            raise ValueError(f"unknown projection operation {op!r}")
        if cls_ not in PROJECTION_CLASSES:
            raise ValueError(f"unknown projection class {cls_!r}")
        if not mid:
            raise ValueError("projection delta requires memory_id")
        if not str(canonical_path or ""):
            raise ValueError("projection delta requires canonical_path")
        c3 = _coord3(coord)
        if queued_at is None:
            from core.symphony_time import now_iso

            queued_at = now_iso()
        return cls(
            delta_id=projection_delta_id(
                operation=op, memory_id=mid, canonical_path=str(canonical_path),
                canonical_hash=str(canonical_hash or ""), coord=c3, commit_seq=int(commit_seq or 0),
                projection_class=cls_, sequenced=bool(sequenced),
            ),
            operation=op,
            memory_id=mid,
            canonical_path=str(canonical_path),
            canonical_hash=str(canonical_hash or ""),
            coord=c3,
            commit_seq=int(commit_seq or 0),
            projection_class=cls_,
            sequenced=bool(sequenced),
            queued_at=str(queued_at),
        )

    def identity_ok(self) -> bool:
        return self.delta_id == projection_delta_id(
            operation=self.operation, memory_id=self.memory_id, canonical_path=self.canonical_path,
            canonical_hash=self.canonical_hash, coord=self.coord, commit_seq=self.commit_seq,
            projection_class=self.projection_class, sequenced=self.sequenced, schema_version=self.schema_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "projection_delta",
            "delta_id": self.delta_id,
            "operation": self.operation,
            "memory_id": self.memory_id,
            "canonical_path": self.canonical_path,
            "canonical_hash": self.canonical_hash,
            "coord": list(self.coord),
            "commit_seq": self.commit_seq,
            "projection_class": self.projection_class,
            "sequenced": self.sequenced,
            "queued_at": self.queued_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ProjectionDelta":
        """Strict parse of one outbox row. Raises ValueError on any defect —
        the diagnostic reader turns that into a surfaced malformed line."""
        if not isinstance(raw, Mapping):
            raise ValueError("row is not an object")
        required = ("delta_id", "operation", "memory_id", "canonical_path", "canonical_hash",
                    "coord", "commit_seq", "projection_class", "sequenced", "schema_version")
        missing = [k for k in required if k not in raw]
        if missing:
            raise ValueError(f"missing fields {missing}")
        if str(raw.get("schema_version")) != PROJECTION_DELTA_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {raw.get('schema_version')!r}")
        delta = cls(
            delta_id=str(raw["delta_id"]),
            operation=str(raw["operation"]),
            memory_id=str(raw["memory_id"]),
            canonical_path=str(raw["canonical_path"]),
            canonical_hash=str(raw["canonical_hash"]),
            coord=_coord3(raw["coord"]),
            commit_seq=int(raw["commit_seq"]),
            projection_class=str(raw["projection_class"]),
            sequenced=bool(raw["sequenced"]),
            queued_at=str(raw.get("queued_at") or ""),
            schema_version=str(raw["schema_version"]),
        )
        if delta.operation not in PROJECTION_OPERATIONS:
            raise ValueError(f"unknown operation {delta.operation!r}")
        if delta.projection_class not in PROJECTION_CLASSES:
            raise ValueError(f"unknown projection_class {delta.projection_class!r}")
        if not delta.identity_ok():
            raise ValueError("delta_id does not match row content")
        return delta


# ── durable append ───────────────────────────────────────────────────────────


def _append_line_fsync(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        if not line.endswith("\n"):
            fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def append_projection_delta(delta: ProjectionDelta, *, root: Path | str | None = None) -> bool:
    """Append one canonical-JSON line, flush, fsync. True on success. On any
    failure return False and let the CALLER call ``mark_projection_dirty`` —
    the canonical write it describes is already valid and stays valid.

    Program 0 (Cut 13 Part B, D-13-50 — the second of the three D-13-5 touches): the
    writer takes the dedicated ``_OUTBOX_LOCK`` then the OS-held outbox lock, BOTH
    NONBLOCKING, through ONE ``outbox_exclusion(blocking=False)``; projector-held
    contention (capture / rotation / bootstrap) ⇒ ``False`` IMMEDIATELY — never a
    timed wait — and the unchanged caller marks dirty through ``_LOCK``, which the
    projector never holds. Bytes / format / fsync unchanged."""
    try:
        line = canonical_json(delta.to_dict())
    except Exception as exc:  # noqa: BLE001 — failure is data; never raise into the write path
        log.error("projection outbox append failed for %s: %s: %s", delta.memory_id, type(exc).__name__, exc)
        return False
    try:
        with outbox_exclusion(root, blocking=False):
            _append_line_fsync(outbox_path(root), line)
        return True
    except OutboxBusy as exc:
        log.warning("projection outbox append refused immediately for %s (%s)", delta.memory_id, exc)
        return False
    except Exception as exc:  # noqa: BLE001 — failure is data; never raise into the write path
        log.error("projection outbox append failed for %s: %s: %s", delta.memory_id, type(exc).__name__, exc)
        return False


def pending_projection_deltas(
    *, root: Path | str | None = None
) -> tuple[tuple[ProjectionDelta, ...], tuple[dict[str, Any], ...]]:
    """Diagnostic reader: (deltas, malformed). A malformed/tampered line is
    SURFACED with its line number and error — never silently treated as
    clean. Does not deduplicate; the projector coalesces by delta_id."""
    path = outbox_path(root)
    if not path.exists():
        return (), ()
    deltas: list[ProjectionDelta] = []
    malformed: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return (), ({"line": 0, "error": f"outbox unreadable: {exc}"},)
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            deltas.append(ProjectionDelta.from_dict(json.loads(raw)))
        except Exception as exc:  # noqa: BLE001
            malformed.append({"line": number, "error": f"{type(exc).__name__}: {exc}"})
    return tuple(deltas), tuple(malformed)


# ── dirty / rebuild-required marker ──────────────────────────────────────────


def mark_projection_dirty(*, reason: str, memory_id: str = "", root: Path | str | None = None) -> bool:
    """Loud, durable 'projection is behind and cannot be trusted as current —
    full rebuild required' marker (LAW-EO-004). Atomic temp + fsync + replace.
    No truth authority. True on success; False (logged) on failure — the
    canonical write is valid regardless."""
    from core.symphony_time import now_iso

    path = projection_dirty_path(root)
    payload = {
        "schema_version": PROJECTION_DIRTY_SCHEMA_VERSION,
        "dirty": True,
        "reason": str(reason or ""),
        "memory_id": str(memory_id or ""),
        "marked_at": now_iso(),
    }
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(canonical_json(payload))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        log.error("PROJECTION DIRTY — rebuild required: %s (memory_id=%s)", reason, memory_id or "-")
        return True
    except Exception as exc:  # noqa: BLE001
        log.critical("could not write projection dirty marker (%s): %s", reason, exc)
        return False


def projection_dirty_state(*, root: Path | str | None = None) -> dict[str, Any]:
    path = projection_dirty_path(root)
    if not path.exists():
        return {"dirty": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — an unreadable marker is itself a dirty condition
        return {"dirty": True, "reason": f"projection dirty marker unreadable: {exc}", "memory_id": ""}
    if not isinstance(data, Mapping):
        return {"dirty": True, "reason": "projection dirty marker malformed", "memory_id": ""}
    return {
        "dirty": bool(data.get("dirty", True)),
        "reason": str(data.get("reason") or ""),
        "memory_id": str(data.get("memory_id") or ""),
        "marked_at": str(data.get("marked_at") or ""),
        "schema_version": str(data.get("schema_version") or ""),
    }


# ── producer helpers: emit outbox work for a COMMITTED canonical file ────────


def projection_class_for(memory_dict: Mapping[str, Any]) -> str:
    """Trait family stays filesystem-only (encoding.NON_SEMANTIC_TYPES);
    everything else is a semantic projection citizen."""
    from memory.encoding import NON_SEMANTIC_TYPES

    return "filesystem_only" if str(memory_dict.get("type") or "").strip().lower() in NON_SEMANTIC_TYPES else "semantic"


def in_serialized_commit_region() -> bool:
    """True when the current thread holds the ONE global commit lock
    (core.commit_region.serialized_commit) — the only place a finish-line
    stamp is lawful, so a delta emitted here may claim ``sequenced``.
    Read-only, failure-soft (False): never an ordering authority itself."""
    try:
        from core import cognition_lock as cl

        return any(getattr(lock, "kind", "") == cl.COMMIT_KIND for lock in cl._stack())
    except Exception:  # noqa: BLE001
        return False


def _root_for_canonical(canonical_path: Path, root: Path | str | None) -> Path | None:
    if root is not None:
        return Path(root)
    from memory.projection_receipt import memory_root_for_canonical_path

    return memory_root_for_canonical_path(canonical_path)


def emit_delta_for_canonical(
    memory_dict: Mapping[str, Any],
    canonical_path: str | Path,
    *,
    operation: str = "upsert",
    commit_seq: int | None = None,
    root: Path | str | None = None,
    sequenced: bool | None = None,
) -> dict[str, Any]:
    """Emit projection work for a canonical file that has ALREADY committed and
    report DURABILITY honestly:

        {"delta": ProjectionDelta | None,
         "dirty_requested": bool,   # a failure made a dirty marker necessary
         "dirty_durable": bool,     # ...and that marker is confirmed on disk
         "durable": bool}           # delta durable OR dirty marker durable

    ``durable`` is the write-ahead-journal closing condition (CUT4_REVIEW_
    REWORK_2 §6): a finalized entry may close only when the projection work is
    durable or durable rebuild-required evidence exists. Reads the exact
    committed bytes and hashes them (never a re-serialised dict); normalises
    coord; classes the record; appends. The outbox root is the memory tree
    the canonical file lives in — resolved from the path BEFORE anything that
    can fail, so a failure marker can never cross profiles (review §5.2).
    ``commit_seq`` defaults to the current epoch (core.timekeeper.
    last_commit_seq, read-only); ``sequenced`` defaults to whether this thread
    is inside the serialized commit region (review §5.1). Never raises; the
    canonical write stays valid whatever happens here (LAW-EO-004)."""
    out: dict[str, Any] = {"delta": None, "dirty_requested": False, "dirty_durable": False, "durable": False}
    memory_id = str(memory_dict.get("id") or "").strip()
    path = Path(canonical_path)
    root = _root_for_canonical(path, root)
    if sequenced is None:
        sequenced = in_serialized_commit_region()
    try:
        raw = path.read_bytes()
        canonical_hash = hashlib.sha256(raw).hexdigest()
        if commit_seq is None:
            try:
                from core.timekeeper import last_commit_seq

                commit_seq = int(last_commit_seq())
            except Exception as exc:  # noqa: BLE001 — epoch unavailable: still emit work
                log.warning("projection delta: last_commit_seq unavailable (%s); using 0", exc)
                commit_seq = 0
        delta = ProjectionDelta.build(
            operation=operation,
            memory_id=memory_id,
            canonical_path=str(path),
            canonical_hash=canonical_hash,
            coord=memory_dict.get("coord") or (),
            commit_seq=int(commit_seq),
            projection_class=projection_class_for(memory_dict),
            sequenced=bool(sequenced),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("projection delta build failed for %s: %s: %s", memory_id, type(exc).__name__, exc)
        out["dirty_requested"] = True
        out["dirty_durable"] = mark_projection_dirty(
            reason=f"projection delta build failed: {type(exc).__name__}: {exc}", memory_id=memory_id, root=root)
        out["durable"] = out["dirty_durable"]
        return out
    if append_projection_delta(delta, root=root):
        out["delta"] = delta
        out["durable"] = True
        return out
    out["dirty_requested"] = True
    out["dirty_durable"] = mark_projection_dirty(
        reason="projection outbox append failed after canonical commit", memory_id=memory_id, root=root)
    out["durable"] = out["dirty_durable"]
    return out


def append_delta_for_canonical(
    memory_dict: Mapping[str, Any],
    canonical_path: str | Path,
    *,
    operation: str = "upsert",
    commit_seq: int | None = None,
    root: Path | str | None = None,
    sequenced: bool | None = None,
) -> ProjectionDelta | None:
    """Thin wrapper over ``emit_delta_for_canonical``: the delta on success,
    None on any failure (a dirty marker was requested in the record's
    profile). Callers that own a write-ahead journal entry must use
    ``emit_delta_for_canonical`` to learn whether the failure evidence is
    DURABLE before closing that entry."""
    return emit_delta_for_canonical(
        memory_dict, canonical_path, operation=operation, commit_seq=commit_seq, root=root, sequenced=sequenced,
    )["delta"]


def publish_final_projection_state(
    memory_id: str,
    canonical_path: str | Path,
    *,
    hook_report: Mapping[str, Any] | None = None,
    root: Path | str | None = None,
    sequenced: bool | None = None,
) -> dict[str, Any]:
    """The write OWNER calls this after every synchronous canonical-mutating
    hook it owns has run (CUT4_REVIEW_REWORK.md §2). It observes the FINAL
    canonical state:

      * re-hashes the new record and emits its final delta (``supersede`` when
        the supersedes hook linked a prior, else ``upsert``) — the LATEST delta
        for a memory_id is the projector's authority; earlier commit-time
        deltas are superseded (idempotent by id, harmless);
      * when the supersedes hook rewrote a prior record whose path it reports,
        emits that prior's own final ``upsert`` delta;
      * when the entity-link hook rewrote peer records it enumerates
        (``entity_linker.peer_rewrites``), emits each peer's own final
        ``upsert`` delta;
      * when a hook mutated additional canonical files it cannot enumerate,
        marks projection dirty — silent peer mutation is not acceptable;
        Cut 14A full rebuild recovers.

    ``sequenced=False`` marks repair of an earlier canonical write as unsequenced
    even when commit-lock exclusion is required. ``None`` retains the normal
    finish-line lock detection.

    Never raises. Returns
        {"delta_id", "projection_dirty", "projection_dirty_durable",
         "peer_delta_ids", "reason", "obligation_satisfied"}
    where ``obligation_satisfied`` is the owner's WAL closing condition
    (CUT4_REVIEW_REWORK_2 §2/§6): every piece of projection work this write
    owed is durable, OR a durable rebuild-required marker covers what is not."""
    out: dict[str, Any] = {"delta_id": None, "projection_dirty": False, "projection_dirty_durable": False,
                           "peer_delta_ids": [], "reason": "", "obligation_satisfied": False}
    path = Path(canonical_path)
    root = _root_for_canonical(path, root)
    report = dict(hook_report or {})
    work_missing = False  # some owed projection work is NOT durable

    def _dirty(reason: str, mid: str) -> None:
        out["projection_dirty"] = True
        if mark_projection_dirty(reason=reason, memory_id=mid, root=root):
            out["projection_dirty_durable"] = True

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _dirty(f"final canonical state unreadable after hooks: {type(exc).__name__}: {exc}", memory_id)
        out["reason"] = "final canonical state unreadable"
        out["obligation_satisfied"] = bool(out["projection_dirty_durable"])
        return out
    supersedes = report.get("supersedes") if isinstance(report.get("supersedes"), Mapping) else None
    superseded_prior = bool(supersedes and supersedes.get("ok"))
    emitted = emit_delta_for_canonical(record, path, operation="supersede" if superseded_prior else "upsert", root=root, sequenced=sequenced)
    if emitted["delta"] is None:
        out["projection_dirty"] = True
        out["projection_dirty_durable"] = out["projection_dirty_durable"] or bool(emitted["dirty_durable"])
        out["reason"] = "final delta append failed"
        work_missing = True
    else:
        out["delta_id"] = emitted["delta"].delta_id
    # prior record rewritten by the supersedes hook: represent it (its path is known)
    if superseded_prior:
        prior_path = supersedes.get("prior_path")
        prior_id = str(supersedes.get("prior_id") or "")
        if prior_path and Path(prior_path).exists():
            try:
                prior_record = json.loads(Path(prior_path).read_text(encoding="utf-8"))
                prior_emitted = emit_delta_for_canonical(prior_record, prior_path, operation="upsert", root=root, sequenced=sequenced)
                if prior_emitted["delta"] is not None:
                    out["peer_delta_ids"].append(prior_emitted["delta"].delta_id)
                else:
                    out["projection_dirty"] = True
                    out["projection_dirty_durable"] = out["projection_dirty_durable"] or bool(prior_emitted["dirty_durable"])
                    work_missing = True
            except Exception as exc:  # noqa: BLE001
                _dirty(f"superseded prior record unreadable: {type(exc).__name__}", prior_id)
                work_missing = True
        else:
            _dirty("supersedes hook rewrote a prior record whose path is unknown", prior_id)
            work_missing = True
    # peer canonical files rewritten by the entity linker: the linker reports
    # every peer it rewrote (id + path), so each is represented as its own
    # final ``upsert`` delta. Only a peer mutation the linker could NOT
    # enumerate (no ``peer_rewrites`` in the report at all, or a peer whose
    # delta could not be emitted) forces rebuild-required.
    try:
        links = int(report.get("entity_links") or 0)
    except (TypeError, ValueError):
        links = 0
    if links > 0:
        linker = report.get("entity_linker") if isinstance(report.get("entity_linker"), Mapping) else {}
        unrepresented = _emit_entity_link_peer_deltas(linker, out, root, sequenced=sequenced)
        if unrepresented:
            _dirty(
                f"entity-link hook mutated {unrepresented} peer canonical file(s) not represented as projection work",
                memory_id,
            )
            out["reason"] = out["reason"] or "peer canonical mutation"
            work_missing = True
    # Rework 3 (§4): an UNKNOWN mutation set is not a zero mutation set. Any
    # hook whose outcome is uncertain (exception after it started, failed /
    # reverted supersedes write, hook runner failure after hooks began, or a
    # report that says so outright) requires durable rebuild-required evidence.
    uncertain_reasons = _hook_uncertainty(report)
    if uncertain_reasons:
        _dirty("post-commit hook mutation set uncertain: " + "; ".join(uncertain_reasons), memory_id)
        out["reason"] = out["reason"] or "hook mutation uncertain"
        work_missing = True
    # closing condition: nothing owed is missing, OR the durable dirty marker covers it
    out["obligation_satisfied"] = (not work_missing) or bool(out["projection_dirty_durable"])
    return out


def _emit_entity_link_peer_deltas(linker: Mapping[str, Any], out: dict[str, Any], root: Path | None,
                                *, sequenced: bool | None = None) -> int:
    """Emit a final ``upsert`` delta for every peer canonical file the entity
    linker reports it rewrote. Returns how many peer mutations are NOT
    represented as durable projection work: every rewrite when the linker did
    not enumerate them at all, else each peer whose final state could not be
    read or whose delta did not append. ``out`` gains the peer delta ids."""
    if "peer_rewrites" not in linker:
        # a linker that cannot enumerate its mutations: nothing is representable
        try:
            return max(1, int(linker.get("links_made") or 1))
        except (TypeError, ValueError):
            return 1
    unrepresented = 0
    for peer in linker.get("peer_rewrites") or []:
        if not isinstance(peer, Mapping):
            unrepresented += 1
            continue
        peer_path = peer.get("path")
        if not peer_path or not Path(peer_path).exists():
            unrepresented += 1
            continue
        try:
            peer_record = json.loads(Path(peer_path).read_text(encoding="utf-8"))
            peer_emitted = emit_delta_for_canonical(peer_record, peer_path, operation="upsert", root=root, sequenced=sequenced)
        except Exception:  # noqa: BLE001
            unrepresented += 1
            continue
        if peer_emitted["delta"] is None:
            out["projection_dirty_durable"] = out["projection_dirty_durable"] or bool(peer_emitted["dirty_durable"])
            unrepresented += 1
            continue
        out["peer_delta_ids"].append(peer_emitted["delta"].delta_id)
    return unrepresented


def _hook_uncertainty(report: Mapping[str, Any]) -> list[str]:
    """Reasons a hook report's mutation set is NOT mechanically known."""
    reasons: list[str] = []
    if not report:
        return reasons
    if report.get("mutation_uncertain"):
        reasons.append("hook report flagged uncertain")
    linker = report.get("entity_linker")
    if isinstance(linker, Mapping) and linker.get("mutation_uncertain"):
        reasons.append(f"entity_linker: {linker.get('error') or 'uncertain'}")
    sup = report.get("supersedes")
    if isinstance(sup, Mapping) and sup.get("mutation_uncertain"):
        reasons.append(f"supersedes: {sup.get('error') or sup.get('reason') or 'uncertain'}")
    if report.get("ran") and report.get("errors") and not reasons:
        # a hook error while hooks were running, not classified above: conservative
        reasons.append("hook errors: " + "; ".join(str(e) for e in report.get("errors") or []))
    return reasons


def settle_projection_obligation(
    obligation_id: str | None,
    memory_id: str,
    final: Mapping[str, Any],
    *,
    steps: tuple[str, ...] = (),
) -> bool:
    """Close the owner's projection-obligation journal entry iff the final
    publication satisfied it (final work durable or durable dirty evidence).
    Otherwise the entry stays open — the next boot marks projection dirty
    for the record. Returns True when the entry was closed. Never raises."""
    if not obligation_id:
        return False
    if not bool(final.get("obligation_satisfied")):
        log.error(
            "projection obligation for %s NOT satisfied (delta=%s dirty=%s durable=%s) — journal entry left open",
            memory_id, final.get("delta_id"), final.get("projection_dirty"), final.get("projection_dirty_durable"),
        )
        return False
    try:
        from memory.encoding_journal import journal_close

        return bool(journal_close(obligation_id, memory_id, ["projection_obligation:final", *steps]))
    except Exception as exc:  # noqa: BLE001
        log.error("projection obligation close failed for %s: %s", memory_id, exc)
        return False


def open_projection_obligation(memory_id: str, canonical_path: str | Path, owner: str) -> dict[str, Any]:
    """Open the owner's durable projection obligation BEFORE canonical-
    mutating post-commit hooks and report the ACTUAL durability posture
    (CUT4_REVIEW_REWORK_3 §3):

        {"journal_id": str | None,
         "channel": "wal" | "dirty" | "none",
         "durable": bool}

    ``wal``   — the obligation journal entry is durable (journal_id set);
    ``dirty`` — the journal could not be written, but a durable rebuild-
                required marker now covers the record's profile;
    ``none``  — NEITHER is durable: the hook window is UNPROTECTED and the
                owner must not enter it (skip the mutating hooks, or refuse /
                degrade before a destructive write). Never raises."""
    posture: dict[str, Any] = {"journal_id": None, "channel": "none", "durable": False}
    try:
        from memory.encoding_journal import journal_open_obligation

        jid = journal_open_obligation(memory_id, str(canonical_path), owner)
    except Exception as exc:  # noqa: BLE001
        log.error("projection obligation open raised for %s: %s", memory_id, exc)
        jid = None
    if jid:
        posture.update(journal_id=jid, channel="wal", durable=True)
        return posture
    root = _root_for_canonical(Path(canonical_path), None) if str(canonical_path or "") else None
    if mark_projection_dirty(
        reason="projection obligation could not be journaled before post-commit hooks",
        memory_id=memory_id, root=root,
    ):
        posture.update(channel="dirty", durable=True)
        return posture
    log.critical(
        "projection obligation for %s has NO durable recovery channel (journal AND dirty marker failed) — "
        "the hook window is unprotected", memory_id,
    )
    return posture


# ── Program 0 (Cut 13 Part B) — the D-13-5 producer-freeze exception: EXACTLY three touches ──
#   (1) clear_projection_dirty — compare-and-clear on the exact marker bytes (asserted under the
#       publication authority token);
#   (2) the D-13-50 fast-fail writer seam: outbox_exclusion(_OUTBOX_LOCK → OS-held
#       <memory>/indexes/projection_delta.lock), nonblocking for writers, blocking (bounded) for the
#       projector — used by append_projection_delta above;
#   (3) the D-13-48/56 coherent capture + D-13-43 rotate-first helpers (projector side, same order).
# Everything else in this Cut-4 module is untouched.

_OUTBOX_LOCK = threading.Lock()            # D-13-50: dedicated; NEVER the dirty-marker _LOCK; a plain Lock (no re-acquire)
OUTBOX_LOCK_FILE = "projection_delta.lock"   # <memory>/indexes/projection_delta.lock — stable infrastructure file (D-13-75)
_PROJECTOR_LOCK_WAIT = 30.0                 # projector side: blocking, BOUNDED
ZERO_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"   # sha256(b"")
OUTBOX_EPOCH_FILE = "outbox_epoch.json"          # <memory>/projection/…
OUTBOX_CURSOR_FILE = "consumed.json"
OUTBOX_RECOVERY_FILE = "outbox_recovery.jsonl"
OUTBOX_MALFORMED_FILE = "outbox_malformed.json"
OUTBOX_BOOTSTRAP_INTENT_FILE = "outbox_bootstrap_intent.json"


class OutboxBusy(RuntimeError):
    """The outbox append/rotation exclusion is held elsewhere (writer: refuse immediately)."""


class OutboxCorrupt(RuntimeError):
    """ESTABLISHED-state verification failure (identity / prefix / offset / short read /
    truncation / missing) — recovery is EXCLUSIVELY the rotate-first law of D-13-43."""


def outbox_lock_path(root: Path | str | None = None) -> Path:
    return _memory_root(root) / "indexes" / OUTBOX_LOCK_FILE


def outbox_quarantine_path(root: Path | str | None, epoch: int, n: int = 0) -> Path:
    suffix = f".quarantine-{int(epoch)}" + (f"-{n}" if n else "")
    return outbox_path(root).with_name(outbox_path(root).name + suffix)


@dataclass(eq=False)
class OutboxAuthority:
    """The ONE outbox-exclusion token (D-13-56): in-process ``_OUTBOX_LOCK`` + the OS lock,
    acquired exactly once; ``_locked`` helpers assert it and never re-acquire."""

    memory_root: Path
    _fd: int
    live: bool = True

    def close(self) -> None:
        if self.live:
            self.live = False
            from memory.projection.generation import _unlock
            _unlock(self._fd)
            os.close(self._fd)


def _assert_outbox_authority(oa: Any, root: Path | str | None) -> None:
    if not isinstance(oa, OutboxAuthority) or not oa.live:
        raise OutboxBusy("stage called without a live outbox-exclusion token")
    if oa.memory_root != _memory_root(root):
        raise OutboxBusy("outbox-exclusion token belongs to a different memory root")


@contextmanager
def outbox_exclusion(root: Path | str | None, *, blocking: bool) -> Iterator[OutboxAuthority]:
    """Acquire ``_OUTBOX_LOCK`` THEN the OS-held exclusive ``projection_delta.lock`` EXACTLY
    once (D-13-48/50/56). ``blocking=False`` (writers): any contention ⇒ ``OutboxBusy``
    immediately. ``blocking=True`` (projector): bounded wait; may wait for an append inside
    its short critical section. Same order for appenders, capture, rotation, bootstrap."""
    from memory.projection.generation import _open_lock_handle, _try_lock

    acquired = _OUTBOX_LOCK.acquire(timeout=_PROJECTOR_LOCK_WAIT) if blocking else _OUTBOX_LOCK.acquire(blocking=False)
    if not acquired:
        raise OutboxBusy("in-process outbox exclusion is held (projector capture/rotation or another writer)")
    try:
        fd = _open_lock_handle(outbox_lock_path(root))
        deadline = time.monotonic() + _PROJECTOR_LOCK_WAIT
        while not _try_lock(fd, exclusive=True):
            if not blocking or time.monotonic() >= deadline:
                os.close(fd)
                raise OutboxBusy("OS outbox lock is held by another process")
            time.sleep(0.005)
        oa = OutboxAuthority(memory_root=_memory_root(root), _fd=fd)
        try:
            yield oa
        finally:
            oa.close()
    finally:
        _OUTBOX_LOCK.release()


def _assert_authority_for_root(authority: Any, root: Path | str | None) -> None:
    """D-13-33 (review a045ec4a §4): the publication token authorises ONE profile — every
    Program-0 helper that takes both a token and a target root binds them, so a live authority
    for profile A can never authorise a mutation/capture in profile B."""
    from memory.projection.generation import assert_authority

    assert_authority(authority, _memory_root(root) / "projection")


def clear_projection_dirty(*, root: Path | str | None, expected_marker_sha256: str, authority: Any) -> bool:
    """D-13-5 (1): compare-and-clear on the EXACT bytes of the dirty marker observed BEFORE
    the build started. Asserted under the publication authority token; taken under the
    dirty-marker ``_LOCK`` so a concurrent ``mark_projection_dirty`` cannot interleave.
    A marker rewritten since (any byte) is NOT cleared — the next drain rebuilds. The token is
    bound to THIS profile (review a045ec4a §4)."""
    from memory.projection.generation import sync_dir

    _assert_authority_for_root(authority, root)
    path = projection_dirty_path(root)
    with _LOCK:
        if not path.exists():
            return False
        try:
            current = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return False
        if current != str(expected_marker_sha256):
            return False
        path.unlink()
        sync_dir(path.parent)
        return True


@dataclass(frozen=True, kw_only=True)
class OutboxSnapshot:
    """D-13-48: ONE coherent read of exactly ``captured_size`` bytes under the exclusion.
    ``observed_prefix_sha256`` = sha256 of ALL captured bytes; ``complete_boundary`` = byte
    after the final LF (≤ captured_size); ``consumed_prefix_sha256`` = sha256([0, boundary));
    ``bytes_to_parse`` = [saved_offset, boundary). Immutable — settlement persists these
    values and never re-hashes a moving file."""

    epoch: int
    file_identity: str
    captured_size: int
    observed_prefix_sha256: str
    complete_boundary: int
    consumed_prefix_sha256: str
    bytes_to_parse: bytes
    saved_offset: int = 0
    prefix_bytes_sha256: str = ""       # sha256([0, saved_offset)) — the consumed prefix as re-verified at capture


@dataclass(frozen=True, kw_only=True)
class OutboxEpoch:
    epoch: int
    file_identity: str
    origin: str                          # "virgin" | "legacy_cut4" | "rotation"
    established_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"epoch": int(self.epoch), "file_identity": self.file_identity, "origin": self.origin, "established_at": self.established_at}


def outbox_file_identity_of(fd: int) -> str:
    st = os.fstat(fd)
    if os.name == "posix":
        return f"{st.st_dev}:{st.st_ino}"
    return f"win:{getattr(st, 'st_dev', 0)}:{getattr(st, 'st_ino', 0)}"   # pragma: no cover - Windows volume + file index


def capture_outbox_snapshot_locked(*, root: Path | str | None, epoch: int, saved_offset: int,
                                   prior_observed: tuple[int, str] | None, consumed_prefix_expected: str | None,
                                   outbox_authority: Any) -> OutboxSnapshot:
    """The ONLY capture primitive (cursor-free; D-13-56). Under the held exclusion: open the
    outbox once → fstat identity + size → read exactly that many bytes from THAT descriptor →
    verify every previously observed byte and the consumed prefix (established state) →
    hashes/boundary. Missing / short read / truncation / prefix mismatch / mid-line offset ⇒
    ``OutboxCorrupt`` (rotate-first recovery, D-13-43)."""
    _assert_outbox_authority(outbox_authority, root)
    path = outbox_path(root)
    try:
        fd = os.open(path, os.O_RDONLY | _O_BINARY)
    except FileNotFoundError as exc:
        raise OutboxCorrupt(f"outbox missing: {path}") from exc
    try:
        identity = outbox_file_identity_of(fd)
        size = os.fstat(fd).st_size
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                raise OutboxCorrupt(f"short read: expected {size} bytes, got {size - remaining}")
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) != size:
        raise OutboxCorrupt("size inconsistency during capture")
    if prior_observed is not None:
        observed_size, observed_sha = int(prior_observed[0]), str(prior_observed[1])
        if size < observed_size:
            raise OutboxCorrupt(f"outbox truncated: {size} < previously observed {observed_size}")
        if hashlib.sha256(data[:observed_size]).hexdigest() != observed_sha:
            raise OutboxCorrupt("previously observed bytes changed (mutation / loss / replacement)")
    saved_offset = int(saved_offset)
    if saved_offset < 0 or saved_offset > size:
        raise OutboxCorrupt(f"saved offset {saved_offset} outside the file ({size} bytes)")
    if saved_offset > 0 and data[saved_offset - 1: saved_offset] != b"\n":
        raise OutboxCorrupt("saved offset is not at a line boundary")
    prefix_sha = hashlib.sha256(data[:saved_offset]).hexdigest()
    if consumed_prefix_expected is not None and prefix_sha != str(consumed_prefix_expected):
        raise OutboxCorrupt("consumed prefix hash mismatch")
    boundary = max(saved_offset, data.rfind(b"\n") + 1)
    return OutboxSnapshot(
        epoch=int(epoch), file_identity=identity, captured_size=size,
        observed_prefix_sha256=hashlib.sha256(data).hexdigest(), complete_boundary=boundary,
        consumed_prefix_sha256=hashlib.sha256(data[:boundary]).hexdigest(),
        bytes_to_parse=data[saved_offset:boundary], saved_offset=saved_offset, prefix_bytes_sha256=prefix_sha,
    )


def capture_outbox_snapshot(*, root: Path | str | None, cursor: Any, authority: Any) -> OutboxSnapshot:
    """Established-state convenience: ONE blocking exclusion + the ``_locked`` capture from the
    cursor; the physical identity must match the cursor's (D-13-43). The token is bound to THIS
    profile (review a045ec4a §4)."""
    _assert_authority_for_root(authority, root)
    with outbox_exclusion(root, blocking=True) as oa:
        snap = capture_outbox_snapshot_locked(
            root=root, epoch=int(cursor.outbox_file_epoch), saved_offset=int(cursor.outbox_byte_offset),
            prior_observed=(int(cursor.observed_size), str(cursor.observed_prefix_sha256)),
            consumed_prefix_expected=str(cursor.consumed_prefix_sha256), outbox_authority=oa)
    if snap.file_identity != str(cursor.outbox_file_identity):
        raise OutboxCorrupt(f"outbox physical identity changed ({cursor.outbox_file_identity} → {snap.file_identity})")
    return snap


def _projection_dir(root: Path | str | None) -> Path:
    return _memory_root(root) / "projection"


def _append_recovery_row(root: Path | str | None, row: Mapping[str, Any]) -> None:
    from memory.projection.generation import sync_dir

    path = _projection_dir(root) / OUTBOX_RECOVERY_FILE
    _append_line_fsync(path, canonical_json(dict(row)))
    sync_dir(path.parent)


def pending_outbox_recovery(root: Path | str | None) -> dict[str, Any] | None:
    """The last ``begin`` recovery-intent row without its final ``completed`` receipt (D-13-43: the
    receipt is written only after the recovery REBUILD published), else None. The returned dict
    carries ``rotated`` = True when the rotation itself (quarantine → new file → epoch + zero
    cursor) is already durable, so a restart skips straight to the rebuild."""
    path = _projection_dir(root) / OUTBOX_RECOVERY_FILE
    if not path.exists():
        return None
    pending: dict[str, Any] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except Exception:  # noqa: BLE001 — a torn recovery tail is evidence; the last complete row decides
            continue
        if not isinstance(row, Mapping):
            continue
        stage = row.get("stage")
        if stage == "begin":
            pending = dict(row)
            pending["rotated"] = False
        elif pending is not None and int(row.get("new_epoch", -1)) == int(pending.get("new_epoch", -2)):
            if stage == "rotated":
                pending["rotated"] = True
            elif stage == "completed":
                pending = None
    return pending


def complete_outbox_recovery(root: Path | str | None, pending: Mapping[str, Any]) -> None:
    """The final recovery receipt — appended by the drain ONLY after the recovery rebuild's
    publication barrier and cursor settlement (D-13-43)."""
    from core.symphony_time import now_iso

    _append_recovery_row(root, {"stage": "completed", "old_epoch": pending.get("old_epoch"), "old_identity": pending.get("old_identity"),
                                "new_epoch": int(pending.get("new_epoch")), "at": now_iso()})


def rotate_outbox_epoch_locked(*, root: Path | str | None, old_state: Mapping[str, Any], outbox_authority: Any) -> OutboxEpoch:
    """D-13-43 rotate FIRST (under the ONE held exclusion; idempotent per step so a restart
    resumes from the recovery-intent row): begin row → quarantine rename of the corrupt
    outbox (+ dir sync) → create + fsync the new empty outbox → publish the new epoch +
    zero cursor (strict, parent-synced) → ``rotated`` row. The full rebuild happens AFTER
    the caller releases the exclusion (deltas appended meanwhile stay pending) and the
    drain appends the final ``completed`` receipt only after that rebuild's barrier."""
    from core.symphony_time import now_iso
    from memory.projection.generation import _strict_write_json, sync_dir

    _assert_outbox_authority(outbox_authority, root)
    old_epoch = int(old_state.get("old_epoch") or 0)
    old_identity = old_state.get("old_identity")
    new_epoch = int(old_state.get("new_epoch") or (old_epoch + 1))
    proj = _projection_dir(root)
    proj.mkdir(parents=True, exist_ok=True)
    resumed = bool(old_state.get("stage") == "begin")
    if not resumed:
        _append_recovery_row(root, {"stage": "begin", "old_epoch": old_epoch, "old_identity": old_identity, "new_epoch": new_epoch,
                                    "reason": str(old_state.get("reason") or "outbox_corrupt"), "at": now_iso()})
    path = outbox_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # quarantine rename — only the OLD file (by identity when known); a crashed attempt may already have moved it
    # (resume: an epoch file already naming new_epoch with the CURRENT identity proves rename + create are done)
    already_rotated = False
    if resumed and (proj / OUTBOX_EPOCH_FILE).exists() and path.exists():
        try:
            ep = json.loads((proj / OUTBOX_EPOCH_FILE).read_text(encoding="utf-8"))
            fd = os.open(path, os.O_RDONLY | _O_BINARY)
            try:
                already_rotated = int(ep.get("epoch", -1)) >= new_epoch and str(ep.get("file_identity")) == outbox_file_identity_of(fd)
            finally:
                os.close(fd)
        except Exception:  # noqa: BLE001 — unreadable epoch file ⇒ fall through to identity-based rename
            already_rotated = False
    if path.exists() and not already_rotated:
        fd = os.open(path, os.O_RDONLY | _O_BINARY)
        try:
            ident = outbox_file_identity_of(fd)
        finally:
            os.close(fd)
        # a FRESH rotation quarantines whatever sits at the path (the corrupt / replaced file); a RESUMED one
        # quarantines only the old identity (a new file created by the crashed attempt is kept)
        if (not resumed) or old_identity is None or ident == str(old_identity):
            n = 0
            target = outbox_quarantine_path(root, old_epoch, n)
            while target.exists():
                n += 1
                target = outbox_quarantine_path(root, old_epoch, n)
            os.replace(path, target)
            sync_dir(path.parent)
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o644)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        sync_dir(path.parent)
    fd = os.open(path, os.O_RDONLY | _O_BINARY)
    try:
        identity = outbox_file_identity_of(fd)
    finally:
        os.close(fd)
    epoch = OutboxEpoch(epoch=new_epoch, file_identity=identity, origin="rotation", established_at=now_iso())
    _strict_write_json(proj / OUTBOX_EPOCH_FILE, epoch.to_dict())
    _strict_write_json(proj / OUTBOX_CURSOR_FILE, {
        "outbox_file_epoch": new_epoch, "outbox_file_identity": identity, "observed_size": 0,
        "observed_prefix_sha256": ZERO_SHA256, "outbox_byte_offset": 0, "consumed_prefix_sha256": ZERO_SHA256,
        "last_delta_id": None, "generation_id": None,
    })
    _append_recovery_row(root, {"stage": "rotated", "old_epoch": old_epoch, "old_identity": old_identity, "new_epoch": new_epoch,
                                "new_identity": identity, "at": now_iso()})
    return epoch


def rotate_outbox_epoch(*, root: Path | str | None, old_state: Mapping[str, Any], authority: Any) -> OutboxEpoch:
    """Convenience: ONE blocking exclusion + ``rotate_outbox_epoch_locked`` (asserts the publication
    authority, bound to THIS profile — review a045ec4a §4)."""
    _assert_authority_for_root(authority, root)
    with outbox_exclusion(root, blocking=True) as oa:
        return rotate_outbox_epoch_locked(root=root, old_state=old_state, outbox_authority=oa)
