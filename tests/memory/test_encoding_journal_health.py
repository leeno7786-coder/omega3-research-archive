from __future__ import annotations

import json

from memory import encoding_journal as journal


def test_journal_status_reports_malformed_lines_as_unclean(tmp_path, monkeypatch):
    path = tmp_path / "encoding_journal.jsonl"
    monkeypatch.setattr(journal, "JOURNAL_PATH", path)
    path.write_text(
        "{not-json}\n"
        + json.dumps({"op": "open", "journal_id": "closed-1", "memory_id": "m1"})
        + "\n"
        + json.dumps({"op": "close", "journal_id": "closed-1", "memory_id": "m1"})
        + "\n",
        encoding="utf-8",
    )

    status = journal.journal_status()

    assert status == {
        "clean": False,
        "unclosed": [],
        "unclosed_count": 0,
        "malformed_count": 1,
        "malformed_lines": [1],
    }


def test_malformed_journal_rows_are_quarantined_before_rewrite(tmp_path, monkeypatch):
    path = tmp_path / "encoding_journal.jsonl"
    path.write_text(
        '{"op":"close","journal_id":"good"}\n'
        'torn\\path fragment\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(journal, "JOURNAL_PATH", path)

    result = journal.quarantine_malformed_entries()

    assert result["malformed"] == 1
    quarantine = path.parent / result["quarantine"]
    assert quarantine.exists()
    assert "torn\\\\path fragment" in quarantine.read_text(encoding="utf-8")
    assert journal.journal_status()["clean"] is True
