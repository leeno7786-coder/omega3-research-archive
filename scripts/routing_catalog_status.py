"""Report production routing edge-case catalog (_unsorted pile) for Phase 8 go/no-go."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from memory.routing.edge_case_catalog import catalog_path, load_entries


def _normalize(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def main() -> int:
    path = catalog_path()
    rows = load_entries(path)
    unique = {_normalize(r.get("text", "")) for r in rows if r.get("text")}
    by_lobe = Counter(int(r["lobe"]) for r in rows if r.get("lobe") is not None)

    print(f"catalog_path={path}")
    print(f"exists={path.is_file()}")
    print(f"total_rows={len(rows)}")
    print(f"unique_texts={len(unique)}")
    print(f"by_lobe={dict(sorted(by_lobe.items()))}")

    if rows:
        ts_vals = [r.get("ts", "") for r in rows if r.get("ts")]
        if ts_vals:
            print(f"oldest_ts={min(ts_vals)}")
            print(f"newest_ts={max(ts_vals)}")

    # Phase 8 quick verdict (production-only heuristics)
    if len(unique) < 10:
        print("phase8_verdict=NO-GO (catalog too small — observe production)")
    elif len(unique) >= 50 and len(by_lobe) >= 3:
        print("phase8_verdict=REVIEW (catalog may warrant LFM — operator sign-off required)")
    else:
        print("phase8_verdict=OBSERVE (accumulating — re-check weekly)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
