"""Isolated pytest/standalone gate for the operator-presence test surface."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
SCRIPT = Path(__file__).with_name("presence_test_surface_script.py")


def _run_surface() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_presence_surface_contract() -> None:
    """Run every original assertion without patching this pytest process."""
    parent_presence_module = sys.modules.get("hud.presence_test")

    result = _run_surface()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "TOTAL  PASS=31  FAIL=0" in result.stdout
    assert sys.modules.get("hud.presence_test") is parent_presence_module


if __name__ == "__main__":
    completed = _run_surface()
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    raise SystemExit(completed.returncode)
