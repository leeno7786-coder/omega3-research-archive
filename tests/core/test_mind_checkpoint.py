"""Phase 0c — reversibility checkpoint/restore for Omega's live mind state.

Every test runs against an INJECTED tmp omega_root; none touch the real
guardian/state, memory lobes, or deepsleep/pressure state.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mind_state_surface import is_excluded, mind_state_files  # noqa: E402
from guardian.mind_checkpoint import (  # noqa: E402
    checkpoint_mind_state,
    list_checkpoints,
    prune_checkpoints,
    restore_mind_state,
    verify_checkpoint,
)


def _seed_state(root: Path, *, mastery: str = "0.5", note: str = "hello") -> None:
    """Lay down a minimal but representative live-mind state under root."""
    gs = root / "guardian" / "state"
    gs.mkdir(parents=True, exist_ok=True)
    (gs / "mastery.json").write_text(json.dumps({"python": mastery}), encoding="utf-8")
    ds = root / "deepsleep_loop" / "state"
    ds.mkdir(parents=True, exist_ok=True)
    (ds / "trajectory.json").write_text(json.dumps({"state": "ESTABLISHING"}), encoding="utf-8")
    lobe = root / "memory" / "000_identity"
    lobe.mkdir(parents=True, exist_ok=True)
    (lobe / "0.00.omega_identity.json").write_text(json.dumps({"note": note}), encoding="utf-8")


def _manifest(cp) -> dict:
    return json.loads((Path(cp.checkpoint_root) / "MANIFEST.json").read_text(encoding="utf-8"))


def _rel_files(cp) -> list[str]:
    return [f["path"] for f in _manifest(cp)["files"]]


def test_checkpoint_then_restore_round_trips_state(tmp_path):
    _seed_state(tmp_path, mastery="0.5", note="hello")
    cp = checkpoint_mind_state(omega_root=tmp_path, label="pre-apply")

    # A rogue APPLY corrupts mastery and an identity seed.
    (tmp_path / "guardian" / "state" / "mastery.json").write_text(
        json.dumps({"python": "9.9"}), encoding="utf-8"
    )
    (tmp_path / "memory" / "000_identity" / "0.00.omega_identity.json").write_text(
        "CORRUPTED", encoding="utf-8"
    )

    report = restore_mind_state(cp.checkpoint_id, omega_root=tmp_path)
    assert report.ok
    assert (
        json.loads((tmp_path / "guardian" / "state" / "mastery.json").read_text())["python"]
        == "0.5"
    )
    assert (
        json.loads((tmp_path / "memory" / "000_identity" / "0.00.omega_identity.json").read_text())[
            "note"
        ]
        == "hello"
    )


def test_restore_removes_files_created_after_checkpoint(tmp_path):
    _seed_state(tmp_path)
    cp = checkpoint_mind_state(omega_root=tmp_path)

    rogue = tmp_path / "memory" / "200_knowledge" / "rogue.json"
    rogue.parent.mkdir(parents=True, exist_ok=True)
    rogue.write_text("{}", encoding="utf-8")

    restore_mind_state(cp.checkpoint_id, omega_root=tmp_path)
    assert not rogue.exists()


def test_checkpoint_excludes_append_only_and_transient(tmp_path):
    _seed_state(tmp_path)
    (tmp_path / "guardian" / "state" / "encoding_journal.jsonl").write_text(
        '{"x":1}\n', encoding="utf-8"
    )
    tw = tmp_path / "memory" / "tmp_writes"
    tw.mkdir(parents=True, exist_ok=True)
    (tw / "abc.tmp").write_text("staging", encoding="utf-8")

    cp = checkpoint_mind_state(omega_root=tmp_path)
    files = _rel_files(cp)
    assert not any(f.endswith(".jsonl") for f in files)
    assert not any("tmp_writes" in f for f in files)
    assert any(f.endswith("mastery.json") for f in files)


def test_checkpoint_on_absent_state_is_valid_and_empty(tmp_path):
    # Fresh clone / first boot: no state dirs exist at all.
    cp = checkpoint_mind_state(omega_root=tmp_path)
    assert cp.file_count == 0
    assert verify_checkpoint(cp.checkpoint_id, omega_root=tmp_path)


def test_checkpoints_do_not_capture_themselves(tmp_path):
    _seed_state(tmp_path)
    checkpoint_mind_state(omega_root=tmp_path, checkpoint_id="cp-00")
    cp2 = checkpoint_mind_state(omega_root=tmp_path, checkpoint_id="cp-01")
    assert not any("checkpoints" in f for f in _rel_files(cp2))


def test_prune_keeps_newest_n(tmp_path):
    _seed_state(tmp_path)
    for i in range(5):
        checkpoint_mind_state(omega_root=tmp_path, checkpoint_id=f"cp-{i:02d}", keep=99)
    prune_checkpoints(keep=2, omega_root=tmp_path)
    remaining = sorted(list_checkpoints(omega_root=tmp_path))
    assert remaining == ["cp-03", "cp-04"]


def test_checkpoint_auto_prunes_to_keep(tmp_path):
    _seed_state(tmp_path)
    for i in range(4):
        checkpoint_mind_state(omega_root=tmp_path, checkpoint_id=f"cp-{i:02d}", keep=2)
    assert len(list_checkpoints(omega_root=tmp_path)) == 2


def test_verify_detects_tampering(tmp_path):
    _seed_state(tmp_path)
    cp = checkpoint_mind_state(omega_root=tmp_path)
    tampered = next(Path(cp.checkpoint_root).rglob("mastery.json"))
    tampered.write_text("TAMPERED", encoding="utf-8")
    assert verify_checkpoint(cp.checkpoint_id, omega_root=tmp_path) is False


def test_surface_excludes_logs_and_factory_preserve():
    assert is_excluded("guardian/state/encoding_journal.jsonl")
    assert is_excluded("guardian/logs/run.log")
    assert is_excluded("memory/tmp_writes/x.tmp")
    assert is_excluded("memory/indexes/label_map.json")
    assert not is_excluded("guardian/state/mastery.json")
    assert not is_excluded("memory/000_identity/0.00.omega_identity.json")


def test_the_browser_profile_is_not_mind_state(tmp_path):
    """The Edge automation profile lives under guardian/state but is browser data.

    Checkpointing it copied deep extension paths past Windows' 260-character limit
    on machines without long-path support, failing every checkpoint (and so every
    write-type skill). A checkpoint neither captures it nor rolls it back.
    """
    _seed_state(tmp_path)
    extension = (
        tmp_path / "guardian" / "state" / "browser" / "edge-debug-profile" / "Default"
        / "Extensions" / "abcdefghijklmnop" / "1.0_0" / "assets" / "panel.js"
    )
    extension.parent.mkdir(parents=True)
    extension.write_text("v1", encoding="utf-8")

    assert is_excluded("guardian/state/browser/edge-debug-profile/Default/Preferences")
    assert extension not in mind_state_files(tmp_path)
    cp = checkpoint_mind_state(omega_root=tmp_path)
    assert not any(path.startswith("guardian/state/browser/") for path in _rel_files(cp))

    extension.write_text("v2", encoding="utf-8")
    assert restore_mind_state(cp.checkpoint_id, omega_root=tmp_path).ok
    assert extension.read_text(encoding="utf-8") == "v2"


def test_operator_chat_lane_updates_are_not_restored_with_mind(tmp_path):
    """Live operator-facing lane updates are delivery state, not mind state."""
    _seed_state(tmp_path)
    updates = tmp_path / "guardian" / "state" / "operator_chat_lane_updates.json"
    updates.write_text('{"version":1,"updates":[],"acknowledged":{}}', encoding="utf-8")

    assert is_excluded("guardian/state/operator_chat_lane_updates.json")
    assert updates not in mind_state_files(tmp_path)
    checkpoint = checkpoint_mind_state(omega_root=tmp_path)
    assert "guardian/state/operator_chat_lane_updates.json" not in _rel_files(checkpoint)

    updates.write_text('{"version":1,"updates":[{"seq":1}],"acknowledged":{}}', encoding="utf-8")
    assert restore_mind_state(checkpoint.checkpoint_id, omega_root=tmp_path).ok
    assert '"seq":1' in updates.read_text(encoding="utf-8")


def test_surface_only_lists_in_scope_files(tmp_path):
    _seed_state(tmp_path)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "pressure.yaml").write_text("x: 1", encoding="utf-8")
    rels = {p.relative_to(tmp_path).as_posix() for p in mind_state_files(tmp_path)}
    assert "guardian/state/mastery.json" in rels
    assert "memory/000_identity/0.00.omega_identity.json" in rels
    assert "config/pressure.yaml" not in rels  # config is not mind state
