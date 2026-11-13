"""Cut 13 Task A2 — independent, content-addressed CoordinateRegistry authority
(D-13-35 / D-13-42 / D-13-46).

    <memory>/projection/registry_authority/
        registry-<sha256>.json    immutable snapshots (the exact registry bytes)
        lineage.jsonl             hash-chained RegistryAuthorityLineageRow (provenance only)
        manifest.json             atomically selected {registry_sha256, seq}

``persist_authority`` is idempotent: an unchanged selection writes nothing; a
snapshot that already exists but is not the selected head causes exactly one
transition; a new snapshot is written + one row + selected, each step synced.
The row's ``introduced_by_candidate_id`` is provenance ONLY — never a claim
that a generation was published (generation↔registry binding is recorded by
the PUBLICATION lineage once a generation is actually published).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memory.atomic_replace import atomic_replace_bytes
from memory.projection.generation import (
    GENESIS_ROW_SHA256,
    GenerationIntegrityError,
    PublicationLineageTampered,
    RegistryAuthorityLineageRow,
    generation_receipt_sha256,  # noqa: F401 (re-exported for callers that bind registry↔generation)
    sha256_bytes,
    sha256_file,
    sync_dir,
)
from memory.taxonomy.normalize import canonical_json

__all__ = [
    "AUTHORITY_DIR", "AUTHORITY_LINEAGE_FILE", "AUTHORITY_MANIFEST_FILE",
    "AuthorityResult", "AuthorityScan", "authority_dir", "snapshot_path", "select_authority",
    "read_authority_lineage", "persist_authority", "read_selected_registry_bytes", "retained_registry_hashes",
]

AUTHORITY_DIR = "registry_authority"
AUTHORITY_LINEAGE_FILE = "lineage.jsonl"
AUTHORITY_MANIFEST_FILE = "manifest.json"


def authority_dir(projection_root: Path) -> Path:
    return Path(projection_root) / AUTHORITY_DIR


def snapshot_path(projection_root: Path, registry_sha256: str) -> Path:
    return authority_dir(projection_root) / f"registry-{registry_sha256}.json"


@dataclass(frozen=True, kw_only=True)
class AuthorityResult:
    registry_sha256: str
    seq: int
    transition: bool          # True iff a lineage row was appended / the selection changed
    snapshot_written: bool


@dataclass(frozen=True, kw_only=True)
class AuthorityScan:
    rows: tuple[RegistryAuthorityLineageRow, ...]
    torn_tail: bytes | None
    head_sha256: str


def _row_sha256(row: RegistryAuthorityLineageRow) -> str:
    return sha256_bytes(canonical_json(row.to_dict()).encode("utf-8"))


def read_authority_lineage(projection_root: Path) -> AuthorityScan:
    path = authority_dir(projection_root) / AUTHORITY_LINEAGE_FILE
    if not path.exists():
        return AuthorityScan(rows=(), torn_tail=None, head_sha256=GENESIS_ROW_SHA256)
    raw = path.read_bytes()
    lines = raw.split(b"\n")
    torn = None
    if lines and lines[-1] != b"":
        torn = lines[-1]
    lines = lines[:-1]
    rows: list[RegistryAuthorityLineageRow] = []
    prev = GENESIS_ROW_SHA256
    for index, line in enumerate(lines, start=1):
        try:
            row = RegistryAuthorityLineageRow.from_dict(json.loads(line.decode("utf-8")))
        except (ValueError, UnicodeDecodeError, GenerationIntegrityError) as exc:
            raise PublicationLineageTampered(f"registry authority row {index} invalid: {exc}") from exc
        if row.seq != index or row.previous_row_sha256 != prev:
            raise PublicationLineageTampered(f"registry authority chain broken at row {index}")
        rows.append(row)
        prev = _row_sha256(row)
    return AuthorityScan(rows=tuple(rows), torn_tail=torn, head_sha256=prev)


def select_authority(projection_root: Path) -> tuple[str, int] | None:
    """The currently selected authority ``(registry_sha256, seq)`` — validated:
    the manifest names a snapshot whose bytes hash to that sha. Malformed ⇒
    GenerationIntegrityError (never 'empty')."""
    path = authority_dir(projection_root) / AUTHORITY_MANIFEST_FILE
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_bytes().decode("utf-8"))
        sha = str(doc["registry_sha256"])
        seq = int(doc["seq"])
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise GenerationIntegrityError(f"registry authority manifest malformed: {exc}") from exc
    snap = snapshot_path(projection_root, sha)
    if not snap.is_file():
        raise GenerationIntegrityError(f"registry authority manifest selects missing snapshot {sha}")
    if sha256_file(snap) != sha:
        raise GenerationIntegrityError(f"registry authority snapshot {sha} does not hash to its name")
    return sha, seq


def read_selected_registry_bytes(projection_root: Path) -> bytes | None:
    selected = select_authority(projection_root)
    if selected is None:
        return None
    return snapshot_path(projection_root, selected[0]).read_bytes()


def _settle_torn_tail(projection_root: Path, scan: AuthorityScan) -> None:
    """Frozen policy for a torn final authority row: preserve the tail as evidence,
    truncate to the last complete row, fsync + parent sync. The row payload is
    provenance only, so nothing is lost that the snapshot + manifest do not hold."""
    if scan.torn_tail is None:
        return
    adir = authority_dir(projection_root)
    path = adir / AUTHORITY_LINEAGE_FILE
    n = 1
    while (adir / f"lineage.torn-{n}").exists():
        n += 1
    (adir / f"lineage.torn-{n}").write_bytes(scan.torn_tail)
    raw = path.read_bytes()
    keep = raw[: len(raw) - len(scan.torn_tail)]
    with open(path, "r+b") as fh:
        fh.truncate(len(keep))
        fh.flush()
        os.fsync(fh.fileno())
    sync_dir(adir)


def _append_row(projection_root: Path, row: RegistryAuthorityLineageRow) -> None:
    adir = authority_dir(projection_root)
    adir.mkdir(parents=True, exist_ok=True)
    path = adir / AUTHORITY_LINEAGE_FILE
    with open(path, "ab") as fh:
        fh.write(canonical_json(row.to_dict()).encode("utf-8") + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
    sync_dir(adir)


def _select(projection_root: Path, registry_sha256: str, seq: int) -> None:
    adir = authority_dir(projection_root)
    atomic_replace_bytes(adir / AUTHORITY_MANIFEST_FILE,
                         canonical_json({"registry_sha256": registry_sha256, "seq": seq}).encode("utf-8"))
    sync_dir(adir)


def persist_authority(projection_root: Path, snapshot_bytes: bytes, *, authority: Any,
                      introduced_by_candidate_id: str | None, selected_at: str) -> AuthorityResult:
    """D-13-42/46 transaction under the publication token: snapshot (immutable, synced)
    → hash-chained provenance row (synced) → atomically selected manifest (synced).
    Idempotent: unchanged selection ⇒ no snapshot write, no row, no manifest replacement."""
    from memory.projection.generation import assert_authority  # local import: generation ↔ authority

    assert_authority(authority)
    projection_root = Path(projection_root)
    sha = sha256_bytes(snapshot_bytes)
    adir = authority_dir(projection_root)
    adir.mkdir(parents=True, exist_ok=True)
    selected = select_authority(projection_root)
    scan = read_authority_lineage(projection_root)
    if scan.torn_tail is not None:
        _settle_torn_tail(projection_root, scan)
        scan = read_authority_lineage(projection_root)
    if selected is not None and selected[0] == sha:
        # unchanged selection: idempotent no-op (unless the provenance row for the selection is missing — recover it)
        if scan.rows and scan.rows[-1].registry_sha256 == sha:
            return AuthorityResult(registry_sha256=sha, seq=selected[1], transition=False, snapshot_written=False)
    snap = snapshot_path(projection_root, sha)
    snapshot_written = False
    if not snap.is_file():
        atomic_replace_bytes(snap, snapshot_bytes)
        sync_dir(adir)
        snapshot_written = True
    elif sha256_file(snap) != sha:
        raise GenerationIntegrityError(f"registry snapshot {sha} on disk does not hash to its name")
    seq = (scan.rows[-1].seq + 1) if scan.rows else 1
    row = RegistryAuthorityLineageRow(
        seq=seq, registry_sha256=sha, previous_registry_sha256=(selected[0] if selected else None),
        previous_row_sha256=scan.head_sha256, introduced_by_candidate_id=introduced_by_candidate_id, selected_at=selected_at,
    )
    _append_row(projection_root, row)
    _select(projection_root, sha, seq)
    return AuthorityResult(registry_sha256=sha, seq=seq, transition=True, snapshot_written=snapshot_written)


def retained_registry_hashes(projection_root: Path, *, generation_bound: set[str], in_progress: set[str]) -> set[str]:
    """Retention set (D-13-42): selected head + newest valid lineage head + every
    generation-bound sha + in-progress shas. Everything else is GC-eligible."""
    keep: set[str] = set(generation_bound) | set(in_progress)
    selected = select_authority(projection_root)
    if selected:
        keep.add(selected[0])
    scan = read_authority_lineage(projection_root)
    if scan.rows:
        keep.add(scan.rows[-1].registry_sha256)
    return keep
