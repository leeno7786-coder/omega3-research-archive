from __future__ import annotations

import json

from tests import run_full_matrix


def test_json_report_creates_missing_parent_directories(tmp_path, monkeypatch) -> None:
    report = tmp_path / "nested" / "reports" / "matrix.json"
    monkeypatch.setattr(run_full_matrix, "discover_scripts", lambda **_: [])

    assert run_full_matrix.main(["--json", str(report)]) == 0
    assert json.loads(report.read_text(encoding="utf-8"))["files_failed"] == 0
