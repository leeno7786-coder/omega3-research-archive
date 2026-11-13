"""
test_legacy_retirement.py -- brain package fully retired

Asserts the legacy `brain/` tree is gone from the converged Symphony
substrate. Run as a plain script or via pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_brain_package_retired() -> None:
    brain_dir = ROOT / "brain"
    assert not brain_dir.exists(), f"legacy brain/ still present at {brain_dir}"

    # Do not `import brain` here — tests/brain/ shadows that name on sys.path.


def main() -> int:
    print("=" * 72)
    print("Legacy brain package retirement")
    print("=" * 72)
    try:
        test_brain_package_retired()
        print("  [OK ] brain/ package directory absent")
        print("=" * 72)
        return 0
    except AssertionError as exc:
        print(f"  [FAIL] {exc}")
        print("=" * 72)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
