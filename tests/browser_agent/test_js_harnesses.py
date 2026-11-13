"""The browser-agent JavaScript integration harness runs in the pytest sweep.

Nothing the sweep executes ran it (found 2026-09-15). The Edge extension's own bun unit
tests used to run here too; the extension now lives in its own repository
(``omega-browser-agent``, removed from this tree by 0bb5f208), and its unit tests went with
its source.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_integration_harness_passes_under_node() -> None:
    result = _run(["node", str(ROOT / "tests" / "browser_agent" / "run_all.mjs")], ROOT)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-4000:]
    assert "tests passed" in output, output[-2000:]
