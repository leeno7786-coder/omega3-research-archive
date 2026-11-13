"""Legacy <profile>/state → guardian/state migration."""

from __future__ import annotations

from pathlib import Path

from memory.legacy_state import migrate_legacy_guardian_state


def test_migrate_copies_missing_strength_state(tmp_path: Path) -> None:
    legacy = tmp_path / "state"
    legacy.mkdir()
    (legacy / "strength_index.json").write_text('{"version":1,"records":{}}', encoding="utf-8")
    (legacy / "unrelated.txt").write_text("skip", encoding="utf-8")

    moved = migrate_legacy_guardian_state(tmp_path)
    assert set(moved) == {"strength_index.json"}
    modern = tmp_path / "guardian" / "state"
    assert (modern / "strength_index.json").is_file()
    assert not (modern / "unrelated.txt").exists()


def test_migrate_does_not_overwrite_modern(tmp_path: Path) -> None:
    legacy = tmp_path / "state"
    modern = tmp_path / "guardian" / "state"
    legacy.mkdir()
    modern.mkdir(parents=True)
    (legacy / "strength_index.json").write_text("legacy", encoding="utf-8")
    (modern / "strength_index.json").write_text("modern", encoding="utf-8")

    moved = migrate_legacy_guardian_state(tmp_path)
    assert moved == []
    assert (modern / "strength_index.json").read_text(encoding="utf-8") == "modern"
