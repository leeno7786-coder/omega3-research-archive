"""Memory Engine v2 integrity audits."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory.engine.walker import coord_index_snapshot, load_coord_index, load_meta_by_path
from memory.sense_registry import sense_registry_path
from memory.taxonomy import graph_store as G
from memory.taxonomy.hashing import span_hash_for


@dataclass(frozen=True)
class IntegrityReport:
    status: str
    orphan_graph_leaves: tuple[str, ...] = ()
    missing_canonical_files: tuple[str, ...] = ()
    missing_source_artifacts: int = 0
    source_hash_mismatches: int = 0
    missing_projection_receipts: int = 0
    malformed_projection_receipts: int = 0
    projection_source_mismatches: int = 0
    projection_claim_hash_mismatches: int = 0
    projection_embedding_mismatches: int = 0
    orphan_projection_receipts: tuple[str, ...] = ()
    missing_intake_packets: tuple[str, ...] = ()
    lobe_route_mismatches: tuple[str, ...] = ()
    orphan_sense_rows: tuple[str, ...] = ()
    dme_parity_status: str = "unknown"
    dme_missing_in_sram: tuple[str, ...] = ()
    dme_orphan_in_sram: tuple[str, ...] = ()
    issues: tuple[str, ...] = field(default_factory=tuple)
    audited_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "memory_integrity_report",
            "status": self.status,
            "orphan_graph_leaves": list(self.orphan_graph_leaves),
            "missing_canonical_files": list(self.missing_canonical_files),
            "missing_source_artifacts": self.missing_source_artifacts,
            "source_hash_mismatches": self.source_hash_mismatches,
            "missing_projection_receipts": self.missing_projection_receipts,
            "malformed_projection_receipts": self.malformed_projection_receipts,
            "projection_source_mismatches": self.projection_source_mismatches,
            "projection_claim_hash_mismatches": self.projection_claim_hash_mismatches,
            "projection_embedding_mismatches": self.projection_embedding_mismatches,
            "orphan_projection_receipts": list(self.orphan_projection_receipts),
            "missing_intake_packets": list(self.missing_intake_packets),
            "lobe_route_mismatches": list(self.lobe_route_mismatches),
            "orphan_sense_rows": list(self.orphan_sense_rows),
            "dme_parity_status": self.dme_parity_status,
            "dme_missing_in_sram": list(self.dme_missing_in_sram),
            "dme_orphan_in_sram": list(self.dme_orphan_in_sram),
            "issues": list(self.issues),
            "audited_at": self.audited_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "IntegrityReport":
        return cls(
            status=str(payload.get("status") or "unknown"),
            orphan_graph_leaves=tuple(payload.get("orphan_graph_leaves") or ()),
            missing_canonical_files=tuple(payload.get("missing_canonical_files") or ()),
            missing_source_artifacts=int(payload.get("missing_source_artifacts") or 0),
            source_hash_mismatches=int(payload.get("source_hash_mismatches") or 0),
            missing_projection_receipts=int(payload.get("missing_projection_receipts") or 0),
            malformed_projection_receipts=int(payload.get("malformed_projection_receipts") or 0),
            projection_source_mismatches=int(payload.get("projection_source_mismatches") or 0),
            projection_claim_hash_mismatches=int(
                payload.get("projection_claim_hash_mismatches") or 0
            ),
            projection_embedding_mismatches=int(
                payload.get("projection_embedding_mismatches") or 0
            ),
            orphan_projection_receipts=tuple(payload.get("orphan_projection_receipts") or ()),
            missing_intake_packets=tuple(payload.get("missing_intake_packets") or ()),
            lobe_route_mismatches=tuple(payload.get("lobe_route_mismatches") or ()),
            orphan_sense_rows=tuple(payload.get("orphan_sense_rows") or ()),
            dme_parity_status=str(payload.get("dme_parity_status") or "unknown"),
            dme_missing_in_sram=tuple(payload.get("dme_missing_in_sram") or ()),
            dme_orphan_in_sram=tuple(payload.get("dme_orphan_in_sram") or ()),
            issues=tuple(payload.get("issues") or ()),
            audited_at=str(payload.get("audited_at") or ""),
        )


def run_integrity_audit(*, root: Path | str | None = None) -> IntegrityReport:
    """Run orphan/source/DME parity audits over the live memory tree."""
    projection_root = _projection_root(root)
    root = _audit_root(root)
    try:
        load_coord_index()
    except Exception:
        pass

    snapshot = coord_index_snapshot()
    orphan_graph, missing_files = _graph_orphan_audit(
        snapshot,
        root=root,
        projection_root=projection_root,
    )
    missing_sources, hash_mismatches = _source_ledger_audit(snapshot, root=root)
    receipt_audit = _projection_receipt_audit(snapshot, root=root)
    missing_intake = _intake_packet_audit(snapshot, root=root)
    route_mismatches = _lobe_route_audit(snapshot, root=root)
    orphan_senses = _sense_registry_audit(snapshot, root=root)
    dme_status, dme_missing, dme_orphan = _dme_parity_audit(root=projection_root)
    audited_at = _now_iso()

    issues: list[str] = []
    if orphan_graph:
        issues.append(f"integrity: {len(orphan_graph)} orphan graph leaf/leaves")
    if missing_files:
        issues.append(
            f"integrity: {len(missing_files)} coord_index entries missing canonical files"
        )
    if orphan_senses:
        issues.append(f"integrity: {len(orphan_senses)} orphan sense registry row(s)")
    if missing_sources:
        issues.append(f"integrity: {missing_sources} missing source ledger artifact(s)")
    if hash_mismatches:
        issues.append(f"integrity: {hash_mismatches} source hash mismatch(es)")
    if receipt_audit["missing"]:
        issues.append(f"integrity: {receipt_audit['missing']} missing projection receipt(s)")
    if receipt_audit["malformed"]:
        issues.append(f"integrity: {receipt_audit['malformed']} malformed projection receipt(s)")
    if receipt_audit["source_mismatch"]:
        issues.append(
            f"integrity: {receipt_audit['source_mismatch']} projection receipt source mismatch(es)"
        )
    if receipt_audit["claim_mismatch"]:
        issues.append(
            "integrity: "
            f"{receipt_audit['claim_mismatch']} projection receipt canonical claim "
            "hash mismatch(es)"
        )
    if receipt_audit["embedding_mismatch"]:
        issues.append(
            "integrity: "
            f"{receipt_audit['embedding_mismatch']} projection receipt embedding "
            "mismatch(es)"
        )
    if receipt_audit["orphans"]:
        issues.append(f"integrity: {len(receipt_audit['orphans'])} orphan projection receipt(s)")
    if missing_intake:
        issues.append(f"integrity: {len(missing_intake)} canonical record(s) missing intake packet")
    if route_mismatches:
        issues.append(
            f"integrity: {len(route_mismatches)} knowledge/coding lobe route mismatch(es)"
        )
    if dme_status != "ok":
        issues.append(
            f"integrity: DME parity drift missing={len(dme_missing)} orphan={len(dme_orphan)}"
        )

    status = "ok" if not issues else "issues"
    report = IntegrityReport(
        status=status,
        orphan_graph_leaves=tuple(sorted(orphan_graph)),
        missing_canonical_files=tuple(sorted(missing_files)),
        missing_source_artifacts=missing_sources,
        source_hash_mismatches=hash_mismatches,
        missing_projection_receipts=int(receipt_audit["missing"]),
        malformed_projection_receipts=int(receipt_audit["malformed"]),
        projection_source_mismatches=int(receipt_audit["source_mismatch"]),
        projection_claim_hash_mismatches=int(receipt_audit["claim_mismatch"]),
        projection_embedding_mismatches=int(receipt_audit["embedding_mismatch"]),
        orphan_projection_receipts=tuple(sorted(receipt_audit["orphans"])),
        missing_intake_packets=tuple(sorted(missing_intake)),
        lobe_route_mismatches=tuple(sorted(route_mismatches)),
        orphan_sense_rows=tuple(sorted(orphan_senses)),
        dme_parity_status=dme_status,
        dme_missing_in_sram=dme_missing,
        dme_orphan_in_sram=dme_orphan,
        issues=tuple(issues),
        audited_at=audited_at,
    )
    _write_cached_report(report, root=root)
    return report


def run_integrity_audit_if_due(
    *,
    root: Path | str | None = None,
    force: bool = False,
    min_interval_seconds: int | None = None,
) -> IntegrityReport:
    """Run a full audit when the cache interval has elapsed."""
    root = _audit_root(root)
    interval = _audit_interval_seconds(min_interval_seconds)
    if not force:
        cached = _read_cached_report(root=root)
        if cached is not None and _cache_is_fresh(cached, interval):
            return cached
    return run_integrity_audit(root=root)


def integrity_audit_due(
    *,
    root: Path | str | None = None,
    force: bool = False,
    min_interval_seconds: int | None = None,
) -> bool:
    """Return whether a full corpus audit is due without running one."""
    if force:
        return True
    root = _audit_root(root)
    cached = _read_cached_report(root=root)
    if cached is None:
        return True
    return not _cache_is_fresh(cached, _audit_interval_seconds(min_interval_seconds))


def _graph_orphan_audit(
    snapshot: dict[str, Any],
    *,
    root: Path | str | None,
    projection_root: Path | str | None,
) -> tuple[set[str], set[str]]:
    index_ids = set(snapshot.keys())
    missing_files: set[str] = set()
    for mid, entry in snapshot.items():
        path_str = entry.get("path")
        if not path_str:
            missing_files.add(mid)
            continue
        path = Path(path_str)
        if not path.is_absolute():
            from core.paths import OMEGA_ROOT

            base = Path(root) if root is not None else OMEGA_ROOT
            path = base / path
        if not path.exists() or not load_meta_by_path(path):
            missing_files.add(mid)

    orphan_graph: set[str] = set()
    try:
        conn = G.connect_for_read(projection_root)
    except Exception:
        return orphan_graph, missing_files
    try:
        rows = conn.execute(
            "SELECT node_id, payload_json FROM nodes WHERE node_type = ?",
            ("memory_record",),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        node_id = str(row["node_id"] or "")
        if not node_id:
            continue
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            payload = {}
        memory_id = str(payload.get("memory_id") or node_id)
        if memory_id not in index_ids or memory_id in missing_files:
            orphan_graph.add(memory_id)
    return orphan_graph, missing_files


def _source_ledger_audit(
    snapshot: dict[str, Any],
    *,
    root: Path | str | None,
) -> tuple[int, int]:
    try:
        from memory.source_ledger.store import read_source
    except Exception:
        return 0, 0

    missing = 0
    mismatches = 0
    for _mid, entry in snapshot.items():
        path_str = entry.get("path")
        if not path_str:
            continue
        path = Path(path_str)
        if not path.is_absolute():
            from core.paths import OMEGA_ROOT

            base = Path(root) if root is not None else OMEGA_ROOT
            path = base / path
        meta = load_meta_by_path(path)
        if not meta:
            continue
        provenance = meta.get("provenance") if isinstance(meta.get("provenance"), dict) else {}
        source_id = str(provenance.get("source_id") or "")
        if not source_id:
            continue
        artifact = read_source(source_id, root=root)
        if artifact is None:
            missing += 1
            continue
        cited_content = str(provenance.get("source_content_hash") or "")
        if cited_content and cited_content != artifact.content_hash:
            mismatches += 1
            continue
        cited_span = str(provenance.get("span_hash") or "")
        if cited_span and cited_span != artifact.span_hash:
            mismatches += 1
            continue
        if (
            cited_span
            and span_hash_for(artifact.content[artifact.span_start : artifact.span_end])
            != artifact.span_hash
        ):
            mismatches += 1
    return missing, mismatches


def _projection_receipt_audit(
    snapshot: dict[str, Any],
    *,
    root: Path | str | None,
) -> dict[str, Any]:
    """Check only receipt-required records; pre-receipt history stays compatible."""
    from memory.projection_receipt import receipt_path, verify_projection_receipt

    counts: dict[str, Any] = {
        "missing": 0,
        "malformed": 0,
        "source_mismatch": 0,
        "claim_mismatch": 0,
        "embedding_mismatch": 0,
        "orphans": [],
    }
    base = _audit_root(root)
    for _mid, entry in snapshot.items():
        path_str = entry.get("path") if isinstance(entry, dict) else None
        if not path_str:
            continue
        path = Path(path_str)
        if not path.is_absolute():
            path = base / path
        meta = load_meta_by_path(path)
        if not isinstance(meta, dict):
            continue
        provenance = meta.get("provenance")
        if not isinstance(provenance, dict) or not provenance.get("projection_receipt_required"):
            continue
        result = verify_projection_receipt(meta, root=base)
        if result.ok:
            continue
        key = {
            "missing_projection_receipt": "missing",
            "malformed_projection_receipt": "malformed",
            "source_mismatch": "source_mismatch",
            "canonical_claim_hash_mismatch": "claim_mismatch",
            "embedding_projection_mismatch": "embedding_mismatch",
        }.get(result.reason, "malformed")
        counts[key] += 1

    receipts_dir = receipt_path("_", root=base).parent
    if receipts_dir.is_dir():
        known = {str(memory_id) for memory_id in snapshot}
        for path in receipts_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                memory_id = str(payload.get("memory_id") or "")
            except Exception:
                memory_id = ""
            if memory_id and memory_id not in known:
                counts["orphans"].append(memory_id)
    return counts


def _intake_packet_audit(
    snapshot: dict[str, Any],
    *,
    root: Path | str | None,
) -> set[str]:
    missing: set[str] = set()
    for mid, entry in snapshot.items():
        path_str = entry.get("path")
        if not path_str:
            continue
        path = Path(path_str)
        if not path.is_absolute():
            from core.paths import OMEGA_ROOT

            base = Path(root) if root is not None else OMEGA_ROOT
            path = base / path
        meta = load_meta_by_path(path)
        if not isinstance(meta, dict):
            continue
        enrichment = meta.get("enrichment") if isinstance(meta.get("enrichment"), dict) else {}
        packet = enrichment.get("intake_packet") if isinstance(enrichment, dict) else None
        if not isinstance(packet, dict):
            missing.add(str(mid))
            continue
        if packet.get("type") != "memory_intake_packet" or packet.get("memory_id") != mid:
            missing.add(str(mid))
            continue
    return missing


def _lobe_route_audit(
    snapshot: dict[str, Any],
    *,
    root: Path | str | None,
) -> set[str]:
    try:
        from memory.routing.coding_vs_knowledge import classify_memory_record_lobe
    except Exception:
        return set()

    mismatches: set[str] = set()
    for mid, entry in snapshot.items():
        coord = list(entry.get("coord") or []) if isinstance(entry, dict) else []
        if not coord or coord[0] not in {2, 8}:
            continue
        path_str = entry.get("path")
        if not path_str:
            continue
        path = Path(path_str)
        if not path.is_absolute():
            from core.paths import OMEGA_ROOT

            base = Path(root) if root is not None else OMEGA_ROOT
            path = base / path
        meta = load_meta_by_path(path)
        if not isinstance(meta, dict):
            continue
        expected_lobe = classify_memory_record_lobe(meta)
        if expected_lobe in {2, 8} and int(coord[0]) != int(expected_lobe):
            mismatches.add(str(mid))
    return mismatches


def _sense_registry_audit(
    snapshot: dict[str, Any],
    *,
    root: Path | str | None,
) -> set[str]:
    path = sense_registry_path(root=root)
    if not path.exists():
        return set()
    index_ids = set(snapshot.keys())
    orphan: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        memory_id = str(row.get("memory_id") or "")
        sense_id = str(row.get("sense_id") or "")
        if memory_id and memory_id not in index_ids:
            orphan.add(sense_id or memory_id)
    return orphan


def _dme_parity_audit(*, root: Path | str | None) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    from memory.dme.adapter import parity_report
    from memory.projection.generation import projection_read_transaction

    with projection_read_transaction(_audit_root(root)):
        report = parity_report(root=root)
    return report.status, report.missing_in_dme, report.orphan_in_dme


def _audit_root(root: Path | str | None) -> Path:
    if root is not None:
        base = Path(root)
        return base if base.name != "memory" else base.parent
    try:
        from memory.engine import validate_write

        mem = Path(validate_write.MEMORY_ROOT)
        return mem.parent if mem.name == "memory" else mem
    except Exception:
        from core.paths import OMEGA_ROOT

        return OMEGA_ROOT


def _projection_root(root: Path | str | None) -> Path | None:
    """Resolve the graph/DME root without conflating it with the memory-tree root."""
    if root is None:
        return None
    base = Path(root)
    if base.name == "memory":
        base = base.parent
    try:
        from core.paths import OMEGA_ROOT

        if base.resolve() == Path(OMEGA_ROOT).resolve():
            return None
    except OSError:
        pass
    return base


def _audit_interval_seconds(min_interval_seconds: int | None) -> int:
    if min_interval_seconds is not None:
        return max(0, int(min_interval_seconds))
    import os

    raw = os.environ.get("OMEGA_MEMORY_INTEGRITY_AUDIT_INTERVAL", "300").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 300


def _cache_path(*, root: Path | str | None) -> Path:
    base = _audit_root(root)
    return (
        (base / "memory" / "integrity" / "last_audit.json")
        if base.name != "memory"
        else base / "integrity" / "last_audit.json"
    )


def _read_cached_report(*, root: Path | str | None) -> IntegrityReport | None:
    path = _cache_path(root=root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return IntegrityReport.from_dict(payload)


def _write_cached_report(report: IntegrityReport, *, root: Path | str | None) -> None:
    path = _cache_path(root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )


def _cache_is_fresh(report: IntegrityReport, interval_seconds: int) -> bool:
    if interval_seconds <= 0:
        return False
    if not report.audited_at:
        return False
    try:
        audited = datetime.fromisoformat(report.audited_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = (datetime.now(timezone.utc) - audited.astimezone(timezone.utc)).total_seconds()
    return age < interval_seconds


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
