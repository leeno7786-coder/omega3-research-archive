"""Cut 13 Task A4 — the generation builder from a CLOSED canonical snapshot
(memory-organ plan d1673531, D-13-14/20/21/26/27/35/36/39/40/44/64/68/74).

    build_generation(root, *, reason, authority) -> Candidate | BuildRejected

Protocol: obligations clean → the ONE clock capture (``last_coverage_clock()`` = the
landed frontier) → STRICT canonical directory membership → hardlink/copy every
canonical file + the SourceLedger store + the selected registry authority snapshot into
the profile-form ``work_dir/input_profile/memory/…`` → coord index BUILT from the captured
files → ``compile_memory_graph(source_root, output_dir)`` → deterministic
``rebuild_sram_from_graph(source_root, output_dir, registry_input)`` → page-0 telemetry →
closure proof (membership + bytes + ledger + authority) → obligations clean again →
``Candidate`` (the publisher writes the receipt). ``work_dir`` is removed on every exit
path. NO embedding call, NO outbox knowledge (Program 0), NO write outside
``work_dir`` / ``candidate_dir``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.cognition_lock import commit_lock
from core.timekeeper import CoverageClock, CoverageClockUnavailable, last_coverage_clock
from memory.dewey_map import LOBE_MAP
from memory.projection.generation import (
    Candidate,
    CandidateManifest,
    GenerationContent,
    GenerationIntegrityError,
    PlaneEntry,
    SourceInputContent,
    assert_authority,
    candidate_dir_name,
    generation_id_for,
    projection_root,
    resolve_memory_root,
    sha256_bytes,
    sha256_file,
    sync_dir,
)
from memory.taxonomy.normalize import canonical_json

__all__ = ["BuildRejected", "CanonicalMember", "MembershipError", "build_generation", "scan_canonical_membership",
           "CANONICAL_CORPUS_SCHEMA", "SOURCE_LEDGER_SCHEMA", "WORK_DIR_PREFIX"]

_log = logging.getLogger(__name__)

CANONICAL_CORPUS_SCHEMA = "omega.canonical_corpus.v1"
SOURCE_LEDGER_SCHEMA = "omega.source_ledger.v1"
WORK_DIR_PREFIX = "work-"
_ADMISSIBLE_SKIP_PARENTS = ("indexes", "schema")


@dataclass(frozen=True, kw_only=True)
class BuildRejected:
    reason: Literal["snapshot_moved", "blocked_on_obligations", "unscorable_coordinates", "registry_moved",
                    "canonical_membership_error", "source_ledger_moved", "coverage_clock_unavailable"]
    detail: dict
    attempts: int


class MembershipError(GenerationIntegrityError):
    """canonical_membership_error{kind, path, memory_id} — publication is BLOCKED, never skipped."""

    def __init__(self, kind: str, path: str, memory_id: str | None) -> None:
        self.kind, self.path, self.memory_id = kind, path, memory_id
        super().__init__(f"canonical_membership_error kind={kind} path={path} memory_id={memory_id}")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "path": self.path, "memory_id": self.memory_id}


@dataclass(frozen=True, kw_only=True)
class CanonicalMember:
    rel_path: str          # profile-relative POSIX path (memory/<lobe>/…)
    memory_id: str
    lobe: int
    size: int
    mtime_ns: int
    sha256: str            # raw file bytes (closure proof: movement detection)
    structural_sha256: str # sha256(canonical_json(record minus `embeddings`)) — the identity input (vectors are never read)


def _lobe_dir_for(rel_path: str) -> str | None:
    parts = Path(rel_path).parts
    return parts[1] if len(parts) >= 3 and parts[0] == "memory" else None


def scan_canonical_membership(memory_root: Path) -> list[CanonicalMember]:
    """The reindex admissibility rule (the twelve LOBE_MAP directories, every ``*.json``
    file, parents ``indexes``/``schema`` and the exact reflection-window archive
    subtree skipped) made STRICT (D-13-26): unreadable JSON,
    schema-invalid records, duplicate memory ids and AMBIGUOUS membership — (i) one id in two
    files, (ii) ``id`` disagreeing with the file name per the validate_write naming rule,
    (iii) ``coord[0]`` disagreeing with the lobe directory, (iv) an object without ``id`` —
    raise MembershipError. Never skipped."""
    from memory.engine import reindex as RX
    from memory.engine.validate_write import _sanitize_id

    memory_root = Path(memory_root)
    # FAIL CLOSED (D-13-26; review 73646a37 §1): the strict-membership contract depends on the
    # schema check. A schema that is absent OR cannot be loaded is a validation mechanism
    # failure — every record would otherwise be admitted unchecked — so it BLOCKS the
    # generation as canonical_membership_error{kind=schema_unavailable}. (legacy reindex
    # warns and skips; the generation builder never does.)
    schema_rel = "schema/" + Path(RX.SCHEMA_PATH).name
    if not Path(RX.SCHEMA_PATH).exists():
        raise MembershipError("schema_unavailable", schema_rel, None)
    try:
        validator = RX.load_schema()
    except Exception as exc:  # noqa: BLE001 — loader failure blocks; never a silent no-op
        raise MembershipError("schema_unavailable", f"{schema_rel} ({type(exc).__name__}: {exc})", None) from exc
    lobe_by_dir = {v: k for k, v in LOBE_MAP.items()}
    members: list[CanonicalMember] = []
    seen: dict[str, str] = {}
    for dirname in sorted(set(LOBE_MAP.values())):
        base = memory_root / dirname
        if not base.is_dir():
            continue
        for path in sorted(p for p in base.rglob("*.json") if p.is_file()):
            if RX.is_reflection_window_archive(path, memory_root=memory_root):
                continue
            if path.parent.name in _ADMISSIBLE_SKIP_PARENTS:
                continue
            rel = "memory/" + path.relative_to(memory_root).as_posix()
            try:
                data = path.read_bytes()
                meta = json.loads(data.decode("utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                raise MembershipError("unreadable", rel, None)
            if not isinstance(meta, dict):
                raise MembershipError("schema_invalid", rel, None)
            mid = meta.get("id")
            if not isinstance(mid, str) or not mid:
                raise MembershipError("ambiguous_no_id", rel, None)
            try:
                validator.validate(meta)
            except Exception:  # noqa: BLE001 — any validation error blocks
                raise MembershipError("schema_invalid", rel, mid)
            if path.stem != _sanitize_id(mid):
                raise MembershipError("ambiguous_name", rel, mid)
            coord = meta.get("coord")
            if isinstance(coord, (list, tuple)) and coord:
                try:
                    lobe = int(coord[0])
                except (TypeError, ValueError):
                    raise MembershipError("schema_invalid", rel, mid)
                if lobe_by_dir.get(dirname) != lobe:
                    raise MembershipError("ambiguous_lobe", rel, mid)
            else:
                raise MembershipError("schema_invalid", rel, mid)
            if mid in seen:
                raise MembershipError("duplicate", rel, mid)
            seen[mid] = rel
            st = path.stat()
            structural = {k: v for k, v in meta.items() if k != "embeddings"}
            members.append(CanonicalMember(rel_path=rel, memory_id=mid, lobe=lobe, size=st.st_size, mtime_ns=st.st_mtime_ns,
                                           sha256=sha256_bytes(data),
                                           structural_sha256=sha256_bytes(canonical_json(structural).encode("utf-8"))))
    return members


def _corpus_hash(members: list[CanonicalMember]) -> str:
    """The canonical_corpus identity: listing + STRUCTURAL hash per record (the `embeddings`
    field is excluded — the projection never reads vectors, so vectors present / absent /
    mutated yield the same generation; representation-neutral, 16 gate 14)."""
    return sha256_bytes("\n".join(f"{m.rel_path}\t{m.structural_sha256}" for m in sorted(members, key=lambda m: m.rel_path)).encode("utf-8"))


def _tree_listing(base: Path) -> list[tuple[str, str]]:
    if not base.is_dir():
        return []
    return [(p.relative_to(base).as_posix(), sha256_file(p)) for p in sorted(base.rglob("*")) if p.is_file()]


def _listing_hash(listing: list[tuple[str, str]]) -> str:
    return sha256_bytes("\n".join(f"{rel}\t{sha}" for rel, sha in listing).encode("utf-8"))


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)          # same filesystem: point-in-time (canonical writes replace inodes atomically)
    except OSError:
        shutil.copy2(src, dst)


def _obligations_clean() -> tuple[bool, dict]:
    from memory import encoding_journal

    status = encoding_journal.journal_status()
    return bool(status.get("clean")), {k: status.get(k) for k in ("unclosed_count", "malformed_count", "error") if k in status}


def _registry_input_bytes(proj: Path, memory_root: Path) -> tuple[bytes, str]:
    """The builder's registry INPUT (D-13-35/42): the selected authority snapshot; before the
    first authority exists, today's legacy fixed-name registry (migration); else empty."""
    from memory.projection.registry_authority import read_selected_registry_bytes, select_authority

    selected = select_authority(proj)
    if selected is not None:
        return read_selected_registry_bytes(proj), f"authority:{selected[0]}"
    legacy = memory_root / "dme" / "coordinate_registry.json"
    if legacy.is_file():
        return legacy.read_bytes(), "legacy:memory/dme/coordinate_registry.json"
    return b"{}", "empty"


def _prior_semantic_retina(proj: Path, memory_root: Path):
    """The ACTIVE generation's retina, verified against its pinned hashes and the
    encoder contract, so the candidate re-encodes only new or changed texts (audit
    2026-09-28 M08). A prior that cannot be verified is not reused: the candidate
    encodes from text instead, and the cause is logged. The caller holds the
    publication authority, so the active generation cannot be collected mid-read."""
    from memory import retina
    from memory.projection.generation import manifest_state, open_generation_view

    if manifest_state(proj).mode != "generation":
        return None
    try:
        view = open_generation_view(memory_root)
    except GenerationIntegrityError as exc:
        _log.warning("generation build: active generation unavailable for retina reuse (%s)", exc)
        return None
    try:
        return retina.generation_retina(view)
    except retina.RetinaUnavailable as exc:
        _log.warning("generation build: prior retina not verified, encoding every text (%s)", exc)
        return None
    finally:
        view.close()


def build_generation(root: Path | str | None, *, reason: str, authority: Any, builder: str = "cut13",
                     max_attempts: int = 3, snapshot_out: dict | None = None) -> Candidate | BuildRejected:
    """``snapshot_out`` (Program 0, D-13-7 — additive, default None): when a dict is passed the
    builder records the CLOSED snapshot's membership ``{"members": {memory_id: sha256}, "canonical_files": n}``
    so the outbox consumer classifies deltas against the SAME closed snapshot the generation was
    built from (stale hash ⇒ canonical_newer_than_delta; missing ⇒ tombstone). Never part of identity."""
    proj = projection_root(root)
    assert_authority(authority, proj)
    memory_root = resolve_memory_root(root)
    proj.mkdir(parents=True, exist_ok=True)
    last: BuildRejected | None = None
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        work_dir = proj / f"{WORK_DIR_PREFIX}{os.urandom(6).hex()}"
        try:
            outcome = _build_once(proj, memory_root, work_dir, reason=reason, builder=builder, attempt=attempt, snapshot_out=snapshot_out)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)   # work_dir removed on every exit path
        if isinstance(outcome, Candidate):
            return outcome
        last = outcome
        if outcome.reason not in ("snapshot_moved", "source_ledger_moved", "registry_moved"):
            return outcome                                # only closure failures are retried
    return last  # type: ignore[return-value]


def _build_once(proj: Path, memory_root: Path, work_dir: Path, *, reason: str, builder: str, attempt: int,
                snapshot_out: dict | None = None) -> Candidate | BuildRejected:
    def rejected(reason_, **detail):
        return BuildRejected(reason=reason_, detail=detail, attempts=attempt)

    # Canonical capture shares the writer exclusion through its complete receipt.
    # Windows readers must close before a writer can replace the canonical file.
    # Release before compilation so other lanes can keep committing.
    with commit_lock():
        clean, ob = _obligations_clean()
        if not clean:
            return rejected("blocked_on_obligations", **ob)
        try:
            coverage_at_capture: CoverageClock = last_coverage_clock()      # the ONE atomic clock-capture seam (D-13-64/68/74)
        except CoverageClockUnavailable as exc:
            return rejected("coverage_clock_unavailable", error=str(exc))
        input_profile = work_dir / "input_profile"
        input_memory = input_profile / "memory"
        candidate = work_dir / "candidate"
        input_memory.mkdir(parents=True)
        candidate.mkdir(parents=True)
        # ── strict membership + capture ────────────────────────────────────────
        try:
            members = scan_canonical_membership(memory_root)
        except MembershipError as exc:
            return rejected("canonical_membership_error", **exc.to_dict())
        for m in members:
            _link_or_copy(memory_root / Path(m.rel_path).relative_to("memory"), input_profile / m.rel_path)
        ledger_src = memory_root / "source_ledger"
        ledger_listing = _tree_listing(ledger_src)
        for rel, _sha in ledger_listing:
            _link_or_copy(ledger_src / rel, input_memory / "source_ledger" / rel)
        (input_memory / "source_ledger").mkdir(exist_ok=True)
        registry_bytes, registry_origin = _registry_input_bytes(proj, memory_root)
        (input_memory / "dme").mkdir()
        (input_memory / "dme" / "coordinate_registry.json").write_bytes(registry_bytes)
    # ── coord index BUILT from the captured files (directory authority) ─────
    from memory.engine import reindex as RX

    try:
        RX._build_candidate_index(root=input_profile)
    except Exception as exc:  # noqa: BLE001 — e.g. mixed embedding geometry: publication blocked, never skipped
        return rejected("canonical_membership_error", kind="index_build_failed", path=None, memory_id=None, error=str(exc))
    index_path = input_memory / "indexes" / "coord_index.json"
    shutil.copyfile(index_path, candidate / "coord_index.snapshot.json")
    # ── graph + DME from the captured input, into the candidate ─────────────
    from memory.association_strength import association_generation
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.taxonomy.compiler import COMPILER_VERSION, compile_memory_graph

    snap = compile_memory_graph(source_root=input_profile, output_dir=candidate)
    report = rebuild_sram_from_graph(source_root=input_profile, output_dir=candidate,
                                     registry_input=input_memory / "dme" / "coordinate_registry.json", snapshot_hash=snap.snapshot_hash,
                                     retina_prior=_prior_semantic_retina(proj, memory_root))
    if report.unscorable_coordinate_count:
        return rejected("unscorable_coordinates", unscorable_coordinate_count=report.unscorable_coordinate_count,
                        page0_used_by_lobe=report.page0_used_by_lobe, page0_capacity_by_lobe=report.page0_capacity_by_lobe,
                        memory_ids=sorted({mid for mid, _ in report.unscorable})[:50])
    # ── closure proof: live membership + bytes + ledger + authority unchanged ─
    with commit_lock():
        try:
            after = scan_canonical_membership(memory_root)
        except MembershipError as exc:
            return rejected("canonical_membership_error", **exc.to_dict())
        if [(m.rel_path, m.size, m.mtime_ns, m.sha256) for m in members] != [(m.rel_path, m.size, m.mtime_ns, m.sha256) for m in after]:
            return rejected("snapshot_moved", captured=len(members), now=len(after))
        if _tree_listing(ledger_src) != ledger_listing:
            return rejected("source_ledger_moved")
        if _registry_input_bytes(proj, memory_root)[0] != registry_bytes:
            return rejected("registry_moved")
        clean, ob = _obligations_clean()
        if not clean:
            return rejected("blocked_on_obligations", **ob)
        association_generation_hash = association_generation()
    if snapshot_out is not None:                                   # Program 0 classification input (D-13-7)
        snapshot_out["members"] = {m.memory_id: m.sha256 for m in members}
        snapshot_out["canonical_files"] = len(members)
    # ── content identity + candidate ────────────────────────────────────────
    from memory.dme.adapter import DME_SCHEMA_VERSION, GENERATION_PLANE_PATHS
    from memory.dme.lobe_layout import ENTRY_WORDS

    corpus_hash = _corpus_hash(members)
    ledger_hash = _listing_hash(ledger_listing)
    planes = tuple(
        PlaneEntry(plane_id=pid, path=rel, sha256=sha256_file(candidate / rel), rows=report.index.seat_count, width_bits=ENTRY_WORDS * 64,
                   scorer="hop", geometry={"kind": "hop", "edge_class": pid.split("_")[-1]})
        for pid, rel in sorted(GENERATION_PLANE_PATHS.items()))
    content = GenerationContent(
        canonical_corpus_hash=corpus_hash, canonical_files=len(members),
        coord_index_sha256=sha256_file(candidate / "coord_index.snapshot.json"),
        registry_sha256=report.registry_sha256,
        graph_sqlite_sha256=sha256_file(candidate / "graph.sqlite"), graph_snapshot_sha256=sha256_file(candidate / "graph_snapshot.json"),
        graph_snapshot_hash=snap.snapshot_hash, compiler_version=COMPILER_VERSION,
        dme_schema_version=DME_SCHEMA_VERSION, sram_sha256=sha256_file(candidate / "sram.bin"),
        sram_meta_sha256=sha256_file(candidate / "sram_meta.json"), sram_index_sha256=sha256_file(candidate / "sram_index.json"),
        dme_manifest_sha256=sha256_file(candidate / "dme_manifest.json"),
        planes=planes,
        source_inputs=(SourceInputContent(kind="canonical_corpus", sha256=corpus_hash, schema_version=CANONICAL_CORPUS_SCHEMA),
                       SourceInputContent(kind="source_ledger", sha256=ledger_hash, schema_version=SOURCE_LEDGER_SCHEMA)),
        components={"semantic_retina": report.semantic_retina},
        association_generation=association_generation_hash,
    )
    gid = generation_id_for(content)
    manifest = CandidateManifest(content=content, generation_id=gid, snapshot_closure="proved",
                                 page0_capacity_by_lobe=report.page0_capacity_by_lobe, page0_used_by_lobe=report.page0_used_by_lobe,
                                 page0_utilization_by_lobe=report.page0_utilization_by_lobe, page0_overflow_attempts=report.page0_overflow_attempts,
                                 unscorable_coordinate_count=report.unscorable_coordinate_count)
    final = proj / candidate_dir_name(gid)
    if final.exists():
        shutil.rmtree(final)
    os.rename(candidate, final)
    sync_dir(proj)
    origins = {"canonical_corpus": "legacy:memory", "source_ledger": "legacy:memory/source_ledger", "coordinate_registry": registry_origin}
    return Candidate(candidate_dir=final, manifest=manifest, coverage_clock=coverage_at_capture, source_input_origins=origins,
                     builder=builder, reason=reason)
