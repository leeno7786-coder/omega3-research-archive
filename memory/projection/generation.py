"""Cut 13 Task A1 — type-closed generation identity, receipt, publication lineage
rows, intents and validators (memory-organ plan d1673531, D-13-1/12/21/32/34/
38/39/44/45/49/54/55/60/61/64/68).

A *generation* is the immutable directory ``<memory>/projection/gen-<64hex>/``
holding one coherent set of derived artifacts. Its identity is
``generation_id = sha256(canonical_json(GenerationContent))`` — EVERYTHING that
is content is inside ``GenerationContent`` and NOTHING else is (no clock, no
origins, no telemetry, no receipt hash). Coverage travels as ONE
``CoverageClock`` object (``core.timekeeper``) on the receipt, the publication
row and the outer manifest; the numeric watermark fields are DERIVED aliases.

This module is Part A only: no outbox / cursor / drain type may appear here
(D-13-53 freeze boundary). It performs no embedding, no model call, and never
mutates a published generation.
"""

from __future__ import annotations

import ctypes
import dataclasses
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping

# The CRT opens descriptors in TEXT mode on Windows unless told otherwise, translating
# LF<->CRLF on os.read/os.write. Every fd in this contract carries bytes with byte-offset
# accounting, so every os.open here is binary. No-op on POSIX.
_O_BINARY = getattr(os, "O_BINARY", 0)

from core.timekeeper import CoverageClock, CoverageClockUnavailable
from memory.taxonomy.normalize import canonical_json

__all__ = [
    "GenerationIntegrityError", "CoverageClockRegression", "UnreceiptedGeneration", "PublicationLineageTampered",
    "CoverageClock", "PlaneEntry", "SourceInputContent", "GenerationContent", "generation_id_for",
    "CandidateManifest", "GenerationReceipt", "Candidate", "PublicationDiagnostics", "ProjectionManifest",
    "PublicationLineageRow", "PublicationResult", "PublicationIntent", "RegistryAuthorityLineageRow",
    "RepairIntentPayload", "RepairIntent", "RepairLineageRow", "LineageScan",
    "GENERATION_DIR_PREFIX", "CANDIDATE_SUFFIX", "RECEIPT_FILE", "LINEAGE_FILE", "GENESIS_ROW_SHA256",
    "GENERATION_ARTIFACTS", "SEMANTIC_RETINA_ARTIFACTS", "SOURCE_INPUT_KINDS", "PUBLICATION_EVENTS",
    "sha256_bytes", "sha256_file", "row_sha256", "publication_row_bytes", "repair_intent_sha256",
    "receipt_bytes", "read_receipt", "generation_receipt_sha256", "generation_dir_name", "candidate_dir_name",
    "validate_candidate", "validate_generation_directory", "validate_published_generation",
    "append_publication_row", "read_publication_lineage", "sync_dir",
]

# ── constants ────────────────────────────────────────────────────────────────

GENERATION_DIR_PREFIX = "gen-"
CANDIDATE_SUFFIX = ".candidate"
RECEIPT_FILE = "generation.json"
LINEAGE_FILE = "lineage.jsonl"
GENESIS_ROW_SHA256 = "0" * 64
SOURCE_INPUT_KINDS: tuple[str, ...] = ("canonical_corpus", "source_ledger")
PUBLICATION_EVENTS: tuple[str, ...] = (
    "published_new", "reused_existing", "reactivated_retained", "adopted_orphan", "replaced_corrupt",
)
# content field → fixed file name inside a generation directory (D-13-12 / D-13-26 layout)
GENERATION_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("coord_index_sha256", "coord_index.snapshot.json"),
    ("registry_sha256", "coordinate_registry.json"),
    ("graph_sqlite_sha256", "graph.sqlite"),
    ("graph_snapshot_sha256", "graph_snapshot.json"),
    ("sram_sha256", "sram.bin"),
    ("sram_meta_sha256", "sram_meta.json"),
    ("sram_index_sha256", "sram_index.json"),
    ("dme_manifest_sha256", "dme_manifest.json"),
)
# components["semantic_retina"] (audit 2026-09-28 M05): the retina is a pinned generation
# artifact. "built" hashes the codes/index files under the encoder contract; "empty" is a
# corpus with nothing to perceive and carries no retina files.
SEMANTIC_RETINA_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("codes_sha256", "retina_codes.bin"),
    ("index_sha256", "retina_index.json"),
)
_SEMANTIC_RETINA_KEYS = {
    "built": frozenset({"status", "schema", "contract_sha256", "records", "codes_sha256", "index_sha256"}),
    "empty": frozenset({"status", "schema", "contract_sha256", "records"}),
}
_HEX64 = frozenset("0123456789abcdef")


# ── errors ───────────────────────────────────────────────────────────────────

class GenerationIntegrityError(RuntimeError):
    """A generation artifact, manifest, receipt, row or intent is invalid. Fail loud;
    never repaired in place."""


class CoverageClockRegression(GenerationIntegrityError):
    """D-13-58/59: the proposed coverage clock is behind the publication head
    (different epoch, or same epoch with a lower sequence)."""

    def __init__(self, *, head: CoverageClock, candidate: CoverageClock, event: str) -> None:
        self.head, self.candidate, self.event = head, candidate, event
        super().__init__(f"coverage clock regression on {event}: head={head.to_dict()} candidate={candidate.to_dict()}")


class UnreceiptedGeneration(GenerationIntegrityError):
    """D-13-45: a directory without a complete GenerationReceipt is not adoptable."""


class PublicationLineageTampered(GenerationIntegrityError):
    """D-13-51 class (c): a committed lineage row is hash-invalid, reordered or tampered.
    Operator-required recovery; nothing is appended automatically."""


# ── hashing helpers ──────────────────────────────────────────────────────────

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise GenerationIntegrityError(message)


def _require_str(value: Any, name: str) -> str:
    _require(isinstance(value, str), f"{name} must be a str")
    return value


def _require_int(value: Any, name: str, *, minimum: int = 0) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool) and value >= minimum, f"{name} must be an int >= {minimum}")
    return value


def _require_dict(value: Any, name: str) -> dict:
    _require(isinstance(value, dict), f"{name} must be a dict")
    return value


def _relative_path_ok(path: str) -> bool:
    if not isinstance(path, str) or not path or "\\" in path:
        return False
    if os.path.isabs(path) or PurePosixPath(path).is_absolute():
        return False
    parts = PurePosixPath(path).parts
    return bool(parts) and all(p not in ("..", ".", "") for p in parts)


# ── content types (EVERYTHING inside generation_id) ──────────────────────────

@dataclass(frozen=True, kw_only=True)
class PlaneEntry:
    plane_id: str
    path: str            # relative to the generation root
    sha256: str
    rows: int
    width_bits: int
    scorer: str
    geometry: dict

    def __post_init__(self) -> None:
        _require_str(self.plane_id, "plane_id") and _require(bool(self.plane_id), "plane_id must be non-empty")
        _require(_relative_path_ok(self.path), f"plane path must be relative and inside the generation: {self.path!r}")
        _require(_is_hex64(self.sha256), "plane sha256 must be 64 hex")
        _require_int(self.rows, "rows"); _require_int(self.width_bits, "width_bits", minimum=1)
        _require_str(self.scorer, "scorer"); _require_dict(self.geometry, "geometry")

    def to_dict(self) -> dict:
        return {"plane_id": self.plane_id, "path": self.path, "sha256": self.sha256, "rows": self.rows,
                "width_bits": self.width_bits, "scorer": self.scorer, "geometry": dict(self.geometry)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "PlaneEntry":
        d = _require_dict(d, "PlaneEntry")
        return cls(plane_id=d.get("plane_id"), path=d.get("path"), sha256=d.get("sha256"), rows=d.get("rows"),
                   width_bits=d.get("width_bits"), scorer=d.get("scorer"), geometry=d.get("geometry"))


@dataclass(frozen=True, kw_only=True)
class SourceInputContent:
    kind: Literal["canonical_corpus", "source_ledger"]
    sha256: str
    schema_version: str

    def __post_init__(self) -> None:
        _require(self.kind in SOURCE_INPUT_KINDS, f"source input kind must be one of {SOURCE_INPUT_KINDS}: {self.kind!r}")
        _require(_is_hex64(self.sha256), "source input sha256 must be 64 hex")
        _require_str(self.schema_version, "schema_version")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "sha256": self.sha256, "schema_version": self.schema_version}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "SourceInputContent":
        d = _require_dict(d, "SourceInputContent")
        return cls(kind=d.get("kind"), sha256=d.get("sha256"), schema_version=d.get("schema_version"))


@dataclass(frozen=True, kw_only=True)
class GenerationContent:
    """EVERYTHING inside generation_id; nothing else (D-13-34/39). No clock, no
    origins, no telemetry, no receipt hash — those live outside the identity."""

    schema_version: str = "omega.projection_generation.v1"
    canonical_corpus_hash: str
    canonical_files: int
    coord_index_sha256: str
    registry_sha256: str          # sha256 of the FINALIZED effective registry (D-13-39) — the only registry field
    graph_sqlite_sha256: str
    graph_snapshot_sha256: str
    graph_snapshot_hash: str
    compiler_version: str
    dme_schema_version: str
    sram_sha256: str
    sram_meta_sha256: str
    sram_index_sha256: str
    dme_manifest_sha256: str
    planes: tuple[PlaneEntry, ...]
    source_inputs: tuple[SourceInputContent, ...]
    components: dict
    association_generation: str | None = None
    semantic_representation: str = "unfrozen"

    def __post_init__(self) -> None:
        _require_str(self.schema_version, "schema_version")
        for name in ("canonical_corpus_hash", "coord_index_sha256", "registry_sha256", "graph_sqlite_sha256",
                     "graph_snapshot_sha256", "graph_snapshot_hash", "sram_sha256", "sram_meta_sha256",
                     "sram_index_sha256", "dme_manifest_sha256"):
            _require(_is_hex64(getattr(self, name)), f"{name} must be 64 hex")
        _require_int(self.canonical_files, "canonical_files")
        _require_str(self.compiler_version, "compiler_version"); _require_str(self.dme_schema_version, "dme_schema_version")
        planes = tuple(self.planes)
        _require(all(isinstance(p, PlaneEntry) for p in planes), "planes must be PlaneEntry")
        _require(len({p.plane_id for p in planes}) == len(planes), "duplicate plane_id")
        _require(len({p.path for p in planes}) == len(planes), "duplicate plane path")
        object.__setattr__(self, "planes", planes)
        inputs = tuple(self.source_inputs)
        _require(all(isinstance(s, SourceInputContent) for s in inputs), "source_inputs must be SourceInputContent")
        kinds = [s.kind for s in inputs]
        _require(len(set(kinds)) == len(kinds), "duplicate source input kind")
        object.__setattr__(self, "source_inputs", tuple(sorted(inputs, key=lambda s: s.kind)))
        _require_dict(self.components, "components")
        _require(self.association_generation is None or isinstance(self.association_generation, str), "association_generation")
        _require_str(self.semantic_representation, "semantic_representation")

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "canonical_corpus_hash": self.canonical_corpus_hash,
            "canonical_files": self.canonical_files,
            "coord_index_sha256": self.coord_index_sha256,
            "registry_sha256": self.registry_sha256,
            "graph_sqlite_sha256": self.graph_sqlite_sha256,
            "graph_snapshot_sha256": self.graph_snapshot_sha256,
            "graph_snapshot_hash": self.graph_snapshot_hash,
            "compiler_version": self.compiler_version,
            "dme_schema_version": self.dme_schema_version,
            "sram_sha256": self.sram_sha256,
            "sram_meta_sha256": self.sram_meta_sha256,
            "sram_index_sha256": self.sram_index_sha256,
            "dme_manifest_sha256": self.dme_manifest_sha256,
            "planes": [p.to_dict() for p in self.planes],
            "source_inputs": [s.to_dict() for s in self.source_inputs],
            "components": json.loads(canonical_json(self.components)),
            "association_generation": self.association_generation,
            "semantic_representation": self.semantic_representation,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "GenerationContent":
        d = _require_dict(d, "GenerationContent")
        known = {f.name for f in dataclasses.fields(cls)}
        _require(set(d) <= known, f"unknown GenerationContent keys: {sorted(set(d) - known)}")
        kw = {k: v for k, v in d.items() if k not in ("planes", "source_inputs")}
        kw["planes"] = tuple(PlaneEntry.from_dict(p) for p in (d.get("planes") or ()))
        kw["source_inputs"] = tuple(SourceInputContent.from_dict(s) for s in (d.get("source_inputs") or ()))
        try:
            return cls(**kw)
        except TypeError as exc:
            raise GenerationIntegrityError(f"GenerationContent: {exc}") from exc


def generation_id_for(content: GenerationContent) -> str:
    """sha256(canonical_json(content)) — no receipt hash, no origin refs, no diagnostics."""
    _require(isinstance(content, GenerationContent), "generation_id_for requires GenerationContent")
    return sha256_bytes(canonical_json(content.to_dict()).encode("utf-8"))


def generation_dir_name(generation_id: str) -> str:
    _require(_is_hex64(generation_id), "generation_id must be 64 hex")
    return GENERATION_DIR_PREFIX + generation_id


def candidate_dir_name(generation_id: str) -> str:
    return generation_dir_name(generation_id) + CANDIDATE_SUFFIX


# ── candidate / receipt / candidate handoff ──────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class CandidateManifest:
    """D-13-38 — everything validate_candidate needs; NO defaults."""

    content: GenerationContent
    generation_id: str
    snapshot_closure: str
    page0_capacity_by_lobe: dict
    page0_used_by_lobe: dict
    page0_utilization_by_lobe: dict
    page0_overflow_attempts: int
    unscorable_coordinate_count: int

    def __post_init__(self) -> None:
        _require(isinstance(self.content, GenerationContent), "content must be GenerationContent")
        _require(_is_hex64(self.generation_id), "generation_id must be 64 hex")
        _require_str(self.snapshot_closure, "snapshot_closure")
        for name in ("page0_capacity_by_lobe", "page0_used_by_lobe", "page0_utilization_by_lobe"):
            _require_dict(getattr(self, name), name)
        _require_int(self.page0_overflow_attempts, "page0_overflow_attempts")
        _require_int(self.unscorable_coordinate_count, "unscorable_coordinate_count")

    def to_dict(self) -> dict:
        return {
            "content": self.content.to_dict(),
            "generation_id": self.generation_id,
            "snapshot_closure": self.snapshot_closure,
            "page0_capacity_by_lobe": json.loads(canonical_json(self.page0_capacity_by_lobe)),
            "page0_used_by_lobe": json.loads(canonical_json(self.page0_used_by_lobe)),
            "page0_utilization_by_lobe": json.loads(canonical_json(self.page0_utilization_by_lobe)),
            "page0_overflow_attempts": self.page0_overflow_attempts,
            "unscorable_coordinate_count": self.unscorable_coordinate_count,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "CandidateManifest":
        d = _require_dict(d, "CandidateManifest")
        try:
            return cls(
                content=GenerationContent.from_dict(d.get("content")), generation_id=d.get("generation_id"),
                snapshot_closure=d.get("snapshot_closure"), page0_capacity_by_lobe=d.get("page0_capacity_by_lobe"),
                page0_used_by_lobe=d.get("page0_used_by_lobe"), page0_utilization_by_lobe=d.get("page0_utilization_by_lobe"),
                page0_overflow_attempts=d.get("page0_overflow_attempts"), unscorable_coordinate_count=d.get("unscorable_coordinate_count"),
            )
        except TypeError as exc:
            raise GenerationIntegrityError(f"CandidateManifest: {exc}") from exc


def _clock_from(d: Mapping[str, Any], owner: str) -> CoverageClock:
    try:
        clock = CoverageClock.from_dict(d.get("coverage_clock"))
    except ValueError as exc:
        raise GenerationIntegrityError(f"{owner}: {exc}") from exc
    for alias in ("canonical_watermark_commit_seq", "latest_canonical_commit_seq_at_capture"):
        if alias in d:
            _require(d[alias] == clock.commit_seq and not isinstance(d[alias], bool),
                     f"{owner}: alias {alias}={d[alias]!r} disagrees with coverage_clock.commit_seq={clock.commit_seq}")
    return clock


def _set_aliases(obj: Any) -> None:
    object.__setattr__(obj, "canonical_watermark_commit_seq", int(obj.coverage_clock.commit_seq))
    object.__setattr__(obj, "latest_canonical_commit_seq_at_capture", int(obj.coverage_clock.commit_seq))


@dataclass(frozen=True, kw_only=True)
class GenerationReceipt:
    """D-13-45/68 — gen-<id>/generation.json: durable, written BEFORE rename, never
    hashed into identity. ONE clock object; watermark aliases are DERIVED."""

    candidate: CandidateManifest
    coverage_clock: CoverageClock
    source_input_origins: dict
    canonical_watermark_commit_seq: int = field(init=False, compare=False, repr=False)
    latest_canonical_commit_seq_at_capture: int = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        _require(isinstance(self.candidate, CandidateManifest), "candidate must be CandidateManifest")
        _require(isinstance(self.coverage_clock, CoverageClock), "coverage_clock must be CoverageClock")
        _require_dict(self.source_input_origins, "source_input_origins")
        _set_aliases(self)

    def to_dict(self) -> dict:
        return {
            "candidate": self.candidate.to_dict(),
            "coverage_clock": self.coverage_clock.to_dict(),
            "canonical_watermark_commit_seq": self.canonical_watermark_commit_seq,
            "latest_canonical_commit_seq_at_capture": self.latest_canonical_commit_seq_at_capture,
            "source_input_origins": json.loads(canonical_json(self.source_input_origins)),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "GenerationReceipt":
        d = _require_dict(d, "GenerationReceipt")
        _require("candidate" in d and "coverage_clock" in d and "source_input_origins" in d,
                 "GenerationReceipt requires candidate, coverage_clock, source_input_origins")
        return cls(candidate=CandidateManifest.from_dict(d["candidate"]), coverage_clock=_clock_from(d, "GenerationReceipt"),
                   source_input_origins=d["source_input_origins"])


def receipt_bytes(receipt: GenerationReceipt) -> bytes:
    """The exact bytes of gen-<id>/generation.json (canonical JSON, no trailing LF)."""
    return canonical_json(receipt.to_dict()).encode("utf-8")


def read_receipt(gen_dir: Path) -> GenerationReceipt:
    path = Path(gen_dir) / RECEIPT_FILE
    if not path.is_file():
        raise UnreceiptedGeneration(f"no {RECEIPT_FILE} in {gen_dir}")
    try:
        doc = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise GenerationIntegrityError(f"unreadable receipt {path}: {exc}") from exc
    if not isinstance(doc, dict) or "candidate" not in doc or "coverage_clock" not in doc:
        raise UnreceiptedGeneration(f"{path} is not a complete GenerationReceipt (frozen unreceipted policy)")
    return GenerationReceipt.from_dict(doc)


def generation_receipt_sha256(gen_dir: Path) -> str:
    """sha256 of the receipt FILE bytes as written (the value bound by publication rows)."""
    path = Path(gen_dir) / RECEIPT_FILE
    if not path.is_file():
        raise UnreceiptedGeneration(f"no {RECEIPT_FILE} in {gen_dir}")
    return sha256_file(path)


@dataclass(frozen=True, kw_only=True)
class Candidate:
    """D-13-44/68 — the ONE builder result / publisher input (in-memory)."""

    candidate_dir: Path
    manifest: CandidateManifest
    coverage_clock: CoverageClock     # the exact object returned by last_coverage_clock()
    source_input_origins: dict
    builder: str
    reason: str

    def __post_init__(self) -> None:
        _require(isinstance(self.manifest, CandidateManifest), "manifest must be CandidateManifest")
        _require(isinstance(self.coverage_clock, CoverageClock), "coverage_clock must be CoverageClock")
        _require_dict(self.source_input_origins, "source_input_origins")
        object.__setattr__(self, "candidate_dir", Path(self.candidate_dir))


@dataclass(frozen=True, kw_only=True)
class PublicationDiagnostics:
    published_at: str
    dir_sync: str
    backend_at_publish: str
    pending_delta_count: int
    oldest_pending_commit_seq: int | None
    oldest_pending_queued_at: str
    sequenced_consumed: int
    unsequenced_consumed: int


# ── outer manifest ───────────────────────────────────────────────────────────

_MANIFEST_SIMPLE = (
    "schema_version", "generation_id", "generation_dir", "lineage_seq", "parent_generation_id", "snapshot_closure",
    "page0_capacity_by_lobe", "page0_used_by_lobe", "page0_utilization_by_lobe", "page0_overflow_attempts",
    "unscorable_coordinate_count", "source_input_origins", "sequenced_consumed", "unsequenced_consumed",
    "unsequenced_posture", "pending_delta_count", "oldest_pending_commit_seq", "oldest_pending_queued_at",
    "builder", "reason", "published_at", "dir_sync", "backend_at_publish",
)


@dataclass(frozen=True, kw_only=True)
class ProjectionManifest:
    """<memory>/projection/manifest.json (outer, atomically replaced). Content section
    == the generation's receipt content; coverage == the publication-lineage head."""

    schema_version: str = "omega.projection_manifest.v1"
    content: GenerationContent
    generation_id: str
    generation_dir: str
    lineage_seq: int
    parent_generation_id: str | None
    coverage_clock: CoverageClock
    snapshot_closure: str
    canonical_watermark_commit_seq: int = field(init=False, compare=False, repr=False)
    latest_canonical_commit_seq_at_capture: int = field(init=False, compare=False, repr=False)
    page0_capacity_by_lobe: dict
    page0_used_by_lobe: dict
    page0_utilization_by_lobe: dict
    page0_overflow_attempts: int
    unscorable_coordinate_count: int
    source_input_origins: dict
    sequenced_consumed: int
    unsequenced_consumed: int
    unsequenced_posture: str = "covered_by_full_rebuild"
    pending_delta_count: int
    oldest_pending_commit_seq: int | None
    oldest_pending_queued_at: str
    builder: str
    reason: str
    published_at: str
    dir_sync: str
    backend_at_publish: str

    def __post_init__(self) -> None:
        _require(isinstance(self.content, GenerationContent), "content must be GenerationContent")
        _require(_is_hex64(self.generation_id), "generation_id must be 64 hex")
        _require_str(self.generation_dir, "generation_dir")
        _require_int(self.lineage_seq, "lineage_seq")
        _require(isinstance(self.coverage_clock, CoverageClock), "coverage_clock must be CoverageClock")
        _require_str(self.snapshot_closure, "snapshot_closure")
        _require_dict(self.source_input_origins, "source_input_origins")
        _set_aliases(self)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {name: getattr(self, name) for name in _MANIFEST_SIMPLE}
        for name in ("page0_capacity_by_lobe", "page0_used_by_lobe", "page0_utilization_by_lobe", "source_input_origins"):
            d[name] = json.loads(canonical_json(getattr(self, name)))
        d["content"] = self.content.to_dict()
        d["coverage_clock"] = self.coverage_clock.to_dict()
        d["canonical_watermark_commit_seq"] = self.canonical_watermark_commit_seq
        d["latest_canonical_commit_seq_at_capture"] = self.latest_canonical_commit_seq_at_capture
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ProjectionManifest":
        d = _require_dict(d, "ProjectionManifest")
        kw = {name: d.get(name) for name in _MANIFEST_SIMPLE if name in d}
        try:
            return cls(content=GenerationContent.from_dict(d.get("content")), coverage_clock=_clock_from(d, "ProjectionManifest"), **kw)
        except TypeError as exc:
            raise GenerationIntegrityError(f"ProjectionManifest: {exc}") from exc


# ── publication lineage row / result / intent ────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class PublicationLineageRow:
    """D-13-49/55/68 — projection/lineage.jsonl: the durable activation / coverage /
    receipt-binding authority. ONE clock object; aliases derived."""

    seq: int
    event: Literal["published_new", "reused_existing", "reactivated_retained", "adopted_orphan", "replaced_corrupt"]
    generation_id: str
    generation_receipt_sha256: str
    repair_row_sha256: str | None
    coverage_clock: CoverageClock
    source_input_origins: dict[str, str]
    parent_generation_id: str | None
    previous_row_sha256: str
    published_at: str
    canonical_watermark_commit_seq: int = field(init=False, compare=False, repr=False)
    latest_canonical_commit_seq_at_capture: int = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        _require_int(self.seq, "seq", minimum=1)
        _require(self.event in PUBLICATION_EVENTS, f"unknown publication event {self.event!r}")
        _require(_is_hex64(self.generation_id), "generation_id must be 64 hex")
        _require(_is_hex64(self.generation_receipt_sha256), "generation_receipt_sha256 must be 64 hex")
        _require(self.repair_row_sha256 is None or _is_hex64(self.repair_row_sha256), "repair_row_sha256 must be 64 hex or None")
        _require(isinstance(self.coverage_clock, CoverageClock), "coverage_clock must be CoverageClock")
        _require_dict(self.source_input_origins, "source_input_origins")
        _require(self.parent_generation_id is None or _is_hex64(self.parent_generation_id), "parent_generation_id")
        _require(_is_hex64(self.previous_row_sha256), "previous_row_sha256 must be 64 hex")
        _require_str(self.published_at, "published_at")
        _set_aliases(self)

    def to_dict(self) -> dict:
        return {
            "seq": self.seq, "event": self.event, "generation_id": self.generation_id,
            "generation_receipt_sha256": self.generation_receipt_sha256, "repair_row_sha256": self.repair_row_sha256,
            "coverage_clock": self.coverage_clock.to_dict(),
            "canonical_watermark_commit_seq": self.canonical_watermark_commit_seq,
            "latest_canonical_commit_seq_at_capture": self.latest_canonical_commit_seq_at_capture,
            "source_input_origins": json.loads(canonical_json(self.source_input_origins)),
            "parent_generation_id": self.parent_generation_id, "previous_row_sha256": self.previous_row_sha256,
            "published_at": self.published_at,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "PublicationLineageRow":
        d = _require_dict(d, "PublicationLineageRow")
        try:
            return cls(seq=d.get("seq"), event=d.get("event"), generation_id=d.get("generation_id"),
                       generation_receipt_sha256=d.get("generation_receipt_sha256"), repair_row_sha256=d.get("repair_row_sha256"),
                       coverage_clock=_clock_from(d, "PublicationLineageRow"), source_input_origins=d.get("source_input_origins"),
                       parent_generation_id=d.get("parent_generation_id"), previous_row_sha256=d.get("previous_row_sha256"),
                       published_at=d.get("published_at"))
        except TypeError as exc:
            raise GenerationIntegrityError(f"PublicationLineageRow: {exc}") from exc


def publication_row_bytes(row: PublicationLineageRow) -> bytes:
    """Exact LF-framed canonical bytes of one lineage row (as appended)."""
    return canonical_json(row.to_dict()).encode("utf-8") + b"\n"


def row_sha256(row: PublicationLineageRow) -> str:
    """sha256 of the row's canonical JSON (without the LF) — the value the next row's
    ``previous_row_sha256`` must carry."""
    return sha256_bytes(canonical_json(row.to_dict()).encode("utf-8"))


@dataclass(frozen=True, kw_only=True)
class PublicationResult:
    """D-13-18/45/49/54/55 — exact type; never returned for a refusal."""

    outcome: Literal["published_new", "adopted_orphan", "reactivated_retained", "reused_existing", "replaced_corrupt"]
    generation_id: str
    active_manifest: ProjectionManifest
    publication_row_seq: int
    publication_row_written: bool
    coverage_advanced: bool
    quarantined: str | None
    repair_row_seq: int | None

    def __post_init__(self) -> None:
        _require(self.outcome in PUBLICATION_EVENTS, f"unknown outcome {self.outcome!r}")


@dataclass(frozen=True, kw_only=True)
class PublicationIntent:
    """D-13-54/60 — projection/publication_intent.json: ONE complete row + target/evidence."""

    schema_version: str = "omega.projection_publication_intent.v1"
    row: PublicationLineageRow
    target_generation_dir: str
    candidate_path: str | None
    candidate_receipt_sha256: str | None
    prepared_at: str

    def __post_init__(self) -> None:
        _require(isinstance(self.row, PublicationLineageRow), "row must be PublicationLineageRow")
        _require_str(self.target_generation_dir, "target_generation_dir")
        _require(self.candidate_receipt_sha256 is None or _is_hex64(self.candidate_receipt_sha256), "candidate_receipt_sha256")
        _require_str(self.prepared_at, "prepared_at")

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version, "row": self.row.to_dict(), "target_generation_dir": self.target_generation_dir,
                "candidate_path": self.candidate_path, "candidate_receipt_sha256": self.candidate_receipt_sha256,
                "prepared_at": self.prepared_at}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "PublicationIntent":
        d = _require_dict(d, "PublicationIntent")
        try:
            return cls(schema_version=d.get("schema_version", "omega.projection_publication_intent.v1"),
                       row=PublicationLineageRow.from_dict(d.get("row")), target_generation_dir=d.get("target_generation_dir"),
                       candidate_path=d.get("candidate_path"), candidate_receipt_sha256=d.get("candidate_receipt_sha256"),
                       prepared_at=d.get("prepared_at"))
        except TypeError as exc:
            raise GenerationIntegrityError(f"PublicationIntent: {exc}") from exc


# ── registry-authority / repair rows ─────────────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class RegistryAuthorityLineageRow:
    """D-13-46 — provenance only; never a claim of successful generation publication."""

    seq: int
    registry_sha256: str
    previous_registry_sha256: str | None
    previous_row_sha256: str
    introduced_by_candidate_id: str | None
    selected_at: str

    def to_dict(self) -> dict:
        return {"seq": self.seq, "registry_sha256": self.registry_sha256, "previous_registry_sha256": self.previous_registry_sha256,
                "previous_row_sha256": self.previous_row_sha256, "introduced_by_candidate_id": self.introduced_by_candidate_id,
                "selected_at": self.selected_at}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "RegistryAuthorityLineageRow":
        d = _require_dict(d, "RegistryAuthorityLineageRow")
        try:
            return cls(**{k: d.get(k) for k in ("seq", "registry_sha256", "previous_registry_sha256", "previous_row_sha256",
                                                "introduced_by_candidate_id", "selected_at")})
        except TypeError as exc:
            raise GenerationIntegrityError(f"RegistryAuthorityLineageRow: {exc}") from exc


@dataclass(frozen=True, kw_only=True)
class RepairIntentPayload:
    """D-13-51/61 — the complete repair preimage; NO field depends on repair_row_sha256."""

    schema_version: str = "omega.projection_repair_intent.v1"
    generation_id: str
    candidate_path: str
    candidate_receipt_sha256: str
    quarantine_path: str
    corrupt_tree_sha256: str
    replacement_tree_sha256: str
    prior_repair_row_sha256: str
    prepared_at: str

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version, "generation_id": self.generation_id, "candidate_path": self.candidate_path,
                "candidate_receipt_sha256": self.candidate_receipt_sha256, "quarantine_path": self.quarantine_path,
                "corrupt_tree_sha256": self.corrupt_tree_sha256, "replacement_tree_sha256": self.replacement_tree_sha256,
                "prior_repair_row_sha256": self.prior_repair_row_sha256, "prepared_at": self.prepared_at}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "RepairIntentPayload":
        d = _require_dict(d, "RepairIntentPayload")
        try:
            return cls(**{k: d.get(k) for k in ("schema_version", "generation_id", "candidate_path", "candidate_receipt_sha256",
                                                "quarantine_path", "corrupt_tree_sha256", "replacement_tree_sha256",
                                                "prior_repair_row_sha256", "prepared_at") if k in d})
        except TypeError as exc:
            raise GenerationIntegrityError(f"RepairIntentPayload: {exc}") from exc


def repair_intent_sha256(payload: RepairIntentPayload) -> str:
    return sha256_bytes(canonical_json(payload.to_dict()).encode("utf-8"))


@dataclass(frozen=True, kw_only=True)
class RepairIntent:
    payload: RepairIntentPayload

    def to_dict(self) -> dict:
        return {"payload": self.payload.to_dict()}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "RepairIntent":
        d = _require_dict(d, "RepairIntent")
        return cls(payload=RepairIntentPayload.from_dict(d.get("payload")))


@dataclass(frozen=True, kw_only=True)
class RepairLineageRow:
    """D-13-47/51/61 — embeds the complete payload: intent_sha256 is reproducible after
    both live intents are cleared."""

    seq: int
    intent: RepairIntentPayload
    intent_sha256: str
    previous_row_sha256: str
    repaired_at: str

    def __post_init__(self) -> None:
        _require(isinstance(self.intent, RepairIntentPayload), "intent must be RepairIntentPayload")
        _require(self.intent_sha256 == repair_intent_sha256(self.intent),
                 "RepairLineageRow.intent_sha256 does not reproduce sha256(canonical_json(intent))")

    def to_dict(self) -> dict:
        return {"seq": self.seq, "intent": self.intent.to_dict(), "intent_sha256": self.intent_sha256,
                "previous_row_sha256": self.previous_row_sha256, "repaired_at": self.repaired_at}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "RepairLineageRow":
        d = _require_dict(d, "RepairLineageRow")
        try:
            return cls(seq=d.get("seq"), intent=RepairIntentPayload.from_dict(d.get("intent")), intent_sha256=d.get("intent_sha256"),
                       previous_row_sha256=d.get("previous_row_sha256"), repaired_at=d.get("repaired_at"))
        except TypeError as exc:
            raise GenerationIntegrityError(f"RepairLineageRow: {exc}") from exc


# ── directory sync (D-13-24 platform contract) ───────────────────────────────

def sync_dir(path: Path) -> str:
    """Make a directory entry change durable. POSIX: fsync the directory fd. Windows:
    FlushFileBuffers on a directory handle (ctypes). Otherwise refuse — never
    silently skip. Returns the mechanism name (recorded in manifests as ``dir_sync``)."""
    path = Path(path)
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | _O_BINARY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return "posix_fsync"
    if sys.platform.startswith("win"):
        return _sync_dir_windows(path)
    raise GenerationIntegrityError(f"directory sync unavailable on platform {sys.platform!r}; refusing to publish")


def _sync_dir_windows(path: Path) -> str:  # pragma: no cover - exercised on Windows only
    # FlushFileBuffers requires GENERIC_WRITE on the handle (a GENERIC_READ-only
    # directory handle opens fine and then FlushFileBuffers fails with
    # ERROR_ACCESS_DENIED). FILE_FLAG_BACKUP_SEMANTICS is what lets CreateFileW open
    # a directory at all. HANDLE is pointer-sized: declare the restype/argtypes so a
    # 64-bit handle is never truncated through ctypes' default c_int.
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_SHARE_DELETE = 0x1, 0x2, 0x4
    OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS = 3, 0x02000000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    try:
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    except AttributeError:  # a test double standing in for kernel32
        pass
    handle = kernel32.CreateFileW(str(path), GENERIC_READ | GENERIC_WRITE,
                                  FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                  None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
    if handle in (None, 0, INVALID_HANDLE_VALUE):
        raise GenerationIntegrityError(
            f"CreateFileW failed for directory sync: {path} (winerror {ctypes.get_last_error() or ctypes.GetLastError()})"
        )
    try:
        if not kernel32.FlushFileBuffers(handle):
            raise GenerationIntegrityError(
                f"FlushFileBuffers failed for directory sync: {path} (winerror {ctypes.GetLastError()})"
            )
    finally:
        kernel32.CloseHandle(handle)
    return "windows_flushfilebuffers"


# ── validators ───────────────────────────────────────────────────────────────

def _check_artifacts(dir_: Path, content: GenerationContent) -> None:
    dir_ = Path(dir_)
    _require(dir_.is_dir(), f"generation directory missing: {dir_}")
    for field_name, file_name in GENERATION_ARTIFACTS:
        target = dir_ / file_name
        _require(target.is_file(), f"missing artifact {file_name} in {dir_.name}")
        actual = sha256_file(target)
        expected = getattr(content, field_name)
        _require(actual == expected, f"sha256 mismatch for {file_name}: manifest {expected} != file {actual}")
    # Gate D2 unit: graph_snapshot_hash must equal the inner graph_snapshot.json's own hash
    try:
        inner = json.loads((dir_ / "graph_snapshot.json").read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise GenerationIntegrityError(f"unreadable graph_snapshot.json: {exc}") from exc
    _require(isinstance(inner, dict) and inner.get("snapshot_hash") == content.graph_snapshot_hash,
             "graph_snapshot_hash disagrees with the inner graph_snapshot.json (mixed generation)")
    seen_ids: set[str] = set()
    root_resolved = dir_.resolve()
    for plane in content.planes:
        _require(plane.plane_id not in seen_ids, f"duplicate plane_id {plane.plane_id}")
        seen_ids.add(plane.plane_id)
        _require(_relative_path_ok(plane.path), f"plane path escapes the generation: {plane.path!r}")
        target = (dir_ / plane.path)
        _require(target.resolve().is_relative_to(root_resolved), f"plane path escapes the generation: {plane.path!r}")
        _require(target.is_file(), f"missing plane file {plane.path}")
        actual = sha256_file(target)
        _require(actual == plane.sha256, f"plane sha256 mismatch for {plane.plane_id}")
    retina = content.components["semantic_retina"]
    for field_name, file_name in SEMANTIC_RETINA_ARTIFACTS:
        target = dir_ / file_name
        if retina["status"] == "empty":
            _require(not target.exists(), f"empty semantic plane carries a stray {file_name} in {dir_.name}")
            continue
        _require(target.is_file(), f"missing semantic retina artifact {file_name} in {dir_.name}")
        actual = sha256_file(target)
        _require(actual == retina[field_name], f"sha256 mismatch for {file_name}: manifest {retina[field_name]} != file {actual}")


def _check_semantic_retina_component(component: Any) -> None:
    _require(isinstance(component, dict), f"components.semantic_retina must pin the retina (built or empty), got {component!r}")
    status = component.get("status")
    _require(status in _SEMANTIC_RETINA_KEYS, f"semantic_retina status must be built or empty: {status!r}")
    _require(set(component) == _SEMANTIC_RETINA_KEYS[status],
             f"semantic_retina ({status}) keys {sorted(component)} != {sorted(_SEMANTIC_RETINA_KEYS[status])}")
    _require(isinstance(component["schema"], str) and bool(component["schema"]), "semantic_retina schema must be a non-empty str")
    _require(_is_hex64(component["contract_sha256"]), "semantic_retina contract_sha256 must be 64 hex")
    if status == "empty":
        _require(component["records"] == 0 and not isinstance(component["records"], bool), "an empty semantic plane pins records == 0")
        return
    _require_int(component["records"], "semantic_retina records", minimum=1)
    for field_name, _file_name in SEMANTIC_RETINA_ARTIFACTS:
        _require(_is_hex64(component[field_name]), f"semantic_retina {field_name} must be 64 hex")


def _check_candidate_manifest(candidate: CandidateManifest) -> None:
    _require(isinstance(candidate, CandidateManifest), "validate_candidate requires a CandidateManifest")
    recomputed = generation_id_for(candidate.content)
    _require(recomputed == candidate.generation_id, f"generation_id {candidate.generation_id} != recomputed {recomputed}")
    _require(candidate.snapshot_closure == "proved", f"snapshot_closure must be 'proved', got {candidate.snapshot_closure!r}")
    _require(candidate.unscorable_coordinate_count == 0,
             f"unscorable_coordinate_count={candidate.unscorable_coordinate_count} prohibits publication (ADR-MO-019)")
    _check_semantic_retina_component(candidate.content.components.get("semantic_retina"))


def _dir_name_matches(dir_: Path, generation_id: str, *, allow_candidate: bool) -> None:
    name = Path(dir_).name
    expected = generation_dir_name(generation_id)
    ok = name == expected or (allow_candidate and name == expected + CANDIDATE_SUFFIX)
    _require(ok, f"directory name {name!r} does not name generation {generation_id}")


def validate_candidate(candidate_dir: Path, candidate: CandidateManifest) -> None:
    """D-13-32/38: candidate naming accepted; artifact refs relative to the candidate
    root; id recomputed from content; every sha256; closure == 'proved'; unscorable == 0.
    Raises GenerationIntegrityError BEFORE any authority/lineage/rename step."""
    _check_candidate_manifest(candidate)
    _dir_name_matches(candidate_dir, candidate.generation_id, allow_candidate=True)
    _check_artifacts(candidate_dir, candidate.content)


def validate_generation_directory(gen_dir: Path, expected: GenerationContent | GenerationReceipt) -> GenerationReceipt:
    """D-13-45/64/68: validate a receipted directory (files + identity + closure + page-0
    + receipt consistency) WITHOUT requiring it to be active or the lineage head.
    ``expected`` is either the content identity or the full receipt (then the whole
    receipt — including its ONE coverage clock — must match)."""
    gen_dir = Path(gen_dir)
    receipt = read_receipt(gen_dir)
    if isinstance(expected, GenerationReceipt):
        _require(receipt == expected, "receipt in directory disagrees with the expected receipt (content/closure/page-0/clock/origins)")
        content = expected.candidate.content
    elif isinstance(expected, GenerationContent):
        _require(receipt.candidate.content == expected, "receipt content disagrees with the expected content")
        content = expected
    else:
        raise GenerationIntegrityError("expected must be GenerationContent or GenerationReceipt")
    _check_candidate_manifest(receipt.candidate)
    _require(generation_id_for(content) == receipt.candidate.generation_id, "receipt generation_id does not match its content")
    _dir_name_matches(gen_dir, receipt.candidate.generation_id, allow_candidate=True)
    _check_artifacts(gen_dir, content)
    return receipt


def validate_published_generation(projection_root: Path, manifest: ProjectionManifest) -> None:
    """D-13-32/49/64: ACTIVE state only — the directory via its receipt, generation_dir
    == 'gen-' + id, outer content == receipt content, page-0 fields copied from the
    receipt; then coverage via the publication-lineage HEAD (receipt sha,
    repair_row_sha256, coverage clock, origins == head row; head names an existing dir)."""
    root = Path(projection_root)
    _require(isinstance(manifest, ProjectionManifest), "validate_published_generation requires a ProjectionManifest")
    _require(manifest.generation_dir == generation_dir_name(manifest.generation_id),
             f"generation_dir {manifest.generation_dir!r} != 'gen-' + generation_id")
    _require(generation_id_for(manifest.content) == manifest.generation_id, "manifest content does not hash to generation_id")
    _require(manifest.snapshot_closure == "proved", "active manifest snapshot_closure must be 'proved'")
    gen_dir = root / manifest.generation_dir
    receipt = validate_generation_directory(gen_dir, manifest.content)
    cand = receipt.candidate
    _require(cand.content == manifest.content, "outer manifest content != receipt content")
    for name in ("page0_capacity_by_lobe", "page0_used_by_lobe", "page0_utilization_by_lobe",
                 "page0_overflow_attempts", "unscorable_coordinate_count"):
        _require(getattr(manifest, name) == getattr(cand, name), f"outer manifest {name} != receipt")
    scan = read_publication_lineage(root / LINEAGE_FILE)
    head = scan.head
    _require(head is not None, "publication lineage has no head row")
    _require(head.generation_id == manifest.generation_id, "lineage HEAD names a different generation")
    _require((root / generation_dir_name(head.generation_id)).is_dir(), "lineage HEAD names an absent generation directory")
    _require(head.seq == manifest.lineage_seq, f"manifest lineage_seq {manifest.lineage_seq} != head seq {head.seq}")
    _require(head.generation_receipt_sha256 == generation_receipt_sha256(gen_dir),
             "lineage HEAD generation_receipt_sha256 != the directory's receipt bytes")
    _require(head.coverage_clock == manifest.coverage_clock, "manifest coverage_clock != publication-lineage head")
    _require(json.loads(canonical_json(head.source_input_origins)) == json.loads(canonical_json(manifest.source_input_origins)),
             "manifest source_input_origins != publication-lineage head")
    _require(head.parent_generation_id == manifest.parent_generation_id, "manifest parent_generation_id != head")


# ── publication lineage journal (append-only LF rows, hash chained) ──────────

@dataclass(frozen=True, kw_only=True)
class LineageScan:
    rows: tuple[PublicationLineageRow, ...]
    torn_tail: bytes | None          # class (b): an incomplete final LF row (evidence, never parsed)
    head: PublicationLineageRow | None
    head_sha256: str                 # GENESIS_ROW_SHA256 when empty


def read_publication_lineage(path: Path) -> LineageScan:
    """Complete rows only; chain (`previous_row_sha256`) and dense seq verified — a
    hash-invalid / reordered / tampered committed row raises PublicationLineageTampered
    (class c). An incomplete final row is reported as ``torn_tail`` (class b)."""
    path = Path(path)
    if not path.exists():
        return LineageScan(rows=(), torn_tail=None, head=None, head_sha256=GENESIS_ROW_SHA256)
    raw = path.read_bytes()
    lines = raw.split(b"\n")
    torn: bytes | None = None
    if lines and lines[-1] != b"":
        torn = lines[-1]
        lines = lines[:-1]
    else:
        lines = lines[:-1]  # drop the empty element after the final LF
    rows: list[PublicationLineageRow] = []
    prev_sha = GENESIS_ROW_SHA256
    for index, line in enumerate(lines, start=1):
        try:
            row = PublicationLineageRow.from_dict(json.loads(line.decode("utf-8")))
        except (ValueError, UnicodeDecodeError, GenerationIntegrityError) as exc:
            raise PublicationLineageTampered(f"lineage row {index} is not a valid committed row: {exc}") from exc
        if row.seq != index or row.previous_row_sha256 != prev_sha:
            raise PublicationLineageTampered(
                f"lineage chain broken at row {index}: seq={row.seq} previous_row_sha256={row.previous_row_sha256[:12]}… expected seq={index} prev={prev_sha[:12]}…")
        rows.append(row)
        prev_sha = row_sha256(row)
    return LineageScan(rows=tuple(rows), torn_tail=torn, head=rows[-1] if rows else None, head_sha256=prev_sha)


def append_publication_row(path: Path, row: PublicationLineageRow) -> None:
    """Append one canonical-JSON LF row: chain verified against the current head first
    (never appends onto a torn or tampered file), then write + flush + fsync + parent
    sync. Recovery of a torn tail is the publisher's job (D-13-51 class b), not this
    function's."""
    path = Path(path)
    scan = read_publication_lineage(path)
    _require(scan.torn_tail is None, "lineage has an incomplete final row; settle it before appending (D-13-51 class b)")
    expected_seq = (scan.head.seq + 1) if scan.head else 1
    if row.seq != expected_seq or row.previous_row_sha256 != scan.head_sha256:
        raise PublicationLineageTampered(
            f"row seq={row.seq} prev={row.previous_row_sha256[:12]}… does not continue head seq={expected_seq - 1} sha={scan.head_sha256[:12]}…")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        fh.write(publication_row_bytes(row))
        fh.flush()
        os.fsync(fh.fileno())
    sync_dir(path.parent)


# ═════════════════════════════════════════════════════════════════════════════
# Task A2 — publication under ONE authority token (D-13-9/33/67), coverage gate
# (D-13-58/59), coverage seal (D-13-77), publication intents (D-13-54/60),
# classification (D-13-45/49), two-stage repair (D-13-51/55/61), lease-aware GC
# (D-13-4), intent-first recovery.
# ═════════════════════════════════════════════════════════════════════════════

import shutil
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

PROJECTION_DIR = "projection"
PUBLICATION_LOCK_FILE = "publication.lock"
LEASE_NAMESPACE_LOCK_FILE = "lease_namespace.lock"
LEASES_DIR = "leases"
MANIFEST_FILE = "manifest.json"
COVERAGE_SEAL_FILE = "coverage_seal.json"
PUBLICATION_INTENT_FILE = "publication_intent.json"
REPAIR_INTENT_FILE = "repair_intent.json"
REPAIR_LINEAGE_FILE = "repair_lineage.jsonl"
QUARANTINE_DIR = "quarantine"
QUARANTINE_KEEP = 2
INFRASTRUCTURE_LOCK_FILES: tuple[str, ...] = (PUBLICATION_LOCK_FILE, LEASE_NAMESPACE_LOCK_FILE)
LEASE_WAIT_SECONDS = 5.0


class PublicationAuthorityBusy(GenerationIntegrityError):
    """D-13-9: another holder owns the OS-held exclusive publication authority."""


class PublicationAuthorityRequired(GenerationIntegrityError):
    """A stage was called without possession of the live authority token (D-13-33)."""


class CoverageSealUnavailable(GenerationIntegrityError):
    """D-13-77: the strict coverage seal could not be made durable; nothing else changed."""


class PublicationBlockedOnLeases(GenerationIntegrityError):
    """D-13-25/37: the generation to be replaced still has live leases after the bounded wait."""


class PublicationIncomplete(GenerationIntegrityError):
    """D-13-51 class (b) without a qualifying intent: the prior chain/manifest were restored;
    the retry obligation is the next dispatch (a NEW closed Candidate)."""


class RecoveryOperatorRequired(GenerationIntegrityError):
    """Recovery found a state the frozen laws forbid to repair automatically."""


class SimulatedCrash(BaseException):
    """Test-only: raised by the crash hook to simulate process death at an exact point."""


_crash_hook: Optional[Callable[[str], None]] = None


def _crash(point: str) -> None:
    hook = _crash_hook
    if hook is not None:
        hook(point)


def _now_iso() -> str:
    from core.symphony_time import now_iso
    return now_iso()


def resolve_memory_root(root: Path | str | None) -> Path:
    """The memory root that owns ``<memory>/projection``. ``None`` ⇒ the active profile's
    ``core.paths.MEMORY_ROOT`` (looked up at call time so test redirects apply); a
    profile dir containing ``memory/`` ⇒ its ``memory``; a ``taxonomy_store`` dir ⇒ its
    parent memory dir; anything else is taken AS the memory root (scratch layouts)."""
    if root is None:
        import core.paths as core_paths
        return Path(core_paths.MEMORY_ROOT)
    base = Path(root)
    if base.name == "taxonomy_store":
        return base.parent
    if base.name == "memory":
        return base
    if (base / "memory").is_dir():
        return base / "memory"
    return base


def projection_root(root: Path | str | None) -> Path:
    return resolve_memory_root(root) / PROJECTION_DIR


def candidate_dir(root: Path, generation_id: str) -> Path:
    return projection_root(root) / candidate_dir_name(generation_id)


# ── OS-held locks (POSIX flock / Windows msvcrt) ─────────────────────────────

def _open_lock_handle(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o644)


def _try_lock(fd: int, *, exclusive: bool) -> bool:
    if os.name == "posix":
        import fcntl
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    import msvcrt  # pragma: no cover - Windows only
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # Windows byte locks are exclusive; shared leases use distinct offsets
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    if os.name == "posix":
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
    else:  # pragma: no cover
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


@dataclass(eq=False)
class PublicationAuthority:
    """The ONE token of D-13-33: an open handle holding the OS-exclusive publication
    lock. Every stage asserts possession by object identity + lock liveness."""

    projection_root: Path
    token: str
    _fd: int
    live: bool = True

    def close(self) -> None:
        if self.live:
            self.live = False
            _unlock(self._fd)
            os.close(self._fd)


@contextmanager
def projection_publication_authority(root: Path) -> Iterator[PublicationAuthority]:
    """D-13-9: one authority per profile, held for the WHOLE transaction; nonblocking;
    NO age-based reclamation (the OS releases it on close / process death)."""
    proj = projection_root(root)
    fd = _open_lock_handle(proj / PUBLICATION_LOCK_FILE)
    if not _try_lock(fd, exclusive=True):
        os.close(fd)
        raise PublicationAuthorityBusy(f"publication authority for {proj} is held elsewhere")
    authority = PublicationAuthority(projection_root=proj, token=os.urandom(8).hex(), _fd=fd)
    try:
        yield authority
    finally:
        authority.close()


def assert_authority(authority: Any, projection_root_: Path | None = None) -> None:
    if not isinstance(authority, PublicationAuthority) or not authority.live:
        raise PublicationAuthorityRequired("stage called without a live publication authority token")
    if projection_root_ is not None and Path(projection_root_) != authority.projection_root:
        raise PublicationAuthorityRequired("publication authority token belongs to a different projection root")


# ── strict small-file writes ─────────────────────────────────────────────────

def _strict_write_bytes(path: Path, data: bytes) -> str:
    """temp (same dir) → flush + fsync → os.replace → parent sync. Returns the dir-sync mechanism."""
    from memory.atomic_replace import atomic_replace_bytes
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace_bytes(path, data)
    return sync_dir(path.parent)


def _strict_write_json(path: Path, doc: Any) -> str:
    return _strict_write_bytes(path, canonical_json(doc).encode("utf-8"))


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_bytes().decode("utf-8"))


def _remove_tree(path: Path) -> None:
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _fsync_tree(dir_: Path) -> None:
    # Windows' fsync (_commit) needs a fd opened for WRITING; a read-only fd fails with
    # EBADF. POSIX fsyncs a read-only fd fine and O_RDONLY is the least privilege there.
    file_flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    for p in sorted(Path(dir_).rglob("*")):
        if p.is_file() and not p.is_symlink():
            fd = os.open(p, file_flags | _O_BINARY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for p in sorted([Path(dir_)] + [d for d in Path(dir_).rglob("*") if d.is_dir()], key=lambda x: len(x.parts), reverse=True):
        sync_dir(p)


def tree_sha256(dir_: Path) -> str:
    """Deterministic hash of a directory tree: sorted relative paths, sizes and content
    hashes (symlinks hash their target string). Used for repair evidence."""
    dir_ = Path(dir_)
    h = hashlib.sha256()
    for p in sorted(dir_.rglob("*")):
        rel = p.relative_to(dir_).as_posix()
        if p.is_symlink():
            h.update(f"L {rel} {os.readlink(p)}\n".encode("utf-8"))
        elif p.is_file():
            h.update(f"F {rel} {p.stat().st_size} {sha256_file(p)}\n".encode("utf-8"))
        else:
            h.update(f"D {rel}\n".encode("utf-8"))
    return h.hexdigest()


# ── coverage seal (D-13-77) ──────────────────────────────────────────────────

def read_coverage_seal(proj: Path) -> CoverageClock | None:
    path = Path(proj) / COVERAGE_SEAL_FILE
    if not path.exists():
        return None
    try:
        return CoverageClock.from_dict(_read_json(path))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise GenerationIntegrityError(f"coverage seal malformed: {exc}") from exc


def _seal_advances(seal: CoverageClock | None, clock: CoverageClock) -> bool:
    if seal is None:
        return True
    if seal.clock_epoch_id != clock.clock_epoch_id:
        return True
    return clock.commit_seq > seal.commit_seq


def ensure_coverage_seal(proj: Path, clock: CoverageClock) -> bool:
    """Strictly persist ``clock`` as the coverage seal iff it advances beyond the durable
    seal (equal coverage ⇒ no write). Failure ⇒ CoverageSealUnavailable, nothing changed."""
    if not _seal_advances(read_coverage_seal(proj), clock):
        return False
    try:
        _strict_write_json(Path(proj) / COVERAGE_SEAL_FILE, clock.to_dict())
    except (OSError, GenerationIntegrityError) as exc:
        raise CoverageSealUnavailable(f"coverage seal write failed: {exc}") from exc
    return True


# ── coverage-clock gate (D-13-58/59) ─────────────────────────────────────────

def coverage_gate(head: CoverageClock | None, candidate: CoverageClock, *, event: str) -> bool:
    """Returns coverage_advanced. No head ⇒ initial coverage accepted (False: nothing to
    advance from). Epoch mismatch or lower sequence ⇒ CoverageClockRegression."""
    if head is None:
        return False
    if head.clock_epoch_id != candidate.clock_epoch_id or candidate.commit_seq < head.commit_seq:
        raise CoverageClockRegression(head=head, candidate=candidate, event=event)
    return candidate.commit_seq > head.commit_seq


# ── intents on disk ──────────────────────────────────────────────────────────

def read_publication_intent(proj: Path) -> PublicationIntent | None:
    path = Path(proj) / PUBLICATION_INTENT_FILE
    if not path.exists():
        return None
    try:
        return PublicationIntent.from_dict(_read_json(path))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise RecoveryOperatorRequired(f"publication intent unreadable/tampered: {exc}") from exc


def read_repair_intent(proj: Path) -> RepairIntent | None:
    path = Path(proj) / REPAIR_INTENT_FILE
    if not path.exists():
        return None
    try:
        return RepairIntent.from_dict(_read_json(path))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise RecoveryOperatorRequired(f"repair intent unreadable/tampered: {exc}") from exc


def _write_intent(path: Path, doc: Any) -> None:
    _strict_write_json(path, doc)


def _clear_intent(path: Path) -> None:
    path = Path(path)
    if path.exists():
        path.unlink()
        sync_dir(path.parent)


def intent_protected_paths(proj: Path) -> set[Path]:
    """Directories named by live intents — never deleted by generic cleanup/GC."""
    proj = Path(proj)
    keep: set[Path] = set()
    pi = read_publication_intent(proj)
    if pi is not None:
        keep.add(proj / pi.target_generation_dir)
        if pi.candidate_path:
            keep.add(proj / pi.candidate_path)
    ri = read_repair_intent(proj)
    if ri is not None:
        keep.add(proj / generation_dir_name(ri.payload.generation_id))
        keep.add(proj / ri.payload.candidate_path)
        keep.add(proj / ri.payload.quarantine_path)
    return keep


# ── active manifest ──────────────────────────────────────────────────────────

def read_active_manifest(proj: Path) -> ProjectionManifest | None:
    """None when no manifest exists at all (legacy); GenerationIntegrityError when a
    manifest exists but is invalid (D-13-16: never legacy fallback)."""
    path = Path(proj) / MANIFEST_FILE
    if not path.exists():
        return None
    try:
        return ProjectionManifest.from_dict(_read_json(path))
    except (OSError, ValueError, UnicodeDecodeError, GenerationIntegrityError) as exc:
        raise GenerationIntegrityError(f"active manifest invalid: {exc}") from exc


def _default_diagnostics(dir_sync: str) -> PublicationDiagnostics:
    return PublicationDiagnostics(published_at=_now_iso(), dir_sync=dir_sync, backend_at_publish="not_recorded",
                                  pending_delta_count=0, oldest_pending_commit_seq=None, oldest_pending_queued_at="",
                                  sequenced_consumed=0, unsequenced_consumed=0)


def construct_manifest(receipt: GenerationReceipt, head: PublicationLineageRow, *, builder: str, reason: str,
                       diagnostics: PublicationDiagnostics) -> ProjectionManifest:
    """The outer manifest is CONSTRUCTED from the target receipt (content, page-0) + the
    publication-lineage head (coverage, origins, seq, parent) + publication diagnostics."""
    cand = receipt.candidate
    return ProjectionManifest(
        content=cand.content, generation_id=cand.generation_id, generation_dir=generation_dir_name(cand.generation_id),
        lineage_seq=head.seq, parent_generation_id=head.parent_generation_id,
        coverage_clock=head.coverage_clock, snapshot_closure=cand.snapshot_closure,
        page0_capacity_by_lobe=cand.page0_capacity_by_lobe, page0_used_by_lobe=cand.page0_used_by_lobe,
        page0_utilization_by_lobe=cand.page0_utilization_by_lobe, page0_overflow_attempts=cand.page0_overflow_attempts,
        unscorable_coordinate_count=cand.unscorable_coordinate_count,
        source_input_origins=dict(head.source_input_origins),
        sequenced_consumed=diagnostics.sequenced_consumed, unsequenced_consumed=diagnostics.unsequenced_consumed,
        pending_delta_count=diagnostics.pending_delta_count, oldest_pending_commit_seq=diagnostics.oldest_pending_commit_seq,
        oldest_pending_queued_at=diagnostics.oldest_pending_queued_at,
        builder=builder, reason=reason, published_at=diagnostics.published_at, dir_sync=diagnostics.dir_sync,
        backend_at_publish=diagnostics.backend_at_publish,
    )


def _activate(proj: Path, manifest: ProjectionManifest) -> None:
    from memory.atomic_replace import atomic_replace_json
    atomic_replace_json(Path(proj) / MANIFEST_FILE, manifest.to_dict())
    sync_dir(proj)
    validate_published_generation(proj, manifest)


# ── leases (probe side; the reader side lands in Task A3b) ───────────────────

def lease_path(proj: Path, generation_id: str) -> Path:
    return Path(proj) / LEASES_DIR / f"{generation_id}.lease"


def generation_is_leased(proj: Path, generation_id: str) -> bool:
    """True iff some process holds a shared OS lease on the generation (an exclusive
    nonblocking probe fails). No lease file ⇒ not leased."""
    path = lease_path(proj, generation_id)
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR | _O_BINARY)
    try:
        if _try_lock(fd, exclusive=True):
            _unlock(fd)
            return False
        return True
    finally:
        os.close(fd)


def _wait_zero_leases(proj: Path, generation_id: str, *, timeout: float | None = None) -> None:
    timeout = LEASE_WAIT_SECONDS if timeout is None else timeout
    deadline = time.monotonic() + timeout
    while generation_is_leased(proj, generation_id):
        if time.monotonic() >= deadline:
            raise PublicationBlockedOnLeases(f"generation {generation_id[:12]}… still leased after {timeout}s")
        time.sleep(0.05)


# ── repair lineage journal (D-13-47/51/61) ───────────────────────────────────

def repair_row_sha256(row: RepairLineageRow) -> str:
    return sha256_bytes(canonical_json(row.to_dict()).encode("utf-8"))


@dataclass(frozen=True, kw_only=True)
class RepairScan:
    rows: tuple[RepairLineageRow, ...]
    torn_tail: bytes | None
    head_sha256: str


def read_repair_lineage(proj: Path) -> RepairScan:
    path = Path(proj) / REPAIR_LINEAGE_FILE
    if not path.exists():
        return RepairScan(rows=(), torn_tail=None, head_sha256=GENESIS_ROW_SHA256)
    raw = path.read_bytes()
    lines = raw.split(b"\n")
    torn = lines[-1] if lines and lines[-1] != b"" else None
    lines = lines[:-1]
    rows: list[RepairLineageRow] = []
    prev = GENESIS_ROW_SHA256
    for index, line in enumerate(lines, start=1):
        try:
            row = RepairLineageRow.from_dict(json.loads(line.decode("utf-8")))
        except (ValueError, UnicodeDecodeError, GenerationIntegrityError) as exc:
            raise RecoveryOperatorRequired(f"repair lineage row {index} invalid/tampered: {exc}") from exc
        if row.seq != index or row.previous_row_sha256 != prev:
            raise RecoveryOperatorRequired(f"repair lineage chain broken at row {index}")
        rows.append(row)
        prev = repair_row_sha256(row)
    return RepairScan(rows=tuple(rows), torn_tail=torn, head_sha256=prev)


def _append_repair_row(proj: Path, row: RepairLineageRow) -> None:
    path = Path(proj) / REPAIR_LINEAGE_FILE
    with open(path, "ab") as fh:
        fh.write(canonical_json(row.to_dict()).encode("utf-8") + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
    sync_dir(proj)


# ── publication lineage: torn-tail settlement (D-13-51 class b) ──────────────

def _settle_torn_lineage_tail(proj: Path) -> bool:
    """Copy an incomplete final LF row to lineage.torn-<n> (evidence), truncate to the last
    complete row, fsync + parent sync. Returns True if a tail was settled."""
    proj = Path(proj)
    path = proj / LINEAGE_FILE
    if not path.exists():
        return False
    raw = path.read_bytes()
    if not raw or raw.endswith(b"\n"):
        return False
    cut = raw.rfind(b"\n") + 1
    tail = raw[cut:]
    n = 1
    while (proj / f"lineage.torn-{n}").exists():
        n += 1
    (proj / f"lineage.torn-{n}").write_bytes(tail)
    with open(path, "r+b") as fh:
        fh.truncate(cut)
        fh.flush()
        os.fsync(fh.fileno())
    sync_dir(proj)
    return True


def _append_or_dedupe(proj: Path, row: PublicationLineageRow) -> bool:
    """Append ``row`` unless the head already IS that row (canonical equality). A head
    at the same seq that differs is a conflict ⇒ operator-required."""
    scan = read_publication_lineage(Path(proj) / LINEAGE_FILE)
    if scan.head is not None and scan.head.seq == row.seq:
        if canonical_json(scan.head.to_dict()) == canonical_json(row.to_dict()):
            return False
        raise RecoveryOperatorRequired("publication lineage head conflicts with the intent row at the same seq")
    append_publication_row(Path(proj) / LINEAGE_FILE, row)
    return True


# ── classification helpers ───────────────────────────────────────────────────

def _lineage_mentions(scan: LineageScan, generation_id: str) -> bool:
    return any(r.generation_id == generation_id for r in scan.rows)


def _make_row(*, seq: int, event: str, generation_id: str, target_receipt_sha: str, repair_row_sha: str | None,
              clock: CoverageClock, origins: Mapping[str, str], parent: str | None, prev_sha: str) -> PublicationLineageRow:
    return PublicationLineageRow(seq=seq, event=event, generation_id=generation_id, generation_receipt_sha256=target_receipt_sha,
                                 repair_row_sha256=repair_row_sha, coverage_clock=clock, source_input_origins=dict(origins),
                                 parent_generation_id=parent, previous_row_sha256=prev_sha, published_at=_now_iso())


@dataclass(frozen=True, kw_only=True)
class _Ctx:
    proj: Path
    candidate: Candidate
    diagnostics: PublicationDiagnostics


def _finish_transition(ctx: _Ctx, intent: PublicationIntent, *, target_receipt: GenerationReceipt,
                       delete_candidate: bool) -> ProjectionManifest:
    """intent (already durable) → [target must exist] → append/dedupe row → activate →
    validate → [delete candidate] → clear intent."""
    proj = ctx.proj
    target = proj / intent.target_generation_dir
    _require(target.is_dir(), f"intent target {intent.target_generation_dir} is absent; no row may be appended")
    _require(generation_receipt_sha256(target) == intent.row.generation_receipt_sha256,
             "intent target receipt does not match the row's generation_receipt_sha256")
    _append_or_dedupe(proj, intent.row)
    _crash("after_row")
    head = read_publication_lineage(proj / LINEAGE_FILE).head
    manifest = construct_manifest(target_receipt, head, builder=ctx.candidate.builder, reason=ctx.candidate.reason,
                                  diagnostics=ctx.diagnostics)
    _activate(proj, manifest)
    _crash("after_manifest")
    if delete_candidate and intent.candidate_path:
        _remove_tree(proj / intent.candidate_path)
        sync_dir(proj)
    _crash("before_intent_clear")
    _clear_intent(proj / PUBLICATION_INTENT_FILE)
    return manifest


def _relative(proj: Path, path: Path) -> str:
    return Path(path).resolve().relative_to(Path(proj).resolve()).as_posix()


# ── publish_generation ───────────────────────────────────────────────────────

def publish_generation(root: Path, candidate: Candidate, *, authority: Any,
                       publication_diagnostics: PublicationDiagnostics | None = None) -> PublicationResult:
    """D-13-41/44/54/60/77 existence-first publication. See the plan's Task A2 for the
    exact order; every outcome is a barrier only once the active manifest is durable
    and valid."""
    proj = projection_root(root)
    assert_authority(authority, proj)
    proj.mkdir(parents=True, exist_ok=True)
    manifest_in = candidate.manifest
    gid = manifest_in.generation_id
    cdir = Path(candidate.candidate_dir)
    # 1. validate the candidate (reject ⇒ candidate removed; nothing else touched)
    try:
        validate_candidate(cdir, manifest_in)
    except GenerationIntegrityError:
        _remove_tree(cdir)
        raise
    # 2. coverage-clock gate — the FIRST publication-state decision (before receipt/authority/intent)
    scan = read_publication_lineage(proj / LINEAGE_FILE)
    _require(scan.torn_tail is None, "publication lineage has a torn tail; run recover_generation_intents first")
    head = scan.head
    head_clock = head.coverage_clock if head else None
    advanced = coverage_gate(head_clock, candidate.coverage_clock, event="publish")
    _crash("after_gate")
    # 3. receipt into the candidate + fsync every candidate file + sync dir
    receipt = GenerationReceipt(candidate=manifest_in, coverage_clock=candidate.coverage_clock,
                                source_input_origins=dict(candidate.source_input_origins))
    from memory.atomic_replace import atomic_replace_bytes
    atomic_replace_bytes(cdir / RECEIPT_FILE, receipt_bytes(receipt))
    _fsync_tree(cdir)
    candidate_receipt_sha = generation_receipt_sha256(cdir)
    _crash("after_receipt")
    # 4. registry authority (idempotent)
    from memory.projection.registry_authority import persist_authority
    persist_authority(proj, (cdir / "coordinate_registry.json").read_bytes(), authority=authority,
                      introduced_by_candidate_id=gid, selected_at=_now_iso())
    _crash("after_authority")
    # 5. coverage seal (only when coverage advances; strict; before any row/manifest)
    ensure_coverage_seal(proj, candidate.coverage_clock)
    _crash("after_seal")
    dir_sync_name = "posix_fsync" if os.name == "posix" else "windows_flushfilebuffers"
    diagnostics = publication_diagnostics or _default_diagnostics(dir_sync_name)
    ctx = _Ctx(proj=proj, candidate=candidate, diagnostics=diagnostics)
    gen_dir = proj / generation_dir_name(gid)
    prev_sha = scan.head_sha256
    next_seq = (head.seq + 1) if head else 1
    parent_id = head.generation_id if (head and head.generation_id != gid) else (head.parent_generation_id if head else None)
    origins = dict(candidate.source_input_origins)
    result_common = dict(generation_id=gid, quarantined=None, repair_row_seq=None)

    if gen_dir.exists():
        # 6. classify the existing directory
        try:
            existing = validate_generation_directory(gen_dir, manifest_in.content)
        except GenerationIntegrityError:
            return _repair_corrupt_same_id(ctx, receipt=receipt, candidate_receipt_sha=candidate_receipt_sha,
                                           head=head, prev_sha=prev_sha, next_seq=next_seq, parent_id=parent_id, authority=authority)
        try:
            active = read_active_manifest(proj)
        except GenerationIntegrityError:
            active = None
        is_active = active is not None and active.generation_id == gid and head is not None and head.generation_id == gid
        target_receipt_sha = generation_receipt_sha256(gen_dir)
        if is_active and not advanced:
            # equal coverage: no intent, no row; non-authoritative diagnostics refreshed only
            _remove_tree(cdir)
            sync_dir(proj)
            manifest = construct_manifest(existing, head, builder=candidate.builder, reason=candidate.reason, diagnostics=diagnostics)
            _activate(proj, manifest)
            _gc(proj, active_id=gid, scan=read_publication_lineage(proj / LINEAGE_FILE))
            return PublicationResult(outcome="reused_existing", active_manifest=manifest, publication_row_seq=head.seq,
                                     publication_row_written=False, coverage_advanced=False, **result_common)
        if is_active:
            event = "reused_existing"
        elif _lineage_mentions(scan, gid):
            event = "reactivated_retained"
        else:
            event = "adopted_orphan"
        row = _make_row(seq=next_seq, event=event, generation_id=gid, target_receipt_sha=target_receipt_sha, repair_row_sha=None,
                        clock=candidate.coverage_clock, origins=origins,
                        parent=(head.generation_id if head and head.generation_id != gid else (head.parent_generation_id if head else None)),
                        prev_sha=prev_sha)
        intent = PublicationIntent(row=row, target_generation_dir=generation_dir_name(gid), candidate_path=_relative(proj, cdir),
                                   candidate_receipt_sha256=candidate_receipt_sha, prepared_at=_now_iso())
        _write_intent(proj / PUBLICATION_INTENT_FILE, intent.to_dict())
        _crash("after_intent")
        manifest = _finish_transition(ctx, intent, target_receipt=existing, delete_candidate=True)
        _gc(proj, active_id=gid, scan=read_publication_lineage(proj / LINEAGE_FILE))
        return PublicationResult(outcome=event, active_manifest=manifest, publication_row_seq=row.seq, publication_row_written=True,
                                 coverage_advanced=(head is not None and advanced), **result_common)

    # 7. published_new: intent → rename → validate target → row → manifest → clear intent
    row = _make_row(seq=next_seq, event="published_new", generation_id=gid, target_receipt_sha=candidate_receipt_sha, repair_row_sha=None,
                    clock=candidate.coverage_clock, origins=origins, parent=(head.generation_id if head else None), prev_sha=prev_sha)
    intent = PublicationIntent(row=row, target_generation_dir=generation_dir_name(gid), candidate_path=_relative(proj, cdir),
                               candidate_receipt_sha256=candidate_receipt_sha, prepared_at=_now_iso())
    _write_intent(proj / PUBLICATION_INTENT_FILE, intent.to_dict())
    _crash("after_intent")
    os.rename(cdir, gen_dir)
    sync_dir(proj)
    _crash("after_rename")
    validate_generation_directory(gen_dir, receipt)
    manifest = _finish_transition(ctx, intent, target_receipt=receipt, delete_candidate=False)
    _gc(proj, active_id=gid, scan=read_publication_lineage(proj / LINEAGE_FILE))
    return PublicationResult(outcome="published_new", active_manifest=manifest, publication_row_seq=row.seq, publication_row_written=True,
                             coverage_advanced=(head is not None and advanced), **result_common)


# ── two-stage repair (D-13-25/47/51/55/61) ───────────────────────────────────

def _quarantine_name(proj: Path, generation_id: str) -> str:
    stamp = _now_iso().replace(":", "").replace("+", "").replace(".", "")
    n = 1
    while (proj / QUARANTINE_DIR / f"{generation_dir_name(generation_id)}.corrupt-{stamp}-{n}").exists():
        n += 1
    return f"{QUARANTINE_DIR}/{generation_dir_name(generation_id)}.corrupt-{stamp}-{n}"


def _repair_corrupt_same_id(ctx: _Ctx, *, receipt: GenerationReceipt, candidate_receipt_sha: str, head: PublicationLineageRow | None,
                            prev_sha: str, next_seq: int, parent_id: str | None, authority: Any) -> PublicationResult:
    proj = ctx.proj
    gid = receipt.candidate.generation_id
    cdir = Path(ctx.candidate.candidate_dir)
    gen_dir = proj / generation_dir_name(gid)
    _wait_zero_leases(proj, gid)
    rscan = read_repair_lineage(proj)
    payload = RepairIntentPayload(
        generation_id=gid, candidate_path=_relative(proj, cdir), candidate_receipt_sha256=candidate_receipt_sha,
        quarantine_path=_quarantine_name(proj, gid), corrupt_tree_sha256=tree_sha256(gen_dir),
        replacement_tree_sha256=tree_sha256(cdir), prior_repair_row_sha256=rscan.head_sha256, prepared_at=_now_iso(),
    )
    _write_intent(proj / REPAIR_INTENT_FILE, RepairIntent(payload=payload).to_dict())
    _crash("after_repair_intent")
    return _resume_repair(proj, RepairIntent(payload=payload), builder=ctx.candidate.builder, reason=ctx.candidate.reason,
                          diagnostics=ctx.diagnostics, authority=authority, clock=receipt.coverage_clock, origins=receipt.source_input_origins)


def _resume_repair(proj: Path, ri: RepairIntent, *, builder: str, reason: str, diagnostics: PublicationDiagnostics, authority: Any,
                   clock: CoverageClock | None, origins: Mapping[str, str] | None) -> PublicationResult:
    """Drive the two-stage repair from whatever durable state exists (idempotent resume)."""
    assert_authority(authority, proj)
    p = ri.payload
    gid = p.generation_id
    gen_dir = proj / generation_dir_name(gid)
    cdir = proj / p.candidate_path
    qdir = proj / p.quarantine_path
    # Stage 1a: quarantine (skip if already done, cancel if gen-G re-validates and is not the replacement)
    if not qdir.exists():
        replaced_already = gen_dir.exists() and (gen_dir / RECEIPT_FILE).is_file() and generation_receipt_sha256(gen_dir) == p.candidate_receipt_sha256
        if not replaced_already:
            still_corrupt = True
            if gen_dir.exists():
                try:
                    validate_generation_directory(gen_dir, read_receipt(gen_dir))
                    still_corrupt = False
                except GenerationIntegrityError:
                    still_corrupt = True
            if not still_corrupt:
                # cancel: the active directory is valid; do not touch it
                _remove_tree(cdir)
                _clear_intent(proj / REPAIR_INTENT_FILE)
                raise RecoveryOperatorRequired("repair intent cancelled: generation re-validated; candidate removed")
            if gen_dir.exists():
                qdir.parent.mkdir(parents=True, exist_ok=True)
                os.rename(gen_dir, qdir)
                sync_dir(qdir.parent); sync_dir(proj)
    _crash("after_quarantine")
    # Stage 1b: replacement rename
    if not gen_dir.exists():
        _require(cdir.is_dir(), "repair: replacement candidate missing and target absent (operator-required)")
        _require(generation_receipt_sha256(cdir) == p.candidate_receipt_sha256, "repair: candidate receipt does not match the intent")
        os.rename(cdir, gen_dir)
        sync_dir(proj)
    _require(generation_receipt_sha256(gen_dir) == p.candidate_receipt_sha256, "repair: gen dir is neither the corrupt original nor the replacement")
    _crash("after_candidate_rename")
    replacement = validate_generation_directory(gen_dir, read_receipt(gen_dir))
    # Stage 1c: repair row (exactly one)
    rscan = read_repair_lineage(proj)
    intent_sha = repair_intent_sha256(p)
    existing_row = next((r for r in rscan.rows if r.intent_sha256 == intent_sha), None)
    if existing_row is None:
        _require(rscan.torn_tail is None, "repair lineage has a torn tail (operator-required)")
        _require(rscan.head_sha256 == p.prior_repair_row_sha256, "repair lineage advanced since the intent (operator-required)")
        existing_row = RepairLineageRow(seq=(rscan.rows[-1].seq + 1) if rscan.rows else 1, intent=p, intent_sha256=intent_sha,
                                        previous_row_sha256=rscan.head_sha256, repaired_at=_now_iso())
        _append_repair_row(proj, existing_row)
    rrow_sha = repair_row_sha256(existing_row)
    _crash("after_repair_row")
    # Stage 2: settle publication lineage, then a SEPARATE rebind PublicationIntent
    _settle_torn_lineage_tail(proj)
    scan = read_publication_lineage(proj / LINEAGE_FILE)
    pi = read_publication_intent(proj)
    if pi is None or pi.row.event != "replaced_corrupt" or pi.row.repair_row_sha256 != rrow_sha:
        head = scan.head
        row = _make_row(seq=(head.seq + 1) if head else 1, event="replaced_corrupt", generation_id=gid,
                        target_receipt_sha=generation_receipt_sha256(gen_dir), repair_row_sha=rrow_sha,
                        clock=(clock or replacement.coverage_clock), origins=(origins if origins is not None else replacement.source_input_origins),
                        parent=(head.generation_id if head and head.generation_id != gid else (head.parent_generation_id if head else None)),
                        prev_sha=scan.head_sha256)
        # dedupe against an already-appended rebind row (crash after row before manifest)
        if head is not None and head.event == "replaced_corrupt" and head.repair_row_sha256 == rrow_sha and head.generation_id == gid:
            row = head
        pi = PublicationIntent(row=row, target_generation_dir=generation_dir_name(gid), candidate_path=None,
                               candidate_receipt_sha256=None, prepared_at=_now_iso())
        _write_intent(proj / PUBLICATION_INTENT_FILE, pi.to_dict())
    _crash("after_rebind_intent")
    ctx = _Ctx(proj=proj, candidate=Candidate(candidate_dir=cdir, manifest=replacement.candidate, coverage_clock=replacement.coverage_clock,
                                              source_input_origins=dict(replacement.source_input_origins), builder=builder, reason=reason),
               diagnostics=diagnostics)
    manifest = _finish_transition(ctx, pi, target_receipt=replacement, delete_candidate=False)
    _clear_intent(proj / REPAIR_INTENT_FILE)
    _bound_quarantine(proj)
    _gc(proj, active_id=gid, scan=read_publication_lineage(proj / LINEAGE_FILE))
    return PublicationResult(outcome="replaced_corrupt", generation_id=gid, active_manifest=manifest, publication_row_seq=pi.row.seq,
                             publication_row_written=True, coverage_advanced=(pi.row.coverage_clock != (scan.head.coverage_clock if scan.head else None)),
                             quarantined=p.quarantine_path, repair_row_seq=existing_row.seq)


def _bound_quarantine(proj: Path) -> None:
    qroot = Path(proj) / QUARANTINE_DIR
    if not qroot.is_dir():
        return
    entries = sorted([d for d in qroot.iterdir() if d.is_dir()], key=lambda d: d.name)
    for old in entries[:-QUARANTINE_KEEP] if len(entries) > QUARANTINE_KEEP else []:
        _remove_tree(old)
    sync_dir(qroot)


# ── lease-aware GC (D-13-4) ──────────────────────────────────────────────────

def _gc(proj: Path, *, active_id: str, scan: LineageScan) -> list[str]:
    """Keep the active generation + 1 previous (the most recent distinct generation in
    lineage whose directory exists), every intent-named path, every leased generation
    (deferred). Older generations, unclaimed orphans and stale candidates are removed."""
    proj = Path(proj)
    keep: set[Path] = {proj / generation_dir_name(active_id)}
    previous: str | None = None
    for row in reversed(scan.rows):
        if row.generation_id != active_id:
            previous = row.generation_id
            break
    if previous:
        keep.add(proj / generation_dir_name(previous))
    keep |= intent_protected_paths(proj)
    removed: list[str] = []
    for entry in sorted(proj.iterdir()):
        name = entry.name
        if not (name.startswith(GENERATION_DIR_PREFIX) and entry.is_dir()):
            continue
        if entry in keep:
            continue
        gid = name[len(GENERATION_DIR_PREFIX):]
        if gid.endswith(CANDIDATE_SUFFIX):
            gid = gid[: -len(CANDIDATE_SUFFIX)]
        if generation_is_leased(proj, gid):
            continue
        _remove_tree(entry)
        removed.append(name)
    if removed:
        sync_dir(proj)
    return removed


# ── intent-first recovery (D-13-54/60/61; run under the dispatcher's token) ──

@dataclass(frozen=True, kw_only=True)
class RecoveryReport:
    actions: tuple[str, ...]
    status: Literal["clean", "completed", "cancelled", "publication_incomplete", "operator_required"]
    active_generation_id: str | None


def recover_generation_intents(root: Path, *, authority: Any, builder: str = "cut13", reason: str = "recovery",
                               diagnostics: PublicationDiagnostics | None = None) -> RecoveryReport:
    """Complete or cancel a live RepairIntent / PublicationIntent (event-specific), settle
    a torn lineage tail, remove stale candidates NOT named by an intent. Never invents a
    lost row; a torn tail without a qualifying intent restores the prior chain and
    reports ``publication_incomplete`` (the retry obligation is the dispatch itself)."""
    proj = projection_root(root)
    assert_authority(authority, proj)
    proj.mkdir(parents=True, exist_ok=True)
    diagnostics = diagnostics or _default_diagnostics("posix_fsync" if os.name == "posix" else "windows_flushfilebuffers")
    actions: list[str] = []
    status = "clean"
    ri = read_repair_intent(proj)
    if ri is not None:
        try:
            _resume_repair(proj, ri, builder=builder, reason=reason, diagnostics=diagnostics, authority=authority, clock=None, origins=None)
            actions.append("repair_completed"); status = "completed"
        except RecoveryOperatorRequired as exc:
            if "cancelled" in str(exc):
                actions.append("repair_cancelled"); status = "cancelled"
            else:
                raise
    pi = read_publication_intent(proj)
    torn = _settle_torn_lineage_tail(proj)
    if torn:
        actions.append("torn_tail_settled")
    if pi is not None:
        scan = read_publication_lineage(proj / LINEAGE_FILE)
        target = proj / pi.target_generation_dir
        cand = (proj / pi.candidate_path) if pi.candidate_path else None
        row = pi.row
        head_is_row = scan.head is not None and canonical_json(scan.head.to_dict()) == canonical_json(row.to_dict())
        if not head_is_row:
            _require(scan.head_sha256 == row.previous_row_sha256 and ((scan.head.seq + 1) if scan.head else 1) == row.seq,
                     "publication intent does not continue the current lineage head (operator-required)")
        if row.event == "published_new":
            if not target.exists():
                if cand is None or not cand.is_dir():
                    raise RecoveryOperatorRequired("published_new intent: target and candidate both absent (metadata loss)")
                _require(generation_receipt_sha256(cand) == row.generation_receipt_sha256, "published_new intent: candidate receipt mismatch")
                os.rename(cand, target)
                sync_dir(proj)
                actions.append("renamed_candidate")
            receipt = validate_generation_directory(target, read_receipt(target))
            _require(generation_receipt_sha256(target) == row.generation_receipt_sha256, "published_new intent: target receipt mismatch")
            ctx = _Ctx(proj=proj, candidate=Candidate(candidate_dir=target, manifest=receipt.candidate, coverage_clock=receipt.coverage_clock,
                                                      source_input_origins=dict(receipt.source_input_origins), builder=builder, reason=reason),
                       diagnostics=diagnostics)
            _finish_transition(ctx, pi, target_receipt=receipt, delete_candidate=False)
        else:
            _require(target.is_dir(), f"{row.event} intent: target absent (operator-required)")
            _require(generation_receipt_sha256(target) == row.generation_receipt_sha256, f"{row.event} intent: target receipt mismatch")
            receipt = validate_generation_directory(target, read_receipt(target))
            if cand is not None and cand.exists():
                _require(generation_receipt_sha256(cand) == pi.candidate_receipt_sha256, f"{row.event} intent: candidate receipt mismatch")
                cand_receipt = read_receipt(cand)
                _require(cand_receipt.coverage_clock == row.coverage_clock and
                         json.loads(canonical_json(cand_receipt.source_input_origins)) == json.loads(canonical_json(row.source_input_origins)),
                         f"{row.event} intent: candidate coverage/origins disagree with the row")
            ctx = _Ctx(proj=proj, candidate=Candidate(candidate_dir=cand or target, manifest=receipt.candidate, coverage_clock=row.coverage_clock,
                                                      source_input_origins=dict(row.source_input_origins), builder=builder, reason=reason),
                       diagnostics=diagnostics)
            _finish_transition(ctx, pi, target_receipt=receipt, delete_candidate=cand is not None)
        actions.append(f"{row.event}_completed"); status = "completed"
    elif torn:
        status = "publication_incomplete"
    # startup cleanup: stale candidates not named by a live intent
    protected = intent_protected_paths(proj)
    for entry in sorted(proj.iterdir()):
        if entry.is_dir() and entry.name.startswith(GENERATION_DIR_PREFIX) and entry.name.endswith(CANDIDATE_SUFFIX) and entry not in protected:
            _remove_tree(entry); actions.append(f"removed_stale_candidate:{entry.name}")
    try:
        active = read_active_manifest(proj)
        active_id = active.generation_id if active else None
    except GenerationIntegrityError:
        active_id = None
    return RecoveryReport(actions=tuple(actions), status=status, active_generation_id=active_id)


# ═════════════════════════════════════════════════════════════════════════════
# Task A3 / A3b — generation views (D-13-2/16/19/28), OS-held shared leases
# (D-13-37/75), the reentrant projection-read transaction (view-or-unavailable),
# read-side immutability (never repair, never rebuild, never write).
# ═════════════════════════════════════════════════════════════════════════════

import contextvars
import threading

_LEASE_NAMESPACE_WAIT = 5.0


class GenerationViewRequired(GenerationIntegrityError):
    """A derived-artifact path/read was requested under generation mode without an
    ambient GenerationView (D-13-19/28): no unleased Path is ever returned."""


class GenerationUnavailable(GenerationIntegrityError):
    """A manifest exists but the active generation cannot be opened (invalid manifest,
    missing directory/receipt, artifact mismatch). Never a legacy fallback (D-13-16)."""


@dataclass(frozen=True, kw_only=True)
class ManifestState:
    mode: Literal["legacy", "generation", "unavailable"]
    manifest: ProjectionManifest | None
    reason: str | None


def manifest_state(proj: Path) -> ManifestState:
    """legacy ⇔ no manifest file at all; unavailable ⇔ a manifest exists but is invalid or
    names a missing generation/receipt; generation ⇔ openable."""
    proj = Path(proj)
    try:
        manifest = read_active_manifest(proj)
    except GenerationIntegrityError as exc:
        return ManifestState(mode="unavailable", manifest=None, reason=f"manifest_invalid: {exc}")
    if manifest is None:
        return ManifestState(mode="legacy", manifest=None, reason=None)
    gen_dir = proj / manifest.generation_dir
    if manifest.generation_dir != generation_dir_name(manifest.generation_id):
        return ManifestState(mode="unavailable", manifest=manifest, reason="manifest_generation_dir_mismatch")
    if not gen_dir.is_dir():
        return ManifestState(mode="unavailable", manifest=manifest, reason="generation_directory_missing")
    if not (gen_dir / RECEIPT_FILE).is_file():
        return ManifestState(mode="unavailable", manifest=manifest, reason="generation_receipt_missing")
    return ManifestState(mode="generation", manifest=manifest, reason=None)


# ── shared lease handles (POSIX flock / Windows LockFileEx) ──────────────────

class _LeaseHandle:
    """An open handle holding a SHARED OS lock for the view's lifetime (D-13-37)."""

    __slots__ = ("path", "fd", "live")

    def __init__(self, path: Path, fd: int) -> None:
        self.path, self.fd, self.live = path, fd, True

    def release(self) -> None:
        if self.live:
            self.live = False
            _unlock_shared(self.fd)
            os.close(self.fd)


def _lock_shared_nb(fd: int) -> bool:
    if os.name == "posix":
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    return _win_lockfileex(fd, exclusive=False)  # pragma: no cover - Windows only


def _unlock_shared(fd: int) -> None:
    if os.name == "posix":
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
    else:  # pragma: no cover
        _win_unlockfileex(fd)


def _win_lockfileex(fd: int, *, exclusive: bool) -> bool:  # pragma: no cover - Windows only
    import msvcrt
    LOCKFILE_FAIL_IMMEDIATELY, LOCKFILE_EXCLUSIVE_LOCK = 0x1, 0x2
    handle = msvcrt.get_osfhandle(fd)
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    overlapped = ctypes.create_string_buffer(32)
    flags = LOCKFILE_FAIL_IMMEDIATELY | (LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0)
    return bool(kernel32.LockFileEx(ctypes.c_void_p(handle), flags, 0, 1, 0, overlapped))


def _win_unlockfileex(fd: int) -> None:  # pragma: no cover - Windows only
    import msvcrt
    handle = msvcrt.get_osfhandle(fd)
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    overlapped = ctypes.create_string_buffer(32)
    kernel32.UnlockFileEx(ctypes.c_void_p(handle), 0, 1, 0, overlapped)


def acquire_shared_lease(proj: Path, generation_id: str) -> _LeaseHandle:
    """Enrol as a reader of ``generation_id``: take the lease NAMESPACE gate SHARED for
    the duration of enrolment (D-13-75; reset holds it EXCLUSIVE), then hold the
    generation lease SHARED for the view's lifetime."""
    proj = Path(proj)
    ns_fd = _open_lock_handle(proj / LEASE_NAMESPACE_LOCK_FILE)
    try:
        deadline = time.monotonic() + _LEASE_NAMESPACE_WAIT
        while not _lock_shared_nb(ns_fd):
            if time.monotonic() >= deadline:
                raise GenerationUnavailable("lease namespace is held exclusively (reset in progress); enrolment refused")
            time.sleep(0.02)
        path = lease_path(proj, generation_id)
        fd = _open_lock_handle(path)
        if not _lock_shared_nb(fd):
            os.close(fd)
            raise GenerationUnavailable(f"generation {generation_id[:12]}… lease is held exclusively; enrolment refused")
        return _LeaseHandle(path, fd)
    finally:
        _unlock_shared(ns_fd)
        os.close(ns_fd)


# ── the view ─────────────────────────────────────────────────────────────────

_ARTIFACT_NAMES = {name for _, name in GENERATION_ARTIFACTS}
_sqlite_verified: dict[tuple[str, int], str] = {}   # (generation_id, size) → sha, verified once per process
_sqlite_verified_lock = threading.Lock()


class GenerationView:
    """ONE captured generation (D-13-2): the manifest read once, every derived path
    resolved inside gen-<id>, a shared OS lease held for the view's lifetime. Readers
    bind graph + registry + SRAM + row index + planes to this object and never observe
    files from two generations. Reads verify artifact bytes against the manifest and
    NEVER repair, rebuild or write."""

    def __init__(self, proj: Path, manifest: ProjectionManifest, lease: _LeaseHandle | None) -> None:
        self.projection_root = Path(proj)
        self.manifest = manifest
        self.generation_id = manifest.generation_id
        self.generation_dir = self.projection_root / manifest.generation_dir
        self._lease = lease
        self._depth = 0
        self.closed = False

    # -- paths -----------------------------------------------------------------
    def path(self, artifact: str) -> Path:
        """Resolved path of a bound artifact (fixed names) or a plane path (relative)."""
        if artifact in _ARTIFACT_NAMES:
            return self.generation_dir / artifact
        for plane in self.manifest.content.planes:
            if plane.plane_id == artifact or plane.path == artifact:
                return self.generation_dir / plane.path
        if self._semantic_retina_sha256(artifact) is not None:
            return self.generation_dir / artifact
        raise GenerationIntegrityError(f"artifact {artifact!r} is not bound by generation {self.generation_id[:12]}…")

    def _semantic_retina_sha256(self, artifact: str) -> str | None:
        """The pinned hash of a built retina artifact; None when not bound."""
        retina = self.manifest.content.components.get("semantic_retina")
        if not isinstance(retina, dict) or retina.get("status") != "built":
            return None
        for field_name, file_name in SEMANTIC_RETINA_ARTIFACTS:
            if file_name == artifact:
                return retina.get(field_name)
        return None

    @property
    def graph_sqlite(self) -> Path: return self.path("graph.sqlite")
    @property
    def graph_snapshot(self) -> Path: return self.path("graph_snapshot.json")
    @property
    def coordinate_registry(self) -> Path: return self.path("coordinate_registry.json")
    @property
    def coord_index(self) -> Path: return self.path("coord_index.snapshot.json")
    @property
    def sram(self) -> Path: return self.path("sram.bin")
    @property
    def sram_meta(self) -> Path: return self.path("sram_meta.json")
    @property
    def sram_index(self) -> Path: return self.path("sram_index.json")
    @property
    def dme_manifest(self) -> Path: return self.path("dme_manifest.json")

    def plane_paths(self) -> dict[str, Path]:
        return {p.plane_id: self.generation_dir / p.path for p in self.manifest.content.planes}

    # -- verified reads (immutability; fail loud, never repair) -----------------
    def expected_sha256(self, artifact: str) -> str:
        for field_name, file_name in GENERATION_ARTIFACTS:
            if file_name == artifact:
                return getattr(self.manifest.content, field_name)
        for plane in self.manifest.content.planes:
            if plane.plane_id == artifact or plane.path == artifact:
                return plane.sha256
        retina_sha = self._semantic_retina_sha256(artifact)
        if retina_sha is not None:
            return retina_sha
        raise GenerationIntegrityError(f"artifact {artifact!r} is not bound by generation {self.generation_id[:12]}…")

    def read_bytes(self, artifact: str) -> bytes:
        """Read a bound artifact and verify its sha256 against the manifest. A missing,
        corrupt or mismatched artifact raises GenerationIntegrityError (lane unavailable);
        nothing is created, repaired or rewritten."""
        path = self.path(artifact)
        if not path.is_file():
            raise GenerationUnavailable(f"generation {self.generation_id[:12]}… artifact missing: {artifact}")
        data = path.read_bytes()
        actual = sha256_bytes(data)
        expected = self.expected_sha256(artifact)
        if actual != expected:
            raise GenerationUnavailable(f"generation {self.generation_id[:12]}… artifact {artifact} corrupt: {actual[:12]}… != {expected[:12]}…")
        return data

    def verified_path(self, artifact: str) -> Path:
        """Path of an artifact after verifying it (bytes hashed; graph.sqlite verified once
        per process per generation + size-checked on every call — files inside gen-* are
        immutable by law and readers open the sqlite read-only)."""
        path = self.path(artifact)
        if artifact == "graph.sqlite":
            if not path.is_file():
                raise GenerationUnavailable(f"generation {self.generation_id[:12]}… graph.sqlite missing")
            size = path.stat().st_size
            key = (self.generation_id, size)
            with _sqlite_verified_lock:
                cached = _sqlite_verified.get(key)
            if cached is None:
                actual = sha256_file(path)
                if actual != self.expected_sha256(artifact):
                    raise GenerationUnavailable(f"generation {self.generation_id[:12]}… graph.sqlite corrupt")
                with _sqlite_verified_lock:
                    _sqlite_verified[key] = actual
            return path
        self.read_bytes(artifact)
        return path

    # -- lifecycle -----------------------------------------------------------------
    def close(self) -> None:
        if not self.closed:
            self.closed = True
            if self._lease is not None:
                self._lease.release()

    def __repr__(self) -> str:
        return f"GenerationView({self.generation_id[:12]}…, dir={self.generation_dir.name})"


def open_generation_view(root: Path, *, lease: bool = True) -> GenerationView:
    """Read the active manifest ONCE, verify the receipt names the same content identity,
    take the shared lease, and return the view. Raises GenerationUnavailable when a
    manifest exists but the generation cannot be opened; raises GenerationViewRequired?
    no — callers with no manifest use manifest_state()/legacy paths."""
    proj = projection_root(root)
    state = manifest_state(proj)
    if state.mode == "legacy":
        raise GenerationUnavailable("no manifest: legacy mode (no view to open)")
    if state.mode == "unavailable":
        raise GenerationUnavailable(state.reason or "unavailable")
    manifest = state.manifest
    handle = acquire_shared_lease(proj, manifest.generation_id) if lease else None
    try:
        gen_dir = proj / manifest.generation_dir
        receipt = read_receipt(gen_dir)
        if receipt.candidate.generation_id != manifest.generation_id or receipt.candidate.content != manifest.content:
            raise GenerationUnavailable("generation receipt disagrees with the active manifest")
    except GenerationIntegrityError:
        if handle is not None:
            handle.release()
        raise
    return GenerationView(proj, manifest, handle)


# ── ambient view / reentrant read transaction (D-13-19/28) ───────────────────

_ambient: contextvars.ContextVar["ProjectionReadContext | None"] = contextvars.ContextVar("omega_projection_read_ctx", default=None)


@dataclass(eq=False)
class ProjectionReadContext:
    """What a read transaction yields: exactly one of a live view (generation mode), a
    legacy marker (no manifest), or an unavailable marker (manifest present but the
    lane cannot be served — canonical recall + verifier continue over current truth)."""

    root: Path
    mode: Literal["legacy", "generation", "unavailable"]
    view: GenerationView | None
    reason: str | None
    depth: int = 0

    @property
    def generation_id(self) -> str | None:
        return self.view.generation_id if self.view else None


def current_read_context() -> ProjectionReadContext | None:
    return _ambient.get()


def current_view() -> GenerationView | None:
    ctx = _ambient.get()
    return ctx.view if ctx is not None else None


@contextmanager
def projection_read_transaction(root: Path) -> Iterator[ProjectionReadContext]:
    """ONE reentrant transaction around every derived read (D-13-28). The outermost
    entry captures the mode/view once; nested entries for the same root reuse it (the
    whole operation stays on one generation even if G+1 publishes mid-call). Yields
    view-or-unavailable — never aborts canonical recall."""
    root = resolve_memory_root(root)
    existing = _ambient.get()
    if existing is not None and Path(existing.root) == root:
        existing.depth += 1
        try:
            yield existing
        finally:
            existing.depth -= 1
        return
    proj = projection_root(root)
    state = manifest_state(proj)
    ctx: ProjectionReadContext
    if state.mode == "legacy":
        ctx = ProjectionReadContext(root=root, mode="legacy", view=None, reason=None)
    elif state.mode == "unavailable":
        ctx = ProjectionReadContext(root=root, mode="unavailable", view=None, reason=state.reason)
    else:
        try:
            view = open_generation_view(root)
            ctx = ProjectionReadContext(root=root, mode="generation", view=view, reason=None)
        except GenerationIntegrityError as exc:
            ctx = ProjectionReadContext(root=root, mode="unavailable", view=None, reason=str(exc))
    token = _ambient.set(ctx)
    try:
        yield ctx
    finally:
        _ambient.reset(token)
        if ctx.view is not None:
            ctx.view.close()


def resolve_view_or_legacy(root: Path) -> tuple[Literal["legacy", "generation"], GenerationView | None]:
    """Path helpers call this: no manifest ⇒ ('legacy', None) — today's fixed names;
    manifest + ambient view ⇒ ('generation', view); manifest + NO ambient view ⇒
    GenerationViewRequired (no unleased Path is ever returned); manifest invalid ⇒
    GenerationUnavailable."""
    root = resolve_memory_root(root)
    ctx = _ambient.get()
    if ctx is not None and Path(ctx.root) == root:
        if ctx.mode == "legacy":
            return "legacy", None
        if ctx.mode == "unavailable":
            raise GenerationUnavailable(ctx.reason or "generation unavailable")
        return "generation", ctx.view
    state = manifest_state(projection_root(root))
    if state.mode == "legacy":
        return "legacy", None
    if state.mode == "unavailable":
        raise GenerationUnavailable(state.reason or "generation unavailable")
    raise GenerationViewRequired("a manifest exists: derived reads require an ambient projection_read_transaction")


# ── read helpers used by the converted readers (Task A3) ─────────────────────

def active_graph_snapshot_hash(root: Path | str | None) -> str | None:
    """The graph snapshot hash the read path must report: the AMBIENT view's manifest
    hash inside a transaction (one generation for the whole operation), else the active
    manifest's hash, or None in legacy mode (callers fall back to today's file read).
    A manifest that exists but is invalid ⇒ GenerationUnavailable (never legacy)."""
    mroot = resolve_memory_root(root)
    ctx = _ambient.get()
    if ctx is not None and Path(ctx.root) == mroot:
        if ctx.mode == "legacy":
            return None
        if ctx.mode == "unavailable":
            raise GenerationUnavailable(ctx.reason or "generation unavailable")
        return ctx.view.manifest.content.graph_snapshot_hash
    state = manifest_state(projection_root(mroot))
    if state.mode == "legacy":
        return None
    if state.mode == "unavailable":
        raise GenerationUnavailable(state.reason or "generation unavailable")
    return state.manifest.content.graph_snapshot_hash


def connect_readonly(path: Path):
    """Open a published graph.sqlite READ-ONLY (URI mode=ro, immutable): no schema
    creation, no journal_mode change, no -wal/-shm; INSERT raises."""
    import sqlite3
    path = Path(path)
    if not path.is_file():
        raise GenerationUnavailable(f"graph.sqlite missing: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


# ═════════════════════════════════════════════════════════════════════════════
# Task A5 — watermarks (canonical-snapshot authority), sequenced=False posture,
# projection health (16 §6: name lag/backlog/lane state; never claim "current"
# with nonzero lag or pending work). Part A: no outbox consumption — the outbox
# is read as a DIAGNOSTIC exactly as Cut 4's health already does.
# ═════════════════════════════════════════════════════════════════════════════

UNSEQUENCED_POSTURE = "covered_by_full_rebuild"


def generation_authority_exists(proj: Path) -> tuple[bool, str | None]:
    """Any surviving generation authority — a manifest (valid OR corrupt), publication
    lineage, a registry-authority manifest, a gen-* directory, a live intent."""
    proj = Path(proj)
    if (proj / MANIFEST_FILE).exists():
        return True, "manifest"
    if (proj / LINEAGE_FILE).exists() and (proj / LINEAGE_FILE).stat().st_size > 0:
        return True, "publication_lineage"
    if (proj / "registry_authority" / "manifest.json").exists():
        return True, "registry_authority"
    if any(p.is_dir() and p.name.startswith(GENERATION_DIR_PREFIX) and not p.name.endswith(CANDIDATE_SUFFIX) for p in proj.iterdir()) if proj.is_dir() else False:
        return True, "generation_directory"
    if (proj / PUBLICATION_INTENT_FILE).exists() or (proj / REPAIR_INTENT_FILE).exists():
        return True, "live_intent"
    return False, None


@dataclass(frozen=True, kw_only=True)
class ProjectionHealth:
    generation: str                        # "legacy" | "<generation_id>" | "unavailable"
    semantic_dme_lane: Literal["legacy", "available", "unavailable"]
    lane_reason: str | None
    current: bool                          # NEVER True with lag > 0, pending > 0, dirty, or any lane_reason
    watermark_commit_seq: int | None       # == the publication-lineage head's coverage (canonical-snapshot authority)
    latest_canonical_commit_seq: int | None   # live: core.timekeeper.last_commit_seq() (reserved / issued)
    lag_commits: int | None
    head_clock: dict | None
    live_clock: dict | None
    reserved_seq: int | None
    landed_seq: int | None
    coverage_seal: dict | None
    pending_delta_count: int               # VALID complete pending rows only
    malformed_delta_count: int             # malformed / tampered COMPLETE rows — loud evidence, never counted as pending
    pending_partial_bytes: int             # unterminated trailing fragment beyond the settled cursor (D-13-31: pending, never parsed)
    outbox_error: str | None               # unreadable outbox ⇒ the OSError text; None when readable
    unsequenced_pending: int
    unsequenced_posture: str
    oldest_pending_age_s: float | None
    dirty: bool
    blocked_on_obligations: bool
    page0_utilization_by_lobe: dict
    live_intents: tuple[str, ...]
    dormant_generation_intent: bool        # a publication/repair intent left where nothing publishes
    program0: dict | None = None           # (D-13-52/66): bootstrap state, epoch, cursor, malformed receipts, recovery pending — None when no Program-0 state survives

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _oldest_age_seconds(queued_at_values: list[str]) -> float | None:
    from datetime import datetime, timezone
    oldest = None
    for value in queued_at_values:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if oldest is None or ts < oldest:
            oldest = ts
    if oldest is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - oldest).total_seconds())


def _program0_consumed_offset(program0: dict | None, memory_root: Path) -> int | None:
    """The settled Program-0 cursor offset when it binds the CURRENT outbox file (same physical
    identity, offset within the file); None otherwise (legacy / pre-bootstrap / corrupt state — the
    drain's rotate-first law decides, health falls back to whole-file counts)."""
    if not program0 or program0.get("cursor_offset") is None:
        return None
    try:
        from memory.projection.projector import read_cursor
        from memory.projection_delta import outbox_file_identity_of, outbox_path
        cursor = read_cursor(memory_root / PROJECTION_DIR)
        path = outbox_path(memory_root)
        fd = os.open(path, os.O_RDONLY | _O_BINARY)
        try:
            identity, size = outbox_file_identity_of(fd), os.fstat(fd).st_size
        finally:
            os.close(fd)
        if cursor is None or cursor.outbox_file_identity != identity or cursor.outbox_byte_offset > size:
            return None
        return int(cursor.outbox_byte_offset)
    except Exception:  # noqa: BLE001
        return None


def _rows_beyond(path: Path, offset: int) -> tuple[list, int, int]:
    """Beyond ``offset``: (parsed deltas, malformed COMPLETE rows incl. blank/whitespace, unterminated
    trailing bytes). The trailing fragment is D-13-31 PENDING evidence — never parsed, never a
    malformed row until it becomes LF-framed — but it is unconsumed work (review a045ec4a §3)."""
    from memory.projection_delta import ProjectionDelta
    try:
        data = Path(path).read_bytes()[offset:]
    except OSError:
        return [], 0, 0
    deltas: list = []
    malformed = 0
    rows = data.split(b"\n")
    partial = len(rows[-1])
    for row in rows[:-1]:
        if not row.strip():
            malformed += 1
            continue
        try:
            deltas.append(ProjectionDelta.from_dict(json.loads(row.decode("utf-8"))))
        except Exception:  # noqa: BLE001
            malformed += 1
    return deltas, malformed, partial


def _blank_complete_rows(path: Path) -> int:
    """D-13-66 (review 3b84d003): a COMPLETE (LF-framed) row that is empty or whitespace-only is
    malformed evidence — the producer never emits one — and COUNTS. The Cut-4 diagnostic reader
    (frozen in Part A, D-13-53) skips such rows, so Part-A health counts them here from the raw
    bytes. An unterminated final fragment is not a complete row (Part-B tail policy) and an
    empty file has zero rows."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return 0
    return sum(1 for row in data.split(b"\n")[:-1] if not row.strip())


def _program0_state(root: Path | str | None, generation_authority: bool) -> dict | None:
    """Program 0's read-only consumption state for health (D-13-52: bootstrap state surfaced;
    D-13-66: malformed receipts visible after settlement + restart). None when the Program-0
    module is absent or nothing Program-0-related exists yet on a legacy profile."""
    try:
        from memory.projection.projector import consumption_state
    except ImportError:  # pragma: no cover - a build without Part B
        return None
    try:
        state = consumption_state(root)
    except Exception as exc:  # noqa: BLE001 — never a crash out of health
        return {"bootstrap_state": "unreadable", "error": f"{type(exc).__name__}: {exc}"}
    if not generation_authority and state.get("epoch") is None and state.get("cursor") is None and not state.get("recovery_pending"):
        return None
    unfinished = bool(state.get("recovery_pending")) or state.get("bootstrap_intent") is not None or state.get("bootstrap_state") == "metadata_corrupt"
    return {
        "unfinished_transition": unfinished,
        "bootstrap_state": state.get("bootstrap_state"), "epoch": (state.get("epoch") or {}).get("epoch"),
        "epoch_origin": (state.get("epoch") or {}).get("origin"), "cursor_offset": (state.get("cursor") or {}).get("outbox_byte_offset"),
        "cursor_observed_size": (state.get("cursor") or {}).get("observed_size"), "cursor_generation_id": (state.get("cursor") or {}).get("generation_id"),
        "recovery_pending": bool(state.get("recovery_pending")), "malformed_receipts": len(state.get("malformed_receipts") or []),
        "malformed_receipts_readable": bool(state.get("malformed_receipts_readable", True)), "bootstrap_intent": state.get("bootstrap_intent") is not None,
        "error": state.get("error"),
    }


def projection_health(root: Path | str | None = None) -> ProjectionHealth:
    """Read-only projection health for the memory root (D-13-14/49/58/64/71/74/77)."""
    from core import timekeeper as TK

    proj = projection_root(root)
    state = manifest_state(proj)
    lane_reason: str | None = None
    generation = "legacy"
    lane: Literal["legacy", "available", "unavailable"] = "legacy"
    head_clock = None
    watermark = None
    page0: dict = {}
    if state.mode == "unavailable":
        generation, lane, lane_reason = "unavailable", "unavailable", state.reason
    elif state.mode == "generation":
        generation, lane = state.manifest.generation_id, "available"
        head_clock = state.manifest.coverage_clock
        watermark = state.manifest.coverage_clock.commit_seq
        page0 = dict(state.manifest.page0_utilization_by_lobe)
    # lineage state (class b / class c) — read-only
    try:
        scan = read_publication_lineage(proj / LINEAGE_FILE)
        if scan.torn_tail is not None and lane_reason is None:
            lane_reason = "publication_incomplete"
        if scan.head is not None and state.mode == "generation":
            head_clock = scan.head.coverage_clock
            watermark = scan.head.coverage_clock.commit_seq
            if scan.head.generation_id != state.manifest.generation_id or scan.head.coverage_clock != state.manifest.coverage_clock:
                lane, lane_reason = "unavailable", lane_reason or "manifest_head_disagreement"
    except PublicationLineageTampered:
        lane, lane_reason = "unavailable", "lineage_operator_required"
    # live clock: reserved (issued) vs landed; epoch/regression detection even when the number is larger
    live_clock = reserved_seq = landed_seq = None
    try:
        rs = TK.reservation_state()
        reserved_seq, landed_seq = rs["last_issued"], rs["landed_seq"]
        live_clock = CoverageClock(clock_epoch_id=rs["clock_epoch_id"], commit_seq=rs["landed_seq"])
        if head_clock is not None:
            if head_clock.clock_epoch_id != live_clock.clock_epoch_id or reserved_seq < head_clock.commit_seq:
                lane, lane_reason = "unavailable", lane_reason or "coverage_clock_regression"
    except CoverageClockUnavailable:
        lane_reason = lane_reason or "coverage_clock_unavailable"
        if state.mode == "generation":
            lane = "unavailable"
    except Exception as exc:  # noqa: BLE001 — timekeeper state unreadable is a named lane reason, never a crash
        lane_reason = lane_reason or f"timekeeper_unreadable: {exc}"
    try:
        seal = read_coverage_seal(proj)
    except GenerationIntegrityError:
        seal = None
        lane_reason = lane_reason or "coverage_seal_unavailable"
    # outbox evidence (Cut 4 reader; never consumption). Malformed / tampered lines and an
    # unreadable outbox are LOUD evidence (review 73646a37 §3): they never collapse into an
    # empty clean backlog and the projection is never "current" over them.
    pending = unsequenced = malformed_count = partial_bytes = 0
    oldest_age = None
    dirty = False
    outbox_error: str | None = None
    authority, _what = generation_authority_exists(proj) if proj.is_dir() else (False, None)
    program0 = _program0_state(root, authority)
    consumed_offset = _program0_consumed_offset(program0, resolve_memory_root(root))
    try:
        from memory.projection_delta import outbox_path, pending_projection_deltas, projection_dirty_state
        deltas, malformed = pending_projection_deltas(root=resolve_memory_root(root))
        unreadable = [m for m in malformed if str(m.get("error", "")).startswith("outbox unreadable")]
        if unreadable:
            outbox_error = str(unreadable[0].get("error"))
            lane_reason = lane_reason or "projection_outbox_unreadable"
        if consumed_offset is not None and not unreadable:
            # Program 0 has settled a cursor for THIS outbox: the honest backlog is every complete row
            # BEYOND the settled offset (consumed malformed rows live on as receipts in `program0`),
            # plus the unterminated trailing fragment as PENDING bytes (review a045ec4a §3)
            deltas, malformed_count, partial_bytes = _rows_beyond(outbox_path(resolve_memory_root(root)), consumed_offset)
        else:
            malformed_count = len(malformed) - len(unreadable)
            if not unreadable:
                path_ = outbox_path(resolve_memory_root(root))
                malformed_count += _blank_complete_rows(path_)
                try:
                    data_ = path_.read_bytes()
                    partial_bytes = len(data_) - (data_.rfind(b"\n") + 1)
                except OSError:
                    partial_bytes = 0
        if partial_bytes:
            lane_reason = lane_reason or "projection_outbox_partial_tail_pending"
        pending = len(deltas)
        if malformed_count:
            lane_reason = lane_reason or "projection_outbox_malformed"
        unsequenced = sum(1 for d in deltas if not getattr(d, "sequenced", False))
        oldest_age = _oldest_age_seconds([getattr(d, "queued_at", "") for d in deltas])
        dirty = bool(projection_dirty_state(root=resolve_memory_root(root)).get("dirty"))
    except Exception as exc:  # noqa: BLE001 — the reader itself failing is named, never swallowed
        outbox_error = f"{type(exc).__name__}: {exc}"
        lane_reason = lane_reason or "projection_outbox_unreadable"
    try:
        from memory import encoding_journal
        blocked = not bool(encoding_journal.journal_status().get("clean"))
    except Exception:  # noqa: BLE001
        blocked = True
    live_intents = tuple(n for n in (PUBLICATION_INTENT_FILE, REPAIR_INTENT_FILE) if (proj / n).exists())
    if program0 and program0.get("unfinished_transition"):
        # review a045ec4a §2: durable rotate-first recovery pending, a live bootstrap intent, or corrupt
        # Program-0 metadata is an UNFINISHED transition — the mandated full rebuild has not completed,
        # so the projection is never "current" (the lane itself may stay readable).
        reason = ("projection_outbox_recovery_pending" if program0.get("recovery_pending")
                  else ("projection_outbox_metadata_corrupt" if program0.get("bootstrap_state") == "metadata_corrupt"
                        else "projection_outbox_bootstrap_incomplete"))
        lane_reason = lane_reason or reason
    lag = (reserved_seq - watermark) if (reserved_seq is not None and watermark is not None) else None
    current = (lane == "available" and lane_reason is None and (lag or 0) == 0 and pending == 0 and malformed_count == 0
               and partial_bytes == 0 and outbox_error is None and not dirty and not blocked and not live_intents)
    return ProjectionHealth(
        generation=generation, semantic_dme_lane=lane, lane_reason=lane_reason, current=current,
        watermark_commit_seq=watermark, latest_canonical_commit_seq=reserved_seq, lag_commits=lag,
        head_clock=(head_clock.to_dict() if head_clock else None), live_clock=(live_clock.to_dict() if live_clock else None),
        reserved_seq=reserved_seq, landed_seq=landed_seq, coverage_seal=(seal.to_dict() if seal else None),
        pending_delta_count=pending, malformed_delta_count=malformed_count, pending_partial_bytes=partial_bytes, outbox_error=outbox_error,
        unsequenced_pending=unsequenced, unsequenced_posture=UNSEQUENCED_POSTURE,
        oldest_pending_age_s=oldest_age, dirty=dirty, blocked_on_obligations=blocked, page0_utilization_by_lobe=page0,
        live_intents=live_intents, dormant_generation_intent=bool(live_intents),
        program0=program0,
    )
