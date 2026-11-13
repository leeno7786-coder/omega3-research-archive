"""B6 — factory reset must wipe all guardian/state residue (reflection window, etc.)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.day_zero_reset as day_zero_reset  # noqa: E402


def test_wipe_guardian_state_completely_removes_reflection_lease_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "guardian" / "state"
    state_root.mkdir(parents=True)
    lease_path = state_root / "reflection_lease.json"
    lease_path.write_text('{"window_id": "stale-lease"}', encoding="utf-8")

    monkeypatch.setattr(day_zero_reset, "GUARDIAN_STATE_ROOT", state_root)

    result = day_zero_reset.wipe_guardian_state_completely()

    assert not lease_path.exists()
    assert result["removed_files"] == 1


def test_wipe_guardian_state_completely_removes_reflection_window_and_subtrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "guardian" / "state"
    hud = state_root / "hud" / "settings.json"
    hud.parent.mkdir(parents=True)
    hud.write_text('{"name": "stale"}', encoding="utf-8")
    window_path = state_root / "reflection_window.json"
    window_path.write_text('{"window_id": "stale"}', encoding="utf-8")

    monkeypatch.setattr(day_zero_reset, "GUARDIAN_STATE_ROOT", state_root)

    result = day_zero_reset.wipe_guardian_state_completely()

    assert not window_path.exists()
    assert not hud.exists()
    assert list(state_root.iterdir()) == []
    assert result["removed_files"] == 1
    assert result["removed_dirs"] == 1


def test_wipe_guardian_ledger_removes_key_chain_and_activated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_root = tmp_path / "guardian" / "ledger"
    ledger_root.mkdir(parents=True)
    (ledger_root / "key.bin").write_bytes(b"OLD-KEY")
    (ledger_root / "chain.jsonl").write_text("{}\n", encoding="utf-8")
    (ledger_root / ".activated").write_text("1", encoding="utf-8")

    monkeypatch.setattr(day_zero_reset, "LEDGER_ROOT", ledger_root)

    removed = day_zero_reset.wipe_guardian_ledger()

    assert set(removed) == {"key.bin", "chain.jsonl", ".activated"}
    assert list(ledger_root.iterdir()) == []
