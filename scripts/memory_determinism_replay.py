"""Run the Memory Engine v2 deterministic ingest/recall replay harness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.determinism_replay import run_determinism_replay  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="Optional output directory for replay roots. Existing content is replaced.",
    )
    parser.add_argument(
        "--keep-roots",
        action="store_true",
        help="Keep temporary replay roots when --base-dir is omitted.",
    )
    args = parser.parse_args(argv)

    report = run_determinism_replay(
        base_dir=args.base_dir,
        keep_roots=args.keep_roots,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
