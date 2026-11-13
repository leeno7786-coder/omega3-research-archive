"""Day-zero must wipe guardian/checkpoints/ (RED → GREEN).

Mind-state checkpoints (guardian/mind_checkpoint) are full snapshots of prior
guardian/state + memory + loop state, kept as restore points. They live as a
SIBLING of guardian/state (so a checkpoint never captures itself) and therefore
survive the complete-state wipe. A factory reset must drop them — otherwise
restore_mind_state(<old_id>) could resurrect the wiped mind.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.day_zero_reset as day_zero_reset  # noqa: E402


def _seed_checkpoint(omega: Path, cid: str) -> Path:
    cp = omega / "guardian" / "checkpoints" / cid
    state = cp / "guardian" / "state"
    state.mkdir(parents=True)
    (state / "boot.manifest.json").write_text('{"boot": "stale"}', encoding="utf-8")
    (state / "substrate_snapshot.json").write_text('{"snap": "stale"}', encoding="utf-8")
    (cp / "MANIFEST.json").write_text('{"checkpoint_id": "' + cid + '"}', encoding="utf-8")
    return cp


def test_wipe_checkpoints_removes_all_restore_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omega = tmp_path / "omega"
    cp1 = _seed_checkpoint(omega, "20260101T000000_000000Z")
    cp2 = _seed_checkpoint(omega, "20260102T000000_000000Z")

    monkeypatch.setattr(day_zero_reset, "OMEGA_ROOT", omega)
    result = day_zero_reset.wipe_checkpoints()

    assert not cp1.exists()
    assert not cp2.exists()
    assert result["removed_checkpoints"] == 2
    cp_root = omega / "guardian" / "checkpoints"
    assert not cp_root.exists() or list(cp_root.iterdir()) == []


def test_wipe_checkpoints_absent_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    omega = tmp_path / "omega"
    omega.mkdir()
    monkeypatch.setattr(day_zero_reset, "OMEGA_ROOT", omega)
    result = day_zero_reset.wipe_checkpoints()
    assert result["removed_checkpoints"] == 0


def test_main_clears_checkpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: a full reset leaves no restore points behind."""
    # Reuse the integration isolation from the proper-clear test.
    from tests.scripts.test_day_zero_proper_clear import (
        _patch_isolated_reset_roots,
        _seed_contamination,
    )

    iso_root = _patch_isolated_reset_roots(tmp_path, monkeypatch)
    _seed_contamination(iso_root)
    _seed_checkpoint(iso_root, "20260103T000000_000000Z")

    day_zero_reset.main()

    cp_root = iso_root / "guardian" / "checkpoints"
    assert not cp_root.exists() or list(cp_root.iterdir()) == [], (
        "guardian/checkpoints must hold no restore points after factory reset"
    )
