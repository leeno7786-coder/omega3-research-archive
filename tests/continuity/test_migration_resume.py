"""WO-08 Steps 4–6 — offline stage / verify / resume: restartable, conflict-safe, loss-accounted.

The CLI never has a default source and refuses the live repository as a destination. Converted
records land in the destination's canonical layout (so a derived index can be rebuilt from them);
the journal, manifest and quarantine live in ``migration_control/`` beside ``memory/`` — outside
every recallable path. Each source is journalled with its source hash and its output hash, so a
replay is a no-op, an interrupted run resumes to exactly one conversion per source version, and a
source or destination that changed underneath the journal is a ``MigrationConflict`` that
overwrites nothing.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "historical"
CONVERTIBLE = ("enriched_record", "distilled_prior_episode", "model_reflection",
               "deterministic_reflection", "untimed_record")
MALFORMED = ("malformed_text", "malformed_file")
EPISODES = Path("memory") / "900_temporal" / "events" / "episode"


def _migrate():
    import importlib

    try:
        return importlib.import_module("scripts.migrate_episode_continuity")
    except ModuleNotFoundError:
        pytest.fail("scripts/migrate_episode_continuity.py does not exist")


def _source_tree(tmp_path: Path, names=CONVERTIBLE + MALFORMED) -> Path:
    root = tmp_path / "source"
    target = root / EPISODES
    target.mkdir(parents=True)
    for name in names:
        shutil.copyfile(FIXTURES / f"{name}.json", target / f"{name}.json")
    return root


def _run(*args: str) -> int:
    return _migrate().main([str(arg) for arg in args])


def _outputs(destination: Path) -> dict[str, bytes]:
    folder = destination / EPISODES
    return {p.name: p.read_bytes() for p in sorted(folder.glob("*.json"))} if folder.is_dir() else {}


def _manifest(destination: Path) -> dict:
    return json.loads((destination / "migration_control" / "manifest.json").read_text(encoding="utf-8"))


def _journal_rows(destination: Path) -> list[dict]:
    path = destination / "migration_control" / "journal.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── dry run and stage ───────────────────────────────────────────────────────


def test_a_dry_run_writes_nothing_and_reports_exact_totals(tmp_path, capsys):
    source = _source_tree(tmp_path)
    destination = tmp_path / "destination"
    code = _run("--source", source, "--destination", destination, "--dry-run")
    assert not destination.exists(), "a dry run wrote to its destination"
    report = json.loads(capsys.readouterr().out)
    assert report["totals"] == {"sources": 7, "converted": 5, "current": 0, "malformed": 2,
                                "conflicts": 0}
    assert report["complete"] is False, "a malformed mandatory source blocks completeness"
    assert code == 2


def test_stage_converts_every_valid_source_and_quarantines_the_malformed(tmp_path):
    source = _source_tree(tmp_path)
    destination = tmp_path / "destination"
    assert _run("--source", source, "--destination", destination, "--stage") == 2
    assert len(_outputs(destination)) == 5
    manifest = _manifest(destination)
    assert manifest["totals"]["converted"] == 5 and manifest["totals"]["malformed"] == 2
    assert {row["source"] for row in manifest["errors"]} == {
        f"{EPISODES.as_posix()}/malformed_text.json", f"{EPISODES.as_posix()}/malformed_file.json"}
    quarantine = destination / "migration_control" / "quarantine"
    assert sorted(p.name for p in quarantine.rglob("*.json")) == ["malformed_file.json",
                                                                 "malformed_text.json"]
    assert manifest["unknown_fields"]["identity.thread_id"] == 5
    assert "outcome" in manifest["compatibility_removal_map"]


def test_a_replayed_stage_duplicates_nothing(tmp_path):
    source = _source_tree(tmp_path, CONVERTIBLE)
    destination = tmp_path / "destination"
    assert _run("--source", source, "--destination", destination, "--stage") == 0
    first, rows = _outputs(destination), _journal_rows(destination)
    assert _run("--source", source, "--destination", destination, "--stage") == 0
    assert _outputs(destination) == first
    assert _journal_rows(destination) == rows, "a replay journalled its sources again"


# ── crash and resume ────────────────────────────────────────────────────────


def test_a_crash_mid_stage_resumes_to_one_conversion_per_source(tmp_path, monkeypatch):
    migrate = _migrate()
    source = _source_tree(tmp_path, CONVERTIBLE)
    clean = tmp_path / "clean"
    assert _run("--source", source, "--destination", clean, "--stage") == 0

    destination = tmp_path / "destination"
    real = migrate._publish
    calls = {"n": 0}

    def crash_on_third(path, data):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt("injected crash during the third publication")
        return real(path, data)

    monkeypatch.setattr(migrate, "_publish", crash_on_third)
    with pytest.raises(KeyboardInterrupt):
        _run("--source", source, "--destination", destination, "--stage")
    monkeypatch.setattr(migrate, "_publish", real)
    assert 0 < len(_outputs(destination)) < 5

    assert _run("--source", source, "--destination", destination, "--resume") == 0
    assert _outputs(destination) == _outputs(clean)
    rows = _journal_rows(destination)
    assert len({row["source"] for row in rows}) == len(rows) == 5


def test_a_changed_source_blocks_resume_without_overwriting(tmp_path, capsys):
    source = _source_tree(tmp_path, CONVERTIBLE)
    destination = tmp_path / "destination"
    assert _run("--source", source, "--destination", destination, "--stage") == 0
    before = _outputs(destination)
    edited = source / EPISODES / "enriched_record.json"
    document = json.loads(edited.read_text(encoding="utf-8"))
    document["title"] = "operator edited this after staging"
    edited.write_text(json.dumps(document), encoding="utf-8")

    assert _run("--source", source, "--destination", destination, "--resume") == 1
    assert "MigrationConflict" in capsys.readouterr().err
    assert _outputs(destination) == before


def test_an_edited_destination_blocks_resume_and_is_left_as_edited(tmp_path, capsys):
    source = _source_tree(tmp_path, CONVERTIBLE)
    destination = tmp_path / "destination"
    assert _run("--source", source, "--destination", destination, "--stage") == 0
    target = next((destination / EPISODES).glob("*.json"))
    target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
    edited = target.read_bytes()

    assert _run("--source", source, "--destination", destination, "--resume") == 1
    assert "MigrationConflict" in capsys.readouterr().err
    assert target.read_bytes() == edited, "an operator edit was overwritten"


# ── verify, index exclusion and refusals ────────────────────────────────────


def test_verify_accepts_a_clean_stage_and_rejects_a_tampered_one(tmp_path):
    source = _source_tree(tmp_path, CONVERTIBLE)
    destination = tmp_path / "destination"
    assert _run("--source", source, "--destination", destination, "--stage") == 0
    assert _run("--source", source, "--destination", destination, "--verify") == 0
    target = next((destination / EPISODES).glob("*.json"))
    target.write_bytes(target.read_bytes().replace(b"episode", b"EPISODE", 1))
    assert _run("--source", source, "--destination", destination, "--verify") == 1


def test_a_derived_index_rebuilt_from_the_destination_never_sees_migration_control(tmp_path):
    from memory.engine.reindex import build_index
    from memory.projection.generation import open_generation_view, resolve_memory_root

    source = _source_tree(tmp_path)
    destination = tmp_path / "destination"
    _run("--source", source, "--destination", destination, "--stage")
    build_index(root=destination)
    view = open_generation_view(resolve_memory_root(destination))
    try:
        index = json.loads(view.read_bytes("coord_index.snapshot.json").decode("utf-8"))
    finally:
        view.close()
    converted_ids = {json.loads(blob)["id"] for blob in _outputs(destination).values()}
    assert set(index) == converted_ids
    assert not any("migration_control" in str(entry.get("path") or "") for entry in index.values())


def test_the_live_repository_is_never_a_destination(tmp_path, capsys):
    from core.paths import OMEGA_ROOT

    source = _source_tree(tmp_path, CONVERTIBLE)
    assert _run("--source", source, "--destination", Path(OMEGA_ROOT), "--stage") == 1
    assert "live" in capsys.readouterr().err.lower()


def test_there_is_no_default_source(tmp_path):
    with pytest.raises(SystemExit):
        _run("--destination", tmp_path / "destination", "--stage")


def test_a_non_episode_document_in_the_source_is_ignored_and_listed(tmp_path):
    source = _source_tree(tmp_path, ("enriched_record",))
    consolidation = source / "memory" / "900_temporal" / "history" / "consolidation"
    consolidation.mkdir(parents=True)
    document = json.loads((FIXTURES / "enriched_record.json").read_text(encoding="utf-8"))
    document.update(id="9.03.c0501da7", coord=[9, 3, 0], type="consolidation")
    (consolidation / "9.03.c0501da7.json").write_text(json.dumps(document), encoding="utf-8")

    destination = tmp_path / "destination"
    assert _run("--source", source, "--destination", destination, "--stage") == 0
    manifest = _manifest(destination)
    assert manifest["totals"]["sources"] == 1 and manifest["totals"]["converted"] == 1
    assert manifest["ignored"] == ["memory/900_temporal/history/consolidation/9.03.c0501da7.json"]
    assert len(_outputs(destination)) == 1
