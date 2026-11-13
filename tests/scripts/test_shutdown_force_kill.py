"""Force-kill shutdown must retain boot lock."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.shutdown as shutdown  # noqa: E402


@pytest.fixture
def lock_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "boot.lock"
    monkeypatch.setattr(shutdown, "BOOT_LOCK_PATH", p)
    return p


def test_shutdown_retains_lock_when_send_stop_force_killed(
    lock_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path.write_text(
        json.dumps({"pid": 5555, "create_time": 2.0, "acquired_at": "x"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(shutdown, "_pid_alive", lambda pid, _ct=None: pid == 5555)
    monkeypatch.setattr(shutdown, "_verify_boot_owner", lambda pid: pid == 5555)

    result = shutdown.shutdown_substrate(send_stop=lambda _pid: False)
    assert result.status == "stop_failed"
    assert result.lock_cleared is False
    assert lock_path.exists()


def test_verify_boot_owner_fails_closed_without_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutdown, "_psutil_module", lambda: None)
    assert shutdown._verify_boot_owner(1234) is False
