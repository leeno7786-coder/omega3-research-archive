"""Every HUD node harness (tests/hud/test_*.mjs) runs in the pytest sweep.

Half of these scripts had no pytest wrapper, so a HUD regression they would catch never
failed a sweep (found 2026-09-15: 9 of 18 unwired). Each harness exits non-zero on failure.
Some also have a dedicated wrapper beside their Python tests; running them here too costs
well under a second each and keeps a new harness from being silently skipped.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESSES = sorted((ROOT / "tests" / "hud").glob("test_*.mjs"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("script", HARNESSES, ids=lambda path: path.name)
def test_hud_node_harness_passes(script: Path) -> None:
    result = subprocess.run(["node", str(script)], cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120)
    assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]


def test_the_harness_list_is_not_empty() -> None:
    assert len(HARNESSES) >= 18, [path.name for path in HARNESSES]
