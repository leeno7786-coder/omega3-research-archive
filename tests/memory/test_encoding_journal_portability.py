from __future__ import annotations

import json
from pathlib import Path


def _configure_journal(tmp_path, monkeypatch):
    from memory import encoding_journal as journal

    profile = tmp_path / "Omega"
    monkeypatch.setattr(journal, "OMEGA_ROOT", profile)
    monkeypatch.setattr(
        journal,
        "JOURNAL_PATH",
        profile / "guardian" / "state" / "encoding_journal.jsonl",
    )
    return journal, profile


def test_encoding_journal_serializes_profile_paths_relative_to_the_ssd_root(
    tmp_path, monkeypatch
):
    journal, profile = _configure_journal(tmp_path, monkeypatch)
    canonical = profile / "memory" / "000_identity" / "0.00.0001.json"

    journal_id = journal.journal_open("0.00.0001", "staging", str(canonical))
    assert journal_id

    entry = json.loads(journal.JOURNAL_PATH.read_text(encoding="utf-8"))
    assert entry["final_path"] == "memory/000_identity/0.00.0001.json"
    assert not Path(entry["final_path"]).is_absolute()


def test_encoding_journal_snapshot_path_is_relative_and_discardable(
    tmp_path, monkeypatch
):
    journal, profile = _configure_journal(tmp_path, monkeypatch)
    canonical = profile / "memory" / "000_identity" / "0.00.0002.json"

    journal_id = journal.journal_open(
        "0.00.0002",
        "staging",
        str(canonical),
        prior_snapshot=b"prior record",
    )
    assert journal_id

    entry = json.loads(journal.JOURNAL_PATH.read_text(encoding="utf-8"))
    snapshot = entry["prior_snapshot"]
    assert snapshot == f"encoding_journal_prior/{journal_id}.bin"
    snapshot_path = journal.JOURNAL_PATH.parent / snapshot
    assert snapshot_path.read_bytes() == b"prior record"

    assert journal.journal_abort(journal_id, "test", {})
    assert not snapshot_path.exists()


def test_recovery_maps_a_pre_move_absolute_memory_path_to_the_current_profile(
    tmp_path, monkeypatch
):
    journal, profile = _configure_journal(tmp_path, monkeypatch)
    journal_id = "pre_move_0003"
    old_path = tmp_path / "old-drive" / "Omega3.0" / "memory" / "000_identity" / "0.00.0003.json"
    journal.JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    journal.JOURNAL_PATH.write_text(
        json.dumps(
            {
                "op": "open",
                "journal_id": journal_id,
                "memory_id": "0.00.0003",
                "staging_ref": "staging",
                "final_path": str(old_path),
                "prior_snapshot": "",
                "started_at": "2026-09-10T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rollback_paths: list[str] = []

    def rollback(_memory, final_path):
        rollback_paths.append(final_path)
        return {"rolled_back": True}

    results = journal.recover_unclosed(rollback)

    assert results[0]["memory_id"] == "0.00.0003"
    assert len(rollback_paths) == 1
    assert Path(rollback_paths[0]) == (
        profile / "memory" / "000_identity" / "0.00.0003.json"
    )


def test_recovery_reads_a_pre_move_absolute_snapshot_from_the_current_profile(
    tmp_path, monkeypatch
):
    journal, profile = _configure_journal(tmp_path, monkeypatch)
    current_snapshot = (
        profile / "guardian" / "state" / "encoding_journal_prior" / "legacy.bin"
    )
    current_snapshot.parent.mkdir(parents=True, exist_ok=True)
    current_snapshot.write_bytes(b"legacy prior record")
    old_snapshot = (
        tmp_path
        / "old-drive"
        / "Omega3.0"
        / "guardian"
        / "state"
        / "encoding_journal_prior"
        / "legacy.bin"
    )

    assert journal._read_snapshot(str(old_snapshot)) == b"legacy prior record"
