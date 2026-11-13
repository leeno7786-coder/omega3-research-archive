"""One-off cleanup (2026-09-05): remove the lobe-4 ``pressure_ignition`` situational
records. That event kind is pipeline plumbing (the ignition already rides inside the
lobe-9 episode record) and ``memory.taxonomy.canonical_capture`` no longer mints it
(``LOG_ONLY_EVENT_KINDS``); this drops the ~50 that were minted before the fix.

Run with the brain STOPPED, from the repo root, in the brain venv:

    .venv-brain\\Scripts\\python.exe scratchpad\\remove_pressure_ignition_records.py

Deleted files are copied to ``memory/quarantine/pressure_ignition_<timestamp>/`` first,
so the step is reversible. Afterwards the coord index is rebuilt and the projection is
marked rebuild-required, so the next DeepSleep publishes a clean DME generation.
Pass ``--dry-run`` to only list what would be removed.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SITUATIONAL = ROOT / "memory" / "400_situational"


def _targets() -> list[Path]:
    out: list[Path] = []
    for path in sorted(SITUATIONAL.rglob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if record.get("type") != "situational":
            continue
        if (record.get("enrichment") or {}).get("event_kind") != "pressure_ignition":
            continue
        out.append(path)
    return out


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    targets = _targets()
    print(f"{len(targets)} pressure_ignition situational record(s) under {SITUATIONAL}")
    for path in targets:
        print("  ", path.relative_to(ROOT))
    if dry_run or not targets:
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = ROOT / "memory" / "quarantine" / f"pressure_ignition_{stamp}"
    quarantine.mkdir(parents=True, exist_ok=True)
    for path in targets:
        shutil.copy2(path, quarantine / path.name)
        path.unlink()
    print(f"moved {len(targets)} file(s) to {quarantine.relative_to(ROOT)}")

    # drop now-empty leaf directories (e.g. 400_situational/when/pressure_ignition)
    for directory in sorted(SITUATIONAL.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
            print("removed empty dir", directory.relative_to(ROOT))

    from memory.engine.reindex import build_index
    from memory.projection_delta import mark_projection_dirty, projection_dirty_state

    build_index()
    index = json.loads((ROOT / "memory" / "indexes" / "coord_index.json").read_text(encoding="utf-8"))
    removed_ids = {p.stem for p in targets}
    print("coord_index rebuilt:", len(index), "entries;", len(removed_ids & set(index)), "removed id(s) still indexed")
    mark_projection_dirty(
        reason=f"removed {len(targets)} pressure_ignition situational records (log-only event kind)",
        memory_id="",
    )
    print("projection:", projection_dirty_state())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
