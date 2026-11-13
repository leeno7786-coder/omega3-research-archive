"""Cut 5A Gate-D1 fixture helpers (plan Task 1a)."""
from __future__ import annotations

import shutil
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
FIXTURE_ROOT = FIXTURE_DIR / "root"


def copy_fixture_root(dest: Path) -> Path:
    """Copy the committed fixture root into ``dest`` (a tmp dir) and return the
    compile root. Compiling writes derived state into the root; the committed
    fixture must never be compiled in place."""
    root = Path(dest) / "cut5a_root"
    shutil.copytree(FIXTURE_ROOT, root)
    return root


def fixture_files() -> dict[str, bytes]:
    """{relative path: bytes} for every committed data file (records + coord_index)."""
    out: dict[str, bytes] = {}
    for p in sorted(FIXTURE_ROOT.rglob("*.json")):
        out[p.relative_to(FIXTURE_DIR).as_posix()] = p.read_bytes()   # committed baseline keys are POSIX-style
    return out
