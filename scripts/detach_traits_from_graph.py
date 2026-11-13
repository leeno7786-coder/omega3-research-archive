"""Detach the identity trait family from the embed/graph path.

Operator ruling 2026-08-10: traits are a dynamic pattern-voting system —
patterns match, observations accumulate, a trait is voted onto the
filesystem under identity, and the omega fold appends it to the chat
templates for personality. They never belonged in the semantic graph.

memory.encoding now bypasses the encode pipeline for NON_SEMANTIC_TYPES;
this script revokes the citizenship EXISTING trait records already hold:

  1. strip ``embeddings`` blocks from every trait-family record (the file
     and its coord-index entry remain — the voting system reads those);
  2. delete their projection receipts (claim citizenship revoked);
   3. republish the graph/DME projection so recall stops serving them.

Run with the brain DOWN. Idempotent.

Usage:
    .\\.venv-brain\\Scripts\\python.exe scripts\\detach_traits_from_graph.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HUD_PROBE = "http://127.0.0.1:13310/"


def _brain_is_up() -> bool:
    try:
        with urllib.request.urlopen(HUD_PROBE, timeout=2):
            return True
    except Exception:
        return False


def _is_trait_family(record: dict) -> bool:
    from memory.encoding import NON_SEMANTIC_TYPES

    rtype = str(record.get("type") or "").strip().lower()
    if rtype in NON_SEMANTIC_TYPES:
        return True
    rid = str(record.get("id") or "")
    return rid.startswith("trait.") or rid.startswith("trait-observation.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = parser.parse_args()

    if _brain_is_up():
        print("REFUSED: the brain is serving — stop it before detaching traits.")
        return 2

    memory_root = ROOT / "memory"
    receipts_dir = memory_root / "source_ledger" / "projection_receipts"

    stripped: list[str] = []
    receipts_deleted: list[str] = []

    for path in sorted(memory_root.rglob("*.json")):
        if receipts_dir in path.parents:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict) or not _is_trait_family(record):
            continue
        rid = str(record.get("id") or path.stem)
        if record.get("embeddings"):
            if not args.dry_run:
                record.pop("embeddings", None)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(
                    json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, path)
            stripped.append(rid)
        receipt = receipts_dir / f"{rid}.json"
        if receipt.is_file():
            if not args.dry_run:
                receipt.unlink()
            receipts_deleted.append(rid)

    print(f"embeddings stripped: {len(stripped)}")
    for r in stripped:
        print(f"  - {r}")
    print(f"projection receipts deleted: {len(receipts_deleted)}")

    if args.dry_run:
        print("dry run — nothing written")
        return 0

    from memory.engine.reindex import reindex_and_compile

    reindex_and_compile()
    print("graph/DME projection republished — traits detached")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
