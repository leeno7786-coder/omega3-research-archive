"""Remove test/MagicMock pollution from live memory, reindex, prune strength orphans."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

POLLUTION_MARKERS = (
    "MagicMock",
    "pytest-of-",
    "AppData\\Local\\Temp\\",
    "/Temp/pytest-of-",
)


def _is_polluted(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(marker in text for marker in POLLUTION_MARKERS)


def _find_polluted(memory_root: Path) -> list[Path]:
    hits: list[Path] = []
    for path in memory_root.rglob("*.json"):
        if "indexes" in path.parts:
            continue
        if _is_polluted(path):
            hits.append(path)
    return sorted(hits)


def _prune_strength_orphans(*, strength_path: Path, index_path: Path, execute: bool) -> int:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    valid_ids = set(index.keys())
    data = json.loads(strength_path.read_text(encoding="utf-8"))
    records = dict(data.get("records") or {})
    orphans = sorted(k for k in records if k not in valid_ids)
    if execute and orphans:
        backup = strength_path.with_suffix(
            strength_path.suffix
            + f".PRE_PRUNE.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        shutil.copy2(strength_path, backup)
        for oid in orphans:
            records.pop(oid, None)
        data["records"] = records
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        strength_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return len(orphans)


def main() -> int:
    from core.paths import OMEGA_ROOT
    from memory.engine.reindex import build_index

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Delete polluted files and repair indexes (default: dry-run)",
    )
    args = parser.parse_args()

    memory_root = OMEGA_ROOT / "memory"
    index_path = memory_root / "indexes" / "coord_index.json"
    strength_path = OMEGA_ROOT / "guardian" / "state" / "strength_index.json"

    polluted = _find_polluted(memory_root)
    print(f"polluted_files={len(polluted)} execute={args.execute}")
    for path in polluted:
        print(f"  {path.relative_to(OMEGA_ROOT)}")

    if args.execute:
        for path in polluted:
            path.unlink(missing_ok=True)
        print("reindex...")
        build_index()
        orphans = _prune_strength_orphans(
            strength_path=strength_path,
            index_path=index_path,
            execute=True,
        )
        print(f"strength_orphans_pruned={orphans}")
    else:
        orphans = _prune_strength_orphans(
            strength_path=strength_path,
            index_path=index_path,
            execute=False,
        )
        print(f"strength_orphans_pending={orphans} (run with --execute after delete+reindex)")

    remaining = _find_polluted(memory_root)
    if args.execute:
        print(f"remaining_polluted={len(remaining)}")
        if remaining:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
