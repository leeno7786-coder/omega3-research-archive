"""Day-zero reset reconcile — registry owns tools, telemetry firehose wiped."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.day_zero_reset as day_zero_reset  # noqa: E402
import memory.engine.validate_write as validate_write  # noqa: E402
from memory.identity.seeds import CONSTITUTION_SEEDS  # noqa: E402

# The full founding constitution set day-zero re-seeds (single source of truth).
_CONSTITUTION_IDS = {s.record_id for s in CONSTITUTION_SEEDS}


def _patch_isolated_reset_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point day_zero_reset at an isolated tmp tree.

    This deliberately reuses the proper-clear suite's helper rather than
    keeping a second copy. A partial copy lived here until 2026-09-06 and
    left ``LEDGER_ROOT``, ``core.paths``, the memory walker and the deepsleep
    policy paths bound to the real installation, so running this test executed
    a genuine factory reset against the operator's own ``guardian/ledger``,
    ``memory/taxonomy_store``, ``knowledge_extractor/state`` and episode
    journal. One helper, one blast radius.
    """
    from tests.scripts.test_day_zero_proper_clear import (
        _patch_isolated_reset_roots as _isolate,
    )

    return _isolate(tmp_path, monkeypatch)


def _seed_contamination(iso_root: Path) -> None:
    """Pre-reset junk: stale memory, coord index, telemetry firehose."""
    stale_memory = iso_root / "memory" / "200_knowledge" / "general" / "stale.json"
    stale_memory.parent.mkdir(parents=True, exist_ok=True)
    stale_memory.write_text('{"id":"2.00.0001"}', encoding="utf-8")

    tools_dir = iso_root / "memory" / "500_tools" / "cognitive"
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / "web_fetch.json").write_text('{"id":"5.00.0001"}', encoding="utf-8")

    receipts = iso_root / "memory" / "source_ledger" / "projection_receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / "stale.json").write_text('{"memory_id":"stale"}', encoding="utf-8")

    coord_index = iso_root / "memory" / "indexes" / "coord_index.json"
    coord_index.write_text(
        json.dumps(
            {
                "stale": {"coord": [2, 0, 1]},
                "tool": {"coord": [5, 0, 1]},
            }
        ),
        encoding="utf-8",
    )

    telemetry = iso_root / "guardian" / "telemetry"
    (telemetry / "firehose.jsonl").write_text('{"event":"stale"}\n', encoding="utf-8")
    (telemetry / "terminal.log").write_text("stale log\n", encoding="utf-8")
    (telemetry / "writer.py").write_text("# telemetry package sentinel\n", encoding="utf-8")


def _memory_json_files(iso_root: Path) -> list[Path]:
    memory_root = iso_root / "memory"
    return [
        p
        for p in memory_root.rglob("*.json")
        if "indexes" not in p.parts
        and "source_ledger" not in p.parts
        and p.name not in ("strength_index.json",)
    ]


def test_day_zero_reset_leaves_identity_only_corpus_and_wipes_telemetry_firehose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    iso_root = _patch_isolated_reset_roots(tmp_path, monkeypatch)
    _seed_contamination(iso_root)

    day_zero_reset.main()

    memory_files = _memory_json_files(iso_root)
    assert len(memory_files) == len(CONSTITUTION_SEEDS)

    for path in memory_files:
        meta = json.loads(path.read_text(encoding="utf-8"))
        assert meta["coord"][0] == 0
        assert meta["id"] in _CONSTITUTION_IDS

    tools_tree = iso_root / "memory" / "500_tools"
    tool_jsons = list(tools_tree.rglob("*.json")) if tools_tree.exists() else []
    assert tool_jsons == []

    coord_index = json.loads(
        (iso_root / "memory" / "indexes" / "coord_index.json").read_text(encoding="utf-8")
    )
    assert len(coord_index) == len(CONSTITUTION_SEEDS)
    assert all(entry["coord"][0] == 0 for entry in coord_index.values())

    telemetry = iso_root / "guardian" / "telemetry"
    assert not (telemetry / "firehose.jsonl").exists()
    assert not (telemetry / "terminal.log").exists()
    assert (telemetry / "writer.py").exists()
    assert not (iso_root / "memory" / "source_ledger" / "projection_receipts").exists()
