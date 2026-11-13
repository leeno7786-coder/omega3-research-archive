from __future__ import annotations


def test_workspace_retention_ignores_program0_working_snapshots(tmp_path):
    from memory.workspace_retention import _load_records

    staged = (
        tmp_path
        / "memory"
        / "projection"
        / "work-test"
        / "candidate"
        / "coord_index.snapshot.json"
    )
    staged.parent.mkdir(parents=True)
    staged.write_text("{", encoding="utf-8")

    assert _load_records(tmp_path / "memory") == []
