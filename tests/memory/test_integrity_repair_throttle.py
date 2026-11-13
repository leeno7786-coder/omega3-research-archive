from __future__ import annotations

import json
from datetime import datetime, timezone

from memory.integrity.audit import IntegrityReport
from memory.integrity.repair import repair_integrity_issues


def test_due_aware_repair_reuses_fresh_integrity_report(tmp_path, monkeypatch):
    """A second episode inside the audit interval must not rescan the corpus."""
    cache_path = tmp_path / "memory" / "integrity" / "last_audit.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(
            {
                "type": "memory_integrity_report",
                "status": "ok",
                "issues": [],
                "audited_at": datetime.now(timezone.utc).isoformat(),
                "dme_parity_status": "ok",
            }
        ),
        encoding="utf-8",
    )

    def _unexpected_full_scan(*_args, **_kwargs):
        raise AssertionError("fresh integrity cache must prevent a full corpus scan")

    monkeypatch.setattr("memory.integrity.audit.run_integrity_audit", _unexpected_full_scan)

    report = repair_integrity_issues(root=tmp_path, if_due=True)

    assert report.skipped_due_interval is True
    assert report.before["status"] == "ok"
    assert report.after == report.before
    assert report.repaired_count == 0


def test_due_aware_repair_acts_on_issues_in_fresh_cached_report(tmp_path, monkeypatch):
    """A fresh audit cache suppresses rescanning, never a known deterministic repair."""
    cache_path = tmp_path / "memory" / "integrity" / "last_audit.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(
            {
                "type": "memory_integrity_report",
                "status": "degraded",
                "issues": ["orphan sense rows"],
                "orphan_sense_rows": ["sense-stale"],
                "audited_at": datetime.now(timezone.utc).isoformat(),
                "dme_parity_status": "ok",
            }
        ),
        encoding="utf-8",
    )

    repaired: list[tuple[str, ...]] = []
    scans: list[int] = []
    monkeypatch.setattr(
        "memory.integrity.repair._repair_missing_intake_packets",
        lambda *_args, **_kwargs: (set(), set()),
    )
    monkeypatch.setattr(
        "memory.integrity.repair._repair_orphan_sense_rows",
        lambda rows, **_kwargs: repaired.append(tuple(rows)) or set(rows),
    )
    monkeypatch.setattr(
        "memory.integrity.repair._repair_projection_claim_mismatches",
        lambda *_args, **_kwargs: (set(), set()),
    )
    monkeypatch.setattr(
        "memory.integrity.repair._repair_projection_drift",
        lambda *_args, **_kwargs: False,
    )

    def _post_repair_audit(*_args, **_kwargs):
        scans.append(1)
        return IntegrityReport(status="ok", issues=(), dme_parity_status="ok")

    monkeypatch.setattr("memory.integrity.audit.run_integrity_audit", _post_repair_audit)

    report = repair_integrity_issues(root=tmp_path, if_due=True)

    assert report.skipped_due_interval is False
    assert repaired == [("sense-stale",)]
    assert report.orphan_sense_rows_removed == ("sense-stale",)
    assert scans == [1]
