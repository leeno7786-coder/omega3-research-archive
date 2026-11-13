"""Autonomous repair pass for safe Memory Engine integrity drift.

DeepSleep uses this module for self-housekeeping. It repairs only deterministic,
rebuildable problems. Source-truth failures remain unresolved pressure because
they require missing evidence, not cleanup.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memory.taxonomy.normalize import canonical_json

log = logging.getLogger("omega.memory.integrity.repair")


@dataclass(frozen=True)
class IntegrityRepairReport:
    before: dict[str, Any]
    after: dict[str, Any]
    intake_packets_repaired: tuple[str, ...] = ()
    intake_packets_unrepaired: tuple[str, ...] = ()
    projection_receipts_repaired: tuple[str, ...] = ()
    projection_receipts_unrepaired: tuple[str, ...] = ()
    orphan_sense_rows_removed: tuple[str, ...] = ()
    orphan_projection_receipts_removed: tuple[str, ...] = ()
    projections_rebuilt: bool = False
    skipped_due_interval: bool = False
    errors: tuple[str, ...] = field(default_factory=tuple)
    type: str = "memory_integrity_repair_report"

    @property
    def repaired_count(self) -> int:
        return (
            len(self.intake_packets_repaired)
            + len(self.projection_receipts_repaired)
            + len(self.orphan_sense_rows_removed)
            + len(self.orphan_projection_receipts_removed)
            + (1 if self.projections_rebuilt else 0)
        )

    @property
    def after_status(self) -> str:
        return str((self.after or {}).get("status") or "unknown")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "before": dict(self.before or {}),
            "after": dict(self.after or {}),
            "intake_packets_repaired": list(self.intake_packets_repaired),
            "intake_packets_unrepaired": list(self.intake_packets_unrepaired),
            "projection_receipts_repaired": list(self.projection_receipts_repaired),
            "projection_receipts_unrepaired": list(self.projection_receipts_unrepaired),
            "orphan_sense_rows_removed": list(self.orphan_sense_rows_removed),
            "orphan_projection_receipts_removed": list(
                self.orphan_projection_receipts_removed
            ),
            "projections_rebuilt": self.projections_rebuilt,
            "skipped_due_interval": self.skipped_due_interval,
            "repaired_count": self.repaired_count,
            "errors": list(self.errors),
            "after_status": self.after_status,
        }


def repair_integrity_issues(
    *, root: Path | str | None = None, if_due: bool = False
) -> IntegrityRepairReport:
    """Repair deterministic integrity drift and return before/after reports.

    ``if_due`` lets per-episode DeepSleep reuse the pressure loop's recent
    integrity report rather than scanning the entire corpus twice per turn.
    """
    from memory.integrity import audit as audit_mod

    before_report = None
    if if_due and not audit_mod.integrity_audit_due(root=root):
        cached = audit_mod.run_integrity_audit_if_due(root=root)
        if not tuple(getattr(cached, "issues", ()) or ()):
            payload = cached.to_dict()
            return IntegrityRepairReport(
                before=payload,
                after=payload,
                skipped_due_interval=True,
            )
        # A fresh cache avoids the redundant pre-repair scan, but known issues
        # remain actionable during this DeepSleep window.
        before_report = cached

    if before_report is None:
        before_report = audit_mod.run_integrity_audit(root=root)
    errors: list[str] = []

    intake_repaired, intake_unrepaired = _repair_missing_intake_packets(
        tuple(getattr(before_report, "missing_intake_packets", ()) or ()),
        root=root,
        errors=errors,
    )
    senses_removed = _repair_orphan_sense_rows(
        tuple(getattr(before_report, "orphan_sense_rows", ()) or ()),
        root=root,
        errors=errors,
    )
    receipts_repaired, receipts_unrepaired = _repair_projection_claim_mismatches(
        before_report,
        root=root,
        errors=errors,
    )
    receipts_removed = _repair_orphan_projection_receipts(
        tuple(getattr(before_report, "orphan_projection_receipts", ()) or ()),
        root=root,
        errors=errors,
    )
    _remove_shadow_compiled_dir(root=root, errors=errors)
    projections_rebuilt = _repair_projection_drift(before_report, root=root, errors=errors)

    after_report = audit_mod.run_integrity_audit(root=root)
    return IntegrityRepairReport(
        before=before_report.to_dict(),
        after=after_report.to_dict(),
        intake_packets_repaired=tuple(sorted(intake_repaired)),
        intake_packets_unrepaired=tuple(sorted(intake_unrepaired)),
        projection_receipts_repaired=tuple(sorted(receipts_repaired)),
        projection_receipts_unrepaired=tuple(sorted(receipts_unrepaired)),
        orphan_sense_rows_removed=tuple(sorted(senses_removed)),
        orphan_projection_receipts_removed=tuple(sorted(receipts_removed)),
        projections_rebuilt=projections_rebuilt,
        errors=tuple(errors),
    )


def _repair_missing_intake_packets(
    memory_ids: tuple[str, ...],
    *,
    root: Path | str | None,
    errors: list[str],
) -> tuple[set[str], set[str]]:
    repaired: set[str] = set()
    unrepaired: set[str] = set()
    if not memory_ids:
        return repaired, unrepaired
    try:
        from memory.engine.validate_write import atomic_write
        from memory.engine.walker import coord_index_snapshot, load_meta_by_path
        from memory.intake import build_intake_packet
        from memory.projection_receipt import (
            rewrite_projection_receipt_for_repaired_claim,
        )
    except Exception as exc:
        errors.append(f"intake repair unavailable: {type(exc).__name__}: {exc}")
        return repaired, set(memory_ids)

    snapshot = coord_index_snapshot()
    for memory_id in memory_ids:
        entry = snapshot.get(memory_id)
        if not isinstance(entry, dict):
            unrepaired.add(memory_id)
            continue
        path = _resolve_path(entry.get("path"), root=root)
        meta = load_meta_by_path(path) if path else None
        if not isinstance(meta, dict):
            unrepaired.add(memory_id)
            continue
        provenance = meta.get("provenance") if isinstance(meta.get("provenance"), dict) else {}
        required = ("source_id", "span_hash", "source_content_hash")
        if any(not str(provenance.get(key) or "").strip() for key in required):
            unrepaired.add(memory_id)
            continue
        enrichment = dict(meta.get("enrichment") or {})
        enrichment["intake_packet"] = build_intake_packet(
            memory_id=str(meta.get("id") or memory_id),
            text=str(meta.get("text") or meta.get("summary") or meta.get("title") or ""),
            coord=list(meta.get("coord") or entry.get("coord") or []),
            source_tag=str(provenance.get("author") or meta.get("source") or "memory_repair"),
            metadata=_metadata_from_record(meta, provenance),
            enrichment=enrichment,
        )
        meta["enrichment"] = enrichment
        try:
            atomic_write(meta, update_index=False)
            if provenance.get("projection_receipt_required"):
                rewrite_projection_receipt_for_repaired_claim(meta, root=root)
            repaired.add(memory_id)
        except Exception as exc:
            unrepaired.add(memory_id)
            errors.append(f"intake repair failed for {memory_id}: {type(exc).__name__}: {exc}")
    return repaired, unrepaired


def _repair_orphan_sense_rows(
    sense_ids: tuple[str, ...],
    *,
    root: Path | str | None,
    errors: list[str],
) -> set[str]:
    if not sense_ids:
        return set()
    try:
        from memory.sense_registry import sense_registry_path

        path = sense_registry_path(root=root)
        if not path.exists():
            return set()
        remove = set(str(item) for item in sense_ids)
        kept: list[dict[str, Any] | str] = []
        removed: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            sense_id = str(row.get("sense_id") or row.get("memory_id") or "")
            if sense_id in remove:
                removed.add(sense_id)
                continue
            kept.append(row)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            for row in kept:
                handle.write((row if isinstance(row, str) else canonical_json(row)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        return removed
    except Exception as exc:
        errors.append(f"sense row repair failed: {type(exc).__name__}: {exc}")
        return set()


def _repair_projection_claim_mismatches(
    report: Any,
    *,
    root: Path | str | None,
    errors: list[str],
) -> tuple[set[str], set[str]]:
    repaired: set[str] = set()
    unrepaired: set[str] = set()
    if int(getattr(report, "projection_claim_hash_mismatches", 0) or 0) <= 0:
        return repaired, unrepaired
    try:
        from memory.engine.walker import coord_index_snapshot, load_meta_by_path
        from memory.projection_receipt import (
            rewrite_projection_receipt_for_repaired_claim,
            verify_projection_receipt,
        )
    except Exception as exc:
        errors.append(f"projection receipt repair unavailable: {type(exc).__name__}: {exc}")
        return repaired, unrepaired

    base = _memory_base(root)
    for memory_id, entry in coord_index_snapshot().items():
        path = _resolve_path(entry.get("path") if isinstance(entry, dict) else None, root=base)
        meta = load_meta_by_path(path) if path else None
        if not isinstance(meta, dict):
            continue
        provenance = meta.get("provenance")
        if not isinstance(provenance, dict) or not provenance.get("projection_receipt_required"):
            continue
        verdict = verify_projection_receipt(meta, root=base)
        if verdict.ok:
            continue
        if verdict.reason != "canonical_claim_hash_mismatch":
            continue
        try:
            rewrite_projection_receipt_for_repaired_claim(meta, root=base)
            repaired.add(str(meta.get("id") or memory_id))
        except Exception as exc:
            mid = str(meta.get("id") or memory_id)
            unrepaired.add(mid)
            errors.append(
                f"projection receipt repair failed for {mid}: {type(exc).__name__}: {exc}"
            )
    return repaired, unrepaired
def _repair_orphan_projection_receipts(
    memory_ids: tuple[str, ...],
    *,
    root: Path | str | None,
    errors: list[str],
) -> set[str]:
    """Delete receipts whose memory_id is no longer in the coord index."""
    if not memory_ids:
        return set()
    try:
        from memory.projection_receipt import receipt_path
    except Exception as exc:
        errors.append(f"orphan receipt repair unavailable: {type(exc).__name__}: {exc}")
        return set()

    removed: set[str] = set()
    audit_root = Path(root) if root is not None else None
    for memory_id in memory_ids:
        try:
            path = receipt_path(memory_id, root=audit_root)
            if path.exists():
                path.unlink()
                removed.add(memory_id)
        except Exception as exc:
            errors.append(
                f"orphan receipt cleanup failed for {memory_id}: "
                f"{type(exc).__name__}: {exc}"
            )
    return removed


def _remove_shadow_compiled_dir(*, root: Path | str | None, errors: list[str]) -> None:
    """Remove stale ``<profile>/compiled`` created by the old taxonomy_root bug."""
    try:
        from memory.integrity.audit import _audit_root

        profile_root = _audit_root(root)
        shadow = Path(profile_root) / "compiled"
        real = Path(profile_root) / "memory" / "taxonomy_store" / "compiled"
        if not shadow.is_dir() or shadow.resolve() == real.resolve():
            return
        # Only delete the known shadow artifact names — never a random compiled/.
        for name in ("graph.sqlite", "graph_snapshot.json", "latest_snapshot_hash.txt"):
            path = shadow / name
            if path.exists():
                path.unlink()
        try:
            if not sorted(shadow.iterdir()):
                shadow.rmdir()
        except OSError:
            pass
    except Exception as exc:
        errors.append(f"shadow compiled cleanup failed: {type(exc).__name__}: {exc}")


def _repair_projection_drift(report: Any, *, root: Path | str | None, errors: list[str]) -> bool:
    needs_rebuild = bool(
        getattr(report, "orphan_graph_leaves", ())
        or getattr(report, "missing_canonical_files", ())
        or getattr(report, "orphan_projection_receipts", ())
        or str(getattr(report, "dme_parity_status", "")) != "ok"
    )
    if not needs_rebuild:
        return False
    try:
        from memory.engine.reindex import reindex_and_compile

        reindex_and_compile(root=root)
        return True
    except Exception as exc:
        errors.append(f"projection rebuild failed: {type(exc).__name__}: {exc}")
        return False


def _metadata_from_record(meta: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": meta.get("id"),
        "type": meta.get("type"),
        "title": meta.get("title"),
        "summary": meta.get("summary"),
        "domain": meta.get("domain"),
        "subtopic": meta.get("subtopic"),
        "tags": list(meta.get("tags") or []),
        "links": list(meta.get("links") or []),
        "origin_id": provenance.get("origin_id"),
        "origin_type": provenance.get("origin_type"),
        "confidence": provenance.get("confidence"),
        "source_id": provenance.get("source_id"),
        "source_type": provenance.get("source_type"),
        "span_start": provenance.get("span_start"),
        "span_end": provenance.get("span_end"),
        "span_hash": provenance.get("span_hash"),
        "source_content_hash": provenance.get("source_content_hash"),
    }
    return {key: value for key, value in out.items() if value is not None}


def _resolve_path(raw_path: Any, *, root: Path | str | None) -> Path | None:
    if not raw_path:
        return None
    path = Path(str(raw_path))
    if path.is_absolute():
        return path
    return (Path(root) / path) if root is not None else path


def _memory_base(root: Path | str | None) -> Path | None:
    if root is None:
        return None
    base = Path(root)
    return base.parent if base.name.lower() == "memory" else base
