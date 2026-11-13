"""Seed lobe 000_identity constitution records from omega loop doctrine.

Idempotent: skips seeds already present in coord_index. Traits are NOT seeded —
they are written by omega_loop reflection → deepsleep identity_store.

Usage:
    .\\.venv-brain\\Scripts\\python.exe scripts\\seed_identity_lobe.py
    .\\.venv-brain\\Scripts\\python.exe scripts\\seed_identity_lobe.py --force   # rewrite missing only (default)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _existing_ids() -> set[str]:
    from memory.engine.walker import coord_index_snapshot, load_coord_index

    load_coord_index()
    ids: set[str] = set()
    for _mid, entry in coord_index_snapshot().items():
        coord = entry.get("coord") or []
        if coord and int(coord[0]) == 0 and int(coord[1]) == 0:
            rid = str(entry.get("id") or _mid)
            ids.add(rid)
    return ids


def seed_constitution(*, dry_run: bool = False) -> dict:
    from memory.identity.seeds import CONSTITUTION_SEEDS, seed_envelope
    from memory.api import write_memory

    existing = _existing_ids()
    created = 0
    skipped = 0
    for seed in CONSTITUTION_SEEDS:
        if seed.record_id in existing:
            skipped += 1
            continue
        if dry_run:
            created += 1
            continue
        envelope = seed_envelope(seed)
        receipt = write_memory(envelope, propose=False, bypass_guardian=True)
        if getattr(receipt, "validation_status", None) == "committed":
            created += 1
            existing.add(seed.record_id)
        else:
            print(f"  FAIL {seed.record_id}: {getattr(receipt, 'message', receipt)}")
    return {"created": created, "skipped": skipped, "total": len(CONSTITUTION_SEEDS)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed 000_identity constitution from omega loop")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no writes")
    args = parser.parse_args()

    print("Seeding 000_identity/constitution from omega loop doctrine...")
    result = seed_constitution(dry_run=args.dry_run)
    print(f"  created: {result['created']}  skipped: {result['skipped']}  total: {result['total']}")
    print("  traits: not seeded (written by omega_loop → deepsleep identity_store)")


if __name__ == "__main__":
    main()
