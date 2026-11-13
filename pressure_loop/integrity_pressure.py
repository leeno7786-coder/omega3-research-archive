"""Advisory pressure from periodic memory integrity audits."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pressure_loop.contributors import Contributor, make_contributor


def integrity_pressure_enabled() -> bool:
    raw = os.environ.get("OMEGA_MEMORY_INTEGRITY_PRESSURE", "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def integrity_pressure_contributor(root: Optional[Path | str] = None) -> Optional[Contributor]:
    if not integrity_pressure_enabled():
        return None
    try:
        from memory.integrity.audit import run_integrity_audit_if_due

        report = run_integrity_audit_if_due(root=root)
    except Exception:
        return None
    if report.status == "ok" or not report.issues:
        return None

    issue_count = len(report.issues)
    weight = min(0.55, 0.20 + 0.05 * issue_count)
    metadata = {
        "recommended_pressure": "memory_reconciliation",
        "issue_count": issue_count,
        "issues": list(report.issues),
        "orphan_graph_leaves": list(report.orphan_graph_leaves),
        "orphan_sense_rows": list(report.orphan_sense_rows),
        "missing_source_artifacts": report.missing_source_artifacts,
        "dme_parity_status": report.dme_parity_status,
        "audited_at": report.audited_at,
        "source": "memory_integrity_audit",
    }
    return make_contributor("memory_integrity", "memory_reconciliation", round(weight, 4), metadata)
