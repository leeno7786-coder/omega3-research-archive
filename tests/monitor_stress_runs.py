#!/usr/bin/env python3
"""
monitor_stress_runs.py — summarize matrix JSON emitted by run_full_matrix.py

Reads `.omega_data/stress_runs/*/matrix.json`, prints per-cycle pass/fail counts
and lists failed scripts so operators can track schema/flow regressions during
long stress loops.

Examples:
  python tests/monitor_stress_runs.py
  python tests/monitor_stress_runs.py --last 5
  python tests/monitor_stress_runs.py --watch 60
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
import logging

log = logging.getLogger("omega.tests.monitor_stress_runs")


ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
STRESS_ROOT = ROOT / ".omega_data" / "stress_runs"


def _runs() -> list[Path]:
    if not STRESS_ROOT.is_dir():
        return []
    dirs = [p for p in STRESS_ROOT.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.name)
    return dirs


def _summarize_one(run_dir: Path) -> dict | None:
    jf = run_dir / "matrix.json"
    if not jf.exists():
        return None
    doc = json.loads(jf.read_text(encoding="utf-8"))
    scripts = doc.get("scripts") or []
    failed = [s for s in scripts if not s.get("ok", True)]
    return {
        "id": run_dir.name,
        "wall": doc.get("wall_seconds"),
        "files_failed": doc.get("files_failed", len(failed)),
        "scripts": len(scripts),
        "parsed_pass": doc.get("totals_parsed_pass"),
        "parsed_fail": doc.get("totals_parsed_fail"),
        "failed_names": [s.get("file", "?") for s in failed],
    }


def _print_banner(rows: list[dict]) -> None:
    print("=" * 72)
    print(f"Stress runs under {STRESS_ROOT}  (cycles: {len(rows)})")
    print("=" * 72)
    for r in rows:
        print(
            f"  {r['id']}:  failed_files={r['files_failed']}/{r['scripts']} "
            f"wall={r['wall']}s "
            f"parsed_PASS={r['parsed_pass']} parsed_FAIL={r['parsed_fail']}"
        )
        if r["failed_names"]:
            for fn in r["failed_names"][:40]:
                print(f"       - {fn}")
            if len(r["failed_names"]) > 40:
                print(f"       … +{len(r['failed_names']) - 40} more")
    print("=" * 72)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Monitor stress_matrix JSON artifacts.")
    ap.add_argument("--last", type=int, default=8, metavar="N", help="Show last N runs")
    ap.add_argument(
        "--watch",
        type=int,
        default=0,
        metavar="SEC",
        help="Poll every SEC seconds until Ctrl+C (0 = single shot)",
    )
    args = ap.parse_args(argv)

    def once() -> int:
        all_dirs = _runs()
        if not all_dirs:
            print(f"No stress runs found under {STRESS_ROOT}")
            return 1
        tail = all_dirs[-max(1, args.last) :]
        rows = []
        for d in tail:
            s = _summarize_one(d)
            if s:
                rows.append(s)
            else:
                rows.append(
                    {
                        "id": d.name,
                        "wall": None,
                        "files_failed": -1,
                        "scripts": 0,
                        "parsed_pass": None,
                        "parsed_fail": None,
                        "failed_names": [],
                    }
                )
                print(f"[pending] {d.name}: matrix.json not yet written")
        _print_banner(rows)
        last_ff = rows[-1].get("files_failed")
        # -1 = matrix.json missing (run still in flight)
        fatal = isinstance(last_ff, int) and last_ff > 0
        return 1 if fatal else 0

    if args.watch <= 0:
        return once()

    try:
        while True:
            rc = once()
            time.sleep(max(5, args.watch))
    except KeyboardInterrupt:
        print("\nmonitor: stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
